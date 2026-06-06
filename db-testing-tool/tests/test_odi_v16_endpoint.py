"""Phase 1a endpoint tests: POST /api/odi/scenario/compare-multi (v16.6).

Covers the two-ODI delta path (with full AVY overrides -> deterministic SDIRA
promotion), the UI path (target_table only -> auto-detect still yields a delta),
the single-ODI v15-only fallback, and the upload ext guards.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_TX = Path(__file__).resolve().parents[1] / "data" / "taxlot"
_DRD = _TX / "DRD_Activity_Fact.xlsx"
_ODI1 = _TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
_ODI2 = _TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001_v2.xml"

_HAVE = _DRD.exists() and _ODI1.exists() and _ODI2.exists()
_avy = pytest.mark.skipif(not _HAVE, reason="AVY taxlot fixtures not present")

client = TestClient(app)

_OVERRIDE_FORM = {
    "profile": "auto",
    "target_table": "AVY_FACT",
    "mapping_sheet": "Table-View",
    "target_col": "B",
    "source_cols": "Y,Z,AA",
    "rule_col": "AD",
}


def _files(two=True):
    f = {
        "xml_file": (_ODI1.name, _ODI1.read_bytes(), "text/xml"),
        "drd_file": (_DRD.name, _DRD.read_bytes(), MIME_XLSX),
    }
    if two:
        f["xml_file_2"] = (_ODI2.name, _ODI2.read_bytes(), "text/xml")
    return f


@_avy
def test_compare_multi_two_odi_delta_promotes_sdira():
    r = client.post("/api/odi/scenario/compare-multi", files=_files(two=True), data=_OVERRIDE_FORM)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["engine"] == "v16-generic-rule-proof"
    assert d["profile_resolved"] == "avy"
    promoted = sorted(d["summary"]["fixed_by_resolved_rule_proof"])
    assert promoted == ["SDIRA_TXN_TP", "SDIRA_TXN_TP_CD"], promoted
    assert d["summary"]["delta_status_counts"]["FIXED_BY_RESOLVED_RULE_PROOF"] == 2
    assert isinstance(d["delta"], list) and d["delta"]
    assert isinstance(d["proof"], list) and len(d["proof"]) == 8


@_avy
def test_compare_multi_ui_path_target_table_only_returns_delta():
    """The UI sends only target_table (auto-detect for sheet/cols). It must
    still produce a v16 delta (not crash, not v15-only)."""
    r = client.post(
        "/api/odi/scenario/compare-multi",
        files=_files(two=True),
        data={"target_table": "AVY_FACT"},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["engine"] == "v16-generic-rule-proof"
    assert isinstance(d["delta"], list) and d["delta"]


@_avy
def test_compare_multi_single_odi_is_v15_only():
    r = client.post("/api/odi/scenario/compare-multi", files=_files(two=False), data=_OVERRIDE_FORM)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["engine"] == "v15-only"
    assert "delta" not in d
    assert "proof" not in d
    assert d["v15_by_target"]


@_avy
def test_compare_multi_non_excel_drd_422():
    f = {
        "xml_file": (_ODI1.name, _ODI1.read_bytes(), "text/xml"),
        "drd_file": ("drd.csv", b"a,b,c\n1,2,3\n", "text/csv"),
        "xml_file_2": (_ODI2.name, _ODI2.read_bytes(), "text/xml"),
    }
    r = client.post("/api/odi/scenario/compare-multi", files=f)
    assert r.status_code == 422, r.text


@_avy
def test_compare_multi_non_xml_odi2_422():
    f = {
        "xml_file": (_ODI1.name, _ODI1.read_bytes(), "text/xml"),
        "drd_file": (_DRD.name, _DRD.read_bytes(), MIME_XLSX),
        "xml_file_2": ("odi2.txt", b"<not xml ext>", "text/plain"),
    }
    r = client.post("/api/odi/scenario/compare-multi", files=f, data=_OVERRIDE_FORM)
    assert r.status_code == 422, r.text
