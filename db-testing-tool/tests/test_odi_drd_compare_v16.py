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
    # No DRD + 2 (dummy) ODIs -> Mode 2; the dummy XML yields no lineage -> raise.
    with pytest.raises(ValueError):
        v16.compare_two_odi_against_drd(b"", b"<x/>", b"<x/>")


def test_no_drd_no_odi2_raises():
    # Neither a DRD nor a 2nd ODI -> nothing to compare against -> raise.
    with pytest.raises(ValueError):
        v16.compare_two_odi_against_drd(None, b"<x/>", None)


def test_empty_odi1_raises():
    with pytest.raises(ValueError):
        v16.compare_two_odi_against_drd(b"PK\x03\x04fake", b"", None)


# ---------------------------------------------------------------------------
# Multi-mode (2026-06-07): Mode 1 ODI-vs-DRD, Mode 2 ODI-vs-ODI (DRD optional),
# both-mode per-ODI-vs-DRD enrichment.
# ---------------------------------------------------------------------------

_XML_DIFF_STATUSES = {"LOGIC_CHANGED", "RESTRUCTURED", "STRUCTURE", "ONLY_IN_ODI1", "ONLY_IN_ODI2"}


@_avy
def test_mode1_odi_vs_drd_differences():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), None, **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_V15_ONLY
    assert res["mode"] == v16.MODE_ODI_VS_DRD
    assert "v15_by_target" in res and res["v15_by_target"]
    diffs = res["differences"]
    assert isinstance(diffs, list) and diffs, "Mode 1 must surface ODI-vs-DRD discrepancies"
    d = diffs[0]
    assert d["mapping_logic_label"] == "DRD"
    assert d["odi_logic_label"] == "ODI"
    assert "delta" not in res and "proof" not in res


@_avy
def test_mode2_odi_vs_odi_no_drd_full_blocks():
    """ODI #1 vs ODI #2 with NO DRD: pure code-vs-code, honest classification,
    and FULL (untruncated) blocks where they differ."""
    res = v16.compare_two_odi_against_drd(None, _b(_ODI1), _b(_ODI2))
    assert res["engine"] == v16.ENGINE_ODI_VS_ODI
    assert res["mode"] == v16.MODE_ODI_VS_ODI
    diffs = res["differences"]
    assert isinstance(diffs, list) and diffs
    assert all(d["status"] in _XML_DIFF_STATUSES for d in diffs), \
        sorted({d["status"] for d in diffs})
    assert diffs[0]["mapping_logic_label"] == "ODI #1"
    assert diffs[0]["odi_logic_label"] == "ODI #2"
    # full blocks where they differ -- untruncated (the inline-view block is large)
    blocks = res["sql_block_diff"]
    assert isinstance(blocks, list) and blocks
    assert all(b["sql_delta_status"] != "UNCHANGED" for b in blocks)
    longest = max(len(b["original_sql_excerpt"]) + len(b["fixed_sql_excerpt"]) for b in blocks)
    assert longest > 2000, "blocks must be FULL (untruncated), not 2000-char excerpts"


@_avy
def test_mode2_drd_is_optional():
    # No raise when DRD is omitted but a 2nd ODI is supplied.
    res = v16.compare_two_odi_against_drd(None, _b(_ODI1), _b(_ODI2))
    assert res["mapping_rows"] == 0
    assert res["summary"]["changed_blocks"] >= 1


@_avy
def test_both_mode_per_odi_vs_drd_carry_each_odi_logic():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_DELTA
    assert res["mode"] == v16.MODE_ODI_VS_ODI_WITH_DRD
    o1 = res["odi1_vs_drd"]
    o2 = res["odi2_vs_drd"]
    assert isinstance(o1, list) and isinstance(o2, list) and o1 and o2
    # Each list carries that ODI's OWN resolved logic -> the two versions differ
    # even when their v15-final mismatch classes coincide (upstream-only change).
    sig1 = [(d["target_column"], d.get("odi_resolved_logic", "")) for d in o1]
    sig2 = [(d["target_column"], d.get("odi_resolved_logic", "")) for d in o2]
    assert sig1 != sig2, "two ODI versions must surface different logic vs the same DRD"
    # delta/proof path preserved (parity with the standalone counts).
    assert "delta" in res and "proof" in res


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
