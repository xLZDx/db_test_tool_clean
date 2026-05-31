from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.datasources import (
    DataSourceCreate,
    QueryInput,
    _enforce_datasource_privilege_policy,
    _enforce_query_statement_allowed,
    _redact_extra_params,
    _split_sql_statements_with_lines,
)
from app.security import _verify_api_key_value
from app.services import oracle_live_runner
from app.services.oracle_live_runner import execute_sql
from app.services.schema_kb_service import (
    SchemaMetadataQueryError,
    _oracle_has_dba_access,
    _query_or_empty,
)
from app.sql_model.oracle_validator import validate_oracle_sql
from app.connectors.oracle_connector import OracleConnector


def test_high_risk_api_key_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("DBTOOL_API_KEY", raising=False)
    monkeypatch.setattr("app.security.settings.DBTOOL_API_KEY", "")

    with pytest.raises(HTTPException) as exc:
        _verify_api_key_value("anything")

    assert exc.value.status_code == 503
    assert "DBTOOL_API_KEY" in exc.value.detail


def test_high_risk_api_key_rejects_wrong_value(monkeypatch):
    monkeypatch.setenv("DBTOOL_API_KEY", "secret")

    with pytest.raises(HTTPException) as exc:
        _verify_api_key_value("wrong")

    assert exc.value.status_code == 401


def test_high_risk_api_key_accepts_exact_value(monkeypatch):
    monkeypatch.setenv("DBTOOL_API_KEY", "secret")

    _verify_api_key_value("secret")


def test_datasource_query_blocks_dml_by_default():
    with pytest.raises(HTTPException) as exc:
        _enforce_query_statement_allowed("DELETE", False, QueryInput(sql="delete from t"))

    assert exc.value.status_code == 403


def test_datasource_query_allows_dml_only_with_explicit_flag():
    _enforce_query_statement_allowed(
        "DELETE", False, QueryInput(sql="delete from t", allow_writes=True)
    )


def test_datasource_query_blocks_ddl_by_default():
    with pytest.raises(HTTPException) as exc:
        _enforce_query_statement_allowed("DROP", False, QueryInput(sql="drop table t"))

    assert exc.value.status_code == 403


def test_live_oracle_runner_has_no_default_sysdba_credentials(monkeypatch):
    monkeypatch.setattr(oracle_live_runner, "oracledb", object())
    monkeypatch.delenv("ORA_LIVE_USER", raising=False)
    monkeypatch.delenv("ORA_LIVE_PASSWORD", raising=False)
    monkeypatch.delenv("ORA_LIVE_MODE", raising=False)

    result = execute_sql("select 1 from dual")

    assert result.success is False
    assert "required" in result.note
    assert "SYSDBA" in result.note


def test_oracle_has_dba_access_false_for_empty_result():
    class Connector:
        def execute_query(self, sql, params):
            return []

    assert _oracle_has_dba_access(Connector()) is False


def test_required_metadata_query_raises_instead_of_empty():
    class Connector:
        def execute_query(self, sql, params):
            raise RuntimeError("ORA-01031")

    with pytest.raises(SchemaMetadataQueryError):
        _query_or_empty(Connector(), "select * from dba_tab_columns", required=True, label="columns")


def test_optional_metadata_query_still_degrades_to_empty(caplog):
    class Connector:
        def execute_query(self, sql, params):
            raise RuntimeError("optional unavailable")

    assert _query_or_empty(Connector(), "select * from all_indexes") == []
    assert "optional unavailable" in caplog.text


def test_oracle_validator_rejects_unvalidated_plsql_block():
    result = validate_oracle_sql("BEGIN NULL; END;")

    assert result.is_valid is False
    assert result.static_errors
    assert "PL/SQL block" in result.static_errors[0].error_message


def test_oracle_connector_rejects_unsafe_quoted_identifier():
    connector = OracleConnector("host", 1521, "db", "u", "p")

    with pytest.raises(ValueError):
        connector._quote_identifier('SCHEMA"; DROP TABLE X; --')


def test_datasource_privilege_policy_blocks_oracle_sys_user():
    body = DataSourceCreate(
        name="sys",
        db_type="oracle",
        host="localhost",
        username="SYS",
        password="secret",
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_datasource_privilege_policy(body)

    assert exc.value.status_code == 403
    assert "SYS" in exc.value.detail


def test_datasource_privilege_policy_blocks_oracle_sysdba_mode():
    body = DataSourceCreate(
        name="privileged",
        db_type="oracle",
        host="localhost",
        username="app_user",
        password="secret",
        extra_params='{"mode":"SYSDBA"}',
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_datasource_privilege_policy(body)

    assert exc.value.status_code == 403
    assert "SYSDBA" in exc.value.detail


def test_datasource_extra_params_redacts_sensitive_values():
    raw = '{"session_minutes":30,"wallet_password":"secret","api_key":"abc"}'

    redacted = _redact_extra_params(raw)

    assert '"session_minutes": 30' in redacted
    assert '"wallet_password": "***"' in redacted
    assert '"api_key": "***"' in redacted
    assert "secret" not in redacted
    assert "abc" not in redacted


def test_datasource_extra_params_redacts_non_json_payload():
    assert _redact_extra_params("wallet_password=secret") == "<redacted>"


def test_oracle_connector_splitter_keeps_q_literal_semicolon_inside_statement():
    connector = OracleConnector("host", 1521, "db", "u", "p")

    statements = connector._split_sql_statements("select q'[a;b]' as x from dual; select 2 from dual;")

    assert statements == ["select q'[a;b]' as x from dual", "select 2 from dual"]


def test_datasource_splitter_keeps_q_literal_semicolon_inside_statement():
    statements = _split_sql_statements_with_lines("select q'!a;b!' as x from dual;\nselect 2 from dual;")

    assert [item["sql"] for item in statements] == ["select q'!a;b!' as x from dual", "select 2 from dual"]
    assert statements[1]["start_line"] == 2
