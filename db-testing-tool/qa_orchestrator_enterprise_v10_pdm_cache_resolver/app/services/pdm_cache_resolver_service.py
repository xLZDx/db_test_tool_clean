"""PDM cache resolver for DRD source schema/table/attribute validation and prediction.

This service assumes full DB physical data model metadata is cached under `data/local_kb`.
It loads all `schema_kb_*.json` and `hint_index_*.json` files and resolves DRD source
references against the cache.

Design goals:
- deterministic and explainable scoring
- exact validation when DRD source schema/table/attribute is correct
- fuzzy prediction when DRD is incomplete or has naming drift
- automatic backfill request when schema/table/attribute cannot be found confidently
- no dependency on XML; XML remains only an optional quality gate
"""
from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.services.pdm_backfill_service import PDMBackfillService


def norm_name(value: Any) -> str:
    return re.sub(r"[^A-Z0-9_#$]", "", str(value or "").upper())


def split_tokens(value: Any) -> List[str]:
    text = re.sub(r"[^A-Z0-9]+", "_", str(value or "").upper())
    return [t for t in text.split("_") if t]


ABBREVIATIONS = {
    "TXN": "TRANSACTION",
    "TRD": "TRADE",
    "SEC": "SECURITY",
    "SCR": "SECURITY",
    "ACCT": "ACCOUNT",
    "AC": "ACCOUNT",
    "AR": "ACCOUNT_RELATIONSHIP",
    "NM": "NAME",
    "CD": "CODE",
    "ID": "IDENTIFIER",
    "DT": "DATE",
    "DTTM": "DATETIME",
    "NUM": "NUMBER",
    "QTY": "QUANTITY",
    "AMT": "AMOUNT",
    "TP": "TYPE",
    "CL": "CLASS",
    "VAL": "VALUE",
    "SRC": "SOURCE",
    "CNCL": "CANCEL",
    "RSN": "REASON",
    "DIM": "DIMENSION",
    "FACT": "FACT"
}


def expanded_tokens(value: Any) -> List[str]:
    out: List[str] = []
    for t in split_tokens(value):
        out.append(t)
        if t in ABBREVIATIONS:
            out.extend(split_tokens(ABBREVIATIONS[t]))
    return out


def ratio(a: Any, b: Any) -> float:
    return SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()


def token_overlap(a: Any, b: Any) -> float:
    ta = set(expanded_tokens(a))
    tb = set(expanded_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class PDMColumn:
    schema: str
    table: str
    column: str
    dtype: str = ""
    nullable: str = ""
    description: str = ""
    source_file: str = ""


@dataclass
class PDMResolution:
    input_schema: str
    input_table: str
    input_attribute: str
    resolved_schema: str
    resolved_table: str
    resolved_attribute: str
    confidence: float
    status: str
    reason: str
    candidates: List[Dict[str, Any]]
    backfill_requested: bool = False
    backfill_event: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class PDMCacheResolver:
    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}
        pdm_cfg = self.config.get("pdm_cache", self.config) or {}
        self.local_kb_dir = Path(pdm_cfg.get("local_kb_dir", "data/local_kb"))
        self.schema_kb_glob = pdm_cfg.get("schema_kb_glob", "schema_kb_*.json")
        self.hint_index_glob = pdm_cfg.get("hint_index_glob", "hint_index_*.json")
        self.exact_match_threshold = float(pdm_cfg.get("exact_match_threshold", 0.98))
        self.auto_accept_threshold = float(pdm_cfg.get("auto_accept_threshold", 0.88))
        self.review_threshold = float(pdm_cfg.get("review_threshold", 0.72))
        self.trigger_backfill_below = float(pdm_cfg.get("trigger_backfill_below", 0.72))
        self.max_candidates = int(pdm_cfg.get("max_candidates", 10))
        self.backfill = PDMBackfillService(str(self.local_kb_dir), pdm_cfg.get("operation_history_file", "operation_history.jsonl"))
        self.columns: List[PDMColumn] = []
        self.hints: List[Dict[str, Any]] = []
        self.loaded_files: List[str] = []
        self._load_cache()

    def _load_json_file(self, path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except UnicodeDecodeError:
            with path.open("r", encoding="latin-1") as f:
                return json.load(f)

    def _walk_objects(self, obj: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self._walk_objects(v)
        elif isinstance(obj, list):
            for item in obj:
                yield from self._walk_objects(item)

    def _extract_columns_from_json(self, payload: Any, source_file: str) -> List[PDMColumn]:
        cols: List[PDMColumn] = []
        for obj in self._walk_objects(payload):
            keys = {norm_name(k): k for k in obj.keys()}
            schema_key = next((keys[k] for k in keys if k in {"SCHEMA", "SCHEMA_NAME", "OWNER", "TABLE_SCHEMA", "DATABASE_SCHEMA"}), None)
            table_key = next((keys[k] for k in keys if k in {"TABLE", "TABLE_NAME", "OBJECT_NAME", "ENTITY", "ENTITY_NAME"}), None)
            col_key = next((keys[k] for k in keys if k in {"COLUMN", "COLUMN_NAME", "ATTRIBUTE", "ATTRIBUTE_NAME", "FIELD", "FIELD_NAME"}), None)
            if table_key and col_key:
                schema = norm_name(obj.get(schema_key, "")) if schema_key else ""
                table = norm_name(obj.get(table_key, ""))
                column = norm_name(obj.get(col_key, ""))
                if table and column:
                    dtype = str(obj.get(keys.get("DATA_TYPE", ""), obj.get(keys.get("DATATYPE", ""), obj.get(keys.get("TYPE", ""), ""))) or "")
                    nullable = str(obj.get(keys.get("NULLABLE", ""), obj.get(keys.get("IS_NULLABLE", ""), "")) or "")
                    desc = str(obj.get(keys.get("DESCRIPTION", ""), obj.get(keys.get("COMMENT", ""), obj.get(keys.get("BUSINESS_DEFINITION", ""), ""))) or "")
                    cols.append(PDMColumn(schema, table, column, dtype, nullable, desc, source_file))
        return cols

    def _load_cache(self) -> None:
        if not self.local_kb_dir.exists():
            return
        for pattern in [self.schema_kb_glob, self.hint_index_glob]:
            for path_str in glob.glob(str(self.local_kb_dir / pattern)):
                path = Path(path_str)
                try:
                    payload = self._load_json_file(path)
                except Exception:
                    continue
                self.loaded_files.append(str(path))
                if path.name.startswith("schema_kb"):
                    self.columns.extend(self._extract_columns_from_json(payload, path.name))
                else:
                    # Hint index format is unknown; keep raw searchable objects.
                    self.hints.extend([x for x in self._walk_objects(payload) if isinstance(x, dict)])

    def cache_summary(self) -> Dict[str, Any]:
        return {
            "local_kb_dir": str(self.local_kb_dir),
            "loaded_files": self.loaded_files,
            "column_count": len(self.columns),
            "hint_count": len(self.hints),
            "schema_count": len({c.schema for c in self.columns if c.schema}),
            "table_count": len({(c.schema, c.table) for c in self.columns})
        }

    def _candidate_score(self, row: Dict[str, Any], col: PDMColumn) -> Tuple[float, List[str]]:
        src_schema = norm_name(row.get("source_schema"))
        src_table = norm_name(row.get("source_table"))
        src_attr = norm_name(row.get("source_attribute"))
        target_col = norm_name(row.get("column"))
        logical = row.get("logical_name", "")
        transformation = row.get("transformation", "")
        reasons: List[str] = []

        schema_score = 1.0 if src_schema and src_schema == col.schema else ratio(src_schema, col.schema) if src_schema and col.schema else 0.2
        table_score = max(ratio(src_table, col.table), token_overlap(src_table, col.table)) if src_table else 0.0
        attr_score = max(ratio(src_attr, col.column), token_overlap(src_attr, col.column)) if src_attr else 0.0
        target_score = max(ratio(target_col, col.column), token_overlap(target_col, col.column)) if target_col else 0.0
        logical_score = max(ratio(logical, col.column), token_overlap(logical, col.column)) if logical else 0.0
        transform_score = 0.10 if col.column in norm_name(transformation) or col.table in norm_name(transformation) else 0.0

        if src_schema and src_schema == col.schema: reasons.append("schema_exact")
        if src_table and src_table == col.table: reasons.append("table_exact")
        if src_attr and src_attr == col.column: reasons.append("attribute_exact")
        if target_col and target_col == col.column: reasons.append("target_column_exact")

        # Attribute is most important, then table, then target/logical hints.
        score = (
            schema_score * 0.10 +
            table_score * 0.25 +
            attr_score * 0.35 +
            target_score * 0.15 +
            logical_score * 0.10 +
            transform_score * 0.05
        )
        return round(min(score, 1.0), 4), reasons

    def resolve_row(self, row: Dict[str, Any]) -> PDMResolution:
        src_schema = norm_name(row.get("source_schema"))
        src_table = norm_name(row.get("source_table"))
        src_attr = norm_name(row.get("source_attribute"))

        candidates: List[Dict[str, Any]] = []
        for col in self.columns:
            score, reasons = self._candidate_score(row, col)
            if score >= 0.20:
                candidates.append({
                    "schema": col.schema,
                    "table": col.table,
                    "attribute": col.column,
                    "dtype": col.dtype,
                    "nullable": col.nullable,
                    "description": col.description,
                    "score": score,
                    "reasons": reasons,
                    "source_file": col.source_file
                })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        candidates = candidates[: self.max_candidates]

        if not candidates:
            event = self.backfill.request_backfill("NO_PDM_CANDIDATES", {"drd_row": row})
            return PDMResolution(src_schema, src_table, src_attr, "", "", "", 0.0, "BACKFILL_REQUESTED", "No PDM candidates found", [], True, event)

        best = candidates[0]
        confidence = float(best["score"])
        if confidence >= self.exact_match_threshold and {"schema_exact", "table_exact", "attribute_exact"}.issubset(set(best.get("reasons", []))):
            status = "VALIDATED_EXACT"
            reason = "DRD source schema/table/attribute validated against PDM cache"
            backfill_event = None
            backfill_requested = False
        elif confidence >= self.auto_accept_threshold:
            status = "PREDICTED_AUTO_ACCEPT"
            reason = "PDM resolver predicted source table/attribute with high confidence"
            backfill_event = None
            backfill_requested = False
        elif confidence >= self.review_threshold:
            status = "PREDICTED_REVIEW_REQUIRED"
            reason = "PDM resolver found plausible candidate, review recommended"
            backfill_event = None
            backfill_requested = False
        else:
            status = "BACKFILL_REQUESTED"
            reason = "Best PDM candidate below confidence threshold"
            backfill_event = self.backfill.request_backfill("LOW_CONFIDENCE_PDM_RESOLUTION", {"drd_row": row, "best_candidate": best, "candidates": candidates})
            backfill_requested = True

        return PDMResolution(
            src_schema,
            src_table,
            src_attr,
            best.get("schema", ""),
            best.get("table", ""),
            best.get("attribute", ""),
            confidence,
            status,
            reason,
            candidates,
            backfill_requested,
            backfill_event
        )

    def resolve_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.resolve_row(r).to_dict() for r in rows]
