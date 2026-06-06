"""Phase 1b: /control-table/compare-docs v16.6 delta overlay (additive).

The v16_delta overlay runs only when DRD is Excel AND both ODI XMLs are present.
It must NOT disturb the existing pairwise/multi expression compare, and a v16
failure must surface as v16_delta.error (never break the core result).
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
_URL = "/api/tests/control-table/compare-docs"


@_avy
def test_compare_docs_adds_v16_delta_with_two_odis():
    files = {
        "drd_file": (_DRD.name, _DRD.read_bytes(), MIME_XLSX),
        "odi_file_1": ("o1.xml", _ODI1.read_bytes(), "text/xml"),
        "odi_file_2": ("o2.xml", _ODI2.read_bytes(), "text/xml"),
    }
    r = client.post(_URL, files=files)
    assert r.status_code == 200, r.text
    d = r.json()
    # Core pairwise/multi compare still present + intact.
    assert len(d["pairwise"]) == 3
    assert d["multi_compare"]["common_target_count"] > 0
    # Additive v16 overlay.
    v = d["v16_delta"]
    assert v and v.get("engine") == "v16-generic-rule-proof"
    assert v["profile_resolved"] == "avy"
    assert sorted(v["summary"]["fixed_by_resolved_rule_proof"]) == ["SDIRA_TXN_TP", "SDIRA_TXN_TP_CD"]
    assert isinstance(v["delta"], list) and v["delta"]


@_avy
def test_compare_docs_no_v16_with_single_odi():
    files = {
        "drd_file": (_DRD.name, _DRD.read_bytes(), MIME_XLSX),
        "odi_file_1": ("o1.xml", _ODI1.read_bytes(), "text/xml"),
    }
    r = client.post(_URL, files=files)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["v16_delta"] is None  # needs BOTH ODIs
    assert len(d["pairwise"]) == 1  # DRD vs ODI1 still works


@_avy
def test_compare_docs_no_v16_when_drd_not_excel():
    files = {
        "drd_file": ("drd.csv", b"target,source\nA,B\n", "text/csv"),
        "odi_file_1": ("o1.xml", _ODI1.read_bytes(), "text/xml"),
        "odi_file_2": ("o2.xml", _ODI2.read_bytes(), "text/xml"),
    }
    r = client.post(_URL, files=files)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["v16_delta"] is None  # DRD must be Excel for the v16 delta
    # core compare unaffected -- 3 docs -> 3 pairs
    assert len(d["pairwise"]) == 3
