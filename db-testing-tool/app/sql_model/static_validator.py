"""P5 -- Static offline validator.

Validates an ODIModel against the local KB WITHOUT any live DB connection.

What it catches (KB-only):
  PDM_MISS          -- a source TableRef is not found in the KB/PDM
  COLUMN_NOT_IN_KB  -- a column referenced in a mapping is not in the KB table def
  NULL_VIOLATION_RISK -- mapping may write NULL into a NOT NULL target column
  UNRESOLVED_EXPR   -- soft warning: UnresolvedExpr present (emitter already catches this)

What it does NOT catch (deferred to XE path):
  ORA-01555 (snapshot too old), partition-pruning 0-rows, CONNECT BY loops.

The static gate is AUTHORITATIVE.  P6 (XE) is confirmatory only --
static result is never overridden by XE.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from app.sql_model.types import ODIModel, Provenance, ResolvedColumn, TableRef, UnresolvedExpr, norm


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class StaticVerdict(Enum):
    STATIC_PASS = "STATIC_PASS"
    PDM_MISS = "PDM_MISS"                     # blocking: table not in KB
    COLUMN_NOT_IN_KB = "COLUMN_NOT_IN_KB"     # blocking: column not in KB table
    NULL_VIOLATION_RISK = "NULL_VIOLATION_RISK"  # warning only
    STATIC_PARTIAL = "STATIC_PARTIAL"         # warnings but no hard blockers


@dataclass(frozen=True)
class ValidationError:
    code: str         # StaticVerdict value or sub-code string
    table: str
    column: str
    detail: str
    is_blocking: bool


@dataclass
class ValidationResult:
    verdict: StaticVerdict
    errors: list[ValidationError] = field(default_factory=list)
    checked_tables: list[str] = field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return any(e.is_blocking for e in self.errors)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "is_blocking": self.is_blocking,
            "error_count": len(self.errors),
            "errors": [
                {
                    "code": e.code,
                    "table": e.table,
                    "column": e.column,
                    "detail": e.detail,
                    "is_blocking": e.is_blocking,
                }
                for e in self.errors
            ],
            "checked_tables": self.checked_tables,
        }


# ---------------------------------------------------------------------------
# KB lookup helper
# ---------------------------------------------------------------------------

class KBLookup:
    """Fast lookup over schema_kb_ds_1.json: schema.table -> {COL: meta}.

    Supports both schema-qualified (SCHEMA.TABLE) and unqualified (TABLE)
    lookups.  Unqualified resolves to the first matching table found in
    schema iteration order.
    """

    def __init__(self, kb_path: Path) -> None:
        # "SCHEMA.TABLE" -> {COL_NAME: {data_type, nullable, is_pk}}
        self._index: dict[str, dict[str, dict]] = {}
        # "TABLE" -> first qualified key found
        self._table_index: dict[str, str] = {}
        self._load(kb_path)

    def _load(self, kb_path: Path) -> None:
        raw: dict = json.loads(kb_path.read_text(encoding="utf-8"))
        for schema_obj in raw.get("pdm", {}).get("schemas", []):
            schema = norm(schema_obj.get("schema", ""))
            for tbl_obj in schema_obj.get("tables", []):
                table = norm(tbl_obj.get("name", ""))
                if not table:
                    continue
                cols: dict[str, dict] = {}
                for c in tbl_obj.get("columns", []):
                    cname = norm(c.get("name", ""))
                    if not cname:
                        continue
                    cols[cname] = {
                        "data_type": (c.get("data_type") or "VARCHAR2").upper(),
                        "nullable": bool(c.get("nullable", True)),
                        "is_pk": bool(c.get("is_pk", False)),
                    }
                key = f"{schema}.{table}" if schema else table
                self._index[key] = cols
                if table not in self._table_index:
                    self._table_index[table] = key

    def _resolve_key(self, ref: TableRef) -> Optional[str]:
        """Return the internal index key for a TableRef, or None if not found."""
        qualified = f"{ref.schema}.{ref.table}" if ref.schema else ref.table
        if qualified in self._index:
            return qualified
        # Unqualified fallback
        fallback = self._table_index.get(ref.table)
        return fallback  # may be None

    def table_exists(self, ref: TableRef) -> bool:
        return self._resolve_key(ref) is not None

    def get_columns(self, ref: TableRef) -> Optional[dict[str, dict]]:
        key = self._resolve_key(ref)
        if key is None:
            return None
        return self._index.get(key)

    def column_exists(self, ref: TableRef, column: str) -> bool:
        cols = self.get_columns(ref)
        if cols is None:
            return False
        return norm(column) in cols

    def column_nullable(self, ref: TableRef, column: str) -> bool:
        """Return True if column is nullable (permissive when unknown)."""
        cols = self.get_columns(ref)
        if cols is None:
            return True
        meta = cols.get(norm(column))
        if meta is None:
            return True
        return meta.get("nullable", True)


# ---------------------------------------------------------------------------
# Core validator
# ---------------------------------------------------------------------------

def validate_model_offline(model: ODIModel, kb: KBLookup) -> ValidationResult:
    """Validate an ODIModel against the KB.  Returns typed errors, never raises."""
    errors: list[ValidationError] = []
    checked_keys: set[str] = set()

    for step in model.staging_steps:
        # -- 1. Check every source table binding exists in KB ------------------
        for binding in step.source_bindings:
            ref = binding.ref
            if not ref.table:
                continue
            key = ref.fq
            checked_keys.add(key)
            if not kb.table_exists(ref):
                errors.append(ValidationError(
                    code="PDM_MISS",
                    table=key,
                    column="",
                    detail=f"Source table {key!r} not found in KB/PDM",
                    is_blocking=True,
                ))

        # -- 2. Check individual column mappings --------------------------------
        for cm in step.column_mappings:
            if isinstance(cm.source, UnresolvedExpr):
                # Already flagged by emitter; surface as non-blocking warning
                errors.append(ValidationError(
                    code="UNRESOLVED_EXPR",
                    table="",
                    column=cm.target_col,
                    detail=f"{cm.source.reason}: {cm.source.detail}",
                    is_blocking=False,
                ))
                continue

            src: ResolvedColumn = cm.source  # type: ignore[assignment]
            if src.ref is None:
                # Literal / complex expression -- no KB check possible
                continue

            ref = src.ref
            col = norm(src.column)
            if not col:
                continue

            # Skip column check if table was already flagged as PDM_MISS
            if not kb.table_exists(ref):
                continue

            if not kb.column_exists(ref, col):
                errors.append(ValidationError(
                    code="COLUMN_NOT_IN_KB",
                    table=ref.fq,
                    column=col,
                    detail=f"Column {ref.fq}.{col} not found in KB table definition",
                    is_blocking=True,
                ))

            # NULL risk check (warning only -- cannot prove NULL statically)
            elif cm.is_nullable and not kb.column_nullable(model.target, cm.target_col):
                errors.append(ValidationError(
                    code="NULL_VIOLATION_RISK",
                    table=ref.fq,
                    column=cm.target_col,
                    detail=(
                        f"Target {cm.target_col!r} is NOT NULL in KB; "
                        f"source {ref.fq}.{col} may produce NULL via outer join"
                    ),
                    is_blocking=False,
                ))

    # -- 3. Check target table itself exists in KB -----------------------------
    checked_keys.add(model.target.fq)
    if not kb.table_exists(model.target):
        errors.append(ValidationError(
            code="PDM_MISS",
            table=model.target.fq,
            column="",
            detail=f"Target table {model.target.fq!r} not found in KB/PDM",
            is_blocking=True,
        ))

    # -- Determine aggregate verdict -------------------------------------------
    blocking = [e for e in errors if e.is_blocking]
    has_pdm_miss = any(e.code == "PDM_MISS" for e in blocking)

    if has_pdm_miss:
        verdict = StaticVerdict.PDM_MISS
    elif blocking:
        verdict = StaticVerdict.COLUMN_NOT_IN_KB
    elif errors:
        verdict = StaticVerdict.STATIC_PARTIAL
    else:
        verdict = StaticVerdict.STATIC_PASS

    return ValidationResult(
        verdict=verdict,
        errors=errors,
        checked_tables=sorted(checked_keys),
    )
