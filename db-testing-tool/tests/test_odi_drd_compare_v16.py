"""Phase 0 tests for the in-memory v16.6 generic rule-proof engine port.

Asserts the engine reproduces the numbers measured directly from the standalone
v16.6 tool on the AVY v1->v2 fixtures (8 base fix-candidates -> 2 GENERIC
proof promotions: SDIRA_TXN_TP + SDIRA_TXN_TP_CD), the single-ODI v15-only
fallback, the empty-input guards, and that the proof layer carries no fixture
hardcodes.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services import odi_drd_compare_v16 as v16

_REPO = Path(__file__).resolve().parents[1]
_TX = _REPO / "data" / "taxlot"
_DRD = _TX / "DRD_Activity_Fact.xlsx"
_ODI1 = _TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
_ODI2 = _TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001_v2.xml"

_HAVE_FIXTURES = _DRD.exists() and _ODI1.exists() and _ODI2.exists()
_avy = pytest.mark.skipif(not _HAVE_FIXTURES, reason="AVY taxlot fixtures not present")

# AVY override layout (the standalone README layout for the big AVY workbook).
_OVERRIDES = dict(
    profile="auto",
    target_table="AVY_FACT",
    mapping_sheet="Table-View",
    target_col="B",
    source_cols="Y,Z,AA",
    rule_col="AD",
)


def _b(p: Path) -> bytes:
    return p.read_bytes()


@_avy
def test_two_odi_delta_reproduces_measured_counts():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_DELTA
    counts = res["summary"]["delta_status_counts"]
    # Measured directly from the standalone v16.6 tool on these fixtures.
    assert counts == {
        "UPSTREAM_CHANGED_NO_FINAL_MISMATCH": 276,
        "UNCHANGED": 84,
        "STILL_OPEN": 5,
        "FIX_CANDIDATE_UPSTREAM_CHANGED": 6,
        "FIXED_BY_RESOLVED_RULE_PROOF": 2,
    }, counts


@_avy
def test_generic_proof_promotes_sdira_pair_not_db_card():
    """The GENERIC (no-hardcode) proof promotes the SDIRA pair -- NOT the
    DB_CARD pair that the old v16.5 hardcoded booster promoted."""
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    promoted = sorted(res["summary"]["fixed_by_resolved_rule_proof"])
    assert promoted == ["SDIRA_TXN_TP", "SDIRA_TXN_TP_CD"], promoted
    # DB_CARD columns must NOT be promoted by the generic layer.
    assert "DB_CARD_TXN_DT" not in promoted
    assert "DB_CARD_ORIG_CCY_CD" not in promoted


@_avy
def test_proof_rows_present_with_evidence():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    proof = {r["target_column"]: r for r in res["proof"]}
    # The 8 base fix-candidates each get a proof row.
    assert len(proof) == 8, sorted(proof)
    sdira = proof["SDIRA_TXN_TP"]
    assert sdira["original_proof_passed"] == "N"
    assert sdira["fixed_proof_passed"] == "Y"
    assert sdira["fixed_evidence_excerpt"]  # non-empty evidence
    assert isinstance(sdira["fixed_checks"], list) and sdira["fixed_checks"]


@_avy
def test_single_odi_is_v15_only_no_delta_fields():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), None, **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_V15_ONLY
    assert "v15_by_target" in res and res["v15_by_target"]
    # Delta/proof fields must be OMITTED (not empty-but-present).
    assert "delta" not in res
    assert "proof" not in res


@_avy
def test_profile_resolved_surfaced():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    assert res["profile_resolved"] == "avy"  # AVY_FACT target -> avy profile


def test_empty_drd_raises():
    with pytest.raises(ValueError):
        v16.compare_two_odi_against_drd(b"", b"<x/>", b"<x/>")


def test_empty_odi1_raises():
    with pytest.raises(ValueError):
        v16.compare_two_odi_against_drd(b"PK\x03\x04fake", b"", None)


def test_no_fixture_hardcodes_in_proof_layer():
    """The ported proof layer must contain no business/fixture tokens
    (matches the v16.6 NO_HARDCODE audit)."""
    src = (_REPO / "app" / "services" / "odi_drd_compare_v16.py").read_text(encoding="utf-8")
    # Strip comments/docstrings via AST so prose mentions don't false-positive,
    # then scan string + name tokens.
    tree = ast.parse(src)
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value.upper())
        elif isinstance(node, ast.Name):
            literals.append(node.id.upper())
    blob = " ".join(literals)
    for token in ["SDIRA", "DB_CARD", "CL_VAL_SDIRA", "SRC_STM_ID = 60", "AVY_FACT_SIDE"]:
        assert token not in blob, f"fixture hardcode leaked into proof layer: {token}"


def test_args_shim_header_row_defaults_none():
    a = v16._args_shim(profile="auto")
    assert a.header_row is None  # auto-detect; 0/'' would mis-detect
    assert a.profile == "auto"
