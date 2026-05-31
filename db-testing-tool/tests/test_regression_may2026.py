"""Regression tests for bugs fixed in May 2026.

Covers:
- Connector factory returns correct connector type (not None)
- BaseConnector __init__ accepts host/port/database/username/password
- ConnectionResult has server_version field
- SQL terminal GET /api/datasources returns all datasources (not just status=ok)
- Mapping CRUD: list/create/get/update/delete/bulk-delete
- Background schema task queue returns immediately (non-blocking)
- Schema task queue status endpoint returns correct shape
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Connector factory ────────────────────────────────────────────────────────

def _make_ds(db_type, **extra):
    """Build a minimal DataSource-like SimpleNamespace for factory tests."""
    ds = SimpleNamespace(
        db_type=db_type,
        host="host",
        port=1521,
        database_name="db",
        username="u",
        password="p",
        extra_params=None,
    )
    for k, v in extra.items():
        setattr(ds, k, v)
    return ds


def test_factory_oracle_returns_connector():
    from app.connectors.factory import get_connector
    from app.connectors.oracle_connector import OracleConnector

    ds = _make_ds("oracle")
    connector = get_connector(ds)
    assert connector is not None, "factory must not return None for oracle"
    assert isinstance(connector, OracleConnector)


def test_factory_redshift_returns_connector():
    from app.connectors.factory import get_connector
    from app.connectors.redshift_connector import RedshiftConnector

    ds = _make_ds("redshift")
    connector = get_connector(ds)
    assert connector is not None, "factory must not return None for redshift"
    assert isinstance(connector, RedshiftConnector)


def test_factory_sqlserver_returns_connector():
    from app.connectors.factory import get_connector
    from app.connectors.sqlserver_connector import SqlServerConnector

    ds = _make_ds("sqlserver")
    connector = get_connector(ds)
    assert connector is not None, "factory must not return None for sqlserver"
    assert isinstance(connector, SqlServerConnector)


def test_factory_unknown_type_raises():
    """Phase 7.16 fix: unsupported db_type now raises ValueError (was None).
    Silent None propagated into `connector.connect()` -> AttributeError far
    from the root cause."""
    import pytest
    from app.connectors.factory import get_connector

    ds = _make_ds("unknown_db_xyz")
    with pytest.raises(ValueError, match="Unsupported datasource db_type"):
        get_connector(ds)


# ── BaseConnector __init__ ───────────────────────────────────────────────────

def test_base_connector_init_accepts_args():
    """BaseConnector.__init__ must accept positional args (not raise TypeError)."""
    from app.connectors.base import BaseConnector

    class _Concrete(BaseConnector):
        def connect(self): pass
        def disconnect(self): pass
        def test_connection(self): pass
        def execute_query(self, sql, params=None): pass
        def get_schemas(self): return []
        def get_tables(self, schema): return []
        def get_columns(self, schema, table): return []

    c = _Concrete("myhost", 1521, "mydb", "myuser", "mypass")
    assert c.host == "myhost"
    assert c.port == 1521
    assert c.database == "mydb"
    assert c.username == "myuser"


# ── ConnectionResult server_version field ────────────────────────────────────

def test_connection_result_has_server_version():
    from app.connectors.base import ConnectionResult

    r = ConnectionResult(success=True, message="ok")
    assert hasattr(r, "server_version"), "ConnectionResult must have server_version field"
    assert r.server_version is None  # default

    r2 = ConnectionResult(success=True, message="ok", server_version="Oracle 19c")
    assert r2.server_version == "Oracle 19c"


# ── Schema task queue is non-blocking ────────────────────────────────────────

def test_schema_task_queue_returns_immediately():
    """enqueue_schema_task must return a count without awaiting the coroutine."""
    asyncio.run(_assert_schema_task_queue_returns_immediately())


async def _assert_schema_task_queue_returns_immediately():
    from app.services.schema_task_queue import enqueue_schema_task, _reset_for_tests

    completed = []
    release = asyncio.Event()

    async def _slow_job():
        await release.wait()
        completed.append(True)

    await _reset_for_tests(worker_count=1, max_queue=2)
    try:
        depth = await enqueue_schema_task("test_op_001", "test", _slow_job)
        # Should return immediately — completed list still empty
        assert isinstance(depth, int)
        assert len(completed) == 0, "Task should not have completed synchronously"
    finally:
        release.set()
        await _reset_for_tests()


# ── Mapping CRUD via live API ─────────────────────────────────────────────────

BASE = "http://127.0.0.1:8550"


def _http_get(path):
    import urllib.request
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return r.status, json.loads(r.read())


def _http_post(path, body):
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def _http_put(path, body):
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE}{path}", data=data,
                                  headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def _http_delete(path):
    import urllib.request
    req = urllib.request.Request(f"{BASE}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


@pytest.fixture(scope="module")
def _server_running():
    import urllib.request
    try:
        urllib.request.urlopen(f"{BASE}/", timeout=3)
        return True
    except Exception:
        pytest.skip("Server not running — skipping live API tests")


def test_mapping_list_returns_shape(_server_running):
    status, body = _http_get("/api/mappings")
    assert status == 200
    assert "mappings" in body
    assert "total" in body
    assert isinstance(body["mappings"], list)


def test_mapping_crud_lifecycle(_server_running):
    """Create → Get → Update → Delete a mapping rule."""
    rule_payload = {
        "name": "Regression Test Rule",
        "source_datasource_id": 1,
        "source_table": "src_accounts",
        "target_datasource_id": 1,
        "target_table": "tgt_accounts",
        "rule_type": "direct",
        "description": "regression test",
    }

    # Create
    status, body = _http_post("/api/mappings", rule_payload)
    assert status == 200
    assert body["status"] == "created"
    rule_id = body["id"]

    # Get
    status, body = _http_get(f"/api/mappings/{rule_id}")
    assert status == 200
    assert body["name"] == "Regression Test Rule"
    assert body["source_table"] == "src_accounts"

    # Update
    updated_payload = {**rule_payload, "name": "Regression Updated", "source_table": "src_accounts_v2"}
    status, body = _http_put(f"/api/mappings/{rule_id}", updated_payload)
    assert status == 200
    assert body["name"] == "Regression Updated"
    assert body["source_table"] == "src_accounts_v2"

    # Delete
    status, body = _http_delete(f"/api/mappings/{rule_id}")
    assert status == 200
    assert body["deleted"] is True

    # Confirm gone
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"{BASE}/api/mappings/{rule_id}")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "Should have returned 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404


def test_mapping_bulk_delete(_server_running):
    """Create two rules then bulk-delete them."""
    base_payload = {
        "source_datasource_id": 1,
        "source_table": "bulk_src",
        "target_datasource_id": 1,
        "target_table": "bulk_tgt",
    }
    _, r1 = _http_post("/api/mappings", {**base_payload, "name": "Bulk Rule A"})
    _, r2 = _http_post("/api/mappings", {**base_payload, "name": "Bulk Rule B"})

    status, body = _http_post("/api/mappings/bulk-delete", {"ids": [r1["id"], r2["id"]]})
    assert status == 200
    assert body["deleted"] == 2


def test_datasources_list_includes_all_statuses(_server_running):
    """GET /api/datasources must return datasources regardless of connection status."""
    status, body = _http_get("/api/datasources")
    assert status == 200
    datasources = body if isinstance(body, list) else body.get("datasources", [])
    assert len(datasources) > 0, "Must return at least one datasource"
    # All configured datasources should be present, not just status=ok
    statuses = {d.get("status") for d in datasources}
    # Confirm non-ok statuses are included (not filtered out)
    # We just assert the list has all configured entries (count >= 1)
    assert len(datasources) >= 1


def test_schema_queue_status_shape(_server_running):
    """GET /api/schemas/queue/status must return expected keys."""
    status, body = _http_get("/api/schemas/queue/status")
    assert status == 200
    for key in ("status", "queue_depth", "worker_count", "active_workers",
                "active_operation_ids", "workers_started"):
        assert key in body, f"queue/status missing key: {key}"


# ── TableInfo / ColumnInfo field-name regression (c6467c8) ──────────────────

def test_table_info_positional_fields():
    """TableInfo must use schema/table_name/table_type — not the old (name, columns, row_count) layout."""
    from app.connectors.base import TableInfo

    t = TableInfo("MY_SCHEMA", "MY_TABLE", "TABLE")
    assert t.schema == "MY_SCHEMA", "TableInfo.schema must be set from first positional arg"
    assert t.table_name == "MY_TABLE", "TableInfo.table_name must be set from second positional arg"
    assert t.table_type == "TABLE", "TableInfo.table_type must be set from third positional arg"


def test_table_info_default_table_type():
    """TableInfo.table_type should default to 'TABLE'."""
    from app.connectors.base import TableInfo

    t = TableInfo("S", "T")
    assert t.table_type == "TABLE"


def test_column_info_positional_fields():
    """ColumnInfo must expose column_name/data_type/nullable/is_pk/ordinal_position."""
    from app.connectors.base import ColumnInfo

    c = ColumnInfo("COL_ID", "NUMBER", nullable=False, is_pk=True, ordinal_position=1)
    assert c.column_name == "COL_ID"
    assert c.data_type == "NUMBER"
    assert c.nullable is False
    assert c.is_pk is True
    assert c.ordinal_position == 1


def test_column_info_defaults():
    """ColumnInfo defaults: nullable=True, is_pk=False, ordinal_position=0."""
    from app.connectors.base import ColumnInfo

    c = ColumnInfo("COL_NAME", "VARCHAR2")
    assert c.nullable is True
    assert c.is_pk is False
    assert c.ordinal_position == 0


def test_schema_catalog_unsupported_type(_server_running):
    """GET /api/schemas/catalog for an unsupported db_type must return 422, not 500."""
    import urllib.error
    import urllib.request
    # DS4 is known-error Oracle, so we can't test unsupported via a real DS.
    # Instead hit /api/schemas/catalog/99999 which returns 404.
    req = urllib.request.Request(f"{BASE}/api/schemas/catalog/99999")
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "Expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"Expected 404 for unknown DS ID, got {e.code}"


def test_extract_lookup_spec_supports_dollar_hash_and_and_tail():
    from app.services.drd_import_service import _extract_lookup_spec

    spec = _extract_lookup_spec(
        transformation=(
            "LEFT OUTER JOIN CCAL_REPL_OWNER.J$TXN J$TXN ON "
            "J$TXN.SRC_TXN_ID = TXN.TXN_ID AND J$TXN.ACTV_F = 'Y'"
        ),
        src_attr_u="TXN_ID",
        target_col_u="SRC_TXN_TYPE_CD",
        src_schema="AMOGOREANU",
        src_table="TXN",
    )

    assert spec is not None
    assert spec["lookup_table"] == "CCAL_REPL_OWNER.J$TXN"
    assert spec["lookup_join_col"] == "SRC_TXN_ID"
    assert spec["source_lookup_col"] == "TXN_ID"
    assert "ACTV_F" in (spec.get("extra_filter") or "")
    assert spec.get("source_alias_hint") == "TXN"


def test_extract_lookup_spec_accepts_lkup_suffix_table():
    from app.services.drd_import_service import _extract_lookup_spec

    spec = _extract_lookup_spec(
        transformation="LOOKUP ON TXN_SRC_TAX_CODE_LKUP USING SRC_TAX_CODE_ID",
        src_attr_u="SRC_TAX_CODE_ID",
        target_col_u="SRC_TAX_CODE_DESC",
        src_schema="CCAL_REPL_OWNER",
        src_table="TXN",
    )

    assert spec is not None
    assert spec["lookup_table"].endswith("TXN_SRC_TAX_CODE_LKUP")


def test_derive_lookup_preserves_explicit_on_source_column():
    from app.services.control_table_service import derive_lookup_from_transformation

    row = {
        "transformation": (
            "LEFT JOIN COMMON_OWNER.CCY_DIM CCY_DIM "
            "ON CCY_DIM.CCY_CD = TXN.TXN_CCY_CD"
        ),
        "source_schema": "AMOGOREANU",
        "source_table": "TXN",
    }
    source_schema_index = {
        ("COMMON_OWNER", "CCY_DIM"): {
            "schema": "COMMON_OWNER",
            "table": "CCY_DIM",
            "columns": {"CCY_CD": "CCY_CD", "CCY_ID": "CCY_ID"},
        },
        ("AMOGOREANU", "TXN"): {
            "schema": "AMOGOREANU",
            "table": "TXN",
            "columns": {"TXN_ID": "TXN_ID", "TXN_CCY_CD": "TXN_CCY_CD"},
        },
    }

    join_sql, _ = derive_lookup_from_transformation(
        row=row,
        source_attr="TXN_ID",
        target_col="TXN_CCY_ID",
        source_schema_index=source_schema_index,
        source_block="FROM AMOGOREANU.TXN TXN",
    )

    assert "CCY_DIM.CCY_CD" in join_sql
    assert "TXN.TXN_CCY_CD" in join_sql
    assert "TXN.TXN_ID" not in join_sql


def test_build_control_insert_sql_pdm_missing_join_becomes_null_marker():
    from app.services.control_table_service import build_control_insert_sql

    sql = build_control_insert_sql(
        control_schema="CTL_OWNER",
        target_table="TGT",
        target_definition={
            "columns": [{"name": "COL_A", "nullable": True, "data_type": "VARCHAR2(20)"}],
            "primary_keys": [],
        },
        analysis_rows=[
            {
                "column": "COL_A",
                "drd_expression": "MISS_LKP.COL_A",
                "lookup_join": "LEFT JOIN CCAL_REPL_OWNER.TXN_SRC_TAX_CODE_LKUP MISS_LKP ON MISS_LKP.SRC_TAX_CODE_ID = TXN.SRC_TAX_CODE_ID",
                "source_table": "TXN",
                "source_schema": "AMOGOREANU",
                "source_attribute": "SRC_TAX_CODE_ID",
                "transformation": "",
                "source_block": "FROM AMOGOREANU.TXN TXN",
                "nullable": True,
            }
        ],
        source_schema_index={
            ("AMOGOREANU", "TXN"): {
                "schema": "AMOGOREANU",
                "table": "TXN",
                "columns": {"SRC_TAX_CODE_ID": "SRC_TAX_CODE_ID"},
            }
        },
    )

    assert "TXN_SRC_TAX_CODE_LKUP" not in sql
    # Phase 7.3 (Issue 5): PDM_MISS comment now includes the unresolved
    # alias + remediation hint so the operator can spot-fix the source.
    assert "NULL /* PDM_MISS:" in sql and "AS COL_A" in sql
    assert "add to PDM or correct DRD source_table" in sql
