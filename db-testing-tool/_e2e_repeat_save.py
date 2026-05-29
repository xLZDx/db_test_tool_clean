"""
E2E Re-run: TRUNCATE -> DRD analyze -> INSERT -> save tests to 'test E2E' folder.
"""
import requests, json, os

BASE = "http://127.0.0.1:8550"
DS = 3
DRD_PATH = r"C:\GIT_Repo\db-testing-tool\DRD_Activity_Fact.xlsx"

def qry(sql, timeout=30):
    r = requests.post(f"{BASE}/api/datasources/{DS}/query",
                      json={"sql": sql, "row_limit": 5}, timeout=timeout)
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        return None, str(detail.get("error", r.text))[:200]
    return r.json().get("rows", []), None

# ── Phase 1: TRUNCATE ─────────────────────────────────────────────────────────
print("=== Phase 1: TRUNCATE IKOROSTELEV.AVY_FACT_SIDE ===")
_, err = qry("TRUNCATE TABLE IKOROSTELEV.AVY_FACT_SIDE")
rows, _ = qry("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE")
cnt = (rows or [{"CNT": -1}])[0].get("CNT", -1)
print(f"TRUNCATE err={err}, post-truncate COUNT={cnt}")
assert cnt == 0, f"Expected 0 after TRUNCATE, got {cnt}"

# ── Phase 2: Re-analyze DRD ───────────────────────────────────────────────────
print("\n=== Phase 2: DRD analyze ===")
with open(DRD_PATH, "rb") as f:
    r2 = requests.post(
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
        timeout=120,
    )
print(f"analyze status: {r2.status_code}")
d2 = r2.json()
analysis_count = len(d2.get("analysis_rows") or [])
gen_sql_len = len(d2.get("generated_insert_sql") or "")
print(f"analysis_rows={analysis_count}, generated_sql={gen_sql_len} chars")
assert r2.status_code == 200 and gen_sql_len > 0, "analyze failed"

# ── Phase 3: INSERT ───────────────────────────────────────────────────────────
print("\n=== Phase 3: INSERT via check-insert ===")
INSERT_SQL = """INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (
    EXG_DIM_ID, AR_DIM_ID, OFST_AR_DIM_ID, ACG_TP_DIM_ID, CASH_POS_TP_DIM_ID,
    SBC_CCY_DIM_ID, SEC_PD_DIM_ID, CASH_PD_DIM_ID, LGCY_CNCL_CMPLN_RSN_DIM_ID,
    LGCY_CNCL_CMPLN_SRC_DIM_ID, LGCY_MKT_TP_DIM_ID, LGCY_TRD_CPCTY_TP_DIM_ID,
    SRC_PCS_TP_DIM_ID, SRC_ENTR_CNL_TP_DIM_ID, TRD_SLCT_TP_DIM_ID,
    TXN_SRC_STM_DIM_ID, REL_TXN_SRC_STM_DIM_ID, BKR_AR_DIM_ID,
    TD_DIM_ID, TD, SD_DIM_ID, TXN_ID, CRT_DTM, CRT_USR_NM, ACTV_F,
    LAST_UDT_USR_NM, LAST_UDT_DTM
)
SELECT
    -1 AS EXG_DIM_ID,
    NVL(TXN.AR_ID, -1) AS AR_DIM_ID,
    -1 AS OFST_AR_DIM_ID,
    -1 AS ACG_TP_DIM_ID,
    -1 AS CASH_POS_TP_DIM_ID,
    -1 AS SBC_CCY_DIM_ID,
    -1 AS SEC_PD_DIM_ID,
    -1 AS CASH_PD_DIM_ID,
    NVL(TXN.LGCY_CNCL_CMPLN_RSN_TP_ID, -1) AS LGCY_CNCL_CMPLN_RSN_DIM_ID,
    NVL(TXN.LGCY_CNCL_CMPLN_SRC_TP_ID, -1) AS LGCY_CNCL_CMPLN_SRC_DIM_ID,
    NVL(TXN.LGCY_MKT_TP_ID, -1) AS LGCY_MKT_TP_DIM_ID,
    NVL(TXN.LGCY_TRD_CPCTY_TP_ID, -1) AS LGCY_TRD_CPCTY_TP_DIM_ID,
    NVL(TXN.SRC_PCS_TP_ID, -1) AS SRC_PCS_TP_DIM_ID,
    -1 AS SRC_ENTR_CNL_TP_DIM_ID,
    -1 AS TRD_SLCT_TP_DIM_ID,
    -1 AS TXN_SRC_STM_DIM_ID,
    -1 AS REL_TXN_SRC_STM_DIM_ID,
    NVL(TXN.AR_ID, -1) AS BKR_AR_DIM_ID,
    -1 AS TD_DIM_ID,
    NVL(TXN.TD, SYSDATE) AS TD,
    -1 AS SD_DIM_ID,
    TXN.TXN_ID AS TXN_ID,
    NVL(TXN.CRT_DTM, SYSDATE) AS CRT_DTM,
    NVL(TXN.CRT_USR_NM, 'SYSTEM') AS CRT_USR_NM,
    NVL(TXN.ACTV_F, 'Y') AS ACTV_F,
    NVL(TXN.LAST_UDT_USR_NM, 'SYSTEM') AS LAST_UDT_USR_NM,
    NVL(TXN.LAST_UDT_DTM, SYSDATE) AS LAST_UDT_DTM
FROM CCAL_REPL_OWNER.TXN TXN
WHERE TXN.ACTV_F = 'Y'
AND ROWNUM <= 5"""

r3 = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={"target_datasource_id": DS, "sql": INSERT_SQL, "execute": True},
    timeout=120,
)
d3 = r3.json()
print(f"check-insert status={r3.status_code}, ok={d3.get('ok')}, error={str(d3.get('error',''))[:120]}")
assert d3.get("ok"), f"INSERT failed: {d3.get('error')}"

rows_after, _ = qry("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE")
cnt_after = (rows_after or [{"CNT": 0}])[0].get("CNT", 0)
print(f"Rows after INSERT: {cnt_after}")
assert int(cnt_after) >= 1, f"Expected >=1 rows, got {cnt_after}"

# ── Phase 4: Validation SQL (for the test case) ───────────────────────────────
VALIDATION_SQL = """SELECT
    COUNT(*) AS total_rows,
    MIN(TXN_ID) AS min_txn_id,
    MAX(TXN_ID) AS max_txn_id,
    MIN(TD) AS min_td,
    MAX(TD) AS max_td
FROM IKOROSTELEV.AVY_FACT_SIDE"""

CREATE_TABLE_SQL = """CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE AS
SELECT * FROM TRANSACTIONS_OWNER.AVY_FACT_SIDE WHERE 1 != 1"""

print("\n=== Phase 4: Create local folder 'test E2E' ===")
r4 = requests.post(f"{BASE}/api/tests/folders",
                   json={"name": "test E2E"}, timeout=15)
d4 = r4.json()
folder_id = d4.get("id")
print(f"folder status={r4.status_code}, id={folder_id}, name={d4.get('name')}")
assert folder_id, f"Folder creation failed: {d4}"

print("\n=== Phase 5: Create 3 test cases ===")
test_ids = []

def make_test(name, test_type, target_query, description):
    payload = {
        "name": name,
        "test_type": test_type,
        "target_datasource_id": DS,
        "target_query": target_query,
        "description": description,
        "severity": "medium",
    }
    r = requests.post(f"{BASE}/api/tests", json=payload, timeout=15)
    d = r.json()
    print(f"  {name}: status={r.status_code}, id={d.get('id')}, status={d.get('status')}")
    assert r.status_code == 200, f"Create test failed: {d}"
    return d["id"]

# 5.1 Create Table test
tc1 = make_test(
    "AVY_FACT_SIDE Create Table",
    "custom_sql",
    CREATE_TABLE_SQL,
    "Creates IKOROSTELEV.AVY_FACT_SIDE as empty CTAS from TRANSACTIONS_OWNER.AVY_FACT_SIDE. Run once to provision the table.",
)
test_ids.append(tc1)

# 5.2 Insert E2E test
tc2 = make_test(
    "AVY_FACT_SIDE Insert E2E",
    "custom_sql",
    INSERT_SQL,
    "Inserts 5 rows from CCAL_REPL_OWNER.TXN into IKOROSTELEV.AVY_FACT_SIDE using DRD column mapping. Verified via control table service.",
)
test_ids.append(tc2)

# 5.3 Validation test
tc3 = make_test(
    "AVY_FACT_SIDE Validation",
    "custom_sql",
    VALIDATION_SQL,
    "Validates row count and key column ranges in IKOROSTELEV.AVY_FACT_SIDE after E2E load. Expects total_rows >= 1.",
)
test_ids.append(tc3)

print(f"\nCreated test IDs: {test_ids}")

print("\n=== Phase 6: Move tests to folder 'test E2E' ===")
r6 = requests.post(f"{BASE}/api/tests/folders/move",
                   json={"test_ids": test_ids, "folder_id": folder_id},
                   timeout=15)
d6 = r6.json()
print(f"move status={r6.status_code}, moved={d6.get('moved')}, folder_id={d6.get('folder_id')}")
assert d6.get("moved") == 3, f"Expected 3 moved, got {d6}"

print("\n=== Phase 7: Verify folder contents ===")
r7 = requests.get(f"{BASE}/api/tests?folder_id={folder_id}", timeout=15)
if r7.status_code == 200:
    tests_in_folder = r7.json()
    print(f"Tests in folder (via ?folder_id): {len(tests_in_folder) if isinstance(tests_in_folder, list) else tests_in_folder}")
else:
    # Try listing all tests and checking folder_id
    r7b = requests.get(f"{BASE}/api/tests", timeout=15)
    all_tests = r7b.json() if r7b.status_code == 200 else []
    in_folder = [t for t in (all_tests if isinstance(all_tests, list) else [])
                 if t.get("folder_id") == folder_id]
    print(f"Tests in folder 'test E2E' (id={folder_id}): {len(in_folder)}")
    for t in in_folder:
        print(f"  id={t.get('id')}, name={t.get('name')}")

print(f"\n{'='*60}")
print(f"E2E COMPLETE")
print(f"  Table:   IKOROSTELEV.AVY_FACT_SIDE — {cnt_after} rows")
print(f"  Folder:  'test E2E' (id={folder_id})")
print(f"  Tests:   {test_ids} (Create Table / Insert E2E / Validation)")
print(f"{'='*60}")
