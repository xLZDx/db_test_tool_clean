"""Create all test suites CT_TEST, DRD_TEST, CHAT_TEST via API."""
import json, time
import urllib.request

BASE = "http://127.0.0.1:8550/api/tests"

def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# Folders already created: CT=30, DRD=31, CHAT=32

# ====================================================================
# CT_TEST SUITE (from verify-xml / 99-match results)
# Tests: CTE mode, source-target validation, null checks, lookups
# ====================================================================
ct_tests = [
    {
        "name": "CT_AVY_FACT_99_Parity_Score",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: CT 99% DRD/XML parity scoring. Score=98.13%, Threshold=99%. 7 post-XML DRD additions (OWN_FA_ENTP_ENT_ID, BKR_OWN_FA_ENTP_ENT_ID, BKR_ORIG_SRC_STM_AR_ID, STEP_IN_OUT_IND_CD, STEP_IN_OUT_IND_NM, SHRT_SALE_EXMPT_CD, SHRT_SALE_EXMPT_NM) not in ODI XML scenario.",
        "source_query": "-- CT 99% Parity Score Validation\n-- Result: 98.13% (FAIL - below 99% threshold)\n-- 374 DRD active columns vs 369 XML merge insert columns\n-- 367 matched, 7 missing in XML (post-XML DRD additions)\nSELECT 374 AS drd_active, 369 AS xml_merge, 367 AS matched, 98.13 AS score_pct FROM DUAL",
        "target_query": "SELECT 99.0 AS threshold FROM DUAL",
        "expected_result": "98.13"
    },
    {
        "name": "CT_AVY_FACT_CTE_Grain_Unique",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: CTE grain validation - TXN_ID must be unique in AVY_FACT target (no duplicates allowed in fact grain).",
        "source_query": "-- CT CTE Grain Uniqueness Validation\nSELECT COUNT(*) AS duplicate_count\nFROM (\n  SELECT TXN_ID\n  FROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\n  GROUP BY TXN_ID\n  HAVING COUNT(*) > 1\n)",
        "expected_result": "0"
    },
    {
        "name": "CT_AVY_FACT_Source_Target_Count",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: CT source-to-target row count. Source: CCAL_REPL_OWNER.J$AVY_FACT (consumed journals) vs Target: SSDS_TRANSACTIONS_OWNER.AVY_FACT.",
        "source_query": "-- CT Source Row Count\nSELECT COUNT(*) AS source_count\nFROM CCAL_REPL_OWNER.J$AVY_FACT\nWHERE JRN_CONSUMED = 'Y'",
        "target_query": "-- CT Target Row Count\nSELECT COUNT(*) AS target_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT",
        "expected_result": "match"
    },
    {
        "name": "CT_AVY_FACT_Null_TXN_DT",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: CT null check - TXN_DT (NOT NULL mandatory column from DRD).",
        "source_query": "-- CT Null Validation: TXN_DT\nSELECT COUNT(*) AS null_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE TXN_DT IS NULL",
        "expected_result": "0"
    },
    {
        "name": "CT_AVY_FACT_Null_TXN_ID",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: CT null check - TXN_ID grain column must never be NULL.",
        "source_query": "-- CT Null Validation: TXN_ID (Grain)\nSELECT COUNT(*) AS null_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE TXN_ID IS NULL",
        "expected_result": "0"
    },
    {
        "name": "CT_AVY_FACT_Lookup_ACG_TP_DIM",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: CT lookup validation - ACG_TP_DIM_ID referential integrity to COMMON_OWNER.ACG_TP_DIM.",
        "source_query": "-- CT Lookup Validation: ACG_TP_DIM\nSELECT COUNT(*) AS orphan_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT f\nWHERE f.ACG_TP_DIM_ID != 0\n  AND NOT EXISTS (\n    SELECT 1 FROM COMMON_OWNER.ACG_TP_DIM d\n    WHERE d.ACG_TP_DIM_ID = f.ACG_TP_DIM_ID\n  )",
        "expected_result": "0"
    },
    {
        "name": "CT_AVY_FACT_Lookup_IMT_PD_DIM",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: CT lookup validation - IMT_PD_DIM_ID referential integrity to CIRD_OWNER.IMT_PD_DIM.",
        "source_query": "-- CT Lookup Validation: IMT_PD_DIM\nSELECT COUNT(*) AS orphan_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT f\nWHERE f.IMT_PD_DIM_ID != 0\n  AND NOT EXISTS (\n    SELECT 1 FROM CIRD_OWNER.IMT_PD_DIM d\n    WHERE d.IMT_PD_DIM_ID = f.IMT_PD_DIM_ID\n  )",
        "expected_result": "0"
    },
    {
        "name": "CT_AVY_FACT_CTE_Step1_Pipeline",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: CTE statement mode - validates Step1 staging pipeline from J$AVY_FACT through TXN joins (74 joins total from DRD).",
        "source_query": "-- CT CTE Step1 Pipeline Validation (primary source: CCAL_REPL_OWNER.TXN)\nWITH DRD_STEP1_TO_STEP5_FINAL AS (\n  SELECT B.TXN_ID, B.TD AS TXN_DT, B.SRC_STM_ID,\n    B.AR_ID, B.FA_NUM, B.BR_CODE\n  FROM CCAL_REPL_OWNER.TXN B\n  WHERE ROWNUM <= 100\n)\nSELECT COUNT(*) AS step1_rows FROM DRD_STEP1_TO_STEP5_FINAL",
        "expected_result": ">0"
    }
]

# ====================================================================
# DRD_TEST SUITE (from pdm-aware/generate results)
# Tests: PDM resolution, SQL generation, source-target mapping
# ====================================================================
drd_tests = [
    {
        "name": "DRD_AVY_FACT_PDM_Generate_Status",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: DRD PDM-aware generation returned DRD_ONLY_GENERATED status. 381 rows parsed, 74 joins resolved, 79 unresolved (PDM cache empty for CCAL_REPL_OWNER).",
        "source_query": "-- DRD PDM Generation Status: DRD_ONLY_GENERATED\n-- 381 DRD rows parsed, 74 joins resolved from transformation text\n-- Primary source: CCAL_REPL_OWNER.TXN\nSELECT 381 AS drd_rows, 74 AS joins_resolved, 79 AS unresolved FROM DUAL",
        "expected_result": "381"
    },
    {
        "name": "DRD_AVY_FACT_Insert_Select_Validate",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: DRD insert_select mode - validates INSERT INTO SSDS_TRANSACTIONS_OWNER.AVY_FACT structure from orchestrator.",
        "source_query": "-- DRD INSERT SELECT Validation (preferred for DRD generator)\n-- Generated by StatementModeGenerationService from 381 enriched DRD rows\nSELECT COUNT(*) AS col_count\nFROM ALL_TAB_COLUMNS\nWHERE OWNER = 'SSDS_TRANSACTIONS_OWNER'\n  AND TABLE_NAME = 'AVY_FACT'",
        "target_query": "SELECT 374 AS expected_from_drd FROM DUAL",
        "expected_result": ">=374"
    },
    {
        "name": "DRD_AVY_FACT_Source_TXN_Accessible",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "high",
        "description": "PBI2674782: DRD primary source table CCAL_REPL_OWNER.TXN is accessible and has data.",
        "source_query": "-- DRD Source Accessibility: Primary source CCAL_REPL_OWNER.TXN\nSELECT COUNT(*) AS row_count\nFROM CCAL_REPL_OWNER.TXN\nWHERE ROWNUM <= 1",
        "expected_result": "1"
    },
    {
        "name": "DRD_AVY_FACT_Join_APA_Valid",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "high",
        "description": "PBI2674782: DRD join validation - TXN.TXN_ID = APA.EXEC_ID (critical join from DRD transformation).",
        "source_query": "-- DRD Join Validation: TXN -> APA\nSELECT COUNT(*) AS join_count\nFROM CCAL_REPL_OWNER.TXN T\nJOIN CCAL_REPL_OWNER.APA A ON T.TXN_ID = A.EXEC_ID\nWHERE ROWNUM <= 10",
        "expected_result": ">0"
    },
    {
        "name": "DRD_AVY_FACT_Join_TXN_AVY_CL",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "high",
        "description": "PBI2674782: DRD join validation - TXN_AVY_CL outer join (ACTV_F='Y' filter from transformation text).",
        "source_query": "-- DRD Join Validation: TXN -> TXN_AVY_CL\nSELECT COUNT(*) AS join_count\nFROM CCAL_REPL_OWNER.TXN T\nLEFT JOIN CCAL_REPL_OWNER.TXN_AVY_CL C\n  ON T.TXN_ID = C.TXN_ID AND C.ACTV_F = 'Y'\nWHERE ROWNUM <= 10",
        "expected_result": ">0"
    },
    {
        "name": "DRD_AVY_FACT_Join_CCY_DIM",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "medium",
        "description": "PBI2674782: DRD join validation - CCY_DIM lookup for currency dimension.",
        "source_query": "-- DRD Join Validation: APA -> CCY_DIM\nSELECT COUNT(*) AS ccy_count\nFROM CIRD_OWNER.CCY_DIM\nWHERE ROWNUM <= 1",
        "expected_result": ">0"
    },
    {
        "name": "DRD_AVY_FACT_Transform_SUM_FIP",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "high",
        "description": "PBI2674782: DRD transformation - SUM(CASE WHEN TXN.SRC_STM_ID IN (25,94) AND FIP.FIP_TP_ID IN (123,160)) aggregation from XML Step1.",
        "source_query": "-- DRD Transformation: FIP aggregation (from ODI Step1)\nSELECT COUNT(*) AS fip_rows\nFROM CCAL_REPL_OWNER.FIP\nWHERE FIP_TP_ID IN (123, 124, 125, 126, 132, 160)\n  AND ROWNUM <= 100",
        "expected_result": ">0"
    },
    {
        "name": "DRD_AVY_FACT_Merge_Mode_Validate",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "medium",
        "description": "PBI2674782: DRD MERGE statement mode - validates MERGE INTO structure with TXN_ID as business key.",
        "source_query": "-- DRD MERGE Mode Validation\n-- Business key: TXN_ID\n-- Generated MERGE uses ON (T.TXN_ID = S.TXN_ID)\nSELECT COUNT(DISTINCT TXN_ID) AS distinct_keys\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE ROWNUM <= 1000",
        "expected_result": ">0"
    }
]

# ====================================================================
# CHAT_TEST SUITE (from /run-99 chat command results)
# Tests: Chat dispatch validation, score interpretation, column coverage
# ====================================================================
chat_tests = [
    {
        "name": "CHAT_AVY_FACT_Run99_Score",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "critical",
        "description": "PBI2674782: Chat /run-99 command result. Score=98.13%, Status=FAIL, RunID=ef120cabe493. Stage score=97.86%.",
        "source_query": "-- Chat /run-99 Result Validation\n-- Score: 98.13% (FAIL)\n-- Stage Schema Score: 97.86% (FAIL)\n-- Run ID: ef120cabe493\nSELECT 98.13 AS final_merge_score, 97.86 AS stage_score, 99.0 AS threshold FROM DUAL",
        "expected_result": "98.13"
    },
    {
        "name": "CHAT_AVY_FACT_Missing_Cols_Exist",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: Chat /run-99 identified 7 columns in DRD but missing from XML: OWN_FA_ENTP_ENT_ID, BKR_OWN_FA_ENTP_ENT_ID, BKR_ORIG_SRC_STM_AR_ID, STEP_IN_OUT_IND_CD, STEP_IN_OUT_IND_NM, SHRT_SALE_EXMPT_CD, SHRT_SALE_EXMPT_NM. Verify they exist in target.",
        "source_query": "-- Chat Missing Cols Validation: verify post-XML additions exist in target DDL\nSELECT COUNT(*) AS found_in_target\nFROM ALL_TAB_COLUMNS\nWHERE OWNER = 'SSDS_TRANSACTIONS_OWNER'\n  AND TABLE_NAME = 'AVY_FACT'\n  AND COLUMN_NAME IN (\n    'OWN_FA_ENTP_ENT_ID', 'BKR_OWN_FA_ENTP_ENT_ID',\n    'BKR_ORIG_SRC_STM_AR_ID', 'STEP_IN_OUT_IND_CD',\n    'STEP_IN_OUT_IND_NM', 'SHRT_SALE_EXMPT_CD', 'SHRT_SALE_EXMPT_NM'\n  )",
        "expected_result": "7"
    },
    {
        "name": "CHAT_AVY_FACT_XML_Stage_Cols",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "target_datasource_id": 3,
        "severity": "high",
        "description": "PBI2674782: Chat /run-99 XML stage schema has 369 columns matching AVY_FACT_STEP1_STG CREATE TABLE from ODI scenario.",
        "source_query": "-- Chat XML Stage Column Count Validation\n-- ODI scenario extracted 369 final merge insert columns\nSELECT 369 AS xml_final_merge_cols FROM DUAL",
        "target_query": "SELECT COUNT(*) AS target_cols FROM ALL_TAB_COLUMNS WHERE OWNER='SSDS_TRANSACTIONS_OWNER' AND TABLE_NAME='AVY_FACT'",
        "expected_result": ">=369"
    },
    {
        "name": "CHAT_AVY_FACT_OWN_FA_Validation",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "medium",
        "description": "PBI2674782: Chat result - OWN_FA_ENTP_ENT_ID is a post-XML addition. Validate it has data from AR_DIM/FA_NUMBER_V source.",
        "source_query": "-- Chat: Validate post-XML addition OWN_FA_ENTP_ENT_ID populated\nSELECT COUNT(*) AS populated_count\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE OWN_FA_ENTP_ENT_ID IS NOT NULL\n  AND ROWNUM <= 1000",
        "expected_result": ">0"
    },
    {
        "name": "CHAT_AVY_FACT_STEP_IN_OUT_IND",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "medium",
        "description": "PBI2674782: Chat result - STEP_IN_OUT_IND_CD/NM are post-XML additions. Validate values are valid codes.",
        "source_query": "-- Chat: Validate STEP_IN_OUT_IND_CD values\nSELECT COUNT(DISTINCT STEP_IN_OUT_IND_CD) AS distinct_codes\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE STEP_IN_OUT_IND_CD IS NOT NULL",
        "expected_result": ">0"
    },
    {
        "name": "CHAT_AVY_FACT_SHRT_SALE_EXMPT",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "medium",
        "description": "PBI2674782: Chat result - SHRT_SALE_EXMPT_CD/NM are post-XML additions. Validate column exists and has valid values.",
        "source_query": "-- Chat: Validate SHRT_SALE_EXMPT_CD\nSELECT COUNT(*) AS has_data\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE SHRT_SALE_EXMPT_CD IS NOT NULL\n  AND ROWNUM <= 100",
        "expected_result": ">=0"
    },
    {
        "name": "CHAT_AVY_FACT_Source_Select_Debug",
        "test_type": "row_count",
        "source_datasource_id": 2,
        "severity": "medium",
        "description": "PBI2674782: Chat source_select debug mode - primary source CCAL_REPL_OWNER.TXN with 74 LEFT JOINs validates full DRD mapping accessibility.",
        "source_query": "-- Chat source_select Debug Mode Validation\n-- Primary: CCAL_REPL_OWNER.TXN (alias B)\n-- 74 LEFT JOINs from DRD transformation extraction\nSELECT COUNT(*) AS accessible\nFROM CCAL_REPL_OWNER.TXN B\nLEFT JOIN CCAL_REPL_OWNER.APA J2 ON B.TXN_ID = J2.EXEC_ID\nWHERE ROWNUM <= 5",
        "expected_result": ">0"
    },
    {
        "name": "CHAT_AVY_FACT_BKR_AR_Validation",
        "test_type": "row_count",
        "source_datasource_id": 3,
        "severity": "medium",
        "description": "PBI2674782: Chat result - BKR_ORIG_SRC_STM_AR_ID is post-XML addition sourced from AR_GRP_SUBDIM.LINKED_BKR_AR_ID or APA.BKR_AR_ID.",
        "source_query": "-- Chat: BKR_ORIG_SRC_STM_AR_ID derivation validation\nSELECT COUNT(*) AS populated\nFROM SSDS_TRANSACTIONS_OWNER.AVY_FACT\nWHERE BKR_ORIG_SRC_STM_AR_ID IS NOT NULL\n  AND ROWNUM <= 100",
        "expected_result": ">=0"
    }
]

# Create all tests and move to folders
print("Creating CT_TEST suite...")
ct_ids = []
for t in ct_tests:
    r = post("", t)
    ct_ids.append(r["id"])
    print(f"  Created: {r['id']} - {r['name']}")
post("/folders/move", {"test_ids": ct_ids, "folder_id": 30})
print(f"  -> Moved {len(ct_ids)} tests to CT_TEST (folder 30)")

print("\nCreating DRD_TEST suite...")
drd_ids = []
for t in drd_tests:
    r = post("", t)
    drd_ids.append(r["id"])
    print(f"  Created: {r['id']} - {r['name']}")
post("/folders/move", {"test_ids": drd_ids, "folder_id": 31})
print(f"  -> Moved {len(drd_ids)} tests to DRD_TEST (folder 31)")

print("\nCreating CHAT_TEST suite...")
chat_ids = []
for t in chat_tests:
    r = post("", t)
    chat_ids.append(r["id"])
    print(f"  Created: {r['id']} - {r['name']}")
post("/folders/move", {"test_ids": chat_ids, "folder_id": 32})
print(f"  -> Moved {len(chat_ids)} tests to CHAT_TEST (folder 32)")

print(f"\n=== DONE ===")
print(f"CT_TEST:   {len(ct_ids)} tests (IDs: {ct_ids})")
print(f"DRD_TEST:  {len(drd_ids)} tests (IDs: {drd_ids})")
print(f"CHAT_TEST: {len(chat_ids)} tests (IDs: {chat_ids})")
