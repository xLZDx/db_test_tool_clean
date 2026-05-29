"""DRD-first Oracle INSERT emitter.

Goal: produce an INSERT that covers every target column the DRD declares,
using ODI as a reference where it agrees with DRD and DRD's col-AD spec
(joins + transformation rule + ETL Notes blocks) where ODI deviates.

Operator-locked invariants:
  * Generic - never hard-codes table / column / schema / block names.
  * Coverage - every target column receives a source expression OR a typed
    fallback (NULL / literal); the emitter never silently drops a column.
  * DRD-first - when DRD col-AD encodes a specific join graph or an ETL Notes
    block reference, those win over the ODI projection.  ODI is only the
    fallback example where DRD says nothing useful.
  * Deterministic - same inputs => byte-identical output.

The emitter accepts the already-parsed comparator output so we can route by
verdict per column without re-running the trace.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.sql_model.comparator import (
    ComparisonResult,
)
from app.sql_model.drd_ad_parser import (
    DrdAdJoin,
    DrdAdRule,
    parse_drd_ad,
)
from app.sql_model.etl_block_index import EtlBlockIndex
from app.sql_model.types import ComparisonVerdict, MismatchKind, ODIModel, norm


# Module-level ETL defaults are bound LATER (after the drd_rules import) --
# they live in the shared rule engine.  See ``_ETL_DEFAULTS`` re-export below.


# ── Per-column source decision ───────────────────────────────────────────────

@dataclass
class _ColumnPlan:
    """The resolved source for one target column."""
    target_col: str
    source_expr: str                 # the SELECT-list expression (without trailing AS)
    provenance: str                  # one of DRD_AD, ETL_BLOCK, ODI, ETL_DEFAULT, FALLBACK
    needs_join: Optional["_JoinNeed"] = None
    needs_cte: Optional["_CteNeed"] = None
    notes: str = ""


@dataclass
class _JoinNeed:
    """A LEFT JOIN <fq_table> <alias> ON <predicate(s)> requirement."""
    fq_table: str                    # SCHEMA.TABLE (UPPER)
    alias: str                       # canonical alias (assigned at planning time)
    on_predicates: List[str]         # raw predicates, in order

    @property
    def key(self) -> Tuple[str, str, Tuple[str, ...]]:
        return (self.fq_table, self.alias, tuple(self.on_predicates))


@dataclass
class _CteNeed:
    """A WITH <name> AS (<body>) requirement (for ETL Notes block resolution).

    ``is_sql`` is True when the body looks like executable SQL; False when it's
    descriptive prose (DRD ETL-Notes blocks are typically prose).  When False,
    the body is emitted as a header comment instead of a CTE and columns that
    reference the block fall back to ``NULL`` with a pointer to that comment.
    """
    name: str                        # CTE alias (lower-case-safe)
    body: str                        # body of the block (SQL or prose)
    is_sql: bool = False             # True => emit as CTE; False => emit as comment


# ── Generic identifier / alias helpers ────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Z][A-Z0-9_$#]*$", re.IGNORECASE)
_BARE_REF_RE = re.compile(r"^[A-Z][A-Z0-9_$#]*\.[A-Z][A-Z0-9_$#]*$", re.IGNORECASE)
_NOT_AN_IDENT_RE = re.compile(r"[^A-Za-z0-9_$#]")


def _safe_ident(name: str) -> str:
    """Strip non-identifier characters, upper-case."""
    if not name:
        return ""
    return _NOT_AN_IDENT_RE.sub("_", str(name)).strip("_").upper()


def _alias_from_table(fq_table: str, used: set) -> str:
    """Build a unique short alias from a fully-qualified table.

    Strategy: take initials of underscore-separated chunks of the table name,
    extend with a numeric suffix on collision.  Generic — purely structural.
    """
    bare = fq_table.split(".")[-1]
    chunks = [c for c in re.split(r"[_$]", bare) if c]
    base = "".join(c[0] for c in chunks).lower() or "x"
    cand = base
    n = 1
    while cand.upper() in used:
        n += 1
        cand = f"{base}{n}"
    used.add(cand.upper())
    return cand


# Delegation: the applicable-only / discriminator detectors live in the shared
# rule engine ``app.sql_model.drd_rules`` so the comparator uses the SAME logic.
from app.sql_model.drd_rules import (
    DEFAULT_ETL_COLUMN_VALUES,
    compose_case_when_expr,
    compose_exists_case_expr,
    extract_applicable_only_code,
    extract_exists_derived_flag,
    extract_t_alias_hint,
    find_discriminator_for_code,
    is_unimplementable_prose_rule,
)

# Generic system-managed column defaults (callers may override via the
# ``etl_column_defaults`` parameter of ``emit_insert_drd_first``).
_ETL_DEFAULTS: Dict[str, str] = dict(DEFAULT_ETL_COLUMN_VALUES)


# ── SQL-vs-prose heuristic ────────────────────────────────────────────────────
#
# DRD ETL-Notes blocks vary: some are real SQL fragments (SELECT/FROM/JOIN),
# others are human prose describing the rule.  Oracle CTEs can only hold the
# former.  This detector is content-agnostic -- counts SQL anchors vs total
# non-blank tokens.

_SQL_ANCHOR_RE = re.compile(
    r"\b(select|from\s+[A-Za-z]|left\s+join|inner\s+join|where|"
    r"group\s+by|order\s+by|having|union|with\s+[A-Za-z]+\s+as)\b",
    re.IGNORECASE,
)
_SQL_REQUIRED_HEADS = re.compile(
    r"^\s*(select|with|--|/\*)",
    re.IGNORECASE,
)


def _body_looks_like_sql(body: str) -> bool:
    """Generic: True if body parses as executable Oracle SQL (or at least its
    first non-blank line starts like one).  Conservative -- when in doubt the
    body is treated as prose so we never inject invalid SQL into a CTE."""
    if not body:
        return False
    text = body.strip()
    if not text:
        return False
    # First non-blank line must start like SQL.
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if not _SQL_REQUIRED_HEADS.match(first_line):
        return False
    # And at least one strong SQL anchor must occur somewhere.
    return bool(_SQL_ANCHOR_RE.search(text))


# ── ODI projection extraction (used as fallback example) ─────────────────────

_PROJ_TAIL_RE_CACHE: Dict[str, re.Pattern] = {}


def _proj_tail_re(col: str) -> re.Pattern:
    key = col.upper()
    pat = _PROJ_TAIL_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(
            r"(?:^|\n|,)\s*([^,\n]+?)\s+(?:AS\s+)?"
            + re.escape(key)
            + r"\s*(?=,|\n|$)",
            re.IGNORECASE,
        )
        _PROJ_TAIL_RE_CACHE[key] = pat
    return pat


def _odi_projection_for(model: ODIModel, col: str) -> Optional[str]:
    """Return the raw ODI source expression for *col*, scanning STAGING steps
    last-first and then the MERGE block.  Pure text search, generic."""
    if not col:
        return None
    pat = _proj_tail_re(col)
    candidates: List[Tuple[int, str]] = []
    for step in model.staging_steps:
        sql = step.select_sql or ""
        m = pat.search(sql)
        if m is not None:
            expr = m.group(1).strip()
            if expr.upper() != col.upper():
                candidates.append((step.step_id, expr))
    if candidates:
        # Return the deepest staging step's projection (highest step_id)
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    # Fall back to MERGE block
    fs = model.final_select_sql or ""
    if fs:
        m = pat.search(fs)
        if m is not None:
            expr = m.group(1).strip()
            if expr.upper() != col.upper():
                return expr
    return None


# ── Base table detection ──────────────────────────────────────────────────────

def _detect_base_table(analysis_rows: List[Dict[str, Any]]) -> Tuple[str, str]:
    """Detect the most-referenced source table (fq, alias).

    Generic: counts non-lookup source_table occurrences in analysis_rows;
    the most frequent fully-qualified table becomes the FROM target.
    """
    from collections import Counter
    counts: Counter = Counter()
    for r in analysis_rows:
        st = (r.get("source_table") or "").strip().upper().split("\n")[0].split(",")[0].strip()
        ss = (r.get("source_schema") or "").strip().upper()
        if not st:
            continue
        if "." in st:
            fq = st
        elif ss:
            fq = f"{ss}.{st}"
        else:
            fq = st
        counts[fq] += 1
    if not counts:
        return ("UNKNOWN.UNKNOWN", "T")
    fq, _ = counts.most_common(1)[0]
    # Derive an alias from the bare table name
    bare = fq.split(".")[-1]
    chunks = [c for c in re.split(r"[_$]", bare) if c]
    alias = "".join(c[0] for c in chunks).lower() or "t"
    return (fq, alias)


# ── Per-row planning ──────────────────────────────────────────────────────────

def _is_simple_literal(expr: str) -> bool:
    if not expr:
        return False
    e = expr.strip().rstrip(",")
    return bool(
        re.match(
            r"^(?:SYSDATE|SYSTIMESTAMP|NULL|TRUNC\(\s*SYSDATE\s*\)|'[^']*'|\d+(?:\.\d+)?)$",
            e,
            re.IGNORECASE,
        )
    )


def _is_simple_ref(expr: str) -> bool:
    if not expr:
        return False
    return bool(_BARE_REF_RE.match(expr.strip()))


# ── Canonical-alias pass + predicate rewriter ────────────────────────────────

def _build_canonical_aliases(
    odi_model: Optional[ODIModel],
    analysis_rows: List[Dict[str, Any]],
    base_fq: str,
    base_alias: str,
) -> Tuple[Dict[str, str], set]:
    """One alias per physical (schema, table).  ODI's alias wins where present;
    otherwise an initials-based alias is generated.  Returns (mapping, used)."""
    canonical: Dict[str, str] = {base_fq.upper(): base_alias}
    used: set = {base_alias.upper()}

    # Prefer ODI's aliases (validated, used in real join predicates)
    if odi_model is not None:
        staging_names = {s.name.upper() for s in odi_model.staging_steps if s.name}
        for step in odi_model.staging_steps:
            for binding in step.source_bindings:
                fq = binding.ref.fq.upper()
                if not fq or fq in canonical or fq.split(".")[-1] in staging_names:
                    continue
                a = binding.alias.upper().lower()
                # Avoid collisions with already-used aliases
                cand = a
                n = 1
                while cand.upper() in used:
                    n += 1
                    cand = f"{a}{n}"
                canonical[fq] = cand
                used.add(cand.upper())

    # Fill in any remaining tables seen in row.source_table or DRD AD joins
    for r in analysis_rows:
        for fq in _row_source_fqs(r):
            if fq in canonical:
                continue
            cand = _alias_from_table(fq, used)
            canonical[fq] = cand
            # used was updated by _alias_from_table
    return canonical, used


def _row_source_fqs(row: Dict[str, Any]) -> List[str]:
    """Yield every fully-qualified physical table this row references
    (row.source_table + every JOIN inside row.transformation)."""
    out: List[str] = []
    st = (row.get("source_table") or "").strip().upper().split("\n")[0].split(",")[0].strip()
    ss = (row.get("source_schema") or "").strip().upper()
    if st:
        fq = f"{ss}.{st}" if ss and "." not in st else st
        if fq.upper() not in out:
            out.append(fq.upper())
    ad = parse_drd_ad(row.get("transformation") or "")
    for j in ad.joins:
        if j.fq_table.upper() not in out:
            out.append(j.fq_table.upper())
    return out


_IDENT_DOT_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_$#]*)\.([A-Za-z][A-Za-z0-9_$#]*)\b")


def _rewrite_predicate(
    on_sql: str,
    drd_alias_to_fq: Dict[str, str],
    fq_to_canonical: Dict[str, str],
) -> str:
    """Rewrite ``<drd_alias>.<col>`` -> ``<canonical_alias>.<col>``.

    ``drd_alias_to_fq`` maps DRD-side aliases (as the author wrote them in the
    col-AD cell) to fq table names; ``fq_to_canonical`` maps fq -> canonical
    alias used by the emitter.  Unknown aliases are left intact -- operator
    will see them and can patch.
    """
    if not on_sql:
        return on_sql

    def _sub(m: re.Match) -> str:
        drd_alias = m.group(1)
        col = m.group(2)
        fq = drd_alias_to_fq.get(drd_alias.upper())
        if not fq:
            return m.group(0)
        canonical = fq_to_canonical.get(fq.upper())
        if not canonical:
            return m.group(0)
        return f"{canonical}.{col}"

    return _IDENT_DOT_RE.sub(_sub, on_sql)


# ── Multi-role fq detection (lookup tables with N distinct ODI aliases) ─────
#
# Some physical tables (e.g. generic lookup / code-value tables) are joined
# more than once in the same ODI staging chain, each time with a different
# alias and a different ON predicate -- one alias per logical "role".  When
# the DRD-author reuses ONE alias across all such rows (a common shortcut),
# the naive harvest collapses every row's ON predicate onto the same alias
# under AND, producing an impossible compound JOIN that matches zero rows
# in production (the silent NULL bug).
#
# This detector is content-agnostic: it surfaces ANY fq that appears with
# >= 2 distinct aliases in ANY staging step.  Generic for any DRD / table.

def _collect_multi_role_fqs(
    odi_model: Optional[ODIModel],
) -> Dict[str, List[Tuple[str, str]]]:
    """Return ``{fq_table_upper: [(odi_alias_upper, on_sql_normalized), ...]}``
    for every fq that ODI joins under more than one distinct alias.  Each
    tuple captures one ODI ROLE for that fq.  Single-role fqs are excluded.
    Generic -- no hardcoded names; works for any schema."""
    if odi_model is None:
        return {}
    fq_to_roles: Dict[str, List[Tuple[str, str]]] = {}
    seen_pairs: Dict[str, set] = {}
    for step in odi_model.staging_steps:
        for edge in step.join_graph:
            ref = edge.joined.ref if edge.joined else None
            if ref is None:
                continue
            fq = ref.fq.upper() if ref.fq else ""
            alias = (edge.joined.alias or "").upper()
            if not fq or not alias:
                continue
            stripped_on = _strip_oracle_outer_marker(edge.on_sql or "")
            pair = (alias, stripped_on)
            seen_pairs.setdefault(fq, set())
            if pair in seen_pairs[fq]:
                continue
            seen_pairs[fq].add(pair)
            fq_to_roles.setdefault(fq, []).append(pair)
    # Keep only fqs that have >=2 distinct aliases
    return {
        fq: roles
        for fq, roles in fq_to_roles.items()
        if len({alias for alias, _on in roles}) >= 2
    }


def _match_drd_predicate_to_odi_role(
    drd_predicates: List["DrdAdJoinPredicate"],
    odi_roles: List[Tuple[str, str]],
) -> Optional[str]:
    """Return the ODI alias whose ON predicate matches one of the DRD-author
    predicates (bare-column-pair, alias-insensitive, order-insensitive).
    ``None`` when no ODI role matches.  Pure structural -- generic for any
    column names."""
    from app.sql_model.drd_ad_parser import predicate_matches
    for p in drd_predicates:
        for alias, on_sql in odi_roles:
            if predicate_matches(p, on_sql):
                return alias
    return None


# Common code/name/desc suffixes used by data-architecture teams to derive
# column names from the underlying lookup role.  Stripping them recovers the
# "role root" used to match against ODI alias names.  Generic -- no business
# domain identifiers, just trailing-token shapes.
_TARGET_COL_SUFFIXES = (
    "_CODE", "_DESC", "_DSC", "_NAME", "_NM", "_CD",
    "_ID", "_NO", "_NUM", "_AMT", "_PCT", "_QTY", "_RATE",
    "_DT", "_TS", "_TIME", "_F", "_FLAG", "_IND",
)


def _target_col_role_root(target: str) -> str:
    """Strip trailing semantic suffixes from a target column to recover the
    role-root used to match against ODI alias names.  Returns upper-case.

    Examples (generic):
        ``WIDGET_TP_CD``    -> ``WIDGET_TP``
        ``GADGET_NM``       -> ``GADGET``
        ``ZED_F``           -> ``ZED``
        ``DRVD_TRD_CPCTY_CD`` -> ``DRVD_TRD_CPCTY``
    """
    if not target:
        return ""
    up = target.strip().upper()
    for suf in _TARGET_COL_SUFFIXES:
        if up.endswith(suf) and len(up) > len(suf):
            return up[: -len(suf)]
    return up


def _match_target_to_odi_role(
    target: str,
    odi_roles: List[Tuple[str, str]],
) -> Optional[str]:
    """When a row has no DRD AD join to match, fall back to matching the
    target column's ROLE ROOT against ODI alias names.  Returns the ODI alias
    that best matches.  Pure structural -- no domain names hardcoded.

    Match strategy (in priority order):
      1. ODI alias == role_root  exactly       (DRVD_TRD_CPCTY_CD -> DRVD_TRD_CPCTY)
      2. role_root starts with ODI alias       (e.g. ``WIDGET_TP_DSC`` root ``WIDGET_TP`` matches alias ``WIDGET``)
      3. ODI alias starts with role_root       (alias ``WIDGET_TYPE_LK`` matches root ``WIDGET_TYPE``)
    Returns ``None`` when none of these match.
    """
    root = _target_col_role_root(target)
    if not root:
        return None
    aliases = [alias.upper() for alias, _on in odi_roles]
    if root in aliases:
        return root
    # Try prefix matches (longest first to avoid weak partial collisions)
    candidates = sorted(aliases, key=len, reverse=True)
    for alias in candidates:
        if alias and root.startswith(alias + "_"):
            return alias
    for alias in candidates:
        if alias and alias.startswith(root + "_"):
            return alias
    return None


# ── Alias extraction from ODI projection text ────────────────────────────────
#
# When DRD signals a T-alias hint, we look at ODI's projection expression to
# find which alias the column actually projects from.  Pure pattern detection;
# no hard-coded names.

_ALIAS_DOT_COL_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_$#]*)\.([A-Za-z][A-Za-z0-9_$#]*)\b",
)


def _extract_alias_for_col(odi_expr: str, col: str) -> Optional[str]:
    """Return the alias of a ``<alias>.<col>`` reference inside ``odi_expr``
    where ``<col>`` matches the requested column (case-insensitive).  ``None``
    if no such reference appears.  Pure structural -- no SQL keywords are
    treated as aliases."""
    if not odi_expr or not col:
        return None
    target = col.strip().upper()
    # Reject SQL keywords used as bare prefixes (avoid CASE/SUM/NVL/MAX/etc.)
    _SQL_KW = {
        "CASE", "WHEN", "THEN", "ELSE", "END", "SUM", "MAX", "MIN", "AVG",
        "COUNT", "NVL", "COALESCE", "DECODE", "TO_DATE", "TO_CHAR",
        "TO_NUMBER", "TRIM", "SUBSTR", "CAST", "EXTRACT", "EXISTS", "IN",
        "AND", "OR", "NOT", "NULL", "IS", "FROM", "WHERE", "ON",
    }
    for m in _ALIAS_DOT_COL_RE.finditer(odi_expr):
        alias, ref_col = m.group(1), m.group(2)
        if alias.upper() in _SQL_KW:
            continue
        if ref_col.upper() == target:
            return alias
    return None


_OUTER_JOIN_MARKER_RE = re.compile(r"\s*\(\s*\+\s*\)")


def _strip_oracle_outer_marker(on_sql: str) -> str:
    """Remove the Oracle 9i-style ``(+)`` outer-join markers from an ON clause.

    Mixing ``(+)`` with ANSI ``LEFT JOIN ... ON`` is invalid Oracle syntax --
    the ``(+)`` form is only allowed in WHERE-clause comma-joins.  When we
    re-emit a join as ``LEFT JOIN ... ON``, every ``(+)`` MUST be stripped or
    Oracle will raise ORA-00936 / ORA-25156.
    """
    if not on_sql:
        return on_sql
    return _OUTER_JOIN_MARKER_RE.sub("", on_sql).strip()


def _harvest_odi_joins(
    odi_model: Optional[ODIModel],
    base_fq: str,
    base_alias: str,
    alias_assignments: Dict[str, str],
    used_aliases: set,
) -> "OrderedDict[Tuple[str, str], _JoinNeed]":
    """Walk every staging step's ``join_graph`` and produce a deduplicated
    list of LEFT JOIN clauses.  Operator-locked invariants:
      * use ODI's own validated ON predicates verbatim (never invent JOIN keys)
      * skip joins targeting intermediate staging tables (those are not real)
      * map ODI aliases consistently to our base alias scheme
      * strip Oracle 9i ``(+)`` markers -- they are invalid inside ANSI
        ``LEFT JOIN ... ON`` (operator-locked, 2026-05-29)
    """
    joins: "OrderedDict[Tuple[str, str], _JoinNeed]" = OrderedDict()
    if odi_model is None:
        return joins
    # Build set of staging-step physical names so we can skip self-references.
    staging_names = {s.name.upper() for s in odi_model.staging_steps if s.name}
    for step in odi_model.staging_steps:
        for edge in step.join_graph:
            ref = edge.joined.ref
            fq = ref.fq.upper() if ref else ""
            if not fq or fq in staging_names:
                continue
            # Use ODI's own alias for stability with its ON predicates.
            odi_alias = edge.joined.alias.upper()
            # Self-join handling: when the joined fq matches the base table,
            # only skip if ODI also used the BASE alias for it -- that's a
            # genuine duplicate FROM entry.  When ODI introduces a SECOND
            # alias for the same physical table (e.g. for T2-style related-row
            # joins), keep the join so its ON predicates land in the SQL and
            # the second-reference projection lands on the right alias.
            if fq == base_fq.upper() and odi_alias.upper() == base_alias.upper():
                continue
            key = (fq, odi_alias)
            stripped_on = _strip_oracle_outer_marker(edge.on_sql or "")
            if key in joins:
                # Same alias + same fq -> merge predicates (uniq)
                existing = joins[key]
                if stripped_on and stripped_on not in existing.on_predicates:
                    existing.on_predicates.append(stripped_on)
                continue
            # Only set the alias_assignments entry for this fq if it isn't
            # already mapped.  In particular, when fq == base_fq the base
            # alias must keep priority -- this self-join's alternative alias
            # is only used by the T-alias-hint path in _plan_column.  The
            # alternative alias is still tracked in used_aliases (so future
            # alias generation doesn't collide) and in the joins dict (so the
            # LEFT JOIN clause is emitted).
            if fq not in alias_assignments:
                alias_assignments[fq] = odi_alias
            used_aliases.add(odi_alias)
            joins[key] = _JoinNeed(
                fq_table=fq,
                alias=odi_alias,
                on_predicates=[stripped_on] if stripped_on else [],
            )
    return joins


def _plan_column(
    target: str,
    row: Dict[str, Any],
    comp_result: Optional[ComparisonResult],
    odi_model: Optional[ODIModel],
    base_alias: str,
    base_fq: str,
    alias_assignments: Dict[str, str],
    used_aliases: set,
    cte_assignments: Dict[str, _CteNeed],
    joins_by_key: Optional["OrderedDict[Tuple[str, str], _JoinNeed]"] = None,
    multi_role_fqs: Optional[Dict[str, List[Tuple[str, str]]]] = None,
) -> _ColumnPlan:
    """Resolve the per-column source expression.

    Strategy (operator-locked 2026-05-29):
      * ODI is the structural example -- mirror what its staging chain projects.
      * DRD col-AD ``source_table.source_attribute`` is the physical contract --
        if present, it overrides ODI's intermediate staging pass-through.
      * ETL Notes prose blocks are SUPPLEMENTAL context (operator commentary);
        they NEVER NULL out a column that has a concrete physical source.

    Resolution order:
      0. ETL default for system-managed columns (CRT_DTM, ACTV_F, ...).
      1. ODI's resolved staging chain (via comparator): the deepest concrete
         ``<table>.<col>`` reference.  This is what runs in production.
      2. DRD physical pass-through (row.source_table + row.source_attribute)
         when ODI's projection is just a staging-table pointer (e.g.
         ``AVY_FACT_STEP5_STG_RT.X`` carries no real source info).
      3. NULL last resort with TODO marker.

    ETL block body (when present) attaches as an inline comment annotation so
    the operator sees the supplemental rule alongside the projection.
    """
    def _first_token(s: str) -> str:
        """Take only the first identifier-like token from possibly-multiline cell."""
        if not s:
            return ""
        # Split on newline / comma / whitespace; keep first non-empty token
        for sep in ("\n", ",", " ", "\t"):
            s = s.split(sep)[0]
        return s.strip()

    target = (target or "").strip().upper()
    raw_source_attr = (row.get("source_attribute") or "")
    # Preserve the full raw cell for T-alias-hint detection BEFORE we strip
    # to the first token (which would drop a trailing "(FROM T2)" annotation).
    t_alias_hint = extract_t_alias_hint(raw_source_attr)
    source_attr = _first_token(raw_source_attr).upper()
    source_table = _first_token(row.get("source_table") or "").upper()
    source_schema = _first_token(row.get("source_schema") or "").upper()
    transformation = (row.get("transformation") or "")
    etl_block_ref = (row.get("etl_block_ref") or "").strip().upper()
    etl_block_body = (row.get("etl_block_body") or "").strip()
    etl_note = ""
    if etl_block_ref and etl_block_body:
        etl_note = f" /* see ETL block {etl_block_ref} in header */"

    # Per-row alias overrides (filled by the multi-role harvest below).  Path
    # 1 / Path 2 consult this BEFORE alias_assignments[fq] so projections on
    # multi-role lookup tables land on the row-specific alias.
    row_alias_for_fq: Dict[str, str] = {}

    # Register inline DRD col-AD joins (the "and below logic" the operator
    # wants captured).  Aliases are rewritten to our canonical scheme so the
    # ON predicates stay consistent across rows.
    if joins_by_key is not None:
        ad_rule = parse_drd_ad(row.get("transformation") or "")
        if ad_rule.joins:
            # Build DRD-alias -> fq map for this cell (incl. base table)
            drd_alias_to_fq: Dict[str, str] = {}
            if ad_rule.base_alias and ad_rule.base_table:
                drd_alias_to_fq[ad_rule.base_alias.upper()] = ad_rule.base_table.upper()
            for j in ad_rule.joins:
                drd_alias_to_fq[j.alias.upper()] = j.fq_table.upper()
            for j in ad_rule.joins:
                fq = j.fq_table.upper()
                if fq == base_fq.upper():
                    continue

                # Decide alias: multi-role fqs get a row-specific alias so
                # each row's predicate lives in its OWN JOIN clause.
                is_multi_role = bool(multi_role_fqs) and fq in (multi_role_fqs or {})
                if is_multi_role:
                    # Prefer the ODI role alias whose ON predicate matches THIS
                    # row's predicate -- that's the authoritative role mapping.
                    matched_odi_alias = _match_drd_predicate_to_odi_role(
                        j.predicates, (multi_role_fqs or {}).get(fq, []),
                    )
                    if matched_odi_alias and matched_odi_alias not in used_aliases:
                        canonical = matched_odi_alias.lower()
                        used_aliases.add(matched_odi_alias)
                    elif matched_odi_alias:
                        # Already used elsewhere -- reuse it (its predicates
                        # already match this row's role).
                        canonical = matched_odi_alias.lower()
                    else:
                        # No ODI match -- generate a row-unique alias derived
                        # from the target column root so the role is visible.
                        bare = fq.split(".")[-1].lower()
                        target_root = target.lower().split("_")[0] or "x"
                        candidate = f"{bare[:3]}_{target_root}"
                        n = 1
                        while candidate.upper() in used_aliases:
                            n += 1
                            candidate = f"{bare[:3]}_{target_root}{n}"
                        canonical = candidate
                        used_aliases.add(canonical.upper())
                    row_alias_for_fq[fq] = canonical
                else:
                    # Single-role fq: keep existing canonical alias.
                    canonical = alias_assignments.get(fq)
                    if canonical is None:
                        canonical = _alias_from_table(fq, used_aliases)
                        alias_assignments[fq] = canonical

                # Rewrite ON predicates to canonical aliases.  Strip any
                # Oracle 9i (+) markers -- they are invalid inside ANSI joins.
                # For multi-role we need an extra rewrite of the DRD-author's
                # local alias (e.g. ``cv``) to the new canonical alias so the
                # predicate stays sane.
                drd_local_alias_to_canonical = dict(alias_assignments)
                if is_multi_role:
                    drd_local_alias_to_canonical[fq] = canonical
                rewritten = [
                    _strip_oracle_outer_marker(
                        _rewrite_predicate(p.raw, drd_alias_to_fq, drd_local_alias_to_canonical)
                    )
                    for p in j.predicates if p.raw
                ]
                rewritten = [r for r in rewritten if r]
                key = (fq, canonical.upper())
                if key in joins_by_key:
                    for r in rewritten:
                        if r and r not in joins_by_key[key].on_predicates:
                            joins_by_key[key].on_predicates.append(r)
                else:
                    joins_by_key[key] = _JoinNeed(
                        fq_table=fq, alias=canonical, on_predicates=rewritten,
                    )

    # 0) System / ETL default columns (caller-overridable via row["_etl_defaults"])
    etl_defaults: Dict[str, str] = row.get("_etl_defaults") or _ETL_DEFAULTS
    if target in etl_defaults:
        return _ColumnPlan(
            target_col=target,
            source_expr=etl_defaults[target],
            provenance="ETL_DEFAULT",
            notes="system-managed",
        )

    # 0.5) Generic EXISTS-derived flag (shared rule engine) -- run EARLY
    # because some DRD parsers leak the natural-language rule text into the
    # ``source_attribute`` field, which would otherwise mislead the
    # DRD_PHYSICAL path into projecting ``<TARGET>.<first_word>``.
    exists_spec_early = extract_exists_derived_flag(transformation)
    if exists_spec_early is not None:
        return _ColumnPlan(
            target_col=target,
            source_expr=compose_exists_case_expr(exists_spec_early, else_value="NULL"),
            provenance="DRD_EXISTS_DERIVED_FLAG",
            notes=f"derived flag: EXISTS({exists_spec_early['table']}, "
                  f"set '{exists_spec_early['set_value']}'){etl_note}",
        )

    # Detect "Applicable only for <CODE>" + discover discriminator from the
    # referenced ETL block.  When BOTH are present, wrap the projection in a
    # CASE so it only fires for the named code -- matching ODI's typical
    # ``CASE WHEN <alias>.<col> = '<CODE>' THEN <expr> ELSE NULL END``.
    applicable_code = extract_applicable_only_code(transformation)
    discriminator: Optional[Tuple[str, str]] = None
    if applicable_code:
        # Try the directly-referenced block first, then the global haystack.
        if etl_block_body:
            discriminator = find_discriminator_for_code(etl_block_body, applicable_code)
        if discriminator is None:
            all_text = row.get("_all_etl_text") or ""
            if all_text:
                discriminator = find_discriminator_for_code(all_text, applicable_code)

    def _maybe_case_wrap(expr: str) -> str:
        """Wrap ``expr`` in a CASE for the applicable-only-for code, when we
        have both the code AND a discriminator from the ETL block."""
        if not applicable_code or not discriminator:
            return expr
        d_alias, d_col = discriminator
        # Map the DRD alias to our canonical alias if known
        d_alias_up = d_alias.upper()
        canonical_alias = d_alias  # default: keep DRD alias
        for fq, ca in alias_assignments.items():
            if ca.upper() == d_alias_up:
                canonical_alias = ca
                break
        return compose_case_when_expr((canonical_alias, d_col), applicable_code, expr)

    # 1) ODI's resolved chain: use it only when it reaches a real source.
    #    The comparator already traced through staging.  When odi_table is a
    #    real (non-staging) ref AND odi_col is concrete, that's the runnable
    #    projection ODI uses.  Re-alias to our scheme.
    if comp_result and comp_result.odi_table and comp_result.odi_col:
        odi_col_clean = _first_token(comp_result.odi_col).upper()
        odi_table_clean = _first_token(comp_result.odi_table).upper()
        odi_schema_clean = _first_token(comp_result.odi_schema).upper()
        fq = f"{odi_schema_clean}.{odi_table_clean}".strip(".").upper()
        # Skip staging-table pointers (e.g. AVY_FACT_STEPN_STG_RT) -- they
        # carry no real source.
        is_staging = (
            odi_model is not None
            and fq.split(".")[-1] in {s.name.upper() for s in odi_model.staging_steps if s.name}
        )
        # Require clean identifier-shaped col before emitting
        if not is_staging and odi_col_clean and _IDENT_RE.match(odi_col_clean):
            # T-alias hint (e.g. "(FROM T2)") overrides the canonical alias
            # mapping when ODI's expression uses a distinct alias for the
            # same physical fq.  Extract the alias verbatim from ODI's
            # expression so the projection lands on the correct self-join
            # target (the JOIN itself is harvested by _harvest_odi_joins
            # since it no longer skips self-joins under distinct aliases).
            alias: Optional[str] = None
            if t_alias_hint and comp_result.odi_expr_sql:
                hinted_alias = _extract_alias_for_col(
                    comp_result.odi_expr_sql, odi_col_clean,
                )
                if hinted_alias and hinted_alias.upper() != base_alias.upper():
                    # Confirm the join graph actually carries this alias
                    # (otherwise the projection would reference an
                    # unjoined relation -> Oracle ORA-00942).
                    if joins_by_key is not None and any(
                        jn.alias.upper() == hinted_alias.upper()
                        for jn in joins_by_key.values()
                    ):
                        alias = hinted_alias
            if alias is None:
                key = fq
                # Multi-role lookup tables: prefer the row-specific alias the
                # DRD AD harvest just registered, so the projection lands on
                # the join that actually matches this column's role.  The
                # base table is exempt -- it's always reachable via base_alias.
                is_mr_secondary = (
                    multi_role_fqs
                    and key in multi_role_fqs
                    and key != base_fq.upper()
                )
                if key in row_alias_for_fq:
                    alias = row_alias_for_fq[key]
                elif is_mr_secondary:
                    # No DRD AD join for this row -- match by target column
                    # role-root against ODI alias names.  Generic heuristic.
                    matched = _match_target_to_odi_role(
                        target, multi_role_fqs[key],
                    )
                    if matched:
                        alias = matched.lower()
                        row_alias_for_fq[key] = alias
                    elif is_unimplementable_prose_rule(transformation):
                        # Multi-role fallback would route to the wrong role
                        # AND the DRD prose explicitly describes a parse /
                        # lookup-not-implemented rule.  Emit NULL with a note
                        # rather than poisoning the projection.
                        return _ColumnPlan(
                            target_col=target,
                            source_expr="NULL",
                            provenance="NULL_UNIMPLEMENTED_PROSE",
                            notes=(
                                f"DRD prose describes a derivation that is "
                                f"not auto-generatable from {fq} (multi-role "
                                f"lookup; no matching role for target {target}). "
                                f"Source DRD rule needs operator implementation.{etl_note}"
                            ),
                        )
                    else:
                        alias = alias_assignments.get(key)
                else:
                    alias = alias_assignments.get(key)
                if alias is None:
                    if key == base_fq.upper():
                        alias = base_alias
                    else:
                        alias = _alias_from_table(fq, used_aliases)
                    alias_assignments[key] = alias
            base_expr = f"{alias}.{odi_col_clean}"
            wrapped = _maybe_case_wrap(base_expr)
            prov = "ODI_CASE_FILTERED" if wrapped != base_expr else "ODI"
            if t_alias_hint and alias.upper() != base_alias.upper():
                prov = "ODI_T_ALIAS"
            note_extra = f" (applicable only for {applicable_code})" if applicable_code else ""
            t_hint_note = f" (T-alias hint: {t_alias_hint})" if t_alias_hint else ""
            return _ColumnPlan(
                target_col=target,
                source_expr=wrapped,
                provenance=prov,
                notes=f"ODI staging chain -> {fq}.{odi_col_clean}{note_extra}{t_hint_note}{etl_note}",
            )

    # 2) DRD physical pass-through: row has explicit source_table + attribute.
    if source_attr and source_table and _IDENT_RE.match(source_attr):
        fq = f"{source_schema}.{source_table}" if source_schema else source_table
        key = fq.upper()
        # Multi-role: prefer row-specific alias when DRD AD established one.
        # Otherwise fall back to target-column-name role matching.
        # SPECIAL CASE: when fq == base_fq, the base FROM clause already
        # provides the canonical alias -- no role-pick needed (the base
        # table is always trivially reachable).
        alias = row_alias_for_fq.get(key)
        is_mr_secondary = (
            bool(multi_role_fqs)
            and key in (multi_role_fqs or {})
            and key != base_fq.upper()
        )
        if alias is None and is_mr_secondary:
            matched = _match_target_to_odi_role(target, multi_role_fqs[key])
            if matched:
                alias = matched.lower()
                row_alias_for_fq[key] = alias
            elif is_unimplementable_prose_rule(transformation):
                # Multi-role lookup + no matching role + prose says
                # "Parse / lookup not implemented".  Emit NULL with note
                # rather than fall back to the wrong role's alias.
                return _ColumnPlan(
                    target_col=target,
                    source_expr="NULL",
                    provenance="NULL_UNIMPLEMENTED_PROSE",
                    notes=(
                        f"DRD prose describes a derivation that is not "
                        f"auto-generatable from {fq} (multi-role lookup; "
                        f"no matching role for target {target}). "
                        f"Source DRD rule needs operator implementation.{etl_note}"
                    ),
                )
        if alias is None:
            alias = alias_assignments.get(key)
        if alias is None:
            if key == base_fq.upper():
                alias = base_alias
            else:
                alias = _alias_from_table(fq, used_aliases)
            alias_assignments[key] = alias
        base_expr = f"{alias}.{source_attr}"
        wrapped = _maybe_case_wrap(base_expr)
        prov = "DRD_PHYSICAL_CASE" if wrapped != base_expr else "DRD_PHYSICAL"
        note_extra = f" (applicable only for {applicable_code})" if applicable_code else ""
        return _ColumnPlan(
            target_col=target,
            source_expr=wrapped,
            provenance=prov,
            notes=f"DRD source {fq}.{source_attr}{note_extra}{etl_note}",
        )

    # 3) NULL last resort
    return _ColumnPlan(
        target_col=target,
        source_expr="NULL",
        provenance="NULL_FALLBACK",
        notes=f"no DRD source attribute and no ODI projection found{etl_note}",
    )


# ── Public entry point ───────────────────────────────────────────────────────

@dataclass
class DrdFirstInsertResult:
    sql: str
    column_count: int
    join_count: int
    cte_count: int
    provenance_summary: Dict[str, int] = field(default_factory=dict)


def emit_insert_drd_first(
    *,
    target_schema: str,
    target_table: str,
    target_definition: Dict[str, Any],
    analysis_rows: List[Dict[str, Any]],
    odi_model: Optional[ODIModel] = None,
    comparison_results: Optional[List[ComparisonResult]] = None,
    parallel_degree: int = 8,
    all_etl_notes_text: str = "",
    etl_column_defaults: Optional[Dict[str, str]] = None,
) -> DrdFirstInsertResult:
    """Build a complete INSERT INTO <target> covering every target column.

    Inputs:
      target_definition  - PDM target (369 cols with dtype + nullable)
      analysis_rows      - per-target DRD context (incl. etl_block_*)
      odi_model          - optional ODI parser result (used as the example
                           when comparator marks the projection MATCHED)
      comparison_results - optional list of ComparisonResult (one per row);
                           routing prefers ODI for MATCHED rows.
    """
    target_fq = f"{target_schema}.{target_table}"
    by_target: Dict[str, Dict[str, Any]] = {
        (r.get("column") or "").strip().upper(): r
        for r in analysis_rows
        if r.get("column")
    }
    by_target_cmp: Dict[str, ComparisonResult] = {}
    if comparison_results:
        by_target_cmp = {
            r.target_col.upper(): r for r in comparison_results if r.target_col
        }

    # Detect the base table from row frequencies; everything else hangs off it.
    base_fq, base_alias = _detect_base_table(analysis_rows)
    # Pre-allocate canonical alias for every physical table referenced anywhere
    # (ODI source_bindings + DRD col-AD joins + row.source_table).  This makes
    # ON-predicate rewriting consistent across rows.
    alias_assignments, used_aliases = _build_canonical_aliases(
        odi_model=odi_model,
        analysis_rows=analysis_rows,
        base_fq=base_fq,
        base_alias=base_alias,
    )
    cte_assignments: "OrderedDict[str, _CteNeed]" = OrderedDict()

    # Operator-overridable ETL-column defaults: merge caller-provided dict
    # on top of the generic shared defaults so other DRDs / target tables
    # work without code change.
    effective_etl_defaults = dict(DEFAULT_ETL_COLUMN_VALUES)
    if etl_column_defaults:
        effective_etl_defaults.update({k.upper(): v for k, v in etl_column_defaults.items()})

    # Stash the full ETL Notes text + effective ETL defaults on every row so
    # _plan_column can consult them without a module-level singleton.
    if all_etl_notes_text:
        for r in analysis_rows:
            r.setdefault("_all_etl_text", all_etl_notes_text)
    for r in analysis_rows:
        r["_etl_defaults"] = effective_etl_defaults

    # Register every ETL Notes block referenced by any row so the prose
    # surfaces in the header even when the projection takes the DRD_PHYSICAL
    # path (v4 design: physical source wins over prose-block reference, but
    # the prose still informs the operator about subset/filter rules).
    for r in analysis_rows:
        ref = (r.get("etl_block_ref") or "").strip().upper()
        body = (r.get("etl_block_body") or "").strip()
        if ref and body:
            name = ref.lower()
            if name not in cte_assignments:
                cte_assignments[name] = _CteNeed(
                    name=name, body=body, is_sql=False,
                )

    # Harvest ODI's join graph FIRST so column planning can reuse aliases.
    # This produces a clean, validated set of LEFT JOINs from ODI's actual
    # implementation (ON predicates verbatim, no invented keys).
    joins_by_key: "OrderedDict[Tuple[str, str], _JoinNeed]" = _harvest_odi_joins(
        odi_model=odi_model,
        base_fq=base_fq,
        base_alias=base_alias,
        alias_assignments=alias_assignments,
        used_aliases=used_aliases,
    )

    # Detect multi-role lookup tables (same fq joined under >=2 aliases in
    # ODI).  When a DRD-author reuses ONE alias for all such rows, the naive
    # harvest would AND every row's ON predicate onto the same alias and
    # produce an impossible JOIN that returns no rows -- the silent NULL bug
    # surfaced 2026-05-29.  Per-row alias picking restores one JOIN per role.
    multi_role_fqs: Dict[str, List[Tuple[str, str]]] = _collect_multi_role_fqs(odi_model)

    plans: List[_ColumnPlan] = []
    provenance_counts: Dict[str, int] = {}
    for col_def in target_definition.get("columns", []) or []:
        target = (col_def.get("name") or "").strip().upper()
        if not target:
            continue
        row = by_target.get(target, {})
        cmp = by_target_cmp.get(target)
        plan = _plan_column(
            target=target,
            row=row,
            comp_result=cmp,
            odi_model=odi_model,
            base_alias=base_alias,
            base_fq=base_fq,
            alias_assignments=alias_assignments,
            used_aliases=used_aliases,
            cte_assignments=cte_assignments,
            joins_by_key=joins_by_key,
            multi_role_fqs=multi_role_fqs,
        )
        plans.append(plan)
        provenance_counts[plan.provenance] = provenance_counts.get(plan.provenance, 0) + 1

    # Compute the set of aliases that actually have a real ON predicate (i.e.
    # are safe to project from).  Anything else gets rewritten to NULL -- we
    # NEVER emit ``ON 1=0`` placeholder joins (operator-locked 2026-05-29).
    safe_aliases: set = {base_alias.upper()}
    for jn in joins_by_key.values():
        if jn.on_predicates:
            safe_aliases.add(jn.alias.upper())

    # Drop any joins that lack ON predicates (they couldn't be sourced from
    # ODI's parsed graph nor from inline DRD col-AD).
    joins_by_key = OrderedDict(
        (k, v) for k, v in joins_by_key.items() if v.on_predicates
    )

    # Rewrite plans whose source alias is unsafe.  Fallback strategy:
    #   (a) if the row's DRD physical source resolves to the base table OR to
    #       a different alias that IS safe, re-route the projection there.
    #   (b) if the row's transformation text parses to an inline JOIN whose
    #       target IS in safe_aliases, re-route to that.
    #   (c) only then NULL with explanatory note.
    def _first_token_local(s: str) -> str:
        if not s: return ""
        for sep in ("\n", ",", " ", "\t"):
            s = s.split(sep)[0]
        return s.strip()

    rewritten = 0
    for p in plans:
        m = re.match(r"^([A-Za-z][A-Za-z0-9_$#]*)\.", p.source_expr or "")
        if not m:
            continue
        alias = m.group(1).upper()
        if alias in safe_aliases:
            continue
        # Find original fq for diagnostic context
        fq_unsafe = next(
            (k for k, v in alias_assignments.items() if v.upper() == alias),
            "<unknown>",
        )
        original = p.source_expr
        row = by_target.get(p.target_col, {})

        # (a) DRD physical source fallback
        src_attr = _first_token_local((row.get("source_attribute") or "")).upper()
        src_tab = _first_token_local((row.get("source_table") or "")).upper()
        src_sch = _first_token_local((row.get("source_schema") or "")).upper()
        if src_attr and src_tab and _IDENT_RE.match(src_attr):
            drd_fq = f"{src_sch}.{src_tab}" if src_sch else src_tab
            drd_fq_u = drd_fq.upper()
            drd_alias = alias_assignments.get(drd_fq_u)
            if drd_fq_u == base_fq.upper():
                p.source_expr = f"{base_alias}.{src_attr}"
                p.notes = f"DRD physical source {drd_fq}.{src_attr} (ODI ref to {fq_unsafe} skipped: no JOIN)"
                old = p.provenance; p.provenance = "DRD_PHYSICAL_FALLBACK"
                provenance_counts[old] = max(0, provenance_counts.get(old, 0) - 1)
                provenance_counts["DRD_PHYSICAL_FALLBACK"] = provenance_counts.get("DRD_PHYSICAL_FALLBACK", 0) + 1
                rewritten += 1
                continue
            if drd_alias and drd_alias.upper() in safe_aliases:
                p.source_expr = f"{drd_alias}.{src_attr}"
                p.notes = f"DRD physical source {drd_fq}.{src_attr} (ODI ref to {fq_unsafe} skipped: no JOIN)"
                old = p.provenance; p.provenance = "DRD_PHYSICAL_FALLBACK"
                provenance_counts[old] = max(0, provenance_counts.get(old, 0) - 1)
                provenance_counts["DRD_PHYSICAL_FALLBACK"] = provenance_counts.get("DRD_PHYSICAL_FALLBACK", 0) + 1
                rewritten += 1
                continue

        # (b) inline DRD col-AD parse: any join target that is safe?
        ad = parse_drd_ad(row.get("transformation") or "")
        for j in ad.joins:
            j_alias = alias_assignments.get(j.fq_table.upper())
            if j_alias and j_alias.upper() in safe_aliases and src_attr:
                p.source_expr = f"{j_alias}.{src_attr}"
                p.notes = f"DRD inline join {j.fq_table}.{src_attr} (ODI ref to {fq_unsafe} skipped: no JOIN)"
                old = p.provenance; p.provenance = "DRD_INLINE_FALLBACK"
                provenance_counts[old] = max(0, provenance_counts.get(old, 0) - 1)
                provenance_counts["DRD_INLINE_FALLBACK"] = provenance_counts.get("DRD_INLINE_FALLBACK", 0) + 1
                rewritten += 1
                break
        else:
            # (c) last resort NULL
            p.source_expr = "NULL"
            p.notes = (
                f"NULL: required JOIN to {fq_unsafe} has no ON predicate in either "
                f"ODI graph or DRD col-AD (was: {original})"
            )
            old = p.provenance; p.provenance = "NULL_NO_JOIN"
            provenance_counts[old] = max(0, provenance_counts.get(old, 0) - 1)
            provenance_counts["NULL_NO_JOIN"] = provenance_counts.get("NULL_NO_JOIN", 0) + 1
            rewritten += 1

    # ── Compose SQL ──────────────────────────────────────────────────────────
    # No CTEs in v4 -- ETL Notes prose blocks are surfaced as a header comment
    # block (operator context); per-column projections cite the referenced
    # block inline so the operator still sees which DRD rule applies.
    sql_ctes = []
    prose_ctes = list(cte_assignments.values())
    cte_block = ""
    prose_header = ""
    if prose_ctes:
        parts = [
            "-- ETL Notes block(s) referenced by some DRD col-AD cells; the",
            "-- emitter projects from each column's physical DRD source_attribute",
            "-- and annotates the projection with the referenced block name so",
            "-- the operator can apply the prose filter rule (e.g. APASEC/APACSH",
            "-- subset selection) by hand.",
        ]
        for cte in prose_ctes:
            parts.append(f"-- ETL_BLOCK[{cte.name.upper()}]:")
            for ln in cte.body.splitlines():
                parts.append(f"--   {ln}" if ln.strip() else "--")
        prose_header = "\n".join(parts) + "\n"

    # INSERT column list
    col_list_lines = []
    for i, p in enumerate(plans):
        sep = "," if i < len(plans) - 1 else ""
        col_list_lines.append(f"    {p.target_col}{sep}")
    col_list = "\n".join(col_list_lines)

    # SELECT list
    sel_lines = []
    for i, p in enumerate(plans):
        sep = "," if i < len(plans) - 1 else ""
        comment = f"  -- [{p.provenance}] {p.notes}" if p.notes else f"  -- [{p.provenance}]"
        sel_lines.append(f"    {p.source_expr:<40s} AS {p.target_col}{sep}{comment}")
    sel_list = "\n".join(sel_lines)

    # FROM + JOIN tree
    from_clause = f"FROM {base_fq} {base_alias}"
    join_lines = []
    for jn in joins_by_key.values():
        if jn.on_predicates:
            on_text = " AND ".join(jn.on_predicates)
        else:
            on_text = "1=0 /* TODO: no DRD AD ON clause; patch manually */"
        join_lines.append(f"LEFT JOIN {jn.fq_table} {jn.alias} ON {on_text}")
    join_block = ("\n" + "\n".join(join_lines)) if join_lines else ""

    header = (
        f"-- Generated by drd_first_emitter v1\n"
        f"-- Target: {target_fq}\n"
        f"-- Columns: {len(plans)}  Joins: {len(joins_by_key)}  CTEs: {len(cte_assignments)}\n"
        f"-- Provenance: " + ", ".join(f"{k}={v}" for k, v in sorted(provenance_counts.items()))
        + "\n"
    )

    # Oracle does not allow ``WITH ... TRUNCATE`` -- a CTE is a SELECT-prefix.
    # Emit TRUNCATE FIRST as its own statement, then start the INSERT with WITH.
    sql = (
        f"{header}\n"
        f"{prose_header}"
        f"TRUNCATE TABLE {target_fq};\n"
        f"{cte_block}"
        f"INSERT /*+ APPEND PARALLEL({parallel_degree}) */ INTO {target_fq} (\n"
        f"{col_list}\n"
        f")\n"
        f"SELECT /*+ PARALLEL({parallel_degree}) */\n"
        f"{sel_list}\n"
        f"{from_clause}"
        f"{join_block};\n"
    )

    # ── Hard rule (operator 2026-05-29): validate Oracle SQL before return ──
    from app.sql_model.oracle_validator import (
        OracleValidationError, validate_oracle_sql,
    )
    val = validate_oracle_sql(sql, run_live=False)
    if not val.is_valid:
        raise OracleValidationError(val)

    return DrdFirstInsertResult(
        sql=sql,
        column_count=len(plans),
        join_count=len(joins_by_key),
        cte_count=len(sql_ctes),
        provenance_summary=provenance_counts,
    )
