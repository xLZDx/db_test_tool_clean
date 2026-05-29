"""
Build and execute INSERT INTO IKOROSTELEV.AVY_FACT_SIDE from source tables.
All 39 source tables are verified accessible.

Join map (verified):
- TXN.TXN_ID is the primary key
- APA.EXEC_ID = TXN.TXN_ID (APA = allocation/position per side)
- FIP.APA_ID = APA.APA_ID (fees/charges)
- AR_DIM.AR_ID = TXN.AR_ID
- SRC_STM_DIM.SRC_STM_ID = TXN.SRC_STM_ID
- DATE_DIM.DT_DIM_ID join on TD
- SHDW_TXN_TP.SRC_TXN_TP = TXN.SRC_TXN_TP
- IMT_PD_DIM.CCAL_PD_ID = APA.PD_ID
"""
import requests
import sys

API = "http://127.0.0.1:8550/api/datasources/3/query"

def run_sql(sql, timeout=300):
    try:
        resp = requests.post(API, json={"sql": sql, "row_limit": 100}, timeout=timeout)
        data = resp.json()
        if resp.status_code != 200:
            detail = data.get("detail", data)
            if isinstance(detail, dict):
                return False, detail.get("error", str(detail))
            return False, str(detail)
        if data.get("error"):
            return False, data["error"]
        msg = data.get("message", "OK")
        affected = data.get("total_rows_affected", 0)
        if affected:
            msg += f" [rows_affected={affected}]"
        return True, msg
    except Exception as e:
        return False, str(e)

# Step 1: Create table
print("=" * 60)
print("STEP 1: CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE")
print("=" * 60)

# Read the DDL from existing file
with open(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_create_ikorostelev.sql") as f:
    ddl_content = f.read()

# Split into DROP and CREATE
statements = [s.strip() for s in ddl_content.split(";") if s.strip()]
for stmt in statements:
    if not stmt:
        continue
    ok, msg = run_sql(stmt)
    action = "DROP" if "DROP" in stmt[:20].upper() else "CREATE"
    print(f"  {action}: {'OK' if ok else 'WARN'} - {msg[:60]}")

# Step 2: Build and execute INSERT
print("\n" + "=" * 60)
print("STEP 2: INSERT INTO IKOROSTELEV.AVY_FACT_SIDE")
print("=" * 60)

# The INSERT uses real source columns from TXN + safe LEFT JOINs
# Strategy: Use scalar subqueries for dimension lookups to avoid fan-outs
# NOT NULL columns get NVL(..., 0) or literal defaults

insert_sql = """
INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (
    EXG_DIM_ID, AR_DIM_ID, OFST_AR_DIM_ID, ACG_TP_DIM_ID,
    CASH_POS_TP_DIM_ID, SBC_CCY_DIM_ID, SEC_PD_DIM_ID, CASH_PD_DIM_ID,
    LGCY_CNCL_CMPLN_RSN_DIM_ID, LGCY_CNCL_CMPLN_SRC_DIM_ID,
    LGCY_MKT_TP_DIM_ID, LGCY_TRD_CPCTY_TP_DIM_ID,
    SRC_PCS_TP_DIM_ID, SRC_ENTR_CNL_TP_DIM_ID, TRD_SLCT_TP_DIM_ID,
    TXN_SRC_STM_DIM_ID, REL_TXN_SRC_STM_DIM_ID, BKR_AR_DIM_ID,
    TD_DIM_ID, TD, SD_DIM_ID, SD, BKG_DT, ORIG_BKG_DT, EXEC_DTM,
    TXN_ID, REL_TXN_ID, TRD_NUM, ORIG_TRD_NUM, TXN_SRC_KEY,
    SEC_APA_ID, CSH_APA_ID,
    SEC_PRC_IN_TXN_CCY, SEC_TXN_AMT, CASH_TXN_AMT,
    SEC_ORIG_QTY, SEC_SRC_PRC_IN_SBC,
    SALE_CHRG_RATE_PCT, SALE_CHRG_RATE_TP_ID,
    SEC_SBC_AMT, CASH_SBC_AMT,
    SBC_CCY_CALC_DT, SBC_EXG_RATE,
    BUY_SELL_IND, CF_GEN_F, TXN_CL_F, OPT_CLSS_CD,
    SEC_SRC_CUSIP,
    TXN_TP_ID, TXN_SBTP_ID,
    EXEC_TP_ID, EXEC_SBTP_ID,
    SEC_APA_TP_ID, CSH_APA_TP_ID,
    CRT_DTM, CRT_USR_NM,
    AR_ID, SEC_PD_ID, CASH_PD_ID,
    BKR_AR_ID, OFST_AR_ID, BR_CD, FA_NUM,
    SRC_CRT_USRNM, ORIG_TD,
    SRC_PCS_TP_CD, SRC_PCS_TP_NM,
    SRC_STM_CD, SRC_STM_NM,
    SHDW_TXN_TP_CD, SHDW_TXN_TP_NM,
    IMP_SRC_ACTN_CD,
    SIS_DLTD_EV_ID, SIS_USR_ID, SIS_AC_CL_CD,
    SRC_CNCL_RSN_ID, SRC_TXN_SEQ_NUM, DVDN_RCRD_DT,
    OWN_IMP_FA_NUM,
    SRC_STM_ID, SRC_CNCL_X_RVSN_CD, SRC_OPT_CLSS_CD,
    TXN_RCNCL_REPL_CNT, TXN_RCNCL_ST_ID,
    LGCY_CNCL_CMPLN_RSN_TP_ID, LGCY_CNCL_CMPLN_SRC_TP_ID,
    LGCY_MKT_TP_ID, LGCY_TRD_CPCTY_TP_ID,
    REL_TXN_SRC_STM_ID, SRC_ENTR_CNL_TP_ID,
    SRC_PCS_TP_ID, TRD_SLCT_TP_ID,
    LAST_UDT_USR_NM, LAST_UDT_DTM,
    ACTV_F, BATCH_DT,
    SRC_TXN_CODE, OMS_EXEC_TP_ID,
    OMS_ORDR_KEY, ORIG_SRC_STM_ID, ORIG_SRC_STM_CD,
    DRVD_TRD_CPCTY_ID, ADL_TRD_INSR,
    SRC_ENTR_DTM,
    EXG_CD, DOL_IND_ID,
    SESS_NO, TM_PRC_DISCRETION_F,
    SEC_SRC_SEQ_NUM, CASH_SRC_SEQ_NUM,
    SEC_APA_EXT_QUALFR, CASH_APA_EXT_QUALFR,
    SEC_DSC_TRAILER_1, CASH_DSC_TRAILER_1,
    SEC_ALT_DSC_TRAILER_2, CASH_ALT_DSC_TRAILER_2,
    CLNT_FRIENDLY_DSC,
    SEC_CIRD_PD_ID, CASH_CIRD_PD_ID,
    SRC_EFF_DT,
    TRD_FCTR_RATE, MRKUP_RATE, STD_CMSN_AMT,
    SYMB, ISIN, SCR_OPT_SYMB, PD_CMPOS_DSC
)
SELECT
    -- NOT NULL dimension IDs (scalar subqueries avoid fan-outs)
    NVL((SELECT MIN(exd.EXG_DIM_ID) FROM CIRD_OWNER.EXG_DIM exd WHERE exd.EXG_CD = t.EXG_CODE), 0),  -- EXG_DIM_ID
    NVL((SELECT MIN(ard.AR_DIM_ID) FROM CCSI_OWNER.AR_DIM ard WHERE ard.AR_ID = t.AR_ID), 0),         -- AR_DIM_ID
    0,                                           -- OFST_AR_DIM_ID
    0,                                           -- ACG_TP_DIM_ID
    0,                                           -- CASH_POS_TP_DIM_ID
    0,                                           -- SBC_CCY_DIM_ID
    NVL((SELECT MIN(imt.IMT_PD_DIM_ID) FROM CIRD_OWNER.IMT_PD_DIM imt WHERE imt.CCAL_PD_ID = sec_apa.PD_ID), 0),  -- SEC_PD_DIM_ID
    NVL((SELECT MIN(imt.IMT_PD_DIM_ID) FROM CIRD_OWNER.IMT_PD_DIM imt WHERE imt.CCAL_PD_ID = csh_apa.PD_ID), 0),  -- CASH_PD_DIM_ID
    0,                                           -- LGCY_CNCL_CMPLN_RSN_DIM_ID
    0,                                           -- LGCY_CNCL_CMPLN_SRC_DIM_ID
    0,                                           -- LGCY_MKT_TP_DIM_ID
    0,                                           -- LGCY_TRD_CPCTY_TP_DIM_ID
    0,                                           -- SRC_PCS_TP_DIM_ID
    0,                                           -- SRC_ENTR_CNL_TP_ID
    0,                                           -- TRD_SLCT_TP_DIM_ID
    NVL((SELECT MIN(ssd.SRC_STM_DIM_ID) FROM COMMON_OWNER.SRC_STM_DIM ssd WHERE ssd.SRC_STM_ID = t.SRC_STM_ID), 0),  -- TXN_SRC_STM_DIM_ID
    0,                                           -- REL_TXN_SRC_STM_DIM_ID
    0,                                           -- BKR_AR_DIM_ID
    NVL((SELECT MIN(dd.DT_DIM_ID) FROM COMMON_OWNER.DATE_DIM dd WHERE dd.CAL_DT = t.TD), 0),  -- TD_DIM_ID
    t.TD,                                        -- TD
    0,                                           -- SD_DIM_ID
    t.SD,                                        -- SD
    t.BKG_DT,                                    -- BKG_DT
    t.ORIG_BKG_DT,                               -- ORIG_BKG_DT
    t.EXEC_DTM,                                  -- EXEC_DTM
    t.TXN_ID,                                    -- TXN_ID
    (SELECT MIN(tr.TRGT_TXN_ID) FROM CCAL_REPL_OWNER.TXN_RLTNP tr WHERE tr.SRC_TXN_ID = t.TXN_ID),  -- REL_TXN_ID
    t.TRD_NUM,                                   -- TRD_NUM
    t.ORIG_TRD_NUM,                              -- ORIG_TRD_NUM
    t.TXN_SRC_KEY,                               -- TXN_SRC_KEY
    sec_apa.APA_ID,                              -- SEC_APA_ID
    csh_apa.APA_ID,                              -- CSH_APA_ID
    sec_apa.SCR_PRC_IN_TXN_CCY,                  -- SEC_PRC_IN_TXN_CCY
    sec_apa.TXN_AMT,                             -- SEC_TXN_AMT
    csh_apa.TXN_AMT,                             -- CASH_TXN_AMT
    sec_apa.ORIG_QTY,                            -- SEC_ORIG_QTY
    sec_apa.SCR_PRC_IN_SBC,                      -- SEC_SRC_PRC_IN_SBC
    NULL,                                        -- SALE_CHRG_RATE_PCT
    sec_apa.SALE_CHRG_RATE_TP_ID,                -- SALE_CHRG_RATE_TP_ID
    sec_apa.STM_BASE_CCY_AMT,                    -- SEC_SBC_AMT
    csh_apa.STM_BASE_CCY_AMT,                    -- CASH_SBC_AMT
    sec_apa.STM_BASE_CCY_CLC_DTM,                -- SBC_CCY_CALC_DT
    sec_apa.STM_BASE_CCY_EXG_RATE,               -- SBC_EXG_RATE
    t.BUY_SELL_IND,                              -- BUY_SELL_IND
    t.CF_GEN_F,                                  -- CF_GEN_F
    t.TXN_CL_F,                                  -- TXN_CL_F
    t.OPTS_CLSS_CODE,                            -- OPT_CLSS_CD
    sec_apa.SRC_PD_ID,                           -- SEC_SRC_CUSIP
    t.TXN_TP_ID,                                 -- TXN_TP_ID
    t.TXN_SBTP_ID,                               -- TXN_SBTP_ID
    t.EXEC_TP_ID,                                -- EXEC_TP_ID
    t.EXEC_SBTP_ID,                              -- EXEC_SBTP_ID
    sec_apa.APA_TP_ID,                           -- SEC_APA_TP_ID
    csh_apa.APA_TP_ID,                           -- CSH_APA_TP_ID
    SYSDATE,                                     -- CRT_DTM
    'ODI_ETL',                                   -- CRT_USR_NM
    t.AR_ID,                                     -- AR_ID
    sec_apa.PD_ID,                               -- SEC_PD_ID
    csh_apa.PD_ID,                               -- CASH_PD_ID
    sec_apa.BKR_AR_ID,                           -- BKR_AR_ID
    sec_apa.OFST_AR_ID,                          -- OFST_AR_ID
    t.BR_CODE,                                   -- BR_CD
    t.FA_NUM,                                    -- FA_NUM
    t.SRC_CRT_USRNM,                             -- SRC_CRT_USRNM
    t.ORIG_TD,                                   -- ORIG_TD
    (SELECT MIN(spc.SRC_PCS_TP_CD) FROM TRANSACTIONS_OWNER.SRC_PCS_TP_DIM spc WHERE spc.SRC_PCS_TP_ID = t.SRC_PCS_TP_ID),  -- SRC_PCS_TP_CD
    (SELECT MIN(spc.SRC_PCS_TP_NM) FROM TRANSACTIONS_OWNER.SRC_PCS_TP_DIM spc WHERE spc.SRC_PCS_TP_ID = t.SRC_PCS_TP_ID),  -- SRC_PCS_TP_NM
    (SELECT MIN(ssd.SRC_STM_CD) FROM COMMON_OWNER.SRC_STM_DIM ssd WHERE ssd.SRC_STM_ID = t.SRC_STM_ID),  -- SRC_STM_CD
    (SELECT MIN(ssd.SRC_STM_NM) FROM COMMON_OWNER.SRC_STM_DIM ssd WHERE ssd.SRC_STM_ID = t.SRC_STM_ID),  -- SRC_STM_NM
    t.SRC_TXN_TP,                                -- SHDW_TXN_TP_CD
    (SELECT MIN(stt.SRC_TXN_TP_NM) FROM CCAL_REPL_OWNER.SHDW_TXN_TP stt WHERE stt.SRC_TXN_TP = t.SRC_TXN_TP),  -- SHDW_TXN_TP_NM
    t.SRC_ACTN_CODE,                             -- IMP_SRC_ACTN_CD
    t.SRC_BUY_SELL_MULTI_ID,                     -- SIS_DLTD_EV_ID
    t.USR_ID,                                    -- SIS_USR_ID
    t.SRC_PRIM_CL,                               -- SIS_AC_CL_CD
    t.SRC_CNCL_RSN_ID,                           -- SRC_CNCL_RSN_ID
    t.SRC_TXN_SEQ_NUM,                           -- SRC_TXN_SEQ_NUM
    t.DVDN_RCRD_DT,                              -- DVDN_RCRD_DT
    t.SALE_PSN_NUM,                              -- OWN_IMP_FA_NUM
    t.SRC_STM_ID,                                -- SRC_STM_ID
    t.SRC_CXL_REV,                               -- SRC_CNCL_X_RVSN_CD
    t.SRC_OPT_CLS,                               -- SRC_OPT_CLSS_CD
    t.TXN_REPL_CNT,                              -- TXN_RCNCL_REPL_CNT
    t.TXN_ST_ID,                                 -- TXN_RCNCL_ST_ID
    t.LGCY_CNCL_CMPLN_RSN_TP_ID,                -- LGCY_CNCL_CMPLN_RSN_TP_ID
    t.LGCY_CNCL_CMPLN_SRC_TP_ID,                -- LGCY_CNCL_CMPLN_SRC_TP_ID
    t.LGCY_MKT_TP_ID,                            -- LGCY_MKT_TP_ID
    t.LGCY_TRD_CPCTY_TP_ID,                     -- LGCY_TRD_CPCTY_TP_ID
    NULL,                                        -- REL_TXN_SRC_STM_ID
    t.SRC_ENTR_CNL_TP_ID,                        -- SRC_ENTR_CNL_TP_ID
    t.SRC_PCS_TP_ID,                             -- SRC_PCS_TP_ID
    t.TRD_SLCT_TP_ID,                            -- TRD_SLCT_TP_ID
    'ODI_ETL',                                   -- LAST_UDT_USR_NM
    SYSDATE,                                     -- LAST_UDT_DTM
    'Y',                                         -- ACTV_F
    TRUNC(SYSDATE),                              -- BATCH_DT
    t.SRC_TXN_CODE,                              -- SRC_TXN_CODE
    t.OMS_EXEC_TP_ID,                            -- OMS_EXEC_TP_ID
    t.OMS_ORDR_KEY,                              -- OMS_ORDR_KEY
    t.ORIG_SRC_STM_ID,                           -- ORIG_SRC_STM_ID
    t.ORIG_SRC_STM_CODE,                         -- ORIG_SRC_STM_CD
    t.DRVD_TRD_CPCTY_TP_ID,                     -- DRVD_TRD_CPCTY_ID
    t.ADL_TRD_INSR,                              -- ADL_TRD_INSR
    t.SRC_ENTR_DTM,                              -- SRC_ENTR_DTM
    t.EXG_CODE,                                  -- EXG_CD
    t.DOL_IND_ID,                                -- DOL_IND_ID
    NULL,                                        -- SESS_NO
    t.TM_PRC_DSCTN_F,                            -- TM_PRC_DISCRETION_F
    sec_apa.SRC_SEQ_NUM,                         -- SEC_SRC_SEQ_NUM
    csh_apa.SRC_SEQ_NUM,                         -- CASH_SRC_SEQ_NUM
    sec_apa.APA_EXT_QUALFR,                      -- SEC_APA_EXT_QUALFR
    csh_apa.APA_EXT_QUALFR,                      -- CASH_APA_EXT_QUALFR
    sec_apa.APA_DSC,                             -- SEC_DSC_TRAILER_1
    csh_apa.APA_DSC,                             -- CASH_DSC_TRAILER_1
    sec_apa.ALT_DSC,                             -- SEC_ALT_DSC_TRAILER_2
    csh_apa.ALT_DSC,                             -- CASH_ALT_DSC_TRAILER_2
    sec_apa.CLNT_FRIENDLY_DESC,                  -- CLNT_FRIENDLY_DSC
    NULL,                                        -- SEC_CIRD_PD_ID
    NULL,                                        -- CASH_CIRD_PD_ID
    sec_apa.SRC_EFF_DT,                          -- SRC_EFF_DT
    sec_apa.TRD_FCTR_RATE,                       -- TRD_FCTR_RATE
    sec_apa.MRKUP_RATE,                          -- MRKUP_RATE
    sec_apa.STD_CMSN_AMT,                        -- STD_CMSN_AMT
    sec_apa.SYMB,                                -- SYMB
    sec_apa.ISIN,                                -- ISIN
    sec_apa.OPT_SYMB,                            -- SCR_OPT_SYMB
    sec_apa.PD_CMPOS_DSC                         -- PD_CMPOS_DSC
FROM (SELECT * FROM CCAL_REPL_OWNER.TXN WHERE ROWNUM <= 10) t
-- Securities side APA (1 row per TXN via ROWNUM)
LEFT JOIN (
    SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.EXEC_ID ORDER BY a.APA_ID) rn
    FROM CCAL_REPL_OWNER.APA a WHERE a.DB_CR_ID = 1
) sec_apa ON sec_apa.EXEC_ID = t.TXN_ID AND sec_apa.rn = 1
-- Cash side APA (1 row per TXN via ROWNUM)
LEFT JOIN (
    SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.EXEC_ID ORDER BY a.APA_ID) rn
    FROM CCAL_REPL_OWNER.APA a WHERE a.DB_CR_ID = 2
) csh_apa ON csh_apa.EXEC_ID = t.TXN_ID AND csh_apa.rn = 1
"""

print("  Executing INSERT...SELECT (ROWNUM <= 10)...")
ok, msg = run_sql(insert_sql, timeout=300)
print(f"  Result: {'SUCCESS' if ok else 'FAILED'}")
print(f"  Message: {msg}")

if not ok:
    print("\n  Attempting to diagnose...")
    # Try just the SELECT to see if it works
    select_only = insert_sql.replace("INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (", "-- INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (")
    # Actually just try a simpler version
    simple_sql = """
    SELECT t.TXN_ID, t.TD, t.AR_ID, sec_apa.APA_ID AS SEC_APA, ssd.SRC_STM_NM
    FROM CCAL_REPL_OWNER.TXN t
    LEFT JOIN CCAL_REPL_OWNER.APA sec_apa ON sec_apa.EXEC_ID = t.TXN_ID AND sec_apa.DB_CR_ID = 1
    LEFT JOIN COMMON_OWNER.SRC_STM_DIM ssd ON ssd.SRC_STM_ID = t.SRC_STM_ID
    WHERE ROWNUM <= 3
    """
    ok2, msg2 = run_sql(simple_sql)
    print(f"  Simple SELECT test: {'OK' if ok2 else 'FAILED'} - {msg2[:100]}")
    
    if not ok2:
        # Try without APA join
        simple_sql2 = """
        SELECT t.TXN_ID, t.TD, t.AR_ID, ssd.SRC_STM_NM
        FROM CCAL_REPL_OWNER.TXN t
        LEFT JOIN COMMON_OWNER.SRC_STM_DIM ssd ON ssd.SRC_STM_ID = t.SRC_STM_ID
        WHERE ROWNUM <= 3
        """
        ok3, msg3 = run_sql(simple_sql2)
        print(f"  Without APA: {'OK' if ok3 else 'FAILED'} - {msg3[:100]}")

# Step 3: Verify
if ok:
    print("\n" + "=" * 60)
    print("STEP 3: VERIFY INSERT")
    print("=" * 60)
    ok_v, msg_v = run_sql("SELECT COUNT(*) AS CNT FROM IKOROSTELEV.AVY_FACT_SIDE")
    print(f"  {msg_v}")
    
    ok_v2, msg_v2 = run_sql("""
        SELECT TXN_ID, TD, AR_ID, SRC_STM_CD, SHDW_TXN_TP_CD, BUY_SELL_IND, BR_CD
        FROM IKOROSTELEV.AVY_FACT_SIDE WHERE ROWNUM <= 5
    """)
    print(f"  Sample: {msg_v2}")

print("\nDone.")
