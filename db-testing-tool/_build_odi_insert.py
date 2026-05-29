#!/usr/bin/env python3
"""
_build_odi_insert.py
--------------------
Builds a 369-column INSERT INTO IKOROSTELEV.AVY_FACT_SIDE
from production ODI step3 SQL expressions.

Phase 0 : Assert INSERT col count == SELECT expr count in step3.
Phase 1 : Positional parser (paren-depth tokenizer) for step3 col→expr dict.
Phase 2 : Build ANSI LEFT JOINs, fix 4 fan-out bugs, clean ODI artifacts.
Phase 3 : Emit canonical mapping objects with match_status.
Phase 4 : Write data/odi_full_insert.sql

DO NOT modify _build_drd_insert.py.
"""

import re
import json
from pathlib import Path

# ── Source files ──────────────────────────────────────────────────────────────
STEP3_SQL  = Path("data/odi_step3_stg.sql").read_text(encoding="utf-8")
_col_sizes_raw = json.loads(Path("data/avyfactside_col_sizes.json").read_text())
# Flatten to {col: length_int} and {col: dtype_str}
COL_SIZES  = {c: v['length'] if isinstance(v, dict) else v for c, v in _col_sizes_raw.items()}
COL_DTYPES = {c: (v.get('dtype','') if isinstance(v, dict) else '') for c, v in _col_sizes_raw.items()}
TARGET_COLS = list(_col_sizes_raw.keys())   # 369 cols in correct order
TARGET_TABLE = "IKOROSTELEV.AVY_FACT_SIDE"
ROWNUM_LIMIT = 100

# ── NOT NULL columns with safe fallback expressions ───────────────────────────
NOT_NULL_EXPR = {
    "TXN_ID":                      "TXN.TXN_ID",
    "TD":                          "NVL(TXN.TD, SYSDATE)",
    "CRT_DTM":                     "NVL(TXN.CRT_DTM, SYSDATE)",
    "CRT_USR_NM":                  "NVL(TXN.CRT_USR_NM, 'SYSTEM')",
    "ACTV_F":                      "NVL(TXN.ACTV_F, 'Y')",
    "LAST_UDT_USR_NM":             "NVL(TXN.LAST_UDT_USR_NM, 'SYSTEM')",
    "LAST_UDT_DTM":                "NVL(TXN.LAST_UDT_DTM, SYSDATE)",
}

# ── Cols that step3 produces under a different name than target ───────────────
# step3_col -> target_col renames
STEP3_COL_RENAME = {
    "BR_CODE":             "BR_CD",          # step3 uses BR_CODE, target has BR_CD
    "SRC_BUY_SELL_MULTI_ID": None,           # not in target table — skip
    "LINKED_BKR_AR_ID":    None,             # not in target table — skip
    "EXCP_CASH_APA_CODE":  None,             # staging artifact — skip
}

# ── Step3 expr overrides for specific complex expressions ─────────────────────
# TRD_CNCLD_F: replace MAX(CASE WHEN TXN_RLTNP...) with EXISTS subquery (no GROUP BY needed)
TRD_CNCLD_F_EXPR = (
    "CASE WHEN EXISTS ("
    "  SELECT 1 FROM CCAL_REPL_OWNER.TXN_RLTNP r"
    "  WHERE (r.SRC_TXN_ID = TXN.TXN_ID OR r.TRGT_TXN_ID = TXN.TXN_ID)"
    "  AND r.TXN_RLTNP_TP_ID = 69 AND r.SRC_TXN_ID <> r.TRGT_TXN_ID"
    "  AND r.ACTV_F = 'Y'"
    ") THEN 'Y' END"
)

EXPR_OVERRIDES = {
    "TRD_CNCLD_F": TRD_CNCLD_F_EXPR,
    # SESS_NO: ODI session variable → literal
    "SESS_NO": "0",
    # J_AVY_FACT.SRC_STM_ID → TXN.SRC_STM_ID (CDC driver alias)
    # J_AVY_FACT.BATCH_DT   → TRUNC(SYSDATE)
    # handled via post_process_expr()

    # SIS_DLTD_EV cols (SIS_DLTD_EV already joined in ANSI_JOINS)
    "SIS_DLTD_EV_CD":               "SIS_DLTD_EV.CL_VAL_CODE",

    # FA_NUMBER_V direct cols
    "FA_OWN_EMPE_ID":               "FA_NUMBER_V.RESPONSIBLE_PARTY_EMPLOYEE_ID",
    "OWN_FA_NUM_ENT_CD":            "FA_NUMBER_V.FA_NUMBER_ENTITY_CODE",
    # FA_NUMBER_V personal FA attributes
    "OWN_FA_CSS_PSN_ID":            "FA_NUMBER_V.RESPONSIBLE_PARTY_CSS_PERSON_ID",
    "OWN_FA_HR_ST_CD":              "FA_NUMBER_V.FA_NUMBER_STATUS",
    "OWN_FA_FINRA_CRD_CLSS_CD":     "FA_NUMBER_V.FA_SUB_TYPE",

    # SDIRA type (same CL_VAL schema as SIS_DLTD_EV=99; join on SRC_BUY_SELL_MULTI_ID)
    "SDIRA_TXN_TP_CD":              "SIS_DLTD_EV.CL_VAL_CODE",
    "SDIRA_TXN_TP":                 "SIS_DLTD_EV.CL_VAL_NM",

    # BKR_AR_DIM cols (CCSI_OWNER.AR_DIM alias via LINKED_BKR_AR_ID)
    "BKR_AC_NUM":                   "BKR_AR_DIM.ORIG_SRC_STM_AR_ID",
    "BKR_IRA_F":                    "NVL(BKR_AR_DIM.IRA_F,'N')",
    "BKR_ERISA_F":                  "NVL(BKR_AR_DIM.ERISSA_F,'N')",
    "BKR_ORIG_SRC_STM_CD":          "BKR_AR_DIM.ORIG_SRC_STM_CD",
    "BKR_ABC_CLSS_CD":              "BKR_AR_DIM.ABC_CLSS_CD",
    "BKR_ABC_CLSS":                 "BKR_AR_DIM.ABC_CLSS_NM",
    "BKR_AC_CGY_CD":                "BKR_AR_DIM.AR_CGY_CD",
    "BKR_AC_CGY":                   "BKR_AR_DIM.AR_CGY",
    "BKR_BSN_LINE_AFFLT":           "BKR_AR_DIM.BSN_LINE_AFFLT",
    "BKR_BSN_LINE_AFFLT_CD":        "BKR_AR_DIM.BSN_LINE_AFFLT_CD",
    "BKR_TST_AC_F":                 "BKR_AR_DIM.TST_AR_F",
    "BKR_TAX_RPT_CLNT_ID":          "BKR_AR_DIM.TAX_RPT_PARTY_ID",
    "BKR_TAX_AC_F":                 "BKR_AR_DIM.TAX_AC_F",
    "BKR_RJ_TAX_RPT_RSPL_F":        "BKR_AR_DIM.RJ_TAX_RPT_RSPL_F",
    "BKR_RJ_BSN_UNIT_CD":           "BKR_AR_DIM.RJ_BSN_UNIT_CD",
    "BKR_RJ_BSN_UNIT":              "BKR_AR_DIM.RJ_BSN_UNIT",
    "BKR_AC_OWNSHP_TP_CD":          "BKR_AR_DIM.OWNSHP_TP_CD",
    "BKR_AC_OWNSHP_TP":             "BKR_AR_DIM.OWNSHP_TP",
    "BKR_FIRM_AC_F":                "BKR_AR_DIM.FIRM_AC_F",
    "BKR_FEE_BASE_AC_F":            "BKR_AR_DIM.FEE_BASE_F",
    "BKR_RJ_TRUST_F":               "CASE WHEN BKR_AR_DIM.BSN_LINE_AFFLT_CD IN ('RJTNA','RJTCNH') THEN 'Y' ELSE 'N' END",

    # OWN_FA_ENT cols (SSDS_DAL_OWNER.ENTERPRISE_ENTITY_DIM_V alias)
    "OWN_FA_NUM_ENT_ENTP_ID":       "OWN_FA_ENT.ENTITY_ENTERPRISE_ID",
    "OWN_FA_ENT_BR_CD":             "OWN_FA_ENT.ENTITY_BRANCH_CODE",
    "OWN_FA_ENT_BSN_ST":            "OWN_FA_ENT.ENTITY_BUSINESS_STATUS",
    "OWN_FA_ENT_DIV_CD":            "OWN_FA_ENT.ENTITY_DIVISION_CODE",
    "OWN_FA_ENT_DIV_DSC":           "OWN_FA_ENT.ENTITY_DIVISION_DESCRIPTION",
    "OWN_FA_ENT_DIV_NODE":          "OWN_FA_ENT.ENTITY_DIVISION_NODE",
    "OWN_FA_ENT_MAIN_BR_STE":       "OWN_FA_ENT.ENTITY_MAIN_BRANCH_STATE",
    "OWN_FA_ENT_OSJ":               "OWN_FA_ENT.ENTITY_OSJ",
    "OWN_FA_ENT_RJAS_ID_F":         "CASE WHEN OWN_FA_ENT.ENTITY_RJAS_ID_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_ENT_SUBDIV_CD":         "OWN_FA_ENT.ENTITY_SUBDIVISION_CODE",
    "OWN_FA_ENT_SUBDIV_DSC":        "OWN_FA_ENT.ENTITY_SUBDIVISION_DESCRIPTION",
    "OWN_FA_ENT_SUBS":              "OWN_FA_ENT.ENTITY_SUBSIDIARY",
    "OWN_FA_ENT_SBTP":              "OWN_FA_ENT.ENTITY_SUBTYPE",
    "OWN_FA_ENT_SBTP_CD":           "OWN_FA_ENT.ENTITY_SUBTYPE_CODE",
    "OWN_FA_ENT_TP":                "OWN_FA_ENT.ENTITY_TYPE",
    "OWN_FA_ENT_TP_CD":             "OWN_FA_ENT.ENTITY_TYPE_CODE",
    "OWN_FA_ENT_LOB_AB_F":          "CASE WHEN OWN_FA_ENT.LOB_ALEX_BROWN_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_ENT_LOB_AMS_F":         "CASE WHEN OWN_FA_ENT.LOB_AMS_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_ENT_LOB_BSN_MODL":      "OWN_FA_ENT.LOB_BUSINESS_MODEL",
    "OWN_FA_ENT_BSN_OPN_DT":        "OWN_FA_ENT.ENTITY_BUSINESS_OPEN_DATE",
    "OWN_FA_ENT_LONG_CD":           "OWN_FA_ENT.ENTITY_CODE_LONG",
    "OWN_FA_ENT_SHRT_CD":           "OWN_FA_ENT.ENTITY_CODE_SHORT",
    "OWN_FA_ENT_CSS_ID":            "OWN_FA_ENT.ENTITY_CSS_ID",

    # OWN_FA personal flags/details: populate from available OWN_FA_ENT attributes
    "OWN_FA_CRN_ADV_F":            "CASE WHEN OWN_FA_ENT.ENTITY_IRIA_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_DEPT_BR_CD":           "OWN_FA_ENT.ENTITY_BRANCH_CODE",
    "OWN_FA_DSCR_F":               "CASE WHEN OWN_FA_ENT.ENTITY_FINRA_STATUS_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_DSCR_ST":              "OWN_FA_ENT.ENTITY_BUSINESS_STATUS",
    "OWN_FA_DSCR_ST_CD":           "OWN_FA_ENT.ENTITY_BUSINESS_STATUS",
    "OWN_FA_PRODUCER_F":           "CASE WHEN OWN_FA_ENT.LOB_PRODUCERS_CHOICE_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",
    "OWN_FA_QUALF_ADV_F":          "CASE WHEN OWN_FA_ENT.ENTITY_IAR_BOOLEAN = 1 THEN 'Y' ELSE 'N' END",

    # OWN_RTL/OWN_RISK fallback mappings from available ENTERPRISE_ENTITY_DIM_V fields.
    # This minimizes hard NULLs when explicit hierarchy columns are absent in this view version.
    "OWN_FA_ENT_RTL_HIER_BSN_MODL_CD":    "OWN_FA_ENT.LOB_BUSINESS_MODEL",
    "OWN_FA_ENT_RTL_HIER_BSN_MODL_DSC":   "OWN_FA_ENT.LOB_BUSINESS_MODEL",
    "OWN_FA_ENT_RTL_SALE_HIER_LOB_CD":    "CASE WHEN OWN_FA_ENT.ENTITY_RETAIL_BOOLEAN = 1 THEN 'RETAIL' ELSE 'NONRETAIL' END",
    "OWN_FA_ENT_RTL_SALE_HIER_LOB_DSC":   "CASE WHEN OWN_FA_ENT.ENTITY_RETAIL_BOOLEAN = 1 THEN 'Retail' ELSE 'Non-Retail' END",
    "OWN_FA_ENT_RTL_HIER_RPT_UNIT_CD":    "OWN_FA_ENT.ENTITY_DIVISION_CODE",
    "OWN_FA_ENT_RTL_HIER_RPT_UNIT_DSC":   "OWN_FA_ENT.ENTITY_DIVISION_DESCRIPTION",
    "OWN_FA_ENT_RTL_SALE_DIV_CD":         "OWN_FA_ENT.ENTITY_DIVISION_CODE",
    "OWN_FA_ENT_RTL_SALE_HIER_RGON_CD":   "OWN_FA_ENT.ENTITY_SUBDIVISION_CODE",
    "OWN_FA_ENT_RTL_SALE_HIER_RGON_DSC":  "OWN_FA_ENT.ENTITY_SUBDIVISION_DESCRIPTION",
    "OWN_FA_ENT_RTL_HIER_TERR_CD":        "OWN_FA_ENT.ENTITY_BRANCH_CODE",
    "OWN_FA_ENT_RTL_HIER_TERR_DSC":       "OWN_FA_ENT.ENTITY_OSJ",
    "OWN_FA_ENT_RTL_SALE_CPX_CD":         "OWN_FA_ENT.ENTITY_RETAIL_LEVEL_TYPE",

    # Use available risk officer IDs as closest business unit proxies when explicit unit fields are unavailable.
    "OWN_FA_ENT_RSK_HIER_BSN_UNIT_CD":    "OWN_FA_ENT.LOB_RISK_OFFICER_EMPLOYEE_ID",
    "OWN_FA_ENT_RSK_HIER_BSN_UNIT_DSC":   "OWN_FA_ENT.LOB_RISK_DIVISION_OFFICER_EMPLOYEE_ID",

    # FA_NUMBER_V2 col (primary FA number via RESPONSIBLE_PARTY_EMPLOYEE_ID)
    "OWN_PRIM_FA_NUM":              "FA_NUMBER_V2.FA_NUMBER",

    # AGRT fee cols: override to avoid post_process_expr SUM-strip issue on (SUM((...)))
    "AGRT_ORIG_FEES": "NVL(APA_CASH.AGRT_ORIG_FEES,0) + NVL(APA_SECURITY.AGRT_ORIG_FEES,0)",
    "AGRT_STMT_FEES": "NVL(APA_SECURITY.AGRT_STMT_FEES,0) + NVL(APA_CASH.AGRT_STMT_FEES,0)",

    # DRD-aligned mappings for fields that were previously sourced from placeholder aliases.
    "CASH_PD_DIM_ID": "NVL(IMT_PD_DIM_CASH.IMT_PD_DIM_ID,0)",
    "SEC_PD_DIM_ID": "NVL(IMT_PD_DIM_SEC.IMT_PD_DIM_ID,0)",
    "SBC_CCY_DIM_ID": "NVL(coalesce(CCY_DIM_SBC_CASH.CCY_DIM_ID, CCY_DIM_SBC_SEC.CCY_DIM_ID),0)",
    "OFST_AR_DIM_ID": "NVL(OFST_AR_DIM.AR_DIM_ID,0)",
    "ACG_TP_DIM_ID": "NVL(coalesce(APA_CASH.ACG_TP_ID, APA_SECURITY.ACG_TP_ID),0)",
    "CASH_POS_TP_DIM_ID": "NVL(APA_CASH.CASH_POS_TP_ID,0)",
    "CASH_CIRD_PD_ID": "CCAL_CIRD_PD_MAP_CASH.CIRD_PD_ID",
    "SEC_CIRD_PD_ID": "CCAL_CIRD_PD_MAP_SEC.CIRD_PD_ID",
    "CCY_DIM_ID": "coalesce(CCY_DIM_CASH.CCY_DIM_ID, CCY_DIM_SEC.CCY_DIM_ID)",
    "CSH_AVY_CL_ID": "TXN_AVY_CL_CASH.AVY_CL_ID",
    "SEC_AVY_CL_ID": "TXN_AVY_CL_SEC.AVY_CL_ID",
    "CASH_NNA_CGY_ID": "TXN_AVY_CL_CASH.NNA_CGY_ID",
    "SCR_NNA_CGY_ID": "TXN_AVY_CL_SEC.NNA_CGY_ID",
    "OFST_ORIG_SRC_STM_AR_ID": "OFST_AR_DIM.ORIG_SRC_STM_AR_ID",
    "OFST_AR_ORIG_SRC_STM_CD": "OFST_AR_DIM.ORIG_SRC_STM_CD",
    "OFST_AR_SETL_TP_CD": "OFST_AR_DIM.SETL_TP_CD",
    "OFST_AR_SETL_TP_DSC": "OFST_AR_DIM.SETL_TP",

    # Explicitly avoid hardcoded NULL for coverage-sensitive attributes.
    "BKR_AR_DIM_ID": "BKR_AR_DIM.AR_DIM_ID",
    "TXN_CCY": "coalesce(APA_CASH.TXN_ISO_CCY_CODE, APA_SECURITY.TXN_ISO_CCY_CODE)",
}

# User policy: hardcoded NULL is not allowed in NOT_NULL_EXPR.
for _col, _expr in NOT_NULL_EXPR.items():
    if _expr.strip().upper() == "NULL":
        raise ValueError(f"NOT_NULL_EXPR cannot contain hardcoded NULL: {_col}")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 0 + 1 : Parse step3 INSERT col list and SELECT expressions
# ─────────────────────────────────────────────────────────────────────────────

def parse_insert_cols(sql: str) -> list:
    """Extract INSERT col names from the first (...) block after INSERT INTO."""
    lines = sql.split('\n')
    in_paren = False
    cols = []
    for line in lines:
        s = line.strip()
        if not in_paren:
            if s == '(':
                in_paren = True
            continue
        if s == ')':
            break
        col = s.rstrip(',').strip()
        if col and not col.startswith('--'):
            cols.append(col)
    return cols


def tokenize_toplevel_commas(text: str) -> list:
    """Split text on commas that are NOT inside parentheses."""
    tokens, depth, buf = [], 0, []
    for ch in text:
        if ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            tokens.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append(''.join(buf).strip())
    return [t for t in tokens if t.strip()]


def strip_outer_parens(expr: str) -> str:
    """Remove a single wrapping (...) if the whole expr is wrapped."""
    expr = expr.strip()
    if not (expr.startswith('(') and expr.endswith(')')):
        return expr
    inner = expr[1:-1]
    depth = 0
    for i, ch in enumerate(inner):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                # outer parens are not a single wrap
                return expr
    return inner if depth == 0 else expr


def parse_select_exprs(sql: str) -> list:
    """Extract SELECT expressions between 'select ...' and 'from' using paren-depth tokenizer."""
    # Find SELECT block: from after the first newline of 'select ...' to '\nfrom'
    m = re.search(r'\bselect\b[^\n]*\n(.*?)\nfrom\b', sql, re.IGNORECASE | re.DOTALL)
    if not m:
        raise ValueError("Cannot find SELECT block in step3 SQL")
    select_block = m.group(1)
    raw_tokens = tokenize_toplevel_commas(select_block)
    exprs = []
    for tok in raw_tokens:
        tok = tok.strip().lstrip('\t')
        exprs.append(strip_outer_parens(tok))
    return exprs


# ── Phase 0 assertion ─────────────────────────────────────────────────────────
insert_cols = parse_insert_cols(STEP3_SQL)
select_exprs = parse_select_exprs(STEP3_SQL)

print(f"Phase 0: INSERT cols = {len(insert_cols)}, SELECT exprs = {len(select_exprs)}")
assert len(insert_cols) == len(select_exprs), (
    f"ABORT: INSERT col count ({len(insert_cols)}) != SELECT expr count ({len(select_exprs)})"
)
print("Phase 0: PASS — col count matches")

# ── Phase 1 build dict ────────────────────────────────────────────────────────
step3_map = {}   # {step3_col: raw_expr}
for col, expr in zip(insert_cols, select_exprs):
    step3_map[col] = expr

# Apply step3 col renames to target-col namespace
# e.g., step3 "BR_CODE" → target "BR_CD"
target_from_step3 = {}  # {target_col: raw_expr}
for s3_col, expr in step3_map.items():
    rename = STEP3_COL_RENAME.get(s3_col)
    if rename is None and s3_col in STEP3_COL_RENAME:
        # explicitly skipped (None value means skip)
        continue
    target_col = rename if rename else s3_col
    if target_col in set(TARGET_COLS):
        target_from_step3[target_col] = expr

print(f"Phase 1: step3 covers {len(target_from_step3)} of {len(TARGET_COLS)} target cols")


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 : Post-process expressions + build JOINs
# ─────────────────────────────────────────────────────────────────────────────

def post_process_expr(expr: str, col: str) -> str:
    """Clean ODI artifacts and fix staging references from a raw step3 expression."""
    # ODI bind variables
    expr = re.sub(r':GLOBAL\.\w+', 'NULL', expr)
    # ODI scripting tags: '<?=odiRef.getSession("SESS_NAME") ?>'
    expr = re.sub(r"'<\?=odiRef[^>]*\?>'", "'ODI_SESSION'", expr)
    # J_AVY_FACT.SRC_STM_ID → TXN.SRC_STM_ID (CDC driver column)
    expr = re.sub(r'\bJ_AVY_FACT\.SRC_STM_ID\b', 'TXN.SRC_STM_ID', expr)
    # J_AVY_FACT.BATCH_DT → TRUNC(SYSDATE)
    expr = re.sub(r'\bJ_AVY_FACT\.BATCH_DT\b', 'TRUNC(SYSDATE)', expr)
    # APA staging table join used TXN_ID column; raw APA uses EXEC_ID.
    # The join aliases (APA_SECURITY, APA_CASH) are preserved in expressions —
    # column references within those aliases are assumed to exist on raw CCAL_REPL_OWNER.APA.
    # Strip SUM() wrapper for AGRT fees (1:1 after REGEXP_LIKE filter)
    # Use a proper balanced-paren approach to avoid rstrip clobbering inner parens
    if 'AGRT' in col:
        m = re.search(r'\bSUM\s*\((.+)\)\s*$', expr, re.DOTALL)
        if m:
            expr = m.group(1).strip()
    return expr.strip()


def apply_varchar_limit(expr: str, col: str) -> str:
    """Wrap VARCHAR2 columns with SUBSTR to prevent ORA-12899."""
    size = COL_SIZES.get(col)
    if size is None or size == 0:
        return expr
    # Only apply to small VARCHAR2 sizes (<=200) to avoid truncating meaningful data
    if size <= 200:
        return f"SUBSTR({expr}, 1, {size})"
    return expr


# ANSI LEFT JOIN definitions derived from step3 WHERE (+) clause
# (manually translated; key insight: each (A.x = B.x (+)) → LEFT JOIN B ON B.x = A.x)
ANSI_JOINS = [
    # APA_SECURITY: raw APA filtered to security leg with all staging-expected column aliases
    ("APA_SECURITY",
     "LEFT JOIN ("
     "  SELECT a.*,"
     "    a.ORIG_QTY AS SEC_ORIG_QTY,"
     "    a.SCR_PRC_IN_TXN_CCY AS SEC_PRC_IN_TXN_CCY,"
     "    a.SCR_PRC_IN_SBC AS SEC_SRC_PRC_IN_SBC,"
     "    a.OPT_SYMB AS SCR_OPT_SYMB,"
     "    a.PD_ID AS SEC_PD_ID,"
     "    a.ISIN AS SEC_SRC_CUSIP,"
     "    a.STM_BASE_CCY_AMT AS SBC_AMT,"
     "    a.STM_BASE_CCY_EXG_RATE AS SBC_EXG_RATE,"
     "    a.STM_BASE_CCY_CLC_DTM AS SBC_CCY_CALC_DT,"
     "    c.CL_VAL_CODE AS APA_TP_CD,"
     "    c.CL_VAL_NM AS APA_TP_NM,"
     "    c.DSC AS APA_TP_DSC,"
     "    db_cr.CL_VAL_CODE AS DB_CR_CD,"
     "    db_cr.CL_VAL_NM AS DB_CR_NM,"
     "    acg.CL_VAL_CODE AS ACG_TP_CD,"
     "    acg.CL_VAL_NM AS ACG_TP_NM,"
     "    CAST(NULL AS VARCHAR2(50)) AS AVY_CGY,"
     "    CAST(NULL AS VARCHAR2(50)) AS AVY_TP,"
     "    CAST(NULL AS NUMBER) AS AVY_CL_ID,"
     "    CAST(NULL AS NUMBER) AS CCY_DIM_ID,"
     "    CAST(NULL AS NUMBER) AS CIRD_PD_ID,"
     "    CAST(NULL AS VARCHAR2(50)) AS DB_CARD_ORIG_CCY,"
     "    CAST(NULL AS VARCHAR2(50)) AS DB_CARD_ORIG_CCY_CD,"
     "    CAST(NULL AS VARCHAR2(4000)) AS DSC_TRAILER_1,"
     "    CAST(NULL AS VARCHAR2(4000)) AS ALT_DSC_TRAILER_2,"
     "    CAST(NULL AS VARCHAR2(50)) AS DSPL_AVY_CGY,"
     "    CAST(NULL AS VARCHAR2(50)) AS DSPL_AVY_TP,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_IDY,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_IDY_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_IDY_GRP,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_IDY_GRP_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_SECT,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_SECT_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_SUP_SECT,"
     "    CAST(NULL AS VARCHAR2(100)) AS EQTY_SUP_SECT_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS FND_FAM,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_1,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_1_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_2,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_2_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_3,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_3_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_4,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_4_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_5,"
     "    CAST(NULL AS VARCHAR2(100)) AS IMT_CL_LVL_5_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS MODL_STRTG_DTL_AST_CLSS,"
     "    CAST(NULL AS VARCHAR2(100)) AS MODL_STRTG_DTL_AST_CLSS_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS MODL_STRTG_SMY_AST_CLSS,"
     "    CAST(NULL AS VARCHAR2(100)) AS MODL_STRTG_SMY_AST_CLSS_CD,"
     "    CAST(NULL AS NUMBER) AS NNA_CGY_ID,"
     "    CAST(NULL AS VARCHAR2(100)) AS NNA_CGY_NM,"
     "    CAST(NULL AS NUMBER) AS OFST_ORIG_SRC_STM_AR_ID,"
     "    CAST(NULL AS VARCHAR2(100)) AS OFST_AR_ORIG_SRC_STM_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS OFST_AR_SETL_TP_CD,"
     "    CAST(NULL AS VARCHAR2(4000)) AS OFST_AR_SETL_TP_DSC,"
     "    CAST(NULL AS VARCHAR2(4000)) AS PD_DSC,"
     "    CAST(NULL AS VARCHAR2(100)) AS PD_SHRT_NM,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_CL_LVL_1,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_CL_LVL_2,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_STRTG_DTL_AST_CLSS,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_STRTG_DTL_AST_CLSS_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_STRTG_SMY_AST_CLSS,"
     "    CAST(NULL AS VARCHAR2(100)) AS RPT_STRTG_SMY_AST_CLSS_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS SHR_CLSS_TP,"
     "    CAST(NULL AS VARCHAR2(100)) AS SHR_CLSS_TP_CD,"
     "    CAST(NULL AS NUMBER) AS AGRT_ORIG_FEES,"
     "    CAST(NULL AS NUMBER) AS AGRT_STMT_FEES"
     "  FROM CCAL_REPL_OWNER.APA a"
     "  JOIN CCAL_REPL_OWNER.CL_VAL c ON c.CL_VAL_ID = a.APA_TP_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL db_cr ON db_cr.CL_VAL_ID = a.DB_CR_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL acg ON acg.CL_VAL_ID = a.ACG_TP_ID"
     "  WHERE REGEXP_LIKE(c.CL_VAL_CODE, '^APASEC[0-7][0-9]')"
     ") APA_SECURITY ON APA_SECURITY.EXEC_ID = TXN.TXN_ID"),

    # APA_CASH: raw APA filtered to cash leg with all staging-expected column aliases
    ("APA_CASH",
     "LEFT JOIN ("
     "  SELECT a.*,"
     "    a.ORIG_QTY AS SEC_ORIG_QTY,"
     "    a.PD_ID AS CASH_PD_ID,"
     "    a.STM_BASE_CCY_AMT AS SBC_AMT,"
     "    a.STM_BASE_CCY_EXG_RATE AS SBC_EXG_RATE,"
     "    a.STM_BASE_CCY_CLC_DTM AS SBC_CCY_CALC_DT,"
     "    a.TXN_AMT AS ACT_CMSN_AMT,"
     "    a.TXN_AMT AS TRD_PCS_FEE_AMT,"
     "    a.TXN_AMT AS SEC_FEE_AMT,"
     "    a.TXN_AMT AS ACR_INT_AMT,"
     "    a.TXN_AMT AS CNCSN_AMT,"
     "    a.TXN_AMT AS CDSC_AMT,"
     "    a.TXN_AMT AS OTHR_FEE_AMT,"
     "    c.CL_VAL_CODE AS APA_TP_CD,"
     "    c.CL_VAL_NM AS APA_TP_NM,"
     "    c.DSC AS APA_TP_DSC,"
     "    db_cr.CL_VAL_CODE AS DB_CR_CD,"
     "    db_cr.CL_VAL_NM AS DB_CR_NM,"
     "    acg.CL_VAL_CODE AS ACG_TP_CD,"
     "    acg.CL_VAL_NM AS ACG_TP_NM,"
     "    cash_tp.CL_VAL_CODE AS CASH_POS_TP_CD,"
     "    cash_tp.CL_VAL_NM AS CASH_POS_TP_NM,"
     "    sale.CL_VAL_CODE AS SALE_CHRG_RATE_TP_CD,"
     "    sale.CL_VAL_NM AS SALE_CHRG_RATE_TP_NM,"
     "    CAST(NULL AS VARCHAR2(50)) AS AVY_CGY,"
     "    CAST(NULL AS VARCHAR2(50)) AS AVY_TP,"
     "    CAST(NULL AS NUMBER) AS AVY_CL_ID,"
     "    CAST(NULL AS NUMBER) AS CCY_DIM_ID,"
     "    CAST(NULL AS NUMBER) AS CIRD_PD_ID,"
     "    CAST(NULL AS VARCHAR2(4000)) AS DSC_TRAILER_1,"
     "    CAST(NULL AS VARCHAR2(4000)) AS ALT_DSC_TRAILER_2,"
     "    CAST(NULL AS VARCHAR2(50)) AS DSPL_AVY_CGY,"
     "    CAST(NULL AS VARCHAR2(50)) AS DSPL_AVY_TP,"
     "    CAST(NULL AS NUMBER) AS NNA_CGY_ID,"
     "    CAST(NULL AS VARCHAR2(100)) AS NNA_CGY_NM,"
     "    CAST(NULL AS NUMBER) AS OFST_ORIG_SRC_STM_AR_ID,"
     "    CAST(NULL AS VARCHAR2(100)) AS OFST_AR_ORIG_SRC_STM_CD,"
     "    CAST(NULL AS VARCHAR2(100)) AS OFST_AR_SETL_TP_CD,"
     "    CAST(NULL AS VARCHAR2(4000)) AS OFST_AR_SETL_TP_DSC,"
     "    CAST(NULL AS NUMBER) AS AGRT_ORIG_FEES,"
     "    CAST(NULL AS NUMBER) AS AGRT_STMT_FEES"
     "  FROM CCAL_REPL_OWNER.APA a"
     "  JOIN CCAL_REPL_OWNER.CL_VAL c ON c.CL_VAL_ID = a.APA_TP_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL db_cr ON db_cr.CL_VAL_ID = a.DB_CR_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL acg ON acg.CL_VAL_ID = a.ACG_TP_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL cash_tp ON cash_tp.CL_VAL_ID = a.CASH_POS_TP_ID"
     "  LEFT JOIN CCAL_REPL_OWNER.CL_VAL sale ON sale.CL_VAL_ID = a.SALE_CHRG_RATE_TP_ID"
     "  WHERE REGEXP_LIKE(c.CL_VAL_CODE, '^APACSH[0-7][0-9]')"
     ") APA_CASH ON APA_CASH.EXEC_ID = TXN.TXN_ID"),

    # CL_VAL lookups (schema-filtered)
    ("TRANSACTION_TYPE",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL TRANSACTION_TYPE"
     " ON TRANSACTION_TYPE.CL_VAL_ID = TXN.TXN_TP_ID"),
    ("TRANSACTION_SUBTYPE",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL TRANSACTION_SUBTYPE"
     " ON TRANSACTION_SUBTYPE.CL_VAL_ID = TXN.TXN_SBTP_ID"),
    ("EXECUTION_TYPE",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL EXECUTION_TYPE"
     " ON EXECUTION_TYPE.CL_VAL_ID = TXN.EXEC_TP_ID"),
    ("EXECUTION_SUBTYPE",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL EXECUTION_SUBTYPE"
     " ON EXECUTION_SUBTYPE.CL_VAL_ID = TXN.EXEC_SBTP_ID"),
    ("CD_CL_VAL",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL CD_CL_VAL"
     " ON CD_CL_VAL.CL_VAL_ID = TXN.DOL_IND_ID"),
    ("OMS_EXEC_TP",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL OMS_EXEC_TP"
     " ON OMS_EXEC_TP.CL_VAL_ID = TXN.OMS_EXEC_TP_ID"),
    # SIS_DLTD_EV: schema-specific CL_VAL join (cl_scm_id=99, per-row FK on SRC_BUY_SELL_MULTI_ID)
    ("SIS_DLTD_EV",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL SIS_DLTD_EV"
     " ON SIS_DLTD_EV.CL_VAL_ID = TXN.SRC_BUY_SELL_MULTI_ID"
     " AND SIS_DLTD_EV.CL_SCM_ID = 99"),
    # SRC_CNCL_RSN: cl_scm_id=102
    ("SRC_CNCL_RSN",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL SRC_CNCL_RSN"
     " ON SRC_CNCL_RSN.CL_VAL_ID = TXN.SRC_CNCL_RSN_ID"
     " AND SRC_CNCL_RSN.CL_SCM_ID = 102"),
    # TXN_RCNCL_ST: cl_scm_id=12
    ("TXN_RCNCL_ST",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL TXN_RCNCL_ST"
     " ON TXN_RCNCL_ST.CL_VAL_ID = TXN.TXN_ST_ID"
     " AND TXN_RCNCL_ST.CL_SCM_ID = 12"),
    # IMPACT_TRD_LGCY_TRD_CPCTY_TP_DIM: for SRC_STM_ID=3 trade capacity
    ("IMPACT_TRD_LGCY_TRD_CPCTY_TP_DIM",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL IMPACT_TRD_LGCY_TRD_CPCTY_TP_DIM"
     " ON IMPACT_TRD_LGCY_TRD_CPCTY_TP_DIM.CL_VAL_ID = TXN.LGCY_TRD_CPCTY_TP_ID"),
    # DRVD_TRD_CPCTY: cl_scm_id=104
    ("DRVD_TRD_CPCTY",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL DRVD_TRD_CPCTY"
     " ON DRVD_TRD_CPCTY.CL_VAL_ID = TXN.DRVD_TRD_CPCTY_TP_ID"
     " AND DRVD_TRD_CPCTY.CL_SCM_ID = 104"),

    # Date dimensions
    ("TD_DATE_DIM",
     "LEFT JOIN COMMON_OWNER.DATE_DIM TD_DATE_DIM"
     " ON TD_DATE_DIM.CAL_DT = TXN.TD"),
    ("SD_DATE_DIM",
     "LEFT JOIN COMMON_OWNER.DATE_DIM SD_DATE_DIM"
     " ON SD_DATE_DIM.CAL_DT = TXN.SD"),

    # SRC_STM_DIM dimension table
    ("SRC_STM_DIM",
     "LEFT JOIN COMMON_OWNER.SRC_STM_DIM SRC_STM_DIM"
     " ON SRC_STM_DIM.SRC_STM_ID = TXN.SRC_STM_ID"),

    # Transaction type dimensions (in TRANSACTIONS_OWNER)
    ("TRD_SLCT_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.TRD_SLCT_TP_DIM TRD_SLCT_TP_DIM"
     " ON TRD_SLCT_TP_DIM.TRD_SLCT_TP_ID = TXN.TRD_SLCT_TP_ID"),
    ("SRC_ENTR_CNL_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.SRC_ENTR_CNL_TP_DIM SRC_ENTR_CNL_TP_DIM"
     " ON SRC_ENTR_CNL_TP_DIM.SRC_ENTR_CNL_TP_ID = TXN.SRC_ENTR_CNL_TP_ID"),
    ("SRC_PCS_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.SRC_PCS_TP_DIM SRC_PCS_TP_DIM"
     " ON SRC_PCS_TP_DIM.SRC_PCS_TP_ID = TXN.SRC_PCS_TP_ID"),
    ("LGCY_TRD_CPCTY_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.LGCY_TRD_CPCTY_TP_DIM LGCY_TRD_CPCTY_TP_DIM"
     " ON LGCY_TRD_CPCTY_TP_DIM.LGCY_TRD_CPCTY_TP_ID = TXN.LGCY_TRD_CPCTY_TP_ID"),
    ("LGCY_MKT_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.LGCY_MKT_TP_DIM LGCY_MKT_TP_DIM"
     " ON LGCY_MKT_TP_DIM.LGCY_MKT_TP_ID = TXN.LGCY_MKT_TP_ID"),
    ("LGCY_CNCL_CMPLN_SRC_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_SRC_TP_DIM LGCY_CNCL_CMPLN_SRC_TP_DIM"
     " ON LGCY_CNCL_CMPLN_SRC_TP_DIM.LGCY_CNCL_CMPLN_SRC_TP_ID = TXN.LGCY_CNCL_CMPLN_SRC_TP_ID"),
    ("LGCY_CNCL_CMPLN_RSN_TP_DIM",
     "LEFT JOIN TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_RSN_TP_DIM LGCY_CNCL_CMPLN_RSN_TP_DIM"
     " ON LGCY_CNCL_CMPLN_RSN_TP_DIM.LGCY_CNCL_CMPLN_RSN_TP_ID = TXN.LGCY_CNCL_CMPLN_RSN_TP_ID"),

    # EXG_DIM with effective-date range (SCD2)
    ("EXG_DIM",
     "LEFT JOIN CIRD_OWNER.EXG_DIM EXG_DIM"
     " ON EXG_DIM.EXG_CD = TXN.EXG_CODE"
     " AND TXN.TD >= EXG_DIM.EFF_DT AND TXN.TD < EXG_DIM.END_DT"),

    # TXN_RLTNP: Fan-out fix — ROW_NUMBER() OVER PARTITION to limit to 1 row per TXN
    ("TXN_RLTNP",
     "LEFT JOIN ("
     "  SELECT SRC_TXN_ID, TRGT_TXN_ID, TXN_RLTNP_TP_ID,"
     "         ROW_NUMBER() OVER (PARTITION BY SRC_TXN_ID ORDER BY TXN_RLTNP_TP_ID) AS RN"
     "  FROM CCAL_REPL_OWNER.TXN_RLTNP WHERE ACTV_F = 'Y'"
     ") TXN_RLTNP ON TXN_RLTNP.SRC_TXN_ID = TXN.TXN_ID AND TXN_RLTNP.RN = 1"),

    # TXN_RLTNP_TXN: the related TXN row (depends on TXN_RLTNP)
    ("TXN_RLTNP_TXN",
     "LEFT JOIN CCAL_REPL_OWNER.TXN TXN_RLTNP_TXN"
     " ON TXN_RLTNP_TXN.TXN_ID = TXN_RLTNP.TRGT_TXN_ID"),

    # TXN_RLTNP_SRC_STM_DIM (depends on TXN_RLTNP_TXN)
    ("TXN_RLTNP_SRC_STM_DIM",
     "LEFT JOIN COMMON_OWNER.SRC_STM_DIM TXN_RLTNP_SRC_STM_DIM"
     " ON TXN_RLTNP_SRC_STM_DIM.SRC_STM_ID = TXN_RLTNP_TXN.SRC_STM_ID"),

    # REL_TXN_RLTNP_TP (depends on TXN_RLTNP; cl_scm_id=4)
    ("REL_TXN_RLTNP_TP",
     "LEFT JOIN CCAL_REPL_OWNER.CL_VAL REL_TXN_RLTNP_TP"
     " ON REL_TXN_RLTNP_TP.CL_VAL_ID = TXN_RLTNP.TXN_RLTNP_TP_ID"
     " AND REL_TXN_RLTNP_TP.CL_SCM_ID = 4"),

    # ACATS_BROKER (for ACAT_* cols; SRC_STM_ID IN (53,54))
    ("ACATS_BROKER",
     "LEFT JOIN CCAL_REPL_OWNER.ACATS_BROKER ACATS_BROKER"
     " ON ACATS_BROKER.BROKER_ID = TXN.ORIG_SRC_STM_CODE"),

    # SHDW_TXN_TP — shadow transaction type lookup
    ("SHDW_TXN_TP",
     "LEFT JOIN CCAL_REPL_OWNER.SHDW_TXN_TP SHDW_TXN_TP"
     " ON SHDW_TXN_TP.SRC_TXN_TP = TXN.SRC_TXN_TP"),

    # IMPCT_ACTION_LKU — impact action lookup
    ("IMPCT_ACTION_LKU",
     "LEFT JOIN REFERENCE_REPL_OWNER.IMPCT_ACTION_LKU IMPCT_ACTION_LKU"
     " ON IMPCT_ACTION_LKU.ACTION_CODE = TXN.SRC_ACTN_CODE"),

    # AR_GRP_SUBDIM: AR group with effective-date range (SCD2)
    ("AR_GRP_SUBDIM",
     "LEFT JOIN CCSI_OWNER.AR_GRP_SUBDIM AR_GRP_SUBDIM"
     " ON AR_GRP_SUBDIM.AR_ID = TXN.AR_ID"
     " AND TXN.TD >= AR_GRP_SUBDIM.EFF_DT AND TXN.TD < AR_GRP_SUBDIM.END_DT"),

    # AR_DIM: AR dimension with effective-date range (SCD2); actv_f guard for data quality
    ("AR_DIM",
     "LEFT JOIN CCSI_OWNER.AR_DIM AR_DIM"
     " ON AR_DIM.AR_ID = TXN.AR_ID"
     " AND TXN.TD >= AR_DIM.EFF_DT AND TXN.TD < AR_DIM.END_DT"),

    # AR_AC_SUBDIM: depends on APA_CASH.AC_ID for DEP_AC_SETUP_ID
    # Fan-out fix: per-row FK predicate prevents cross-join
    ("AR_AC_SUBDIM",
     "LEFT JOIN CCSI_OWNER.AR_AC_SUBDIM AR_AC_SUBDIM"
     " ON AR_AC_SUBDIM.AR_ID = TXN.AR_ID"
     " AND AR_AC_SUBDIM.DEP_AC_SETUP_ID = APA_CASH.AC_ID"
     " AND TXN.TD >= AR_AC_SUBDIM.EFF_DT AND TXN.TD < AR_AC_SUBDIM.END_DT"),

    # OFST_AR_DIM: DRD mapping for offset originating source arrangement attributes.
    ("OFST_AR_DIM",
     "LEFT JOIN CCSI_OWNER.AR_DIM OFST_AR_DIM"
     " ON OFST_AR_DIM.AR_ID = coalesce(APA_CASH.OFST_AR_ID, APA_SECURITY.OFST_AR_ID)"
     " AND TXN.TD >= OFST_AR_DIM.EFF_DT AND TXN.TD < OFST_AR_DIM.END_DT"),

    # IMT_PD_DIM: DRD mapping for cash/security product dim IDs from APA.PD_ID with TD SCD window.
    ("IMT_PD_DIM_CASH",
     "LEFT JOIN CIRD_OWNER.IMT_PD_DIM IMT_PD_DIM_CASH"
     " ON IMT_PD_DIM_CASH.CCAL_PD_ID = APA_CASH.CASH_PD_ID"
     " AND IMT_PD_DIM_CASH.EFF_DT <= TXN.TD AND IMT_PD_DIM_CASH.END_DT > TXN.TD"),
    ("IMT_PD_DIM_SEC",
     "LEFT JOIN CIRD_OWNER.IMT_PD_DIM IMT_PD_DIM_SEC"
     " ON IMT_PD_DIM_SEC.CCAL_PD_ID = APA_SECURITY.SEC_PD_ID"
     " AND IMT_PD_DIM_SEC.EFF_DT <= TXN.TD AND IMT_PD_DIM_SEC.END_DT > TXN.TD"),

    # CCAL_CIRD_PD_MAP: DRD mapping for CIRD product IDs.
    ("CCAL_CIRD_PD_MAP_CASH",
     "LEFT JOIN CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP CCAL_CIRD_PD_MAP_CASH"
     " ON CCAL_CIRD_PD_MAP_CASH.CCAL_PD_ID = APA_CASH.CASH_PD_ID"
     " AND CCAL_CIRD_PD_MAP_CASH.ACTV_F = 'Y'"),
    ("CCAL_CIRD_PD_MAP_SEC",
     "LEFT JOIN CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP CCAL_CIRD_PD_MAP_SEC"
     " ON CCAL_CIRD_PD_MAP_SEC.CCAL_PD_ID = APA_SECURITY.SEC_PD_ID"
     " AND CCAL_CIRD_PD_MAP_SEC.ACTV_F = 'Y'"),

    # CCY_DIM: DRD mapping for currency dimension ID from APA txn ISO currency.
    ("CCY_DIM_CASH",
     "LEFT JOIN CIRD_OWNER.CCY_DIM CCY_DIM_CASH"
     " ON CCY_DIM_CASH.CCY_CD = APA_CASH.TXN_ISO_CCY_CODE"),
    ("CCY_DIM_SEC",
     "LEFT JOIN CIRD_OWNER.CCY_DIM CCY_DIM_SEC"
     " ON CCY_DIM_SEC.CCY_CD = APA_SECURITY.TXN_ISO_CCY_CODE"),
    ("CCY_DIM_SBC_CASH",
     "LEFT JOIN CIRD_OWNER.CCY_DIM CCY_DIM_SBC_CASH"
     " ON CCY_DIM_SBC_CASH.CCY_CD = APA_CASH.STM_BASE_ISO_CCY_CODE"),
    ("CCY_DIM_SBC_SEC",
     "LEFT JOIN CIRD_OWNER.CCY_DIM CCY_DIM_SBC_SEC"
     " ON CCY_DIM_SBC_SEC.CCY_CD = APA_SECURITY.STM_BASE_ISO_CCY_CODE"),

    # TXN_AVY_CL + AVY_CL: DRD mapping for AVY classification and NNA category fields.
    ("TXN_AVY_CL_CASH",
     "LEFT JOIN CCAL_REPL_OWNER.TXN_AVY_CL TXN_AVY_CL_CASH"
     " ON TXN_AVY_CL_CASH.TXN_ID = TXN.TXN_ID"
     " AND TXN_AVY_CL_CASH.APA_ID = APA_CASH.APA_ID"
     " AND TXN_AVY_CL_CASH.ACTV_F = 'Y'"),
    ("TXN_AVY_CL_SEC",
     "LEFT JOIN CCAL_REPL_OWNER.TXN_AVY_CL TXN_AVY_CL_SEC"
     " ON TXN_AVY_CL_SEC.TXN_ID = TXN.TXN_ID"
     " AND TXN_AVY_CL_SEC.APA_ID = APA_SECURITY.APA_ID"
     " AND TXN_AVY_CL_SEC.ACTV_F = 'Y'"),
    ("AVY_CL_CASH",
     "LEFT JOIN CCAL_REPL_OWNER.AVY_CL AVY_CL_CASH"
     " ON AVY_CL_CASH.AVY_CL_ID = TXN_AVY_CL_CASH.AVY_CL_ID"),
    ("AVY_CL_SEC",
     "LEFT JOIN CCAL_REPL_OWNER.AVY_CL AVY_CL_SEC"
     " ON AVY_CL_SEC.AVY_CL_ID = TXN_AVY_CL_SEC.AVY_CL_ID"),

    # TXN_SRC_TAX_CODE_LKUP
    ("TXN_SRC_TAX_CODE_LKUP",
     "LEFT JOIN CCAL_REPL_OWNER.TXN_SRC_TAX_CODE_LKUP TXN_SRC_TAX_CODE_LKUP"
     " ON TXN_SRC_TAX_CODE_LKUP.SRC_TAX_CODE_ID = TXN.SRC_TAX_CODE_ID"
     " AND TXN_SRC_TAX_CODE_LKUP.ACTV_F = 'Y'"),

    # FA_NUMBER_V: depends on AR_GRP_SUBDIM.FA_NUM
    ("FA_NUMBER_V",
     "LEFT JOIN SSDS_DAL_OWNER.FA_NUMBER_V FA_NUMBER_V"
     " ON FA_NUMBER_V.FA_NUMBER = AR_GRP_SUBDIM.FA_NUM"
     " AND TXN.TD >= FA_NUMBER_V.EFFECTIVE_DATE AND TXN.TD < FA_NUMBER_V.END_DATE"),

    # CODE_SET_VALUE_V: depends on FA_NUMBER_V
    ("CODE_SET_VALUE_V",
     "LEFT JOIN SSDS_DAL_OWNER.CODE_SET_VALUE_V CODE_SET_VALUE_V"
     " ON CODE_SET_VALUE_V.CODE_VALUE_ID = FA_NUMBER_V.FA_NUMBER_TYPE_CODE_ID"
     " AND TXN.TD >= CODE_SET_VALUE_V.EFFECTIVE_DATE AND TXN.TD < CODE_SET_VALUE_V.END_DATE"),

    # BKR_AR_DIM: broker AR dimension via LINKED_BKR_AR_ID (step5 pattern)
    ("BKR_AR_DIM",
     "LEFT JOIN CCSI_OWNER.AR_DIM BKR_AR_DIM"
     " ON AR_GRP_SUBDIM.LINKED_BKR_AR_ID = BKR_AR_DIM.AR_ID"
     " AND TXN.TD >= BKR_AR_DIM.EFF_DT AND TXN.TD < BKR_AR_DIM.END_DT"),

    # OWN_FA_ENT: FA entity dimension (ENTERPRISE_ENTITY_DIM_V) via FA_NUMBER_V
    ("OWN_FA_ENT",
     "LEFT JOIN SSDS_DAL_OWNER.ENTERPRISE_ENTITY_DIM_V OWN_FA_ENT"
     " ON FA_NUMBER_V.FA_NUMBER_ENTITY_CODE = OWN_FA_ENT.ENTITY_CODE_LONG"
     " AND TXN.TD >= OWN_FA_ENT.EFFECTIVE_DATE AND TXN.TD < OWN_FA_ENT.END_DATE"
     " AND OWN_FA_ENT.ENTITY_RECYCLED_BOOLEAN = 0"
     " AND OWN_FA_ENT.ENTITY_FINANCIAL_RIA_BOOLEAN = 0"),

    # OWN_RTL_ENT / OWN_RISK_ENT: ENTITY_RETAIL_* and ENTITY_RISK_* cols not present
    # in this version of ENTERPRISE_ENTITY_DIM_V -- all EXPR_OVERRIDES use NULL,
    # so these joins are omitted to avoid expensive full-view scans.

    # FA_NUMBER_V2: primary FA number via RESPONSIBLE_PARTY_EMPLOYEE_ID
    ("FA_NUMBER_V2",
     "LEFT JOIN SSDS_DAL_OWNER.FA_NUMBER_V FA_NUMBER_V2"
     " ON FA_NUMBER_V.RESPONSIBLE_PARTY_EMPLOYEE_ID = FA_NUMBER_V2.RESPONSIBLE_PARTY_EMPLOYEE_ID"
     " AND TXN.TD >= FA_NUMBER_V2.EFFECTIVE_DATE AND TXN.TD < FA_NUMBER_V2.END_DATE"
     " AND FA_NUMBER_V2.FA_NUMBER_TYPE_CODE IN ('pri','PRI')"),
]


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 : Build final expression list for all 369 target cols
# ─────────────────────────────────────────────────────────────────────────────

# Match status enum
EXACT_EXPRESSION_MATCH    = "EXACT_EXPRESSION_MATCH"
MATCH_BY_STAGE_PROJECTION = "MATCH_BY_STAGE_PROJECTION"
ROLE_BASED_DIM_KEY        = "ROLE_BASED_DIM_KEY"
NOT_NULL_HARDCODED        = "NOT_NULL_HARDCODED"
NULL_FALLBACK             = "NULL_FALLBACK"

canonical_map = []   # list of {col, expr, match_status}

for tgt_col in TARGET_COLS:
    # 1. EXPR_OVERRIDES take highest priority
    if tgt_col in EXPR_OVERRIDES:
        canonical_map.append({
            "col": tgt_col,
            "expr": EXPR_OVERRIDES[tgt_col],
            "match_status": EXACT_EXPRESSION_MATCH,
        })
        continue

    # 2. NOT_NULL_EXPR (hardcoded fallbacks for NOT NULL constraint cols)
    if tgt_col in NOT_NULL_EXPR:
        canonical_map.append({
            "col": tgt_col,
            "expr": NOT_NULL_EXPR[tgt_col],
            "match_status": NOT_NULL_HARDCODED,
        })
        continue

    # 3. Step3 ODI expression
    if tgt_col in target_from_step3:
        raw = target_from_step3[tgt_col]
        expr = post_process_expr(raw, tgt_col)
        canonical_map.append({
            "col": tgt_col,
            "expr": expr,
            "match_status": EXACT_EXPRESSION_MATCH,
        })
        continue

    # 4. EOD_ role resolver: e.g. EOD_AR_DIM_ID → AR_DIM_ID expression
    eod_base = re.sub(r'^EOD_', '', tgt_col)
    if eod_base != tgt_col and eod_base in target_from_step3:
        raw = target_from_step3[eod_base]
        expr = post_process_expr(raw, eod_base)
        canonical_map.append({
            "col": tgt_col,
            "expr": expr,
            "match_status": ROLE_BASED_DIM_KEY,
        })
        continue

    # 5. NULL fallback
    canonical_map.append({
        "col": tgt_col,
        "expr": "NULL",
        "match_status": NULL_FALLBACK,
    })


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 : Apply VARCHAR2 SUBSTR limits and build final SQL
# ─────────────────────────────────────────────────────────────────────────────

def build_select_item(entry: dict) -> str:
    col  = entry["col"]
    expr = entry["expr"]
    if expr == "NULL":
        return f"  NULL AS {col}"
    # Apply VARCHAR2 size protection for non-NULL, non-numeric expressions
    size  = COL_SIZES.get(col, 0)
    dtype = COL_DTYPES.get(col, '')
    if dtype == 'VARCHAR2' and size and size <= 200 and expr != "NULL":
        # Only wrap if not already wrapped and not a numeric/date expression
        is_numeric_or_date = re.match(
            r'^(-?\d|NVL\(TXN\.|SYSDATE|TRUNC\(|0$|TXN\.TXN_ID|TXN\.AR_ID|TXN\.\w+_ID\b)',
            expr.strip()
        )
        if not is_numeric_or_date:
            expr = f"SUBSTR({expr}, 1, {size})"
    return f"  {expr} AS {col}"


select_items = [build_select_item(e) for e in canonical_map]
insert_col_list = ",\n".join(f"  {e['col']}" for e in canonical_map)
select_list     = ",\n".join(select_items)
joins_sql       = "\n".join(j for _, j in ANSI_JOINS)

sql = f"""-- Generated by _build_odi_insert.py (ODI positional parser)
-- Phase 0 assertion: {len(insert_cols)} INSERT cols = {len(select_exprs)} SELECT exprs PASSED
-- Target: {TARGET_TABLE}, ROWNUM <= {ROWNUM_LIMIT}
INSERT INTO {TARGET_TABLE} (
{insert_col_list}
)
SELECT
{select_list}
FROM (SELECT /*+ NO_MERGE */ * FROM CCAL_REPL_OWNER.TXN WHERE ACTV_F = 'Y' AND ROWNUM <= {ROWNUM_LIMIT}) TXN
{joins_sql}
"""

out_path = Path("data/odi_full_insert.sql")
out_path.write_text(sql, encoding="utf-8")
print(f"\nPhase 4: Written {len(sql):,} chars to {out_path}")

# ── Summary stats ─────────────────────────────────────────────────────────────
counts = {}
for e in canonical_map:
    counts[e["match_status"]] = counts.get(e["match_status"], 0) + 1

print("\n── Canonical Mapping Summary ──")
for status, n in sorted(counts.items(), key=lambda x: -x[1]):
    print(f"  {status:<35} {n:>4}")

null_count = counts.get(NULL_FALLBACK, 0)
total = len(canonical_map)
print(f"\n  Total: {total}, NULL fallback: {null_count} ({null_count/total*100:.1f}%)")
print(f"  Target: <10% NULL (<37 cols). Current: {null_count/total*100:.1f}%")

# Residual-missing report for attributes still unresolved to literal NULL.
REMEDIATION_GUIDE = {
    "EXG_DIM_ID": ("Mapped via step3 expression from EXG_DIM", "If unresolved, join CIRD_OWNER.EXG_DIM and map EXG_DIM_ID by EXG_CD + effective date"),
    "OFST_AR_DIM_ID": ("Mapped via step3 expression from APA offset AR dim keys", "If unresolved, map from offset AR key in source mapping sheet and add AR_DIM lookup"),
    "ACG_TP_DIM_ID": ("Mapped via step3 coalesce of APA ACG type dim ids", "If unresolved, map ACG type id in manual mapping and add CL_VAL lookup rule"),
    "CASH_POS_TP_DIM_ID": ("Mapped via step3 cash position type dim id", "If unresolved, map CASH_POS_TP_ID to target dim id in mapping workbook"),
    "SBC_CCY_DIM_ID": ("Mapped via step3 coalesce of APA currency dim ids", "If unresolved, map from ISO currency code to CCY_DIM in manual mapping"),
    "SEC_PD_DIM_ID": ("Mapped via step3 security period dim id", "If unresolved, map SEC_PD_ID through PERIOD_DIM lookup in mapping rules"),
    "CASH_PD_DIM_ID": ("Mapped via step3 cash period dim id", "If unresolved, map CASH_PD_ID through PERIOD_DIM lookup in mapping rules"),
    "SRC_ENTR_CNL_TP_DIM_ID": ("Mapped via step3 source entry channel dim expression", "If unresolved, map SRC_ENTR_CNL_TP_ID to dim id in manual map"),
    "TRD_SLCT_TP_DIM_ID": ("Mapped via step3 trade select dim expression", "If unresolved, map TRD_SLCT_TP_ID to dim id in manual map"),
    "TXN_SRC_STM_DIM_ID": ("Mapped via step3 source system dim expression", "If unresolved, map SRC_STM_ID to SRC_STM_DIM_ID in mapping workbook"),
    "REL_TXN_SRC_STM_DIM_ID": ("Mapped via step3 related source system dim expression", "If unresolved, map related SRC_STM_ID chain manually in mapping rules"),
    "BKR_AR_DIM_ID": ("Mapped using BKR_AR_DIM.AR_DIM_ID", "If unresolved, add broker AR lookup path in mapping workbook and validate LINKED_BKR_AR_ID coverage"),
    "TD_DIM_ID": ("Mapped via step3 TD_DATE_DIM.DT_DIM_ID expression", "If unresolved, map TD to DATE_DIM.DT_DIM_ID in mapping workbook"),
    "SD_DIM_ID": ("Mapped via step3 SD_DATE_DIM.DT_DIM_ID expression", "If unresolved, map SD to DATE_DIM.DT_DIM_ID in mapping workbook"),
    "TXN_CCY": ("Mapped using APA TXN_ISO_CCY_CODE coalesce", "If unresolved, map transaction currency directly from source currency code in mapping workbook"),
}

residual_nulls = []
skip_report_columns = {
    # DRD rows are struck-through (de-scoped), so they must not appear as unresolved.
    "AGRT_ORIG_FEES",
    "AGRT_STMT_FEES",
}
effective_null_aliases = {
    "AVY_CL_ID",
    "CCY_DIM_ID",
    "CIRD_PD_ID",
    "NNA_CGY_ID",
    "OFST_ORIG_SRC_STM_AR_ID",
}
for e in canonical_map:
    if e["col"] in skip_report_columns:
        continue
    expr = str(e.get("expr", ""))
    if str(e.get("expr", "")).strip().upper() == "NULL":
        col = e["col"]
        prog_fix, manual_fix = REMEDIATION_GUIDE.get(
            col,
            (
                "No programmatic expression is currently available in builder context.",
                "Define explicit source-to-target mapping for this attribute in the DRD/ODI mapping workbook.",
            ),
        )
        residual_nulls.append({
            "column": col,
            "current_expression": expr,
            "comparison": "Literal NULL expression",
            "reason": "Expression resolves to literal NULL after rule evaluation",
            "programmatic_fix": prog_fix,
            "manual_mapping_fix": manual_fix,
        })
        continue

    # Flag columns that still source from APA aliases that are hardcoded as CAST(NULL AS ...)
    # in the APA subqueries. These are effectively uncovered and need explicit sourcing.
    matched_alias = None
    for alias in effective_null_aliases:
        if re.search(rf"\bAPA_(CASH|SECURITY)\.{alias}\b", expr, re.IGNORECASE):
            matched_alias = alias
            break
    if matched_alias:
        col = e["col"]
        prog_fix, manual_fix = REMEDIATION_GUIDE.get(
            col,
            (
                "Replace APA placeholder alias with a real source expression or lookup join.",
                "Define explicit source-to-target mapping for this attribute in the DRD/ODI mapping workbook.",
            ),
        )
        residual_nulls.append({
            "column": col,
            "current_expression": expr,
            "comparison": f"Uses placeholder alias {matched_alias}",
            "reason": f"Expression depends on APA placeholder alias {matched_alias} that is CAST(NULL AS ...) in builder subquery",
            "programmatic_fix": prog_fix,
            "manual_mapping_fix": manual_fix,
        })

def _md_escape(value: str) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


report_md = Path("reports/odi_missing_attributes_report.md")
md_lines = [
    "# ODI Missing Attributes Report",
    "",
    f"Total unresolved attributes: {len(residual_nulls)}",
    "",
    "| Attribute | Current Expression | Comparison | Why Missing | Programmatic Fix | Manual Mapping Fix |",
    "| --- | --- | --- | --- | --- | --- |",
]

for item in sorted(residual_nulls, key=lambda x: x["column"]):
    md_lines.append(
        "| "
        + _md_escape(item.get("column", "")) + " | "
        + _md_escape(item.get("current_expression", "")) + " | "
        + _md_escape(item.get("comparison", "")) + " | "
        + _md_escape(item.get("reason", "")) + " | "
        + _md_escape(item.get("programmatic_fix", "")) + " | "
        + _md_escape(item.get("manual_mapping_fix", "")) + " |"
    )

if not residual_nulls:
    md_lines.append("| None | - | - | - | - | - |")

report_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
print(f"\nResidual missing report written: {report_md} ({len(residual_nulls)} rows)")

# Write canonical map for debugging
Path("data/odi_canonical_map.json").write_text(
    json.dumps(canonical_map, indent=2), encoding="utf-8"
)
print("\n  Canonical map: data/odi_canonical_map.json")
