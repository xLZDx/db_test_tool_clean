"""Unit tests for P6 Oracle XE harness (SyntheticDataGenerator + run_insert_on_xe).

All tests are offline — Oracle connection is mocked via monkeypatching.
The XE_UNAVAILABLE path (no oracledb / connect failure) is always exercised.
CONFIRMED / FAIL_ZERO_ROWS / ORA_ERROR paths use mock cursors.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.sql_model.types import AliasBinding, ODIModel, StagingStep, TableRef
from app.db.xe_harness import (
    SyntheticDataGenerator,
    XeRunResult,
    XeVerdict,
    run_insert_on_xe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_KB = {
    "pdm": {
        "schemas": [
            {
                "schema": "SRC",
                "tables": [
                    {
                        "name": "SRC_TBL",
                        "columns": [
                            {"name": "ID",     "data_type": "NUMBER",    "nullable": False},
                            {"name": "LABEL",  "data_type": "VARCHAR2",  "nullable": True},
                            {"name": "DT",     "data_type": "DATE",      "nullable": True},
                            {"name": "TS",     "data_type": "TIMESTAMP", "nullable": True},
                            {"name": "CODE",   "data_type": "CHAR",      "nullable": True},
                        ],
                    }
                ],
            },
            {
                "schema": "TGT",
                "tables": [
                    {
                        "name": "TGT_TBL",
                        "columns": [
                            {"name": "ID",    "data_type": "NUMBER",   "nullable": False},
                            {"name": "LABEL", "data_type": "VARCHAR2", "nullable": True},
                        ],
                    }
                ],
            },
        ]
    }
}


def _write_kb(data: dict) -> pathlib.Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
    json.dump(data, tmp)
    tmp.flush()
    tmp.close()
    return pathlib.Path(tmp.name)


def _kb_path() -> pathlib.Path:
    return _write_kb(_MINIMAL_KB)


def _simple_model(src_schema="SRC", src_table="SRC_TBL",
                  tgt_schema="TGT", tgt_table="TGT_TBL") -> ODIModel:
    src_ref = TableRef(schema=src_schema, table=src_table)
    tgt_ref = TableRef(schema=tgt_schema, table=tgt_table)
    step = StagingStep(
        step_id=1,
        name="STEP1",
        source_bindings=[AliasBinding(alias="S", ref=src_ref)],
    )
    return ODIModel(
        target=tgt_ref,
        staging_steps=[step],
        final_insert_columns=["ID", "LABEL"],
    )


# ---------------------------------------------------------------------------
# SyntheticDataGenerator tests
# ---------------------------------------------------------------------------

class TestSyntheticDataGenerator:

    def test_get_columns_qualified(self):
        gen = SyntheticDataGenerator(_kb_path())
        cols = gen.get_columns(TableRef("SRC", "SRC_TBL"))
        assert len(cols) == 5
        names = [c["name"] for c in cols]
        assert "ID" in names
        assert "LABEL" in names

    def test_get_columns_unqualified_fallback(self):
        gen = SyntheticDataGenerator(_kb_path())
        cols = gen.get_columns(TableRef("", "TGT_TBL"))
        assert len(cols) == 2

    def test_get_columns_missing_returns_empty(self):
        gen = SyntheticDataGenerator(_kb_path())
        cols = gen.get_columns(TableRef("SRC", "GHOST"))
        assert cols == []

    # _fake_value type coverage
    def test_fake_value_number(self):
        v = SyntheticDataGenerator._fake_value("NUMBER", "ID", 0)
        assert v == "1"
        v2 = SyntheticDataGenerator._fake_value("NUMBER", "ID", 4)
        assert v2 == "5"

    def test_fake_value_integer(self):
        v = SyntheticDataGenerator._fake_value("INTEGER", "X", 2)
        assert v == "3"

    def test_fake_value_date(self):
        v = SyntheticDataGenerator._fake_value("DATE", "DT", 0)
        assert v.startswith("DATE '")
        assert "'" in v

    def test_fake_value_timestamp(self):
        v = SyntheticDataGenerator._fake_value("TIMESTAMP", "TS", 1)
        assert v.startswith("TIMESTAMP '")
        assert "00:00:00" in v

    def test_fake_value_char(self):
        v = SyntheticDataGenerator._fake_value("CHAR", "CODE", 5)
        assert v.startswith("'C")
        assert v.endswith("'")

    def test_fake_value_varchar2(self):
        v = SyntheticDataGenerator._fake_value("VARCHAR2", "LABEL", 0)
        assert v.startswith("'T_LABEL")
        assert v.endswith("'")

    def test_fake_value_unknown_type_treated_as_varchar2(self):
        v = SyntheticDataGenerator._fake_value("BLOB", "BIN", 0)
        # falls through to VARCHAR2 path
        assert v.startswith("'")

    # DDL generation
    def test_create_table_sql_qualified(self):
        gen = SyntheticDataGenerator(_kb_path())
        ref = TableRef("SRC", "SRC_TBL")
        ddl = gen.create_table_sql(ref)
        assert ddl.startswith("CREATE TABLE SRC.SRC_TBL")
        assert "ID NUMBER" in ddl
        assert "LABEL VARCHAR2(1000)" in ddl
        assert "DT DATE" in ddl
        assert "TS TIMESTAMP" in ddl

    def test_create_table_sql_with_scratch_schema(self):
        gen = SyntheticDataGenerator(_kb_path())
        ref = TableRef("SRC", "SRC_TBL")
        ddl = gen.create_table_sql(ref, scratch_schema="SCRATCH")
        assert "SCRATCH.SRC_TBL" in ddl

    def test_create_table_sql_missing_table_returns_empty(self):
        gen = SyntheticDataGenerator(_kb_path())
        ddl = gen.create_table_sql(TableRef("SRC", "NO_SUCH"))
        assert ddl == ""

    # INSERT generation
    def test_insert_rows_sql_returns_n_rows(self):
        gen = SyntheticDataGenerator(_kb_path())
        ref = TableRef("SRC", "SRC_TBL")
        stmts = gen.insert_rows_sql(ref, n=3)
        assert len(stmts) == 3
        for stmt in stmts:
            assert stmt.startswith("INSERT INTO SRC.SRC_TBL")

    def test_insert_rows_sql_missing_table_returns_empty(self):
        gen = SyntheticDataGenerator(_kb_path())
        stmts = gen.insert_rows_sql(TableRef("SRC", "GHOST"), n=5)
        assert stmts == []

    def test_insert_rows_sql_contains_values(self):
        gen = SyntheticDataGenerator(_kb_path())
        ref = TableRef("SRC", "SRC_TBL")
        stmts = gen.insert_rows_sql(ref, n=1)
        assert "VALUES" in stmts[0]

    # _oracle_ddl_type mapping
    def test_oracle_ddl_type_number(self):
        assert SyntheticDataGenerator._oracle_ddl_type("NUMBER") == "NUMBER"
        assert SyntheticDataGenerator._oracle_ddl_type("INTEGER") == "NUMBER"
        assert SyntheticDataGenerator._oracle_ddl_type("FLOAT") == "NUMBER"

    def test_oracle_ddl_type_date(self):
        assert SyntheticDataGenerator._oracle_ddl_type("DATE") == "DATE"

    def test_oracle_ddl_type_timestamp(self):
        assert SyntheticDataGenerator._oracle_ddl_type("TIMESTAMP") == "TIMESTAMP"

    def test_oracle_ddl_type_clob(self):
        assert SyntheticDataGenerator._oracle_ddl_type("CLOB") == "CLOB"

    def test_oracle_ddl_type_varchar2(self):
        assert SyntheticDataGenerator._oracle_ddl_type("VARCHAR2") == "VARCHAR2(1000)"
        assert SyntheticDataGenerator._oracle_ddl_type("NVARCHAR2") == "VARCHAR2(1000)"
        assert SyntheticDataGenerator._oracle_ddl_type("CHAR") == "VARCHAR2(1000)"


# ---------------------------------------------------------------------------
# run_insert_on_xe — XE_UNAVAILABLE paths
# ---------------------------------------------------------------------------

class TestRunInsertXeUnavailable:

    def test_unavailable_when_oracledb_not_installed(self):
        """ImportError from oracledb -> XE_UNAVAILABLE, never is_pass."""
        kb = _kb_path()
        model = _simple_model()

        with patch.dict("sys.modules", {"oracledb": None}):
            result = run_insert_on_xe(model, "INSERT INTO TGT.TGT_TBL SELECT 1 FROM DUAL", kb)

        assert result.xe_status == "unavailable"
        assert result.verdict == XeVerdict.XE_UNAVAILABLE
        assert result.to_dict()["is_pass"] is False

    def test_unavailable_when_connect_raises(self):
        """Connection failure -> XE_UNAVAILABLE, never is_pass."""
        kb = _kb_path()
        model = _simple_model()

        mock_oracledb = MagicMock()
        mock_oracledb.connect.side_effect = Exception("ORA-12541: no listener")

        with patch.dict("sys.modules", {"oracledb": mock_oracledb}):
            result = run_insert_on_xe(model, "INSERT INTO TGT.TGT_TBL SELECT 1 FROM DUAL", kb)

        assert result.xe_status == "unavailable"
        assert result.verdict == XeVerdict.XE_UNAVAILABLE
        assert result.to_dict()["is_pass"] is False
        assert "ORA-12541" in result.note

    def test_xe_run_result_to_dict_unavailable(self):
        r = XeRunResult(xe_status="unavailable", verdict=XeVerdict.XE_UNAVAILABLE, note="no listener")
        d = r.to_dict()
        assert d["xe_status"] == "unavailable"
        assert d["verdict"] == "xe_unavailable"
        assert d["is_pass"] is False
        assert d["rows_affected"] == 0


# ---------------------------------------------------------------------------
# run_insert_on_xe — connected paths (mocked cursor)
# ---------------------------------------------------------------------------

def _make_mock_conn(rowcount: int = 3, main_raises: Exception | None = None):
    """Return a mocked oracledb connection."""
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda self: self
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.rowcount = rowcount

    if main_raises is not None:
        # Make execute raise on the INSERT (last call), not on CREATE/INSERTs
        call_count = [0]
        original_execute = mock_cur.execute

        def side_effect(sql, *args, **kwargs):
            call_count[0] += 1
            if "INSERT INTO TGT" in sql or "INSERT INTO SRC" in sql.upper() and call_count[0] > 5:
                pass  # allow synthetic inserts
            # main INSERT is the emitted SQL we pass in
            if sql.startswith("INSERT") and "DUAL" in sql:
                raise main_raises
            return original_execute(sql, *args, **kwargs)

        mock_cur.execute = side_effect

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.rollback = MagicMock()
    mock_conn.close = MagicMock()
    return mock_conn


class TestRunInsertXeConnected:

    def _run(self, rowcount: int, emit_sql: str = "INSERT INTO TGT.TGT_TBL SELECT 1 FROM DUAL"):
        kb = _kb_path()
        model = _simple_model()

        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda self: self
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.rowcount = rowcount

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.rollback = MagicMock()
        mock_conn.close = MagicMock()

        mock_oracledb = MagicMock()
        mock_oracledb.connect.return_value = mock_conn

        with patch.dict("sys.modules", {"oracledb": mock_oracledb}):
            result = run_insert_on_xe(model, emit_sql, kb)

        return result, mock_conn

    def test_confirmed_when_rows_affected_gt_0(self):
        result, mock_conn = self._run(rowcount=5)
        assert result.xe_status == "confirmed"
        assert result.verdict == XeVerdict.CONFIRMED
        assert result.rows_affected == 5
        assert result.to_dict()["is_pass"] is True
        # Always ROLLBACK at the end
        mock_conn.rollback.assert_called_once()

    def test_fail_zero_rows_when_rowcount_is_0(self):
        result, mock_conn = self._run(rowcount=0)
        assert result.xe_status == "confirmed"
        assert result.verdict == XeVerdict.FAIL_ZERO_ROWS
        assert result.rows_affected == 0
        assert result.to_dict()["is_pass"] is False
        mock_conn.rollback.assert_called_once()

    def test_rollback_always_called(self):
        """ROLLBACK must be called even when rows > 0."""
        result, mock_conn = self._run(rowcount=1)
        mock_conn.rollback.assert_called_once()
        mock_conn.close.assert_called_once()

    def test_xe_status_confirmed_on_connection_success(self):
        """xe_status='confirmed' means we connected, regardless of verdict."""
        result, _ = self._run(rowcount=0)
        assert result.xe_status == "confirmed"

    def test_ora_error_on_main_insert_exception(self):
        kb = _kb_path()
        model = _simple_model()

        mock_cur = MagicMock()
        mock_cur.__enter__ = lambda self: self
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.rowcount = 0
        main_sql = "INSERT INTO TGT.TGT_TBL SELECT 1 FROM DUAL"

        call_args = []

        def execute_side_effect(sql, *a, **kw):
            call_args.append(sql)
            if sql == main_sql:
                raise Exception("ORA-00942: table or view does not exist")

        mock_cur.execute = execute_side_effect
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.rollback = MagicMock()
        mock_conn.close = MagicMock()

        mock_oracledb = MagicMock()
        mock_oracledb.connect.return_value = mock_conn

        with patch.dict("sys.modules", {"oracledb": mock_oracledb}):
            result = run_insert_on_xe(model, main_sql, kb)

        assert result.xe_status == "confirmed"
        assert result.verdict == XeVerdict.ORA_ERROR
        assert result.to_dict()["is_pass"] is False
        ora_errs = [e for e in result.ora_errors if "MAIN INSERT" in e]
        assert len(ora_errs) == 1
        assert "ORA-00942" in ora_errs[0]
        mock_conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# XeRunResult invariants
# ---------------------------------------------------------------------------

class TestXeRunResultInvariants:

    def test_only_confirmed_verdict_is_pass(self):
        """is_pass must be True ONLY for CONFIRMED — all other verdicts must be False."""
        for verdict in XeVerdict:
            is_pass_val = XeRunResult(
                xe_status="confirmed" if verdict != XeVerdict.XE_UNAVAILABLE else "unavailable",
                verdict=verdict,
            ).to_dict()["is_pass"]
            if verdict == XeVerdict.CONFIRMED:
                assert is_pass_val is True, f"Expected is_pass=True for {verdict}"
            else:
                assert is_pass_val is False, f"Expected is_pass=False for {verdict}"

    def test_xe_unavailable_is_never_pass(self):
        r = XeRunResult(xe_status="unavailable", verdict=XeVerdict.XE_UNAVAILABLE)
        assert r.to_dict()["is_pass"] is False

    def test_fail_zero_rows_is_never_pass(self):
        r = XeRunResult(xe_status="confirmed", verdict=XeVerdict.FAIL_ZERO_ROWS, rows_affected=0)
        assert r.to_dict()["is_pass"] is False

    def test_ora_error_is_never_pass(self):
        r = XeRunResult(xe_status="confirmed", verdict=XeVerdict.ORA_ERROR, rows_affected=0)
        assert r.to_dict()["is_pass"] is False

    def test_to_dict_all_fields_present(self):
        r = XeRunResult(
            xe_status="confirmed",
            verdict=XeVerdict.CONFIRMED,
            rows_affected=7,
            ora_errors=["ORA-00955: ok"],
            synthetic_tables_created=["SRC.SRC_TBL"],
            note="test note",
        )
        d = r.to_dict()
        assert d["xe_status"] == "confirmed"
        assert d["verdict"] == "confirmed"
        assert d["rows_affected"] == 7
        assert d["ora_errors"] == ["ORA-00955: ok"]
        assert d["synthetic_tables_created"] == ["SRC.SRC_TBL"]
        assert d["note"] == "test note"
        assert d["is_pass"] is True
