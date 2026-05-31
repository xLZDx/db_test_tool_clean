"""Oracle database connector using python-oracledb."""
from typing import Any, Dict, List, Optional
from app.connectors.base import BaseConnector, ConnectionResult, ColumnInfo, TableInfo
import logging
import re

logger = logging.getLogger(__name__)

# Global pool cache to avoid creating many pools for the same DB user/DSN
_GLOBAL_ORACLE_POOLS: Dict[str, object] = {}


class OracleConnector(BaseConnector):

    _SCRIPT_SEPARATOR_RE = re.compile(r"^\s*/\s*$", flags=re.MULTILINE)

    def __init__(self, host: str, port: int, database: str,
                 username: str, password: str, extra_params: Optional[Dict] = None):
        super().__init__(host, port or 1521, database, username, password, extra_params)
        self._pool = None

    def _dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"

    def _pool_key(self) -> str:
        return f"{self.username}@{self._dsn()}"

    def _use_pool(self) -> bool:
        raw = self.extra_params.get("use_pool", False)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _direct_connect(self):
        import oracledb
        timeout_seconds = float(self.extra_params.get("connect_timeout", 5))
        # Optional privileged-session mode (SYSDBA / SYSOPER) carried via
        # extra_params["mode"] so service-account flows like the
        # Phase 7.15 PDM regenerator can authenticate as SYS.
        mode_raw = str(self.extra_params.get("mode", "") or "").strip().upper()
        mode_kwarg: Dict[str, Any] = {}
        if mode_raw in ("SYSDBA", "SYSOPER"):
            sym = "SYSDBA" if mode_raw == "SYSDBA" else "SYSOPER"
            mode_const = getattr(oracledb, sym, None)
            if mode_const is not None:
                mode_kwarg = {"mode": mode_const}
        try:
            return oracledb.connect(
                user=self.username,
                password=self.password,
                dsn=self._dsn(),
                tcp_connect_timeout=timeout_seconds,
                **mode_kwarg,
            )
        except TypeError:
            # Backward compatibility with older python-oracledb versions.
            return oracledb.connect(
                user=self.username,
                password=self.password,
                dsn=self._dsn(),
                **mode_kwarg,
            )

    @staticmethod
    def _should_attempt_direct_fallback(pool_error: Exception) -> bool:
        msg = str(pool_error).upper()
        # DPY-6005 / ORA-12170 indicate connectivity timeout, and DPY-4005
        # means pool wait timed out. Direct fallback often repeats timeout.
        if "DPY-6005" in msg or "ORA-12170" in msg or "DPY-4005" in msg:
            return False
        return True

    @staticmethod
    def _is_pool_wait_timeout(pool_error: Exception) -> bool:
        return "DPY-4005" in str(pool_error).upper()

    def _reset_pool(self):
        pool = getattr(self, "_pool", None)
        if pool is not None:
            try:
                pool.close(force=True)
            except Exception:
                pass
        _GLOBAL_ORACLE_POOLS.pop(self._pool_key(), None)
        self._pool = None

    @staticmethod
    def _raise_pool_acquire_error(pool_error: Exception, fallback_error: Optional[Exception] = None) -> None:
        if fallback_error is None:
            raise RuntimeError(f"Failed to acquire Oracle connection from pool: {pool_error}")
        raise RuntimeError(
            f"Failed to acquire Oracle connection from pool: {pool_error}; "
            f"direct connect fallback failed: {fallback_error}"
        )

    def _acquire_connection(self):
        conn = None
        used_pool = False
        close_after_use = False

        if getattr(self, "_pool", None):
            try:
                conn = self._pool.acquire()
                used_pool = True
            except Exception as e:
                if self._is_pool_wait_timeout(e):
                    try:
                        self._reset_pool()
                        self.connect()
                        if getattr(self, "_pool", None):
                            conn = self._pool.acquire()
                            used_pool = True
                            return conn, used_pool, close_after_use
                    except Exception:
                        pass
                if not self._should_attempt_direct_fallback(e):
                    logger.error("Failed to acquire Oracle connection from pool: %s", e)
                    self._raise_pool_acquire_error(e)
                logger.warning("Failed to acquire Oracle connection from pool; attempting direct connection fallback: %s", e)
                try:
                    conn = self._direct_connect()
                    close_after_use = True
                except Exception as fallback_error:
                    logger.error("Failed to acquire Oracle connection from pool: %s", e)
                    self._raise_pool_acquire_error(e, fallback_error)
            return conn, used_pool, close_after_use

        if not self._connection:
            self.connect()
        if getattr(self, "_pool", None):
            try:
                conn = self._pool.acquire()
                used_pool = True
            except Exception as e:
                if self._is_pool_wait_timeout(e):
                    try:
                        self._reset_pool()
                        self.connect()
                        if getattr(self, "_pool", None):
                            conn = self._pool.acquire()
                            used_pool = True
                            return conn, used_pool, close_after_use
                    except Exception:
                        pass
                if not self._should_attempt_direct_fallback(e):
                    logger.error("Failed to acquire Oracle connection from pool: %s", e)
                    self._raise_pool_acquire_error(e)
                logger.warning("Failed to acquire Oracle connection from pool; attempting direct connection fallback: %s", e)
                try:
                    conn = self._direct_connect()
                    close_after_use = True
                except Exception as fallback_error:
                    logger.error("Failed to acquire Oracle connection from pool: %s", e)
                    self._raise_pool_acquire_error(e, fallback_error)
        else:
            conn = self._connection

        return conn, used_pool, close_after_use

    def test_connection(self) -> ConnectionResult:
        try:
            conn = self._direct_connect()
            ver = conn.version
            conn.close()
            return ConnectionResult(True, "Connected", ver)
        except Exception as e:
            return ConnectionResult(False, str(e))

    def connect(self):
        if not self._use_pool():
            if getattr(self, "_connection", None) is None:
                self._connection = self._direct_connect()
            self._pool = None
            return

        # Use a SessionPool to limit simultaneous sessions per DB user.
        import oracledb
        # Use a global cache keyed by username+dsn to reuse the same pool across connector instances
        pool_key = self._pool_key()
        if pool_key in _GLOBAL_ORACLE_POOLS:
            self._pool = _GLOBAL_ORACLE_POOLS[pool_key]
            return

        # Allow pool sizing via extra_params: pool_min, pool_max, pool_increment
        pool_min = int(self.extra_params.get("pool_min", 1))
        pool_max = int(self.extra_params.get("pool_max", 1))  # default to 1 to be conservative
        pool_increment = int(self.extra_params.get("pool_increment", 1))

        # Safety caps to avoid creating enormous pools
        if pool_min < 1:
            pool_min = 1
        if pool_max < pool_min:
            pool_max = pool_min
        if pool_max > 50:
            pool_max = 50

        pool_kwargs = {
            "user": self.username,
            "password": self.password,
            "dsn": self._dsn(),
            "min": pool_min,
            "max": pool_max,
            "increment": pool_increment,
            "threaded": True,
        }
        getmode_pref = str(self.extra_params.get("pool_getmode", "nowait")).strip().lower()
        timed_wait = getattr(oracledb, "POOL_GETMODE_TIMEDWAIT", None) or getattr(oracledb, "SPOOL_ATTRVAL_TIMEDWAIT", None)
        no_wait = getattr(oracledb, "POOL_GETMODE_NOWAIT", None) or getattr(oracledb, "SPOOL_ATTRVAL_NOWAIT", None)
        if getmode_pref == "timedwait" and timed_wait is not None:
            pool_kwargs["getmode"] = timed_wait
            pool_kwargs["wait_timeout"] = int(self.extra_params.get("pool_wait_timeout", 2))
        elif no_wait is not None:
            pool_kwargs["getmode"] = no_wait
        pool = oracledb.SessionPool(**pool_kwargs)
        _GLOBAL_ORACLE_POOLS[pool_key] = pool
        self._pool = pool

    def disconnect(self):
        # Do not close global shared pools here (others may be using them).
        # Only clear instance references; pool lifecycle is process-level.
        self._pool = None

        if getattr(self, "_connection", None):
            try:
                self._connection.close()
            except Exception:
                pass
            self._connection = None

    def get_schemas(self) -> List[str]:
        sql = """
            SELECT username FROM all_users
            WHERE oracle_maintained = 'N'
            ORDER BY username
        """
        return [r["USERNAME"] for r in self.execute_query(sql)]

    def get_tables(self, schema: str) -> List[TableInfo]:
        sql = """
            SELECT owner, object_name, object_type
            FROM all_objects
            WHERE owner = :schema
              AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW')
            ORDER BY object_name
        """
        rows = self.execute_query(sql, {"schema": schema.upper()})
        results = []
        for r in rows:
            otype = r["OBJECT_TYPE"]
            if otype == "MATERIALIZED VIEW":
                otype = "MVIEW"
            results.append(TableInfo(r["OWNER"], r["OBJECT_NAME"], otype))
        return results

    def get_columns(self, schema: str, table: str) -> List[ColumnInfo]:
        sql = """
            SELECT c.column_name, c.data_type, c.nullable, c.column_id,
                   CASE WHEN pk.column_name IS NOT NULL THEN 1 ELSE 0 END AS is_pk
            FROM all_tab_columns c
            LEFT JOIN (
                SELECT acc.column_name
                FROM all_constraints ac
                JOIN all_cons_columns acc ON ac.constraint_name = acc.constraint_name
                    AND ac.owner = acc.owner
                WHERE ac.constraint_type = 'P'
                  AND ac.owner = :schema AND ac.table_name = :table_name
            ) pk ON c.column_name = pk.column_name
            WHERE c.owner = :schema AND c.table_name = :table_name
            ORDER BY c.column_id
        """
        rows = self.execute_query(sql, {"schema": schema.upper(), "table_name": table.upper()})
        return [
            ColumnInfo(
                column_name=r["COLUMN_NAME"],
                data_type=r["DATA_TYPE"],
                nullable=r["NULLABLE"] == "Y",
                is_pk=bool(r["IS_PK"]),
                ordinal_position=r["COLUMN_ID"] or 0,
            ) for r in rows
        ]

    def execute_query(self, sql: str, params: Optional[Dict] = None, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
        # Acquire a connection from the pool if available, otherwise ensure a single connection
        import time
        if getattr(self, "_pool", None) is None and getattr(self, "_connection", None) is None:
            self.connect()

        conn, used_pool, close_after_use = self._acquire_connection()

        if conn is None:
            logger.error("Oracle connection is None. Could not connect to database.")
            raise RuntimeError("Oracle connection is None. Could not connect to database.")

        attempts = 0
        try:
            while True:
                try:
                    statements = self._split_sql_statements(sql)
                    # Preserve bind support for the common single-statement path.
                    if len(statements) == 1:
                        cur = conn.cursor()
                        try:
                            cur.execute(statements[0], params or {})
                            if cur.description:
                                cols = [d[0] for d in cur.description]
                                if isinstance(max_rows, int) and max_rows > 0:
                                    fetched = cur.fetchmany(max_rows)
                                else:
                                    fetched = cur.fetchall()
                                return [dict(zip(cols, row)) for row in fetched]
                            conn.commit()
                            affected = cur.rowcount if isinstance(cur.rowcount, int) and cur.rowcount > -1 else 0
                            return [{"ROWS_AFFECTED": affected}]
                        finally:
                            try:
                                cur.close()
                            except Exception:
                                pass

                    # Multi-statement scripts are executed statement-by-statement.
                    # Return rows from the last query statement if any.
                    last_rows: List[Dict[str, Any]] = []
                    for statement in statements:
                        cur = conn.cursor()
                        try:
                            cur.execute(statement)
                            if cur.description:
                                cols = [d[0] for d in cur.description]
                                last_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                        finally:
                            try:
                                cur.close()
                            except Exception:
                                pass
                    conn.commit()
                    return last_rows
                except Exception as e:
                    # If sessions-per-user limit is hit, retry a few times with backoff
                    msg = str(e).upper()
                    if 'ORA-02391' in msg or 'SESSIONS_PER_USER' in msg:
                        attempts += 1
                        if attempts <= 3:
                            wait = 0.5 * attempts
                            logger.warning("ORA-02391 encountered - retrying after %.1fs (attempt %d)", wait, attempts)
                            time.sleep(wait)
                            continue
                    # Re-raise after no more retries
                    raise
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            if (used_pool or close_after_use) and conn is not None:
                try:
                    conn.close()  # returns connection to pool
                except Exception:
                    pass

    def _quote_identifier(self, value: str) -> str:
        text = str(value or "").strip()
        if not text or '"' in text or "\x00" in text:
            raise ValueError(f"unsafe Oracle identifier: {value!r}")
        return f'"{text}"'

    def get_row_count(self, schema: str, table: str) -> int:
        rows = self.execute_query(
            f"SELECT COUNT(*) AS cnt FROM {self._quote_identifier(schema)}.{self._quote_identifier(table)}"
        )
        return rows[0]["CNT"] if rows else 0

    def validate_sql(self, sql: str) -> Optional[str]:
        """Validate SQL syntax via EXPLAIN PLAN without executing it.

        Returns ``None`` on success or an error message string on failure.
        """
        results = self.validate_sql_batch([sql])
        return results[0]

    def validate_sql_batch(self, sql_list: list) -> list:
        """Validate a list of SQL statements.  Returns a list of error strings
        (``None`` for valid statements) in the same order as *sql_list*.

        Uses a single connection for the entire batch to avoid pool contention.
        """
        if getattr(self, "_pool", None) is None and getattr(self, "_connection", None) is None:
            self.connect()

        conn = None
        used_pool = False
        close_after_use = False
        try:
            conn, used_pool, close_after_use = self._acquire_connection()
        except Exception:
            conn = None

        if conn is None:
            return ["Cannot acquire Oracle connection for SQL validation."] * len(sql_list)

        results: list = []
        try:
            for sql in sql_list:
                errors = []
                for statement in self._split_sql_statements(sql):
                    if self._skip_explain_for_statement(statement):
                        head = (statement or "").lstrip().upper()
                        if head.startswith(("BEGIN", "DECLARE")):
                            errors.append("PL/SQL block was not validated by EXPLAIN PLAN; use live execution in an explicit admin-gated path")
                        continue
                    cur = conn.cursor()
                    try:
                        cur.execute(f"EXPLAIN PLAN FOR {statement}")
                    except Exception as e:
                        errors.append(str(e))
                    finally:
                        try:
                            cur.close()
                        except Exception:
                            pass
                results.append("; ".join(errors) if errors else None)
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            if (used_pool or close_after_use) and conn is not None:
                try:
                    conn.close()  # returns connection to pool
                except Exception:
                    pass
        return results

    def _split_sql_statements(self, sql_text: str) -> List[str]:
        text = (sql_text or "").replace("\r\n", "\n").strip()
        if not text:
            return []

        chunks = [c.strip() for c in self._SCRIPT_SEPARATOR_RE.split(text) if c and c.strip()]
        statements: List[str] = []
        for chunk in chunks:
            upper = chunk.lstrip().upper()
            if upper.startswith("BEGIN") or upper.startswith("DECLARE"):
                # Keep PL/SQL terminator so anonymous blocks execute correctly.
                statements.append(chunk)
                continue
            statements.extend(self._split_non_block_statements(chunk))
        return [s for s in statements if s]

    def _split_non_block_statements(self, sql_text: str) -> List[str]:
        out: List[str] = []
        current: List[str] = []
        in_single = False
        in_double = False
        in_line_comment = False
        in_block_comment = False
        i = 0
        text = sql_text or ""
        n = len(text)

        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""

            if in_line_comment:
                current.append(ch)
                if ch == "\n":
                    in_line_comment = False
                i += 1
                continue

            if in_block_comment:
                current.append(ch)
                if ch == "*" and nxt == "/":
                    current.append(nxt)
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue

            if not in_single and not in_double:
                if ch == "-" and nxt == "-":
                    current.append(ch)
                    current.append(nxt)
                    in_line_comment = True
                    i += 2
                    continue
                if ch == "/" and nxt == "*":
                    current.append(ch)
                    current.append(nxt)
                    in_block_comment = True
                    i += 2
                    continue

            if ch == "'" and not in_double:
                # Keep escaped Oracle single quotes ('') inside string literal.
                if in_single and nxt == "'":
                    current.append(ch)
                    current.append(nxt)
                    i += 2
                    continue
                in_single = not in_single
                current.append(ch)
                i += 1
                continue

            if ch == '"' and not in_single:
                in_double = not in_double
                current.append(ch)
                i += 1
                continue

            if ch == ";" and not in_single and not in_double:
                statement = "".join(current).strip()
                if statement:
                    out.append(statement)
                current = []
                i += 1
                continue

            current.append(ch)
            i += 1

        tail = "".join(current).strip()
        if tail:
            out.append(tail)
        return out

    def _skip_explain_for_statement(self, statement: str) -> bool:
        stmt = (statement or "").lstrip().upper()
        if not stmt:
            return True
        if stmt.startswith("BEGIN") or stmt.startswith("DECLARE"):
            return True
        return stmt.startswith((
            "CREATE ",
            "ALTER ",
            "DROP ",
            "TRUNCATE ",
            "COMMENT ",
            "GRANT ",
            "REVOKE ",
        ))
