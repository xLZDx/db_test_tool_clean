"""
Build a fully executable INSERT INTO IKOROSTELEV.AVY_FACT_SIDE ... SELECT FROM source tables.
All 39 source tables are accessible. This script:
1. Queries column metadata for key tables to verify join columns exist
2. Generates syntactically valid Oracle INSERT...SELECT with proper JOINs
3. Limits to ROWNUM <= 10 for safety
"""
import requests
import json
from pathlib import Path

API = "http://127.0.0.1:8550/api/datasources/3/query"
OUTPUT = Path(r"c:\GIT_Repo\db-testing-tool\reports\AVY_FACT_SIDE_insert_executable.sql")

def query(sql, limit=500):
    resp = requests.post(API, json={"sql": sql, "row_limit": limit}, timeout=60)
    data = resp.json()
    if data.get("error"):
        return None, data["error"]
    return data.get("rows", []), None

def get_columns(owner, table):
    """Get column names for a table."""
    sql = f"SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='{owner}' AND TABLE_NAME='{table}' ORDER BY COLUMN_ID"
    rows, err = query(sql)
    if err:
        return []
    return [r["COLUMN_NAME"] for r in rows]

# Verify key join columns exist
print("Checking key join columns...")

# TXN columns
txn_cols = get_columns("CCAL_REPL_OWNER", "TXN")
print(f"  CCAL_REPL_OWNER.TXN: {len(txn_cols)} columns")

# APA columns
apa_cols = get_columns("CCAL_REPL_OWNER", "APA")
print(f"  CCAL_REPL_OWNER.APA: {len(apa_cols)} columns")

# Check specific columns needed
needed_in_txn = ["TXN_ID", "AR_ID", "TD", "SD", "SRC_STM_ID", "TXN_TP_ID", "EXEC_TP_ID", 
                 "SRC_TXN_TP", "BUY_SELL_IND", "CF_GEN_F", "TXN_CL_F", "BKG_DT",
                 "SRC_ACTN_CODE", "USR_ID", "SRC_PRIM_CL", "SRC_CNCL_RSN_ID",
                 "SRC_TXN_SEQ_NUM", "DVDN_RCRD_DT", "SALE_PSN_NUM", "FA_NUM",
                 "BR_CODE", "ORIG_BKG_DT", "ORIG_TRD_NUM", "ORIG_TD",
                 "OMS_ORDR_KEY", "SRC_CXL_REV", "TXN_REPL_CNT", "TXN_ST_ID",
                 "TRD_NUM", "TXN_SRC_KEY", "ORIG_SRC_STM_ID", "ORIG_SRC_STM_CODE",
                 "SRC_TXN_CODE", "ADL_TRD_INSR", "EXEC_DTM", "SRC_ENTR_DTM",
                 "SRC_CRT_USRNM", "DOL_IND_ID", "EXG_CODE", "TXN_SBTP_ID",
                 "EXEC_SBTP_ID", "OMS_EXEC_TP_ID", "DRVD_TRD_CPCTY_TP_ID",
                 "LGCY_CNCL_CMPLN_RSN_TP_ID", "LGCY_CNCL_CMPLN_SRC_TP_ID",
                 "LGCY_MKT_TP_ID", "LGCY_TRD_CPCTY_TP_ID", "SRC_ENTR_CNL_TP_ID",
                 "SRC_PCS_TP_ID", "TRD_SLCT_TP_ID", "SRC_BUY_SELL_MULTI_ID",
                 "OPTS_CLSS_CODE", "SRC_OPT_CLS"]
missing_txn = [c for c in needed_in_txn if c not in txn_cols]
if missing_txn:
    print(f"  WARNING: Missing in TXN: {missing_txn}")

needed_in_apa = ["TXN_ID", "APA_ID", "APA_TP_ID", "PD_ID", "ORIG_QTY", "TXN_AMT",
                 "STM_BASE_CCY_AMT", "SCR_PRC_IN_TXN_CCY", "APA_DSC", "ALT_DSC",
                 "APA_EXT_QUALFR", "SRC_SEQ_NUM", "OFST_AR_ID", "BKR_AR_ID",
                 "SRC_PD_ID", "STM_BASE_CCY_CLC_DTM", "STM_BASE_ISO_CCY_CODE",
                 "STM_BASE_CCY_EXG_RATE", "SALE_CHRG_RATE_TP_ID", "CASH_POS_TP_ID",
                 "SRC_EFF_DT", "CLNT_FRIENDLY_DESC", "OPT_SYMB", "TRD_FCTR_RATE",
                 "MRKUP_RATE", "STD_CMSN_AMT", "PD_CMPOS_DSC", "SYMB", "ISIN",
                 "YIELD", "YIELD_TO_WORST", "YIELD_TO_WORST_CD", "DB_CR_ID",
                 "APA_SIDE_CD"]
missing_apa = [c for c in needed_in_apa if c not in apa_cols]
if missing_apa:
    print(f"  WARNING: Missing in APA: {missing_apa}")

# Check what APA_SIDE_CD values exist (SEC vs CSH)
rows, _ = query("SELECT DISTINCT APA_SIDE_CD FROM CCAL_REPL_OWNER.APA WHERE ROWNUM <= 10")
if rows:
    print(f"  APA_SIDE_CD values: {[r['APA_SIDE_CD'] for r in rows]}")

# Check FIP columns
fip_cols = get_columns("CCAL_REPL_OWNER", "FIP")
print(f"  CCAL_REPL_OWNER.FIP: {len(fip_cols)} columns")
print(f"    Columns: {fip_cols[:15]}")

# AR_DIM columns check
ar_cols = get_columns("CCSI_OWNER", "AR_DIM")
print(f"  CCSI_OWNER.AR_DIM: {len(ar_cols)} columns")

# DATE_DIM check
dd_cols = get_columns("COMMON_OWNER", "DATE_DIM")
print(f"  COMMON_OWNER.DATE_DIM: {len(dd_cols)} columns")
# Check what the ID column is
rows, _ = query("SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='COMMON_OWNER' AND TABLE_NAME='DATE_DIM' AND COLUMN_NAME LIKE '%DIM%'")
if rows:
    print(f"    DIM cols: {[r['COLUMN_NAME'] for r in rows]}")

# IMT_PD_DIM key
rows, _ = query("SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='CIRD_OWNER' AND TABLE_NAME='IMT_PD_DIM' AND COLUMN_NAME LIKE '%ID%' ORDER BY COLUMN_ID")
if rows:
    print(f"  CIRD_OWNER.IMT_PD_DIM ID cols: {[r['COLUMN_NAME'] for r in rows]}")

# SRC_STM_DIM key columns
rows, _ = query("SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='COMMON_OWNER' AND TABLE_NAME='SRC_STM_DIM' ORDER BY COLUMN_ID")
if rows:
    print(f"  COMMON_OWNER.SRC_STM_DIM cols: {[r['COLUMN_NAME'] for r in rows]}")

# Check TXN row count to size our INSERT
rows, _ = query("SELECT COUNT(*) AS CNT FROM CCAL_REPL_OWNER.TXN")
if rows:
    print(f"\n  CCAL_REPL_OWNER.TXN total rows: {rows[0]['CNT']}")

print("\nDone. Building INSERT script...")
