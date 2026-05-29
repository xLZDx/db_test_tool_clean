"""Fix datatype issue (TD_DIM_ID is NUMBER, not DATE) and retry INSERT."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

# Get exact data types for AVY_FACT_SIDE NOT NULL cols
r = requests.post(f"{BASE}/api/datasources/{DS}/query",
    json={"sql": """SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS
WHERE OWNER = 'IKOROSTELEV' AND TABLE_NAME = 'AVY_FACT_SIDE'
AND COLUMN_NAME IN ('EXG_DIM_ID','AR_DIM_ID','OFST_AR_DIM_ID','ACG_TP_DIM_ID',
  'CASH_POS_TP_DIM_ID','SBC_CCY_DIM_ID','SEC_PD_DIM_ID','CASH_PD_DIM_ID',
  'LGCY_CNCL_CMPLN_RSN_DIM_ID','LGCY_CNCL_CMPLN_SRC_DIM_ID','LGCY_MKT_TP_DIM_ID',
  'LGCY_TRD_CPCTY_TP_DIM_ID','SRC_PCS_TP_DIM_ID','SRC_ENTR_CNL_TP_DIM_ID',
  'TRD_SLCT_TP_DIM_ID','TXN_SRC_STM_DIM_ID','REL_TXN_SRC_STM_DIM_ID',
  'BKR_AR_DIM_ID','TD_DIM_ID','TD','SD_DIM_ID','TXN_ID','CRT_DTM','CRT_USR_NM',
  'ACTV_F','LAST_UDT_USR_NM','LAST_UDT_DTM')
ORDER BY COLUMN_ID""", "row_limit": 50}, timeout=30)
rows = r.json().get("rows", [])
col_types = {row["COLUMN_NAME"]: row["DATA_TYPE"] for row in rows}
print("NOT NULL column types:")
for k, v in col_types.items():
    print(f"  {k}: {v}")

# Build INSERT with correct types
# All *_DIM_ID columns are NUMBER, use -1
# TD is DATE → TXN.TD
# CRT_DTM, LAST_UDT_DTM are DATE/TIMESTAMP → TXN.CRT_DTM, TXN.LAST_UDT_DTM
# CRT_USR_NM, LAST_UDT_USR_NM, ACTV_F are VARCHAR2

insert_sql = """INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (
    EXG_DIM_ID,
    AR_DIM_ID,
    OFST_AR_DIM_ID,
    ACG_TP_DIM_ID,
    CASH_POS_TP_DIM_ID,
    SBC_CCY_DIM_ID,
    SEC_PD_DIM_ID,
    CASH_PD_DIM_ID,
    LGCY_CNCL_CMPLN_RSN_DIM_ID,
    LGCY_CNCL_CMPLN_SRC_DIM_ID,
    LGCY_MKT_TP_DIM_ID,
    LGCY_TRD_CPCTY_TP_DIM_ID,
    SRC_PCS_TP_DIM_ID,
    SRC_ENTR_CNL_TP_DIM_ID,
    TRD_SLCT_TP_DIM_ID,
    TXN_SRC_STM_DIM_ID,
    REL_TXN_SRC_STM_DIM_ID,
    BKR_AR_DIM_ID,
    TD_DIM_ID,
    TD,
    SD_DIM_ID,
    TXN_ID,
    CRT_DTM,
    CRT_USR_NM,
    ACTV_F,
    LAST_UDT_USR_NM,
    LAST_UDT_DTM
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

print(f"\nINSERT SQL ready ({len(insert_sql)} chars)")

print("\n=== Execute via check-insert ===")
r2 = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={"target_datasource_id": DS, "sql": insert_sql, "execute": True},
    timeout=120,
)
print(f"status: {r2.status_code}")
d2 = r2.json()
print(f"ok: {d2.get('ok')}")
err = str(d2.get("error", ""))
print(f"error: {err[:300]}")
if d2.get('diagnostics'):
    mc = d2['diagnostics'].get('missing_columns', [])
    if mc:
        print(f"missing_columns: {[c['column'] for c in mc]}")

print("\n=== Verify row count ===")
r3 = requests.post(f"{BASE}/api/datasources/{DS}/query",
    json={"sql": "SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
    timeout=30)
cnt = (r3.json().get("rows") or [{"CNT": 0}])[0].get("CNT", 0)
print(f"COUNT(*) = {cnt}")

if cnt > 0:
    print(f"PASS R4: {cnt} rows loaded")
    # Show sample
    r4 = requests.post(f"{BASE}/api/datasources/{DS}/query",
        json={"sql": "SELECT TXN_ID, TD, CRT_USR_NM, AR_DIM_ID FROM IKOROSTELEV.AVY_FACT_SIDE WHERE ROWNUM <= 3", "row_limit": 3},
        timeout=30)
    print(f"Sample rows: {r4.json().get('rows', [])}")
else:
    print("FAIL R4: still 0 rows")
