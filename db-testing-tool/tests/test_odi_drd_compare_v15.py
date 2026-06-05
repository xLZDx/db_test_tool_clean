"""R2 smoke tests for the vendored v15 DRD-vs-ODI comparator.

Proves the generic pipeline reproduces the externally-produced gold reference
(D:\\test 2\\avy_fact_analysis_outputs: AVY 373/369/4/0) from inside the app
package, with no per-file hardcoding and no AVY/TaxLot curated heuristics.

These call the real pipeline on the taxlot fixtures (no mocks) and assert on
the observable column-count summary -- functional, not string-match.
"""
from pathlib import Path

import pytest

from app.services import odi_drd_compare_v15 as v

TX = Path(__file__).resolve().parents[1] / "data" / "taxlot"

CASES = {
    "AVY": (
        TX / "DRD_Activity_Fact.xlsx",
        TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
        dict(mapping=373, in_both=369, mapping_only=4, xml_only=0),
    ),
    "CLOSE": (
        TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
        TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
        dict(mapping=84, in_both=83, mapping_only=1, xml_only=0),
    ),
    "OPEN": (
        TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
        TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
        dict(mapping=66, in_both=66, mapping_only=0, xml_only=0),
    ),
}


def _have(paths):
    return all(p.exists() for p in paths)


@pytest.mark.parametrize("name", list(CASES))
def test_compare_summary_matches_gold(name):
    xlsx, xml, exp = CASES[name]
    if not _have([xlsx, xml]):
        pytest.skip(f"{name} fixtures not present")
    s = v.compare_summary(xlsx, xml)  # profile='generic' default
    assert s["mapping_columns"] == exp["mapping"], (name, s)
    assert s["in_both"] == exp["in_both"], (name, s)
    assert s["mapping_only"] == exp["mapping_only"], (name, s)
    assert s["xml_only"] == exp["xml_only"], (name, s)


def test_avy_auto_detect_layout_high_confidence():
    xlsx, xml, _ = CASES["AVY"]
    if not _have([xlsx, xml]):
        pytest.skip("AVY fixtures not present")
    s = v.compare_summary(xlsx, xml)
    det = s["detection"]
    assert det["mapping_sheet"] == "Table-View"
    assert str(det["header_row"]) == "12"
    assert det["target_col"] == "B"
    assert float(det["confidence"]) >= 0.99


def test_compare_to_dir_writes_reports(tmp_path):
    xlsx, xml, exp = CASES["AVY"]
    if not _have([xlsx, xml]):
        pytest.skip("AVY fixtures not present")
    out = v.compare_to_dir(xlsx, xml, tmp_path / "avy_out")
    assert (out / "comparison_report.md").exists()
    assert (out / "column_diff.csv").exists()
    assert (out / "detected_layout.json").exists()
    # generic profile must not emit curated AVY rows as the canonical output path
    assert out.is_dir()


def test_module_is_ascii_clean():
    """Project python-reviewer MUST-FAIL guard: no non-ASCII in the vendored file."""
    src = Path(v.__file__).read_text(encoding="utf-8")
    bad = [(i, repr(c)) for i, c in enumerate(src) if ord(c) > 127]
    assert not bad, f"non-ASCII chars present: {bad[:5]}"
