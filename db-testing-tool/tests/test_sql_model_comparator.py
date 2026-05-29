"""Tests for app.sql_model.comparator — semantic 3-way DRD vs ODI comparator."""
from __future__ import annotations

import pytest

from app.sql_model.comparator import (
    ComparisonResult,
    DrdClaim,
    compare_drd_odi,
    compare_drd_rows_to_model,
    comparison_summary,
)
from app.sql_model.types import (
    AliasBinding,
    ColumnMapping,
    ComparisonVerdict,
    MismatchKind,
    ODIModel,
    Provenance,
    ResolvedColumn,
    StagingStep,
    TableRef,
    UnresolvedExpr,
)


# ── Test fixture builders ─────────────────────────────────────────────────────

def _make_table(schema: str, table: str) -> TableRef:
    return TableRef(schema=schema, table=table)


def _make_binding(alias: str, schema: str, table: str) -> AliasBinding:
    return AliasBinding(alias=alias, ref=_make_table(schema, table))


def _make_resolved(alias: str, col: str, schema: str, table: str) -> ResolvedColumn:
    return ResolvedColumn(
        expr_sql=f"{alias}.{col}",
        provenance=Provenance.ODI,
        ref=_make_table(schema, table),
        column=col.upper(),
        original_expr=f"{alias}.{col}",
    )


def _make_literal(expr: str) -> ResolvedColumn:
    return ResolvedColumn(
        expr_sql=expr,
        provenance=Provenance.LITERAL,
        ref=None,
        column="",
        original_expr=expr,
    )


def _make_complex(expr: str) -> ResolvedColumn:
    return ResolvedColumn(
        expr_sql=expr,
        provenance=Provenance.ODI,
        ref=None,
        column="",
        original_expr=expr,
    )


def _make_unresolved(expr: str, reason: str) -> UnresolvedExpr:
    return UnresolvedExpr(original_expr=expr, reason=reason, detail="test detail")


def _make_model_with_step1(column_mappings, source_bindings=None):
    step = StagingStep(
        step_id=1,
        name="SSDS_AVY_FACT_STEP1_STG",
        select_sql="SELECT ...",
        column_mappings=column_mappings,
        source_bindings=source_bindings or [],
    )
    target = _make_table("IKOROSTELEV", "AVY_FACT_SIDE")
    model = ODIModel(target=target)
    model.staging_steps = [step]
    model.final_insert_columns = [cm.target_col for cm in column_mappings]
    return model


# ── DrdClaim.from_dict ────────────────────────────────────────────────────────

def test_drd_claim_from_dict_basic():
    d = {
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "BKR_AR_ID",
        "transformation": "",
    }
    claim = DrdClaim.from_dict(d)
    assert claim.target_col == "BKR_AR_ID"
    assert claim.source_schema == "CCAL_REPL_OWNER"
    assert claim.source_table == "APA"
    assert claim.source_attr == "BKR_AR_ID"
    assert claim.has_source is True


def test_drd_claim_from_dict_empty_source_attr():
    d = {"physical_name": "DERIVED_COL", "source_schema": "", "source_table": "", "source_attribute": ""}
    claim = DrdClaim.from_dict(d)
    assert claim.has_source is False


def test_drd_claim_from_dict_normalizes_case():
    d = {"physical_name": "bkr_ar_id", "source_schema": "ccal_repl_owner", "source_table": "apa", "source_attribute": "bkr_ar_id"}
    claim = DrdClaim.from_dict(d)
    assert claim.target_col == "BKR_AR_ID"
    assert claim.source_schema == "CCAL_REPL_OWNER"
    assert claim.source_table == "APA"


# ── MATCHED verdict ───────────────────────────────────────────────────────────

def test_matched_physical_table_and_column():
    """DRD says source_table=APA_SECURITY_POSITION, ODI resolves to APA_SECURITY_POSITION.BKR_AR_ID."""
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("APA", "BKR_AR_ID", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA_SECURITY_POSITION",
        "source_attribute": "BKR_AR_ID",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.MATCHED
    assert result.is_ok is True
    assert result.odi_table == "APA_SECURITY_POSITION"
    assert result.odi_col == "BKR_AR_ID"


def test_matched_via_alias():
    """DRD uses alias 'APA', ODI resolves APA -> CCAL_REPL_OWNER.APA_SECURITY_POSITION."""
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("APA", "BKR_AR_ID", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "BKR_AR_ID",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.MATCHED
    assert "APA" in result.alias_evidence


# ── ALIAS_DRIFT_ONLY verdict ──────────────────────────────────────────────────

def test_alias_drift_only_same_column_different_table():
    """Column name matches but table differs (DRD uses a different alias or old table name).
    Column name is distinctive, so ALIAS_DRIFT_ONLY (not REAL_MISMATCH).
    """
    bindings = [_make_binding("J_AVY_FACT", "CCAL_REPL_OWNER", "J_AVY_FACT")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("J_AVY_FACT", "BKR_AR_ID", "CCAL_REPL_OWNER", "J_AVY_FACT"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",               # DRD says APA, ODI says J_AVY_FACT
        "source_attribute": "BKR_AR_ID",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.ALIAS_DRIFT_ONLY
    assert result.is_ok is True


def test_real_mismatch_generic_column_name_different_table():
    """Generic column name 'ID' with different source table is REAL_MISMATCH."""
    bindings = [_make_binding("DIM", "CIRD_OWNER", "IMT_PD_DIM")]
    cm = ColumnMapping("SEC_PD_ID", _make_resolved("DIM", "ID", "CIRD_OWNER", "IMT_PD_DIM"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "SEC_PD_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "ID",     # generic name
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.REAL_MISMATCH
    assert result.is_ok is False


# ── REAL_MISMATCH verdict ─────────────────────────────────────────────────────

def test_real_mismatch_different_column():
    """ODI maps to completely different column than DRD claims."""
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("APA", "DIFFERENT_COL", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA_SECURITY_POSITION",
        "source_attribute": "BKR_AR_ID",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.REAL_MISMATCH
    assert result.is_ok is False
    assert result.odi_col == "DIFFERENT_COL"


# ── UNRESOLVABLE verdict ──────────────────────────────────────────────────────

def test_unresolvable_when_odi_unresolved_expr():
    """If ODI has an UnresolvedExpr, verdict is UNRESOLVABLE."""
    cm = ColumnMapping("SOME_COL", _make_unresolved("BAD_ALIAS.SOME_COL", "ALIAS_NOT_IN_JOIN_GRAPH"))
    model = _make_model_with_step1([cm])

    drd = DrdClaim.from_dict({
        "physical_name": "SOME_COL",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "SOME_COL",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.UNRESOLVABLE
    assert "ALIAS_NOT_IN_JOIN_GRAPH" in result.unresolved_reason


def test_unresolvable_when_drd_has_no_source_and_odi_complex():
    """DRD has no source attribute and ODI uses a complex expression."""
    cm = ColumnMapping("DERIVED", _make_complex("NVL(APA.COL, 'DEFAULT')"))
    model = _make_model_with_step1([cm])

    drd = DrdClaim.from_dict({
        "physical_name": "DERIVED",
        "source_schema": "",
        "source_table": "",
        "source_attribute": "",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.UNRESOLVABLE
    assert result.is_ok is False


def test_unresolvable_complex_expression_with_drd_transformation():
    """ODI is complex expression, DRD has transformation rule → UNRESOLVABLE (manual verify)."""
    cm = ColumnMapping("CALC_COL", _make_complex("CASE WHEN APA.FLAG = 'Y' THEN 1 ELSE 0 END"))
    model = _make_model_with_step1([cm])

    drd = DrdClaim.from_dict({
        "physical_name": "CALC_COL",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "FLAG",
        "transformation": "CASE WHEN FLAG='Y' THEN 1 ELSE 0",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.UNRESOLVABLE
    assert "complex" in result.explanation.lower()


# ── SOURCE_MISSING verdict ────────────────────────────────────────────────────

def test_source_missing_when_column_not_in_model():
    """Column claimed by DRD is not in any ODI staging step."""
    cm = ColumnMapping("EXISTING_COL", _make_resolved("T", "EXISTING_COL", "SCHEMA", "TABLE"))
    model = _make_model_with_step1([cm])

    drd = DrdClaim.from_dict({
        "physical_name": "NONEXISTENT_COL",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "NONEXISTENT_COL",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.SOURCE_MISSING
    assert result.is_ok is False


# ── LITERAL matched ───────────────────────────────────────────────────────────

def test_literal_matched_when_drd_has_no_source():
    """DRD has no source (literal/constant column); ODI also uses a literal → MATCHED."""
    cm = ColumnMapping("RECORD_TYPE_CODE", _make_literal("'AVY_FACT'"))
    model = _make_model_with_step1([cm])

    drd = DrdClaim.from_dict({
        "physical_name": "RECORD_TYPE_CODE",
        "source_schema": "",
        "source_table": "",
        "source_attribute": "",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.MATCHED
    assert result.odi_expr_sql == "'AVY_FACT'"


# ── Batch comparison ──────────────────────────────────────────────────────────

def test_compare_drd_rows_to_model_batch():
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")]
    col_maps = [
        ColumnMapping("COL_A", _make_resolved("APA", "COL_A", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")),
        ColumnMapping("COL_B", _make_resolved("APA", "COL_B", "CCAL_REPL_OWNER", "APA_SECURITY_POSITION")),
    ]
    model = _make_model_with_step1(col_maps, bindings)

    drd_rows = [
        {"physical_name": "COL_A", "source_schema": "", "source_table": "APA_SECURITY_POSITION", "source_attribute": "COL_A"},
        {"physical_name": "COL_B", "source_schema": "", "source_table": "APA_SECURITY_POSITION", "source_attribute": "COL_B"},
        {"physical_name": "", "source_schema": "", "source_table": "", "source_attribute": "SKIP_ME"},  # empty target
    ]
    results = compare_drd_rows_to_model(drd_rows, model)

    assert len(results) == 2  # empty physical_name row skipped
    assert all(r.verdict == ComparisonVerdict.MATCHED for r in results)


def test_comparison_summary_counts():
    bindings = [_make_binding("T", "S", "TABLE")]
    col_maps = [
        ColumnMapping("MATCHED_COL", _make_resolved("T", "MATCHED_COL", "S", "TABLE")),
        ColumnMapping("WRONG_COL", _make_resolved("T", "OTHER_COL", "S", "TABLE")),
        ColumnMapping("UNRES_COL", _make_unresolved("X.UNRES_COL", "ALIAS_NOT_IN_JOIN_GRAPH")),
    ]
    model = _make_model_with_step1(col_maps, bindings)

    rows = [
        {"physical_name": "MATCHED_COL", "source_schema": "S", "source_table": "TABLE", "source_attribute": "MATCHED_COL"},
        {"physical_name": "WRONG_COL", "source_schema": "S", "source_table": "TABLE", "source_attribute": "MATCHED_COL"},
        {"physical_name": "UNRES_COL", "source_schema": "S", "source_table": "TABLE", "source_attribute": "UNRES_COL"},
    ]
    results = compare_drd_rows_to_model(rows, model)
    summary = comparison_summary(results)

    assert summary["total"] == 3
    assert summary["matched"] == 1
    assert summary["real_mismatch"] == 1
    assert summary["unresolvable"] == 1
    assert summary["error_count"] == 2
    assert summary["ok_count"] == 1
    assert "WRONG_COL" in summary["mismatch_targets"]
    assert "UNRES_COL" in summary["unresolvable_targets"]


def test_to_dict_roundtrip():
    """ComparisonResult.to_dict() must produce a plain dict with all expected keys."""
    cm = ColumnMapping("COL_A", _make_resolved("T", "COL_A", "S", "TABLE"))
    model = _make_model_with_step1([cm], [_make_binding("T", "S", "TABLE")])
    drd = DrdClaim.from_dict({"physical_name": "COL_A", "source_schema": "S", "source_table": "TABLE", "source_attribute": "COL_A"})
    result = compare_drd_odi(drd, model)
    d = result.to_dict()

    assert isinstance(d, dict)
    for key in ("verdict", "target_col", "drd_table", "odi_table", "odi_col", "explanation", "is_ok"):
        assert key in d, f"missing key: {key}"
    assert d["verdict"] == "MATCHED"
    assert d["is_ok"] is True


# ── False SOURCE_MISSING regression ──────────────────────────────────────────

def test_transformation_drift_drd_complex_odi_passthrough_resolved_path():
    """P0: DRD describes derivation (CASE/parse/lookup); ODI projects a bare
    column pass-through.  Must emit REAL_MISMATCH + TRANSFORMATION_DRIFT with
    drd_logic + odi_logic populated for side-by-side display.
    """
    # ODI: bare pass-through of TRADE_NUMBER column on TXN table.
    bindings = [_make_binding("TXN", "CCAL_REPL_OWNER", "TXN")]
    cm = ColumnMapping("DERIVED_COL", _make_resolved("TXN", "TRADE_NUMBER", "CCAL_REPL_OWNER", "TXN"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "DERIVED_COL",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "TXN",
        "source_attribute": "TRADE_NUMBER",
        "transformation": (
            "For TXN.SRC_STM_ID=60 use TRADE_NUMBER. "
            "Parse to extract Transaction Type. For example, from '02726' "
            "value it will be '027', while 26 stands for year. Lookup CL_VAL_CODE"
        ),
    })
    r = compare_drd_odi(drd, model)

    assert r.verdict == ComparisonVerdict.REAL_MISMATCH, r.verdict
    assert r.mismatch_kind == MismatchKind.TRANSFORMATION_DRIFT
    # Side-by-side evidence
    assert r.drd_logic.startswith("For TXN.SRC_STM_ID=60")
    assert r.odi_logic == "TXN.TRADE_NUMBER"
    assert "TRANSFORMATION_DRIFT" in r.explanation


def test_transformation_drift_via_text_search_in_step_sql():
    """P0: column NOT in column_mappings but ODI projects it as bare pass-through
    in the staging SQL text body.  TRANSFORMATION_DRIFT must still fire when
    DRD requires derivation.
    """
    # Build a step whose select_sql carries a projection line
    # for SDIRA_LIKE_COL but column_mappings is empty
    step = StagingStep(
        step_id=3,
        name="SSDS_AVY_FACT_STEP3_STG",
        select_sql=(
            "INSERT INTO SSDS_AVY_FACT_STEP3_STG SELECT\n"
            "    t.AR_ID AS AR_ID,\n"
            "    t.TRD_NUM AS DERIVED_COL,\n"
            "    t.TXN_ID AS TXN_ID\n"
            "FROM CCAL_REPL_OWNER.TXN t"
        ),
        column_mappings=[],  # parser couldn't extract
        source_bindings=[_make_binding("T", "CCAL_REPL_OWNER", "TXN")],
    )
    target = _make_table("IKOROSTELEV", "AVY_FACT_SIDE")
    model = ODIModel(target=target)
    model.staging_steps = [step]
    model.final_insert_columns = ["DERIVED_COL"]

    drd = DrdClaim.from_dict({
        "physical_name": "DERIVED_COL",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "TXN",
        "source_attribute": "TRD_NUM",
        "transformation": (
            "Parse TRD_NUM and lookup CL_VAL_CODE to derive transaction code; "
            "use only for SRC_STM_ID = 60 cases"
        ),
    })
    r = compare_drd_odi(drd, model)

    assert r.verdict == ComparisonVerdict.REAL_MISMATCH
    assert r.mismatch_kind == MismatchKind.TRANSFORMATION_DRIFT
    assert "t.TRD_NUM" in r.odi_logic
    assert "Parse" in r.drd_logic


def test_simple_drd_simple_odi_matched_no_drift():
    """No false TRANSFORMATION_DRIFT when both DRD and ODI are simple."""
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("APA", "BKR_AR_ID", "CCAL_REPL_OWNER", "APA"))
    model = _make_model_with_step1([cm], bindings)

    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "BKR_AR_ID",
        "transformation": "",  # no rule = simple pass-through
    })
    r = compare_drd_odi(drd, model)

    assert r.verdict == ComparisonVerdict.MATCHED
    assert r.mismatch_kind == MismatchKind.NONE
    assert r.drd_logic == "BKR_AR_ID"
    assert r.odi_logic == "APA.BKR_AR_ID"


def test_mismatch_kind_column_vs_table():
    """Same table, different column -> COLUMN_MISMATCH.
    Different table, different column -> TABLE_MISMATCH."""
    # Different column on same table
    bindings = [_make_binding("APA", "CCAL_REPL_OWNER", "APA")]
    cm = ColumnMapping("BKR_AR_ID", _make_resolved("APA", "DIFFERENT_COL", "CCAL_REPL_OWNER", "APA"))
    model = _make_model_with_step1([cm], bindings)
    drd = DrdClaim.from_dict({
        "physical_name": "BKR_AR_ID",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "APA",
        "source_attribute": "BKR_AR_ID",
    })
    r = compare_drd_odi(drd, model)
    assert r.verdict == ComparisonVerdict.REAL_MISMATCH
    assert r.mismatch_kind == MismatchKind.COLUMN_MISMATCH


def test_join_drift_drd_requires_join_not_in_odi():
    """P0.5: DRD col-AD declares a join predicate; ODI does NOT implement it.
    Verdict must be REAL_MISMATCH + JOIN_DRIFT regardless of column match."""
    from app.sql_model.types import JoinEdge, JoinType
    # ODI: joined APA on AR_DIM via APA.AR_ID = AR_DIM.PARTY_ID (DIFFERENT predicate)
    apa_b = _make_binding("APA", "CCAL_REPL_OWNER", "APA")
    ar_b = _make_binding("AR_DIM", "CCSI_OWNER", "AR_DIM")
    step = StagingStep(
        step_id=1,
        name="SSDS_STEP1_STG",
        select_sql="...",
        column_mappings=[
            ColumnMapping("ANY_COL", _make_resolved("AR_DIM", "ANY_COL", "CCSI_OWNER", "AR_DIM"))
        ],
        source_bindings=[apa_b, ar_b],
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=apa_b,
                joined=ar_b,
                on_sql="APA.AR_ID = AR_DIM.PARTY_ID",
            )
        ],
    )
    model = ODIModel(target=_make_table("IK", "T"))
    model.staging_steps = [step]
    model.final_insert_columns = ["ANY_COL"]

    # DRD says: join AR_DIM on AR_DIM.AR_ID = APA.AR_OWNER (different ON)
    drd = DrdClaim.from_dict({
        "physical_name": "ANY_COL",
        "source_schema": "CCSI_OWNER",
        "source_table": "AR_DIM",
        "source_attribute": "ANY_COL",
        "transformation": (
            "ccal_repl_owner.apa apa\n"
            "left join ccsi_owner.ar_dim ar ON ar.AR_ID = apa.AR_OWNER"
        ),
    })
    r = compare_drd_odi(drd, model)
    assert r.verdict == ComparisonVerdict.REAL_MISMATCH
    assert r.mismatch_kind == MismatchKind.JOIN_DRIFT
    assert "JOIN_DRIFT" in r.explanation


def test_etl_block_body_flows_into_drd_logic_for_drift_detection():
    """P3: when the row carries an ETL-Notes block body, the comparator must
    use it (not the brief 'Use X logic' placeholder) as the effective rule for
    DRIFT detection and side-by-side display."""
    bindings = [_make_binding("TXN", "CCAL_REPL_OWNER", "TXN")]
    cm = ColumnMapping("CASH_AMT", _make_resolved("TXN", "AMT", "CCAL_REPL_OWNER", "TXN"))
    model = _make_model_with_step1([cm], bindings)
    drd = DrdClaim.from_dict({
        "physical_name": "CASH_AMT",
        "source_schema": "CCAL_REPL_OWNER",
        "source_table": "TXN",
        "source_attribute": "AMT",
        "transformation": "Use APACSH logic from 'ETL Notes' tab.",
        "etl_block_ref": "APACSH",
        "etl_block_body": (
            "Check for regexp_like(cv.cl_val_code,'^APACSH[0-7][0-9]')\n"
            "For a Transaction, if only one record exists then consider it.\n"
            "If 2 records exist, choose the higher-priority APACSH variant."
        ),
    })
    r = compare_drd_odi(drd, model)
    # ODI is bare pass-through; DRD (via ETL block) is multi-line derivation
    # -> TRANSFORMATION_DRIFT must fire.
    assert r.verdict == ComparisonVerdict.REAL_MISMATCH
    assert r.mismatch_kind == MismatchKind.TRANSFORMATION_DRIFT
    # Side-by-side display must show the resolved ETL block content
    assert "APACSH" in r.drd_logic
    assert "regexp_like" in r.drd_logic


def test_applicable_filter_drift_shared_with_emitter():
    """Same rule engine that the EMITTER uses to wrap projections in
    CASE WHEN must also let the COMPARATOR flag APPLICABLE_FILTER_DRIFT when
    ODI doesn't filter.  Uses arbitrary generic identifiers (no real-DRD names)."""
    # ODI does a bare pass-through
    bindings = [_make_binding("SRC", "OWNER", "SRC_TBL")]
    cm = ColumnMapping("MY_AMT", _make_resolved("SRC", "AMT", "OWNER", "SRC_TBL"))
    model = _make_model_with_step1([cm], bindings)
    drd = DrdClaim.from_dict({
        "physical_name": "MY_AMT",
        "source_schema": "OWNER",
        "source_table": "SRC_TBL",
        "source_attribute": "AMT",
        "transformation": "Applicable only for WIDGET42",
        "etl_block_body": "Check regexp_like(d.kind_col,'^WIDGET[0-9]+')",
    })
    r = compare_drd_odi(drd, model)
    assert r.verdict == ComparisonVerdict.REAL_MISMATCH
    assert r.mismatch_kind == MismatchKind.APPLICABLE_FILTER_DRIFT
    assert "WIDGET42" in r.drd_logic
    assert "CASE WHEN" in r.drd_logic
    assert "d.KIND_COL" in r.drd_logic
    assert r.odi_logic == "SRC.AMT"


def test_join_drift_satisfied_no_drift():
    """When ODI implements every DRD-required join predicate, JOIN_DRIFT does
    NOT fire (verdict stays MATCHED or whatever the column compare yielded)."""
    from app.sql_model.types import JoinEdge, JoinType
    apa_b = _make_binding("APA", "CCAL_REPL_OWNER", "APA")
    ar_b = _make_binding("AR_DIM", "CCSI_OWNER", "AR_DIM")
    step = StagingStep(
        step_id=1,
        name="STEP1",
        select_sql="...",
        column_mappings=[
            ColumnMapping("ANY_COL", _make_resolved("AR_DIM", "ANY_COL", "CCSI_OWNER", "AR_DIM"))
        ],
        source_bindings=[apa_b, ar_b],
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=apa_b,
                joined=ar_b,
                on_sql="AR_DIM.AR_ID = APA.AR_OWNER",  # same as DRD
            )
        ],
    )
    model = ODIModel(target=_make_table("IK", "T"))
    model.staging_steps = [step]
    model.final_insert_columns = ["ANY_COL"]
    drd = DrdClaim.from_dict({
        "physical_name": "ANY_COL",
        "source_schema": "CCSI_OWNER",
        "source_table": "AR_DIM",
        "source_attribute": "ANY_COL",
        "transformation": (
            "ccal_repl_owner.apa apa\n"
            "left join ccsi_owner.ar_dim ar ON ar.AR_ID = apa.AR_OWNER"
        ),
    })
    r = compare_drd_odi(drd, model)
    assert r.verdict == ComparisonVerdict.MATCHED
    assert r.mismatch_kind == MismatchKind.NONE


def test_drd_logic_odi_logic_populated_for_unresolved():
    """Side-by-side fields are populated even for UNRESOLVABLE rows so the
    operator can see both perspectives in the grid."""
    cm = ColumnMapping("X", _make_unresolved("BAD.X", "ALIAS_NOT_IN_JOIN_GRAPH"))
    model = _make_model_with_step1([cm])
    drd = DrdClaim.from_dict({
        "physical_name": "X", "source_schema": "S", "source_table": "T",
        "source_attribute": "X", "transformation": "complex derive rule",
    })
    r = compare_drd_odi(drd, model)
    assert r.verdict == ComparisonVerdict.UNRESOLVABLE
    assert r.drd_logic == "complex derive rule"
    assert r.odi_logic == "BAD.X"


def test_column_in_merge_block_but_no_step_mapping_is_unresolvable_not_source_missing():
    """Regression: column present in final_insert_columns (MERGE block) but absent
    from column_mappings (STEP_INSERT had no explicit column list) must produce
    UNRESOLVABLE with reason ODI_COLUMN_IN_FINAL_SOURCE_NOT_TRACED, NOT SOURCE_MISSING.

    This was the SDIRA_TXN_TP_CD false-SOURCE_MISSING root cause.
    """
    # Build a step with EMPTY column_mappings but a source binding
    step = StagingStep(
        step_id=5,
        name="SSDS_AVY_FACT_STEP5_STG",
        select_sql="SELECT ...",
        column_mappings=[],  # no explicit column list parsed
        source_bindings=[_make_binding("RT", "DS_OWNER", "TRADE_NUM_TABLE")],
    )
    target = _make_table("IKOROSTELEV", "AVY_FACT_SIDE")
    model = ODIModel(target=target)
    model.staging_steps = [step]
    # MERGE block IS aware of the column
    model.final_insert_columns = ["SDIRA_TXN_TP_CD"]

    drd = DrdClaim.from_dict({
        "physical_name": "SDIRA_TXN_TP_CD",
        "source_schema": "DS_OWNER",
        "source_table": "TRADE_NUM_TABLE",
        "source_attribute": "SDIRA_TXN_TP_CD",
        "transformation": "Parse TRADE_NUMBER to extract SDIRA Transaction Type",
    })
    result = compare_drd_odi(drd, model)

    # Must NOT be SOURCE_MISSING — column is in the MERGE block
    assert result.verdict == ComparisonVerdict.UNRESOLVABLE, (
        f"Expected UNRESOLVABLE, got {result.verdict}: {result.explanation}"
    )
    assert result.unresolved_reason == "ODI_COLUMN_IN_FINAL_SOURCE_NOT_TRACED"
    # DRD transformation rule lives in the dedicated drd_logic field (P0+P2),
    # surfaced side-by-side with odi_logic in the UI grid.
    assert "SDIRA Transaction Type" in result.drd_logic
    # DRD source table IS in the step JOINs → must say so
    assert "IS present in ODI step JOINs" in result.explanation


def test_align_expr_with_source_attr_reconciles_alias_pk_placeholder():
    """Regression: build_control_insert_sql was emitting AR_DIM_18.AR_ID AS AC_CGY_CD
    because the baseline test SQL extractor handed back the join PK as a placeholder
    column. The DRD source_attribute='AR_CGY_CD' is the truth; alignment must rewrite
    AR_DIM_18.AR_ID -> AR_DIM_18.AR_CGY_CD.
    """
    from app.services.control_table_service import align_expr_with_source_attr

    # Real failure case from operator's screenshot
    assert align_expr_with_source_attr("AR_DIM_18.AR_ID", "AR_CGY_CD") == "AR_DIM_18.AR_CGY_CD"
    # Already-aligned expr untouched
    assert align_expr_with_source_attr("AR_DIM_18.AR_CGY_CD", "AR_CGY_CD") == "AR_DIM_18.AR_CGY_CD"
    # CASE / NVL / DECODE / arithmetic must NOT be rewritten
    assert align_expr_with_source_attr("NVL(t.X,'-1')", "X") == "NVL(t.X,'-1')"
    assert align_expr_with_source_attr("CASE WHEN t.X=1 THEN 'A' END", "FOO") == "CASE WHEN t.X=1 THEN 'A' END"
    # Literals untouched
    assert align_expr_with_source_attr("SYSDATE", "FOO") == "SYSDATE"
    assert align_expr_with_source_attr("'ODI_ETL'", "FOO") == "'ODI_ETL'"
    # Empty source_attr — no-op
    assert align_expr_with_source_attr("t.X", "") == "t.X"
    # source_attr that's itself an expression — no-op
    assert align_expr_with_source_attr("t.X", "NVL(t.Y, 0)") == "t.X"
    # Empty expr — no-op
    assert align_expr_with_source_attr("", "X") == ""
    # Case insensitive match
    assert align_expr_with_source_attr("ar_dim_1.ar_id", "AR_CGY_CD") == "ar_dim_1.AR_CGY_CD"


def test_column_in_merge_block_drd_source_table_not_in_joins():
    """When the DRD-claimed source table is NOT present in any step's JOIN graph,
    the UNRESOLVABLE explanation must say so — prompting the analyst to verify
    whether ODI uses the wrong lookup/join chain.
    """
    step = StagingStep(
        step_id=5,
        name="SSDS_AVY_FACT_STEP5_STG",
        select_sql="SELECT ...",
        column_mappings=[],
        source_bindings=[_make_binding("T", "DS_OWNER", "SOME_OTHER_TABLE")],
    )
    target = _make_table("IKOROSTELEV", "AVY_FACT_SIDE")
    model = ODIModel(target=target)
    model.staging_steps = [step]
    model.final_insert_columns = ["MISSING_SRC_COL"]

    drd = DrdClaim.from_dict({
        "physical_name": "MISSING_SRC_COL",
        "source_schema": "DS_OWNER",
        "source_table": "EXPECTED_LOOKUP_TABLE",
        "source_attribute": "MISSING_SRC_COL",
        "transformation": "",
    })
    result = compare_drd_odi(drd, model)

    assert result.verdict == ComparisonVerdict.UNRESOLVABLE
    assert result.unresolved_reason == "ODI_COLUMN_IN_FINAL_SOURCE_NOT_TRACED"
    # Table not found — must warn the analyst
    assert "NOT found in any ODI step JOINs" in result.explanation
