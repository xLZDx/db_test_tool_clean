"""Tests for app.sql_model.drd_first_emitter -- the DRD-first INSERT emitter.

Verifies:
  * Coverage: every target column gets a source expression
  * No PDM_MISS / TODO leakage when DRD AD is present
  * ETL block reference becomes a CTE
  * DRD AD JOIN becomes a LEFT JOIN
  * ODI projection used when comparator reports MATCHED
  * Generic: works for arbitrary target schema / table / columns
"""
from __future__ import annotations

import re

from app.sql_model.comparator import ComparisonResult
from app.sql_model.drd_first_emitter import emit_insert_drd_first
from app.sql_model.types import ComparisonVerdict, MismatchKind, ODIModel, TableRef


def _td(*cols):
    """Build a minimal target_definition dict from a list of (name, dtype)."""
    return {
        "name": "TARGET",
        "columns": [
            {"name": n, "data_type": dt, "nullable": True, "is_pk": False}
            for (n, dt) in cols
        ],
        "primary_keys": [],
    }


def test_every_target_column_gets_an_expression():
    tdef = _td(("A", "NUMBER"), ("B", "VARCHAR2(10)"), ("C", "DATE"))
    rows = [
        {"column": "A", "source_table": "SRC_T", "source_attribute": "A"},
        {"column": "B", "source_table": "SRC_T", "source_attribute": "B"},
    ]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows,
    )
    assert res.column_count == 3
    # Every target appears in the INSERT col list
    for c in ("A", "B", "C"):
        assert re.search(rf"\b{c}\b", res.sql)


def test_etl_default_for_system_columns():
    tdef = _td(("CRT_DTM", "DATE"), ("CRT_USR_NM", "VARCHAR2(30)"))
    rows = [
        {"column": "CRT_DTM", "source_table": "", "source_attribute": ""},
        {"column": "CRT_USR_NM", "source_table": "", "source_attribute": ""},
    ]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
    )
    assert "SYSDATE" in res.sql
    assert "'ETL'" in res.sql  # generic default user name
    assert res.provenance_summary.get("ETL_DEFAULT", 0) == 2


def test_etl_defaults_overridable_by_caller():
    """Operator-locked: ``etl_column_defaults`` must override the generic map
    so any target table works without code change.  Uses arbitrary identifiers."""
    tdef = _td(("AUDIT_TS", "DATE"), ("CREATED_BY", "VARCHAR2(30)"))
    rows = [
        {"column": "AUDIT_TS"},
        {"column": "CREATED_BY"},
    ]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
        etl_column_defaults={"AUDIT_TS": "SYSTIMESTAMP", "CREATED_BY": "'OPS_TEAM'"},
    )
    assert "SYSTIMESTAMP" in res.sql
    assert "'OPS_TEAM'" in res.sql


def test_etl_block_prose_becomes_header_comment_not_cte():
    """v4 design: ETL Notes prose blocks NEVER inside WITH (Oracle parse fail).
    They surface as a header comment block; the column projects from its
    physical DRD source_attribute and annotates the projection with the block
    reference inline.
    """
    tdef = _td(("MY_AMT", "NUMBER"))
    rows = [{
        "column": "MY_AMT",
        "source_table": "SOMETBL",
        "source_attribute": "AMT",
        "transformation": "Use MYBLOCK logic from 'ETL Notes' tab.",
        "etl_block_ref": "MYBLOCK",
        "etl_block_body": "If 2 records exist, choose the higher-priority one.",
    }]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
    )
    # No CTE emitted for prose
    assert "WITH" not in res.sql.split("INSERT")[0]
    # Header comment carries the block context
    assert "ETL_BLOCK[MYBLOCK]" in res.sql
    # Projection uses physical source, not NULL
    assert "AMT" in res.sql
    assert "NULL " not in res.sql.split("INSERT")[1]


def test_drd_physical_source_used_when_no_odi_chain():
    """v4: when no comparator/ODI gives a concrete projection, the emitter
    projects directly from row.source_table + row.source_attribute (with the
    base-alias scheme), never NULL."""
    tdef = _td(("LK_NAME", "VARCHAR2(50)"))
    rows = [{
        "column": "LK_NAME",
        "source_schema": "OTHER",
        "source_table": "LOOKUP_TBL",
        "source_attribute": "NAME",
        "transformation": (
            "primary_owner.txn t\n"
            "left join other.lookup_tbl lk ON t.lk_id = lk.id"
        ),
    }]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
    )
    # Physical source projection emitted (alias from LOOKUP_TBL initials)
    assert ".NAME" in res.sql
    # And NO bad ON-clause is invented from DRD col-AD (we use ODI's only)
    assert "1=0" not in res.sql
    assert res.provenance_summary.get("DRD_PHYSICAL", 0) == 1


def test_odi_projection_used_when_matched():
    """When the comparator reports MATCHED for a column with a simple ODI
    projection, the emitter must adopt that projection verbatim (alias-mapped)."""
    tdef = _td(("X", "NUMBER"))
    rows = [{"column": "X", "source_table": "MAIN", "source_attribute": "X"}]
    cmp = [ComparisonResult(
        verdict=ComparisonVerdict.MATCHED,
        target_col="X",
        drd_schema="", drd_table="MAIN", drd_attr="X",
        odi_schema="", odi_table="MAIN", odi_col="X",
        odi_expr_sql="MAIN.X",
        odi_step=1,
        explanation="MATCHED",
        odi_logic="MAIN.X",
    )]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
        comparison_results=cmp,
    )
    # ODI projection adopted; provenance recorded
    assert res.provenance_summary.get("ODI", 0) == 1


def test_generic_no_hardcoded_business_names():
    """Same emitter with arbitrary schema / table / columns -> clean output."""
    tdef = _td(
        ("WIDGET_ID", "NUMBER"),
        ("WIDGET_NAME", "VARCHAR2(100)"),
        ("CREATED_AT", "DATE"),  # not in _ETL_DEFAULTS -> simple fallback
    )
    rows = [
        {"column": "WIDGET_ID", "source_table": "WIDGETS",
         "source_attribute": "ID", "source_schema": "INV"},
        {"column": "WIDGET_NAME", "source_table": "WIDGETS",
         "source_attribute": "NAME", "source_schema": "INV"},
        {"column": "CREATED_AT", "source_schema": "INV", "source_table": "WIDGETS",
         "source_attribute": "CREATED_AT"},
    ]
    res = emit_insert_drd_first(
        target_schema="MYAPP", target_table="WIDGET_FACT",
        target_definition=tdef, analysis_rows=rows,
    )
    assert res.column_count == 3
    assert "MYAPP.WIDGET_FACT" in res.sql
    assert "INV.WIDGETS" in res.sql


def test_extract_alias_for_col_picks_real_alias_not_keyword():
    """The alias-extractor must skip SQL keywords (CASE, SUM, NVL, MAX, ...)
    so a wrapped expression yields the underlying alias.  Generic, no
    business-name constants."""
    from app.sql_model.drd_first_emitter import _extract_alias_for_col
    assert _extract_alias_for_col("ALPHA_REL_BETA.WIDGET_COL", "WIDGET_COL") == "ALPHA_REL_BETA"
    assert _extract_alias_for_col("NVL(zed.X, 0)", "X") == "zed"
    assert _extract_alias_for_col("CASE WHEN zed.A = 1 THEN beta.B END", "B") == "beta"
    assert _extract_alias_for_col("SUM(x.AMT)", "AMT") == "x"
    assert _extract_alias_for_col("", "X") is None
    assert _extract_alias_for_col("x.Y", "Z") is None


def test_t_alias_hint_routes_to_odi_self_join_alias():
    """G feature: DRD source_attribute hint "(FROM T2)" + an ODI staging-step
    expression that uses a distinct alias for the same physical table makes
    the emitter project from THAT alias verbatim (instead of the canonical
    base alias).  Operator-locked: REL_<COL>-style self-join cases must land
    on the second alias.  Pure generic -- arbitrary names used here."""
    # Build a minimal ODI model with a self-join on ALPHA (base + ALPHA_REL).
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    base = TableRef(schema="MYS", table="ALPHA")
    rel_tbl = TableRef(schema="MYS", table="ALPHA_REL")
    second_alpha = TableRef(schema="MYS", table="ALPHA")
    step = StagingStep(
        step_id=1,
        name="STG1",
        select_sql="SELECT a.X, alpha_rel.Y, alpha_rel_alpha.X AS REL_X FROM MYS.ALPHA a "
                   "LEFT JOIN MYS.ALPHA_REL alpha_rel ON a.ID = alpha_rel.SRC_ID "
                   "LEFT JOIN MYS.ALPHA alpha_rel_alpha ON alpha_rel.TGT_ID = alpha_rel_alpha.ID",
        source_bindings=[
            AliasBinding(alias="a", ref=base),
            AliasBinding(alias="alpha_rel", ref=rel_tbl),
            AliasBinding(alias="alpha_rel_alpha", ref=second_alpha),
        ],
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="a", ref=base),
                joined=AliasBinding(alias="alpha_rel", ref=rel_tbl),
                on_sql="a.ID = alpha_rel.SRC_ID",
            ),
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="alpha_rel", ref=rel_tbl),
                joined=AliasBinding(alias="alpha_rel_alpha", ref=second_alpha),
                on_sql="alpha_rel.TGT_ID = alpha_rel_alpha.ID",
            ),
        ],
    )
    model = ODIModel(
        target=TableRef(schema="MYS", table="TARGET"),
        staging_steps=[step],
    )
    # DRD row: source_attribute has "(FROM T2)" hint -> route to the second alias.
    tdef = _td(("REL_X", "NUMBER"))
    rows = [{
        "column": "REL_X",
        "source_schema": "MYS",
        "source_table": "ALPHA",
        "source_attribute": "X (FROM T2)",  # G hint
    }]
    cmp = [ComparisonResult(
        verdict=ComparisonVerdict.MATCHED,
        target_col="REL_X",
        drd_schema="MYS", drd_table="ALPHA", drd_attr="X",
        odi_schema="MYS", odi_table="ALPHA", odi_col="X",
        odi_expr_sql="alpha_rel_alpha.X",
        odi_step=1,
        explanation="MATCHED via self-join",
        odi_logic="alpha_rel_alpha.X",
    )]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TARGET",
        target_definition=tdef, analysis_rows=rows,
        odi_model=model,
        comparison_results=cmp,
    )
    # Projection lands on the SECOND alias, NOT the base.
    assert "alpha_rel_alpha.X" in res.sql, (
        f"Expected second-alias projection 'alpha_rel_alpha.X' in:\n{res.sql}"
    )
    # And the self-join must be present in the FROM/JOIN tree.
    assert "LEFT JOIN MYS.ALPHA" in res.sql.upper().replace("MYS.ALPHA_REL", "")
    # Provenance is the G-specific marker.
    assert res.provenance_summary.get("ODI_T_ALIAS", 0) == 1


def test_t_alias_hint_lowercase_from_also_detected():
    """Operator cells commonly mix case; lowercase 'from' must work too."""
    from app.sql_model.drd_rules import extract_t_alias_hint
    assert extract_t_alias_hint("widget_col (from t2)") == "T2"


def test_target_col_role_root_strips_common_suffixes():
    """Generic suffix stripper -- recovers role-root for ODI alias matching.
    No business-domain names hardcoded."""
    from app.sql_model.drd_first_emitter import _target_col_role_root
    assert _target_col_role_root("WIDGET_TP_CD") == "WIDGET_TP"
    assert _target_col_role_root("GADGET_NM") == "GADGET"
    assert _target_col_role_root("ZED_F") == "ZED"
    assert _target_col_role_root("AMOUNT_AMT") == "AMOUNT"
    assert _target_col_role_root("X_ID") == "X"
    assert _target_col_role_root("") == ""
    # No matching suffix -> returns the column itself upper-cased.
    assert _target_col_role_root("widget") == "WIDGET"


def test_match_target_to_odi_role_exact_match():
    """When a row has no DRD AD join, the target-column root matches an
    ODI alias name -- that's the right role to project from."""
    from app.sql_model.drd_first_emitter import _match_target_to_odi_role
    odi_roles = [
        ("ALPHA_LK", "t.alpha_id = ALPHA_LK.id"),
        ("BETA_LK", "t.beta_id = BETA_LK.id"),
    ]
    # Exact match on alias name
    assert _match_target_to_odi_role("ALPHA_LK_CD", odi_roles) == "ALPHA_LK"


def test_match_target_to_odi_role_root_prefix():
    """role_root starts with alias -- still a valid match."""
    from app.sql_model.drd_first_emitter import _match_target_to_odi_role
    odi_roles = [
        ("WIDGET", "t.widget_id = WIDGET.id"),
        ("GADGET", "t.gadget_id = GADGET.id"),
    ]
    # WIDGET_TP_CD root = WIDGET_TP, starts with "WIDGET_"
    assert _match_target_to_odi_role("WIDGET_TP_CD", odi_roles) == "WIDGET"


def test_match_target_to_odi_role_returns_none_when_no_match():
    from app.sql_model.drd_first_emitter import _match_target_to_odi_role
    odi_roles = [("ALPHA_LK", "...")]
    assert _match_target_to_odi_role("BETA_CD", odi_roles) is None


def test_collect_multi_role_fqs_finds_multi_alias_lookups():
    """ODI joins the same fq under >= 2 distinct aliases -> multi-role.
    Generic -- no domain names."""
    from app.sql_model.drd_first_emitter import _collect_multi_role_fqs
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    lk = TableRef(schema="MYS", table="LOOKUP")
    base = TableRef(schema="MYS", table="BASE")
    step = StagingStep(
        step_id=1, name="STG1",
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="alpha_lk", ref=lk),
                on_sql="b.alpha_id = alpha_lk.id",
            ),
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="beta_lk", ref=lk),
                on_sql="b.beta_id = beta_lk.id",
            ),
        ],
    )
    model = ODIModel(target=TableRef(schema="MYS", table="T"), staging_steps=[step])
    mr = _collect_multi_role_fqs(model)
    assert "MYS.LOOKUP" in mr
    aliases = {a for a, _on in mr["MYS.LOOKUP"]}
    assert aliases == {"ALPHA_LK", "BETA_LK"}


def test_collect_multi_role_fqs_excludes_single_role():
    """Single-role joins (only one alias) are NOT classified as multi-role."""
    from app.sql_model.drd_first_emitter import _collect_multi_role_fqs
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    lk = TableRef(schema="MYS", table="LOOKUP")
    base = TableRef(schema="MYS", table="BASE")
    step = StagingStep(
        step_id=1, name="STG1",
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="lk", ref=lk),
                on_sql="b.x_id = lk.id",
            ),
        ],
    )
    model = ODIModel(target=TableRef(schema="MYS", table="T"), staging_steps=[step])
    mr = _collect_multi_role_fqs(model)
    assert "MYS.LOOKUP" not in mr


def test_collect_multi_role_fqs_handles_none_odi():
    from app.sql_model.drd_first_emitter import _collect_multi_role_fqs
    assert _collect_multi_role_fqs(None) == {}


def test_multi_role_lookup_splits_per_role_in_output():
    """Smoke: when ODI has 2 lookup roles for the same fq, two DRD rows that
    each reference a different role-key must produce TWO separate LEFT JOINs
    (not one with AND-ed predicates), and their projections must land on
    distinct aliases.  Generic, no domain names."""
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    base = TableRef(schema="MYS", table="BASE")
    lk = TableRef(schema="MYS", table="LOOKUP")
    step = StagingStep(
        step_id=1, name="STG1",
        source_bindings=[
            AliasBinding(alias="b", ref=base),
            AliasBinding(alias="alpha_lk", ref=lk),
            AliasBinding(alias="beta_lk", ref=lk),
        ],
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="alpha_lk", ref=lk),
                on_sql="b.alpha_id = alpha_lk.id",
            ),
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="beta_lk", ref=lk),
                on_sql="b.beta_id = beta_lk.id",
            ),
        ],
    )
    model = ODIModel(target=TableRef(schema="MYS", table="TGT"), staging_steps=[step])
    tdef = _td(
        ("BASE_ID", "NUMBER"),
        ("BASE_X", "NUMBER"),
        ("BASE_Y", "NUMBER"),
        ("ALPHA_NAME", "VARCHAR2(50)"),
        ("BETA_NAME", "VARCHAR2(50)"),
    )
    rows = [
        # Anchor rows: make BASE the dominant source_table so the auto-
        # detected FROM target is BASE, not LOOKUP.  (The detector picks the
        # most-frequent source_table.)
        {"column": "BASE_ID", "source_schema": "MYS", "source_table": "BASE", "source_attribute": "ID"},
        {"column": "BASE_X", "source_schema": "MYS", "source_table": "BASE", "source_attribute": "X"},
        {"column": "BASE_Y", "source_schema": "MYS", "source_table": "BASE", "source_attribute": "Y"},
        # Two rows, each with a DRD AD join naming a different role key.
        {
            "column": "ALPHA_NAME",
            "source_schema": "MYS", "source_table": "LOOKUP",
            "source_attribute": "NAME",
            "transformation": "left join MYS.LOOKUP cv ON b.alpha_id = cv.id",
        },
        {
            "column": "BETA_NAME",
            "source_schema": "MYS", "source_table": "LOOKUP",
            "source_attribute": "NAME",
            "transformation": "left join MYS.LOOKUP cv ON b.beta_id = cv.id",
        },
    ]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows, odi_model=model,
    )
    # Two distinct LEFT JOIN MYS.LOOKUP entries (one per role).
    assert res.sql.upper().count("LEFT JOIN MYS.LOOKUP") == 2, (
        f"Expected 2 LEFT JOINs to MYS.LOOKUP, got:\n{res.sql}"
    )
    # Each projection on its own role alias (case-insensitive).
    sql_up = res.sql.upper()
    assert "ALPHA_LK.NAME" in sql_up, (
        f"Expected ALPHA_LK.NAME projection in:\n{res.sql}"
    )
    assert "BETA_LK.NAME" in sql_up, (
        f"Expected BETA_LK.NAME projection in:\n{res.sql}"
    )


def test_unimplementable_prose_emits_null_for_multi_role_with_no_match():
    """When the row's transformation says "Parse ... does not exist today"
    AND the target column can't be matched to any ODI role, emit NULL with
    a NULL_UNIMPLEMENTED_PROSE provenance + operator-readable note."""
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    base = TableRef(schema="MYS", table="BASE")
    lk = TableRef(schema="MYS", table="LOOKUP")
    step = StagingStep(
        step_id=1, name="STG1",
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="alpha_lk", ref=lk),
                on_sql="b.alpha_id = alpha_lk.id",
            ),
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="beta_lk", ref=lk),
                on_sql="b.beta_id = beta_lk.id",
            ),
        ],
    )
    model = ODIModel(target=TableRef(schema="MYS", table="TGT"), staging_steps=[step])
    tdef = _td(("BASE_ID", "NUMBER"), ("ZED_CODE", "VARCHAR2(20)"))
    rows = [
        # Anchor row -- gives base table detector something to pin to BASE.
        {
            "column": "BASE_ID",
            "source_schema": "MYS", "source_table": "BASE",
            "source_attribute": "ID",
        },
        {
            "column": "ZED_CODE",
            "source_schema": "MYS", "source_table": "LOOKUP",
            "source_attribute": "CODE",
            # No DRD AD join + prose says parse + does not exist today.
            "transformation": "For B.X = 5, use FIELD.  Parse to extract Zed Code.  "
                             "Lookup CODE (does not exist today)."
        },
    ]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows, odi_model=model,
    )
    assert res.provenance_summary.get("NULL_UNIMPLEMENTED_PROSE", 0) == 1
    # No alpha_lk or beta_lk projection for ZED_CODE.
    assert "alpha_lk.CODE" not in res.sql
    assert "beta_lk.CODE" not in res.sql
    # Operator-readable hint in the comment.
    assert "operator implementation" in res.sql or "Source DRD rule needs" in res.sql


def test_base_table_exempt_from_multi_role_routing():
    """When fq == base_fq, the multi-role check must NOT block the projection
    -- the base FROM clause provides the alias.  Critical: prevents the
    unimplementable-prose check from NULL-ing out columns whose source is
    the base table (e.g. SDIRA_TXN_YR projecting from t.TRD_NUM)."""
    from app.sql_model.types import (
        AliasBinding, JoinEdge, JoinType, ODIModel, StagingStep, TableRef,
    )
    base = TableRef(schema="MYS", table="BASE")
    base2 = TableRef(schema="MYS", table="BASE")  # second alias -> multi-role
    step = StagingStep(
        step_id=1, name="STG1",
        join_graph=[
            JoinEdge(
                join_type=JoinType.LEFT,
                driving=AliasBinding(alias="b", ref=base),
                joined=AliasBinding(alias="b_rel", ref=base2),
                on_sql="b.id = b_rel.parent_id",
            ),
        ],
    )
    model = ODIModel(target=TableRef(schema="MYS", table="TGT"), staging_steps=[step])
    tdef = _td(("XYZ_YR", "VARCHAR2(15)"))
    rows = [{
        "column": "XYZ_YR",
        "source_schema": "MYS", "source_table": "BASE",
        "source_attribute": "TRD_NUM",
        # Prose contains "parse" -> would normally trigger unimplementable;
        # but source is base table, must NOT be NULL-ed.
        "transformation": "Parse to extract last two digits and add century part."
    }]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows, odi_model=model,
    )
    # Must NOT be NULL_UNIMPLEMENTED_PROSE -- base table is always reachable.
    assert res.provenance_summary.get("NULL_UNIMPLEMENTED_PROSE", 0) == 0
    # Must project from the base alias.
    assert ".TRD_NUM" in res.sql.upper()


def test_out_of_scope_targets_skipped_from_insert():
    """De-scoped target columns (e.g. struck-through DRD rows) must be
    DROPPED entirely from the INSERT column list AND the SELECT projection.
    Generic, no business names."""
    tdef = _td(("KEEP_ME", "NUMBER"), ("SKIP_ME", "NUMBER"), ("ALSO_KEEP", "VARCHAR2(50)"))
    rows = [
        {"column": "KEEP_ME", "source_table": "SRC", "source_attribute": "KEEP_ME"},
        {"column": "ALSO_KEEP", "source_table": "SRC", "source_attribute": "ALSO_KEEP"},
    ]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
        out_of_scope_targets={"SKIP_ME"},
    )
    # SKIP_ME absent from INSERT col list AND SELECT projection.
    assert "SKIP_ME" not in res.sql
    # KEEP_ME and ALSO_KEEP present.
    assert "KEEP_ME" in res.sql
    assert "ALSO_KEEP" in res.sql
    assert res.column_count == 2  # only the two kept


def test_substring_parse_emits_case_when_filter():
    """When DRD prose describes SUBSTR-based parse with a filter condition,
    the emitter must produce a CASE WHEN ... THEN SUBSTR ... ELSE NULL END
    expression with the filter alias rewritten to the base alias."""
    tdef = _td(("PARSED_VAL", "VARCHAR2(20)"))
    rows = [{
        "column": "PARSED_VAL",
        "source_schema": "MYS", "source_table": "SRC",
        "source_attribute": "RAW_FIELD",
        "transformation": "For SRC.STREAM_ID = 60, use RAW_FIELD.  "
                         "Parse to extract first three chars.",
    }]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows,
    )
    assert res.provenance_summary.get("DRD_SUBSTR_PARSE", 0) == 1
    assert "SUBSTR(" in res.sql
    assert "CASE WHEN" in res.sql
    # Filter alias rewritten from SRC.STREAM_ID to base-alias.STREAM_ID
    # (whatever alias _detect_base_table picks; never the bare table name).
    assert "SRC.STREAM_ID" not in res.sql


def test_substring_parse_skipped_when_source_not_base_table():
    """SUBSTR rule must NOT fire when source_table is a secondary lookup
    -- those need either DRD AD join or NULL_UNIMPLEMENTED_PROSE."""
    tdef = _td(("OTHER", "NUMBER"), ("OTHER2", "NUMBER"), ("OTHER3", "NUMBER"),
               ("LOOKUP_VAL", "VARCHAR2(20)"))
    rows = [
        # Anchor rows so BASE wins the base-detection vote.
        {"column": "OTHER", "source_table": "BASE", "source_attribute": "X"},
        {"column": "OTHER2", "source_table": "BASE", "source_attribute": "Y"},
        {"column": "OTHER3", "source_table": "BASE", "source_attribute": "Z"},
        {
            "column": "LOOKUP_VAL",
            "source_schema": "MYS", "source_table": "LOOKUP",  # NOT base!
            "source_attribute": "CODE",
            "transformation": "Parse to extract first three chars.",
        },
    ]
    res = emit_insert_drd_first(
        target_schema="MYS", target_table="TGT",
        target_definition=tdef, analysis_rows=rows,
    )
    # SUBSTR should NOT have fired -- LOOKUP is not the base table.
    assert res.provenance_summary.get("DRD_SUBSTR_PARSE", 0) == 0


def test_dedupe_predicates_collapses_alias_variants():
    """Semantically-identical predicates across DRD rows must collapse to
    one in the ON clause.  Composite-key joins (distinct bare-col pairs)
    must be preserved."""
    from app.sql_model.drd_first_emitter import _dedupe_predicates
    # Same predicate, different case + operand order.
    inp = ["T.X = LK.Y", "t.x = lk.y", "LK.Y = T.X"]
    assert _dedupe_predicates(inp) == ["T.X = LK.Y"]
    # Composite-key join (two genuinely different pairs) preserved.
    inp2 = ["T.X = LK.Y", "T.A = LK.B"]
    assert _dedupe_predicates(inp2) == ["T.X = LK.Y", "T.A = LK.B"]
    # Empty / None tolerated.
    assert _dedupe_predicates([]) == []
    assert _dedupe_predicates(["", "T.X = LK.Y"]) == ["T.X = LK.Y"]


def test_canonical_predicate_key_alias_insensitive():
    """Same bare-column-pair under any alias casing = same key."""
    from app.sql_model.drd_first_emitter import _canonical_predicate_key
    assert _canonical_predicate_key("T.X = LK.Y") == _canonical_predicate_key("t.x = lk.y")
    assert _canonical_predicate_key("T.X = LK.Y") == _canonical_predicate_key("LK.Y = T.X")
    # Different bare-column pairs -> different keys.
    assert _canonical_predicate_key("T.X = LK.Y") != _canonical_predicate_key("T.X = LK.Z")


def test_oracle_outer_marker_stripped_from_ansi_joins():
    """Operator-locked 2026-05-29: mixing ``(+)`` with ``LEFT JOIN ... ON`` is
    invalid Oracle.  The emitter MUST strip ``(+)`` from every ON predicate."""
    from app.sql_model.drd_first_emitter import _strip_oracle_outer_marker
    assert _strip_oracle_outer_marker("a.x = b.y (+)") == "a.x = b.y"
    assert _strip_oracle_outer_marker("a.x(+) = b.y") == "a.x = b.y"
    assert _strip_oracle_outer_marker("a.x = b.y") == "a.x = b.y"
    assert _strip_oracle_outer_marker("a.x = b.y(+) AND c.z = d.w(+)") == "a.x = b.y AND c.z = d.w"
    assert _strip_oracle_outer_marker("") == ""


def test_no_pdm_miss_in_output():
    """The new emitter must NEVER emit PDM_MISS markers -- those were the
    operator's complaint about the old generator."""
    tdef = _td(("A", "NUMBER"), ("B", "VARCHAR2(10)"))
    rows = [
        {"column": "A", "source_table": "S", "source_attribute": "A"},
        {"column": "B"},  # no source info at all
    ]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
    )
    assert "PDM_MISS" not in res.sql


def test_two_rows_same_source_table_share_alias():
    """Two rows from the same physical table must share one alias and one
    placeholder JOIN (the JOIN is placeholder because no ODI model provides
    the real ON predicate)."""
    tdef = _td(("LK_NAME", "VARCHAR2(50)"), ("LK_CODE", "VARCHAR2(10)"))
    rows = [
        {"column": "LK_NAME", "source_schema": "REF", "source_table": "LOOKUP_TBL",
         "source_attribute": "NAME"},
        {"column": "LK_CODE", "source_schema": "REF", "source_table": "LOOKUP_TBL",
         "source_attribute": "CODE"},
    ]
    res = emit_insert_drd_first(
        target_schema="X", target_table="Y",
        target_definition=tdef, analysis_rows=rows,
    )
    # Both rows alias from the same source table -> at most one placeholder JOIN
    assert res.sql.count("LEFT JOIN REF.LOOKUP_TBL") <= 1
    assert ".NAME" in res.sql and ".CODE" in res.sql
