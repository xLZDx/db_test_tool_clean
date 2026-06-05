"""F1/F2/F3 (operator 2026-06-05): the GUI comparison + target resolution must use
the PROPER parsers, not the tolerant regex / typed field alone.

F1: ODI .xml  -> OdiXmlParser + emit_insert (the regex pulled ~3 of 66 columns).
F2: DRD .xlsx -> parse_drd_file (the regex pulled 0 -- it cannot read a binary
    spreadsheet).
F3: a typed/auto-filled target absent from the PDM resolves to the DRD's physical
    table (meta['table_name']) instead of raising a PDM-miss 422.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_OPEN_DRD = _ROOT / "data" / "taxlot" / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"
_OPEN_ODI = _ROOT / "data" / "taxlot" / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"


@pytest.mark.skipif(not _OPEN_ODI.exists(), reason="OPEN ODI XML fixture missing")
def test_f1_odi_xml_uses_proper_parser_not_regex():
    """ODI scenario XML -> full per-column map via OdiXmlParser+emit_insert, far
    more than the ~3 the tolerant regex extracted."""
    from app.routers.tests_control_table import _mappings_from_doc
    res = _mappings_from_doc(_OPEN_ODI.read_bytes(), "scen.xml")
    n = len(res.get("mappings") or [])
    assert n >= 30, f"ODI XML proper-parse should yield many columns, got {n}"
    cols = {m["target"] for m in res["mappings"]}
    assert "SRC_STM_CD" in cols or "AR_ID" in cols, sorted(cols)[:20]


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_f2_drd_xlsx_uses_proper_parser_not_regex():
    """DRD .xlsx (binary) -> per-column map via parse_drd_file, not 0 from the
    text regex that cannot read a spreadsheet."""
    from app.routers.tests_control_table import _mappings_from_doc
    res = _mappings_from_doc(_OPEN_DRD.read_bytes(), "open.xlsx")
    n = len(res.get("mappings") or [])
    assert n >= 30, f"DRD xlsx proper-parse should yield many columns, got {n}"


def test_f1f2_sql_paste_falls_back_to_regex():
    """Pasted SQL text (not .xml/.xlsx) still uses the tolerant regex path."""
    from app.routers.tests_control_table import _mappings_from_doc
    res = _mappings_from_doc(b"X.A AS C1, Y.B AS C2", "paste.txt")
    cols = {m["target"] for m in (res.get("mappings") or [])}
    assert cols == {"C1", "C2"}, cols


def test_f1f2_unparseable_xml_degrades_gracefully():
    """A .xml that is not a real ODI scenario must not crash -> empty/regex result."""
    from app.routers.tests_control_table import _mappings_from_doc
    res = _mappings_from_doc(b"<root><x>nope</x></root>", "junk.xml")
    assert isinstance(res.get("mappings"), list)  # no exception, valid shape


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_f3_target_resolves_to_drd_physical_table_when_typed_name_absent():
    """A typed/auto-filled target absent from the PDM (CLS_TAX_LOTS_FACT_RJT)
    resolves to the DRD's physical table instead of a PDM-miss."""
    from app.services.control_table_service import _resolve_physical_target
    b = _OPEN_DRD.read_bytes()
    _sch, tbl = _resolve_physical_target(2, "TAXLOT_OWNER", "CLS_TAX_LOTS_FACT_RJT", b, "open.xlsx", None)
    assert tbl.upper() == "OPN_TAX_LOTS_NON_BKR_FACT", (_sch, tbl)


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_f3_correct_typed_target_unchanged():
    """A correct typed target is returned unchanged (no spurious remap)."""
    from app.services.control_table_service import _resolve_physical_target
    b = _OPEN_DRD.read_bytes()
    _sch, tbl = _resolve_physical_target(2, "TAXLOT_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT", b, "open.xlsx", None)
    assert tbl.upper() == "OPN_TAX_LOTS_NON_BKR_FACT", (_sch, tbl)
