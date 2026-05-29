"""Tests for the chain safety-scan in generate_final_mismatch_report.

Operator (2026-05-29) added Q1: "always check the entire ODI code, all
steps, even if not needed, as a safety check".  Q3 was the specific bug:
TXN_CCY has STEP3=NULL and STEP5=CCY.CCY_NM; comparator says MATCHED
but the chain inconsistency was silently hidden.
"""
from __future__ import annotations

import pathlib
import sys

# Add scripts/ to sys.path so we can import the generator module under test
_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO))

from app.sql_model.types import ColumnDerivation  # noqa: E402
from generate_final_mismatch_report import (  # noqa: E402
    _CAT_NULL_INJECTED_THEN_OVERWRITTEN,
    _scan_chain_for_warnings,
)


def _mk(
    label: str, step_id: int, expr: str, kind: str,
    alias: str = "", col: str = "", authoritative: bool = False,
) -> ColumnDerivation:
    return ColumnDerivation(
        step_label=label, step_id=step_id, expr_sql=expr, expr_kind=kind,
        is_authoritative=authoritative, source_alias=alias, source_col=col,
    )


def test_txn_ccy_pattern_null_step3_then_real_step5():
    """Mirrors the operator's screenshot: STEP3 writes literal NULL,
    STEP5 writes CCY.CCY_NM.  Safety scan must flag this."""
    chain = [
        _mk("STEP3", 3, "NULL", "literal", authoritative=True),
        _mk("STEP4", 4, "AVY_FACT_STEP3_STG_RT.TXN_CCY", "passthrough"),
        _mk("STEP5", 5, "CCY.CCY_NM", "column_ref", alias="CCY", col="CCY_NM"),
        _mk("MERGE_USING", 98, "AVY_FACT_STEP5_STG_RT.TXN_CCY", "passthrough"),
        _mk("MERGE", 99, "S.TXN_CCY", "column_ref", alias="S", col="TXN_CCY"),
    ]
    warnings = _scan_chain_for_warnings("TXN_CCY", chain)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["kind"] == _CAT_NULL_INJECTED_THEN_OVERWRITTEN
    assert w["null_step"] == "STEP3"
    assert w["real_step"] == "STEP5"
    assert "CCY.CCY_NM" in w["real_expr"]


def test_chain_with_no_null_emits_no_warning():
    """Clean column_ref chain across steps -- no warning."""
    chain = [
        _mk("STEP1", 1, "APA.STM_BASE_CCY_AMT", "column_ref",
            alias="APA", col="STM_BASE_CCY_AMT", authoritative=True),
        _mk("STEP3", 3, "APA_CASH.SBC_AMT", "passthrough"),
        _mk("MERGE", 99, "S.CASH_SBC_AMT", "column_ref",
            alias="S", col="CASH_SBC_AMT"),
    ]
    assert _scan_chain_for_warnings("CASH_SBC_AMT", chain) == []


def test_chain_with_null_in_merge_only_does_not_warn():
    """MERGE-bucket NULL/literal entries are excluded -- they reflect
    downstream consumption, not real derivation."""
    chain = [
        _mk("STEP3", 3, "TXN.SOMETHING", "column_ref",
            alias="TXN", col="SOMETHING", authoritative=True),
        _mk("MERGE", 99, "NULL", "literal"),  # would never normally occur but covers the filter
    ]
    assert _scan_chain_for_warnings("SOMETHING", chain) == []


def test_chain_with_only_null_no_real_does_not_warn():
    """If the ENTIRE chain is NULL/literal/passthrough with no real
    later derivation, there's no inconsistency to flag -- it's a
    different kind of issue (SOURCE_MISSING / UNRESOLVABLE)."""
    chain = [
        _mk("STEP3", 3, "NULL", "literal", authoritative=True),
        _mk("STEP4", 4, "AVY_FACT_STEP3_STG_RT.X", "passthrough"),
        _mk("MERGE", 99, "S.X", "column_ref", alias="S", col="X"),
    ]
    assert _scan_chain_for_warnings("X", chain) == []


def test_chain_with_null_AFTER_real_does_not_warn():
    """If NULL is LATER than the real source, the real one is in an
    earlier step and gets overwritten by NULL -- different pattern.
    Our scan looks for NULL-then-real (the operator's case)."""
    chain = [
        _mk("STEP1", 1, "APA.X", "column_ref", alias="APA", col="X"),
        _mk("STEP3", 3, "NULL", "literal", authoritative=True),
    ]
    assert _scan_chain_for_warnings("X", chain) == []


def test_recommendation_text_mentions_steps_and_expression():
    """Warning recommendation must be actionable: name the NULL step,
    the real step, and quote the real expression."""
    chain = [
        _mk("STEP3", 3, "NULL", "literal", authoritative=True),
        _mk("STEP5", 5, "CCY.CCY_NM", "column_ref", alias="CCY", col="CCY_NM"),
    ]
    w = _scan_chain_for_warnings("TXN_CCY", chain)[0]
    assert "STEP3" in w["recommendation"]
    assert "STEP5" in w["recommendation"]
    assert "NULL" in w["recommendation"]
    assert "CCY.CCY_NM" in w["recommendation"]


def test_empty_chain_returns_empty_warnings():
    assert _scan_chain_for_warnings("ANY", []) == []
