"""Gate V1 tests for the vendored v18 KB-resolved insert builder + POST
/api/tests/control-table/build-v18.

Proves (functionally, by running the real v18 builder -- not string-match):
- the helper emits a real schema-qualified INSERT from the AVY DRD,
- NULL stubs are CLASSIFIED (audit vs business) so Gate V2 can flag business
  stubs instead of hiding them (the operator's "stub" complaint),
- the endpoint returns 200 with the v18 SQL + classification,
- fail-loud paths: non-Excel upload and a missing target schema never return a
  200 with junk SQL.

These run the v18 subprocess (materialize engine + hardcode gate); they are
fixture-guarded and skip when the v18 tree / DRD / KB is absent.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.v18_insert import (
    V18_TOOL_ROOT,
    V18BuildError,
    _DEFAULT_SCHEMA_KB,
    _STAGE_JOIN_THRESHOLD,
    _fix_alias_in_on,
    _inject_parallel_hint,
    _reorder_joins_by_dependency,
    _stage_projection_over_join,
    build_v18_insert_to_dir,
)

REPO = Path(__file__).resolve().parents[1]
TX = REPO / "data" / "taxlot"
AVY_DRD = TX / "DRD_Activity_Fact.xlsx"
CLOSE_DRD = TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
URL = "/api/tests/control-table/build-v18"

_V18_PRESENT = (V18_TOOL_ROOT / "insert_builder" / "universal_insert_builder.py").exists()
_KB_PRESENT = _DEFAULT_SCHEMA_KB.exists()

client = TestClient(app)

_needs_v18 = pytest.mark.skipif(
    not (_V18_PRESENT and _KB_PRESENT),
    reason="v18 tool tree or schema KB absent",
)


@_needs_v18
@pytest.mark.skipif(not AVY_DRD.exists(), reason="AVY DRD fixture absent")
def test_build_v18_endpoint_avy_clean_insert():
    with AVY_DRD.open("rb") as fd:
        resp = client.post(
            URL,
            files={"drd_file": ("avy.xlsx", fd,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"target_schema": "TRANSACTIONS_OWNER", "target_table": "AVY_FACT", "profile": "avy"},
        )
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["engine"] == "v18-insert-builder"
    sql = d["generated_sql"]
    assert "INSERT INTO" in sql.upper()
    # schema-qualified target (the schema_kb_sql_gate requires owner.table)
    assert "TRANSACTIONS_OWNER.AVY_FACT" in sql.upper()
    # classification keys present + consistent
    assert set(d["business_stub_columns"]).isdisjoint(set(d["audit_stub_columns"]))
    assert d["stub_count"] == len(d["stub_columns"])
    assert d["target"] == "TRANSACTIONS_OWNER.AVY_FACT"


@_needs_v18
@pytest.mark.skipif(not CLOSE_DRD.exists(), reason="CLOSE DRD fixture absent")
def test_build_v18_helper_classifies_audit_vs_business_stubs():
    import tempfile, shutil, gc
    td = Path(tempfile.mkdtemp(prefix="t_v18_"))
    try:
        res = build_v18_insert_to_dir(
            CLOSE_DRD, td / "out",
            target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
            profile="taxlot",
        )
        assert "INSERT INTO" in res["generated_sql"].upper()
        # audit columns must NOT be reported as business stubs; business stubs are
        # the real unresolved mappings Gate V2 must surface.
        audit = set(res["audit_stub_columns"])
        business = set(res["business_stub_columns"])
        assert audit.isdisjoint(business)
        # CLOSE is known to emit audit stubs (CRT_DTM etc.) -> they land in audit, not business
        for c in res["stub_columns"]:
            if c in {"CRT_DTM", "CRT_USR_NM", "LAST_UDT_DTM", "LAST_UDT_USR_NM"}:
                assert c in audit and c not in business
    finally:
        gc.collect()
        shutil.rmtree(td, ignore_errors=True)


@_needs_v18
@pytest.mark.skipif(not CLOSE_DRD.exists(), reason="CLOSE DRD fixture absent")
def test_build_v18_retargets_to_control_schema():
    # control_schema retargets INSERT INTO <owner>.<table> -> <control_schema>.<table>
    # (the user's own control table). NOT hardcoded -- driven by the param.
    import tempfile, shutil, gc
    td = Path(tempfile.mkdtemp(prefix="t_v18cs_"))
    try:
        res = build_v18_insert_to_dir(
            CLOSE_DRD, td / "out",
            target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
            profile="taxlot", control_schema="IKOROSTELEV",
        )
        sql = res["generated_sql"].upper()
        assert "INSERT INTO IKOROSTELEV.CLS_TAX_LOTS_NON_BKR_FACT" in sql
        assert "INSERT INTO TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT" not in sql
        assert res["target"] == "IKOROSTELEV.CLS_TAX_LOTS_NON_BKR_FACT"
        assert res["production_target"] == "TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT"
        assert res["control_schema"] == "IKOROSTELEV"
    finally:
        gc.collect()
        shutil.rmtree(td, ignore_errors=True)


# --- V7: alias-in-ON post-fix (pure logic, no DB) ------------------------------

def test_fix_alias_in_on_replaces_bare_alias_in_join_on():
    sql = (
        "INSERT INTO O.T (A, B)\n"
        "SELECT AR_GRP_SUBDIM.FA_NUM AS OWN_FA_NUM,\n"
        "  FA_NUMBER_V.FA_NUMBER_ENTITY_CODE AS OWN_FA_NUM_ENT_CD\n"
        "FROM CCAL.TXN TXN\n"
        "    LEFT JOIN SSDS.FA_NUMBER_V FA_NUMBER_V ON FA_NUMBER_V.FA_NUMBER = OWN_FA_NUM and TXN.td >= FA_NUMBER_V.EFFECTIVE_DATE\n"
        "    LEFT JOIN SSDS.ENTERPRISE_ENTITY_DIM_V E ON E.Entity_code_long = OWN_FA_NUM_ENT_CD\n"
    )
    fixed, names = _fix_alias_in_on(sql)
    assert "FA_NUMBER_V.FA_NUMBER = AR_GRP_SUBDIM.FA_NUM" in fixed   # OWN_FA_NUM inlined in ON
    assert "E.Entity_code_long = FA_NUMBER_V.FA_NUMBER_ENTITY_CODE" in fixed
    assert {"OWN_FA_NUM", "OWN_FA_NUM_ENT_CD"} <= {n.upper() for n in names}
    # the SELECT-list alias DEFINITIONS are untouched
    assert "AS OWN_FA_NUM," in fixed
    assert "AS OWN_FA_NUM_ENT_CD" in fixed


def test_fix_alias_in_on_noop_when_no_alias_in_on():
    sql = ("INSERT INTO O.T (A)\nSELECT X.C AS A\nFROM S.X X\n    LEFT JOIN S.Y Y ON Y.id = X.id\n")
    fixed, names = _fix_alias_in_on(sql)
    assert names == []
    assert fixed == sql


def test_fix_alias_in_on_protects_qualified_refs():
    # a QUALIFIED ref (A.OWN_FA_NUM) in ON must NOT be rewritten (only bare aliases)
    sql = ("SELECT A.FA AS OWN_FA_NUM\nFROM S.A A\n    JOIN S.B B ON B.x = A.OWN_FA_NUM\n")
    fixed, names = _fix_alias_in_on(sql)
    assert "A.OWN_FA_NUM" in fixed
    assert names == []


# --- V8: join dependency reorder (pure logic, no DB) ---------------------------

def test_reorder_joins_fixes_forward_reference():
    # B's ON references C, but C is defined AFTER B -> Oracle ORA-00904.
    sql = (
        "INSERT INTO O.T (A)\n"
        "SELECT B.x AS A\n"
        "FROM S.BASE BASE\n"
        "    LEFT JOIN S.B B ON B.id = C.fk\n"
        "    LEFT JOIN S.C C ON C.id = BASE.cid\n"
    )
    fixed, relocated = _reorder_joins_by_dependency(sql)
    assert fixed.index(" S.C C ") < fixed.index(" S.B B "), "C must be ordered before B"
    assert relocated, "relocation recorded"


def test_reorder_joins_noop_when_already_ordered():
    sql = (
        "INSERT INTO O.T (A)\n"
        "SELECT BASE.x AS A\n"
        "FROM S.BASE BASE\n"
        "    LEFT JOIN S.B B ON B.id = BASE.id\n"
        "    LEFT JOIN S.C C ON C.id = B.id\n"
    )
    fixed, relocated = _reorder_joins_by_dependency(sql)
    assert relocated == []
    assert fixed == sql


# --- parallel hint (pure logic) ------------------------------------------------

def test_inject_parallel_hint_adds_to_insert_select():
    sql = "INSERT INTO O.T (A)\nSELECT X.c AS A FROM S.X X"
    out = _inject_parallel_hint(sql)
    assert "SELECT /*+ PARALLEL */" in out


def test_inject_parallel_hint_noop_when_already_hinted():
    sql = "INSERT INTO O.T (A)\nSELECT /*+ PARALLEL(8) */ X.c AS A FROM S.X X"
    out = _inject_parallel_hint(sql)
    assert out.count("/*+") == 1
    assert out == sql


# --- V9: stage wide-projection-over-many-joins into a MATERIALIZE'd CTE --------

def _synth_wide_insert(n_joins: int) -> str:
    cols = [f"OUT_{i}" for i in range(n_joins)]
    sel = ",\n    ".join(f"J{i}.VAL AS OUT_{i}" for i in range(n_joins))
    joins = "\n    ".join(f"LEFT JOIN S.T{i} J{i} ON J{i}.ID = TXN.K{i}" for i in range(n_joins))
    return ("INSERT INTO O.TGT (\n    " + ",\n    ".join(cols) + "\n)\n"
            f"SELECT\n    {sel}\nFROM S.TXN TXN\n    {joins}")


def test_stage_projection_rewrites_wide_join_into_materialized_cte():
    n = _STAGE_JOIN_THRESHOLD + 5
    sql = _synth_wide_insert(n)
    staged, src_cols, reason = _stage_projection_over_join(sql)
    assert reason is None and staged != sql
    # one materialized CTE, PARALLEL on BOTH selects (operator: "max parallel per select")
    assert "WITH stg AS (" in staged
    assert "/*+ MATERIALIZE PARALLEL */" in staged
    assert "SELECT /*+ PARALLEL */" in staged
    assert staged.rstrip().endswith("FROM stg")
    # INSERT target preserved verbatim (so control-schema retarget still matched it)
    assert "INSERT INTO O.TGT (" in staged
    # every projection ref rebased to the staged flat table; no raw alias.col left in outer
    assert "stg.J0__VAL AS OUT_0" in staged
    assert "stg.J{}__VAL AS OUT_{}".format(n - 1, n - 1) in staged
    assert src_cols == n


def test_stage_projection_noop_below_threshold():
    sql = _synth_wide_insert(_STAGE_JOIN_THRESHOLD - 1)
    staged, src_cols, reason = _stage_projection_over_join(sql)
    assert staged == sql and src_cols is None and reason == "below_threshold"


def test_stage_projection_case_insensitive_rebase():
    # a lowercase alias.col ref (v18 mixes case) must still be rebased -- this was the
    # OWN_FA_ENT bug; IGNORECASE fixes the whole class.
    n = _STAGE_JOIN_THRESHOLD + 1
    sql = _synth_wide_insert(n).replace("J3.VAL AS OUT_3", "j3.val AS OUT_3")
    staged, _src, reason = _stage_projection_over_join(sql)
    # reason is None proves the unrebased-ref guard passed -> the lowercase ref WAS
    # rebased (an escaped j3.val in the outer projection would set reason != None).
    assert reason is None
    # the lowercase ref is rebased to the staged flat column (uppercased name)
    assert "stg.J3__VAL AS OUT_3" in staged
    # the staged source projection (inside the CTE) legitimately keeps J3.VAL AS J3__VAL
    assert "J3.VAL AS J3__VAL" in staged


def test_stage_projection_fail_safe_on_col_count_mismatch():
    # 3 target cols but 2 projection exprs -> never emit; return the monolith.
    sql = ("INSERT INTO O.T (A, B, C)\nSELECT J0.X AS A, J1.Y AS B\n"
           "FROM S.TXN TXN\n    " +
           "\n    ".join(f"LEFT JOIN S.T{i} J{i} ON J{i}.ID = TXN.K{i}" for i in range(_STAGE_JOIN_THRESHOLD + 2)))
    staged, src_cols, reason = _stage_projection_over_join(sql)
    assert staged == sql and src_cols is None and reason.startswith("col_count_mismatch")


@_needs_v18
@pytest.mark.skipif(not AVY_DRD.exists(), reason="AVY DRD fixture absent")
def test_build_v18_stages_avy_but_not_close():
    import tempfile, shutil, gc
    td = Path(tempfile.mkdtemp(prefix="t_v18stage_"))
    try:
        avy = build_v18_insert_to_dir(
            AVY_DRD, td / "avy", target_schema="TRANSACTIONS_OWNER",
            target_table="AVY_FACT", profile="avy", control_schema="IKOROSTELEV",
        )
        assert avy["staged"] is True
        assert avy["stage_skip_reason"] is None
        assert avy["stage_source_cols"] and avy["stage_source_cols"] > 0
        assert "WITH stg AS (" in avy["generated_sql"]
        assert "INSERT INTO IKOROSTELEV.AVY_FACT" in avy["generated_sql"].upper()
        if CLOSE_DRD.exists():
            close = build_v18_insert_to_dir(
                CLOSE_DRD, td / "close", target_schema="TAXLOT_OWNER",
                target_table="CLS_TAX_LOTS_NON_BKR_FACT", profile="taxlot",
                control_schema="IKOROSTELEV",
            )
            # CLOSE has ~7 joins -> below threshold -> stays a monolith
            assert close["staged"] is False
            assert close["stage_skip_reason"] == "below_threshold"
            assert "WITH stg AS (" not in close["generated_sql"]
    finally:
        gc.collect()
        shutil.rmtree(td, ignore_errors=True)


def test_build_v18_rejects_non_excel():
    resp = client.post(
        URL,
        files={"drd_file": ("x.txt", b"not excel", "text/plain")},
        data={"target_schema": "X", "target_table": "Y"},
    )
    assert resp.status_code == 422


@_needs_v18
@pytest.mark.skipif(not AVY_DRD.exists(), reason="AVY DRD fixture absent")
def test_build_v18_requires_target_schema_fail_loud():
    # v18 needs a qualified owner.table; an empty target schema must fail loud
    # (422), never 200 with junk SQL.
    with AVY_DRD.open("rb") as fd:
        resp = client.post(
            URL,
            files={"drd_file": ("avy.xlsx", fd,
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"target_schema": "", "target_table": "AVY_FACT", "profile": "avy"},
        )
    assert resp.status_code == 422, resp.text


def test_build_v18_helper_requires_target_schema():
    with pytest.raises(V18BuildError):
        build_v18_insert_to_dir(
            AVY_DRD, Path(__file__).resolve().parents[1] / "data" / "_nonexistent_out_v18",
            target_schema="", target_table="AVY_FACT", profile="avy",
        )
