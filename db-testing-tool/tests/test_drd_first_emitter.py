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
