"""Tests for the ODI derivation walker.

Generic placeholders only (WIDGET / GADGET / ZED).  Each test constructs a
minimal ODIModel by hand or via the parser on a tiny synthetic ODI-like
SQL block, then asserts on the resulting ``column_derivations`` map.
"""
from __future__ import annotations

import pytest

from app.sql_model.derivation_walker import (
    _classify_select_item,
    _is_staging_alias,
    enrich_model,
)
from app.sql_model.types import (
    EXPR_KIND_AGG,
    EXPR_KIND_CASE_WHEN,
    EXPR_KIND_COLUMN_REF,
    EXPR_KIND_FUNCTION,
    EXPR_KIND_LITERAL,
    EXPR_KIND_PASSTHROUGH,
    MERGE_STEP_ID,
    MERGE_USING_STEP_ID,
    ColumnDerivation,
    ODIModel,
    StagingStep,
    TableRef,
)


def _make_model(steps_sql, merge_sql=""):
    """Build a minimal ODIModel from raw SELECT SQL strings.

    ``steps_sql`` is a list of (step_id, name, sql) tuples; each sql is the
    full ``INSERT INTO X (...) SELECT ...`` for that step.
    """
    m = ODIModel(target=TableRef(schema="MYS", table="TGT"))
    for sid, name, sql in steps_sql:
        m.staging_steps.append(StagingStep(step_id=sid, name=name, select_sql=sql))
    if merge_sql:
        m.final_select_sql = merge_sql
    return m


def test_staging_alias_detector_matches_step_pattern():
    """ODI step names like SSDS_WIDGET_STEPn_STG (with optional _RT) are
    recognised as staging tables."""
    staging = {"SSDS_WIDGET_STEP1_STG", "SSDS_WIDGET_STEP1_STG_RT"}
    assert _is_staging_alias("SSDS_WIDGET_STEP1_STG", staging) is True
    assert _is_staging_alias("SSDS_WIDGET_STEP1_STG_RT", staging) is True
    # Pattern-only fallback (alias not in the explicit set)
    assert _is_staging_alias("OTHER_STEP3_STG", staging) is True
    # Non-staging alias rejected
    assert _is_staging_alias("WIDGET_DIM", staging) is False
    assert _is_staging_alias("", staging) is False


def test_simple_column_ref_classified_as_column_ref():
    """A bare ``alias.col`` where alias is NOT a staging table is column_ref."""
    sql = "INSERT INTO STG (X) SELECT WIDGET_LK.X FROM WIDGET_LK"
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    chain = m.column_derivations.get("X")
    assert chain is not None
    assert len(chain) == 1
    auth = chain[0]
    assert auth.expr_kind == EXPR_KIND_COLUMN_REF
    assert auth.source_alias.upper() == "WIDGET_LK"
    assert auth.source_col.upper() == "X"
    assert auth.is_authoritative is True


def test_staging_passthrough_classified_correctly():
    """A column ref whose alias matches a known staging table is passthrough."""
    step1 = "INSERT INTO SSDS_WIDGET_STEP1_STG (X) SELECT WIDGET_LK.X FROM WIDGET_LK"
    step2 = (
        "INSERT INTO SSDS_WIDGET_STEP2_STG (X) "
        "SELECT SSDS_WIDGET_STEP1_STG.X FROM SSDS_WIDGET_STEP1_STG"
    )
    m = _make_model([
        (1, "SSDS_WIDGET_STEP1_STG", step1),
        (2, "SSDS_WIDGET_STEP2_STG", step2),
    ])
    enrich_model(m)
    chain = m.column_derivations.get("X")
    assert chain is not None
    assert len(chain) == 2
    kinds = [d.expr_kind for d in chain]
    assert kinds[0] == EXPR_KIND_COLUMN_REF      # STEP1: real ref
    assert kinds[1] == EXPR_KIND_PASSTHROUGH     # STEP2: passes through STEP1
    # Authoritative is STEP1 (first non-passthrough)
    auth = next(d for d in chain if d.is_authoritative)
    assert auth.step_id == 1
    assert auth.expr_kind == EXPR_KIND_COLUMN_REF


def test_case_when_classified_correctly():
    sql = (
        "INSERT INTO STG (FLG) "
        "SELECT CASE WHEN T.X = 1 THEN 'Y' ELSE 'N' END FROM WIDGET_TBL T"
    )
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    chain = m.column_derivations.get("FLG")
    assert chain is not None
    assert chain[0].expr_kind == EXPR_KIND_CASE_WHEN
    assert chain[0].is_authoritative is True


def test_aggregate_classified_correctly():
    sql = (
        "INSERT INTO STG (LIST, CNT) "
        "SELECT LISTAGG(T.X, ',') WITHIN GROUP (ORDER BY T.X), COUNT(*) "
        "FROM WIDGET T"
    )
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    chain_list = m.column_derivations.get("LIST")
    chain_cnt = m.column_derivations.get("CNT")
    assert chain_list and chain_list[0].expr_kind == EXPR_KIND_AGG
    assert chain_cnt and chain_cnt[0].expr_kind == EXPR_KIND_AGG


def test_nvl_function_classified_as_function():
    sql = "INSERT INTO STG (X) SELECT NVL(T.X, 0) FROM WIDGET T"
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    chain = m.column_derivations.get("X")
    assert chain and chain[0].expr_kind == EXPR_KIND_FUNCTION


def test_literal_classified_correctly():
    sql = "INSERT INTO STG (X, Y, Z) SELECT 42, 'hello', NULL FROM DUAL"
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    assert m.column_derivations["X"][0].expr_kind == EXPR_KIND_LITERAL
    assert m.column_derivations["Y"][0].expr_kind == EXPR_KIND_LITERAL
    assert m.column_derivations["Z"][0].expr_kind == EXPR_KIND_LITERAL


def test_authoritative_picks_first_non_passthrough():
    """In a 3-step chain where STEP1 has the real derivation and STEP2+STEP3
    are passthroughs, STEP1 is the authoritative step."""
    step1 = "INSERT INTO SSDS_W_STEP1_STG (X) SELECT WIDGET_LK.X FROM WIDGET_LK"
    step2 = (
        "INSERT INTO SSDS_W_STEP2_STG (X) "
        "SELECT SSDS_W_STEP1_STG.X FROM SSDS_W_STEP1_STG"
    )
    step3 = (
        "INSERT INTO SSDS_W_STEP3_STG (X) "
        "SELECT SSDS_W_STEP2_STG.X FROM SSDS_W_STEP2_STG"
    )
    m = _make_model([
        (1, "SSDS_W_STEP1_STG", step1),
        (2, "SSDS_W_STEP2_STG", step2),
        (3, "SSDS_W_STEP3_STG", step3),
    ])
    enrich_model(m)
    chain = m.column_derivations["X"]
    assert len(chain) == 3
    auth = [d for d in chain if d.is_authoritative]
    assert len(auth) == 1
    assert auth[0].step_id == 1


def test_all_passthrough_chain_marks_earliest_authoritative():
    """When every step is a passthrough (a true ODI gap), the EARLIEST step
    is still marked authoritative so the consumer sees an honest chain."""
    step1 = (
        "INSERT INTO SSDS_W_STEP1_STG (X) "
        "SELECT SSDS_W_STEP0_STG.X FROM SSDS_W_STEP0_STG"
    )
    step2 = (
        "INSERT INTO SSDS_W_STEP2_STG (X) "
        "SELECT SSDS_W_STEP1_STG.X FROM SSDS_W_STEP1_STG"
    )
    m = _make_model([
        (1, "SSDS_W_STEP1_STG", step1),
        (2, "SSDS_W_STEP2_STG", step2),
    ])
    enrich_model(m)
    chain = m.column_derivations["X"]
    assert all(d.expr_kind == EXPR_KIND_PASSTHROUGH for d in chain)
    # Earliest authoritative
    auth = [d for d in chain if d.is_authoritative][0]
    assert auth.step_id == 1


def test_no_business_domain_names_in_walker_module():
    """The walker module must contain ZERO business-domain identifiers."""
    import pathlib
    path = pathlib.Path(__file__).parent.parent / "app" / "sql_model" / "derivation_walker.py"
    text = path.read_text(encoding="utf-8")
    forbidden = ("AVY_", "APACSH", "APASEC", "SHDW_", "CCAL_", "BKR_", "SDIRA")
    for token in forbidden:
        assert token not in text, (
            f"Business identifier '{token}' leaked into derivation_walker"
        )


def test_parse_failure_falls_back_to_empty_map():
    """Malformed SQL (unrecoverable even after preprocessing) -> empty
    derivation map (no crash, no silent passthrough garbage)."""
    sql = "this is not valid SQL at all"
    m = _make_model([(1, "STG", sql)])
    enrich_model(m)
    # No columns recovered for this step.
    # (Other steps could still populate, but here there's only one.)
    assert m.column_derivations == {}
