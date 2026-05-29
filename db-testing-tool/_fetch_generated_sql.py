"""Fetch the analyze-generated INSERT SQL and save it for inspection."""
import requests, json, os

BASE = "http://127.0.0.1:8550"
DS = 3
DRD = "DRD_Activity_Fact.xlsx"

with open(DRD, "rb") as f:
    r = requests.post(
        f"{BASE}/api/tests/control-table/analyze",
        files={"file": ("DRD_Activity_Fact.xlsx", f,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "sheet_name": "Table-View (2)",
            "source_datasource_id": str(DS),
            "target_datasource_id": str(DS),
            "target_schema": "IKOROSTELEV",
            "target_table": "AVY_FACT_SIDE",
            "control_schema": "IKOROSTELEV",
        },
        timeout=180,
    )

d = r.json()
sql = d.get("generated_insert_sql", "")
rows_count = len(d.get("analysis_rows") or [])
print(f"Status: {r.status_code}, analysis_rows: {rows_count}, sql_len: {len(sql)}")

# Save full SQL
with open("data/generated_insert_full.sql", "w", encoding="utf-8") as out:
    out.write(sql)
print("Saved to data/generated_insert_full.sql")

# Save full response (minus sql) for inspection
d_inspect = {k: v for k, v in d.items() if k != "generated_insert_sql"}
d_inspect["analysis_rows_count"] = rows_count
d_inspect["sql_line_count"] = sql.count("\n")
with open("data/generated_insert_meta.json", "w", encoding="utf-8") as out:
    json.dump(d_inspect, out, indent=2, default=str)

# Show first 40 lines of SQL
lines = sql.split("\n")
print(f"\n--- First 40 lines of generated SQL ({len(lines)} total) ---")
for i, line in enumerate(lines[:40], 1):
    print(f"{i:4}: {line}")

print(f"\n--- Lines 800-850 ---")
for i, line in enumerate(lines[799:850], 800):
    print(f"{i:4}: {line}")
