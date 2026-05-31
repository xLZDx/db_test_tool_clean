"""Oracle SQL validator: static parse (sqlglot) + optional live EXPLAIN (XE).

Operator-locked HARD rule (2026-05-29): no SQL artefact may be returned to
the operator or persisted to disk / a test_suite until this validator passes.

Two tiers:
  * STATIC (always runs) -- sqlglot with ``dialect="oracle"``.  Catches syntax
    errors offline.  Cheap, fast, no DB needed.
  * LIVE (optional)     -- xe_harness ``EXPLAIN PLAN FOR ...`` against Oracle
    XE.  Catches schema-level errors (missing tables/columns, dtype clashes).
    Skipped when XE is unavailable; result is reported as ``XE_UNAVAILABLE``
    so the caller knows live verification did not run.

Returned ``OracleValidationResult`` always tells the operator three things:
  * ``is_valid`` -- True only if EVERY statement passed every enabled tier.
  * ``static_errors`` -- one entry per failed statement, with sqlglot's error.
  * ``live_status`` -- ``"PASSED"`` / ``"FAILED"`` / ``"XE_UNAVAILABLE"`` /
    ``"SKIPPED"``; FAILED carries the ORA-xxxxx detail.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── Result dataclasses ────────────────────────────────────────────────────────

@dataclass
class StaticParseError:
    """One sqlglot parse failure."""
    statement_index: int       # 0-based index of the failing statement in the input
    statement_preview: str     # first ~120 chars of the offending statement
    error_message: str         # raw sqlglot error text
    line: Optional[int] = None
    column: Optional[int] = None


@dataclass
class OracleValidationResult:
    is_valid: bool
    static_errors: List[StaticParseError] = field(default_factory=list)
    statements_checked: int = 0
    live_status: str = "SKIPPED"      # PASSED / FAILED / XE_UNAVAILABLE / SKIPPED
    live_error: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_valid": self.is_valid,
            "statements_checked": self.statements_checked,
            "static_errors": [
                {
                    "statement_index": e.statement_index,
                    "statement_preview": e.statement_preview,
                    "error_message": e.error_message,
                    "line": e.line,
                    "column": e.column,
                }
                for e in self.static_errors
            ],
            "live_status": self.live_status,
            "live_error": self.live_error,
            "notes": list(self.notes),
        }


class OracleValidationError(RuntimeError):
    """Raised when an emitter / writer rejects non-parseable SQL.

    Callers that go through ``validate_oracle_sql`` and find ``is_valid=False``
    must raise this rather than silently returning the bad SQL.
    """
    def __init__(self, result: OracleValidationResult):
        self.result = result
        first = result.static_errors[0] if result.static_errors else None
        msg = "Oracle validation failed: "
        if first is not None:
            msg += (
                f"static parse error at statement #{first.statement_index}: "
                f"{first.error_message} -- preview: {first.statement_preview!r}"
            )
        elif result.live_status == "FAILED":
            msg += f"live EXPLAIN PLAN failed: {result.live_error}"
        else:
            msg += "unknown failure (no detail)"
        super().__init__(msg)


# ── Statement splitter (Oracle-aware) ─────────────────────────────────────────
#
# We need to split a multi-statement SQL script into individual statements so
# sqlglot parses each separately.  Oracle uses ``;`` for SQL terminators and
# ``/`` on its own line for PL/SQL block terminators.  String literals and
# comments must be respected when splitting.

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _mask_comments_and_strings(sql: str) -> str:
    """Replace comments + single-quoted strings with same-length spaces so
    splitter offsets stay aligned with the original text."""
    out = list(sql)
    # Block comments
    for m in _BLOCK_COMMENT_RE.finditer(sql):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    # Line comments
    for m in _LINE_COMMENT_RE.finditer(sql):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    # Single-quoted strings (Oracle's escape is '' inside a literal)
    i = 0
    s = "".join(out)
    out = list(s)
    in_str = False
    while i < len(out):
        ch = out[i]
        if ch == "'":
            if not in_str:
                in_str = True
                i += 1
                continue
            # closing quote OR escape '': peek next
            if i + 1 < len(out) and out[i + 1] == "'":
                # '' escape -> mask both as spaces
                out[i] = " "
                out[i + 1] = " "
                i += 2
                continue
            in_str = False
            i += 1
            continue
        if in_str and ch != "\n":
            out[i] = " "
        i += 1
    return "".join(out)


_PLSQL_START_RE = re.compile(r"\b(BEGIN|DECLARE)\b", re.IGNORECASE)


def _starts_plsql_block(masked: str, idx: int) -> bool:
    """True if the next non-whitespace token at masked[idx:] is BEGIN/DECLARE."""
    j = idx
    while j < len(masked) and masked[j] in " \t\r\n":
        j += 1
    m = _PLSQL_START_RE.match(masked, j)
    return m is not None


def split_oracle_statements(sql: str) -> List[str]:
    """Split a multi-statement Oracle SQL script into individual statements.

    Recognises:
      * ``;`` as a SQL statement terminator (depth-0, outside strings, outside
        a PL/SQL block).
      * ``/`` on its own line as the PL/SQL block terminator.
      * Anonymous PL/SQL blocks: when the next statement starts with BEGIN
        or DECLARE, every ``;`` is treated as an internal terminator until
        the closing ``/`` line is reached.
    """
    if not sql or not sql.strip():
        return []
    masked = _mask_comments_and_strings(sql)
    statements: List[str] = []
    start = 0
    depth = 0
    in_plsql = _starts_plsql_block(masked, 0)
    i = 0
    while i < len(masked):
        ch = masked[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0 and not in_plsql:
            stmt = sql[start:i].strip()
            if stmt:
                statements.append(stmt)
            start = i + 1
            # Decide whether the NEXT statement begins a PL/SQL block.
            in_plsql = _starts_plsql_block(masked, start)
        elif ch == "\n":
            # Detect "/" on its own line (PL/SQL terminator).  Also acts as a
            # statement terminator for regular SQL when no ``;`` was used.
            j = i + 1
            while j < len(masked) and masked[j] in " \t":
                j += 1
            if j < len(masked) and masked[j] == "/" and (
                j + 1 >= len(masked) or masked[j + 1] in "\n\r"
            ):
                stmt = sql[start:i].strip()
                if stmt:
                    statements.append(stmt)
                start = j + 2
                i = j + 1
                in_plsql = _starts_plsql_block(masked, start)
        i += 1
    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


# ── Static validation (sqlglot) ───────────────────────────────────────────────

def _statement_preview(stmt: str, max_len: int = 120) -> str:
    flat = re.sub(r"\s+", " ", stmt).strip()
    return flat[:max_len] + ("..." if len(flat) > max_len else "")


def _validate_one_statement(stmt: str, idx: int) -> Optional[StaticParseError]:
    """Return None on success, StaticParseError on failure."""
    import sqlglot
    from sqlglot.errors import ParseError, TokenError

    # Oracle PL/SQL anonymous blocks (DECLARE / BEGIN ... END) are not supported
    # by sqlglot's parser at the AST level; treat them as "skip with note" so
    # the operator still sees they were not strictly verified.  This keeps the
    # rule conservative: we never silently approve them.
    head = stmt.strip().upper()[:32]
    if head.startswith("BEGIN") or head.startswith("DECLARE"):
        return StaticParseError(
            statement_index=idx,
            statement_preview=_statement_preview(stmt),
            error_message="PL/SQL block is not statically validated; use an explicit admin-gated live runner",
        )

    try:
        sqlglot.parse(stmt, dialect="oracle")
        return None
    except (ParseError, TokenError) as exc:
        line = col = None
        # sqlglot >= 25 attaches a list of error dicts to ParseError
        errs = getattr(exc, "errors", None)
        if errs:
            first = errs[0] if isinstance(errs, list) and errs else errs
            if isinstance(first, dict):
                line = first.get("line")
                col = first.get("col")
        return StaticParseError(
            statement_index=idx,
            statement_preview=_statement_preview(stmt),
            error_message=str(exc),
            line=line,
            column=col,
        )
    except Exception as exc:
        # Defensive: ANY unexpected sqlglot internal error must still BLOCK.
        return StaticParseError(
            statement_index=idx,
            statement_preview=_statement_preview(stmt),
            error_message=f"sqlglot internal error: {exc!r}",
        )


# ── Live validation (xe_harness EXPLAIN PLAN) ────────────────────────────────

def _live_explain_plan(sql: str) -> tuple[str, str]:
    """Run EXPLAIN PLAN FOR <sql> against Oracle XE.

    Returns (status, detail) where status is one of:
      "PASSED" / "FAILED" / "XE_UNAVAILABLE" / "SKIPPED".
    """
    try:
        from app.db.xe_harness import get_xe_connection
    except ImportError:
        return ("SKIPPED", "xe_harness not importable")

    try:
        conn_info = get_xe_connection()
    except Exception as exc:
        return ("XE_UNAVAILABLE", f"connect failed: {exc}")
    if conn_info is None:
        return ("XE_UNAVAILABLE", "no XE connection available")

    try:
        cur = conn_info.cursor()
        cur.execute(f"EXPLAIN PLAN FOR {sql}")
        cur.close()
        return ("PASSED", "")
    except Exception as exc:
        return ("FAILED", str(exc))


# ── Public entry point ────────────────────────────────────────────────────────

def validate_oracle_sql(
    sql: str,
    *,
    run_live: bool = False,
) -> OracleValidationResult:
    """Validate an Oracle SQL script.

    Args:
      sql:      The full SQL text (may contain multiple statements).
      run_live: If True, also runs EXPLAIN PLAN FOR each statement against
                Oracle XE via ``xe_harness``.  Defaults to False because XE is
                often offline; static is mandatory regardless.
    """
    result = OracleValidationResult(is_valid=True)
    if not sql or not sql.strip():
        result.is_valid = False
        result.static_errors.append(
            StaticParseError(
                statement_index=0, statement_preview="",
                error_message="empty SQL input",
            )
        )
        return result

    statements = split_oracle_statements(sql)
    result.statements_checked = len(statements)
    if not statements:
        result.is_valid = False
        result.static_errors.append(
            StaticParseError(
                statement_index=0, statement_preview="",
                error_message="no SQL statements found after splitting",
            )
        )
        return result

    # ── Static ──
    for idx, stmt in enumerate(statements):
        err = _validate_one_statement(stmt, idx)
        if err is not None:
            result.is_valid = False
            result.static_errors.append(err)

    # ── Live (best-effort) ──
    if run_live and result.is_valid:
        statuses: List[str] = []
        for stmt in statements:
            up = stmt.strip().upper()
            # EXPLAIN PLAN does not apply to TRUNCATE / DDL / PL/SQL blocks;
            # skip those at the live tier but record the skip.
            if (up.startswith("TRUNCATE") or up.startswith("CREATE")
                    or up.startswith("DROP") or up.startswith("ALTER")
                    or up.startswith("BEGIN") or up.startswith("DECLARE")):
                statuses.append("SKIPPED")
                continue
            status, detail = _live_explain_plan(stmt)
            statuses.append(status)
            if status == "FAILED":
                result.is_valid = False
                result.live_status = "FAILED"
                result.live_error = detail
                break
        if result.is_valid:
            if any(s == "PASSED" for s in statuses):
                result.live_status = "PASSED"
            elif all(s == "SKIPPED" for s in statuses):
                result.live_status = "SKIPPED"
            else:
                # All non-skipped runs were XE_UNAVAILABLE
                result.live_status = "XE_UNAVAILABLE"
    elif not run_live:
        result.live_status = "SKIPPED"
        result.notes.append("live EXPLAIN PLAN skipped (run_live=False)")

    return result


def assert_valid_oracle_sql(sql: str, *, run_live: bool = False) -> None:
    """Convenience: validate and raise ``OracleValidationError`` on failure."""
    res = validate_oracle_sql(sql, run_live=run_live)
    if not res.is_valid:
        raise OracleValidationError(res)
