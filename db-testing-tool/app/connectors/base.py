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
    """Base class for all database connectors."""

    def __init__(self, host: str, port: int, database: str,
                 username: str, password: str, extra_params: Optional[Dict] = None):
        self.host = host
        self.port = port
        self.database = database
        self.username = username
        self.password = password
        self.extra_params = extra_params or {}

    def test_connection(self) -> ConnectionResult:
        """Test the connection."""
        raise NotImplementedError
    
    async def execute_query(self, query: str) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
        """Execute a query."""
        raise NotImplementedError
    
    async def get_tables(self) -> Tuple[bool, List[TableInfo], Optional[str]]:
        """Get list of tables."""
        raise NotImplementedError
