"""Unit tests for P5 static offline validator (KBLookup + validate_model_offline).

All tests work entirely offline — no DB, no network, no file I/O beyond
what we explicitly construct in memory.
"""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from app.sql_model.types import (
    AliasBinding,
    ColumnMapping,
    ODIModel,
    Provenance,
    ResolvedColumn,
    StagingStep,
    TableRef,
    UnresolvedExpr,
)
from app.sql_model.static_validator import (
    KBLookup,
    StaticVerdict,
    ValidationResult,
    validate_model_offline,
)


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_KB = {
    "pdm": {
        "schemas": [
            {
                "schema": "SRC",
                "tables": [
                    {
                        "name": "SRC_TABLE",
                        "columns": [
                            {"name": "ID",   "data_type": "NUMBER",   "nullable": False, "is_pk": True},
                            {"name": "NAME", "data_type": "VARCHAR2", "nullable": True,  "is_pk": False},
                            {"name": "VAL",  "data_type": "NUMBER",   "nullable": True,  "is_pk": False},
                        ],
                    }
                ],
            },
            {
                "schema": "TGT",
                "tables": [
                    {
                        "name": "TGT_TABLE",
                        "columns": [
                            {"name": "ID",        "data_type": "NUMBER",   "nullable": False, "is_pk": True},
                            {"name": "NAME",      "data_type": "VARCHAR2", "nullable": True,  "is_pk": False},
                            {"name": "REQUIRED",  "data_type": "VARCHAR2", "nullable": False, "is_pk": False},
                        ],
                    }
                ],
            },
        ]
    }
}


def _write_kb(data: dict) -> pathlib.Path:
    """Write a KB dict to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
    json.dump(data, tmp)
    tmp.flush()
    tmp.close()
    return pathlib.Path(tmp.name)


def _make_kb() -> KBLookup:
    path = _write_kb(_MINIMAL_KB)
    return KBLookup(path)


def _src_ref() -> TableRef:
    return TableRef(schema="SRC", table="SRC_TABLE")


def _tgt_ref() -> TableRef:
    return TableRef(schema="TGT", table="TGT_TABLE")


def _resolved(ref: TableRef, col: str) -> ResolvedColumn:
    return ResolvedColumn(
        expr_sql=f"{ref.fq}.{col}",
        provenance=Provenance.ODI,
        ref=ref,
        column=col,
    )


def _simple_model(
    *,
    src_ref: TableRef | None = None,
    tgt_ref: TableRef | None = None,
    mappings: list[ColumnMapping] | None = None,
) -> ODIModel:
    """Build a minimal 1-step model for test scenarios."""
    if src_ref is None:
        src_ref = _src_ref()
    if tgt_ref is None:
        tgt_ref = _tgt_ref()
    if mappings is None:
        mappings = [
            ColumnMapping(
                target_col="ID",
                source=_resolved(src_ref, "ID"),
                is_nullable=False,
                is_pk=True,
            ),
            ColumnMapping(
                target_col="NAME",
                source=_resolved(src_ref, "NAME"),
                is_nullable=True,
            ),
        ]
    step = StagingStep(
        step_id=1,
        name="STEP1",
        column_mappings=mappings,
        source_bindings=[AliasBinding(alias="S", ref=src_ref)],
    )
    return ODIModel(
        target=tgt_ref,
        staging_steps=[step],
        final_insert_columns=["ID", "NAME"],
    )


# ---------------------------------------------------------------------------
# KBLookup unit tests
# ---------------------------------------------------------------------------

class TestKBLookup:
    def test_qualified_table_exists(self):
        kb = _make_kb()
        assert kb.table_exists(TableRef("SRC", "SRC_TABLE")) is True

    def test_qualified_table_missing(self):
        kb = _make_kb()
        assert kb.table_exists(TableRef("SRC", "MISSING_TABLE")) is False

    def test_unqualified_table_exists_via_fallback(self):
        kb = _make_kb()
        # No schema given — should resolve via _table_index
        assert kb.table_exists(TableRef("", "TGT_TABLE")) is True

    def test_unqualified_table_missing(self):
        kb = _make_kb()
        assert kb.table_exists(TableRef("", "DOES_NOT_EXIST")) is False

    def test_wrong_schema_falls_back_to_unqualified(self):
        # KBLookup intentionally falls back to the _table_index when the
        # qualified key (TGT.SRC_TABLE) is not found, so it still resolves.
        # This is by design: ODI XML often omits or mis-qualifies schema names.
        kb = _make_kb()
        assert kb.table_exists(TableRef("TGT", "SRC_TABLE")) is True

    def test_column_exists_qualified(self):
        kb = _make_kb()
        ref = TableRef("SRC", "SRC_TABLE")
        assert kb.column_exists(ref, "ID") is True
        assert kb.column_exists(ref, "NAME") is True
        assert kb.column_exists(ref, "GHOST") is False

    def test_column_exists_case_insensitive(self):
        kb = _make_kb()
        ref = TableRef("SRC", "SRC_TABLE")
        assert kb.column_exists(ref, "id") is True
        assert kb.column_exists(ref, "name") is True

    def test_column_nullable_true(self):
        kb = _make_kb()
        ref = TableRef("SRC", "SRC_TABLE")
        assert kb.column_nullable(ref, "NAME") is True

    def test_column_nullable_false(self):
        kb = _make_kb()
        ref = TableRef("SRC", "SRC_TABLE")
        assert kb.column_nullable(ref, "ID") is False

    def test_column_nullable_permissive_on_unknown_col(self):
        """Return True (permissive) when column is not in KB — avoid false warnings."""
        kb = _make_kb()
        ref = TableRef("SRC", "SRC_TABLE")
        assert kb.column_nullable(ref, "PHANTOM") is True

    def test_column_nullable_permissive_on_unknown_table(self):
        kb = _make_kb()
        assert kb.column_nullable(TableRef("SRC", "MISSING"), "ID") is True

    def test_get_columns_qualified(self):
        kb = _make_kb()
        cols = kb.get_columns(TableRef("SRC", "SRC_TABLE"))
        assert cols is not None
        assert "ID" in cols
        assert "NAME" in cols

    def test_get_columns_missing_returns_none(self):
        kb = _make_kb()
        assert kb.get_columns(TableRef("SRC", "MISSING")) is None


# ---------------------------------------------------------------------------
# validate_model_offline scenarios
# ---------------------------------------------------------------------------

class TestValidateModelOffline:

    def test_static_pass_all_present(self):
        kb = _make_kb()
        model = _simple_model()
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.STATIC_PASS
        assert result.errors == []
        assert not result.is_blocking
        # target + source tables must appear in checked_tables
        assert "TGT.TGT_TABLE" in result.checked_tables
        assert "SRC.SRC_TABLE" in result.checked_tables

    def test_pdm_miss_source_table(self):
        kb = _make_kb()
        ghost_ref = TableRef("SRC", "GHOST_TABLE")
        model = _simple_model(src_ref=ghost_ref)
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.PDM_MISS
        assert result.is_blocking
        pdm_errors = [e for e in result.errors if e.code == "PDM_MISS"]
        assert len(pdm_errors) >= 1
        assert "GHOST_TABLE" in pdm_errors[0].detail

    def test_pdm_miss_target_table(self):
        kb = _make_kb()
        ghost_tgt = TableRef("TGT", "GHOST_TARGET")
        model = _simple_model(tgt_ref=ghost_tgt)
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.PDM_MISS
        assert result.is_blocking
        tgt_errors = [e for e in result.errors if e.code == "PDM_MISS" and "GHOST_TARGET" in e.detail]
        assert len(tgt_errors) == 1

    def test_column_not_in_kb(self):
        kb = _make_kb()
        src = _src_ref()
        mappings = [
            ColumnMapping(
                target_col="ID",
                source=_resolved(src, "DOES_NOT_EXIST"),
                is_nullable=True,
            )
        ]
        model = _simple_model(mappings=mappings)
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.COLUMN_NOT_IN_KB
        assert result.is_blocking
        col_errors = [e for e in result.errors if e.code == "COLUMN_NOT_IN_KB"]
        assert len(col_errors) == 1
        assert "DOES_NOT_EXIST" in col_errors[0].detail

    def test_unresolved_expr_is_non_blocking_warning(self):
        kb = _make_kb()
        mappings = [
            ColumnMapping(
                target_col="NAME",
                source=UnresolvedExpr(
                    original_expr=":GLOBAL.GV_SOME_VAR",
                    reason="ALIAS_NOT_IN_JOIN_GRAPH",
                    detail="bind variable not resolvable offline",
                ),
                is_nullable=True,
            )
        ]
        model = _simple_model(mappings=mappings)
        result = validate_model_offline(model, kb)
        # Unresolved → STATIC_PARTIAL (warnings, no hard blockers from this)
        assert result.verdict in (StaticVerdict.STATIC_PARTIAL, StaticVerdict.STATIC_PASS)
        unresolved_errors = [e for e in result.errors if e.code == "UNRESOLVED_EXPR"]
        assert len(unresolved_errors) == 1
        assert unresolved_errors[0].is_blocking is False
        assert not result.is_blocking

    def test_null_violation_risk_is_non_blocking(self):
        """Source col is nullable; target col is NOT NULL → NULL_VIOLATION_RISK warning."""
        kb = _make_kb()
        src = _src_ref()
        tgt = _tgt_ref()
        # VAL is nullable in SRC; REQUIRED is NOT NULL in TGT
        mappings = [
            ColumnMapping(
                target_col="REQUIRED",
                source=_resolved(src, "VAL"),
                is_nullable=True,   # source side may produce NULL (e.g., outer join)
                is_pk=False,
            )
        ]
        model = _simple_model(tgt_ref=tgt, mappings=mappings)
        result = validate_model_offline(model, kb)
        null_errors = [e for e in result.errors if e.code == "NULL_VIOLATION_RISK"]
        assert len(null_errors) == 1
        assert null_errors[0].is_blocking is False
        assert result.verdict in (StaticVerdict.STATIC_PARTIAL, StaticVerdict.STATIC_PASS)

    def test_null_violation_risk_skipped_when_source_not_nullable(self):
        """If mapping.is_nullable=False, no NULL risk even if target is NOT NULL."""
        kb = _make_kb()
        src = _src_ref()
        tgt = _tgt_ref()
        mappings = [
            ColumnMapping(
                target_col="REQUIRED",
                source=_resolved(src, "VAL"),
                is_nullable=False,  # mapping guarantees NOT NULL
            )
        ]
        model = _simple_model(tgt_ref=tgt, mappings=mappings)
        result = validate_model_offline(model, kb)
        null_errors = [e for e in result.errors if e.code == "NULL_VIOLATION_RISK"]
        assert null_errors == []

    def test_column_check_skipped_when_table_is_pdm_miss(self):
        """Column-level checks should not run if the parent table is a PDM_MISS."""
        kb = _make_kb()
        ghost = TableRef("SRC", "GHOST_SRC")
        mappings = [
            ColumnMapping(
                target_col="ID",
                source=_resolved(ghost, "ID"),
                is_nullable=False,
            )
        ]
        model = _simple_model(src_ref=ghost, mappings=mappings)
        result = validate_model_offline(model, kb)
        # PDM_MISS for the table should dominate; no spurious COLUMN_NOT_IN_KB
        pdm = [e for e in result.errors if e.code == "PDM_MISS"]
        col_miss = [e for e in result.errors if e.code == "COLUMN_NOT_IN_KB"]
        assert len(pdm) >= 1
        assert col_miss == []

    def test_to_dict_shape(self):
        kb = _make_kb()
        model = _simple_model()
        d = validate_model_offline(model, kb).to_dict()
        assert "verdict" in d
        assert "is_blocking" in d
        assert "error_count" in d
        assert "errors" in d
        assert "checked_tables" in d
        assert isinstance(d["errors"], list)
        assert isinstance(d["checked_tables"], list)

    def test_verdict_pdm_miss_beats_column_not_in_kb(self):
        """If both target PDM_MISS and COLUMN_NOT_IN_KB exist, verdict = PDM_MISS."""
        kb = _make_kb()
        ghost_tgt = TableRef("TGT", "NO_SUCH_TARGET")
        src = _src_ref()
        mappings = [
            ColumnMapping(
                target_col="ID",
                source=_resolved(src, "GHOST_COL"),
                is_nullable=True,
            )
        ]
        model = _simple_model(tgt_ref=ghost_tgt, mappings=mappings)
        result = validate_model_offline(model, kb)
        # PDM_MISS must take precedence in the verdict
        assert result.verdict == StaticVerdict.PDM_MISS

    def test_empty_staging_steps_target_not_in_kb(self):
        """Model with no steps but unknown target should still produce PDM_MISS."""
        kb = _make_kb()
        model = ODIModel(
            target=TableRef("TGT", "GHOST_EMPTY"),
            staging_steps=[],
        )
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.PDM_MISS

    def test_empty_staging_steps_target_in_kb(self):
        """Model with no steps and valid target should produce STATIC_PASS."""
        kb = _make_kb()
        model = ODIModel(
            target=TableRef("TGT", "TGT_TABLE"),
            staging_steps=[],
        )
        result = validate_model_offline(model, kb)
        assert result.verdict == StaticVerdict.STATIC_PASS
        assert result.errors == []
