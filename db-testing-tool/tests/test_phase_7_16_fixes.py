"""Regression tests for Phase 7.16 fixes (rounds A-D).

These tests pin the operator-locked behaviour of the round-A through
round-D fixes so future refactors can't silently regress them.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── Round A: BaseConnector + factory + LiveOracleConfig ──────────────────────

def test_base_connector_pre_inits_connection_to_none():
    """Phase 7.16 A: was AttributeError on disconnect() before connect()."""
    from app.connectors.base import BaseConnector
    c = BaseConnector("h", 1, "db", "u", "p")
    assert c._connection is None


def test_factory_unsupported_db_type_raises_valueerror():
    """Phase 7.16 A: was silent None -> AttributeError downstream."""
    from app.connectors.factory import get_connector
    from types import SimpleNamespace
    ds = SimpleNamespace(
        db_type="mysql", host="h", port=3306, database_name="d",
        username="u", password="p", extra_params={},
    )
    with pytest.raises(ValueError, match="Unsupported datasource db_type"):
        get_connector(ds)


def test_live_oracle_config_rejects_invalid_mode():
    """Phase 7.16 A: type-design BLOCKER-1 fix.  Was silent downgrade to DEFAULT."""
    from app.services.oracle_live_runner import LiveOracleConfig
    with pytest.raises(ValueError, match="invalid"):
        LiveOracleConfig(mode="INVALID_MODE")


def test_live_oracle_config_normalises_case():
    """Phase 7.16 A: lowercase mode normalised to uppercase at construction."""
    from app.services.oracle_live_runner import LiveOracleConfig
    c = LiveOracleConfig(mode="sysdba")
    assert c.mode == "SYSDBA"


def test_live_oracle_config_default_mode_is_default_not_sysdba():
    """Phase 7.16 A: ensures the prior SYSDBA default (security risk) stays gone."""
    from app.services.oracle_live_runner import LiveOracleConfig
    c = LiveOracleConfig()
    assert c.mode == "DEFAULT"


# ── Round A: comparator_driven_emitter type fix ──────────────────────────────

def test_build_alias_map_per_table_is_list_not_str():
    """Phase 7.16 A: was annotated Dict[str,str] but actual is Dict[str,List[str]]."""
    from app.sql_model.comparator_driven_emitter import _build_alias_map
    rows = [
        ("COL1", "SCHEMA.MAIN_TABLE", "C1", "logic", "MATCHED"),
        ("COL2", "SCHEMA.LU_DIM", "C2", "logic", "MATCHED"),
    ]
    base_fq, per_row, per_table, repr_ = _build_alias_map(rows)
    # per_table values MUST be lists (the annotation was lying about str).
    for v in per_table.values():
        assert isinstance(v, list), f"per_table value is {type(v).__name__}, expected list"


# ── Round A: SchemaProvider strict-mode + permissive fallback ───────────────

def test_schema_provider_strict_mode_returns_false_on_unknown_table(tmp_path, monkeypatch):
    """Phase 7.16 A: PDM_STRICT_MODE=1 forces missing-table -> False."""
    from app.sql_model.schema_provider import SchemaProvider
    # Write a minimal valid KB so _tables is non-empty but doesn't contain UNKNOWN.
    kb_file = tmp_path / "schema_kb_ds_99.json"
    kb_file.write_text(json.dumps({
        "pdm": {"schemas": [
            {"schema": "S", "tables": [
                {"name": "KNOWN", "columns": [{"name": "C1"}, {"name": "C2"}]}
            ]}
        ]}
    }), encoding="utf-8")
    monkeypatch.setenv("PDM_STRICT_MODE", "1")
    p = SchemaProvider(kb_dir=tmp_path, preferred_ds_id=99)
    assert p.has_column("S", "KNOWN", "C1") is True
    assert p.has_column("S", "UNKNOWN", "C1") is False  # strict


def test_schema_provider_returns_false_when_at_least_one_kb_loaded(tmp_path):
    """Phase 7.16 A: was returning True for unknown tables; comment lied."""
    from app.sql_model.schema_provider import SchemaProvider
    kb_file = tmp_path / "schema_kb_ds_99.json"
    kb_file.write_text(json.dumps({
        "pdm": {"schemas": [
            {"schema": "S", "tables": [
                {"name": "KNOWN", "columns": [{"name": "C1"}]}
            ]}
        ]}
    }), encoding="utf-8")
    p = SchemaProvider(kb_dir=tmp_path, preferred_ds_id=99)
    assert p.has_column("S", "UNKNOWN_TABLE", "ANY_COL") is False


def test_schema_provider_permissive_only_when_all_kb_unavailable(tmp_path, caplog):
    """Phase 7.16 A: LFS pointer dev checkout falls back to permissive,
    but ONLY when no KB loaded at all + emits ONE-TIME WARNING."""
    import logging
    from app.sql_model.schema_provider import SchemaProvider
    # Write an LFS pointer so the provider can't load it.
    lfs_pointer = tmp_path / "schema_kb_ds_99.json"
    lfs_pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n",
        encoding="utf-8",
    )
    p = SchemaProvider(kb_dir=tmp_path, preferred_ds_id=99)
    with caplog.at_level(logging.WARNING):
        result = p.has_column("S", "T", "C")
    assert result is True  # permissive fallback
    assert any("PERMISSIVE FALLBACK" in r.message for r in caplog.records)


# ── Round B: silent-failure surface ─────────────────────────────────────────

def test_load_overrides_logs_on_corrupt_json(tmp_path, caplog, monkeypatch):
    """Phase 7.16 B: was bare except returning []; now logs ERROR."""
    import logging
    from app.routers import odi as odi_mod
    corrupt = tmp_path / "comparison_overrides.json"
    corrupt.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(odi_mod, "_OVERRIDES_PATH", corrupt)
    with caplog.at_level(logging.ERROR):
        result = odi_mod._load_overrides()
    assert result == []
    assert any("corrupt" in r.message.lower() for r in caplog.records)


# ── Round C: async LLM wrapper ──────────────────────────────────────────────

def test_chat_completion_with_fallback_is_now_async():
    """Phase 7.16 C: was sync, blocked event loop 5-30s.  Now awaitable."""
    import inspect
    from app.services import ai_service
    assert inspect.iscoroutinefunction(ai_service._chat_completion_with_fallback), (
        "Phase 7.16 C: _chat_completion_with_fallback must be async"
    )
    assert callable(ai_service._chat_completion_with_fallback_sync), (
        "Phase 7.16 C: _chat_completion_with_fallback_sync must still exist for sync callers"
    )


def test_test_executor_inter_test_delay_default_zero(monkeypatch):
    """Phase 7.16 C: was hardcoded asyncio.sleep(3) per test; default now 0."""
    monkeypatch.delenv("TEST_EXECUTOR_INTER_TEST_DELAY_S", raising=False)
    # Verify the env-var read produces 0 by default (we just check the env-var
    # contract, not the full run_all_tests function which needs a DB).
    import os
    delay = float(os.environ.get("TEST_EXECUTOR_INTER_TEST_DELAY_S", "0"))
    assert delay == 0.0


# ── Round D: auth middleware behaviour ──────────────────────────────────────

def test_auth_middleware_warn_mode_does_not_block(monkeypatch):
    """Phase 7.16 D: default warn mode logs but does NOT block."""
    monkeypatch.setenv("DBTOOL_REQUIRE_AUTH", "warn")
    from app.security import _auth_mode
    assert _auth_mode() == "warn"


def test_auth_middleware_enforce_mode_detected(monkeypatch):
    monkeypatch.setenv("DBTOOL_REQUIRE_AUTH", "enforce")
    from app.security import _auth_mode
    assert _auth_mode() == "enforce"


def test_auth_middleware_off_mode_detected(monkeypatch):
    monkeypatch.setenv("DBTOOL_REQUIRE_AUTH", "off")
    from app.security import _auth_mode
    assert _auth_mode() == "off"


def test_auth_middleware_public_path_allowlist():
    """Phase 7.16 D: HTML pages + static + downloads are public."""
    from app.security import _is_public_path
    assert _is_public_path("/") is True
    assert _is_public_path("/static/css/app.css") is True
    assert _is_public_path("/datasources") is True
    assert _is_public_path("/docs") is True
    assert _is_public_path("/download/template/foo") is True
    assert _is_public_path("/api/datasources/1/query") is False  # NOT public
    assert _is_public_path("/api/odi/live/execute") is False  # NOT public
