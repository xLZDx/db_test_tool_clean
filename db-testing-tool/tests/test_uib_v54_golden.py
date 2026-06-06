"""Gate G4 -- golden acceptance test for the v5.4 DRD-driven builder.

Pins the operator-locked success criteria so neither the builder nor the wiring
can silently regress:
  1. CLEAN DRD-driven INSERT: 0 `O.`, 0 odi_final_source, 0 AVY_FACT_STEP5_STG_RT;
     AVY primary source `FROM CCAL_REPL_OWNER.TXN TXN`; CL_VAL CL_SCM_ID filters.
  2. 3-WAY DRD/ODI/generated: generated == DRD (every tri row generated_vs_drd ==
     'DRD_SOURCE') and the generator introduces NO new mismatch
     (same_mismatch_as_drd_odi == 'Y' for every row) -> differences land ONLY on
     ODI, exactly the EXPECTED review-field set per fixture.
  3. ODI is optional (DRD-only generation works).
"""
import csv
import re
from pathlib import Path

import pytest

from app.services import universal_insert_builder_v54 as uib

TX = Path(__file__).resolve().parents[1] / "data" / "taxlot"

FIX = {
    "AVY": dict(
        xlsx=TX / "DRD_Activity_Fact.xlsx",
        xml=TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
        tsch="TRANSACTIONS_OWNER", ttbl="AVY_FACT", profile="avy",
        total=373,
        odi_only={
            "MM_ALT_ID", "BATCH_DT", "BKR_AR_ID", "LGCY_TRD_CPCTY_TP_DIM_ID",
            "DB_CARD_TXN_DT", "DB_CARD_ORIG_CCY_CD", "SDIRA_TXN_TP_CD",
            "SDIRA_TXN_TP", "SDIRA_TXN_YR", "STEP_IN_OUT_IND_CD",
            "STEP_IN_OUT_IND_NM", "SHRT_SALE_EXMPT_CD", "SHRT_SALE_EXMPT_NM",
        },
    ),
    "CLOSE": dict(
        xlsx=TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
        xml=TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
        tsch="TAXLOT_OWNER", ttbl="CLS_TAX_LOTS_NON_BKR_FACT", profile="taxlot",
        total=84,
        odi_only={"ACG_TP_NM", "CCAL_PD_ID", "SRC_STM_CD", "SRC_STM_NM", "POS_CLS_CD"},
    ),
    "OPEN": dict(
        xlsx=TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
        xml=TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
        tsch="TAXLOT_OWNER", ttbl="OPN_TAX_LOTS_NON_BKR_FACT", profile="taxlot",
        total=66,
        odi_only={"ACG_TP_NM", "MISS_COST_BSS_F", "MISS_IVS_COST_F", "WASH_SALE_TP"},
    ),
}

_FORBIDDEN = ("odi_final_source", "AVY_FACT_STEP5_STG_RT", "SSDS_AVY_FACT_STEP5_STG")


def _build(tmp_path, f, with_odi=True):
    out = uib.build_to_dir(
        f["xlsx"], f["xml"] if with_odi else None, tmp_path,
        target_schema=f["tsch"], target_table=f["ttbl"], profile=f["profile"],
    )
    sql = (Path(out) / "generated_insert_select_candidate.sql").read_text(encoding="utf-8")
    tri = []
    tp = Path(out) / "tri_compare_report.csv"
    if tp.exists():
        with tp.open(encoding="utf-8-sig", newline="") as fh:
            tri = list(csv.DictReader(fh))
    return sql, tri


@pytest.mark.parametrize("name", list(FIX))
def test_golden_clean_insert(tmp_path, name):
    f = FIX[name]
    if not (f["xlsx"].exists() and f["xml"].exists()):
        pytest.skip(f"{name} fixtures absent")
    sql, _ = _build(tmp_path, f)
    assert "INSERT INTO" in sql.upper()
    for tok in _FORBIDDEN:
        assert tok not in sql, f"{name}: forbidden ODI-final token {tok!r}"
    assert not re.search(r"\bO\.", sql), f"{name}: forbidden `O.` final-CTE alias"
    assert len(re.findall(r"\bJOIN\b", sql)) > 0, f"{name}: no DRD joins"


def test_golden_avy_source_and_scheme_filters(tmp_path):
    f = FIX["AVY"]
    if not (f["xlsx"].exists() and f["xml"].exists()):
        pytest.skip("AVY fixtures absent")
    sql, _ = _build(tmp_path, f)
    assert re.search(r"FROM\s+CCAL_REPL_OWNER\.TXN\s+TXN", sql)
    assert re.search(r"CL_SCM_ID\s*=\s*\d+", sql), "expected CL_VAL CL_SCM_ID scheme filters"


@pytest.mark.parametrize("name", list(FIX))
def test_golden_3way_diffs_only_vs_odi(tmp_path, name):
    f = FIX[name]
    if not (f["xlsx"].exists() and f["xml"].exists()):
        pytest.skip(f"{name} fixtures absent")
    _, tri = _build(tmp_path, f)
    assert tri, f"{name}: tri_compare_report.csv is empty/missing"  # no vacuous all(...) pass
    assert len(tri) == f["total"], (name, len(tri), f["total"])
    # generated == DRD for every column
    assert all(r.get("generated_vs_drd") == "DRD_SOURCE" for r in tri), f"{name}: a column diverges from DRD"
    # no NEW mismatch introduced by the generator
    assert all(r.get("same_mismatch_as_drd_odi") == "Y" for r in tri), f"{name}: generator introduced a new mismatch"
    # differences land ONLY on ODI, exactly the expected review set
    odi_only = {r["target_column"] for r in tri
                if "MISMATCH" in (r.get("generated_vs_odi", "") or "").upper()
                or "REVIEW" in (r.get("generated_vs_odi", "") or "").upper()}
    assert odi_only == f["odi_only"], (name, "got", sorted(odi_only), "want", sorted(f["odi_only"]))


def test_golden_odi_optional(tmp_path):
    f = FIX["AVY"]
    if not f["xlsx"].exists():
        pytest.skip("AVY DRD absent")
    sql, _ = _build(tmp_path, f, with_odi=False)
    assert "INSERT INTO" in sql.upper()
    assert not re.search(r"\bO\.", sql)
    assert len(re.findall(r"\bJOIN\b", sql)) > 0
