#!/usr/bin/env python3
"""App-native, hardened wrapper around the vendored v18.0 insert builder.

Gate V1 (2026-06-09): the control-table generator must emit the KB-resolved
v18 INSERT (not the v5.4 heuristic that leaked prose joins / wrong owners).

This module deliberately does NOT use the v18 scaffold ``service_adapter.py`` /
``fastapi_router.py`` (those take caller-supplied filesystem paths, have no auth,
leak subprocess stderr into HTTP 500s, and run a 3600s blocking subprocess on the
event loop -- all flagged in the V1 review). Instead it exposes one pure-sync
function the FastAPI layer calls via ``asyncio.to_thread`` with server-controlled
paths only.

Pinned canonical v18 tree (git-tracked). The stray copies under
``temp files/New folder/v18_unzipped/`` and ``temp files/OLD/`` are NOT used.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Single pinned location for the v18 tool (review: pin ONE copy; fail loud if absent).
V18_TOOL_ROOT = (
    _REPO_ROOT
    / "temp files"
    / "New folder"
    / "drd_odi_insert_universal_tool_v18_0"
    / "drd_odi_insert_universal_tool_v18_0"
)
_INSERT_SCRIPT = V18_TOOL_ROOT / "insert_builder" / "universal_insert_builder.py"
_RESOLUTION_PROFILE = V18_TOOL_ROOT / "insert_builder" / "profiles" / "lh_ds3_resolution_profile.json"

# Default schema KB. ds_1 and ds_3 are equivalent for KBLookup, but the v18
# resolution profile is "lh_ds3", so the matching KB is ds_3.
_DEFAULT_SCHEMA_KB = _REPO_ROOT / "data" / "local_kb" / "schema_kb_ds_3.json"

# Audit/system columns are legitimately populated by ETL/triggers, not by the
# DRD mapping; a NULL there is acceptable. Business columns emitted as NULL are
# the real "stub" the operator rejects (classified, not hidden -- see Gate V2).
_AUDIT_STUB_COLUMNS = frozenset({
    "CRT_DTM", "CRT_USR_NM", "LAST_UDT_DTM", "LAST_UDT_USR_NM",
    "CREATE_DTM", "CREATE_USER", "UPDATE_DTM", "UPDATE_USER",
    "LOAD_DTM", "ETL_BATCH_ID",
})

_DEFAULT_TIMEOUT_S = 120


def _read_impl_null_status(out_dir: Path) -> Dict[str, str]:
    """From the v18 tool's `implementation_map.csv` (authoritative per-column
    resolution), classify each NULL-emitted target column GENERICALLY -- no
    hardcoded column names. Returns {COLUMN -> 'null_per_drd' | 'real_stub'}.

    A NULL is ``null_per_drd`` (expected, not a defect) when the DRD's own resolved
    expression is itself NULL/blank -- i.e. the DRD maps the column to NULL, OR the
    DRD's source is unresolvable so v18 correctly emits NULL (operator: a DRD that
    references a non-existent/ambiguous source is a DRD bug, and the column should be
    NULL). A NULL is a ``real_stub`` only when the DRD intended a concrete non-NULL
    source (``drd_expression`` is a real expression) yet v18 still produced NULL --
    that is the genuine "business column with data that did not get mapped".

    Columns absent from the map (unknown) are NOT returned -> the caller keeps them
    as business stubs (conservative: never hide an unexplained NULL).
    """
    p = out_dir / "implementation_map.csv"
    if not p.exists():
        return {}
    status: Dict[str, str] = {}
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                col = (row.get("target_column") or "").strip().upper()
                gen = (row.get("generated_expression") or "").strip()
                drd = (row.get("drd_expression") or "").strip()
                if not col or gen.upper() != "NULL":
                    continue
                status[col] = "real_stub" if (drd and drd.upper() != "NULL") else "null_per_drd"
    except (OSError, csv.Error, UnicodeDecodeError):
        return {}
    return status


class V18BuildError(RuntimeError):
    """Raised when the v18 builder cannot produce a valid INSERT.

    The message is ASCII-only and never contains a server filesystem path, so it
    is safe to surface (the FastAPI layer maps it to HTTP 422).
    """


def _fix_alias_in_on(sql: str) -> tuple[str, list]:
    """Fix v18's ORA-00904: it sometimes puts a SELECT-list OUTPUT ALIAS inside a
    JOIN ON predicate, e.g. ``ON FA_NUMBER_V.FA_NUMBER = OWN_FA_NUM`` where
    ``AR_GRP_SUBDIM.FA_NUM AS OWN_FA_NUM`` is in the SELECT. Oracle does not expose
    SELECT aliases in ON scope -> invalid identifier.

    Generic + safe: build alias -> QUALIFIED source (``a.b AS ALIAS``); then, ONLY
    inside JOIN-line ON predicates, replace a BARE identifier equal to such an alias
    with its source. Qualified refs (``x.ALIAS``) are protected by the lookbehind, so
    real join columns are never touched; tables without the pattern are a no-op.
    Returns (fixed_sql, [aliases_fixed]).
    """
    alias_src = {}
    # qualified-source aliases: "<a.b> AS ALIAS" terminated by comma / whitespace
    # (e.g. newline before FROM, for the last SELECT item) / close-paren / end.
    for m in re.finditer(r"(?<![.\w])([A-Za-z0-9_$#]+\.[A-Za-z0-9_$#]+)\s+AS\s+([A-Za-z0-9_$#]+)(?=[\s,)]|$)",
                         sql, re.I):
        alias_src[m.group(2).upper()] = m.group(1)
    if not alias_src:
        return sql, []
    fixed, out_lines = [], []
    for line in sql.split("\n"):  # split (not splitlines) -> exact identity when no change
        if re.search(r"\bJOIN\b", line, re.I):
            jm = re.search(r"\bON\b", line, re.I)
            if jm:
                head, tail = line[:jm.end()], line[jm.end():]
                for cand, src in alias_src.items():
                    new_tail = re.sub(r"(?<![.\w])" + re.escape(cand) + r"(?![.\w])", src, tail)
                    if new_tail != tail:
                        tail = new_tail
                        if cand not in fixed:
                            fixed.append(cand)
                line = head + tail
        out_lines.append(line)
    return "\n".join(out_lines), fixed


def _reorder_joins_by_dependency(sql: str) -> tuple[str, list]:
    """Fix v18's forward-reference ORA-00904: a JOIN's ON references an alias that
    is introduced by a LATER join. Reorder the join list so each join appears AFTER
    the aliases its ON predicate references (stable topological sort, base table
    first). Each join keeps its own ON, so outer-join results are unchanged; only
    the textual order moves to satisfy alias scope. No-op when already ordered.
    Returns (reordered_sql, [relocated_aliases]).
    """
    lines = sql.split("\n")
    join_re = re.compile(r"^\s*((?:LEFT|RIGHT|INNER|FULL|CROSS|OUTER)\s+)*JOIN\s+", re.I)
    # main FROM = a FROM line (no JOIN on it) immediately followed by a JOIN line
    from_idx = None
    for i in range(len(lines) - 1):
        if (re.match(r"^\s*FROM\s+\S+", lines[i], re.I) and "JOIN" not in lines[i].upper()
                and join_re.match(lines[i + 1])):
            from_idx = i
            break
    if from_idx is None:
        return sql, []
    fm = re.match(r"^\s*FROM\s+(\S+)\s+(\S+)", lines[from_idx], re.I)
    base_alias = fm.group(2).upper() if fm else ""

    j = from_idx + 1
    join_lines = []
    while j < len(lines) and join_re.match(lines[j]):
        join_lines.append(lines[j])
        j += 1
    if len(join_lines) < 2:
        return sql, []

    parsed, aliases = [], set()
    for ln in join_lines:
        m = re.search(r"JOIN\s+(\S+)\s+(\S+?)(?:\s+ON\s+(.*))?$", ln, re.I)
        alias = m.group(2).upper() if m else ""
        on_pred = (m.group(3) or "") if m else ""
        deps = {x.upper() for x in re.findall(r"([A-Za-z0-9_$#]+)\.[A-Za-z0-9_$#]+", on_pred)}
        parsed.append({"line": ln, "alias": alias, "deps": deps})
        aliases.add(alias)
    all_aliases = aliases | {base_alias}

    defined = {base_alias}
    remaining, ordered = parsed[:], []
    while remaining:
        placed = False
        for k, jn in enumerate(remaining):
            real_deps = (jn["deps"] & all_aliases) - {jn["alias"]}
            if real_deps <= defined:
                ordered.append(jn); defined.add(jn["alias"]); remaining.pop(k); placed = True
                break
        if not placed:  # cycle / external dep -> keep original order, break the stall
            jn = remaining.pop(0); ordered.append(jn); defined.add(jn["alias"])

    new_join_lines = [jn["line"] for jn in ordered]
    if new_join_lines == join_lines:
        return sql, []
    relocated = [ordered[i]["alias"] for i in range(len(ordered)) if ordered[i]["line"] != join_lines[i]]
    new_lines = lines[:from_idx + 1] + new_join_lines + lines[j:]
    return "\n".join(new_lines), relocated


_JOIN_LINE_RE = re.compile(r"^(\s*)((?:LEFT|RIGHT|INNER|FULL|CROSS)\s+)*JOIN\b", re.I)


def _widen_inner_to_left(sql: str) -> tuple[str, list]:
    """V10: convert bare/INNER JOINs to LEFT JOINs (fact-load grain preservation).

    v18 sometimes emits a dimension lookup as a bare ``JOIN`` (INNER). In a
    DRD-driven FACT-table load that silently DROPS fact rows whenever the optional
    dimension does not match -- and an INNER join downstream of a LEFT-joined alias
    also cancels the upstream LEFT. The fix is the standard fact-load rule: every
    join after the base preserves the grain, i.e. is a LEFT join. Filters belong in
    WHERE, not in the join type. (Operator-approved; verified by the row-production
    cert: AVY went 0 rows -> rows after this.) LEFT/RIGHT/FULL/CROSS are untouched.
    Returns (sql, [aliases_widened]).
    """
    widened, out_lines = [], []
    for line in sql.split("\n"):
        # only a BARE 'JOIN' or 'INNER JOIN' at line start (not LEFT/RIGHT/FULL/CROSS)
        if re.match(r"^\s*(INNER\s+)?JOIN\b", line, re.I):
            m = re.search(r"\bJOIN\s+\S+\s+([A-Za-z0-9_$#]+)\b", line, re.I)
            new = re.sub(r"^(\s*)(?:INNER\s+)?JOIN\b", r"\1LEFT JOIN", line, count=1, flags=re.I)
            if new != line:
                out_lines.append(new)
                if m:
                    widened.append(m.group(1))
                continue
        out_lines.append(line)
    return "\n".join(out_lines), widened


def _drop_unreferenced_cross_joins(sql: str) -> tuple[str, list]:
    """V11: drop ``JOIN <table> <alias> ON 1=1`` lines whose alias is never
    referenced (``alias.col``) anywhere in the statement.

    v18 can emit one self-join per source column with ``ON 1=1`` instead of reusing
    a single join + a real key (e.g. IMP_OTSND: 142 self-joins to one table, only 1
    referenced). An unreferenced ``ON 1=1`` join is a pure cartesian multiplier
    (left_rows x table_rows) contributing nothing -> removing it is correctness-
    preserving AND kills the row explosion / parse blow-up. Referenced ON-1=1 joins
    are left alone (their missing key is a separate, deeper issue). Returns
    (sql, [dropped_aliases]).
    """
    # aliases referenced as <alias>.<col> anywhere in the SQL
    refs = {m.group(1).upper() for m in re.finditer(r"(?<![.\w])([A-Za-z0-9_$#]+)\.[A-Za-z0-9_$#]+", sql)}
    dropped, out_lines = [], []
    for line in sql.split("\n"):
        m = re.match(r"^\s*(?:(?:LEFT|RIGHT|INNER|FULL|CROSS)\s+)*JOIN\s+\S+\s+([A-Za-z0-9_$#]+)\s+ON\s+(.*\S)\s*$",
                     line, re.I)
        if m:
            alias, on = m.group(1), m.group(2).strip()
            if re.fullmatch(r"1\s*=\s*1", on) and alias.upper() not in refs:
                dropped.append(alias)
                continue  # drop the line
        out_lines.append(line)
    return "\n".join(out_lines), dropped


def _promote_real_base(sql: str) -> tuple[str, Optional[str]]:
    """V12 (table-level, not alias-level): when v18 picks a base table that the
    projection never uses and self-joins the REAL source ``ON 1=1`` (e.g. IMP_OTSND:
    base TXN unreferenced + 142 ``ON 1=1`` joins to one table, a 1:1 DRD), promote the
    real source table to the FROM and drop the bogus base + the cartesian joins.

    Tightly gated so it only fires for that defect: the FROM-base alias must be
    referenced NOWHERE (``base.col`` absent), AND every join must be ``ON 1=1`` (no
    real key -> the base contributes no grain). Then the single referenced ON-1=1
    table becomes the base and all those joins are dropped. If the base is used, or
    any join has a real key, this is a no-op (AVY/CLOSE/OPEN untouched). Returns
    (sql, new_base_alias_or_None).
    """
    lines = sql.split("\n")
    base_idx = base_alias = None
    base_re = re.compile(r"^(\s*)FROM\s+\S+\s+([A-Za-z0-9_$#]+)\s*$", re.I)
    for i, ln in enumerate(lines):
        m = base_re.match(ln)
        if m and not re.search(r"\bJOIN\b", ln, re.I):
            base_idx, base_alias = i, m.group(2)
            break
    if base_idx is None:
        return sql, None
    # base must be referenced NOWHERE (projection + ON clauses) to be promotable
    if re.search(r"(?<![.\w])" + re.escape(base_alias) + r"\.[A-Za-z0-9_$#]+", sql):
        return sql, None
    join_idxs = [i for i, ln in enumerate(lines)
                 if re.match(r"^\s*(?:(?:LEFT|RIGHT|INNER|FULL|CROSS)\s+)*JOIN\b", ln, re.I)]
    if not join_idxs:
        return sql, None
    # every join must be ON 1=1 (a real-key join means the base may define grain)
    new_base = None
    for i in join_idxs:
        m = re.search(r"\bJOIN\s+(\S+)\s+([A-Za-z0-9_$#]+)\s+ON\s+(.*\S)\s*$", lines[i], re.I)
        if not m or not re.fullmatch(r"1\s*=\s*1", m.group(3).strip()):
            return sql, None
        if new_base is None and re.search(r"(?<![.\w])" + re.escape(m.group(2)) + r"\.[A-Za-z0-9_$#]+", sql):
            new_base = (m.group(1), m.group(2))
    if new_base is None:
        return sql, None
    indent = re.match(r"^(\s*)", lines[base_idx]).group(1)
    lines[base_idx] = f"{indent}FROM {new_base[0]} {new_base[1]}"
    out = [ln for i, ln in enumerate(lines) if i not in set(join_idxs)]
    return "\n".join(out), new_base[1]


def _coerce_number_varchar_joins(sql: str, kb, len_resolver) -> tuple[str, list]:
    """V13: when a JOIN equates a NUMBER column to a VARCHAR column, wrap the NUMBER
    side as ``LPAD(TO_CHAR(num), <varchar_len>, '0')`` so the comparison is done in
    the VARCHAR domain (operator-chosen). This (a) is robust to non-numeric junk in
    the VARCHAR (no implicit string->number -> no ORA-01722), and (b) preserves
    leading-zero codes (ISO `'064'` etc.) that a naive ``TO_CHAR`` would break.

    GENERIC -- no hardcoded names: column types come from the schema KB; the pad
    width is the VARCHAR column's length from ``len_resolver(owner, table, col)``
    (DB-backed; supplied by the caller). When the KB lacks a type or the resolver
    returns no length, the predicate is left untouched (ODI-faithful). Only simple
    ``a.col = b.col`` equalities on JOIN lines are touched; >=, <, ranges, and
    matched-type joins are no-ops. Returns (sql, [coerced "alias.col" number sides]).
    """
    if len_resolver is None or kb is None:
        return sql, []
    from app.sql_model.static_validator import TableRef  # lazy

    alias_tbl: Dict[str, tuple] = {}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_$#]+)\.([A-Za-z0-9_$#]+)\s+([A-Za-z0-9_$#]+)\b",
                         sql, re.I):
        alias_tbl[m.group(3).upper()] = (m.group(1).upper(), m.group(2).upper())

    _type_cache: Dict[tuple, Optional[str]] = {}

    def col_type(alias: str, col: str) -> Optional[str]:
        ot = alias_tbl.get(alias.upper())
        if not ot:
            return None
        key = (ot[0], ot[1], col.upper())
        if key in _type_cache:
            return _type_cache[key]
        try:
            ref = TableRef(owner=ot[0], name=ot[1])
        except TypeError:
            ref = TableRef(ot[0], ot[1])
        cols = kb.get_columns(ref) or {}
        meta = cols.get(col.upper()) or cols.get(col) or {}
        dt = (meta.get("data_type") or "").upper() or None
        _type_cache[key] = dt
        return dt

    coerced: list = []

    def _repl(m):
        a1, c1, a2, c2 = m.group(1), m.group(2), m.group(3), m.group(4)
        t1, t2 = col_type(a1, c1), col_type(a2, c2)
        if not t1 or not t2:
            return m.group(0)
        if t1 == "NUMBER" and t2.startswith("VARCHAR"):
            num, vc = (a1, c1), (a2, c2)
        elif t2 == "NUMBER" and t1.startswith("VARCHAR"):
            num, vc = (a2, c2), (a1, c1)
        else:
            return m.group(0)  # matched types (or both non-num/vc) -> leave alone
        ot = alias_tbl.get(vc[0].upper())
        ln = None
        try:
            ln = len_resolver(ot[0], ot[1], vc[1].upper()) if ot else None
        except Exception:  # noqa: BLE001 -- resolver failure -> ODI-faithful no-op
            ln = None
        if not ln or ln <= 0:
            return m.group(0)
        coerced.append(f"{num[0]}.{num[1]}")
        return f"LPAD(TO_CHAR({num[0]}.{num[1]}), {int(ln)}, '0') = {vc[0]}.{vc[1]}"

    eq_re = re.compile(r"([A-Za-z0-9_$#]+)\.([A-Za-z0-9_$#]+)\s*=\s*([A-Za-z0-9_$#]+)\.([A-Za-z0-9_$#]+)")
    out_lines = []
    for line in sql.split("\n"):
        if re.search(r"\bJOIN\b", line, re.I) and re.search(r"\bON\b", line, re.I):
            line = eq_re.sub(_repl, line)
        out_lines.append(line)
    return "\n".join(out_lines), coerced


def _nvl_wrap_expr(val: str, dtype: Optional[str]) -> Optional[str]:
    """Wrap a projection value in ``NVL(.., <type default>)`` for a target column
    data type, or None when the type is unknown (-> leave it unwrapped).

    For VARCHAR targets the value is run through ``TO_CHAR`` first so the NVL
    operands are always strings -- this is a no-op on strings but converts a NUMBER
    expr (a DRD type mismatch), avoiding ``NVL(number, '-999')`` -> ORA-01722.

    Defaults are deliberately NON-real sentinels (operator, 2026-06-10): a qty/amt of
    0 or a blank string could be a VALID value, so a NULL must be replaced by an
    out-of-domain marker (-999 / '-999' / a sentinel date) that is unambiguously
    "was NULL", never mistaken for real data."""
    d = (dtype or "").strip().upper()
    if not d:
        return None
    if d.startswith("TIMESTAMP"):
        return f"NVL({val}, TIMESTAMP '1900-01-01 00:00:00')"
    if d == "DATE":
        return f"NVL({val}, DATE '1900-01-01')"
    if d in ("NUMBER", "FLOAT", "INTEGER", "INT", "DECIMAL", "NUMERIC",
             "BINARY_FLOAT", "BINARY_DOUBLE", "SMALLINT"):
        return f"NVL({val}, -999)"
    if d in ("VARCHAR2", "VARCHAR", "CHAR", "NVARCHAR2", "NCHAR"):
        return f"NVL(TO_CHAR({val}), '-999')"
    if d in ("CLOB", "NCLOB"):
        return f"NVL({val}, '-999')"
    return None


def _wrap_projection_in_nvl(sql: str, prod_owner: str, prod_table: str, type_resolver) -> tuple[str, int]:
    """V14: wrap every projected value in ``NVL(<value>, <type-default>)`` so NULLs
    become a type-appropriate default -> the faithful (NOT NULL-preserving) control
    table never gets ORA-01400, WITHOUT weakening the table to nullable (operator:
    "we don't fit tests to results"). The default per the TARGET column's data type
    (``type_resolver(owner, table, col)`` -- DB-backed, caller-supplied). No-op
    without a resolver, on already-NVL'd values, or where the type is unknown.
    Returns (sql, wrapped_count)."""
    if type_resolver is None:
        return sql, 0
    try:
        work = sql.rstrip()
        trailing = ""
        if work.endswith(";"):
            work, trailing = work[:-1].rstrip(), ";"
        m = re.search(r"\bINSERT\s+INTO\s+" + _IDENT_RE + r"\." + _IDENT_RE + r"\s*\(", work, re.I)
        if not m:
            return sql, 0
        p = work.find("(", m.end() - 1)
        depth, col_end = 0, -1
        for i in range(p, len(work)):
            if work[i] == "(":
                depth += 1
            elif work[i] == ")":
                depth -= 1
                if depth == 0:
                    col_end = i
                    break
        if col_end < 0:
            return sql, 0
        ms = re.search(r"\bSELECT\b", work[col_end + 1:], re.I)
        if not ms:
            return sql, 0
        proj_start = col_end + 1 + ms.end()
        proj_and_from = work[proj_start:]
        from_at = None
        for idx, ch in _scan_top_level(proj_and_from):
            if ch in "Ff" and re.match(r"FROM\b", proj_and_from[idx:idx + 5], re.I):
                from_at = idx
                break
        if from_at is None:
            return sql, 0
        projection = proj_and_from[:from_at]
        head = work[:proj_start]            # INSERT INTO tgt (cols) ... SELECT [hint]
        tail = proj_and_from[from_at:]       # FROM ...
        # split projection on top-level commas
        exprs, start = [], 0
        for idx, ch in _scan_top_level(projection):
            if ch == ",":
                exprs.append(projection[start:idx])
                start = idx + 1
        exprs.append(projection[start:])
        wrapped, count = [], 0
        for expr in exprs:
            # last top-level ' AS '
            last = None
            for i, ch in _scan_top_level(expr):
                if expr[i:i + 4].upper() == " AS ":
                    last = i
            if last is None:
                wrapped.append(expr)
                continue
            val, out_col = expr[:last].strip(), expr[last + 4:].strip()
            if not val.upper().startswith("NVL("):
                w = _nvl_wrap_expr(val, type_resolver(prod_owner, prod_table, out_col))
                if w:
                    val = w
                    count += 1
            wrapped.append(f"\n       {val} AS {out_col}")
        if count == 0:
            return sql, 0
        return head + ",".join(wrapped) + "\n" + tail + trailing, count
    except Exception:  # noqa: BLE001 -- never break the build
        return sql, 0


# Operator-specified parallel hints (2026-06-10): DML on INSERT/MERGE, QUERY on SELECT.
_PAR_QUERY = "/*+ PARALLEL(DEFAULT) ENABLE_PARALLEL_QUERY */"
_PAR_DML = "/*+ PARALLEL(DEFAULT) ENABLE_PARALLEL_DML */"


def _inject_insert_dml_hint(sql: str) -> str:
    """Add the parallel-DML hint to the INSERT/MERGE keyword (operator convention).
    Run AFTER the control-schema retarget so its `INSERT INTO` regex still matches.
    No-op if already hinted or no INSERT/MERGE INTO is found."""
    m = re.search(r"\b(INSERT|MERGE)\s+INTO\b", sql, re.I)
    if not m:
        return sql
    kw_end = m.start() + len(m.group(1))
    if sql[kw_end:kw_end + 24].lstrip().startswith("/*+"):  # already hinted
        return sql
    return sql[:kw_end] + " " + _PAR_DML + sql[kw_end:]


def _inject_parallel_hint(sql: str, degree=None) -> str:
    """Add the parallel-QUERY hint to the INSERT's SELECT so Oracle parallelises the
    scan/join. No-op if the SELECT is already hinted or no INSERT...SELECT is found.
    Affects EXECUTION DOP, not parse time."""
    hint = _PAR_QUERY
    m = re.search(r"\)\s*SELECT\b", sql, re.I)
    if not m:
        return sql
    pos = m.end()
    if sql[pos:pos + 24].lstrip().startswith("/*+"):  # already hinted
        return sql
    return sql[:pos] + " " + hint + sql[pos:]


# V9: when v18 emits a wide projection over many joins (AVY: 369 columns over 110
# joins), Oracle cannot plan the single INSERT...SELECT -- EXPLAIN + capped INSERT
# both time out. Probe (tools/v9_parse_wall_probe2) proved the wall is the
# projection-over-join-graph, not the joins (NO_ELIMINATE_OJ + trivial projection
# plans the full 110-join graph in 3.5s). The cure (ODI's staging principle) is to
# separate the join from the projection: stage the JOIN result (raw source columns
# only) in a MATERIALIZE'd CTE, then run the CASE projection over that single flat
# table. Proven on FREEPDB1: EXPLAIN 18.6s / capped execute 35.7s vs >90s timeout.
_STAGE_JOIN_THRESHOLD = 25  # AVY (110) stages; CLOSE (7) / OPEN (6) stay monolithic
_IDENT_RE = r"[A-Za-z0-9_$#]+"
_MAX_ORACLE_IDENT = 128  # 12.2+ identifier limit; stg colname must fit


def _scan_top_level(s: str):
    """Yield (index, char) at paren depth 0, skipping single-quote string literals
    (with '' escape). Used to split a SELECT list / locate the top-level FROM
    without being fooled by a scalar subquery's own FROM or by commas in CASE/func
    argument lists."""
    depth = 0
    in_str = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            yield i, ch
        i += 1


def _stage_projection_over_join(sql: str) -> tuple[str, Optional[int], Optional[str]]:
    """Rewrite a wide-projection-over-many-joins INSERT into a staged
    MATERIALIZE'd-CTE form so Oracle can plan it (V9). Returns
    ``(new_sql, source_col_count, None)`` on success, or
    ``(original_sql, None, skip_reason)`` on ANY uncertainty -- the monolith is
    always a safe fallback (it is what shipped pre-V9), so this never emits SQL it
    is not confident about. No-op below the join threshold."""
    try:
        if len(re.findall(r"\bJOIN\b", sql, re.I)) < _STAGE_JOIN_THRESHOLD:
            return sql, None, "below_threshold"
        work = sql.strip().rstrip(";").rstrip()
        m = re.search(r"\bINSERT\s+INTO\s+(" + _IDENT_RE + r"\." + _IDENT_RE + r")\s*\(", work, re.I)
        if not m:
            return sql, None, "no_insert"
        target = m.group(1)
        # balanced INSERT column list
        p = work.find("(", m.end() - 1)
        depth, col_end = 0, -1
        for i in range(p, len(work)):
            if work[i] == "(":
                depth += 1
            elif work[i] == ")":
                depth -= 1
                if depth == 0:
                    col_end = i
                    break
        if col_end < 0:
            return sql, None, "unbalanced_col_list"
        target_cols = [c.strip() for c in work[p + 1:col_end].split(",") if c.strip()]
        rest = work[col_end + 1:]
        ms = re.search(r"\bSELECT\b", rest, re.I)
        if not ms:
            return sql, None, "no_select"
        proj_and_from = rest[ms.end():]
        from_at = None
        for idx, ch in _scan_top_level(proj_and_from):
            if ch in "Ff" and re.match(r"FROM\b", proj_and_from[idx:idx + 5], re.I):
                from_at = idx
                break
        if from_at is None:
            return sql, None, "no_top_level_from"
        projection = proj_and_from[:from_at].strip()
        from_block = proj_and_from[from_at:].strip()
        # split projection on top-level commas
        exprs, start = [], 0
        for idx, ch in _scan_top_level(projection):
            if ch == ",":
                exprs.append(projection[start:idx].strip())
                start = idx + 1
        exprs.append(projection[start:].strip())
        if len(exprs) != len(target_cols):
            return sql, None, f"col_count_mismatch_{len(target_cols)}_vs_{len(exprs)}"

        # known FROM/JOIN aliases (base + each JOIN's alias), longest-first
        aliases = []
        fm = re.search(r"\bFROM\s+" + _IDENT_RE + r"\." + _IDENT_RE + r"\s+(" + _IDENT_RE + r")", from_block, re.I)
        if fm:
            aliases.append(fm.group(1))
        for jm in re.finditer(r"\bJOIN\s+" + _IDENT_RE + r"\." + _IDENT_RE + r"\s+(" + _IDENT_RE + r")\b",
                              from_block, re.I):
            aliases.append(jm.group(1))
        seen, uniq = set(), []
        for a in aliases:
            if a.upper() not in seen:
                seen.add(a.upper())
                uniq.append(a)
        if not uniq:
            return sql, None, "no_aliases"
        aliases = sorted(uniq, key=len, reverse=True)
        ref_re = re.compile(r"(?<![.\w])(" + "|".join(re.escape(a) for a in aliases) + r")\.(" + _IDENT_RE + r")",
                            re.IGNORECASE)

        refs: Dict[tuple, str] = {}

        def _sub(mm):
            a, c = mm.group(1).upper(), mm.group(2).upper()
            name = f"{a}__{c}"
            refs[(a, c)] = name
            return f"stg.{name}"

        rebased = []
        for expr in exprs:
            # split value-expr from output alias at the LAST top-level ' AS '
            last = None
            for i, ch in _scan_top_level(expr):
                if expr[i:i + 4].upper() == " AS ":
                    last = i
            if last is not None:
                val, out_col = expr[:last].strip(), expr[last + 4:].strip()
            else:
                toks = expr.strip().split()
                val, out_col = expr, (toks[-1] if toks else expr)
            rebased.append((ref_re.sub(_sub, val), out_col))

        # guard: every known-alias ref in the OUTER projection must now be stg.*
        for val, _oc in rebased:
            if ref_re.search(val):
                return sql, None, "unrebased_ref"
        # guard: stg identifiers fit Oracle's limit
        if any(len(name) > _MAX_ORACLE_IDENT for name in refs.values()):
            return sql, None, "identifier_too_long"
        if not refs:
            return sql, None, "no_source_refs"

        stg_cols = ",\n           ".join(f"{a}.{c} AS {name}" for (a, c), name in sorted(refs.items()))
        proj = ",\n       ".join(f"{v} AS {oc}" for v, oc in rebased)
        staged = (
            f"INSERT INTO {target} (\n    " + ",\n    ".join(target_cols) + "\n)\n"
            f"WITH stg AS (\n    SELECT /*+ MATERIALIZE PARALLEL(DEFAULT) ENABLE_PARALLEL_QUERY */"
            f"\n           {stg_cols}\n    {from_block}\n)\n"
            f"SELECT {_PAR_QUERY}\n       {proj}\nFROM stg"
        )
        return staged, len(refs), None
    except Exception as exc:  # noqa: BLE001 -- never break the build; fall back to monolith
        return sql, None, f"exception_{type(exc).__name__}"


def build_v18_insert_to_dir(
    drd_path: Path,
    out_dir: Path,
    *,
    target_schema: str,
    target_table: str,
    profile: str = "auto",
    schema_kb: Optional[Path] = None,
    control_schema: Optional[str] = None,
    parallel: bool = True,
    varchar_len_resolver=None,
    target_type_resolver=None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Run the vendored v18 insert builder and return its result.

    All paths are server-controlled by the caller; nothing here comes from an
    HTTP body except ``target_schema`` / ``target_table`` / ``profile`` (plain
    identifiers passed as argv, never shell-interpolated).

    ``control_schema`` (optional): when given, the generated INSERT is retargeted
    to ``<control_schema>.<table>`` (the user's own control table). Driven by the
    same config as the rest of the control-table flow; NOT hardcoded.

    Returns a dict with: engine, generated_sql, returncode, stub_columns,
    stub_count, business_stub_columns (real unmapped business cols only),
    null_per_drd_columns (V4: NULLs the DRD itself maps to NULL / unresolvable-source
    DRD bugs -> correctly NULL, NOT defects), audit_stub_columns, hardcode_gate,
    hardcode_gate_failed, target (effective), production_target, control_schema,
    on_alias_fixes (V7), join_reorder (V8), parallel_hint, staged (V9 bool),
    stage_source_cols (staged CTE source-column count or None),
    stage_skip_reason (why staging was skipped, or None when it staged).

    Raises V18BuildError if the tool is missing, times out, fails to start, or
    produces no INSERT statement (fail-loud -- never returns a stub silently).
    """
    if not _INSERT_SCRIPT.exists():
        raise V18BuildError(
            "v18 insert builder is not available in this deployment "
            "(expected under temp files/New folder/drd_odi_insert_universal_tool_v18_0)."
        )
    if not str(target_table).strip():
        raise V18BuildError("v18 build requires a target table.")
    if not str(target_schema).strip():
        # v18 emits INSERT INTO schema.table; without a schema the SQL gate
        # rejects it. Fail loud with guidance rather than producing junk.
        raise V18BuildError(
            "v18 build requires a target schema (owner) so it can emit a "
            "schema-qualified INSERT INTO owner.table."
        )

    kb = Path(schema_kb) if schema_kb else _DEFAULT_SCHEMA_KB
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ABSOLUTE paths only. The v18 tool materializes its engine into
    # <out>/.generated_profile_engine/ and resolves --xlsx/--schema-kb relative to
    # THAT directory; a relative input path is silently joined onto the engine dir
    # and "not found" -> rc=2, no INSERT (looks like flaky builds). Resolving here
    # makes every caller robust regardless of cwd. (out_dir is the subprocess's own
    # output target -- resolve it too so the engine's join base is stable.)
    drd_abs = str(Path(drd_path).resolve())
    out_abs = str(out_dir.resolve())
    kb_abs = str(kb.resolve())

    cmd: List[str] = [
        sys.executable, "-B", str(_INSERT_SCRIPT),
        "--xlsx", drd_abs,
        "--out", out_abs,
        "--profile", (profile or "auto"),
        "--target-schema", str(target_schema).strip(),
        "--target-table", str(target_table).strip(),
        "--schema-kb", kb_abs,
    ]
    if _RESOLUTION_PROFILE.exists():
        cmd += ["--resolution-profile", str(_RESOLUTION_PROFILE)]
    cmd += ["--quiet"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_INSERT_SCRIPT.parent),  # so sibling imports (profile_engine_renderer) resolve
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise V18BuildError(f"v18 build timed out after {timeout_s}s.") from exc
    except (FileNotFoundError, OSError) as exc:
        raise V18BuildError(f"v18 build could not start: {type(exc).__name__}.") from exc

    gen = out_dir / "generated_insert_select_candidate.sql"
    sql = gen.read_text(encoding="utf-8") if gen.exists() else ""
    if "INSERT INTO" not in sql.upper():
        # Fail loud (matches build-v54 contract). Do NOT leak the temp path.
        raise V18BuildError(
            "v18 builder produced no INSERT (the DRD layout/target could not be "
            "resolved; check sheet/header/columns, --profile, or the target owner)."
        )

    # Normalize the trailing statement terminator ONCE: v18 ends the last JOIN line
    # with " ;", which otherwise hides that join from the ON-1=1 join transforms
    # (V11/V12) and leaves a stray ';' mid-line after edits. Strip here; re-append at
    # the very end so the emitted SQL still terminates cleanly.
    _stripped = sql.rstrip()
    _had_semi = _stripped.endswith(";")
    if _had_semi:
        sql = _stripped[:-1].rstrip()

    # Fix v18's alias-in-ON ORA-00904 (SELECT alias used in a JOIN ON predicate).
    # Done on the raw SQL before any retarget (ON clauses are unaffected by retarget).
    sql, on_alias_fixes = _fix_alias_in_on(sql)
    # Then fix forward-referenced JOIN aliases (reorder joins by ON-dependency).
    # After the alias-in-ON fix so dependencies reflect the inlined source columns.
    sql, join_reorder = _reorder_joins_by_dependency(sql)
    # V11: drop unreferenced ON-1=1 cross-joins (v18 over-self-joins; pure cartesian
    # multipliers -> removed for correctness + to kill the row/parse explosion).
    sql, dropped_cross_joins = _drop_unreferenced_cross_joins(sql)
    # V12: if v18 chose a base table the projection never uses and self-joined the
    # REAL source ON 1=1 (IMP_OTSND: 1:1 DRD over-generated as 142 cross-joins),
    # promote the real source to the base + drop the cartesian joins (table-level,
    # not alias-level). No-op when the base is used / any join has a real key.
    sql, promoted_base = _promote_real_base(sql)
    # V10: widen bare/INNER joins to LEFT (fact-load grain preservation; an INNER
    # dimension lookup silently drops fact rows -> 0-row loads). Done before staging
    # so the staged join block carries the corrected join types.
    sql, widened_inner_joins = _widen_inner_to_left(sql)
    # V13: coerce NUMBER = VARCHAR join predicates to LPAD(TO_CHAR(num), len, '0') =
    # varchar (robust + leading-zero-safe). Needs column types (KB) + the VARCHAR
    # length (DB-backed resolver supplied by the caller); no-op without a resolver.
    coerced_joins: List[str] = []
    if varchar_len_resolver is not None:
        try:
            from app.sql_model.static_validator import KBLookup  # lazy
            _kb = KBLookup(kb) if Path(kb).exists() else None
            sql, coerced_joins = _coerce_number_varchar_joins(sql, _kb, varchar_len_resolver)
        except Exception:  # noqa: BLE001 -- never break the build over coercion
            coerced_joins = []
    # V14: NVL-wrap every projected value with a type-appropriate default so the
    # faithful (NOT NULL) control table never gets ORA-01400 -- without weakening the
    # schema. Done before staging so the staged projection carries the NVL wrappers.
    nvl_wrapped = 0
    if target_type_resolver is not None:
        sql, nvl_wrapped = _wrap_projection_in_nvl(
            sql, str(target_schema).strip(), str(target_table).strip(), target_type_resolver)

    # Optional control-schema retarget. The same config the rest of the
    # control-table flow uses (request `control_schema`, settings "Default
    # Control Schema") -- NOT hardcoded. v18 emits the production owner; when a
    # control schema is given we retarget INSERT INTO <owner>.<table> ->
    # <control_schema>.<table> so the row lands in the user's own control table
    # (where they hold full privileges; no GRANT/DBA needed). The SELECT side is
    # untouched. Empty/None => keep the production target (back-compat). Done
    # BEFORE V9 staging so the staged form carries the final INSERT target.
    production_target = f"{str(target_schema).strip()}.{str(target_table).strip()}"
    effective_target = production_target
    cs = (control_schema or "").strip()
    if cs:
        sql = re.sub(
            r"(INSERT\s+INTO\s+)[A-Za-z0-9_$#]+(\s*\.\s*[A-Za-z0-9_$#]+)",
            lambda m: m.group(1) + cs + m.group(2),
            sql,
            count=1,
            flags=re.I,
        )
        effective_target = f"{cs}.{str(target_table).strip()}"

    # V9: stage wide-projection-over-many-joins into a MATERIALIZE'd CTE so Oracle
    # can plan it (AVY: 110 joins). Self-guarded -- returns the monolith unchanged
    # on any uncertainty or below the join threshold (CLOSE/OPEN untouched). When
    # it stages, PARALLEL is already on BOTH selects, so skip the simple hint.
    staged_sql, stage_source_cols, stage_skip = _stage_projection_over_join(sql)
    staged = stage_skip is None
    if staged:
        sql = staged_sql
    elif parallel:
        # Monolith path: parallel-QUERY hint on the single SELECT (execution DOP).
        sql = _inject_parallel_hint(sql)
    # parallel-DML hint on the INSERT keyword (both staged + monolith).
    if parallel:
        sql = _inject_insert_dml_hint(sql)

    # re-append the terminator stripped above so the emitted SQL ends cleanly
    if _had_semi:
        sql = sql.rstrip() + ";\n"

    gate_report: Dict[str, Any] = {}
    gate_path = out_dir / "hardcode_gate_report.json"
    if gate_path.exists():
        try:
            gate_report = json.loads(gate_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            gate_report = {"_parse_error": True}

    # NULL-stub extraction + classification (Gate V2 acts on business stubs).
    all_stubs = [c.upper() for c in re.findall(r"NULL\s+AS\s+([A-Z0-9_$#]+)", sql, re.I)]
    audit_stubs = [c for c in all_stubs if c in _AUDIT_STUB_COLUMNS]
    non_audit = [c for c in all_stubs if c not in _AUDIT_STUB_COLUMNS]
    # V4: reclassify NULLs the DRD itself maps to NULL (or whose DRD source is a bug
    # -> correctly NULL) out of "business stubs". GENERIC -- driven by the v18 tool's
    # own implementation_map.csv (drd_expression), no hardcoded column names. A
    # business stub is now ONLY a column the DRD intended to populate (real source)
    # that v18 left NULL. Unknown columns stay as business stubs (never hide a NULL).
    impl_status = _read_impl_null_status(out_dir)
    null_per_drd = [c for c in non_audit if impl_status.get(c) == "null_per_drd"]
    business_stubs = [c for c in non_audit if impl_status.get(c) != "null_per_drd"]

    # rc != 0 with a real INSERT present == the v18 hardcode gate flagged the
    # package (code-quality), not a bad SQL. Surface it; do not fail the build.
    hardcode_gate_failed = bool(proc.returncode != 0 and sql)

    return {
        "engine": "v18-insert-builder",
        "generated_sql": sql,
        "returncode": proc.returncode,
        "stub_columns": all_stubs,
        "stub_count": len(all_stubs),
        "business_stub_columns": business_stubs,
        "audit_stub_columns": audit_stubs,
        "null_per_drd_columns": null_per_drd,
        "hardcode_gate": gate_report,
        "hardcode_gate_failed": hardcode_gate_failed,
        "target": effective_target,
        "production_target": production_target,
        "control_schema": cs or None,
        "on_alias_fixes": on_alias_fixes,
        "join_reorder": join_reorder,
        "widened_inner_joins": widened_inner_joins,
        "dropped_cross_joins": dropped_cross_joins,
        "promoted_base": promoted_base,
        "coerced_joins": coerced_joins,
        "nvl_wrapped": nvl_wrapped,
        "parallel_hint": bool(parallel),
        "staged": staged,
        "stage_source_cols": stage_source_cols,
        "stage_skip_reason": stage_skip,
    }
