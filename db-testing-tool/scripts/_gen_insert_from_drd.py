"""Generate proper INSERT statement for AVY_FACT_SIDE based on DRD source mappings."""
import json
from pathlib import Path

MAPPINGS_PATH = Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_drd_mappings.json")
LIVE_COLS_PATH = Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_live_columns.json")
OUTPUT = Path(r"c:\GIT_Repo\db-testing-tool\reports\AVY_FACT_SIDE_insert_from_source.sql")

with open(MAPPINGS_PATH) as f:
    mappings = json.load(f)

with open(LIVE_COLS_PATH, encoding="utf-8-sig") as f:
    live_cols = {r["COLUMN_NAME"]: r for r in json.load(f)}

# Get unique source tables
sources = set()
for m in mappings:
    if m["src_table"] and m["src_schema"]:
        sources.add((m["src_schema"], m["src_table"]))

print("=== Source Tables ===")
for schema, table in sorted(sources):
    cols_from = sum(1 for mm in mappings if mm["src_schema"] == schema and mm["src_table"] == table)
    print(f"  {schema}.{table} ({cols_from} columns)")

# Build INSERT ... SELECT
# Primary source table is CCAL_REPL_OWNER.TXN (most columns come from it)
primary_table = "CCAL_REPL_OWNER.TXN"
primary_alias = "t"

# Group by source table to determine JOINs
table_aliases = {
    ("CCAL_REPL_OWNER", "TXN"): "t",
    ("CCAL_REPL_OWNER", "TXN_ENTRY"): "te",
    ("CCAL_REPL_OWNER", "TXN_RLTNP"): "tr",
    ("CCAL_REPL_OWNER", "SHDW_TXN_TP"): "stt",
    ("CCAL_REPL_OWNER", "CL_VAL"): "cv",
    ("CCAL_REPL_OWNER", "ACATS_BROKER"): "ab",
    ("CCAL_REPL_OWNER", "IMPCT_ACTION_LKU"): "ial",
    ("CCSI_OWNER", "AR_DIM"): "ard",
    ("CCSI_OWNER", "AR_GRP_SUBDIM"): "ags",
    ("COMMON_OWNER", "SRC_STM_DIM"): "ssd",
    ("COMMON_OWNER", "DOL_IND_DIM"): "did",
    ("COMMON_OWNER", "EXG_DIM"): "exd",
    ("COMMON_OWNER", "CCY_DIM"): "ccd",
    ("SSDS_DAL_OWNER", "FA_NUMBER_V"): "fn",
    ("SSDS_DAL_OWNER", "ENTERPRISE_ENTITY_DIM_V"): "eed",
    ("REFERENCE_REPL_OWNER", "IMPCT_ACTION_LKU"): "ial2",
}

# Only include columns that exist in live table
live_set = set(live_cols.keys())

# ETL-generated columns (not from source)
etl_cols = {
    "CRT_DTM": "SYSDATE",
    "CRT_USR_NM": "'ODI_ETL'",
    "LAST_UDT_USR_NM": "'ODI_ETL'",
    "LAST_UDT_DTM": "SYSDATE",
    "ACTV_F": "'Y'",
    "BATCH_DT": "TRUNC(SYSDATE)",
}

lines_target = []
lines_source = []

for m in mappings:
    col = m["target_col"]
    if col not in live_set:
        continue  # Skip columns not in live table

    # ETL columns
    if col in etl_cols:
        lines_target.append(col)
        lines_source.append(etl_cols[col])
        continue

    src_schema = m["src_schema"]
    src_table = m["src_table"]
    src_col = m["src_col"]
    transform = m["transform"]

    # Build source expression
    if transform and "CASE" in transform.upper():
        # Use transformation as-is (it's a CASE expression)
        expr = f"/* {transform[:100]} */ NULL"
    elif transform and ("JOIN" in transform.upper() or "SELECT" in transform.upper()):
        # Complex join - use alias.column if available
        alias = table_aliases.get((src_schema, src_table), src_table.lower()[:3])
        if src_col:
            expr = f"{alias}.{src_col}"
        else:
            expr = "NULL"
    elif src_col and src_schema and src_table:
        alias = table_aliases.get((src_schema, src_table), src_table.lower()[:3])
        expr = f"{alias}.{src_col}"
    elif src_col:
        expr = f"t.{src_col}"
    else:
        expr = "NULL"

    lines_target.append(col)
    lines_source.append(expr)

# Generate SQL
sql_lines = []
sql_lines.append("-- ============================================================================")
sql_lines.append("-- AVY_FACT_SIDE INSERT FROM SOURCE (DRD-based mapping)")
sql_lines.append("-- Target: TRANSACTIONS_OWNER.AVY_FACT_SIDE")
sql_lines.append("-- Primary Source: CCAL_REPL_OWNER.TXN")
sql_lines.append("-- Generated from DRD_Activity_Fact.xlsx mapping expressions")
sql_lines.append("-- ============================================================================")
sql_lines.append("")
sql_lines.append("INSERT INTO TRANSACTIONS_OWNER.AVY_FACT_SIDE (")

for i, col in enumerate(lines_target):
    comma = "," if i < len(lines_target) - 1 else ""
    sql_lines.append(f"    {col}{comma}")

sql_lines.append(")")
sql_lines.append("SELECT")

for i, expr in enumerate(lines_source):
    comma = "," if i < len(lines_source) - 1 else ""
    # Add comment with target col name for readability
    sql_lines.append(f"    {expr}{comma}  -- {lines_target[i]}")

sql_lines.append("FROM CCAL_REPL_OWNER.TXN t")
sql_lines.append("")
sql_lines.append("-- === JOINs (derived from DRD transformation rules) ===")

# Generate JOINs based on what's referenced
referenced_tables = set()
for m in mappings:
    if m["target_col"] in live_set and m["src_table"] and m["src_schema"]:
        key = (m["src_schema"], m["src_table"])
        if key != ("CCAL_REPL_OWNER", "TXN"):
            referenced_tables.add(key)

join_conditions = {
    ("CCAL_REPL_OWNER", "TXN_ENTRY"): "LEFT JOIN CCAL_REPL_OWNER.TXN_ENTRY te ON te.TXN_ID = t.TXN_ID",
    ("CCAL_REPL_OWNER", "TXN_RLTNP"): "LEFT JOIN CCAL_REPL_OWNER.TXN_RLTNP tr ON tr.TXN_ID = t.TXN_ID",
    ("CCAL_REPL_OWNER", "SHDW_TXN_TP"): "LEFT JOIN CCAL_REPL_OWNER.SHDW_TXN_TP stt ON stt.SRC_TXN_TP = t.SRC_TXN_TP",
    ("CCAL_REPL_OWNER", "CL_VAL"): "LEFT JOIN CCAL_REPL_OWNER.CL_VAL cv ON cv.CL_VAL_ID = t.SRC_BUY_SELL_MULTI_ID",
    ("CCAL_REPL_OWNER", "ACATS_BROKER"): "LEFT JOIN CCAL_REPL_OWNER.ACATS_BROKER ab ON ab.BROKER_ID = t.CNTRA_BROKER_ID",
    ("CCAL_REPL_OWNER", "IMPCT_ACTION_LKU"): "LEFT JOIN CCAL_REPL_OWNER.IMPCT_ACTION_LKU ial ON ial.IMPCT_ACTION_ID = t.IMPCT_ACTION_ID",
    ("CCSI_OWNER", "AR_DIM"): "LEFT JOIN CCSI_OWNER.AR_DIM ard ON ard.AR_ID = t.AR_ID AND ard.ACTV_F = 'Y'",
    ("CCSI_OWNER", "AR_GRP_SUBDIM"): "LEFT JOIN CCSI_OWNER.AR_GRP_SUBDIM ags ON ags.AR_ID = t.AR_ID AND ags.ACTV_F = 'Y'",
    ("COMMON_OWNER", "SRC_STM_DIM"): "LEFT JOIN COMMON_OWNER.SRC_STM_DIM ssd ON ssd.SRC_STM_ID = t.SRC_STM_ID",
    ("COMMON_OWNER", "DOL_IND_DIM"): "LEFT JOIN COMMON_OWNER.DOL_IND_DIM did ON did.DOL_IND_ID = te.DOL_IND_ID",
    ("COMMON_OWNER", "EXG_DIM"): "LEFT JOIN COMMON_OWNER.EXG_DIM exd ON exd.EXG_CD = te.EXG_CD",
    ("COMMON_OWNER", "CCY_DIM"): "LEFT JOIN COMMON_OWNER.CCY_DIM ccd ON ccd.CCY_CD = te.SBC_CCY_CD",
    ("SSDS_DAL_OWNER", "FA_NUMBER_V"): "LEFT JOIN SSDS_DAL_OWNER.FA_NUMBER_V fn ON fn.FA_NUMBER = ags.FA_NUM",
    ("SSDS_DAL_OWNER", "ENTERPRISE_ENTITY_DIM_V"): "LEFT JOIN SSDS_DAL_OWNER.ENTERPRISE_ENTITY_DIM_V eed ON eed.ENTITY_CODE = fn.FA_NUMBER_ENTITY_CODE",
    ("REFERENCE_REPL_OWNER", "IMPCT_ACTION_LKU"): "LEFT JOIN REFERENCE_REPL_OWNER.IMPCT_ACTION_LKU ial2 ON ial2.IMPCT_ACTION_ID = t.IMPCT_ACTION_ID",
}

for tbl in sorted(referenced_tables):
    if tbl in join_conditions:
        sql_lines.append(join_conditions[tbl])
    else:
        alias = table_aliases.get(tbl, tbl[1].lower()[:3])
        sql_lines.append(f"-- LEFT JOIN {tbl[0]}.{tbl[1]} {alias} ON ???  -- TODO: determine join condition")

sql_lines.append("")
sql_lines.append("WHERE 1=1")
sql_lines.append("    -- AND t.BATCH_DT = TRUNC(SYSDATE)  -- Filter for current batch")
sql_lines.append(";")

# Write file
output_text = "\n".join(sql_lines)
OUTPUT.write_text(output_text, encoding="utf-8")
print(f"\nGenerated: {OUTPUT}")
print(f"Columns: {len(lines_target)}")
print(f"Lines: {len(sql_lines)}")
print(f"File size: {OUTPUT.stat().st_size} bytes")
