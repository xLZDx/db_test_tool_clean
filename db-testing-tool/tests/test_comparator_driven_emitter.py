"""Tests for app/sql_model/comparator_driven_emitter.py.

Operator-locked Phase 7.5 (2026-05-30): the new emitter REUSES the
comparator's per-column verdict and projects from ODI's USING(...)
inner SELECT, so the JOIN graph is honoured by construction and
PROVENANCE_FALLBACK is eliminated.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from app.sql_model.comparator import ComparisonResult
from app.sql_model.comparator_driven_emitter import (
    ComparatorDrivenInsert,
    _extract_odi_using_subselect,
    emit_insert_comparator_driven,
)
from app.sql_model.types import (
    ComparisonVerdict, MismatchKind, ODIModel, TableRef,
)


def _make_result(target_col: str, verdict: ComparisonVerdict, **kw) -> ComparisonResult:
    return ComparisonResult(
        verdict=verdict,
        target_col=target_col.upper(),
        drd_schema=kw.get("drd_schema", ""),
        drd_table=kw.get("drd_table", ""),
        drd_attr=kw.get("drd_attr", ""),
        odi_schema=kw.get("odi_schema", ""),
        odi_table=kw.get("odi_table", ""),
        odi_col=kw.get("odi_col", target_col.upper()),
        odi_expr_sql=kw.get("odi_expr_sql", ""),
        odi_step=kw.get("odi_step", 1),
        explanation="",
        mismatch_kind=MismatchKind.NONE,
        drd_logic="",
        odi_logic="",
    )


def _make_model(final_select_sql: str, final_insert_columns: list) -> ODIModel:
    return ODIModel(
        target=TableRef(schema="T", table="X"),
        staging_steps=[],
        final_select_sql=final_select_sql,
        final_insert_columns=final_insert_columns,
    )


# ── _extract_odi_using_subselect ──────────────────────────────────────────────

def test_extract_using_subselect_returns_inner_select():
    """The MERGE INTO ... USING (...) wrapper has the inner SELECT we
    need to project from.  Extractor must return everything between
    the outer parens."""
    sql = (
        "merge into TARGET T\n"
        "using\n"
        "(\n"
        "    select COL_A, COL_B from STG\n"
        ") S\n"
        "when matched then update set ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "select COL_A, COL_B from STG" in inner
    assert "merge" not in inner.lower()
    assert "when matched" not in inner.lower()


def test_extract_using_subselect_handles_nested_parens():
    """Balanced-paren walker must NOT stop on the first inner `)`."""
    sql = (
        "merge into T using (\n"
        "    select COL_A from (select 1 from dual) x\n"
        ") S when matched then ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "select COL_A from (select 1 from dual) x" in inner


def test_extract_using_subselect_empty_on_no_using():
    assert _extract_odi_using_subselect("") == ""
    assert _extract_odi_using_subselect("select 1 from dual") == ""


# ── emit_insert_comparator_driven ─────────────────────────────────────────────

def test_emit_matched_column_projects_from_S_alias():
    """MATCHED column => project `S.<col>` from the inner SELECT."""
    model = _make_model(
        final_select_sql="merge into X T using (select COL_A from STG) S when matched then ...",
        final_insert_columns=["COL_A"],
    )
    res = emit_insert_comparator_driven(
        target_schema="CTL", target_table="X",
        drd_rows=[{"physical_name": "COL_A", "source_table": "STG", "source_attribute": "COL_A"}],
        comparison_results=[_make_result("COL_A", ComparisonVerdict.MATCHED, odi_col="COL_A")],
        odi_model=model,
    )
    assert "S.COL_A" in res.sql
    assert "INSERT INTO CTL.X" in res.sql
    assert "select COL_A from STG" in res.sql
    assert res.matched_count == 1


def test_emit_source_missing_column_emits_null_with_comment():
    """SOURCE_MISSING => emit NULL with comment quoting the DRD source
    so operator sees the gap."""
    model = _make_model(
        final_select_sql="merge into X using (select 1 from dual) S when matched then ...",
        final_insert_columns=[],
    )
    res = emit_insert_comparator_driven(
        target_schema="CTL", target_table="X",
        drd_rows=[{"physical_name": "GAP_COL", "source_table": "TGT_TBL", "source_attribute": "TGT_ATTR"}],
        comparison_results=[_make_result(
            "GAP_COL", ComparisonVerdict.SOURCE_MISSING,
            drd_table="TGT_TBL", drd_attr="TGT_ATTR",
        )],
        odi_model=model,
    )
    assert "NULL" in res.sql
    assert "SOURCE_MISSING" in res.sql
    # The DRD source must be quoted in the comment so operator sees it.
    assert "TGT_TBL" in res.sql
    assert "TGT_ATTR" in res.sql
    assert res.source_missing_cols == ["GAP_COL"]
    assert res.null_substitutions == ["GAP_COL"]


def test_emit_real_mismatch_projects_odi_with_warning_comment():
    """REAL_MISMATCH => project ODI's value but warn operator that DRD
    disagrees and they MUST decide."""
    model = _make_model(
        final_select_sql="merge into X using (select COL_X from STG) S when matched then ...",
        final_insert_columns=["COL_X"],
    )
    res = emit_insert_comparator_driven(
        target_schema="CTL", target_table="X",
        drd_rows=[{"physical_name": "COL_X", "source_table": "T", "source_attribute": "OTHER"}],
        comparison_results=[_make_result(
            "COL_X", ComparisonVerdict.REAL_MISMATCH,
            drd_attr="OTHER", odi_table="A", odi_col="COL_X",
        )],
        odi_model=model,
    )
    assert "S.COL_X" in res.sql
    assert "REAL_MISMATCH" in res.sql
    assert "MUST decide" in res.sql
    assert res.real_mismatch_cols == ["COL_X"]


def test_emit_odi_extra_columns_skipped():
    """ODI_EXTRA is impossible in target_cols (no DRD declaration),
    but if a stray ODI_EXTRA row sneaks in, the emitter skips it
    safely instead of crashing."""
    model = _make_model(
        final_select_sql="merge into X using (select 1 from dual) S when matched then ...",
        final_insert_columns=[],
    )
    res = emit_insert_comparator_driven(
        target_schema="CTL", target_table="X",
        drd_rows=[{"physical_name": "REAL_COL", "source_table": "T", "source_attribute": "A"}],
        comparison_results=[
            _make_result("REAL_COL", ComparisonVerdict.MATCHED),
            _make_result("STRAY", ComparisonVerdict.ODI_EXTRA),
        ],
        odi_model=model,
    )
    assert "STRAY" not in res.sql
    assert "REAL_COL" in res.sql


def test_emit_comma_placement_keeps_oracle_parseable():
    """Comments must come AFTER the comma (else `--` swallows it and
    breaks Oracle parsing).  Regression test for the bug found while
    building the emitter."""
    model = _make_model(
        final_select_sql="merge into X using (select A, B from S) S when matched then ...",
        final_insert_columns=["A", "B"],
    )
    res = emit_insert_comparator_driven(
        target_schema="CTL", target_table="X",
        drd_rows=[
            {"physical_name": "A", "source_table": "S", "source_attribute": "A"},
            {"physical_name": "B", "source_table": "S", "source_attribute": "B"},
        ],
        comparison_results=[
            _make_result("A", ComparisonVerdict.MATCHED, odi_col="A"),
            _make_result("B", ComparisonVerdict.MATCHED, odi_col="B"),
        ],
        odi_model=model,
    )
    # Check comma is OUTSIDE the comment for the first projection.
    assert "S.A,  -- " in res.sql or "S.A,\n" in res.sql
    # And no SELECT line should have a `--` followed by a `,`.
    for line in res.sql.splitlines():
        if "--" in line:
            idx = line.index("--")
            assert "," not in line[idx:], f"comma inside comment on line: {line}"


def test_emit_empty_drd_returns_empty_sql():
    """No DRD target columns => can't emit; return empty SQL + note."""
    model = _make_model(
        final_select_sql="merge into X using (select 1 from dual) S when matched then ...",
        final_insert_columns=[],
    )
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[], comparison_results=[], odi_model=model,
    )
    assert res.sql == ""
    assert any("zero target columns" in n for n in res.notes)


# ── Phase 7.5 review (BLOCKER + MAJOR) regression tests ──────────────────────

def _select_lines(sql: str) -> list:
    """Extract the SELECT-projection lines (the ones that produce a
    column value), as a positional list.  Excludes header / INSERT
    INTO / FROM."""
    out = []
    seen_select = False
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("SELECT") and not seen_select:
            seen_select = True
            continue
        if not seen_select:
            continue
        if stripped.upper().startswith("FROM"):
            break
        if not stripped:
            continue
        out.append(line)
    return out


def test_paren_walker_skips_string_literal_parens():
    """Hardened paren walker must NOT close on `(` or `)` inside
    single-quoted string literals.  Regression for review BLOCKER."""
    # ODI-style USING wrapper with DECODE that has `'Y(es)'` inside.
    sql = (
        "merge into T using (\n"
        "  select DECODE(X, 'Y(es)', 1, 0) AS Y from STG\n"
        ") S when matched then ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "DECODE" in inner
    assert "'Y(es)'" in inner
    # If naive walker had run, it would close on the `)` after 'Y(es' --
    # the inner would be truncated.  Assert full extraction.
    assert "STG" in inner


def test_paren_walker_skips_block_comment_parens():
    """`)` inside `/* ... */` must not affect depth."""
    sql = (
        "merge into T using (\n"
        "  /* footnote with )((( random parens */\n"
        "  select COL_A from STG\n"
        ") S when matched then ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "select COL_A from STG" in inner


def test_paren_walker_skips_line_comment_parens():
    """`)` after `--` must not affect depth until newline."""
    sql = (
        "merge into T using (\n"
        "  -- trailing  ) noise\n"
        "  select COL_A from STG\n"
        ") S when matched then ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "select COL_A from STG" in inner


def test_extraction_failure_flag_set_on_no_using():
    """When the paren walker can't find USING, set extraction_failed=True
    so the caller can refuse to show empty SQL silently."""
    model = _make_model(final_select_sql="garbage", final_insert_columns=[])
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[{"physical_name": "Y", "source_table": "T", "source_attribute": "Y"}],
        comparison_results=[],
        odi_model=model,
    )
    assert res.extraction_failed is True
    assert "USING" in res.extraction_failure_reason
    assert res.sql == ""


def test_source_missing_into_not_null_column_flagged():
    """When target_definition says a SOURCE_MISSING column is NOT NULL
    (or is a PK), emit a clear runtime-risk warning in the SQL comment
    AND list the column in null_in_not_null_risk_cols.  Operator must
    see the constraint risk BEFORE running."""
    model = _make_model(
        final_select_sql="merge into X using (select 1 from dual) S when matched then ...",
        final_insert_columns=[],
    )
    tdef = {
        "columns": [
            {"name": "GAP_COL", "nullable": False, "is_pk": True},
        ],
    }
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[{"physical_name": "GAP_COL", "source_table": "T", "source_attribute": "A"}],
        comparison_results=[_make_result("GAP_COL", ComparisonVerdict.SOURCE_MISSING)],
        odi_model=model,
        target_definition=tdef,
    )
    assert "GAP_COL" in res.null_in_not_null_risk_cols
    assert "OPERATOR MUST REVIEW" in res.sql
    assert "ORA-01400" in res.sql


def test_select_line_position_for_matched_column():
    """Position-aware test (review MAJOR 2): the SELECT-list line for
    the Nth DRD column must project from the Nth column expression.
    Catches a bug where the emitter swapped columns."""
    model = _make_model(
        final_select_sql="merge into X using (select A, B, C from S) S when matched then ...",
        final_insert_columns=["A", "B", "C"],
    )
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "A"}, {"physical_name": "B"}, {"physical_name": "C"},
        ],
        comparison_results=[
            _make_result("A", ComparisonVerdict.MATCHED, odi_col="A"),
            _make_result("B", ComparisonVerdict.SOURCE_MISSING, drd_attr="B"),
            _make_result("C", ComparisonVerdict.MATCHED, odi_col="C"),
        ],
        odi_model=model,
    )
    select_lines = _select_lines(res.sql)
    assert len(select_lines) == 3
    assert select_lines[0].lstrip().startswith("S.A"), f"slot 0: {select_lines[0]!r}"
    assert select_lines[1].lstrip().startswith("NULL"), f"slot 1: {select_lines[1]!r}"
    assert select_lines[2].lstrip().startswith("S.C"), f"slot 2: {select_lines[2]!r}"


def test_odi_extra_stray_emits_null_not_skip():
    """If an ODI_EXTRA row accidentally has a DRD-declared target_col,
    the emitter must emit explicit NULL (not skip) so the SELECT-list
    cardinality matches the INSERT column list -- review MINOR."""
    model = _make_model(
        final_select_sql="merge into X using (select A, STRAY from S) S when matched then ...",
        final_insert_columns=["A", "STRAY"],
    )
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "A"},
            {"physical_name": "STRAY"},  # DRD declares it
        ],
        comparison_results=[
            _make_result("A", ComparisonVerdict.MATCHED, odi_col="A"),
            _make_result("STRAY", ComparisonVerdict.ODI_EXTRA, odi_col="STRAY"),  # mis-routed
        ],
        odi_model=model,
    )
    select_lines = _select_lines(res.sql)
    assert len(select_lines) == 2  # cardinality preserved
    # The stray ODI_EXTRA must be NULL with a [BUG] marker.
    assert any("[BUG]" in l for l in select_lines)
