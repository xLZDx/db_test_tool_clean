"""
Verify source table access and build executable INSERT for IKOROSTELEV.AVY_FACT_SIDE.
Queries LH datasource (id=3) to check each source table and its columns.
"""
import requests
import json
from pathlib import Path

API = "http://127.0.0.1:8550/api/datasources/3/query"

def query(sql, limit=100):
    resp = requests.post(API, json={"sql": sql, "row_limit": limit}, timeout=30)
    data = resp.json()
    if data.get("error"):
        return None, data["error"]
    return data.get("rows", []), None

# Tables to check (from DRD mappings)
tables = [
    "CCAL_REPL_OWNER.TXN",
    "CCAL_REPL_OWNER.APA",
    "CCAL_REPL_OWNER.SHDW_TXN_TP",
    "CCAL_REPL_OWNER.CL_VAL",
    "CCAL_REPL_OWNER.ACATS_BROKER",
    "CCAL_REPL_OWNER.TXN_RLTNP",
    "CCAL_REPL_OWNER.AVY_CL",
    "CCAL_REPL_OWNER.FIP",
    "CCAL_REPL_OWNER.TXN_AVY_CL",
    "CCAL_REPL_OWNER.NNA_CGY",
    "CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP",
    "CCAL_REPL_OWNER.TXN_SRC_TAX_CODE_LKUP",
    "CCSI_OWNER.AR_DIM",
    "CCSI_OWNER.AR_GRP_SUBDIM",
    "CCSI_OWNER.AR_AC_SUBDIM",
    "COMMON_OWNER.SRC_STM_DIM",
    "COMMON_OWNER.DATE_DIM",
    "COMMON_OWNER.ACG_TP_DIM",
    "COMMON_OWNER.CASH_POS_TP_DIM",
    "CIRD_OWNER.EXG_DIM",
    "CIRD_OWNER.CCY_DIM",
    "CIRD_OWNER.IMT_PD_DIM",
    "REFERENCE_REPL_OWNER.IMPCT_ACTION_LKU",
    "Reference_Repl_Owner.CCY",
    "SSDS_DAL_OWNER.FA_NUMBER_V",
    "SSDS_DAL_OWNER.ENTERPRISE_ENTITY_DIM_V",
    "SSDS_DAL_OWNER.ENTERPRISE_ENTITY_RISK_DIM",
    "SSDS_DAL_OWNER.PERSON_RV",
    "SSDS_DAL_OWNER.PERSON_AMS_DISCRETIONARY_STATUS_V",
    "SSDS_DAL_OWNER.PERSON_BROKERAGE_SUBDIMENSION_V",
    "SSDS_DAL_OWNER.CODE_SET_VALUE_V",
    "SSDS_DAL_OWNER.ENTERPRISE_ENTITY_RETAIL_DIMENSION_V",
    "TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_RSN_TP_DIM",
    "TRANSACTIONS_OWNER.LGCY_CNCL_CMPLN_SRC_TP_DIM",
    "TRANSACTIONS_OWNER.LGCY_MKT_TP_DIM",
    "TRANSACTIONS_OWNER.LGCY_TRD_CPCTY_TP_DIM",
    "TRANSACTIONS_OWNER.SRC_ENTR_CNL_TP_DIM",
    "TRANSACTIONS_OWNER.SRC_PCS_TP_DIM",
    "TRANSACTIONS_OWNER.TRD_SLCT_TP_DIM",
]

accessible = []
inaccessible = []

print("=" * 80)
print("SOURCE TABLE ACCESS CHECK")
print("=" * 80)

for tbl in tables:
    sql = f"SELECT COUNT(*) AS CNT FROM {tbl} WHERE ROWNUM <= 1"
    rows, err = query(sql, 1)
    if err:
        print(f"  FAIL: {tbl} => {err[:80]}")
        inaccessible.append(tbl)
    else:
        print(f"  OK:   {tbl}")
        accessible.append(tbl)

print(f"\nAccessible: {len(accessible)}/{len(tables)}")
print(f"Inaccessible: {len(inaccessible)}")
if inaccessible:
    print("\nFailed tables:")
    for t in inaccessible:
        print(f"  - {t}")

# Save results
results = {"accessible": accessible, "inaccessible": inaccessible}
Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_table_access.json").write_text(
    json.dumps(results, indent=2)
)
