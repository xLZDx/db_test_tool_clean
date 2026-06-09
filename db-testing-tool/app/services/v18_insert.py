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


def _inject_parallel_hint(sql: str, degree=None) -> str:
    """Add a PARALLEL hint to the INSERT's SELECT so Oracle uses all CPU on the
    scan/join (matches the tool's existing PARALLEL(DEFAULT) convention). No-op if
    the SELECT is already hinted or no INSERT...SELECT is found. Affects EXECUTION
    DOP, not parse time."""
    hint = f"/*+ PARALLEL({degree}) */" if degree else "/*+ PARALLEL */"
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
            f"WITH stg AS (\n    SELECT /*+ MATERIALIZE PARALLEL */\n           {stg_cols}\n    {from_block}\n)\n"
            f"SELECT /*+ PARALLEL */\n       {proj}\nFROM stg"
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
    stub_count, business_stub_columns, audit_stub_columns, hardcode_gate,
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

    cmd: List[str] = [
        sys.executable, "-B", str(_INSERT_SCRIPT),
        "--xlsx", str(drd_path),
        "--out", str(out_dir),
        "--profile", (profile or "auto"),
        "--target-schema", str(target_schema).strip(),
        "--target-table", str(target_table).strip(),
        "--schema-kb", str(kb),
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

    # Fix v18's alias-in-ON ORA-00904 (SELECT alias used in a JOIN ON predicate).
    # Done on the raw SQL before any retarget (ON clauses are unaffected by retarget).
    sql, on_alias_fixes = _fix_alias_in_on(sql)
    # Then fix forward-referenced JOIN aliases (reorder joins by ON-dependency).
    # After the alias-in-ON fix so dependencies reflect the inlined source columns.
    sql, join_reorder = _reorder_joins_by_dependency(sql)

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
        # Monolith path: PARALLEL hint on the single SELECT (execution DOP).
        sql = _inject_parallel_hint(sql)

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
    business_stubs = [c for c in all_stubs if c not in _AUDIT_STUB_COLUMNS]

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
        "hardcode_gate": gate_report,
        "hardcode_gate_failed": hardcode_gate_failed,
        "target": effective_target,
        "production_target": production_target,
        "control_schema": cs or None,
        "on_alias_fixes": on_alias_fixes,
        "join_reorder": join_reorder,
        "parallel_hint": bool(parallel),
        "staged": staged,
        "stage_source_cols": stage_source_cols,
        "stage_skip_reason": stage_skip,
    }
