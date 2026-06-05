"""Regression: recover a transformation/lookup rule a DRD author misplaced in the
"Nullable?" column (C25) instead of the "Transformation" column (C26).

Operator 2026-06-04: the CLOSE taxlot DRD puts the CL_VAL scheme rules
("Use SUB_LOT_TXN_EV_TP_ID under CL_VAL_ID where CL_SCM_ID = 86 and pick
CL_VAL_NM") in the Nullable column for several CL_VAL target columns.  The
parser read transformation strictly from C26 and silently dropped the rule, so
the generated INSERT could never honor the scheme -- which made it look like the
"DRD was under-specified" when in fact the DRD carried the rule all along.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.drd_import_service import _is_misplaced_transformation_prose

_ROOT = Path(__file__).resolve().parents[1]
_CLOSE_DRD = _ROOT / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"


def test_is_misplaced_prose_unit():
    # Real Y/N-style nullable flags are NOT misclassified as transformation.
    for flag in ("", "Y", "N", "Yes", "No", "TRUE", "false", "NULL", "Not Null", "1", "0", "N/A"):
        assert _is_misplaced_transformation_prose(flag) is False, flag
    # Prose / lookup rules ARE recovered.
    assert _is_misplaced_transformation_prose("Use SUB_LOT_TXN_EV_TP_ID under CL_VAL_ID where CL_SCM_ID = 86 and pick CL_VAL_NM")
    assert _is_misplaced_transformation_prose("Use SRC_RCRD_TP_ID in CL_VAL and use- CL_VAL_CODE")
    assert _is_misplaced_transformation_prose("Lookup on IMT_PD_DIM")  # has whitespace
    assert _is_misplaced_transformation_prose("CL_SCM_ID=84")          # keyword, no space


pytestmark_drd = pytest.mark.skipif(
    not _CLOSE_DRD.exists(), reason="taxlot CLOSE DRD fixture missing"
)


@pytestmark_drd
def test_close_drd_recovers_cl_val_scheme_rules():
    """The two CL_VAL columns whose rule the author put in the Nullable column
    must now carry that rule in `transformation`; a column with a correctly
    placed rule is unaffected."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    rows = {(r.get("column") or "").upper(): r for r in res.get("analysis_rows", [])}

    cls = (rows.get("CLS_TXN_EV_TP", {}).get("transformation") or "").upper()
    assert "CL_SCM_ID = 86" in cls or "CL_SCM_ID=86" in cls, cls
    assert "CL_VAL_NM" in cls, cls

    src = (rows.get("SRC_RCRD_TP_CD", {}).get("transformation") or "").upper()
    assert "CL_VAL_CODE" in src, src

    # correctly-placed rule (C26) is untouched
    opn = (rows.get("OPN_TXN_EV_TP", {}).get("transformation") or "").upper()
    assert "ALWAYS NULL" in opn, opn


@pytestmark_drd
def test_conditional_rule_not_collapsed_to_constant():
    """A conditional rule ("if ZERO_BSS_IND is 01 then set to Y else N") must NOT
    be collapsed to the bare constant 'Y' by the constant-rule extractor -- that
    drops the `else N` branch.  Regression for the 2026-06-04 over-fire."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    sql = (res.get("generated_insert_sql") or "").upper()
    # the buggy collapse produced exactly "'Y' AS ZERO_COST_BSS_F"
    assert "'Y' AS ZERO_COST_BSS_F" not in sql, "constant-rule wrongly collapsed a conditional to 'Y'"
    # the conditional must now be emitted as a full CASE on the real source column
    flat = re.sub(r"\s+", " ", sql)
    assert "CASE WHEN" in flat and "ZERO_BSS_IND_F = '01' THEN 'Y' ELSE 'N'" in flat, \
        "expected CASE WHEN ZERO_BSS_IND_F = '01' THEN 'Y' ELSE 'N' for ZERO_COST_BSS_F"


@pytestmark_drd
def test_target_only_column_resolved_to_source_in_expr():
    """A CASE/arithmetic that the DRD wrote with a TARGET column name (EXG_RATE)
    must read the SOURCE column (SBC_EXG_RATE) -- EXG_RATE does not exist in the
    source table, so a source-qualified `...TGT.EXG_RATE` would be ORA-00904.
    (operator 2026-06-04)."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    sql = (res.get("generated_insert_sql") or "").upper()
    # no source-qualified reference to the target-only column EXG_RATE
    assert "RJTRUST_TGT.EXG_RATE" not in sql, "target-only EXG_RATE leaked as a source column"
    # the real source column is used instead
    assert "SBC_EXG_RATE" in sql, "expected source column SBC_EXG_RATE in the FX-conversion CASEs"


@pytestmark_drd
def test_leading_placeholder_resolves_to_row_source():
    """A cost/factor CASE that leads with a logical placeholder ("ORIG_COST
    COST_AMT, case ... THEN ORIG_COST ...") must resolve ORIG_COST to the row's
    own source column (COST_AMT -> NML_CCY_OPN_COST_AMT), not leave the
    placeholder as a phantom source column.  (operator 2026-06-04)."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    sql = (res.get("generated_insert_sql") or "").upper()
    # the placeholder token must be fully resolved away
    assert "ORIG_COST" not in sql, "leading placeholder ORIG_COST leaked into the INSERT"
    assert "NML_CCY_OPN_COST_AMT" in sql, "expected COST_AMT's real source column"


@pytestmark_drd
def test_bare_lookup_alias_rewritten_to_join_alias():
    """A projection that references a single-joined lookup table by its BARE name
    (SRC_STM_DIM.SRC_STM_CD) must be rewritten to the real join alias
    (SRC_STM_DIM_1.SRC_STM_CD) -- otherwise it is an undefined alias at runtime.
    (operator 2026-06-04)."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    sql = (res.get("generated_insert_sql") or "").upper()
    # bare-alias projection must be gone; the renamed join alias is used instead
    assert "SRC_STM_DIM.SRC_STM" not in sql, "bare SRC_STM_DIM alias leaked (undefined at runtime)"
    assert "SRC_STM_DIM_1.SRC_STM" in sql, "expected the renamed join alias SRC_STM_DIM_1"


@pytestmark_drd
def test_dim_source_gets_natural_key_join():
    """A row whose source is a dimension table with no explicit DRD join must get
    a derived natural-key join like ODI's (ACG_TP_DIM.ACG_TP_CD =
    source.ACG_TP_CODE), not collapse to NULL.  (operator 2026-06-04)."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    flat = re.sub(r"\s+", " ", (res.get("generated_insert_sql") or "").upper())
    # the dim is joined on the natural key, exactly like ODI
    assert "LEFT JOIN COMMON_OWNER.ACG_TP_DIM" in flat, "ACG_TP_DIM not joined"
    assert "ACG_TP_CD = CLOSE_TAX_LOT_NONBKR_RJTRUST_TGT.ACG_TP_CODE" in flat, \
        "expected natural-key join ACG_TP_DIM.ACG_TP_CD = source.ACG_TP_CODE"
    # the dim projections resolve to the join alias, not NULL
    assert "NULL AS ACG_TP_ID" not in flat and "NULL AS ACG_TP_NM" not in flat, "ACG dim projection collapsed to NULL"
    assert "ACG_TP_DIM_1.ACG_TP_ID AS ACG_TP_ID" in flat, "ACG_TP_ID not projected from the join alias"


_OPEN_DRD = _ROOT / "data" / "taxlot" / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_open_multijoin_dim_projection_not_bare():
    """OPEN joins SRC_STM_DIM / ACG_TP_DIM more than once.  The projection must
    reference the suffixed join alias (SRC_STM_DIM_1), never the bare name --
    which is undefined when the dim is joined multiple times (ORA-00904).  The
    alias-realignment must not strip a source table's own numbered join alias.
    (operator 2026-06-05)."""
    from app.services.control_table_service import analyze_control_table
    try:
        res = analyze_control_table(
            file_bytes=_OPEN_DRD.read_bytes(), filename=_OPEN_DRD.name,
            target_schema="TAXLOT_OWNER", target_table="OPN_TAX_LOTS_NON_BKR_FACT",
            source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
        )
    except Exception as exc:  # PDM (ds_2) not present in this env
        pytest.skip(f"OPEN target not resolvable: {exc}")
    sql = (res.get("generated_insert_sql") or "").upper()
    assert "SRC_STM_DIM.SRC_STM" not in sql, "bare SRC_STM_DIM projection leaked (undefined alias)"
    assert "ACG_TP_DIM.ACG_TP" not in sql, "bare ACG_TP_DIM projection leaked (undefined alias)"


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_open_fx_placeholder_resolves_to_drd_currency_source():
    """The DRD FX-conversion CASEs write a generic ``CCY_CODE`` placeholder.  It
    must resolve to THIS DRD's currency-code source column -- CCY_CD's source
    attribute, which is STM_BASE_ISO_CCY_CODE for OPEN -- the way ODI does, NOT a
    hardcoded NML_ISO_CCY_CODE.  (operator 2026-06-05: OPEN deeper)"""
    from app.services.control_table_service import analyze_control_table
    try:
        res = analyze_control_table(
            file_bytes=_OPEN_DRD.read_bytes(), filename=_OPEN_DRD.name,
            target_schema="TAXLOT_OWNER", target_table="OPN_TAX_LOTS_NON_BKR_FACT",
            source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
        )
    except Exception as exc:  # PDM (ds_2) not present in this env
        pytest.skip(f"OPEN target not resolvable: {exc}")
    flat = re.sub(r"\s+", "", (res.get("generated_insert_sql") or "").upper())
    assert "STM_BASE_ISO_CCY_CODE='USD'" in flat, \
        "FX CASE did not resolve CCY_CODE to the DRD currency source col"
    assert "NML_ISO_CCY_CODE='USD'" not in flat, \
        "FX CASE still uses the old hardcoded NML_ISO_CCY_CODE"


@pytest.mark.skipif(not _OPEN_DRD.exists(), reason="OPEN DRD fixture missing")
def test_open_prose_alias_case_rewritten_to_staging():
    """DRD CASEs that reference a prose/logical source name (TAX_LOT_OPN_MSTR /
    TAXLOT_DTL_OPN) must be rewritten onto the real staging table when the column
    exists there (the way ODI does), not dropped to a bare column / NULL by the
    is_safe guard.  MISS_COST_BSS_F / MISS_IVS_COST_F / COST_BSS_LEGS_CVR_F each
    carry such a CASE.  (operator 2026-06-05: OPEN deeper, item B)"""
    from app.services.control_table_service import analyze_control_table
    try:
        res = analyze_control_table(
            file_bytes=_OPEN_DRD.read_bytes(), filename=_OPEN_DRD.name,
            target_schema="TAXLOT_OWNER", target_table="OPN_TAX_LOTS_NON_BKR_FACT",
            source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
        )
    except Exception as exc:  # PDM (ds_2) not present in this env
        pytest.skip(f"OPEN target not resolvable: {exc}")
    sql = (res.get("generated_insert_sql") or "").upper()
    flat = re.sub(r"\s+", " ", sql)
    # each prose-alias column now emits a real CASE, not a bare column / NULL
    for col in ("MISS_COST_BSS_F", "MISS_IVS_COST_F", "COST_BSS_LEGS_CVR_F"):
        assert re.search(rf"\bEND\s+AS\s+{col}\b", flat), \
            f"{col} did not emit a CASE (prose-alias rewrite dropped it)"
    # the prose/logical source names must NOT leak into the emitted SQL
    assert "TAX_LOT_OPN_MSTR." not in sql, "prose alias TAX_LOT_OPN_MSTR leaked into INSERT"
    assert "TAXLOT_DTL_OPN." not in sql, "prose alias TAXLOT_DTL_OPN leaked into INSERT"


@pytestmark_drd
def test_close_fx_placeholder_still_resolves_to_its_currency_source():
    """The generic CCY_CODE resolution must NOT regress CLOSE: CLOSE's CCY_CD
    source attribute is NML_ISO_CCY_CODE, so CLOSE's FX CASEs must still use it
    (removing the hardcode is behaviour-preserving for CLOSE)."""
    from app.services.control_table_service import analyze_control_table
    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(), filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
    )
    flat = re.sub(r"\s+", "", (res.get("generated_insert_sql") or "").upper())
    assert "NML_ISO_CCY_CODE='USD'" in flat, \
        "CLOSE FX CASE lost its currency source col (regression)"


# ---------------------------------------------------------------------------
# Shared DRD-prose interpreters: the comparison BASELINE (drd_expression) must
# honor the SAME rules the emitter does, so a correctly-generated CASE/literal
# does NOT show as a false GENERATED_MISMATCH against a raw-source-column
# baseline.  (operator 2026-06-05: "посмотри ZERO_COST_BSS_F ... используй этот
# модуль сравнения для сгенерированных инсертов")
# ---------------------------------------------------------------------------


def _comparison_row(res, col: str):
    for r in (res.get("comparison", {}) or {}).get("rows", []) or []:
        if (r.get("column") or "").upper() == col.upper():
            return r
    return None


def test_module_level_prose_interpreters_are_importable_and_pure():
    """The interpreters are module-level (shared by emitter + baseline), pure,
    and generic: flag/constant prose -> SQL, but lookup/conditional prose is NOT
    collapsed to a literal."""
    from app.services.control_table_service import (
        _extract_if_then_else_case,
        _extract_constant_rule_expr,
        _extract_default_expr,
    )
    # flag conditional -> CASE on the row's real source column
    assert _extract_if_then_else_case(
        "if ZERO_BSS_IND is 01 then set to Y else N", "ZERO_BSS_IND_F", "S"
    ) == "CASE WHEN S.ZERO_BSS_IND_F = '01' THEN 'Y' ELSE 'N' END"
    # constant rule -> resolved literal
    assert _extract_constant_rule_expr("Use value- Closed") == "'Closed'"
    assert _extract_constant_rule_expr("populate as 6") == "6"
    assert _extract_constant_rule_expr("Always NULL") == "NULL"
    # GUARD: a lookup-phrased rule needs a real join -> NOT collapsed to a literal
    assert _extract_constant_rule_expr("Use SRC_RCRD_TP_ID in CL_VAL and get code") is None
    assert _extract_constant_rule_expr("look up CL_VAL_NM") is None
    # GUARD: a conditional is NOT a pure constant
    assert _extract_constant_rule_expr("if X is 01 then set to Y else N") is None
    # DEFAULT clause prose
    assert _extract_default_expr("AUDIT COLUMN. DEFAULT SYSDATE") == "SYSDATE"
    assert _extract_default_expr("plain source column") is None


@pytestmark_drd
def test_close_baseline_flag_conditional_matches_generated():
    """ZERO_COST_BSS_F: DRD prose "if ZERO_BSS_IND is 01 then set to Y else N".
    The drd_expression BASELINE must now be the CASE (not the raw source column)
    so it MATCHES the (correct) generated CASE instead of a false mismatch."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(), filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
    )
    row = _comparison_row(res, "ZERO_COST_BSS_F")
    assert row is not None, "ZERO_COST_BSS_F missing from comparison"
    drd_u = re.sub(r"\s+", " ", (row.get("drd_expression") or "").upper())
    assert "CASE WHEN" in drd_u and "ZERO_BSS_IND_F = '01'" in drd_u, \
        f"baseline did not interpret the flag prose into a CASE: {drd_u!r}"
    assert row.get("status") == "match_all", \
        f"expected match_all (baseline CASE == generated CASE), got {row.get('status')!r}"
    assert row.get("is_real_difference") is False


@pytestmark_drd
def test_close_baseline_constant_rule_matches_generated():
    """POS_CLS_TP: DRD prose "Use value- Closed".  The drd_expression baseline
    must be the resolved literal 'Closed' (not the raw OPN_CLS column) so it
    MATCHES the generated 'Closed' -- reconcile must NOT revert the literal."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(), filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2, target_datasource_id=2, control_schema="ikorostelev",
    )
    row = _comparison_row(res, "POS_CLS_TP")
    assert row is not None, "POS_CLS_TP missing from comparison"
    assert (row.get("drd_expression") or "").strip().upper() == "'CLOSED'", \
        f"baseline did not resolve the constant rule: {row.get('drd_expression')!r}"
    assert row.get("status") == "match_all", \
        f"expected match_all (baseline 'Closed' == generated 'Closed'), got {row.get('status')!r}"
    assert row.get("is_real_difference") is False
