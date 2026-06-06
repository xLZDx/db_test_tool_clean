"""Gate G1 smoke tests for the vendored v5.4 DRD-driven INSERT builder.

Proves the in-app build_to_dir produces a DRD-driven INSERT (real joins, NO ODI
final-CTE helper: no `O.`, no `odi_final_source`, no Step5 fake source) and that
ODI is optional (DRD-only generation works). Functional: runs the real builder
on the taxlot fixtures (no mocks).
"""
import re
from pathlib import Path

import pytest

from app.services import universal_insert_builder_v54 as uib

TX = Path(__file__).resolve().parents[1] / "data" / "taxlot"

CASES = {
    "AVY": (TX / "DRD_Activity_Fact.xlsx",
            TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
            "TRANSACTIONS_OWNER", "AVY_FACT", "avy"),
    "CLOSE": (TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
              TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
              "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT", "taxlot"),
    "OPEN": (TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
             TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
             "TAXLOT_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT", "taxlot"),
}

_FORBIDDEN = ("odi_final_source", "AVY_FACT_STEP5_STG_RT", "SSDS_AVY_FACT_STEP5_STG")


def _gen_sql(out: Path) -> str:
    return (out / "generated_insert_select_candidate.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", list(CASES))
def test_drd_driven_no_odi_final_helper(tmp_path, name):
    xlsx, xml, tsch, ttbl, prof = CASES[name]
    if not (xlsx.exists() and xml.exists()):
        pytest.skip(f"{name} fixtures absent")
    out = uib.build_to_dir(xlsx, xml, tmp_path / name, target_schema=tsch, target_table=ttbl, profile=prof)
    sql = _gen_sql(Path(out))
    for tok in _FORBIDDEN:
        assert tok not in sql, f"{name}: forbidden ODI-final token {tok!r} in generated SQL"
    assert not re.search(r"\bO\.", sql), f"{name}: forbidden `O.` final-CTE alias in generated SQL"
    assert len(re.findall(r"\bJOIN\b", sql)) > 0, f"{name}: no joins built from DRD"


def test_avy_builds_from_txn_and_real_joins(tmp_path):
    xlsx, xml, tsch, ttbl, prof = CASES["AVY"]
    if not (xlsx.exists() and xml.exists()):
        pytest.skip("AVY fixtures absent")
    out = uib.build_to_dir(xlsx, xml, tmp_path / "avy", target_schema=tsch, target_table=ttbl, profile=prof)
    sql = _gen_sql(Path(out))
    assert re.search(r"FROM\s+CCAL_REPL_OWNER\.TXN\s+TXN", sql), "AVY primary source must be CCAL_REPL_OWNER.TXN"
    # CL_VAL lookups must carry a scheme filter (CL_SCM_ID), not a guessed key
    assert re.search(r"CL_SCM_ID\s*=\s*\d+", sql), "expected CL_VAL CL_SCM_ID scheme filter(s)"


def test_odi_is_optional_drd_only_generates(tmp_path):
    xlsx, _xml, tsch, ttbl, prof = CASES["AVY"]
    if not xlsx.exists():
        pytest.skip("AVY DRD absent")
    out = uib.build_to_dir(xlsx, None, tmp_path / "avy_drdonly", target_schema=tsch, target_table=ttbl, profile=prof)
    sql = _gen_sql(Path(out))
    assert len(re.findall(r"\bJOIN\b", sql)) > 0, "DRD-only build must still produce joins"
    for tok in _FORBIDDEN:
        assert tok not in sql
