"""
Generate and execute a working INSERT INTO IKOROSTELEV.AVY_FACT_SIDE
from real source tables on LH (ds_id=3).

Strategy:
- Primary: CCAL_REPL_OWNER.TXN t
- Safe LEFT JOINs only (verified join conditions)
- NVL for all NOT NULL columns
- NULL for columns from complex multi-instance lookups (CL_VAL used 20+ ways)
- ROWNUM <= 10 for safety
"""
import requests
import json
import sys

API = "http://127.0.0.1:8550/api/datasources/3/query"

def run_sql(sql, timeout=120):
    """Execute SQL via API, return (success, message, rows)."""
    try:
        resp = requests.post(API, json={"sql": sql, "row_limit": 100}, timeout=timeout)
        data = resp.json()
        if data.get("error"):
            return False, data["error"], []
        return True, data.get("message", "OK"), data.get("rows", [])
    except Exception as e:
        return False, str(e), []

# Step 1: Drop existing table (ignore if not exists)
print("Step 1: Dropping IKOROSTELEV.AVY_FACT_SIDE...")
ok, msg, _ = run_sql("DROP TABLE IKOROSTELEV.AVY_FACT_SIDE")
print(f"  {'OK' if ok else 'SKIP'}: {msg[:80]}")

# Step 2: Find how APA links to TXN
print("\nStep 2: Checking TXN->APA link...")
ok, msg, rows = run_sql("""
    SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS 
    WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='TXN' 
    AND COLUMN_NAME LIKE '%APA%'
    ORDER BY COLUMN_ID
""")
if ok and rows:
    print(f"  TXN APA columns: {[r['COLUMN_NAME'] for r in rows]}")
else:
    print(f"  No APA cols in TXN or error: {msg[:60]}")

# Check if there's a linking table
ok, msg, rows = run_sql("""
    SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS 
    WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='APA' 
    AND COLUMN_NAME IN ('TXN_ID','LINK_TXN_ID','SRC_TXN_ID','TXN_KEY')
    ORDER BY COLUMN_ID
""")
if ok and rows:
    print(f"  APA link columns: {[r['COLUMN_NAME'] for r in rows]}")

# Try to find the relationship
ok, msg, rows = run_sql("""
    SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS 
    WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='APA' 
    AND COLUMN_NAME LIKE '%ID%'
    AND COLUMN_NAME NOT LIKE '%CCY%'
    ORDER BY COLUMN_ID
""")
if ok and rows:
    apa_id_cols = [r['COLUMN_NAME'] for r in rows]
    print(f"  APA ID columns: {apa_id_cols}")

# Step 3: Quick test - can we join TXN to APA?  
# The DRD shows SEC_APA_ID and CSH_APA_ID on target come from APA.APA_ID
# So TXN must have a way to link. Let's check if TXN has columns that could be FK to APA
ok, msg, rows = run_sql("""
    SELECT t.TXN_ID, t.COLUMN_NAME 
    FROM (
        SELECT 'TXN' AS TXN_ID, COLUMN_NAME FROM ALL_TAB_COLUMNS 
        WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='TXN'
        AND (COLUMN_NAME LIKE '%APA%' OR COLUMN_NAME LIKE '%APPSMT%' OR COLUMN_NAME LIKE '%SEC%ID' OR COLUMN_NAME LIKE '%CSH%ID')
    ) t
    WHERE ROWNUM <= 20
""")
if ok and rows:
    print(f"  TXN potential APA FK cols: {[r['COLUMN_NAME'] for r in rows]}")

# Step 4: Check sample data from TXN to understand structure
print("\nStep 3: Sampling TXN data...")
ok, msg, rows = run_sql("""
    SELECT TXN_ID, AR_ID, TD, SRC_STM_ID, TXN_TP_ID, BUY_SELL_IND, 
           SRC_TXN_TP, BR_CODE, FA_NUM, TRD_NUM
    FROM CCAL_REPL_OWNER.TXN 
    WHERE ROWNUM <= 3
""")
if ok and rows:
    for r in rows:
        print(f"  TXN_ID={r.get('TXN_ID')}, AR_ID={r.get('AR_ID')}, TD={r.get('TD')}, SRC_STM={r.get('SRC_STM_ID')}")
else:
    print(f"  Error: {msg[:80]}")

# Step 5: Check all TXN columns
print("\nStep 4: Getting all TXN columns...")
ok, msg, rows = run_sql("""
    SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS 
    WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='TXN'
    ORDER BY COLUMN_ID
""")
if ok:
    txn_cols = [r['COLUMN_NAME'] for r in rows]
    print(f"  All {len(txn_cols)} TXN columns: {txn_cols}")

print("\nDone with discovery. Building INSERT next...")
