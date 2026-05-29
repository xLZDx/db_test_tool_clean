"""
Build AVY_FACT_SIDE CREATE TABLE, INSERT, and comparison report
from DRD Excel (column definitions) + ODI XML (PDM data types).

Target: SSDS_TRANSACTIONS_OWNER.AVY_FACT_SIDE on LH datasource
"""
import re
import openpyxl
from pathlib import Path

DRD_PATH = r"c:\Users\ikorostelev\Downloads\DRD_Activity_Fact.xlsx"
ODI_PATH = r"c:\Users\ikorostelev\Downloads\1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
OUTPUT_DIR = Path(r"c:\GIT_Repo\db-testing-tool\reports")
OUTPUT_DIR.mkdir(exist_ok=True)

TARGET_SCHEMA = "SSDS_TRANSACTIONS_OWNER"
TARGET_TABLE = "AVY_FACT_SIDE"


def parse_drd_columns():
    """Parse DRD Excel Table-View (2) for column definitions."""
    wb = openpyxl.load_workbook(DRD_PATH, read_only=True, data_only=True)
    ws = wb["Table-View (2)"]
    
    columns = []
    # Row 12 is the header, data starts at row 13
    for row in ws.iter_rows(min_row=13, max_col=15, values_only=True):
        logical_name = row[0]
        physical_name = row[1]
        oracle_dtype = row[3]
        nullable = row[4]
        
        if not physical_name or not isinstance(physical_name, str):
            continue
        physical_name = physical_name.strip()
        if not physical_name or physical_name.startswith("--"):
            continue
            
        # Clean data type
        dtype = str(oracle_dtype).strip() if oracle_dtype else "VARCHAR2(4000)"
        # Normalize nullable
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


def parse_odi_step3_columns():
    """Parse ODI XML STEP3_STG create table for PDM data types."""
    with open(ODI_PATH, "r", encoding="iso-8859-1") as f:
        content = f.read()
    
    # Find the STEP3_STG create table statement
    pattern = r"create table.*?SSDS_AVY_FACT_STEP3_STG.*?\(\s*\n(.*?)\)\s*\n"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        # Try alternate pattern
        pattern2 = r"create table.*?#SSDS\.SSDS_AVY_FACT_STEP3_STG.*?\(\n(.*?)\)\s*$"
        match = re.search(pattern2, content, re.DOTALL | re.MULTILINE)
    
    columns = {}
    if match:
        col_block = match.group(1)
        for line in col_block.strip().split("\n"):
            line = line.strip().rstrip(",")
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) >= 2:
                col_name = parts[0]
                rest = parts[1]
                # Extract type and nullable
                null_match = re.search(r"(NULL|NOT NULL)\s*$", rest, re.I)
                nullable = null_match.group(1) if null_match else "NULL"
                dtype = rest[:null_match.start()].strip() if null_match else rest.strip()
                columns[col_name.upper()] = {
                    "dtype": dtype,
                    "nullable": nullable.upper()
                }
    
    # Also parse STEP4 insert column list for the full final column set
    pattern_insert = r"insert.*?into.*?SSDS_AVY_FACT_STEP4_STG.*?\(\n(.*?)\)\nselect"
    match_ins = re.search(pattern_insert, content, re.DOTALL)
    step4_cols = []
    if match_ins:
        col_block = match_ins.group(1)
        for line in col_block.strip().split("\n"):
            col = line.strip().rstrip(",")
            if col:
                step4_cols.append(col.upper())
    
    return columns, step4_cols


def build_comparison(drd_cols, odi_cols):
    """Compare DRD columns vs ODI columns, return mismatches."""
    mismatches = []
    drd_set = {c["physical_name"].upper() for c in drd_cols}
    odi_set = set(odi_cols.keys())
    
    # In DRD but not ODI
    for col in drd_cols:
        pname = col["physical_name"].upper()
        if pname not in odi_set:
            mismatches.append({
                "column": col["physical_name"],
                "issue": "IN_DRD_NOT_IN_ODI",
                "drd_type": col["oracle_dtype"],
                "odi_type": "-",
            })
        else:
            # Check type mismatch
            odi_type = odi_cols[pname]["dtype"]
            drd_type = col["oracle_dtype"]
            # Normalize for comparison
            drd_norm = re.sub(r"\s+", "", drd_type.upper())
            odi_norm = re.sub(r"\s+", "", odi_type.upper())
            if drd_norm != odi_norm:
                mismatches.append({
                    "column": col["physical_name"],
                    "issue": "TYPE_MISMATCH",
                    "drd_type": drd_type,
                    "odi_type": odi_type,
                })
    
    # In ODI but not DRD
    for col_name in odi_set:
        if col_name not in drd_set:
            mismatches.append({
                "column": col_name,
                "issue": "IN_ODI_NOT_IN_DRD",
                "drd_type": "-",
                "odi_type": odi_cols[col_name]["dtype"],
            })
    
    return mismatches


def generate_create_table(drd_cols, odi_cols):
    """Generate CREATE TABLE using DRD column names with ODI PDM data types."""
    lines = [f"CREATE TABLE {TARGET_SCHEMA}.{TARGET_TABLE} ("]
    
    col_defs = []
    for col in drd_cols:
        pname = col["physical_name"].upper()
        # Use ODI data type if available, otherwise DRD type
        if pname in odi_cols:
            dtype = odi_cols[pname]["dtype"]
            nullable = odi_cols[pname]["nullable"]
        else:
            dtype = col["oracle_dtype"]
            nullable = col["nullable"]
        
        col_defs.append(f"    {pname} {dtype} {nullable}")
    
    lines.append(",\n".join(col_defs))
    lines.append(")")
    return "\n".join(lines)


def generate_insert(drd_cols):
    """Generate INSERT with all NULL values for testing."""
    col_names = [col["physical_name"].upper() for col in drd_cols]
    
    lines = [f"INSERT INTO {TARGET_SCHEMA}.{TARGET_TABLE} ("]
    lines.append("    " + ",\n    ".join(col_names))
    lines.append(") VALUES (")
    lines.append("    " + ",\n    ".join(["NULL"] * len(col_names)))
    lines.append(")")
    return "\n".join(lines)


def generate_validation_sql():
    """Generate validation queries."""
    sqls = []
    sqls.append(f"-- Validation 1: Table exists and column count\nSELECT COUNT(*) AS COL_COUNT FROM ALL_TAB_COLUMNS WHERE OWNER='{TARGET_SCHEMA}' AND TABLE_NAME='{TARGET_TABLE}'")
    sqls.append(f"-- Validation 2: Row count\nSELECT COUNT(*) AS ROW_COUNT FROM {TARGET_SCHEMA}.{TARGET_TABLE}")
    sqls.append(f"-- Validation 3: Column listing\nSELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE FROM ALL_TAB_COLUMNS WHERE OWNER='{TARGET_SCHEMA}' AND TABLE_NAME='{TARGET_TABLE}' ORDER BY COLUMN_ID")
    return ";\n\n".join(sqls)


def main():
    print("=" * 60)
    print("Building AVY_FACT_SIDE SQL from DRD + ODI PDM")
    print("=" * 60)
    
    # 1. Parse DRD
    print("\n[1] Parsing DRD Excel...")
    drd_cols = parse_drd_columns()
    print(f"    Found {len(drd_cols)} columns in DRD")
    
    # 2. Parse ODI XML
    print("\n[2] Parsing ODI XML for PDM data types...")
    odi_cols, step4_cols = parse_odi_step3_columns()
    print(f"    Found {len(odi_cols)} columns in ODI STEP3_STG")
    print(f"    Found {len(step4_cols)} columns in ODI STEP4 insert")
    
    # 3. Comparison
    print("\n[3] Comparing DRD vs ODI...")
    mismatches = build_comparison(drd_cols, odi_cols)
    
    # Filter only valid/meaningful mismatches
    valid_mismatches = [m for m in mismatches if m["issue"] != "IN_ODI_NOT_IN_DRD" 
                        or m["column"] not in ("SESS_NO", "CRT_DTM", "CRT_USR_NM", 
                        "LAST_UDT_USR_NM", "LAST_UDT_DTM", "ACTV_F", "BATCH_DT", "ROWNM")]
    
    print(f"    Total mismatches: {len(mismatches)}")
    print(f"    Valid mismatches (excluding ETL-only cols): {len(valid_mismatches)}")
    
    # Save comparison report
    report_path = OUTPUT_DIR / "avyfactside_drd_vs_odi_comparison.txt"
    with open(report_path, "w") as f:
        f.write(f"DRD vs ODI Comparison Report - {TARGET_SCHEMA}.{TARGET_TABLE}\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"DRD columns: {len(drd_cols)}\n")
        f.write(f"ODI STEP3_STG columns: {len(odi_cols)}\n\n")
        f.write(f"{'COLUMN':<40} {'ISSUE':<20} {'DRD TYPE':<25} {'ODI TYPE':<25}\n")
        f.write("-" * 110 + "\n")
        for m in valid_mismatches:
            f.write(f"{m['column']:<40} {m['issue']:<20} {m['drd_type']:<25} {m['odi_type']:<25}\n")
    print(f"    Saved: {report_path}")
    
    # 4. Generate CREATE TABLE
    print("\n[4] Generating CREATE TABLE...")
    ddl = generate_create_table(drd_cols, odi_cols)
    ddl_path = OUTPUT_DIR / "avyfactside_create_table.sql"
    with open(ddl_path, "w") as f:
        f.write(f"-- DROP TABLE {TARGET_SCHEMA}.{TARGET_TABLE};\n\n")
        f.write(ddl + ";\n")
    print(f"    Saved: {ddl_path} ({len(drd_cols)} columns)")
    
    # 5. Generate INSERT
    print("\n[5] Generating INSERT statement...")
    insert_sql = generate_insert(drd_cols)
    insert_path = OUTPUT_DIR / "avyfactside_insert.sql"
    with open(insert_path, "w") as f:
        f.write(insert_sql + ";\n")
    print(f"    Saved: {insert_path}")
    
    # 6. Generate validation
    print("\n[6] Generating validation SQL...")
    val_sql = generate_validation_sql()
    val_path = OUTPUT_DIR / "avyfactside_validation.sql"
    with open(val_path, "w") as f:
        f.write(val_sql + ";\n")
    print(f"    Saved: {val_path}")
    
    # Print summary of valid mismatches
    print("\n" + "=" * 60)
    print("VALID MISMATCHES (DRD vs ODI):")
    print("=" * 60)
    type_mismatches = [m for m in valid_mismatches if m["issue"] == "TYPE_MISMATCH"]
    in_drd_only = [m for m in valid_mismatches if m["issue"] == "IN_DRD_NOT_IN_ODI"]
    in_odi_only = [m for m in valid_mismatches if m["issue"] == "IN_ODI_NOT_IN_DRD"]
    
    if type_mismatches:
        print(f"\nTYPE MISMATCHES ({len(type_mismatches)}):")
        for m in type_mismatches[:20]:
            print(f"  {m['column']:<35} DRD: {m['drd_type']:<20} ODI: {m['odi_type']}")
    
    if in_drd_only:
        print(f"\nIN DRD ONLY ({len(in_drd_only)}):")
        for m in in_drd_only[:10]:
            print(f"  {m['column']}")
    
    if in_odi_only:
        print(f"\nIN ODI ONLY ({len(in_odi_only)}):")
        for m in in_odi_only[:10]:
            print(f"  {m['column']}")
    
    print("\nDone!")


if __name__ == "__main__":
    main()
