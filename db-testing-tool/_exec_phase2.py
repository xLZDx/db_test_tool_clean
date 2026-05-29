"""Phase 2 execution: POST DRD to /api/tests/control-table/analyze then pdm-enrich."""
import json, sys, os, requests

BASE = "http://127.0.0.1:8550"
DRD  = r"C:\GIT_Repo\db-testing-tool\DRD_Activity_Fact.xlsx"
XML  = r"C:\GIT_Repo\db-testing-tool\1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
OUT  = r"C:\GIT_Repo\db-testing-tool\data\phase2_analyze_result.json"

# --- Step 2.1: control-table/analyze ---
print("=== Phase 2.1: POST to /api/tests/control-table/analyze ===")
with open(DRD, "rb") as f:
    resp = requests.post(
        f"{BASE}/api/tests/control-table/analyze",
        files={"file": ("DRD_Activity_Fact.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "target_schema": "IKOROSTELEV",
            "target_table": "AVY_FACT_SIDE",
            "source_datasource_id": "3",
            "target_datasource_id": "3",
            "sheet_name": "Table-View (2)",
        },
        timeout=120,
    )

print(f"Status: {resp.status_code}")
if resp.status_code != 200:
    print("ERROR:", resp.text[:500])
    sys.exit(1)

data = resp.json()
keys = list(data.keys())
print(f"Response keys: {keys}")

# Find generated SQL
gen_sql = (
    data.get("generated_insert_sql")
    or data.get("generated_sql")
    or data.get("insert_sql")
    or ""
)
rows = data.get("analysis_rows") or data.get("rows") or []
print(f"analysis_rows count: {len(rows)}")
print(f"generated_sql length: {len(gen_sql)} chars")
print(f"generated_sql starts with: {gen_sql[:80]!r}")

# --- Step 2.4: Validate generated_sql ---
if not gen_sql or "INSERT" not in gen_sql.upper():
    print("WARNING: generated_sql is empty or does not contain INSERT — will use pdm-enrich result")
else:
    print("PASS: generated_sql contains INSERT")

# Save full result
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w") as f:
    json.dump({"analyze": data, "generated_sql": gen_sql, "analysis_rows": rows}, f, indent=2, default=str)
print(f"Saved analyze result to {OUT}")

# --- Step 2.3: pdm-enrich ---
print("\n=== Phase 2.3: POST to /api/tests/control-table/pdm-enrich ===")
with open(DRD, "rb") as df, open(XML, "rb") as xf:
    resp2 = requests.post(
        f"{BASE}/api/tests/control-table/pdm-enrich",
        files={
            "drd_file": ("DRD_Activity_Fact.xlsx", df, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "xml_file": ("scenario.xml", xf, "application/xml"),
        },
        data={
            "target_schema": "IKOROSTELEV",
            "target_table": "AVY_FACT_SIDE",
            "source_datasource_id": "3",
            "target_datasource_id": "3",
            "sheet_name": "Table-View (2)",
        },
        timeout=120,
    )

print(f"pdm-enrich status: {resp2.status_code}")
if resp2.status_code == 200:
    d2 = resp2.json()
    print(f"pdm-enrich keys: {list(d2.keys())}")
    enrich_sql = d2.get("generated_insert_sql") or d2.get("generated_sql") or d2.get("insert_sql") or ""
    print(f"enriched_sql length: {len(enrich_sql)}")
    if enrich_sql and "INSERT" in enrich_sql.upper():
        print("PASS: pdm-enrich generated INSERT SQL — using this for Phase 4")
        gen_sql = enrich_sql
    with open(OUT.replace(".json", "_pdmenrich.json"), "w") as f:
        json.dump(d2, f, indent=2, default=str)
else:
    print(f"pdm-enrich response: {resp2.text[:300]}")

# --- Final output ---
print(f"\n=== Phase 2 Summary ===")
print(f"Final generated_sql length: {len(gen_sql)}")
print(f"Contains INSERT: {'INSERT' in gen_sql.upper()}")
print(f"First 200 chars: {gen_sql[:200]}")
