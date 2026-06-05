"""R4 (2026-06-06): AVY golden-oracle acceptance test.

The external reference at D:\\test 2\\avy_fact_analysis_outputs was vendored into
data/taxlot/avy_golden/summary.json as the committed ground-truth oracle. This
test runs our vendored v15 generic comparator on the AVY fixtures and asserts it
reproduces the oracle EXACTLY -- counts AND the 4 missing-column NAMES. Drift in
either our tool OR the oracle fails here, so neither can rot silently.

Functional, not string-match: it executes the real pipeline and asserts on the
observable summary the endpoint returns.
"""
import json
from pathlib import Path

import pytest

from app.services import odi_drd_compare_v15 as v

ROOT = Path(__file__).resolve().parents[1]
TX = ROOT / "data" / "taxlot"
GOLDEN = TX / "avy_golden" / "summary.json"
AVY_XLSX = TX / "DRD_Activity_Fact.xlsx"
AVY_XML = TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"


def _load_gold():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_golden_fixture_present_and_well_formed():
    assert GOLDEN.exists(), f"golden oracle missing: {GOLDEN}"
    g = _load_gold()
    # the oracle schema this test depends on
    for key in ("mapping_columns", "xml_final_insert_columns",
                "missing_in_xml_final", "extra_in_xml_final"):
        assert key in g, f"oracle missing key {key!r}"
    # pin the oracle's own headline numbers so a corrupted oracle is caught too
    assert g["mapping_columns"] == 373
    assert g["xml_final_insert_columns"] == 369
    assert sorted(g["missing_in_xml_final"]) == [
        "SHRT_SALE_EXMPT_CD", "SHRT_SALE_EXMPT_NM",
        "STEP_IN_OUT_IND_CD", "STEP_IN_OUT_IND_NM",
    ]
    assert g["extra_in_xml_final"] == []


@pytest.mark.skipif(not (AVY_XLSX.exists() and AVY_XML.exists()),
                    reason="AVY fixtures not present")
def test_v15_reproduces_avy_golden_oracle():
    g = _load_gold()
    s = v.compare_summary(AVY_XLSX, AVY_XML)  # profile='generic'

    # counts: our (in_both) == oracle final-load columns; our mapping == oracle mapping
    assert s["mapping_columns"] == g["mapping_columns"], (s["mapping_columns"], g["mapping_columns"])
    assert s["in_both"] == g["xml_final_insert_columns"], (s["in_both"], g["xml_final_insert_columns"])

    # the DRD-only set must equal the oracle's missing_in_xml_final, BY NAME
    assert set(s["drd_only_columns"]) == set(g["missing_in_xml_final"]), {
        "v15_drd_only": sorted(s["drd_only_columns"]),
        "oracle_missing": sorted(g["missing_in_xml_final"]),
    }
    # ODI-only must equal the oracle's extra set (empty)
    assert set(s["odi_only_columns"]) == set(g["extra_in_xml_final"])
    # internal consistency: mapping == in_both + drd_only
    assert s["mapping_columns"] == s["in_both"] + s["mapping_only"]
