"""Generic 99% DRD/XML orchestration service.

Compares the DRD target column schema against what is extracted from an ODI
scenario XML export and scores parity. Scores >= 99.0 are reported as PASS.
"""
from __future__ import annotations

import io
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from app.config import DATA_DIR
from app.services.odi_xml_reverse_engineer_service import (
    extract_odi_xml_metadata,
    normalize_dtype,
    sanitize_oracle_comment,
)

# XML encodings accepted from user-supplied config.  Anything outside this set
# is silently reset to the safe default so that the config cannot be used as a
# path-traversal or encoding-confusion vector.
_ALLOWED_XML_ENCODINGS = frozenset({"UTF-8", "ISO-8859-1", "UTF-16", "WINDOWS-1252"})


def _sanitize_csv_cell(val: Any) -> Any:
    """Prefix formula-injection tokens in CSV cells with a single quote."""
    v = str(val) if not isinstance(val, str) else val
    if v and v[0] in ("=", "+", "@", "\t", "\r"):
        return "'" + v
    return val


def nullable_from(value: str) -> str:
    value = str(value or "").strip().lower()
    return "NULL" if value in ("yes", "y", "null", "nullable", "", "nan") else "NOT NULL"


def _get(row, idx):
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx]).strip() if pd.notna(row[idx]) else ""


def extract_drd_schema_from_excel_bytes(
    file_bytes: bytes,
    config: Dict[str, Any],
    include_removed: bool = False,
) -> List[Dict[str, Any]]:
    """Canonical DRD extractor for 99% schema parity.

    Reads a DRD Excel worksheet according to the column-index mapping in
    ``config["drd"]`` and returns one dict per active target column.
    """
    drd_cfg = config.get("drd", {})
    sheet = drd_cfg.get("primary_sheet", "Table-View")
    first_row = int(drd_cfg.get("first_data_row_index", 12))
    colmap = drd_cfg.get("columns", {})
    exclude_prefixes = [x.upper() for x in drd_cfg.get("exclude_actions_startswith", ["Remove"])]

    raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet, header=None, engine="openpyxl")
    rows: List[Dict[str, Any]] = []
    for ridx in range(first_row, len(raw)):
        row = raw.iloc[ridx]
        target = _get(row, colmap.get("target_column"))
        dtype = _get(row, colmap.get("oracle_datatype"))
        action = _get(row, colmap.get("action"))
        if not re.match(r"^[A-Z][A-Z0-9_#]*$", target or "") or not dtype:
            continue
        if not include_removed and any(action.upper().startswith(p) for p in exclude_prefixes):
            continue
        rows.append(
            {
                "ordinal": len(rows) + 1,
                "excel_row": ridx + 1,
                "column": target.upper(),
                "dtype": normalize_dtype(dtype),
                "nullable": nullable_from(_get(row, colmap.get("nullable"))),
                "action": action,
                "logical_name": _get(row, colmap.get("logical_name")),
                "comment": _get(row, colmap.get("business_definition")),
                "source_schema": _get(row, colmap.get("source_schema")),
                "source_table": _get(row, colmap.get("source_table")),
                "source_attribute": _get(row, colmap.get("source_attribute")),
                "transformation": _get(row, colmap.get("transformation")),
            }
        )
    return rows


def create_table_ddl(rows: List[Dict[str, Any]], config: Dict[str, Any]) -> str:
    table = config.get("table", {}).get("name", "TARGET_TABLE")
    include_comments = bool(config.get("outputs", {}).get("include_comments", True))
    lines = [f"CREATE TABLE {table} ("]
    for i, r in enumerate(rows):
        comma = "," if i < len(rows) - 1 else ""
        lines.append(f"    {r['column']} {r['dtype']} {r['nullable']}{comma}")
    lines.append(");")
    if include_comments:
        for r in rows:
            comment = sanitize_oracle_comment(r.get("comment") or "")
            if comment:
                lines.append(f"COMMENT ON COLUMN {table}.{r['column']} IS '{comment}';")
    return "\n".join(lines)


def score_99(
    drd_rows: List[Dict[str, Any]],
    xml_columns,
    config: Dict[str, Any],
    removed_rows: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    policy = config.get("match_policy", {})
    aliases = policy.get("aliases", {}) or {}
    ignore_xml_helper = set(policy.get("ignore_xml_helper_columns", []) or [])
    post_xml_drd = set(policy.get("post_xml_drd_additions", []) or [])
    ignore_action_prefixes = tuple(
        x.upper() for x in policy.get("ignore_xml_if_drd_action_starts_with", ["Remove"])
    )

    removed_cols = set()
    for r in removed_rows or []:
        if str(r.get("action", "")).upper().startswith(ignore_action_prefixes):
            removed_cols.add(r["column"])

    d = {r["column"]: r for r in drd_rows}
    x = set([r["column"] if isinstance(r, dict) else str(r).upper() for r in xml_columns])

    scored_drd = [c for c in d if c not in post_xml_drd]
    scored_xml = [c for c in x if c not in ignore_xml_helper and c not in removed_cols]

    matched, missing = [], []
    for c in scored_drd:
        if c in x or aliases.get(c) in x:
            matched.append(c)
        else:
            missing.append(c)

    alias_values = set(aliases.values())
    extra = [c for c in scored_xml if c not in d and c not in alias_values]
    denominator = max(len(scored_drd), len(scored_xml)) or 1
    score = round(len(matched) / denominator * 100, 2)
    threshold = float(policy.get("success_threshold", 99.0))
    return {
        "score": score,
        "threshold": threshold,
        "status": "PASS" if score >= threshold else "FAIL",
        "scored_drd_columns": len(scored_drd),
        "scored_xml_columns": len(scored_xml),
        "matched_columns": len(matched),
        "missing": missing,
        "extra": extra,
    }


def default_config() -> Dict[str, Any]:
    return {
        "table": {"name": "TARGET_SCHEMA.TARGET_TABLE", "short_name": "TARGET_TABLE"},
        "drd": {
            "primary_sheet": "Table-View",
            "first_data_row_index": 12,
            "columns": {
                "logical_name": 0,
                "target_column": 1,
                "oracle_datatype": 3,
                "nullable": 4,
                "business_definition": 7,
                "action": 9,
                "source_schema": 22,
                "source_table": 23,
                "source_attribute": 24,
                "transformation": 27,
            },
            "exclude_actions_startswith": ["Remove"],
        },
        "xml": {"encoding": "ISO-8859-1", "compare_mode": "final_merge"},
        "match_policy": {
            "success_threshold": 99.0,
            "aliases": {},
            "ignore_xml_helper_columns": ["ROWNM"],
            "ignore_xml_if_drd_action_starts_with": ["Remove"],
            "post_xml_drd_additions": [],
        },
        "outputs": {"include_comments": True},
    }


def _safe_user_config(user_config: Dict[str, Any]) -> Dict[str, Any]:
    """Return only allowlisted keys from user-supplied config.

    Prevents path-traversal via DATA_DIR overrides or encoding confusion via
    arbitrary xml.encoding values.
    """
    allowed_top = {"table", "match_policy", "outputs", "drd"}
    safe: Dict[str, Any] = {k: v for k, v in user_config.items() if k in allowed_top}
    # Allow xml.encoding only if it is one of the known-safe values
    if "xml" in user_config:
        xml_enc = str((user_config.get("xml") or {}).get("encoding") or "").upper()
        if xml_enc in _ALLOWED_XML_ENCODINGS:
            safe["xml"] = {"encoding": xml_enc}
    return safe


def merge_config(user_config: Dict[str, Any] | None) -> Dict[str, Any]:
    base = default_config()
    user_config = user_config or {}

    def deep_update(a: Dict[str, Any], b: Dict[str, Any]):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                deep_update(a[k], v)
            else:
                a[k] = v
        return a

    return deep_update(base, user_config)


def run_99_orchestration(
    drd_bytes: bytes,
    xml_bytes: bytes,
    user_config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    config = merge_config(_safe_user_config(user_config or {}))
    run_id = uuid.uuid4().hex[:12]
    data_dir_path = Path(DATA_DIR)
    out_dir = data_dir_path / "orchestrator_runs" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    drd_rows = extract_drd_schema_from_excel_bytes(drd_bytes, config, include_removed=False)
    all_drd_rows = extract_drd_schema_from_excel_bytes(drd_bytes, config, include_removed=True)
    removed_rows = [r for r in all_drd_rows if str(r.get("action", "")).upper().startswith("REMOVE")]
    xml_meta = extract_odi_xml_metadata(
        xml_bytes,
        encoding=config.get("xml", {}).get("encoding", "ISO-8859-1"),
    )

    ddl = create_table_ddl(drd_rows, config)
    stage_score = score_99(drd_rows, xml_meta["stage_columns"], config, removed_rows)
    final_merge_score = score_99(
        drd_rows, xml_meta["final_merge_insert_columns"], config, removed_rows
    )

    result: Dict[str, Any] = {
        "run_id": run_id,
        "table": config.get("table", {}).get("name"),
        "status": final_merge_score["status"],
        "score": final_merge_score["score"],
        "threshold": final_merge_score["threshold"],
        "stage_schema_score": stage_score,
        "final_merge_score": final_merge_score,
        "counts": {
            "drd_active_columns": len(drd_rows),
            "drd_removed_columns": len(removed_rows),
            "xml_stage_columns": len(xml_meta["stage_columns"]),
            "xml_final_merge_insert_columns": len(xml_meta["final_merge_insert_columns"]),
        },
        "artifacts": {},
    }

    short = config.get("table", {}).get("short_name", "target")
    paths = {
        "comparison_json": out_dir / "comparison_99_logic.json",
        "generated_sql": out_dir / f"agent_generated_{short}_from_DRD.sql",
        "drd_columns_csv": out_dir / "drd_extracted_columns.csv",
        "xml_stage_csv": out_dir / "xml_stage_columns.csv",
        "xml_final_merge_csv": out_dir / "xml_final_merge_insert_columns.csv",
    }
    paths["comparison_json"].write_text(json.dumps(result, indent=2), encoding="utf-8")
    paths["generated_sql"].write_text(ddl, encoding="utf-8")

    # Sanitize CSV cells to prevent formula injection in downstream consumers
    def _write_sanitized_csv(df: pd.DataFrame, path: Path) -> None:
        for col in df.columns:
            df[col] = df[col].apply(_sanitize_csv_cell)
        df.to_csv(path, index=False)

    _write_sanitized_csv(pd.DataFrame(drd_rows), paths["drd_columns_csv"])
    _write_sanitized_csv(pd.DataFrame(xml_meta["stage_columns"]), paths["xml_stage_csv"])
    _write_sanitized_csv(
        pd.DataFrame({"column": xml_meta["final_merge_insert_columns"]}),
        paths["xml_final_merge_csv"],
    )

    # Return relative paths only — never expose absolute server paths to clients
    result["artifacts"] = {
        k: v.relative_to(data_dir_path).as_posix() for k, v in paths.items()
    }
    paths["comparison_json"].write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
