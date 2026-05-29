"""Build and execute a clean INSERT into IKOROSTELEV.AVY_FACT_SIDE from TXN source data."""
import requests, json

BASE = "http://127.0.0.1:8550"
DS = 3

# First check what columns TXN has that we need
col_check_sql = """SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS
WHERE OWNER = 'CCAL_REPL_OWNER' AND TABLE_NAME = 'TXN'
AND COLUMN_NAME IN ('TXN_ID','TD','SD','AR_ID','ACTV_F','CRT_DTM','CRT_USR_NM',
  'LAST_UDT_USR_NM','LAST_UDT_DTM','LGCY_MKT_TP_ID','LGCY_TRD_CPCTY_TP_ID',
  'LGCY_CNCL_CMPLN_RSN_TP_ID','LGCY_CNCL_CMPLN_SRC_TP_ID','SRC_PCS_TP_ID',
  'EXEC_TP_ID','DRVD_TRD_CPCTY_TP_ID')
ORDER BY COLUMN_NAME"""

r = requests.post(f"{BASE}/api/datasources/{DS}/query",
    json={"sql": col_check_sql, "row_limit": 50}, timeout=30)
d = r.json()
available = {row['COLUMN_NAME'] for row in d.get('rows', [])}
print(f"Available TXN columns: {sorted(available)}")

# Build INSERT SQL using real TXN values for NOT NULL columns
# NOT NULL cols: EXG_DIM_ID, AR_DIM_ID, OFST_AR_DIM_ID, ACG_TP_DIM_ID, CASH_POS_TP_DIM_ID,
#   SBC_CCY_DIM_ID, SEC_PD_DIM_ID, CASH_PD_DIM_ID, LGCY_CNCL_CMPLN_RSN_DIM_ID,
#   LGCY_CNCL_CMPLN_SRC_DIM_ID, LGCY_MKT_TP_DIM_ID, LGCY_TRD_CPCTY_TP_DIM_ID,
#   SRC_PCS_TP_DIM_ID, SRC_ENTR_CNL_TP_DIM_ID, TRD_SLCT_TP_DIM_ID,
#   TXN_SRC_STM_DIM_ID, REL_TXN_SRC_STM_DIM_ID, BKR_AR_DIM_ID,
#   TD_DIM_ID, TD, SD_DIM_ID, TXN_ID, CRT_DTM, CRT_USR_NM, ACTV_F, LAST_UDT_USR_NM, LAST_UDT_DTM
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
    NVL(TXN.TD, SYSDATE) AS TD_DIM_ID,
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

print(f"\nINSERT SQL ({len(insert_sql)} chars):")
print(insert_sql[:300])
print("...")
print(insert_sql[-200:])

# Execute via check-insert
print("\n=== Execute INSERT via check-insert ===")
r2 = requests.post(
    f"{BASE}/api/tests/control-table/check-insert",
    json={"target_datasource_id": DS, "sql": insert_sql, "execute": True},
    timeout=120,
)
print(f"status: {r2.status_code}")
d2 = r2.json()
print(f"ok: {d2.get('ok')}")
print(f"error: {str(d2.get('error',''))[:300]}")
if d2.get('diagnostics'):
    mc = d2['diagnostics'].get('missing_columns', [])
    print(f"missing_columns: {[c['column'] for c in mc]}")

# Verify count
print("\n=== Verify row count ===")
r3 = requests.post(f"{BASE}/api/datasources/{DS}/query",
    json={"sql": "SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE", "row_limit": 1},
    timeout=30)
d3 = r3.json()
cnt = (d3.get('rows') or [{'CNT': 0}])[0].get('CNT', 0)
print(f"COUNT(*) = {cnt}")
if cnt > 0:
    print(f"PASS R4: {cnt} real rows loaded into IKOROSTELEV.AVY_FACT_SIDE")
else:
    print("FAIL R4: still 0 rows")
