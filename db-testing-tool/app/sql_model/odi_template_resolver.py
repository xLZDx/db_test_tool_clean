"""Resolve ODI XML template tags and bind variables to SQL-ready strings.

ODI XML scenario exports contain two classes of non-SQL placeholders that
must be resolved before any SQL parser can process the text:

  1. Template tags:  <?= odiRef.getObjectName("L","TBL","SCHEMA","D") ?>
                     <?=odiRef.getSchemaName("SCHEMA","D") ?>
                     <?=odiRef.getSession("PARAM") ?>

  2. Bind variables: :GLOBAL.GV_ODI_SESS_NO
                     :SSDS.SSDS_MARKER_NAME
                     :GLOBAL.MONITOR_RECORDS_PER_THREAD

Operator rule (2026-05-28): `:GLOBAL.GV_*` bind vars -> replace with
meaningful values by context, NOT raise an error.
"""
from __future__ import annotations

import re
from typing import Optional

# ── Known bind-variable -> meaningful Oracle literal ─────────────────────────
# Key: UPPER portion after the namespace dot (e.g. "GV_ODI_SESS_NO").
# Value: a valid Oracle SQL literal (number or quoted string).
_BIND_VAR_DEFAULTS: dict[str, str] = {
    "GV_ODI_SESS_NO": "0",
    "GV_RT_THRD_NO": "1",
    "MONITOR_RECORDS_PER_THREAD": "1000",
    "MONITOR_SUBCRIBER_NAME": "'SUBSCRIBER'",
    "GV_MARKER_NAME": "'MARKER'",
    "SSDS_MARKER_NAME": "'MARKER'",
    # Staging variable names used by getObjectName resolver
    "SSDS_AVY_FACT_TABLE_NAME": "IKOROSTELEV.AVY_FACT_SIDE",
    "SSDS_AVY_FACT_EXCP_TABLE_NAME": "IKOROSTELEV.AVY_FACT_SIDE_EXCP",
    "SSDS_AVY_FACT_STEP5_STG": "SSDS_AVY_FACT_STEP5_STG",
}

# Regex patterns (compiled once at import time) ────────────────────────────────
_RE_OBJECT_NAME = re.compile(
    r"<\?=\s*odiRef\.getObjectName\(([^?]{1,400}?)\)\s*\?>",
    re.DOTALL,
)
_RE_SCHEMA_NAME = re.compile(
    r"<\?=\s*odiRef\.getSchemaName\(([^?]{1,200}?)\)\s*\?>",
    re.DOTALL,
)
_RE_SESSION = re.compile(
    r"<\?=\s*odiRef\.getSession\(([^?]{1,200}?)\)\s*\?>",
    re.DOTALL,
)
_RE_ANY_ODI = re.compile(r"<\?=\s*odiRef\.[^?]{0,400}?\?>", re.DOTALL)
# Matches :NAMESPACE.VAR_NAME (e.g. :GLOBAL.GV_ODI_SESS_NO or :SSDS.MARKER)
_RE_BIND_VAR = re.compile(r":([A-Z][A-Z0-9_]*)\.([A-Z0-9_]+)", re.I)


def _parse_odi_args(raw: str) -> list[str]:
    """Split the raw argument string of an odiRef.*(…) call."""
    return [tok.strip().strip('"').strip("'") for tok in raw.split(",")]


def resolve_odi_templates(sql: str, schema_map: Optional[dict[str, str]] = None) -> str:
    """Replace all ODI template tags and bind variables in *sql*.

    Args:
        sql:        Raw SQL text from a DefTxt block (may contain template tags).
        schema_map: Optional override mapping logical schema -> physical schema.
                    If absent the logical name is used as-is (e.g. 'CCAL_REPL_OWNER').

    Returns:
        SQL text with every placeholder replaced by a valid Oracle SQL fragment.
    """
    if schema_map is None:
        schema_map = {}

    def _resolve_object(m: re.Match) -> str:
        # getObjectName("L", "TABLE_OR_VAR", "SCHEMA", "D")
        args = _parse_odi_args(m.group(1))
        if len(args) < 3:
            return "UNKNOWN_TABLE"
        table_arg: str = args[1]   # e.g. "#SSDS.SSDS_AVY_FACT_STEP1_STG" or "J$AVY_FACT"
        schema_arg: str = args[2]  # e.g. "CCAL_REPL_OWNER"

        if table_arg.startswith("#"):
            # Staging-table variable: strip # prefix and variable namespace prefix.
            # "#SSDS.SSDS_AVY_FACT_STEP1_STG"  ->  "SSDS_AVY_FACT_STEP1_STG"
            tbl = table_arg.lstrip("#")
            if "." in tbl:
                tbl = tbl.split(".", 1)[1]
            return tbl
        else:
            schema = schema_map.get(schema_arg, schema_arg)
            return f"{schema}.{table_arg}"

    def _resolve_schema(m: re.Match) -> str:
        args = _parse_odi_args(m.group(1))
        schema = args[0] if args else "UNKNOWN_SCHEMA"
        return schema_map.get(schema, schema)

    def _resolve_session(m: re.Match) -> str:
        args = _parse_odi_args(m.group(1))
        param = args[0] if args else "SESSION"
        return f"'{param}'"

    def _resolve_bind(m: re.Match) -> str:
        name_upper = m.group(2).upper()
        # Direct match first
        if name_upper in _BIND_VAR_DEFAULTS:
            return _BIND_VAR_DEFAULTS[name_upper]
        # Fuzzy: check if any key is a suffix of the variable name
        for key, val in _BIND_VAR_DEFAULTS.items():
            if name_upper.endswith(key) or key.endswith(name_upper):
                return val
        return "NULL"

    sql = _RE_OBJECT_NAME.sub(_resolve_object, sql)
    sql = _RE_SCHEMA_NAME.sub(_resolve_schema, sql)
    sql = _RE_SESSION.sub(_resolve_session, sql)
    sql = _RE_ANY_ODI.sub("'ODI_EXPR'", sql)   # catch-all for unrecognised tags
    sql = _RE_BIND_VAR.sub(_resolve_bind, sql)
    return sql
