"""Connector factory for getting database connectors."""
from typing import Optional
from app.connectors.base import BaseConnector
from app.secret_store import decrypt_secret_if_needed, decrypt_sensitive_extra_params_dict


def get_connector(datasource_model) -> Optional[BaseConnector]:
    """Get a database connector instance from a datasource model.

    Args:
        datasource_model: SQLAlchemy DataSource model or SimpleNamespace with
            .db_type, .host, .port, .database_name, .username, .password,
            .extra_params attributes.

    Returns:
        Connector instance, or None if the datasource type is not supported.
    """
    db_type = (getattr(datasource_model, "db_type", None) or "").lower().strip()
    host = getattr(datasource_model, "host", "") or ""
    port_raw = getattr(datasource_model, "port", None)
    # Support both .database_name (SQLAlchemy model) and .database (SimpleNamespace legacy)
    database = (
        getattr(datasource_model, "database_name", None)
        or getattr(datasource_model, "database", None)
        or ""
    )
    username = getattr(datasource_model, "username", "") or ""
    password = decrypt_secret_if_needed(getattr(datasource_model, "password", "") or "") or ""
    extra_params = getattr(datasource_model, "extra_params", None) or {}
    if isinstance(extra_params, str):
        import json
        try:
            extra_params = json.loads(extra_params)
        except Exception:
            extra_params = {}
    if isinstance(extra_params, dict):
        extra_params = decrypt_sensitive_extra_params_dict(extra_params)

    if db_type == "oracle":
        from app.connectors.oracle_connector import OracleConnector
        port = int(port_raw) if port_raw else 1521
        return OracleConnector(
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            extra_params=extra_params,
        )

    if db_type in ("redshift",):
        from app.connectors.redshift_connector import RedshiftConnector
        port = int(port_raw) if port_raw else 5439
        return RedshiftConnector(
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            extra_params=extra_params,
        )

    if db_type in ("sqlserver", "mssql", "sql server"):
        from app.connectors.sqlserver_connector import SqlServerConnector
        port = int(port_raw) if port_raw else 1433
        return SqlServerConnector(
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            extra_params=extra_params,
        )

    # Phase 7.16 (type-design MAJOR-6 fix): was `return None` which propagated
    # silently into `schema_kb_service.py:627` `connector.connect()` -> raised
    # AttributeError far from the root cause.  Now fail loud with the actual
    # db_type so the operator gets one clear "unsupported X" message.
    raise ValueError(
        f"Unsupported datasource db_type={db_type!r}; supported: "
        "oracle, redshift, sqlserver/mssql"
    )


def get_connector_from_model(datasource_model) -> BaseConnector:
    """Alias for get_connector.  Raises ValueError on unsupported db_type."""
    return get_connector(datasource_model)

