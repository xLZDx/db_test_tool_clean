"""Tests for the DRD-driven comparator emitter (Phase 7.6).

Operator-locked architecture (2026-05-30): the emitter projects from
DRD-stated sources for every column.  ODI is used ONLY for verification
(verdict annotation in comments) -- ODI is never the source of the
output.

These tests pin the architectural invariant: every projection that
isn't NULL must come from a DRD-derived alias + DRD source_attribute,
NEVER from a `S.<col>` style ODI staging reference.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from app.sql_model.comparator import ComparisonResult
from app.sql_model.comparator_driven_emitter import (
    ComparatorDrivenInsert,  # back-compat alias
    DrdDrivenInsert,
    _build_alias_map,
    _extract_odi_using_subselect,
    _is_lookup_table,
    _lookup_discriminator,
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
        drd_attr=kw.get("drd_attr", target_col.upper()),
        odi_schema=kw.get("odi_schema", ""),
        odi_table=kw.get("odi_table", ""),
        odi_col=kw.get("odi_col", target_col.upper()),
        odi_expr_sql=kw.get("odi_expr_sql", ""),
        odi_step=kw.get("odi_step", 1),
        explanation="",
        mismatch_kind=MismatchKind.NONE,
        drd_logic=kw.get("drd_logic", ""),
        odi_logic="",
    )


def _make_model() -> ODIModel:
    """ODI model is irrelevant to DRD-driven emitter except for API
    symmetry; return a minimal one."""
    return ODIModel(
        target=TableRef(schema="T", table="X"),
        staging_steps=[],
        final_select_sql="",
        final_insert_columns=[],
    )


def _select_lines(sql: str) -> list:
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


# ── ARCHITECTURAL INVARIANT: DRD is the source, NEVER ODI ───────────────────

def test_emitter_never_projects_from_S_alias():
    """Operator-locked Phase 7.6: NO projection line may use `S.<col>`
    (that would be ODI-driven, the rejected v7.5 approach)."""
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "COL_A", "source_schema": "S", "source_table": "T", "source_attribute": "A"},
            {"physical_name": "COL_B", "source_schema": "S", "source_table": "T", "source_attribute": "B"},
        ],
        comparison_results=[
            _make_result("COL_A", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="A"),
            _make_result("COL_B", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="B"),
        ],
        odi_model=_make_model(),
    )
    for line in _select_lines(res.sql):
        assert not line.lstrip().startswith("S."), (
            f"projection from S.<col> -- emitter must use DRD-derived alias, "
            f"not ODI staging: {line!r}"
        )


def test_matched_column_projected_from_drd_table_alias():
    """MATCHED column with DRD source CCAL.APA.YIELD must project from
    an alias of APA (not from S, not from ODI staging)."""
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "YLD", "source_schema": "CCAL", "source_table": "APA", "source_attribute": "YIELD"},
        ],
        comparison_results=[
            _make_result(
                "YLD", ComparisonVerdict.MATCHED,
                drd_schema="CCAL", drd_table="APA", drd_attr="YIELD",
                odi_table="APA", odi_col="YIELD",
            ),
        ],
        odi_model=_make_model(),
    )
    select = _select_lines(res.sql)
    assert len(select) == 1
    line = select[0]
    # Projection is from APA's alias + YIELD attribute (DRD-driven).
    assert "APA.YIELD" in line.upper() or "apa.YIELD" in line, line
    assert ".YIELD" in line.upper()
    assert " AS YLD" in line
    # Base table is in FROM clause.
    assert "FROM CCAL.APA" in res.sql.upper()


def test_real_mismatch_projects_drd_source_not_odi():
    """REAL_MISMATCH: DRD says X.Y, ODI says X.Z.  Emitter projects
    X.Y (DRD spec); ODI divergence shown in COMMENT only."""
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "COL", "source_schema": "S", "source_table": "T",
             "source_attribute": "DRD_ATTR"},
        ],
        comparison_results=[
            _make_result(
                "COL", ComparisonVerdict.REAL_MISMATCH,
                drd_schema="S", drd_table="T", drd_attr="DRD_ATTR",
                odi_table="OTHER_TABLE", odi_col="OTHER_COL",
            ),
        ],
        odi_model=_make_model(),
    )
    select = _select_lines(res.sql)
    assert len(select) == 1
    # DRD source MUST be in the projection.
    assert "DRD_ATTR" in select[0]
    # ODI divergent value must NOT be in the projection (only in comment).
    proj_part = select[0].split("--")[0]
    assert "OTHER_COL" not in proj_part
    # But comment WARNS about ODI divergence.
    assert "REAL_MISMATCH" in select[0]
    assert "OTHER_COL" in select[0]
    assert "COL" in res.real_mismatch_cols


def test_source_missing_emits_null_with_drd_source_in_comment():
    """SOURCE_MISSING: DRD says X.Y; ODI doesn't have it.  Emit DRD's
    spec (with warning).  Operator should fix ODI or accept gap."""
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "GAP", "source_schema": "S", "source_table": "T",
             "source_attribute": "GAP_ATTR"},
        ],
        comparison_results=[
            _make_result(
                "GAP", ComparisonVerdict.SOURCE_MISSING,
                drd_schema="S", drd_table="T", drd_attr="GAP_ATTR",
            ),
        ],
        odi_model=_make_model(),
    )
    select = _select_lines(res.sql)
    assert len(select) == 1
    # DRD source is still projected (operator's intent: ODI gap, not DRD gap).
    assert "GAP_ATTR" in select[0]
    assert "SOURCE_MISSING" in select[0]
    assert "GAP" in res.source_missing_cols


# ── Alias map invariants ──────────────────────────────────────────────────────

def test_base_table_is_most_frequent_source():
    """Most-frequent DRD source_table becomes the base; alias = 't'."""
    tuples = [
        ("A", "S.APA", "COL_A", "", "MATCHED"),
        ("B", "S.APA", "COL_B", "", "MATCHED"),
        ("C", "S.APA", "COL_C", "", "MATCHED"),
        ("D", "S.TXN", "COL_D", "", "MATCHED"),
    ]
    base, per_row, _ = _build_alias_map(tuples)
    assert base == "S.APA"
    assert per_row[("S.APA", "")] == "t"


def test_lookup_table_per_discriminator_aliasing():
    """Multiple uses of CL_VAL with different discriminators get
    DIFFERENT aliases (CL_VAL_1, CL_VAL_2, ...)."""
    tuples = [
        ("BASE_COL", "S.TXN", "ID", "", "MATCHED"),
        ("X", "S.CL_VAL", "CL_VAL_CODE", "Use CL_VAL where CL_SCM_ID = 114", "MATCHED"),
        ("Y", "S.CL_VAL", "CL_VAL_CODE", "Use CL_VAL where CL_SCM_ID = 115", "MATCHED"),
    ]
    base, per_row, _ = _build_alias_map(tuples)
    assert base == "S.TXN"
    # Two distinct lookup aliases.
    assert per_row[("S.CL_VAL", "114")] != per_row[("S.CL_VAL", "115")]


def test_lookup_table_same_discriminator_shares_alias():
    """Two rows hitting the SAME (lookup_table, discriminator) share
    one alias."""
    tuples = [
        ("BASE_COL", "S.TXN", "ID", "", "MATCHED"),
        ("X", "S.CL_VAL", "CL_VAL_CODE", "Use CL_VAL where CL_SCM_ID = 114", "MATCHED"),
        ("Y", "S.CL_VAL", "CL_VAL_NM", "Use CL_VAL where CL_SCM_ID = 114", "MATCHED"),
    ]
    base, per_row, _ = _build_alias_map(tuples)
    assert len([k for k in per_row.keys() if k[0] == "S.CL_VAL"]) == 1


# ── Discriminator extraction ──────────────────────────────────────────────────

def test_discriminator_extracted_from_transformation_text():
    assert _lookup_discriminator("Use TAX_LOT_TXN_TP_ID as CL_Val_id where CL_SCM_ID = 84") == "84"
    assert _lookup_discriminator("Use CL_VAL where CL_SCM_ID=114") == "114"
    assert _lookup_discriminator("plain join, no discriminator") == ""
    assert _lookup_discriminator("") == ""


def test_is_lookup_table_recognises_known_markers():
    assert _is_lookup_table("CCAL.CL_VAL") is True
    assert _is_lookup_table("X.AR_DIM") is True
    assert _is_lookup_table("X.SOMETHING_LKU") is True
    assert _is_lookup_table("X.SOMETHING_MAP") is True
    assert _is_lookup_table("X.APA") is False
    assert _is_lookup_table("") is False


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_drd_returns_extraction_failed():
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[], comparison_results=[], odi_model=_make_model(),
    )
    assert res.extraction_failed is True
    assert res.sql == ""


def test_drd_row_with_no_source_table_emits_null():
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "BASE", "source_schema": "S", "source_table": "T", "source_attribute": "ID"},
            {"physical_name": "NO_SOURCE", "source_schema": "", "source_table": "", "source_attribute": ""},
        ],
        comparison_results=[
            _make_result("BASE", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="ID"),
            _make_result("NO_SOURCE", ComparisonVerdict.SOURCE_MISSING),
        ],
        odi_model=_make_model(),
    )
    select = _select_lines(res.sql)
    assert len(select) == 2
    # First row gets a proper projection
    assert "ID" in select[0]
    # Second row gets NULL because DRD has no source
    assert "NULL" in select[1]
    assert "NO_SOURCE" in res.null_substitutions


def test_null_for_not_null_column_flagged_as_runtime_risk():
    tdef = {"columns": [{"name": "PK_COL", "nullable": False, "is_pk": True}]}
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "BASE", "source_schema": "S", "source_table": "T", "source_attribute": "ID"},
            {"physical_name": "PK_COL", "source_schema": "", "source_table": "", "source_attribute": ""},
        ],
        comparison_results=[
            _make_result("BASE", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="ID"),
            _make_result("PK_COL", ComparisonVerdict.SOURCE_MISSING),
        ],
        odi_model=_make_model(),
        target_definition=tdef,
    )
    assert "PK_COL" in res.null_in_not_null_risk_cols
    assert "ORA-01400" in res.sql


def test_oracle_parses_cleanly_with_dummy_joins():
    """Even with CROSS JOIN placeholders, the SQL must parse under
    sqlglot Oracle dialect (we generate valid-but-incomplete SQL,
    NOT garbage)."""
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "A", "source_schema": "S", "source_table": "T", "source_attribute": "A"},
            {"physical_name": "B", "source_schema": "S", "source_table": "OTHER", "source_attribute": "B"},
        ],
        comparison_results=[
            _make_result("A", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="A"),
            _make_result("B", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="OTHER", drd_attr="B"),
        ],
        odi_model=_make_model(),
    )
    # CROSS JOIN appears for the non-base table
    assert "CROSS JOIN" in res.sql
    # And join_undetermined_tables records it
    assert res.join_undetermined_tables, "operator must see which joins lack ON predicates"
    # Validate SQL parses
    import sqlglot
    try:
        sqlglot.parse_one(res.sql, dialect="oracle")
    except Exception as e:
        pytest.fail(f"emitted SQL does not parse: {e}\n\nSQL:\n{res.sql}")


# ── Comma-before-comment (Oracle parse safety) ───────────────────────────────

def test_comma_placement_for_oracle_parse():
    res = emit_insert_comparator_driven(
        target_schema="C", target_table="X",
        drd_rows=[
            {"physical_name": "A", "source_schema": "S", "source_table": "T", "source_attribute": "A"},
            {"physical_name": "B", "source_schema": "S", "source_table": "T", "source_attribute": "B"},
        ],
        comparison_results=[
            _make_result("A", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="A"),
            _make_result("B", ComparisonVerdict.MATCHED, drd_schema="S", drd_table="T", drd_attr="B"),
        ],
        odi_model=_make_model(),
    )
    select = _select_lines(res.sql)
    # First line: comma BEFORE the comment.
    for line in select[:-1]:
        if "--" in line:
            idx = line.index("--")
            assert "," not in line[idx:], f"comma inside comment: {line!r}"
    # Last line must NOT have a trailing comma before the comment.
    last_proj = select[-1].split("--")[0]
    assert not last_proj.rstrip().endswith(","), f"last projection ends with comma: {select[-1]!r}"


# ── Back-compat: paren walker still passes its own invariants ────────────────

def test_paren_walker_extracts_inner_select():
    """The Phase 7.5 helper is retained for any external caller that
    used it.  Its hardened paren-walker invariants still hold."""
    sql = "merge into T using (\n  select COL_A from STG\n) S when matched then ...\n"
    inner = _extract_odi_using_subselect(sql)
    assert "select COL_A from STG" in inner


def test_paren_walker_skips_string_literal_parens():
    sql = (
        "merge into T using (\n"
        "  select DECODE(X, 'Y(es)', 1, 0) AS Y from STG\n"
        ") S when matched then ...\n"
    )
    inner = _extract_odi_using_subselect(sql)
    assert "STG" in inner


def test_back_compat_alias_exposed():
    """Older callers import `ComparatorDrivenInsert`; keep it working."""
    assert ComparatorDrivenInsert is DrdDrivenInsert
