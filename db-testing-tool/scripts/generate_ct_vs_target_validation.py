"""Generate CT-vs-Target row/column comparison SQL for offline operator use.

After the operator has populated:
  CONTROL: <control_schema>.<target_table>  -- via the GUI's INSERT
                                                (drd_first_insert SQL)
  TARGET:  <prod_schema>.<target_table>     -- production ODI load result

This script emits Oracle SQL that the operator runs on the live DB to
prove the two are equivalent (or surface where they differ).  It is
GENERIC -- it consumes the target column list from the schema KB or
from a JSON `target_definition`; no project-specific column names are
hardcoded.

Operator-locked (2026-05-29 Phase 7.1):
  * Generic: works on any control/target schema pair.
  * Offline: this script emits SQL; it never connects to Oracle.
  * Read-only intent: the emitted SQL is SELECT-only.
  * Three validation queries: ROW_COUNT, ROW_HASH_DIFF, COLUMN_NULL_PARITY.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_target_def_from_kb(
    kb_path: pathlib.Path, target_schema_pdm: str, target_table: str,
) -> dict | None:
    """Read columns + types + PK info from local schema KB."""
    if not kb_path.exists():
        return None
    kb = json.loads(kb_path.read_text(encoding="utf-8"))
    for sch in kb.get("pdm", {}).get("schemas", []):
        if sch.get("schema", "").upper() != target_schema_pdm.upper():
            continue
        for tbl in sch.get("tables", []):
            if tbl.get("name", "").upper() == target_table.upper():
                return {
                    "schema": sch["schema"],
                    "name": tbl["name"],
                    "columns": [
                        {
                            "name": c["name"],
                            "data_type": c.get("data_type"),
                            "is_pk": c.get("is_pk", False),
                            "ordinal_position": c.get("ordinal_position", 0),
                        }
                        for c in tbl.get("columns", [])
                    ],
                }
    return None


import re as _re


class IdentifierError(ValueError):
    """Raised when a user-supplied SQL identifier fails validation."""


# Oracle unquoted identifier grammar: letter, then letters/digits/_/$/#.
# Max 30 chars (Oracle 12.1) -- we use 128 to be forward-compatible.
_VALID_IDENT = _re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")


def _quote(ident: str) -> str:
    """Validate and uppercase an Oracle identifier.

    Operator-locked security gate (2026-05-29 Phase 7.2): all CLI-supplied
    schema / table / column names are emitted verbatim into the generated
    SQL.  An identifier containing whitespace, semicolons, quotes,
    parentheses, or any other character outside Oracle's unquoted-identifier
    grammar would let a malicious config / arg craft injected SQL the
    operator runs on the live DB.  Reject those at generation time.
    """
    if ident is None or not _VALID_IDENT.match(ident):
        raise IdentifierError(
            f"Refusing to emit SQL with invalid Oracle identifier: {ident!r}.  "
            f"Allowed pattern: ^[A-Za-z][A-Za-z0-9_$#]*$ (max 128 chars)."
        )
    return ident.upper()


def _emit_row_count_check(
    ct_schema: str, prod_schema: str, table: str,
) -> str:
    """Compare row counts.  Trivial but essential."""
    ct = f"{_quote(ct_schema)}.{_quote(table)}"
    pr = f"{_quote(prod_schema)}.{_quote(table)}"
    return (
        f"-- ── Check 1: ROW COUNT ──────────────────────────────────────\n"
        f"-- Expect CT_COUNT = PROD_COUNT (the ODI scenario should land\n"
        f"-- the same row set in both tables).  If they differ, every\n"
        f"-- downstream column check is suspect -- fix this first.\n"
        f"WITH ct AS (SELECT COUNT(*) AS n FROM {ct} ),\n"
        f"     pr AS (SELECT COUNT(*) AS n FROM {pr} )\n"
        f"SELECT ct.n AS ct_count,\n"
        f"       pr.n AS prod_count,\n"
        f"       (pr.n - ct.n) AS diff\n"
        f"  FROM ct, pr;\n"
    )


def _emit_minus_diff(
    ct_schema: str, prod_schema: str, table: str, columns: list[str],
) -> str:
    """Symmetric MINUS comparison on every column -- shows rows that
    exist in one side but not the other (including value differences)."""
    ct = f"{_quote(ct_schema)}.{_quote(table)}"
    pr = f"{_quote(prod_schema)}.{_quote(table)}"
    col_list = ", ".join(_quote(c) for c in columns)
    return (
        f"-- ── Check 2: ROW-LEVEL DIFFERENCES (symmetric MINUS) ────────\n"
        f"-- Top query: rows in CT that are NOT in PROD (CT extra or\n"
        f"-- column-value drift).  Bottom: rows in PROD that are NOT in\n"
        f"-- CT.  Both empty = perfect equivalence.\n"
        f"-- All columns from the target definition are compared.\n"
        f"-- WARNING: this can be expensive on full tables; consider\n"
        f"-- adding a WHERE on a recent partition for spot-check use.\n"
        f"-- CT - PROD\n"
        f"SELECT 'CT_EXTRA' AS side, t.*\n"
        f"  FROM (SELECT {col_list} FROM {ct}\n"
        f"        MINUS\n"
        f"        SELECT {col_list} FROM {pr}) t\n"
        f" UNION ALL\n"
        f"-- PROD - CT\n"
        f"SELECT 'PROD_EXTRA' AS side, t.*\n"
        f"  FROM (SELECT {col_list} FROM {pr}\n"
        f"        MINUS\n"
        f"        SELECT {col_list} FROM {ct}) t;\n"
    )


def _emit_per_column_null_parity(
    ct_schema: str, prod_schema: str, table: str, columns: list[str],
) -> str:
    """For each column, count NULLs on both sides.  Columns where CT
    and PROD have different NULL counts are likely candidates for the
    REAL_MISMATCH / SOURCE_MISSING / CHAIN_WARNING issues surfaced
    by the comparator (e.g. TXN_CCY's STEP3=NULL pattern leaks here
    if STEP5's join fails for any row)."""
    ct = f"{_quote(ct_schema)}.{_quote(table)}"
    pr = f"{_quote(prod_schema)}.{_quote(table)}"
    union_parts = []
    for col in columns:
        col_q = _quote(col)
        union_parts.append(
            f"SELECT '{col_q}' AS column_name,\n"
            f"       (SELECT COUNT(*) FROM {ct}  WHERE {col_q} IS NULL) AS ct_null_count,\n"
            f"       (SELECT COUNT(*) FROM {pr}  WHERE {col_q} IS NULL) AS prod_null_count\n"
            f"  FROM dual"
        )
    body = "\n UNION ALL\n".join(union_parts)
    return (
        f"-- ── Check 3: PER-COLUMN NULL PARITY ──────────────────────────\n"
        f"-- For each column, count NULLs on both sides.  Rows where\n"
        f"-- CT_NULL_COUNT != PROD_NULL_COUNT indicate a structural\n"
        f"-- divergence -- usually a SOURCE_MISSING column or a chain\n"
        f"-- inconsistency (NULL_INJECTED_THEN_OVERWRITTEN that didn't\n"
        f"-- overwrite on every row).\n"
        f"-- ORDER BY diff DESC at the bottom so the worst offenders\n"
        f"-- bubble up first.\n"
        f"SELECT column_name, ct_null_count, prod_null_count,\n"
        f"       (prod_null_count - ct_null_count) AS diff\n"
        f"  FROM (\n"
        f"{body}\n"
        f")\n"
        f" ORDER BY ABS(prod_null_count - ct_null_count) DESC, column_name;\n"
    )


def _emit_per_column_value_drift_hash(
    ct_schema: str, prod_schema: str, table: str, key_cols: list[str],
    compare_cols: list[str],
) -> str:
    """Join CT to PROD on primary key columns; show columns whose values
    differ row-by-row.  Best validation for ROW_HASH_DIFF when PK is
    known."""
    if not key_cols:
        return (
            f"-- ── Check 4: PER-COLUMN VALUE DRIFT (joined on PK) ──────\n"
            f"-- SKIPPED: no primary key columns identified in the\n"
            f"-- target definition.  Add PK metadata to the PDM KB to\n"
            f"-- enable per-column drift detection joined on PK.\n"
        )
    ct = f"{_quote(ct_schema)}.{_quote(table)}"
    pr = f"{_quote(prod_schema)}.{_quote(table)}"
    join_cond = " AND ".join(
        f"ct.{_quote(c)} = pr.{_quote(c)}" for c in key_cols
    )
    diff_unions = []
    for c in compare_cols:
        cq = _quote(c)
        diff_unions.append(
            f"SELECT '{cq}' AS column_name, COUNT(*) AS drift_rows\n"
            f"  FROM {ct} ct JOIN {pr} pr ON {join_cond}\n"
            f" WHERE DECODE(ct.{cq}, pr.{cq}, 0, 1) = 1"
        )
    body = "\n UNION ALL\n".join(diff_unions) if diff_unions else "SELECT NULL AS column_name, 0 AS drift_rows FROM dual WHERE 1=0"
    return (
        f"-- ── Check 4: PER-COLUMN VALUE DRIFT (joined on PK) ──────────\n"
        f"-- For each non-PK column, count rows where CT.col != PROD.col\n"
        f"-- joined on the PK columns: {', '.join(_quote(c) for c in key_cols)}.\n"
        f"-- DECODE handles NULL-equality correctly: NULL == NULL counts\n"
        f"-- as match.\n"
        f"-- WARNING: full-table join; partition for spot-check.\n"
        f"SELECT column_name, drift_rows\n"
        f"  FROM (\n"
        f"{body}\n"
        f")\n"
        f" WHERE drift_rows > 0\n"
        f" ORDER BY drift_rows DESC;\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Emit CT vs Target validation SQL (offline; SELECT-only).",
    )
    ap.add_argument("--kb", default=str(ROOT / "data" / "local_kb" / "schema_kb_ds_3.json"),
                    help="Schema KB JSON file (default: data/local_kb/schema_kb_ds_3.json)")
    ap.add_argument("--target-pdm-schema", default="TRANSACTIONS_OWNER",
                    help="Schema in the PDM where the target table is defined")
    ap.add_argument("--table", default="AVY_FACT_SIDE",
                    help="Target table name (in both CT and PROD)")
    ap.add_argument("--ct-schema", default="IKOROSTELEV",
                    help="Control schema (where GUI INSERT lands)")
    ap.add_argument("--prod-schema", default="TRANSACTIONS_OWNER",
                    help="Production schema (where ODI scenario lands)")
    ap.add_argument("--output", default=str(ROOT / "data" / "api_runs" / "CT_VS_TARGET_VALIDATION.sql"),
                    help="Output SQL file path")
    args = ap.parse_args()

    target_def = _load_target_def_from_kb(
        pathlib.Path(args.kb), args.target_pdm_schema, args.table,
    )
    if not target_def:
        print(f"ERROR: target table {args.target_pdm_schema}.{args.table} not found in KB at {args.kb}")
        return 2

    cols = [c["name"] for c in target_def["columns"]]
    pk_cols = [c["name"] for c in target_def["columns"] if c.get("is_pk")]
    non_pk_cols = [c["name"] for c in target_def["columns"] if not c.get("is_pk")]
    print(f"Target {args.target_pdm_schema}.{args.table}: {len(cols)} columns, "
          f"{len(pk_cols)} PK columns")

    parts = [
        f"-- ╔══════════════════════════════════════════════════════════╗",
        f"-- ║  CT vs TARGET row/column validation                      ║",
        f"-- ║                                                          ║",
        f"-- ║  CT:    {args.ct_schema}.{args.table:<43}║",
        f"-- ║  TARGET:{args.prod_schema}.{args.table:<43}║",
        f"-- ║  Total target columns: {len(cols):<31}   ║",
        f"-- ║  PK columns:           {len(pk_cols):<31}   ║",
        f"-- ║  Generated by generate_ct_vs_target_validation.py        ║",
        f"-- ║  Run on Oracle DB after CT has been populated.           ║",
        f"-- ║  Read-only: all queries are SELECT only.                 ║",
        f"-- ╚══════════════════════════════════════════════════════════╝",
        "",
        _emit_row_count_check(args.ct_schema, args.prod_schema, args.table),
        "",
        _emit_per_column_null_parity(args.ct_schema, args.prod_schema, args.table, cols),
        "",
        _emit_per_column_value_drift_hash(
            args.ct_schema, args.prod_schema, args.table, pk_cols, non_pk_cols,
        ),
        "",
        _emit_minus_diff(args.ct_schema, args.prod_schema, args.table, cols),
        "",
    ]

    # Path-traversal guard: refuse to write outside the project root.
    out_path = pathlib.Path(args.output).resolve()
    try:
        out_path.relative_to(ROOT)
    except ValueError:
        print(
            f"ERROR: refusing to write outside project root.\n"
            f"  Project root: {ROOT}\n"
            f"  Requested:    {out_path}"
        )
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_text("\n".join(parts), encoding="utf-8")
    except IdentifierError as exc:
        # _quote rejected an identifier during emission.  Re-raise as
        # exit-2 with a clean operator message; never write partial SQL.
        print(f"ERROR: {exc}")
        return 2
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
