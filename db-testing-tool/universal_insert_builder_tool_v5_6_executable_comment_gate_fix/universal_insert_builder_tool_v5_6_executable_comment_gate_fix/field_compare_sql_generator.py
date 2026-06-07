#!/usr/bin/env python3
"""
field_compare_sql_generator.py

Generate Oracle SQL to compare every field between:
- real target table
- control table created by generated INSERT

The script is metadata-driven:
- target table
- control table
- grain/key columns
- compare columns from CLI or CSV file

It produces:
- 00_row_count_check.sql
- 01_duplicate_grain_check.sql
- 02_unmatched_rows_check.sql
- 03_field_mismatch_summary.sql
- 04_field_mismatch_details.sql
- field_compare_config.json

Default comparison style matches the user's pattern:
NVL(TO_CHAR(T.COL), '<NULL>') <> NVL(TO_CHAR(CTL.COL), '<NULL>')
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def norm_identifier(value: str) -> str:
    value = (value or "").strip()
    value = value.strip('"')
    return value.upper()


def split_csv_list(value: str) -> List[str]:
    if not value:
        return []
    return [norm_identifier(x) for x in re.split(r"[,\n;]+", value) if x.strip()]


def read_columns_file(path: Path, column_field: str = "") -> List[str]:
    """
    Reads columns from a CSV file.
    Auto-detects common field names:
    - target_column
    - column_name
    - target_col
    - TARGET_COL
    """
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        if column_field:
            field = column_field
        else:
            candidates = [
                "target_column", "column_name", "target_col", "TARGET_COL",
                "Target Column", "Target column", "COLUMN_NAME",
            ]
            field = next((c for c in candidates if c in headers), "")
            if not field:
                raise ValueError(
                    f"Could not auto-detect target column field in {path}. "
                    f"Headers: {headers}. Use --columns-field."
                )

        cols = []
        for row in reader:
            col = norm_identifier(row.get(field, ""))
            if col:
                cols.append(col)
        return cols


def unique_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for item in items:
        item = norm_identifier(item)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def q(col: str, quote_identifiers: bool = False) -> str:
    col = norm_identifier(col)
    return f'"{col}"' if quote_identifiers else col


def table_ref(value: str) -> str:
    # Preserve schema.table, normalize parts.
    parts = [p for p in (value or "").strip().split(".") if p]
    return ".".join(norm_identifier(p) for p in parts)


def value_expr(alias: str, col: str, mode: str, null_token: str, quote_identifiers: bool = False) -> str:
    c = f"{alias}.{q(col, quote_identifiers)}"
    if mode == "native":
        return c
    if mode == "to_char":
        return f"TO_CHAR({c})"
    # nvl_to_char
    return f"NVL(TO_CHAR({c}), '{null_token}')"


def mismatch_predicate(col: str, mode: str, null_token: str, quote_identifiers: bool = False) -> str:
    t = f"T.{q(col, quote_identifiers)}"
    c = f"CTL.{q(col, quote_identifiers)}"

    if mode == "native":
        return f"(({t} <> {c}) OR ({t} IS NULL AND {c} IS NOT NULL) OR ({t} IS NOT NULL AND {c} IS NULL))"

    if mode == "to_char":
        # Null-safe explicit string comparison without NVL.
        tt = f"TO_CHAR({t})"
        cc = f"TO_CHAR({c})"
        return f"(({tt} <> {cc}) OR ({tt} IS NULL AND {cc} IS NOT NULL) OR ({tt} IS NOT NULL AND {cc} IS NULL))"

    # User-style default.
    return f"NVL(TO_CHAR({t}), '{null_token}') <> NVL(TO_CHAR({c}), '{null_token}')"


def key_join_predicate(keys: List[str], null_safe_keys: bool, null_token: str, quote_identifiers: bool = False) -> str:
    preds = []
    for k in keys:
        t = f"T.{q(k, quote_identifiers)}"
        c = f"CTL.{q(k, quote_identifiers)}"
        if null_safe_keys:
            preds.append(f"NVL(TO_CHAR({t}), '{null_token}') = NVL(TO_CHAR({c}), '{null_token}')")
        else:
            preds.append(f"{t} = {c}")
    return "\n        AND ".join(preds)


def exists_join_predicate(keys: List[str], t_alias: str, ctl_alias: str, null_safe_keys: bool, null_token: str, quote_identifiers: bool = False) -> str:
    preds = []
    for k in keys:
        t = f"{t_alias}.{q(k, quote_identifiers)}"
        c = f"{ctl_alias}.{q(k, quote_identifiers)}"
        if null_safe_keys:
            preds.append(f"NVL(TO_CHAR({t}), '{null_token}') = NVL(TO_CHAR({c}), '{null_token}')")
        else:
            preds.append(f"{t} = {c}")
    return "\n        AND ".join(preds)


def key_select_list(keys: List[str], alias: str = "T", quote_identifiers: bool = False) -> str:
    return ", ".join(f"{alias}.{q(k, quote_identifiers)}" for k in keys)


def build_where_clause(base_where: str, alias: str) -> str:
    if not base_where:
        return ""
    # User should write aliases T/CTL if needed. We just include as is.
    return f"\nWHERE {base_where}"


def build_common_join(target_table: str, control_table: str, keys: List[str], parallel: int, null_safe_keys: bool, null_token: str, quote_identifiers: bool = False, target_where: str = "", control_where: str = "") -> str:
    target = table_ref(target_table)
    control = table_ref(control_table)

    target_source = f"{target} T"
    control_source = f"{control} CTL"

    # Filters can be pushed via inline views.
    if target_where:
        target_source = f"(SELECT /*+ PARALLEL({parallel}) */ * FROM {target} T_BASE WHERE {target_where}) T"
    if control_where:
        control_source = f"(SELECT /*+ PARALLEL({parallel}) */ * FROM {control} CTL_BASE WHERE {control_where}) CTL"

    return f"""FROM {target_source}
JOIN {control_source}
    ON {key_join_predicate(keys, null_safe_keys, null_token, quote_identifiers)}"""


def generate_row_count_sql(target_table: str, control_table: str, parallel: int, target_where: str, control_where: str) -> str:
    target = table_ref(target_table)
    control = table_ref(control_table)
    tw = f"WHERE {target_where}" if target_where else ""
    cw = f"WHERE {control_where}" if control_where else ""
    return f"""/*
00_row_count_check.sql

Compare row counts between target and control.
*/

SELECT 'TARGET' AS source_name, COUNT(*) AS row_count
FROM {target}
{tw}
UNION ALL
SELECT 'CONTROL' AS source_name, COUNT(*) AS row_count
FROM {control}
{cw};
"""


def generate_duplicate_grain_sql(target_table: str, control_table: str, keys: List[str], parallel: int, quote_identifiers: bool, target_where: str, control_where: str) -> str:
    target = table_ref(target_table)
    control = table_ref(control_table)
    key_list = ", ".join(q(k, quote_identifiers) for k in keys)
    tw = f"WHERE {target_where}" if target_where else ""
    cw = f"WHERE {control_where}" if control_where else ""
    return f"""/*
01_duplicate_grain_check.sql

Checks whether the grain/key is unique in each table.
If duplicates exist, field-by-field comparison can multiply rows and produce false mismatches.
*/

SELECT 'TARGET' AS source_name, {key_list}, COUNT(*) AS duplicate_count
FROM {target}
{tw}
GROUP BY {key_list}
HAVING COUNT(*) > 1
UNION ALL
SELECT 'CONTROL' AS source_name, {key_list}, COUNT(*) AS duplicate_count
FROM {control}
{cw}
GROUP BY {key_list}
HAVING COUNT(*) > 1;
"""


def generate_unmatched_rows_sql(target_table: str, control_table: str, keys: List[str], parallel: int, null_safe_keys: bool, null_token: str, quote_identifiers: bool, target_where: str, control_where: str) -> str:
    target = table_ref(target_table)
    control = table_ref(control_table)

    t_filter = f"AND ({target_where})" if target_where else ""
    c_filter = f"AND ({control_where})" if control_where else ""

    return f"""/*
02_unmatched_rows_check.sql

Finds rows present only in target or only in control by grain/key.
*/

SELECT /*+ PARALLEL(32) */
    'TARGET_ONLY' AS mismatch_type,
    {key_select_list(keys, 'T', quote_identifiers)}
FROM {target} T
WHERE 1 = 1
{t_filter}
  AND NOT EXISTS (
      SELECT 1
      FROM {control} CTL
      WHERE {exists_join_predicate(keys, 'T', 'CTL', null_safe_keys, null_token, quote_identifiers)}
      {c_filter}
  )
UNION ALL
SELECT /*+ PARALLEL(32) */
    'CONTROL_ONLY' AS mismatch_type,
    {key_select_list(keys, 'CTL', quote_identifiers)}
FROM {control} CTL
WHERE 1 = 1
{c_filter}
  AND NOT EXISTS (
      SELECT 1
      FROM {target} T
      WHERE {exists_join_predicate(keys, 'T', 'CTL', null_safe_keys, null_token, quote_identifiers)}
      {t_filter}
  );
"""


def generate_mismatch_summary_sql(target_table: str, control_table: str, keys: List[str], columns: List[str], parallel: int, compare_mode: str, null_token: str, null_safe_keys: bool, quote_identifiers: bool, target_where: str, control_where: str) -> str:
    join = build_common_join(target_table, control_table, keys, parallel, null_safe_keys, null_token, quote_identifiers, target_where, control_where)

    parts = []
    for col in columns:
        pred = mismatch_predicate(col, compare_mode, null_token, quote_identifiers)
        parts.append(f"""SELECT /*+ PARALLEL({parallel}) */
    '{col}' AS column_name,
    COUNT(*) AS mismatch_count
{join}
WHERE {pred}""")

    return """/*
03_field_mismatch_summary.sql

Returns one row per compared column with mismatch_count.

Default comparison:
NVL(TO_CHAR(T.COL), '<NULL>') <> NVL(TO_CHAR(CTL.COL), '<NULL>')
*/

""" + "\nUNION ALL\n".join(parts) + "\nORDER BY mismatch_count DESC, column_name;\n"


def generate_mismatch_details_sql(target_table: str, control_table: str, keys: List[str], columns: List[str], parallel: int, compare_mode: str, null_token: str, null_safe_keys: bool, quote_identifiers: bool, target_where: str, control_where: str, sample_limit: int) -> str:
    join = build_common_join(target_table, control_table, keys, parallel, null_safe_keys, null_token, quote_identifiers, target_where, control_where)
    key_list = key_select_list(keys, "T", quote_identifiers)

    parts = []
    for col in columns:
        pred = mismatch_predicate(col, compare_mode, null_token, quote_identifiers)
        t_val = value_expr("T", col, "to_char", null_token, quote_identifiers)
        c_val = value_expr("CTL", col, "to_char", null_token, quote_identifiers)
        parts.append(f"""/*
Column: {col}
*/
SELECT *
FROM (
    SELECT /*+ PARALLEL({parallel}) */
        '{col}' AS column_name,
        {key_list},
        {t_val} AS target_value,
        {c_val} AS control_value
    {join}
    WHERE {pred}
)
WHERE ROWNUM <= {sample_limit};
""")
    return """/*
04_field_mismatch_details.sql

Sample detail query for each compared column.
Each query returns grain/key + target/control values for mismatches.
*/

""" + "\n\n".join(parts)


def generate_all(args: argparse.Namespace) -> Path:
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    keys = split_csv_list(args.keys)
    if not keys:
        raise ValueError("--keys is required, comma-separated grain columns")

    columns = split_csv_list(args.columns)
    if args.columns_file:
        file_cols = read_columns_file(Path(args.columns_file).expanduser().resolve(), args.columns_field)
        columns.extend(file_cols)

    exclude = set(split_csv_list(args.exclude_columns))
    exclude.update(keys if args.exclude_keys else [])

    columns = [c for c in unique_keep_order(columns) if c not in exclude]
    if not columns:
        raise ValueError("No compare columns found. Use --columns or --columns-file.")

    target = table_ref(args.target_table)
    control = table_ref(args.control_table)

    config = {
        "target_table": target,
        "control_table": control,
        "keys": keys,
        "compare_columns": columns,
        "excluded_columns": sorted(exclude),
        "compare_mode": args.compare_mode,
        "null_token": args.null_token,
        "parallel": args.parallel,
        "null_safe_keys": args.null_safe_keys,
        "target_where": args.target_where,
        "control_where": args.control_where,
    }

    (out / "field_compare_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    (out / "00_row_count_check.sql").write_text(
        generate_row_count_sql(target, control, args.parallel, args.target_where, args.control_where),
        encoding="utf-8",
    )
    (out / "01_duplicate_grain_check.sql").write_text(
        generate_duplicate_grain_sql(target, control, keys, args.parallel, args.quote_identifiers, args.target_where, args.control_where),
        encoding="utf-8",
    )
    (out / "02_unmatched_rows_check.sql").write_text(
        generate_unmatched_rows_sql(target, control, keys, args.parallel, args.null_safe_keys, args.null_token, args.quote_identifiers, args.target_where, args.control_where),
        encoding="utf-8",
    )
    (out / "03_field_mismatch_summary.sql").write_text(
        generate_mismatch_summary_sql(target, control, keys, columns, args.parallel, args.compare_mode, args.null_token, args.null_safe_keys, args.quote_identifiers, args.target_where, args.control_where),
        encoding="utf-8",
    )
    (out / "04_field_mismatch_details.sql").write_text(
        generate_mismatch_details_sql(target, control, keys, columns, args.parallel, args.compare_mode, args.null_token, args.null_safe_keys, args.quote_identifiers, args.target_where, args.control_where, args.sample_limit),
        encoding="utf-8",
    )

    summary = {
        "output_dir": str(out),
        "target_table": target,
        "control_table": control,
        "key_count": len(keys),
        "compare_column_count": len(columns),
        "files": [
            "00_row_count_check.sql",
            "01_duplicate_grain_check.sql",
            "02_unmatched_rows_check.sql",
            "03_field_mismatch_summary.sql",
            "04_field_mismatch_details.sql",
            "field_compare_config.json",
        ],
    }
    (out / "field_compare_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not args.quiet:
        print(json.dumps(summary, indent=2))

    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Generate Oracle SQL to compare every field between target and control tables by grain.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--target-table", required=True, help="Real target table, e.g. TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT")
    p.add_argument("--control-table", required=True, help="Control table created by generated insert, e.g. IKOROSTELEV.CLS_TAX_LOTS_NON_BKR_FACT")
    p.add_argument("--keys", required=True, help="Comma-separated grain/key columns")
    p.add_argument("--columns", default="", help="Comma-separated compare columns")
    p.add_argument("--columns-file", default="", help="CSV file with compare columns, e.g. column_contract.csv or implementation_map.csv")
    p.add_argument("--columns-field", default="", help="Column name inside --columns-file; auto-detected if omitted")
    p.add_argument("--exclude-columns", default="", help="Comma-separated columns to exclude")
    p.add_argument("--exclude-keys", action="store_true", help="Exclude grain keys from compare column list")
    p.add_argument("--out", default="field_compare_sql", help="Output folder")

    p.add_argument("--parallel", type=int, default=32, help="Oracle PARALLEL hint")
    p.add_argument("--compare-mode", choices=["nvl_to_char", "to_char", "native"], default="nvl_to_char")
    p.add_argument("--null-token", default="-999", help="Null replacement token for nvl_to_char mode")
    p.add_argument("--null-safe-keys", action="store_true", help="Use NVL(TO_CHAR()) equality for grain keys")
    p.add_argument("--quote-identifiers", action="store_true", help="Quote column identifiers")
    p.add_argument("--target-where", default="", help="Optional filter applied to target side")
    p.add_argument("--control-where", default="", help="Optional filter applied to control side")
    p.add_argument("--sample-limit", type=int, default=100, help="Sample rows per column in details SQL")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        generate_all(args)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
