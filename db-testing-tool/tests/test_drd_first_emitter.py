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
