"""Generic tests for the shared DRD rule engine.

Operator-locked: NO real-DRD table / column / business-domain names appear
in these fixtures.  The rules are pure pattern detection; we prove that by
exercising them with arbitrary placeholder identifiers like ``WIDGET42`` and
``foo_alias.bar_col``.
"""
from __future__ import annotations

import pytest

from app.sql_model.drd_rules import (
    DEFAULT_ETL_COLUMN_VALUES,
    compose_case_when_expr,
    compose_exists_case_expr,
    compute_drd_expected_expr,
    extract_applicable_only_code,
    extract_exists_derived_flag,
    find_discriminator_for_code,
)


# ── extract_applicable_only_code ──────────────────────────────────────────────

def test_applicable_only_for_basic():
    assert extract_applicable_only_code("Applicable only for WIDGET42") == "WIDGET42"


def test_applicable_only_to_alternate_wording():
    assert extract_applicable_only_code("This rule is applicable only to GADGET07") == "GADGET07"


def test_case_insensitive():
    assert extract_applicable_only_code("APPLICABLE ONLY FOR ZED99") == "ZED99"


def test_no_match_returns_none():
    assert extract_applicable_only_code("plain text without any code") is None
    assert extract_applicable_only_code("") is None
    assert extract_applicable_only_code(None) is None


# ── find_discriminator_for_code ───────────────────────────────────────────────

def test_discriminator_via_regexp_like_arbitrary_names():
    body = "Check regexp_like(foo.bar_col, '^WIDGET[0-9]+')"
    assert find_discriminator_for_code(body, "WIDGET42") == ("foo", "bar_col")


def test_discriminator_via_equality_arbitrary_names():
    body = "filter where some_alias.kind_col = 'GADGET07' applies"
    assert find_discriminator_for_code(body, "GADGET07") == ("some_alias", "kind_col")


def test_discriminator_via_between_arbitrary_names():
    body = "WHEN  thing.code_col  between 'ZED01' and 'ZED99' THEN ..."
    assert find_discriminator_for_code(body, "ZED50") == ("thing", "code_col")


def test_discriminator_no_match_returns_none():
    body = "WHEN unrelated.col = 'OTHER' THEN ..."
    assert find_discriminator_for_code(body, "WIDGET42") is None
    assert find_discriminator_for_code("", "WIDGET42") is None
    assert find_discriminator_for_code("anything", "") is None


def test_discriminator_prefix_must_be_string_prefix_of_code():
    """``^WIDGET`` matches ``WIDGET42`` but NOT ``OTHER42``."""
    body = "regexp_like(t.x, '^WIDGET')"
    assert find_discriminator_for_code(body, "WIDGET01") == ("t", "x")
    assert find_discriminator_for_code(body, "OTHER01") is None


# ── compose_case_when_expr ────────────────────────────────────────────────────

def test_compose_case_when_basic():
    sql = compose_case_when_expr(("a", "b"), "WIDGET42", "src_t.qty")
    assert sql == "CASE WHEN a.B = 'WIDGET42' THEN src_t.qty ELSE NULL END"


# ── compute_drd_expected_expr (end-to-end) ────────────────────────────────────

def test_compute_expected_expr_when_applicable_and_discriminator_present():
    row = {
        "transformation": "Apply per FOO logic. Applicable only for WIDGET42",
        "etl_block_body": "regexp_like(t.kind, '^WIDGET[0-9]+')",
    }
    expected = compute_drd_expected_expr(row, "", "t.amount")
    assert expected == "CASE WHEN t.KIND = 'WIDGET42' THEN t.amount ELSE NULL END"


def test_compute_expected_expr_uses_global_haystack_when_block_empty():
    row = {
        "transformation": "Applicable only for GADGET07",
        "etl_block_body": "",
    }
    haystack = "some.col between 'GADGET01' and 'GADGET99'"
    expected = compute_drd_expected_expr(row, haystack, "src.amt")
    assert expected == "CASE WHEN some.COL = 'GADGET07' THEN src.amt ELSE NULL END"


def test_compute_expected_expr_none_when_no_applicable_clause():
    row = {"transformation": "plain rule", "etl_block_body": "anything"}
    assert compute_drd_expected_expr(row, "", "src.x") is None


def test_compute_expected_expr_none_when_no_discriminator_found():
    row = {"transformation": "Applicable only for WIDGET42", "etl_block_body": ""}
    assert compute_drd_expected_expr(row, "", "src.x") is None


# ── DEFAULT_ETL_COLUMN_VALUES sanity ─────────────────────────────────────────

# ── EXISTS-derived flag detection ────────────────────────────────────────────

def test_extract_exists_derived_flag_generic_table():
    """Operator-shown DRD pattern: "If there is a record in <T> ... then set to '<V>'".
    Uses arbitrary placeholder identifiers (no real-DRD names)."""
    txt = (
        "If there is a record in OWNER.MY_LINK table with FOO.ID = BAR.FK_ID "
        "and STATUS_CD = 99 (Active) and BAR.ID <> BAR.PARENT_ID "
        "then set to 'Y' for both ends."
    )
    spec = extract_exists_derived_flag(txt)
    assert spec is not None
    assert spec["table"] == "OWNER.MY_LINK"
    assert spec["set_value"] == "Y"
    assert "FOO.ID = BAR.FK_ID" in spec["predicates"]
    assert "STATUS_CD = 99" in spec["predicates"]
    assert "BAR.ID <> BAR.PARENT_ID" in spec["predicates"]


def test_extract_exists_derived_flag_no_match():
    assert extract_exists_derived_flag("plain text rule") is None
    assert extract_exists_derived_flag("") is None
    assert extract_exists_derived_flag(None) is None


def test_extract_exists_derived_flag_alternate_wording():
    txt = "If a record exists in SCH.T with X = 1 then set to 'A'"
    spec = extract_exists_derived_flag(txt)
    assert spec is not None
    assert spec["table"] == "SCH.T"
    assert spec["set_value"] == "A"


def test_compose_exists_case_expr_with_default_else_null():
    spec = {"table": "X.Y", "predicates": ["a = b", "c = 1"], "set_value": "Z"}
    sql = compose_exists_case_expr(spec)
    assert sql == (
        "CASE WHEN EXISTS (SELECT 1 FROM X.Y WHERE a = b AND c = 1) "
        "THEN 'Z' ELSE NULL END"
    )


def test_compose_exists_case_expr_with_explicit_else_value():
    spec = {"table": "T", "predicates": ["a = 1"], "set_value": "Y"}
    sql = compose_exists_case_expr(spec, else_value="'N'")
    assert "THEN 'Y' ELSE 'N' END" in sql


def test_default_etl_columns_contains_audit_columns():
    """The default map should cover common audit columns -- but callers can
    override.  No specific business-domain entries."""
    for k in ("CRT_DTM", "LAST_UDT_DTM", "ACTV_F", "BATCH_DT"):
        assert k in DEFAULT_ETL_COLUMN_VALUES
