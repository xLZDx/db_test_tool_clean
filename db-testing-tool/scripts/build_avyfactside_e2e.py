"""
Full E2E: Compare DRD vs Live Table vs ODI, generate INSERT and test coverage SQL.
Target: TRANSACTIONS_OWNER.AVY_FACT_SIDE on LH (ds_id=3)
"""
import json
import re
import openpyxl
from pathlib import Path

DRD_PATH = r"c:\Users\ikorostelev\Downloads\DRD_Activity_Fact.xlsx"
ODI_PATH = r"c:\Users\ikorostelev\Downloads\1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
LIVE_COLS_PATH = Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_live_columns.json")
OUTPUT_DIR = Path(r"c:\GIT_Repo\db-testing-tool\reports")

TARGET_SCHEMA = "TRANSACTIONS_OWNER"
TARGET_TABLE = "AVY_FACT_SIDE"


def parse_drd_columns():
    """Parse DRD Excel for physical column names and Oracle data types."""
    wb = openpyxl.load_workbook(DRD_PATH, read_only=True, data_only=True)
    ws = wb["Table-View (2)"]
    columns = []
    for row in ws.iter_rows(min_row=13, max_col=15, values_only=True):
        logical_name = row[0]
        physical_name = row[1]
        oracle_dtype = row[3]
        nullable = row[4]
        if not physical_name or not isinstance(physical_name, str):
            continue
        physical_name = physical_name.strip().upper()
        if not physical_name or physical_name.startswith("--"):
            continue
        dtype = str(oracle_dtype).strip() if oracle_dtype else "VARCHAR2(4000)"
        if nullable and str(nullable).strip().upper() in ("YES", "NULL", "Y"):
            null_str = "NULL"
        else:
            null_str = "NOT NULL"
        columns.append({
            "logical_name": str(logical_name).strip() if logical_name else "",
            "physical_name": physical_name,
            "oracle_dtype": dtype,
            "nullable": null_str,
        })
    wb.close()
    return columns


def parse_live_columns():
    """Load live table columns from JSON export."""
    with open(LIVE_COLS_PATH, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    cols = {}
    for row in data:
        name = row["COLUMN_NAME"]
        dtype = row["DATA_TYPE"]
        length = row.get("DATA_LENGTH")
        precision = row.get("DATA_PRECISION")
        scale = row.get("DATA_SCALE")
        nullable = row.get("NULLABLE", "Y")
        
        # Build full type string
        if dtype == "NUMBER":
            if precision:
                full_type = f"NUMBER({precision},{scale or 0})"
            else:
                full_type = "NUMBER"
        elif dtype in ("VARCHAR2", "CHAR"):
            full_type = f"{dtype}({length})"
        elif dtype == "TIMESTAMP(6)":
            full_type = "TIMESTAMP(6)"
        else:
            full_type = dtype
        
        cols[name] = {
            "dtype": full_type,
            "nullable": "NULL" if nullable == "Y" else "NOT NULL"
        }
    return cols


def parse_odi_step3_columns():
    """Parse ODI XML STEP3_STG create table for column definitions."""
    with open(ODI_PATH, "r", encoding="iso-8859-1") as f:
        content = f.read()
    
    # Find create table for STEP3_STG
    pattern = r"create table.*?SSDS_AVY_FACT_STEP3_STG.*?\(\n(.*?)\)\n"
    match = re.search(pattern, content, re.DOTALL)
    cols = {}
    if match:
        for line in match.group(1).strip().split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) >= 2:
                col_name = parts[0].upper()
                rest = parts[1]
                null_match = re.search(r"(NULL|NOT NULL)\s*$", rest, re.I)
                nullable = null_match.group(1).upper() if null_match else "NULL"
                dtype = rest[:null_match.start()].strip() if null_match else rest.strip()
                cols[col_name] = {"dtype": dtype, "nullable": nullable}
    return cols


def build_comparison(drd_cols, live_cols, odi_cols):
    """Three-way comparison: DRD vs Live vs ODI."""
    report_lines = []
    report_lines.append(f"THREE-WAY COMPARISON: DRD vs LIVE ({TARGET_SCHEMA}.{TARGET_TABLE}) vs ODI")
    report_lines.append("=" * 120)
    report_lines.append(f"DRD columns: {len(drd_cols)}")
    report_lines.append(f"Live table columns: {len(live_cols)}")
    report_lines.append(f"ODI STEP3_STG columns: {len(odi_cols)}")
    report_lines.append("")
    
    drd_set = {c["physical_name"] for c in drd_cols}
    live_set = set(live_cols.keys())
    odi_set = set(odi_cols.keys())
    
    # Valid mismatches only (not ETL-internal columns)
    etl_internal = {"SESS_NO", "CRT_DTM", "CRT_USR_NM", "LAST_UDT_USR_NM", 
                    "LAST_UDT_DTM", "ACTV_F", "BATCH_DT", "ROWNM"}
    
    mismatches = []
    
    # 1. Columns in DRD but not in live table (need investigation)
    drd_not_live = drd_set - live_set
    if drd_not_live:
        report_lines.append(f"\n--- IN DRD BUT NOT IN LIVE TABLE ({len(drd_not_live)}) ---")
        report_lines.append(f"{'COLUMN':<40} {'DRD TYPE':<25} {'IN ODI?':<10}")
        report_lines.append("-" * 75)
        for col in sorted(drd_not_live):
            drd_info = next(c for c in drd_cols if c["physical_name"] == col)
            in_odi = "YES" if col in odi_set else "NO"
            report_lines.append(f"{col:<40} {drd_info['oracle_dtype']:<25} {in_odi}")
            mismatches.append({"col": col, "issue": "IN_DRD_NOT_LIVE", "drd_type": drd_info['oracle_dtype']})
    
    # 2. Columns in live table but not in DRD (ETL-added)
    live_not_drd = live_set - drd_set - etl_internal
    if live_not_drd:
        report_lines.append(f"\n--- IN LIVE TABLE BUT NOT IN DRD ({len(live_not_drd)}) ---")
        report_lines.append(f"{'COLUMN':<40} {'LIVE TYPE':<25} {'IN ODI?':<10}")
        report_lines.append("-" * 75)
        for col in sorted(live_not_drd):
            in_odi = "YES" if col in odi_set else "NO"
            report_lines.append(f"{col:<40} {live_cols[col]['dtype']:<25} {in_odi}")
    
    # 3. Type mismatches between DRD and live table
    common = drd_set & live_set
    type_mismatches = []
    for col in sorted(common):
        drd_info = next(c for c in drd_cols if c["physical_name"] == col)
        live_info = live_cols[col]
        drd_type_norm = re.sub(r"\s+", "", drd_info["oracle_dtype"].upper())
        live_type_norm = re.sub(r"\s+", "", live_info["dtype"].upper())
        # Normalize NUMBER(38) == NUMBER(38,0)
        drd_type_norm = drd_type_norm.replace("NUMBER(38)", "NUMBER(38,0)")
        live_type_norm = live_type_norm.replace("NUMBER(38)", "NUMBER(38,0)")
        if drd_type_norm != live_type_norm:
            # Filter out trivial size differences (VARCHAR2 length)
            drd_base = re.match(r"(\w+)", drd_type_norm).group(1) if drd_type_norm else ""
            live_base = re.match(r"(\w+)", live_type_norm).group(1) if live_type_norm else ""
            if drd_base != live_base:
                type_mismatches.append((col, drd_info["oracle_dtype"], live_info["dtype"]))
            else:
                # Same base type, different size - only flag if DRD > live (potential truncation)
                type_mismatches.append((col, drd_info["oracle_dtype"], live_info["dtype"]))
    
    if type_mismatches:
        report_lines.append(f"\n--- DATA TYPE MISMATCHES (DRD vs LIVE) ({len(type_mismatches)}) ---")
        report_lines.append(f"{'COLUMN':<40} {'DRD TYPE':<25} {'LIVE TYPE':<25}")
        report_lines.append("-" * 90)
        for col, drd_t, live_t in type_mismatches:
            report_lines.append(f"{col:<40} {drd_t:<25} {live_t}")
    
    report_lines.append(f"\n\n--- SUMMARY ---")
    report_lines.append(f"Columns matching (DRD & Live): {len(common)}")
    report_lines.append(f"In DRD not Live: {len(drd_not_live)}")
    report_lines.append(f"In Live not DRD (excl ETL): {len(live_not_drd)}")
    report_lines.append(f"Type mismatches: {len(type_mismatches)}")
    
    return "\n".join(report_lines), common, type_mismatches


def generate_insert_sql(drd_cols, live_cols):
    """Generate INSERT using only columns that exist in the live table."""
    # Use DRD columns that exist in live table
    valid_cols = [c for c in drd_cols if c["physical_name"] in live_cols]
    col_names = [c["physical_name"] for c in valid_cols]
    
    # Generate INSERT with test values (NULL for first pass)
    lines = [f"INSERT INTO {TARGET_SCHEMA}.{TARGET_TABLE} ("]
    lines.append("    " + ",\n    ".join(col_names))
    lines.append(") SELECT")
    
    # Generate NULL for each column
    select_parts = []
    for c in valid_cols:
        select_parts.append(f"    NULL")
    lines.append(",\n".join(select_parts))
    lines.append("FROM DUAL")
    
    return "\n".join(lines), len(valid_cols)


def generate_test_coverage_sql(live_cols):
    """Generate test SQL statements for data validation."""
    sqls = []
    
    # Test 1: Verify table exists and has expected column count
    sqls.append(f"""-- TEST 1: Table exists with expected column count
SELECT COUNT(*) AS COL_COUNT FROM ALL_TAB_COLUMNS 
WHERE OWNER='{TARGET_SCHEMA}' AND TABLE_NAME='{TARGET_TABLE}'""")
    
    # Test 2: Row count check
    sqls.append(f"""-- TEST 2: Row count
SELECT COUNT(*) AS ROW_COUNT FROM {TARGET_SCHEMA}.{TARGET_TABLE}""")
    
    # Test 3: Check NOT NULL columns have data
    sqls.append(f"""-- TEST 3: Verify key columns are populated (sample top 10 rows)
SELECT TXN_ID, TD, AR_ID, SRC_STM_ID, TXN_TP_CD, BUY_SELL_IND
FROM {TARGET_SCHEMA}.{TARGET_TABLE}
WHERE ROWNUM <= 10""")
    
    # Test 4: Check data types match expectations
    sqls.append(f"""-- TEST 4: Validate data type distribution
SELECT DATA_TYPE, COUNT(*) AS CNT
FROM ALL_TAB_COLUMNS 
WHERE OWNER='{TARGET_SCHEMA}' AND TABLE_NAME='{TARGET_TABLE}'
GROUP BY DATA_TYPE
ORDER BY CNT DESC""")
    
    # Test 5: Check for NULL percentage on critical columns
    sqls.append(f"""-- TEST 5: NULL percentage on key columns (sample 1000 rows)
SELECT 
    COUNT(*) AS TOTAL_ROWS,
    SUM(CASE WHEN TXN_ID IS NULL THEN 1 ELSE 0 END) AS NULL_TXN_ID,
    SUM(CASE WHEN TD IS NULL THEN 1 ELSE 0 END) AS NULL_TD,
    SUM(CASE WHEN AR_ID IS NULL THEN 1 ELSE 0 END) AS NULL_AR_ID,
    SUM(CASE WHEN SRC_STM_ID IS NULL THEN 1 ELSE 0 END) AS NULL_SRC_STM_ID,
    SUM(CASE WHEN SHDW_TXN_TP_CD IS NULL THEN 1 ELSE 0 END) AS NULL_SHDW_TXN_TP_CD
FROM (SELECT * FROM {TARGET_SCHEMA}.{TARGET_TABLE} WHERE ROWNUM <= 1000)""")
    
    # Test 6: Verify foreign key references are valid (dimension IDs)
    sqls.append(f"""-- TEST 6: Check dimension ID validity
SELECT 
    COUNT(*) AS TOTAL,
    SUM(CASE WHEN EXG_DIM_ID = 0 THEN 1 ELSE 0 END) AS DEFAULT_EXG_DIM,
    SUM(CASE WHEN AR_DIM_ID = 0 THEN 1 ELSE 0 END) AS DEFAULT_AR_DIM,
    SUM(CASE WHEN TD_DIM_ID = 0 THEN 1 ELSE 0 END) AS DEFAULT_TD_DIM
FROM (SELECT * FROM {TARGET_SCHEMA}.{TARGET_TABLE} WHERE ROWNUM <= 1000)""")
    
    # Test 7: Date range validation
    sqls.append(f"""-- TEST 7: Date range validation
SELECT 
    MIN(TD) AS MIN_TD,
    MAX(TD) AS MAX_TD,
    MIN(SD) AS MIN_SD,
    MAX(SD) AS MAX_SD,
    COUNT(DISTINCT TRUNC(TD)) AS DISTINCT_TRADE_DATES
FROM {TARGET_SCHEMA}.{TARGET_TABLE}
WHERE ROWNUM <= 10000""")
    
    # Test 8: Source system distribution  
    sqls.append(f"""-- TEST 8: Source system distribution
SELECT SRC_STM_CD, SRC_STM_NM, COUNT(*) AS CNT
FROM {TARGET_SCHEMA}.{TARGET_TABLE}
WHERE ROWNUM <= 50000
GROUP BY SRC_STM_CD, SRC_STM_NM
ORDER BY CNT DESC""")
    
    # Test 9: Transaction type coverage
    sqls.append(f"""-- TEST 9: Transaction type coverage
SELECT TXN_TP_CD, TXN_TP_NM, COUNT(*) AS CNT
FROM {TARGET_SCHEMA}.{TARGET_TABLE}
WHERE ROWNUM <= 50000
GROUP BY TXN_TP_CD, TXN_TP_NM
ORDER BY CNT DESC""")
    
    # Test 10: Verify SHDW_TXN_TP_CD values match expected list
    sqls.append(f"""-- TEST 10: Shadow transaction type values
SELECT SHDW_TXN_TP_CD, COUNT(*) AS CNT
FROM {TARGET_SCHEMA}.{TARGET_TABLE}
WHERE ROWNUM <= 50000
GROUP BY SHDW_TXN_TP_CD
ORDER BY CNT DESC""")
    
    return ";\n\n".join(sqls)


def main():
    print("=" * 70)
    print(f"AVY_FACT_SIDE E2E: Compare DRD vs Live vs ODI + Generate SQL")
    print(f"Target: {TARGET_SCHEMA}.{TARGET_TABLE} on LH datasource")
    print("=" * 70)
    
    # Parse sources
    print("\n[1] Parsing DRD Excel...")
    drd_cols = parse_drd_columns()
    print(f"    DRD columns: {len(drd_cols)}")
    
    print("\n[2] Loading live table columns...")
    live_cols = parse_live_columns()
    print(f"    Live columns: {len(live_cols)}")
    
    print("\n[3] Parsing ODI XML...")
    odi_cols = parse_odi_step3_columns()
    print(f"    ODI STEP3 columns: {len(odi_cols)}")
    
    # Comparison
    print("\n[4] Building three-way comparison...")
    report, common_cols, type_mismatches = build_comparison(drd_cols, live_cols, odi_cols)
    report_path = OUTPUT_DIR / "avyfactside_3way_comparison.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"    Saved: {report_path}")
    print(f"    Common columns (DRD ∩ Live): {len(common_cols)}")
    print(f"    Type mismatches: {len(type_mismatches)}")
    
    # Print mismatch summary
    if type_mismatches:
        print("\n    TYPE MISMATCHES (showing first 15):")
        for col, drd_t, live_t in type_mismatches[:15]:
            print(f"      {col:<35} DRD:{drd_t:<20} LIVE:{live_t}")
    
    # INSERT statement
    print("\n[5] Generating INSERT statement...")
    insert_sql, col_count = generate_insert_sql(drd_cols, live_cols)
    insert_path = OUTPUT_DIR / "avyfactside_insert_lh.sql"
    with open(insert_path, "w") as f:
        f.write(insert_sql + ";\n")
    print(f"    Saved: {insert_path} ({col_count} columns)")
    
    # Test coverage SQL
    print("\n[6] Generating test coverage SQL...")
    test_sql = generate_test_coverage_sql(live_cols)
    test_path = OUTPUT_DIR / "avyfactside_test_coverage.sql"
    with open(test_path, "w") as f:
        f.write(test_sql + ";\n")
    print(f"    Saved: {test_path}")
    
    print("\n" + "=" * 70)
    print("DONE. Files ready for execution via DB Tool GUI:")
    print(f"  1. {insert_path}")
    print(f"  2. {test_path}")
    print(f"  3. {report_path} (comparison)")
    print("=" * 70)


if __name__ == "__main__":
    main()
