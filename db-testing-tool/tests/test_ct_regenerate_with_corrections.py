"""Phase 2 chunk 2: POST /control-table/regenerate-with-corrections.

Covers: build base INSERT -> apply correction (sentinel lands), skipped column
surfaced (not silently dropped), datasource-scoped learn (create/update/previous
+ scope isolation across datasources, skipped cols never learned), and the
non-Excel DRD guard.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import init_db, sync_engine
from app.main import app

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DRD = Path(__file__).resolve().parents[1] / "data" / "taxlot" / "DRD_Activity_Fact.xlsx"
_avy = pytest.mark.skipif(not _DRD.exists(), reason="AVY DRD fixture not present")

_URL = "/api/tests/control-table/regenerate-with-corrections"
_DS_A = 9901  # sentinel test datasources (cleaned up in teardown)
_DS_B = 9902
client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _schema_and_cleanup():
    asyncio.run(init_db())  # ensure datasource_id/confirmed_by + unique index exist
    yield
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM control_table_correction_rules WHERE datasource_id IN (:a, :b)"), {"a": _DS_A, "b": _DS_B})


def _drd():
    return ("DRD_Activity_Fact.xlsx", _DRD.read_bytes(), MIME_XLSX)


def _base_first_column() -> str:
    r = client.post(_URL, files={"drd_file": _drd()}, data={"target_table": "AVY_FACT", "profile": "auto"})
    assert r.status_code == 200, r.text
    base = r.json()["base_sql"]
    m = re.search(r"INSERT\s+INTO\s+\S+\s*\(([^)]+)\)", base, re.I | re.S)
    cols = [c.strip().strip('"').upper() for c in (m.group(1).split(",") if m else [])]
    cols = [c for c in cols if re.fullmatch(r"[A-Z_][A-Z0-9_]*", c)]
    assert cols, "no insert columns parsed from base_sql"
    return cols[0]


@_avy
def test_apply_correction_and_surface_skipped():
    col = _base_first_column()
    corr = [
        {"column": col, "expression": "'SENTINEL_VAL'", "issue_type": "logic"},
        {"column": "ZZZ_BOGUS_COL", "expression": "'X'"},
    ]
    r = client.post(_URL, files={"drd_file": _drd()},
                    data={"target_table": "AVY_FACT", "profile": "auto", "corrections_json": json.dumps(corr)})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["engine"] == "v54-regenerate-with-corrections"
    assert d["applied"] == [col]
    assert "'SENTINEL_VAL'" in d["corrected_sql"]
    skipped = {s["column"]: s["reason"] for s in d["skipped_columns"]}
    assert skipped.get("ZZZ_BOGUS_COL") == "not_in_generated_sql"
    # learn was not requested -> nothing learned
    assert d["learned"] == [] and d["learn_requested"] is False


@_avy
def test_learn_is_datasource_scoped_and_skips_unapplied():
    col = _base_first_column()
    corr = [
        {"column": col, "expression": "'V1'", "issue_type": "logic", "chosen_source": "odi2"},
        {"column": "ZZZ_BOGUS_COL", "expression": "'X'", "issue_type": "logic"},  # skipped -> must NOT learn
    ]
    # create on DS_A
    r1 = client.post(_URL, files={"drd_file": _drd()},
                     data={"target_table": "AVY_FACT", "profile": "auto", "learn": "true",
                           "datasource_id": str(_DS_A), "confirmed_by": "tester", "corrections_json": json.dumps(corr)})
    d1 = r1.json()
    learned1 = {x["column"]: x for x in d1["learned"]}
    assert col in learned1 and learned1[col]["created"] is True
    assert "ZZZ_BOGUS_COL" not in learned1  # skipped column never learned
    assert d1["failed_learn"] == []

    # same DS_A + same col, new expr -> update in place, previous surfaced
    corr2 = [{"column": col, "expression": "'V2'", "issue_type": "logic"}]
    r2 = client.post(_URL, files={"drd_file": _drd()},
                     data={"target_table": "AVY_FACT", "profile": "auto", "learn": "true",
                           "datasource_id": str(_DS_A), "corrections_json": json.dumps(corr2)})
    upd = {x["column"]: x for x in r2.json()["learned"]}[col]
    assert upd["created"] is False and upd["updated"] is True
    assert upd["previous_expression"] == "'V1'"

    # different datasource -> NEW rule (scope isolation), not an update of DS_A
    r3 = client.post(_URL, files={"drd_file": _drd()},
                     data={"target_table": "AVY_FACT", "profile": "auto", "learn": "true",
                           "datasource_id": str(_DS_B), "corrections_json": json.dumps(corr2)})
    iso = {x["column"]: x for x in r3.json()["learned"]}[col]
    assert iso["created"] is True, "ds B must get its own rule (no leak from ds A)"

    # confirm two distinct rows exist (one per datasource) at the DB level
    with sync_engine.begin() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM control_table_correction_rules "
                 "WHERE target_table='AVY_FACT' AND target_column=:c AND datasource_id IN (:a,:b)"),
            {"c": col, "a": _DS_A, "b": _DS_B},
        ).scalar()
    assert n == 2


@_avy
def test_non_excel_drd_422():
    r = client.post(_URL, files={"drd_file": ("drd.csv", b"a,b\n1,2\n", "text/csv")},
                    data={"target_table": "AVY_FACT"})
    assert r.status_code == 422, r.text
