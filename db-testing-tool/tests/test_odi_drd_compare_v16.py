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
def test_single_odi_is_v16_drd_mode_no_delta_fields():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), None, **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_DELTA
    assert "v16_by_target" in res and res["v16_by_target"]
    assert res["bucket_counts"]["matched"] > 0
    assert res["sql"].strip()
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

_XML_DIFF_STATUSES = {"CHANGED", "UNCHANGED", "NO_RESOLVED_LINEAGE", "ONLY_IN_ODI1", "ONLY_IN_ODI2"}


@_avy
def test_mode1_odi_vs_drd_differences():
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), None, **_OVERRIDES)
    assert res["engine"] == v16.ENGINE_DELTA
    assert res["mode"] == v16.MODE_ODI_VS_DRD
    assert "v16_by_target" in res and res["v16_by_target"]
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


@_avy
def test_mode3_odi2_rows_follow_final_delta_status():
    """Resolved columns must not remain in active ODI2-vs-DRD rows.

    Single source of truth for mode3 issue state is delta_status, not the raw
    v15 mismatch class on ODI2.
    """
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    delta = res["delta"]
    resolved_cols = {
        r["target_column"]
        for r in delta
        if r.get("delta_status") in {
            "FIXED_BY_RESOLVED_RULE_PROOF",
            "FIXED_BY_FINAL_COMPARE",
            "UNCHANGED",
            "UPSTREAM_CHANGED_NO_FINAL_MISMATCH",
        }
    }
    odi2_active_cols = {r.get("target_column", "") for r in res.get("odi2_vs_drd", [])}
    assert not (resolved_cols & odi2_active_cols)
    assert isinstance(res.get("odi2_vs_drd_resolved"), list)
    unified = res.get("unified_issue_rows")
    assert isinstance(unified, list) and unified
    allowed_states = {"active", "candidate", "regression", "resolved", "unknown"}
    sample = unified[0]
    for k in (
        "target_column",
        "delta_status",
        "issue_state",
        "conclusion",
        "difference_type",
        "mapping_logic",
        "odi_logic",
        "recommended_action",
    ):
        assert k in sample
    assert {u.get("issue_state") for u in unified} <= allowed_states


@_avy
def test_mode3_odi_vs_odi_selected_set_matches_standalone():
    """The DRD review set (selected) reproduces the standalone's 8 CHANGED
    columns -- the original_vs_fixed_resolved_xml_delta.csv CHANGED rows."""
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    assert res.get("has_selection") is True
    diffs = res["differences"]
    selected = [d for d in diffs if d.get("selected")]
    cols = sorted(d["target_column"] for d in selected)
    assert cols == [
        "BKR_AR_ID", "DB_CARD_ORIG_CCY_CD", "DB_CARD_TXN_DT",
        "LGCY_TRD_CPCTY_TP_DIM_ID", "MM_ALT_ID",
        "SDIRA_TXN_TP", "SDIRA_TXN_TP_CD", "SDIRA_TXN_YR",
    ], cols
    # the resolvable side (ODI #1) reaches a real transform (CASE / lookup), not noise
    assert any("CASE" in (d["mapping_logic"] or "").upper() for d in selected)
    # "show all" is the full set, much larger than the selected review set
    assert len(diffs) > len(selected)


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


def test_target_load_sql_blocks_respects_final_steps_and_drops_runtime_noise():
    blocks = [
        {
            "step_no": "8",
            "task_no": "9",
            "sql": "INSERT INTO ODI_RUNTIME_VALUE.SSDS_SESS_LOG (SESS_NO) SELECT 1 FROM dual",
        },
        {
            "step_no": "16",
            "task_no": "17",
            "sql": "INSERT INTO ODI_RUNTIME_VALUE.AVY_FACT_LOG (TXN_ID) SELECT TXN_ID FROM ODI_RUNTIME_VALUE.J$AVY_FACT",
        },
        {
            "step_no": "120",
            "task_no": "5",
            "sql": "INSERT INTO AVY_FACT_SIDE (TXN_ID) SELECT TXN_ID FROM INLINE_VIEW_1",
        },
    ]
    final_lineage = [{"step_no": "120", "task_no": "5", "target_column": "TXN_ID"}]
    out = v16._target_load_sql_blocks(blocks, final_lineage, target_table_hint="AVY_FACT_SIDE")
    assert len(out) == 1
    assert out[0]["step_no"] == "120"
    assert "AVY_FACT_SIDE" in out[0]["sql"].upper()


def test_diffs_from_v15_mismatches_expands_grouped_rows_per_column_generic_names():
    mismatches = [
        {
            "Area / Columns": "`COL_ALPHA` `COL_BETA` `COL_GAMMA`",
            "Difference Type": "Transformation logic difference",
            "Mapping Logic": "DRD_GENERIC_RULE",
            "ODI XML Logic": "INLINE_VIEW.COL_ALPHA",
            "Conclusion": "generic grouped mismatch",
            "Recommended Action": "review",
        }
    ]
    mapping_by_target = {
        "COL_ALPHA": {"drd_rule": "RULE_ALPHA"},
        "COL_BETA": {"drd_rule": "RULE_BETA"},
        "COL_GAMMA": {"drd_rule": "RULE_GAMMA"},
    }
    resolved_by_target = {
        "COL_ALPHA": {
            "resolved_expression": "CASE WHEN A.K = 1 THEN 'X' END",
            "final_expression": "A.COL_ALPHA",
            "resolved_step": "23",
            "resolved_task": "100",
            "lineage_path": "step=23/task=100/col=COL_ALPHA/expr=CASE WHEN A.K = 1 THEN 'X' END",
        },
        "COL_BETA": {
            "resolved_expression": "B.COL_BETA",
            "final_expression": "B.COL_BETA",
            "resolved_step": "23",
            "resolved_task": "100",
            "lineage_path": "step=23/task=100/col=COL_BETA/expr=B.COL_BETA",
        },
        "COL_GAMMA": {
            "resolved_expression": "TO_CHAR(C.COL_GAMMA)",
            "final_expression": "C.COL_GAMMA",
            "resolved_step": "24",
            "resolved_task": "101",
            "lineage_path": "step=24/task=101/col=COL_GAMMA/expr=TO_CHAR(C.COL_GAMMA)",
        },
    }
    sql_by_step_task = {
        ("23", "100"): "SELECT A.COL_ALPHA, B.COL_BETA FROM SRC_A A JOIN SRC_B B ON A.ID=B.ID WHERE A.K=1",
        ("24", "101"): "SELECT TO_CHAR(C.COL_GAMMA) AS COL_GAMMA FROM SRC_C C",
    }

    rows = v16._diffs_from_v15_mismatches(
        mismatches,
        mapping_by_target,
        resolved_by_target,
        sql_by_step_task,
    )

    assert len(rows) == 3
    got_cols = sorted(r["target_column"] for r in rows)
    assert got_cols == ["COL_ALPHA", "COL_BETA", "COL_GAMMA"]
    for r in rows:
        assert r["Area / Columns"] == r["target_column"]
        assert "Column:" in (r.get("ODI XML Logic") or "")
        assert "Step" in (r.get("ODI XML Logic") or "")


def test_build_odi_column_trace_is_generic_not_business_specific():
    trace = v16._build_odi_column_trace(
        "GENERIC_COL_X",
        {
            "final_expression": "X.GENERIC_COL_X",
            "resolved_expression": "CASE WHEN X.FLAG = 1 THEN X.GENERIC_COL_X END",
            "resolved_step": "17",
            "resolved_task": "42",
            "lineage_path": "step=17/task=42/col=GENERIC_COL_X/expr=CASE WHEN X.FLAG = 1 THEN X.GENERIC_COL_X END",
        },
        "SELECT X.GENERIC_COL_X FROM SRC_X X LEFT JOIN DIM_Y Y ON X.Y_ID=Y.ID WHERE X.FLAG=1",
        "DRD says derive from SRC_X with lookup DIM_Y",
        "INLINE_VIEW.GENERIC_COL_X",
    )

    assert "Column: GENERIC_COL_X" in trace
    assert "Resolved at: Step 17 / Task 42" in trace
    assert "Lookup/join/filter context:" in trace
    assert "DRD rule context:" in trace


@_avy
def test_mode3_unified_rows_enriched_for_sdira_candidates():
    """Columns present in delta but absent from odi2 mismatches must still carry
    useful DRD/ODI logic (not empty cells)."""
    res = v16.compare_two_odi_against_drd(_b(_DRD), _b(_ODI1), _b(_ODI2), **_OVERRIDES)
    rows = {r.get("target_column", ""): r for r in (res.get("unified_issue_rows") or [])}
    row = rows.get("SDIRA_TXN_TP")
    assert row is not None
    assert (row.get("delta_status") or "") in {
        "FIX_CANDIDATE_UPSTREAM_CHANGED",
        "FIXED_BY_RESOLVED_RULE_PROOF",
        "FIXED_BY_FINAL_COMPARE",
        "STILL_OPEN",
    }
    assert (row.get("mapping_logic") or "").strip(), "SDIRA mapping logic must not be empty"
    assert (row.get("odi_logic") or "").strip(), "SDIRA ODI logic must not be empty"
