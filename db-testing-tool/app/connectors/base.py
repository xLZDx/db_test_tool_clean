"""Base connector interface for database connections."""
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    """Column metadata — matches what all connectors produce."""
    column_name: str
    data_type: str
    nullable: bool = True
    is_pk: bool = False
    ordinal_position: int = 0


@dataclass
class TableInfo:
    """Table metadata — matches what all connectors produce."""
    schema: str
    table_name: str
    table_type: str = "TABLE"   # TABLE | VIEW | MVIEW


@dataclass
class ConnectionResult:
    """Result of a connection attempt."""
    success: bool
    message: str
    error: Optional[str] = None
    connection_id: Optional[str] = None
    server_version: Optional[str] = None


class BaseConnector:
    """Base class for all database connectors.

    Operator-locked (Phase 7.16): every concrete connector is SYNC.  The
    legacy `async` declarations on `execute_query` / `get_tables` were a
    Liskov violation -- subclasses overrode them as sync `def` returning
    `List[Dict]`, but the base advertised `Tuple[bool, List, Optional[str]]`
    plus async semantics.  Callers `await`ing the base signature got a
    coroutine wrapping a sync return.  The base interface is now sync to
    match reality; `_connection` is pre-initialised to `None` to make the
    `if self._connection:` guards in concrete `disconnect()` methods safe
    on a fresh instance.
    """

    def __init__(self, host: str, port: int, database: str,
                 username: str, password: str, extra_params: Optional[Dict] = None):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.extra_params = extra_params or {}
        # Phase 7.16 fix (type-design MAJOR-7): pre-init so concrete
        # `disconnect()` guards `if self._connection:` don't raise
        # AttributeError on a fresh instance that never called connect().
        self._connection: Optional[Any] = None

    def test_connection(self) -> ConnectionResult:
        """Test the connection."""
        raise NotImplementedError

    def execute_query(self, sql: str, params: Optional[Dict[str, Any]] = None,
                      max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
        """Execute a query.  Returns list of dict rows.

        Note: this signature matches the THREE concrete connectors
        (OracleConnector, RedshiftConnector, SqlServerConnector).  The
        legacy async + 3-tuple signature was never implemented anywhere.
        """
        raise NotImplementedError

    def get_tables(self, schema: str) -> List[TableInfo]:
        """Get list of tables in a schema."""
        raise NotImplementedError

    def get_columns(self, schema: str, table: str) -> List[ColumnInfo]:
        """Get list of columns for a table."""
        raise NotImplementedError

    def get_schemas(self) -> List[str]:
        """Get list of schemas."""
        raise NotImplementedError

    def connect(self) -> None:
        """Open a connection.  Sets self._connection."""
        raise NotImplementedError

    def disconnect(self) -> None:
        """Close connection if open."""
        raise NotImplementedError
