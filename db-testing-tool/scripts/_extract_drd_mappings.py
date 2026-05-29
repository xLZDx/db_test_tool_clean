"""Extract source mapping expressions from DRD Excel for AVY_FACT_SIDE INSERT generation."""
import openpyxl
import json
from pathlib import Path

DRD_PATH = r"c:\Users\ikorostelev\Downloads\DRD_Activity_Fact.xlsx"
OUTPUT = Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_drd_mappings.json")

wb = openpyxl.load_workbook(DRD_PATH, read_only=True, data_only=True)
ws = wb["Table-View (2)"]

mappings = []
for row in ws.iter_rows(min_row=13, max_col=34, values_only=True):
    phys = row[1]
    if not phys or not isinstance(phys, str):
        continue
    phys = phys.strip().upper()
    if not phys or phys.startswith("--"):
        continue
    src_schema = str(row[24]).strip() if row[24] else ""
    src_table = str(row[25]).strip() if row[25] else ""
    src_col = str(row[26]).strip() if row[26] else ""
    transform = str(row[29]).strip() if row[29] else ""
    nullable = str(row[4]).strip().upper() if row[4] else "NULL"
    dtype = str(row[3]).strip() if row[3] else ""
    mappings.append({
        "target_col": phys,
        "src_schema": src_schema if src_schema != "None" else "",
        "src_table": src_table if src_table != "None" else "",
        "src_col": src_col if src_col != "None" else "",
        "transform": transform if transform != "None" else "",
        "nullable": nullable,
        "dtype": dtype,
    })

# Save full JSON
with open(OUTPUT, "w") as f:
    json.dump(mappings, f, indent=2)

# Print summary
total = len(mappings)
with_src = sum(1 for m in mappings if m["src_col"])
with_transform = sum(1 for m in mappings if m["transform"])
print(f"Total columns: {total}")
print(f"With source column: {with_src}")
print(f"With transformation: {with_transform}")
print(f"\nFirst 30 mappings with source info:")
print(f"{'TARGET':<35} {'SRC_SCHEMA':<20} {'SRC_TABLE':<35} {'SRC_COL':<30} {'TRANSFORM':<80}")
print("-" * 200)
shown = 0
for m in mappings:
    if m["src_col"] or m["transform"]:
        print(f"{m['target_col']:<35} {m['src_schema']:<20} {m['src_table']:<35} {m['src_col']:<30} {m['transform'][:80]}")
        shown += 1
        if shown >= 30:
            break

wb.close()
