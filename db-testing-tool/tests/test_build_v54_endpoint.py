"""Gate G2 endpoint tests for POST /api/tests/control-table/build-v54.

Proves: DRD-driven build over the API returns a clean INSERT (no `O.` / no ODI
final-CTE), the 3-way tri-compare shows differences ONLY vs ODI (DRD == generated;
same_mismatch_as_drd_odi == Y for every row), ODI is optional, and a malformed
upload fails loud (never HTTP 200 with empty SQL -- the T2 BLOCKER fix).
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

TX = Path(__file__).resolve().parents[1] / "data" / "taxlot"
CLOSE_DRD = TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
CLOSE_ODI = TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
URL = "/api/tests/control-table/build-v54"

client = TestClient(app)


@pytest.mark.skipif(not (CLOSE_DRD.exists() and CLOSE_ODI.exists()), reason="CLOSE fixtures absent")
def test_build_v54_3way_diffs_only_vs_odi():
    with CLOSE_DRD.open("rb") as fd, CLOSE_ODI.open("rb") as fo:
        resp = client.post(
            URL,
            files={"drd_file": ("close.xlsx", fd, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                   "odi_file": ("close.xml", fo, "text/xml")},
            data={"target_schema": "TAXLOT_OWNER", "target_table": "CLS_TAX_LOTS_NON_BKR_FACT", "profile": "taxlot"},
        )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    sql = d["generated_sql"]
    assert "INSERT INTO" in sql.upper()
    # DRD-driven: no ODI final-CTE helper
    assert "odi_final_source" not in sql
    import re
    assert not re.search(r"\bO\.", sql)
    # 3-way invariant: every row's generated-vs-DRD is DRD_SOURCE, and the
    # generated-vs-ODI mismatch set equals the DRD-vs-ODI mismatch set (no NEW
    # mismatch introduced) -> differences land ONLY on ODI.
    tri = d["tri_compare"]
    assert tri, "tri_compare empty"
    assert all(r.get("generated_vs_drd") == "DRD_SOURCE" for r in tri), "some column diverges from DRD"
    assert all(r.get("same_mismatch_as_drd_odi") == "Y" for r in tri), "generator introduced a NEW mismatch vs ODI"
    assert d["odi_provided"] is True


@pytest.mark.skipif(not CLOSE_DRD.exists(), reason="CLOSE DRD absent")
def test_build_v54_odi_optional():
    with CLOSE_DRD.open("rb") as fd:
        resp = client.post(
            URL,
            files={"drd_file": ("close.xlsx", fd, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"target_schema": "TAXLOT_OWNER", "target_table": "CLS_TAX_LOTS_NON_BKR_FACT", "profile": "taxlot"},
        )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert "INSERT INTO" in d["generated_sql"].upper()
    assert d["odi_provided"] is False


def test_build_v54_rejects_non_excel():
    resp = client.post(
        URL,
        files={"drd_file": ("x.txt", b"not excel", "text/plain")},
        data={"target_schema": "X", "target_table": "Y"},
    )
    assert resp.status_code == 422  # ext allow-list


def test_build_v54_malformed_xlsx_never_200_empty():
    # T2 BLOCKER: a file that passes the .xlsx ext check but is not a real
    # workbook must fail loud (non-200), never return 200 with empty SQL.
    resp = client.post(
        URL,
        files={"drd_file": ("bad.xlsx", b"not a real workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"target_schema": "X", "target_table": "Y"},
    )
    assert resp.status_code != 200
    assert resp.status_code in (422, 500)
