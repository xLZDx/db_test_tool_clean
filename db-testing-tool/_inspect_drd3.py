"""Full column inspection of Table-View (2) header row and source mapping."""
import openpyxl
import json

wb = openpyxl.load_workbook("DRD_Activity_Fact.xlsx", read_only=True, data_only=True)
ws = wb["Table-View (2)"]

rows = list(ws.iter_rows(values_only=True))
header_row = rows[11]  # Row 12 (0-indexed 11)
print("=== All 34 header columns ===")
for i, h in enumerate(header_row):
    print(f"  Col {i+1}: {h}")

print("\n=== Sample data row 13 (first data row) ===")
data_row = rows[12]
for i, v in enumerate(data_row):
    if v is not None:
        print(f"  Col {i+1} [{header_row[i]}]: {v}")

print("\n=== Total data rows ===")
target_cols = [r[1] for r in rows[12:] if r[1] is not None]
print(f"Target column count: {len(target_cols)}")
print("First 10:", target_cols[:10])
print("Last 10:", target_cols[-10:])
wb.close()
