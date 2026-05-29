"""Semantic 3-way comparator: DRD claim vs ODI resolved source vs KB.

Replaces the strip_sql_qualifiers regex approach with typed IR comparison.

Input sides:
  DRD   — what the design document says the source is
            (source_schema, source_table, source_attribute, transformation)
  ODI   — what the actual ODI XML mapping resolves to
            (ColumnMapping from OdiXmlParser → ResolvedColumn | UnresolvedExpr)
  KB    — the local schema KB (schema_kb_ds_1.json) that knows physical tables

Output: ComparisonVerdict + an explanation dict with evidence for the grid.

Design rule: no regex string-mangling on the result; comparison is structural.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from app.sql_model.drd_ad_parser import (
    DrdAdRule,
    compare_drd_ad_joins,
    parse_drd_ad,
)
from app.sql_model.drd_rules import (
    extract_applicable_only_code,
    extract_exists_derived_flag,
    find_discriminator_for_code,
)
from app.sql_model.types import (
    AliasBinding,
    ColumnMapping,
    ComparisonVerdict,
    MismatchKind,
    ODIModel,
    Provenance,
    ResolvedColumn,
    StagingStep,
    TableRef,
    UnresolvedExpr,
    norm,
)

if TYPE_CHECKING:
    from app.sql_model.static_validator import KBLookup

logger = logging.getLogger(__name__)

# Trailing parenthetical notes in DRD source_attribute cells, e.g. "SRC_STM_ID (FROM T2)".
# Requires a space before '(' and only allows A-Z 0-9 _ and spaces inside — so Oracle
# function calls like TO_DATE(LOAD_DT,'YYYYMMDD') are NOT stripped (no leading space,
# contains commas/quotes).
_PAREN_NOTE_RE = re.compile(r"\s+\([A-Z0-9_ ]+\)\s*$")


# ── Generic DRD-rule complexity / ODI expression heuristics ────────────────────
#
# These detectors are intentionally schema-agnostic and column-agnostic.  They
# answer:
#   * "Does the DRD say the column needs derivation (not pass-through)?"
#   * "Is the ODI projection a simple pass-through (not derivation)?"
# When DRD says derive but ODI passes through (or vice versa) -> TRANSFORMATION_DRIFT.

# Words that strongly indicate the DRD rule is NOT a simple pass-through.
# Word-boundary matched, case-insensitive.  Generic across any DRD / table.
_COMPLEX_RULE_KEYWORDS_RE = re.compile(
    r"\b(parse|lookup|case\s+when|decode|regexp|extract|substr|substring|"
    r"trim|to_date|to_number|to_char|coalesce|nvl|nullif|"
    r"if\s+|when\s+|then\s+|else\s+|"
    r"derive|compute|calculate|concatenate|concat|"
    r"multiply|divide|round|floor|ceil|"
    r"join\b|left\s+join|inner\s+join|left\s+outer|right\s+outer)\b",
    re.IGNORECASE,
)

# Bare ALIAS.COL or bare COL — these are simple pass-throughs.
_SIMPLE_REF_RE = re.compile(
    r"^\s*[A-Z][A-Z0-9_$#]*(?:\.[A-Z][A-Z0-9_$#]*)?\s*$",
    re.IGNORECASE,
)

# Literals + safe constants — also "simple", not transformations.
_LITERAL_OR_CONST_RE = re.compile(
    r"^\s*(?:SYSDATE|SYSTIMESTAMP|NULL|TRUNC\(\s*SYSDATE\s*\)|"
    r"'[^']*'|\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)

# "None" / "-" / blank cells in DRD col AD that mean "no rule documented".
_TRIVIAL_RULE_TOKENS = {"", "none", "n/a", "na", "-", "--", "tbd", "?"}


def _drd_rule_is_complex(transformation: str) -> bool:
    """Return True if the DRD transformation rule encodes derivation logic
    (CASE / parse / lookup / join / multi-step) rather than a pass-through.

    Generic: no hard-coded table / column / domain names. Heuristics:
      * Empty / placeholder text ("None", "-", ...) -> NOT complex.
      * Multi-line text -> complex (DRD authors use line breaks for steps).
      * Length > 60 chars after collapsing whitespace -> complex.
      * Any keyword from _COMPLEX_RULE_KEYWORDS_RE -> complex.
    """
    if not transformation:
        return False
    t = transformation.strip()
    if t.lower() in _TRIVIAL_RULE_TOKENS:
        return False
    if "\n" in t:
        return True
    # Collapse runs of whitespace for the length heuristic
    collapsed = re.sub(r"\s+", " ", t)
    if len(collapsed) > 60:
        return True
    if _COMPLEX_RULE_KEYWORDS_RE.search(collapsed):
        return True
    return False


def _odi_expr_is_simple(expr_sql: str) -> bool:
    """Return True if the ODI projection is a bare column ref / literal / const.

    Simple = nothing the operator needs to verify by hand.  Anything else
    (CASE, NVL, arithmetic, function call) is non-simple.
    """
    if not expr_sql:
        return False
    e = expr_sql.strip()
    if e.endswith(","):
        e = e[:-1].strip()
    if _SIMPLE_REF_RE.match(e):
        return True
    if _LITERAL_OR_CONST_RE.match(e):
        return True
    return False


# ── Generic text-search fallback for columns not in column_mappings ───────────
#
# Some ODI STEP_INSERT blocks have no explicit column list, so the parser leaves
# StagingStep.column_mappings empty for those projections.  When that happens
# and the column IS still in model.final_insert_columns, scan select_sql text
# directly for `<expr> [AS] <COL_NAME>` patterns.  This handles the long tail
# of "the parser didn't catch it" without committing the whole comparator to a
# full re-parse.

_PROJECTION_TAIL_RE_CACHE: dict[str, re.Pattern] = {}


def _projection_tail_re(col_name: str) -> re.Pattern:
    """Return / cache a regex matching `<expr> [AS] <col_name> [,]` projections."""
    key = col_name.upper()
    pat = _PROJECTION_TAIL_RE_CACHE.get(key)
    if pat is None:
        # Find the col as a trailing alias in a SELECT-list item.  Allow the
        # expression to be a bare alias.col, a function call, an arithmetic
        # expression, etc.  Anchor on `,` `\n` or start-of-line on the left and
        # `,` `\n` or end-of-line on the right.
        pat = re.compile(
            r"(?:^|\n|,)[\s]*"          # boundary
            r"([^,\n]+?)"                 # the expression itself
            r"\s+(?:AS\s+)?"             # optional AS
            + re.escape(key)             # the column name
            + r"\s*(?=,|\n|$)",          # boundary on the right
            re.IGNORECASE,
        )
        _PROJECTION_TAIL_RE_CACHE[key] = pat
    return pat


_STAGING_REF_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)\s*$",
    re.IGNORECASE,
)


_BARE_REF_FINDER_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_$#]*)\.([A-Za-z][A-Za-z0-9_$#]*)\b",
)

_SQL_RESERVED_PREFIXES = {
    "SYSDATE", "SYSTIMESTAMP", "TRUNC", "TO_CHAR", "TO_DATE", "TO_NUMBER",
    "NVL", "COALESCE", "DECODE", "CASE", "WHEN", "THEN", "ELSE", "END",
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "EXISTS", "IN", "BETWEEN",
    "LIKE", "REGEXP_LIKE", "SUM", "AVG", "MIN", "MAX", "COUNT", "CAST",
}


def _extract_column_refs(expr: str) -> List[Tuple[str, str]]:
    """Extract every ``<alias>.<col>`` reference from a SQL expression.

    Strips out SQL keyword "aliases" (SYSDATE.something would be bogus).
    Generic -- no specific table / column / business names.

    Used by the semantic-equivalence check: when ODI projects
    ``NVL(EXG_DIM.EXG_DIM_ID,0)`` or ``(coalesce(APA_CASH.X, APA_SECURITY.X))``,
    we extract the leaves so the comparator can match against DRD's bare
    ``source_attribute`` regardless of the SQL wrapping.
    """
    if not expr:
        return []
    out: List[Tuple[str, str]] = []
    for m in _BARE_REF_FINDER_RE.finditer(expr):
        alias = m.group(1)
        col = m.group(2)
        if alias.upper() in _SQL_RESERVED_PREFIXES:
            continue
        out.append((alias, col))
    return out


def _columns_equivalent_modulo_prefix(odi_col: str, drd_col: str) -> bool:
    """True if ``odi_col`` and ``drd_col`` refer to the same underlying column
    modulo a single prefix segment.

    Use case: ODI subset-CTEs rename pre-joined columns by prepending a role
    prefix (``OFST_<X>``, ``SEC_<X>``, ``CASH_<X>``, ``BKR_<X>``, ``SBC_<X>``,
    ``OWN_<X>``, ...).  Generic: ANY single prefix segment ending in ``_``
    counts; we do not hardcode the list.

    Examples (generic, no business-domain names):
      * OFST_AR_DIM_ID  vs  AR_DIM_ID    -> True
      * SEC_ORIG_QTY    vs  ORIG_QTY     -> True
      * FOO_BAR_BAZ     vs  BAR_BAZ      -> True  (FOO_ prefix)
      * XYZ             vs  ABC           -> False
    """
    if not odi_col or not drd_col:
        return False
    o = odi_col.strip().upper()
    d = drd_col.strip().upper()
    if o == d:
        return True
    if o.endswith("_" + d):
        return True
    if d.endswith("_" + o):
        return True
    return False


def _odi_expr_references_column(odi_expr: str, drd_col: str) -> bool:
    """True if any leaf ref in ``odi_expr`` projects from the same bare column
    name as ``drd_col`` (modulo a single role-prefix segment).  Generic --
    works for any column / alias names.

    Examples (no business-domain names):
      * ``NVL(X.Y, 0)``                  vs "Y"        -> True
      * ``coalesce(A.X, B.X)``           vs "X"        -> True
      * ``CASE WHEN c THEN A.X ELSE B.X END`` vs "X"   -> True
      * ``APA_CASH.OFST_AR_DIM_ID``      vs "AR_DIM_ID" -> True (prefix)
      * ``A.Z``                          vs "X"        -> False
    """
    if not odi_expr or not drd_col:
        return False
    drd_up = drd_col.strip().upper()
    if not drd_up:
        return False
    refs = _extract_column_refs(odi_expr)
    return any(_columns_equivalent_modulo_prefix(col, drd_up) for _alias, col in refs)


def _normalize_case_when_redundant(expr: str) -> str:
    """Strip CASE wrappers that route every branch to the SAME column.

    Example:
        ``(CASE WHEN a.x = 1 THEN APA.AMT ELSE APA.AMT END)`` -> ``APA.AMT``
        ``(CASE WHEN a.x = 1 THEN APA.AMT WHEN a.x = 2 THEN APA.AMT END)`` -> ``APA.AMT``
        ``coalesce(APA.X, APA.X)``                                     -> ``APA.X``

    Generic -- pattern-based; no specific table / column names.
    """
    if not expr:
        return expr
    e = expr.strip()
    # Find all "THEN <ref>" + "ELSE <ref>" + "coalesce(<ref>, <ref>)" references.
    # If they all match a single bare <alias>.<col>, the CASE is redundant.
    refs = re.findall(
        r"(?:\bTHEN\s+|\bELSE\s+|coalesce\s*\(\s*)"
        r"([A-Za-z][A-Za-z0-9_$#]*\.[A-Za-z][A-Za-z0-9_$#]*)",
        e,
        re.IGNORECASE,
    )
    if not refs:
        return expr
    norm_refs = {r.strip().upper() for r in refs}
    if len(norm_refs) == 1:
        single = next(iter(norm_refs))
        return single
    return expr


def _staging_table_to_step_id(model: ODIModel, table_ref: str) -> Optional[int]:
    """Map a staging-table reference (any variant: with/without prefix, with
    or without _RT) back to the step that writes it."""
    t = table_ref.upper()
    for s in model.staging_steps:
        if not s.name:
            continue
        n = norm(s.name)
        no_prefix = re.sub(r"^[A-Z0-9]+_", "", n)
        candidates = {n, n + "_RT", no_prefix, no_prefix + "_RT"}
        if t in candidates:
            return s.step_id
    return None


def _text_search_one_step(step: StagingStep, col_name: str) -> Optional[str]:
    """Search ONLY this step's select_sql; return raw expr or None."""
    if not step or not step.select_sql or not col_name:
        return None
    pat = _projection_tail_re(col_name)
    m = pat.search(step.select_sql)
    if m is None:
        return None
    expr = m.group(1).strip()
    if expr.upper() == col_name.upper():
        return None
    return expr


def _follow_staging_chain_text(
    model: ODIModel,
    col_name: str,
    staging_tables: set,
    max_depth: int = 10,
) -> Optional[tuple[int, str]]:
    """Walk ``<staging_table>.<col>`` pass-throughs across STEP boundaries.

    Strategy:
      1. Start from the MERGE block (or the deepest step) projection for ``col_name``.
      2. If the projection matches ``<staging_table>.<col>``, look up that
         staging_table's writer-step and search ITS select_sql for the
         referenced ``col``.
      3. Continue until we hit a non-pass-through expression OR a real
         (non-staging) table.

    Returns ``(step_id, expr)`` of the deepest concrete projection found, or
    ``None`` if nothing.
    """
    # Initial expression: from the MERGE if any, else from the latest step
    fs = model.final_select_sql or ""
    if fs:
        m = _projection_tail_re(col_name).search(fs)
        if m is not None and m.group(1).strip().upper() != col_name.upper():
            current_expr = m.group(1).strip()
            current_step_id = 0  # MERGE
        else:
            current_expr = None
            current_step_id = None
    else:
        current_expr = None
        current_step_id = None

    if current_expr is None:
        # No MERGE projection -- fall back to global latest-step search
        found = _text_search_step_for_column(model, col_name)
        if found is None:
            return None
        current_step_id, current_expr = found

    last: tuple[int, str] = (current_step_id, _normalize_case_when_redundant(current_expr))
    visited: set = set()

    for _ in range(max_depth):
        norm_expr = _normalize_case_when_redundant(current_expr)
        last = (current_step_id, norm_expr)
        m = _STAGING_REF_RE.match(norm_expr)
        if not m:
            return last
        ref_table = m.group(1).upper()
        ref_col = m.group(2).upper()
        if ref_table not in staging_tables:
            return last  # real source reached
        # Find the step that writes this staging table
        writer_step_id = _staging_table_to_step_id(model, ref_table)
        if writer_step_id is None:
            return last
        step = model.step(writer_step_id)
        if step is None:
            return last
        next_expr = _text_search_one_step(step, ref_col)
        if next_expr is None:
            return last
        key = (writer_step_id, ref_col)
        if key in visited:
            return last  # cycle guard
        visited.add(key)
        current_step_id = writer_step_id
        current_expr = next_expr
    return last


def _text_search_step_for_column(
    model: ODIModel,
    col_name: str,
) -> Optional[tuple[int, str]]:
    """Scan every staging step's select_sql text for ``<expr> AS <col_name>``.

    Returns ``(step_id, raw_expr)`` of the LAST step (highest step_id) where
    the projection is found, or falls back to the final MERGE SELECT body
    (``model.final_select_sql``, returned as step_id=0) if no STEP projects
    the column directly.

    Generic — no schema / table / column-name hard-coding; just regex over
    SQL text.
    """
    if not col_name:
        return None
    pat = _projection_tail_re(col_name)
    found: Optional[tuple[int, str]] = None
    for step in model.staging_steps:  # iterate in order (step_id ascending)
        sql = step.select_sql or ""
        m = pat.search(sql)
        if m is not None:
            expr = m.group(1).strip()
            # Skip degenerate matches like the column appearing twice on the same line
            if expr.upper() == col_name.upper():
                continue
            found = (step.step_id, expr)
    if found is not None:
        return found
    # Last-resort fallback: scan the MERGE block SELECT body.  This catches
    # columns that ODI projects only at the top-level pass-through layer
    # (e.g. ``MERGE_STG_RT.<col>``) without the staging steps ever deriving
    # them.  When DRD demands derivation and this is what ODI does, it IS
    # the TRANSFORMATION_DRIFT we want to surface.
    fs = model.final_select_sql or ""
    if fs:
        m = pat.search(fs)
        if m is not None:
            expr = m.group(1).strip()
            if expr.upper() != col_name.upper():
                return (0, expr)
    return None


# ── DRD side representation ───────────────────────────────────────────────────

@dataclass(frozen=True)
class DrdClaim:
    """Normalized representation of one DRD mapping row."""
    target_col: str               # e.g. "BKR_AR_ID"
    source_schema: str            # e.g. "CCAL_REPL_OWNER"
    source_table: str             # e.g. "APA" or "APA_SECURITY_POSITION"
    source_attr: str              # e.g. "BKR_AR_ID"
    transformation: str           # free-text rule, may be empty
    # P3: cross-tab ETL-Notes block reference + resolved body (if any).  Populated
    # by build_analysis_rows when the row says "Use <NAME> logic from 'ETL Notes' tab".
    etl_block_ref: str = ""
    etl_block_body: str = ""
    # Full ETL Notes content (across every sheet) -- used by the shared
    # rule engine to resolve discriminators in master / sentence-headered
    # blocks that aren't indexed as named blocks.
    all_etl_text: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DrdClaim":
        raw_attr = norm(d.get("source_attribute") or "")
        # Strip trailing parenthetical notes like " (FROM T2)" — must have a space
        # before '(' and only alphanumeric/underscore/space inside so Oracle calls
        # like TO_DATE(col,'fmt') are never touched.
        clean_attr = _PAREN_NOTE_RE.sub("", raw_attr)
        return cls(
            target_col=norm(d.get("physical_name") or d.get("target_col") or ""),
            source_schema=norm(d.get("source_schema") or ""),
            source_table=norm(d.get("source_table") or ""),
            source_attr=clean_attr,
            transformation=(d.get("transformation") or "").strip(),
            etl_block_ref=(d.get("etl_block_ref") or "").strip(),
            etl_block_body=(d.get("etl_block_body") or "").strip(),
            all_etl_text=(d.get("_all_etl_text") or d.get("all_etl_text") or "").strip(),
        )

    @property
    def has_source(self) -> bool:
        return bool(self.source_attr)

    @property
    def effective_rule_text(self) -> str:
        """The fullest representation of what the DRD requires for this column.

        If a cross-tab ETL block was resolved, the block body is the real spec;
        the bare ``transformation`` cell typically just says "Use X logic from
        ETL Notes" which is meaningless on its own.  Concatenate both when both
        are present.
        """
        parts = []
        if self.transformation:
            parts.append(self.transformation)
        if self.etl_block_body:
            parts.append(f"[{self.etl_block_ref}] {self.etl_block_body}")
        return "\n".join(parts).strip()


# ── Comparison result ─────────────────────────────────────────────────────────

@dataclass
class ComparisonResult:
    """Full output of compare_drd_odi()."""
    verdict: ComparisonVerdict
    target_col: str
    drd_schema: str
    drd_table: str
    drd_attr: str
    odi_schema: str
    odi_table: str
    odi_col: str
    odi_expr_sql: str
    odi_step: int                               # which staging step resolved this
    explanation: str                            # human-readable, shown in grid
    unresolved_reason: str = ""                 # filled when UNRESOLVABLE
    alias_evidence: str = ""                    # "alias APA -> CCAL_REPL_OWNER.APA_SECURITY_POSITION"
    pdm_confirmed: bool = False                 # SOURCE_MISSING but column exists in PDM target table
    pdm_target_confirmed: bool = False          # UNRESOLVABLE but target col confirmed in PDM
    pdm_col_name: str = ""                      # PDM-authoritative column name (for grid display)
    # ── P0+P2 (2026-05-28): side-by-side DRD vs ODI evidence ──
    mismatch_kind: MismatchKind = MismatchKind.NONE
    drd_logic: str = ""                         # raw DRD col AD text (Transformation/Business Rules/Join)
    odi_logic: str = ""                         # raw ODI projection SQL fragment

    @property
    def is_ok(self) -> bool:
        return self.verdict in (ComparisonVerdict.MATCHED, ComparisonVerdict.ALIAS_DRIFT_ONLY)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "target_col": self.target_col,
            "drd_schema": self.drd_schema,
            "drd_table": self.drd_table,
            "drd_attr": self.drd_attr,
            "odi_schema": self.odi_schema,
            "odi_table": self.odi_table,
            "odi_col": self.odi_col,
            "odi_expr_sql": self.odi_expr_sql,
            "odi_step": self.odi_step,
            "explanation": self.explanation,
            "unresolved_reason": self.unresolved_reason,
            "alias_evidence": self.alias_evidence,
            "is_ok": self.is_ok,
            "pdm_confirmed": self.pdm_confirmed,
            "pdm_target_confirmed": self.pdm_target_confirmed,
            "pdm_col_name": self.pdm_col_name,
            "mismatch_kind": self.mismatch_kind.value,
            "drd_logic": self.drd_logic,
            "odi_logic": self.odi_logic,
        }


# ── Step lineage tracer ───────────────────────────────────────────────────────

def _find_column_in_model(
    target_col: str,
    model: ODIModel,
) -> Optional[tuple[int, ColumnMapping]]:
    """Search for target_col starting from the last step (STEP5) back to STEP1.

    Returns (step_id, ColumnMapping) or None if not found in any step.

    Strategy: the MERGE block's final INSERT columns are the ground truth for
    what we need. Each column is projected through the chain. We find it in
    the last step that contains it — that's the closest we get to the source
    for a simple pass-through column.
    """
    col_up = norm(target_col)
    for step in reversed(model.staging_steps):
        for cm in step.column_mappings:
            if norm(cm.target_col) == col_up:
                return step.step_id, cm
    return None


def _get_staging_table_names(model: ODIModel) -> set:
    """Return the set of all intermediate staging table names (uppercased).

    ODI MERGE blocks often reference the staging table with a runtime suffix
    (``_RT``) and/or without the schema prefix (e.g. step name
    ``SSDS_AVY_FACT_STEP5_STG`` becomes ``AVY_FACT_STEP5_STG_RT`` in the
    MERGE SELECT).  We collect every plausible variant so the staging-chain
    walker can recognise the pass-through.
    """
    out: set = set()
    for s in model.staging_steps:
        if not s.name:
            continue
        n = norm(s.name)
        out.add(n)
        # Strip a single leading <prefix>_ chunk (e.g. SSDS_)
        n_no_prefix = re.sub(r"^[A-Z0-9]+_", "", n)
        if n_no_prefix and n_no_prefix != n:
            out.add(n_no_prefix)
        # Runtime view suffix
        out.add(n + "_RT")
        if n_no_prefix and n_no_prefix != n:
            out.add(n_no_prefix + "_RT")
    return out


def _trace_to_original_source(
    column: str,
    model: ODIModel,
    staging_tables: Optional[set] = None,
) -> Optional[tuple[int, ColumnMapping]]:
    """Trace through staging steps to find the original non-staging source.

    ODI multi-step mappings use intermediate staging tables:
      STEP5 sources from SSDS_AVY_FACT_STEP5_STG
      STEP4 sources from SSDS_AVY_FACT_STEP3_STG (written by STEP3)
      STEP3 sources from real tables (TXN, APA, ...) ← origin

    Special case: a step whose step.name equals the staging table it sources
    from is self-referential (e.g. the final step that creates its own staging
    table).  In that case we search all earlier steps (lower step_id) for the
    column instead.

    Returns (step_id, ColumnMapping) where step_id is the step that contains
    the original non-staging source.  Falls back to _find_column_in_model
    result if the chain cannot be traced further.
    """
    if staging_tables is None:
        staging_tables = _get_staging_table_names(model)

    found = _find_column_in_model(column, model)
    if found is None:
        return None

    # Steps sorted by step_id descending (most recent first) for fallback search
    steps_desc = sorted(model.staging_steps, key=lambda s: s.step_id, reverse=True)

    step_id, cm = found
    for _ in range(10):
        if not isinstance(cm.source, ResolvedColumn):
            break
        src: ResolvedColumn = cm.source  # type: ignore[assignment]
        if src.ref is None:
            break
        src_table = norm(src.ref.table)
        if src_table not in staging_tables:
            break  # reached a real source table — stop

        # Find the step that writes to this staging table, EXCLUDING the current
        # step to prevent self-referential infinite loops (e.g. STEP5 whose
        # name == its own source table SSDS_AVY_FACT_STEP5_STG).
        prev_step = next(
            (s for s in model.staging_steps
             if norm(s.name) == src_table and s.step_id != step_id),
            None,
        )
        src_col = norm(src.column)

        if prev_step is None:
            # Self-referential or unknown writer: search all earlier steps
            # (highest step_id first) for the same column name.
            fallback: Optional[tuple[int, ColumnMapping]] = None
            for s in steps_desc:
                if s.step_id >= step_id:
                    continue
                match = next((c for c in s.column_mappings if norm(c.target_col) == src_col), None)
                if match is not None:
                    fallback = (s.step_id, match)
                    break
            if fallback is None:
                break
            step_id, cm = fallback
            continue

        # Normal case: look for the column in the previous step's mappings
        prev_cm = next(
            (c for c in prev_step.column_mappings if norm(c.target_col) == src_col),
            None,
        )
        if prev_cm is None:
            break

        step_id = prev_step.step_id
        cm = prev_cm

    return step_id, cm


def _alias_map_for_step(step: StagingStep) -> dict[str, AliasBinding]:
    """Build alias -> AliasBinding for a single step's source bindings."""
    return {b.alias: b for b in step.source_bindings}


def _step_has_table(table_norm: str, staging_steps: list[StagingStep]) -> bool:
    """Return True if *table_norm* (already norm()'d) appears in any step's
    source_bindings as either a physical table name or an alias name."""
    for step in staging_steps:
        for b in step.source_bindings:
            if norm(b.ref.table) == table_norm or norm(b.alias) == table_norm:
                return True
    return False


# ── Core comparison ───────────────────────────────────────────────────────────

def compare_drd_odi(
    drd: DrdClaim,
    model: ODIModel,
    _staging_tables: Optional[set] = None,
    kb: Optional["KBLookup"] = None,
    _final_insert_cols: Optional[frozenset] = None,
) -> ComparisonResult:
    """Compare a single DRD row claim against the ODI model.

    Returns a ComparisonResult with a ComparisonVerdict and full evidence.
    Uses lineage tracing to follow staging-table pass-throughs back to the
    original non-staging source before comparing.
    """
    col = drd.target_col or drd.source_attr

    # Pre-compute staging table names once per batch (passed in by caller)
    if _staging_tables is None:
        _staging_tables = _get_staging_table_names(model)

    # Pre-compute DRD-side facts used by multiple branches.  ``effective_rule_text``
    # includes any cross-tab ETL block body (P3) so multi-line APACSH / APASEC /
    # etc. logic flows into the comparison instead of just the brief
    # "Use X logic from ETL Notes" placeholder.
    _drd_effective_rule = drd.effective_rule_text
    _drd_logic_raw = _drd_effective_rule or drd.source_attr
    _drd_is_complex = _drd_rule_is_complex(_drd_effective_rule)
    # P0.5: parse the DRD col-AD cell (+ ETL block body, if any) once per
    # column.  Used to detect JOIN_DRIFT.
    _drd_ad_rule: DrdAdRule = parse_drd_ad(_drd_effective_rule)

    # ── Find the column in the ODI model (with staging lineage tracing) ──────
    found = _trace_to_original_source(col, model, staging_tables=_staging_tables)
    if found is None:
        # Lazily compute final_insert_columns set (passed in by caller for batch)
        if _final_insert_cols is None:
            _final_insert_cols = frozenset(norm(c) for c in model.final_insert_columns)
        in_final = norm(col) in _final_insert_cols

        # Generic text-search fallback: STEP_INSERT may have no explicit column
        # list, but the SELECT body still projects the column.  Scan staging
        # step select_sql text for `<expr> AS <col>` / `<expr> <col>`.  Then
        # recursively follow ``<staging_table>.<col>`` pass-throughs across
        # steps until we hit a real source expression.
        text_found = _follow_staging_chain_text(model, col, _staging_tables)

        if text_found is not None:
            ts_step_id, ts_expr = text_found
            ts_expr_upper = ts_expr.upper()

            # Shared rule engine: EXISTS-derived flag MATCH via MAX(CASE).
            _exists_spec_ts = extract_exists_derived_flag(drd.transformation)
            if _exists_spec_ts is not None:
                _has_max_case_ts = "MAX" in ts_expr_upper and "CASE" in ts_expr_upper
                _set_val_up = _exists_spec_ts["set_value"].upper()
                _has_set_val_ts = f"'{_set_val_up}'" in ts_expr_upper
                if _has_max_case_ts and _has_set_val_ts:
                    return ComparisonResult(
                        verdict=ComparisonVerdict.MATCHED,
                        target_col=col,
                        drd_schema=drd.source_schema,
                        drd_table=drd.source_table,
                        drd_attr=drd.source_attr,
                        odi_schema="",
                        odi_table="",
                        odi_col="",
                        odi_expr_sql=ts_expr,
                        odi_step=ts_step_id,
                        explanation=(
                            f"MATCHED via EXISTS<->MAX(CASE): both DRD and ODI flag "
                            f"existence in {_exists_spec_ts['table']} -> '{_exists_spec_ts['set_value']}'"
                        ),
                        mismatch_kind=MismatchKind.NONE,
                        drd_logic=_drd_effective_rule or drd.transformation,
                        odi_logic=ts_expr,
                    )

            # Shared rule engine: when DRD says "Applicable only for <CODE>",
            # check whether ODI's projection text reflects the same filter.
            _ap_code = extract_applicable_only_code(drd.transformation)
            if _ap_code and f"'{_ap_code}'" in ts_expr_upper and "CASE" in ts_expr_upper:
                # ODI implements the CASE filter on the same CODE -> match.
                return ComparisonResult(
                    verdict=ComparisonVerdict.MATCHED,
                    target_col=col,
                    drd_schema=drd.source_schema,
                    drd_table=drd.source_table,
                    drd_attr=drd.source_attr,
                    odi_schema="",
                    odi_table="",
                    odi_col="",
                    odi_expr_sql=ts_expr,
                    odi_step=ts_step_id,
                    explanation=(
                        f"MATCHED via applicable-filter: both DRD and ODI restrict "
                        f"to '{_ap_code}'"
                    ),
                    mismatch_kind=MismatchKind.NONE,
                    drd_logic=_drd_logic_raw,
                    odi_logic=ts_expr,
                )
            # If DRD demands a filter and ODI projection has neither CASE nor
            # the code literal -> APPLICABLE_FILTER_DRIFT.
            if _ap_code:
                _discrim = (
                    find_discriminator_for_code(drd.etl_block_body, _ap_code)
                    or find_discriminator_for_code(drd.all_etl_text, _ap_code)
                )
                if _discrim is not None and (
                    "CASE" not in ts_expr_upper or f"'{_ap_code}'" not in ts_expr_upper
                ):
                    expected = (
                        f"CASE WHEN {_discrim[0]}.{_discrim[1].upper()} = "
                        f"'{_ap_code}' THEN <source_expr> ELSE NULL END"
                    )
                    return ComparisonResult(
                        verdict=ComparisonVerdict.REAL_MISMATCH,
                        target_col=col,
                        drd_schema=drd.source_schema,
                        drd_table=drd.source_table,
                        drd_attr=drd.source_attr,
                        odi_schema="",
                        odi_table="",
                        odi_col="",
                        odi_expr_sql=ts_expr,
                        odi_step=ts_step_id,
                        explanation=(
                            f"APPLICABLE_FILTER_DRIFT: DRD requires CASE filter on "
                            f"{_discrim[0]}.{_discrim[1]} = '{_ap_code}'; ODI projects an "
                            f"unfiltered expression"
                        ),
                        mismatch_kind=MismatchKind.APPLICABLE_FILTER_DRIFT,
                        drd_logic=expected,
                        odi_logic=ts_expr,
                    )
            odi_is_simple = _odi_expr_is_simple(ts_expr)
            if drd.has_source and _drd_is_complex and odi_is_simple:
                # Generic TRANSFORMATION_DRIFT: DRD requires derivation,
                # ODI does pass-through.
                return ComparisonResult(
                    verdict=ComparisonVerdict.REAL_MISMATCH,
                    target_col=col,
                    drd_schema=drd.source_schema,
                    drd_table=drd.source_table,
                    drd_attr=drd.source_attr,
                    odi_schema="",
                    odi_table="",
                    odi_col="",
                    odi_expr_sql=ts_expr,
                    odi_step=ts_step_id,
                    explanation=(
                        "TRANSFORMATION_DRIFT: DRD describes derivation logic "
                        "but ODI projects a simple pass-through"
                    ),
                    mismatch_kind=MismatchKind.TRANSFORMATION_DRIFT,
                    drd_logic=_drd_logic_raw,
                    odi_logic=ts_expr,
                )
            # ODI projection found via text but no drift signal yet -> defer
            # to UNRESOLVABLE; surface both sides so operator sees the gap.
            return ComparisonResult(
                verdict=ComparisonVerdict.UNRESOLVABLE,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema="",
                odi_table="",
                odi_col="",
                odi_expr_sql=ts_expr,
                odi_step=ts_step_id,
                explanation=(
                    "Column projected by ODI but staging chain could not be traced"
                ),
                unresolved_reason="ODI_PROJECTION_FOUND_VIA_TEXT_SEARCH",
                drd_logic=_drd_logic_raw,
                odi_logic=ts_expr,
            )

        if in_final:
            # ODI knows about the column (it's in the MERGE block) but neither
            # column_mappings nor text-search recovered an expression.
            pdm_target_confirmed = False
            pdm_col_name = ""
            if kb is not None:
                try:
                    target_cols = kb.get_columns(model.target)
                    if target_cols and norm(col) in target_cols:
                        pdm_target_confirmed = True
                        pdm_col_name = col
                except Exception as exc:
                    logger.warning("KB lookup failed for %s: %s", col, exc)
            step_has_drd_tbl = (
                _step_has_table(norm(drd.source_table), model.staging_steps)
                if drd.source_table else False
            )
            parts: list[str] = [
                f"Column {col!r} is present in ODI MERGE block"
                " but staging chain could not be traced"
            ]
            if drd.source_table:
                if step_has_drd_tbl:
                    parts.append(
                        f"DRD source table {drd.source_table!r} IS present in ODI step JOINs"
                    )
                else:
                    parts.append(
                        f"DRD source table {drd.source_table!r} NOT found in any ODI step JOINs"
                    )
            if pdm_target_confirmed:
                parts.append("target column confirmed in PDM")
            return ComparisonResult(
                verdict=ComparisonVerdict.UNRESOLVABLE,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema="",
                odi_table="",
                odi_col="",
                odi_expr_sql="",
                odi_step=0,
                explanation=" -- ".join(parts),
                unresolved_reason="ODI_COLUMN_IN_FINAL_SOURCE_NOT_TRACED",
                pdm_target_confirmed=pdm_target_confirmed,
                pdm_col_name=pdm_col_name,
                drd_logic=_drd_logic_raw,
                odi_logic="",
            )

        # Column not found anywhere in ODI (not in staging steps, not in MERGE
        # block, not via text search) -- genuine SOURCE_MISSING.
        pdm_confirmed = False
        pdm_col_name = ""
        if kb is not None:
            try:
                target_cols = kb.get_columns(model.target)
                if target_cols and norm(col) in target_cols:
                    pdm_confirmed = True
                    pdm_col_name = col
            except Exception as exc:
                logger.warning("KB lookup failed for %s: %s", col, exc)
        return ComparisonResult(
            verdict=ComparisonVerdict.SOURCE_MISSING,
            target_col=col,
            drd_schema=drd.source_schema,
            drd_table=drd.source_table,
            drd_attr=drd.source_attr,
            odi_schema="",
            odi_table="",
            odi_col="",
            odi_expr_sql="",
            odi_step=0,
            explanation=(
                f"Column {col!r} not found in any ODI staging step or MERGE block"
                + (" -- column EXISTS in PDM target (ODI mapping not emitted)" if pdm_confirmed else "")
            ),
            pdm_confirmed=pdm_confirmed,
            pdm_col_name=pdm_col_name,
            drd_logic=_drd_logic_raw,
            odi_logic="",
        )

    step_id, cm = found

    # ── ODI side: UnresolvedExpr → UNRESOLVABLE ────────────────────────────
    if isinstance(cm.source, UnresolvedExpr):
        pdm_target_confirmed = False
        pdm_col_name = ""
        if kb is not None:
            try:
                target_cols = kb.get_columns(model.target)
                if target_cols and norm(col) in target_cols:
                    pdm_target_confirmed = True
                    pdm_col_name = col
            except Exception as exc:
                logger.warning("KB lookup failed for %s: %s", col, exc)
        return ComparisonResult(
            verdict=ComparisonVerdict.UNRESOLVABLE,
            target_col=col,
            drd_schema=drd.source_schema,
            drd_table=drd.source_table,
            drd_attr=drd.source_attr,
            odi_schema="",
            odi_table="",
            odi_col="",
            odi_expr_sql=cm.source.original_expr,
            odi_step=step_id,
            explanation=(
                f"ODI expression could not be resolved: {cm.source.reason}"
                + (" -- target column confirmed in PDM" if pdm_target_confirmed else "")
            ),
            unresolved_reason=f"{cm.source.reason}: {cm.source.detail}",
            pdm_target_confirmed=pdm_target_confirmed,
            pdm_col_name=pdm_col_name,
            drd_logic=_drd_logic_raw,
            odi_logic=cm.source.original_expr,
        )

    src: ResolvedColumn = cm.source  # type: ignore[assignment]
    _odi_logic_raw = src.expr_sql or src.original_expr or ""
    # Normalize redundant CASE wrappers so the structural compare sees the
    # real underlying column ref.
    _odi_logic_raw = _normalize_case_when_redundant(_odi_logic_raw)

    # If the resolved trace landed on a staging table, follow the chain via
    # text search until we reach a real source.  This handles the case where
    # ODI's MERGE block sources from STEP5_STG_RT which is itself a pointer
    # to an earlier step's projection.
    if src.ref is not None and norm(src.ref.table) in _staging_tables:
        chain = _follow_staging_chain_text(model, col, _staging_tables)
        if chain is not None:
            ts_id, ts_expr = chain
            ts_expr_norm = _normalize_case_when_redundant(ts_expr)
            _odi_logic_raw = ts_expr_norm
            # If the chain landed on a bare <real_table>.<real_col>, rebuild
            # src so the structural compare sees the real source.
            m = _STAGING_REF_RE.match(ts_expr_norm)
            if m is not None:
                real_table = m.group(1).upper()
                real_col = m.group(2).upper()
                if real_table not in _staging_tables:
                    # Try to look up the schema from the step's source_bindings
                    real_schema = ""
                    step_for_resolve = model.step(ts_id)
                    if step_for_resolve is not None:
                        for b in step_for_resolve.source_bindings:
                            if b.ref.table == real_table:
                                real_schema = b.ref.schema
                                break
                    try:
                        new_ref = TableRef(schema=real_schema, table=real_table)
                        src = ResolvedColumn(
                            expr_sql=ts_expr_norm,
                            provenance=src.provenance,
                            ref=new_ref,
                            column=real_col,
                            original_expr=src.original_expr,
                        )
                        step_id = ts_id
                    except Exception:
                        pass

    # ── Shared rule engine: EXISTS-derived flag MATCH ───────────────────────
    # When DRD says ``If there is a record in T with <preds> then set to '<V>'``
    # and ODI projects ``(MAX((CASE WHEN <similar_preds> THEN '<V>' ...)))``,
    # the two are semantically the same boolean flag.  Generic: no specific
    # table / column / value hard-coded.
    _exists_spec = extract_exists_derived_flag(drd.transformation)
    if _exists_spec is not None and _odi_logic_raw:
        _odi_text_up = _odi_logic_raw.upper()
        _has_max_case = "MAX" in _odi_text_up and "CASE" in _odi_text_up
        _has_set_value = f"'{_exists_spec['set_value'].upper()}'" in _odi_text_up
        if _has_max_case and _has_set_value:
            return ComparisonResult(
                verdict=ComparisonVerdict.MATCHED,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema=src.ref.schema if src.ref else "",
                odi_table=src.ref.table if src.ref else "",
                odi_col=src.column,
                odi_expr_sql=_odi_logic_raw,
                odi_step=step_id,
                explanation=(
                    f"MATCHED via EXISTS<->MAX(CASE) equivalence: both flag "
                    f"existence of record in {_exists_spec['table']} -> '{_exists_spec['set_value']}'"
                ),
                mismatch_kind=MismatchKind.NONE,
                drd_logic=_drd_effective_rule or drd.transformation or drd.source_attr,
                odi_logic=_odi_logic_raw,
            )

    # ── Shared rule engine: APPLICABLE-filter MATCH / DRIFT ─────────────────
    # When DRD says "Applicable only for <CODE>" AND ODI's expression text
    # contains the same code literal inside a CASE, that's a structural MATCH.
    # When ODI lacks the CASE/code reference, it's APPLICABLE_FILTER_DRIFT.
    _ap_code_resolved = extract_applicable_only_code(drd.transformation)
    if _ap_code_resolved and _odi_logic_raw:
        _odi_text_up = _odi_logic_raw.upper()
        _has_case = "CASE" in _odi_text_up
        _has_code = f"'{_ap_code_resolved}'" in _odi_text_up
        if _has_case and _has_code:
            return ComparisonResult(
                verdict=ComparisonVerdict.MATCHED,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema=src.ref.schema if src.ref else "",
                odi_table=src.ref.table if src.ref else "",
                odi_col=src.column,
                odi_expr_sql=_odi_logic_raw,
                odi_step=step_id,
                explanation=(
                    f"MATCHED via applicable-filter: both DRD and ODI restrict "
                    f"to '{_ap_code_resolved}'"
                ),
                mismatch_kind=MismatchKind.NONE,
                drd_logic=_drd_effective_rule or drd.transformation or drd.source_attr,
                odi_logic=_odi_logic_raw,
            )
        # ODI doesn't match the required code -> drift (only when we can
        # resolve a discriminator -- otherwise we lack the structural info).
        _discrim_resolved = (
            find_discriminator_for_code(drd.etl_block_body, _ap_code_resolved)
            or find_discriminator_for_code(drd.all_etl_text, _ap_code_resolved)
        )
        if _discrim_resolved is not None and not (_has_case and _has_code):
            expected = (
                f"CASE WHEN {_discrim_resolved[0]}.{_discrim_resolved[1].upper()} = "
                f"'{_ap_code_resolved}' THEN <source_expr> ELSE NULL END"
            )
            return ComparisonResult(
                verdict=ComparisonVerdict.REAL_MISMATCH,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema=src.ref.schema if src.ref else "",
                odi_table=src.ref.table if src.ref else "",
                odi_col=src.column,
                odi_expr_sql=_odi_logic_raw,
                odi_step=step_id,
                explanation=(
                    f"APPLICABLE_FILTER_DRIFT: DRD requires CASE filter on "
                    f"{_discrim_resolved[0]}.{_discrim_resolved[1]} = '{_ap_code_resolved}'; "
                    f"ODI projection does not match"
                ),
                mismatch_kind=MismatchKind.APPLICABLE_FILTER_DRIFT,
                drd_logic=expected,
                odi_logic=_odi_logic_raw,
            )

    # ── DRD side: no source claim ─────────────────────────────────────────────
    if not drd.has_source:
        if src.provenance == Provenance.LITERAL:
            return ComparisonResult(
                verdict=ComparisonVerdict.MATCHED,
                target_col=col,
                drd_schema="",
                drd_table="",
                drd_attr="",
                odi_schema="",
                odi_table="",
                odi_col="",
                odi_expr_sql=src.expr_sql,
                odi_step=step_id,
                explanation="DRD has no source claim; ODI uses a literal expression (OK)",
                drd_logic=_drd_logic_raw,
                odi_logic=_odi_logic_raw,
            )
        return ComparisonResult(
            verdict=ComparisonVerdict.UNRESOLVABLE,
            target_col=col,
            drd_schema="",
            drd_table="",
            drd_attr="",
            odi_schema=src.ref.schema if src.ref else "",
            odi_table=src.ref.table if src.ref else "",
            odi_col=src.column,
            odi_expr_sql=src.expr_sql,
            odi_step=step_id,
            explanation="DRD has no source attribute — unclear rule; cannot verify",
            unresolved_reason="UNCLEAR_RULE",
            drd_logic=_drd_logic_raw,
            odi_logic=_odi_logic_raw,
        )

    # ── P0.5: JOIN_DRIFT short-circuit (priority over TRANSFORMATION_DRIFT) ──
    # When the DRD col-AD encodes specific join predicate(s), that's the
    # primary contract.  If ODI's actual join graph doesn't satisfy them,
    # surface JOIN_DRIFT *before* checking pass-through semantics.
    _early_step_obj = model.step(step_id)
    _drd_joins_all_satisfied = False
    if (
        drd.has_source
        and _early_step_obj is not None
        and (_drd_ad_rule.joins or _drd_ad_rule.lookup_pairs)
    ):
        _odi_on_sqls = [e.on_sql for e in _early_step_obj.join_graph if e.on_sql]
        _cmp = compare_drd_ad_joins(_drd_ad_rule, _odi_on_sqls)
        if _cmp["any_required"] and not _cmp["all_satisfied"]:
            _missing = ", ".join(
                f"{p.left}={p.right}" for p in _cmp["unsatisfied"]
            )
            return ComparisonResult(
                verdict=ComparisonVerdict.REAL_MISMATCH,
                target_col=col,
                drd_schema=drd.source_schema,
                drd_table=drd.source_table,
                drd_attr=drd.source_attr,
                odi_schema=src.ref.schema if src.ref else "",
                odi_table=src.ref.table if src.ref else "",
                odi_col=src.column,
                odi_expr_sql=_odi_logic_raw,
                odi_step=step_id,
                explanation=(
                    f"JOIN_DRIFT: DRD requires join predicate(s) not present in ODI: {_missing}"
                ),
                mismatch_kind=MismatchKind.JOIN_DRIFT,
                drd_logic=_drd_logic_raw,
                odi_logic=_odi_logic_raw,
            )
        if _cmp["all_satisfied"]:
            _drd_joins_all_satisfied = True

    # ── TRANSFORMATION_DRIFT detector ─────────────────────────────────────────
    # If DRD describes derivation logic (CASE/parse/lookup/multi-line) but ODI
    # projects a simple pass-through (or a complex expression but the DRD says
    # plain pass-through), surface that explicitly so the operator can see the
    # disagreement before the column / table comparison overrides it.
    #
    # Exception: if the DRD rule's complexity is purely a JOIN spec that ODI
    # already satisfies, that's NOT transformation drift -- the join *is* the
    # rule and ODI implements it correctly.  We re-evaluate "complex" on the
    # remainder of the rule text after stripping JOIN fragments.
    _drd_is_complex_after_joins = _drd_is_complex
    if _drd_joins_all_satisfied and _drd_is_complex:
        # Re-evaluate complexity on the residual rule text with JOIN clauses
        # masked out.  If nothing complex remains, the join WAS the rule.
        residual = drd.transformation or ""
        for j in _drd_ad_rule.joins:
            residual = residual.replace(j.raw, " ")
        for lp in _drd_ad_rule.lookup_pairs:
            residual = residual.replace(lp.raw, " ")
        if not _drd_rule_is_complex(residual):
            _drd_is_complex_after_joins = False

    _odi_is_simple = _odi_expr_is_simple(_odi_logic_raw) or (
        src.ref is not None and src.provenance != Provenance.LITERAL
    )
    if drd.has_source and _drd_is_complex_after_joins and _odi_is_simple:
        return ComparisonResult(
            verdict=ComparisonVerdict.REAL_MISMATCH,
            target_col=col,
            drd_schema=drd.source_schema,
            drd_table=drd.source_table,
            drd_attr=drd.source_attr,
            odi_schema=src.ref.schema if src.ref else "",
            odi_table=src.ref.table if src.ref else "",
            odi_col=src.column,
            odi_expr_sql=_odi_logic_raw,
            odi_step=step_id,
            explanation=(
                "TRANSFORMATION_DRIFT: DRD describes derivation logic "
                "but ODI projects a simple pass-through"
            ),
            mismatch_kind=MismatchKind.TRANSFORMATION_DRIFT,
            drd_logic=_drd_logic_raw,
            odi_logic=_odi_logic_raw,
        )

    # ── Complex ODI expression (CASE/NVL/arithmetic): ref is None ────────────
    if src.ref is None:
        # A complex expression.  We cannot verify structural equality without
        # executing the SQL, but we enrich the explanation with DRD context:
        # the DRD transformation rule text + whether the DRD-claimed source
        # table actually appears in the ODI step JOINs.
        step_has_drd_tbl = (
            _step_has_table(norm(drd.source_table), model.staging_steps)
            if drd.source_table else False
        )
        _parts: list[str] = []
        if drd.transformation:
            _parts.append(f"ODI uses a complex expression; DRD rule: {drd.transformation}")
        else:
            _parts.append(
                "ODI uses a complex expression (NVL/CASE/arithmetic) -- manual verify required"
            )
        if drd.source_table:
            if step_has_drd_tbl:
                _parts.append(
                    f"DRD source table {drd.source_table!r} IS present in ODI step JOINs"
                )
            else:
                _parts.append(
                    f"DRD source table {drd.source_table!r} NOT found in any ODI step JOINs"
                    " -- verify lookup/join chain matches DRD"
                )
        return ComparisonResult(
            verdict=ComparisonVerdict.UNRESOLVABLE,
            target_col=col,
            drd_schema=drd.source_schema,
            drd_table=drd.source_table,
            drd_attr=drd.source_attr,
            odi_schema="",
            odi_table="",
            odi_col="",
            odi_expr_sql=src.expr_sql,
            odi_step=step_id,
            explanation=" -- ".join(_parts),
            unresolved_reason="COMPLEX_EXPRESSION",
            drd_logic=_drd_logic_raw,
            odi_logic=_odi_logic_raw,
        )

    # ── Both sides have concrete source: structural comparison ────────────────
    odi_schema = src.ref.schema
    odi_table = src.ref.table
    odi_col = src.column

    drd_table_up = norm(drd.source_table)
    drd_attr_up = norm(drd.source_attr)

    # Column name check (the most important signal)
    col_matches = odi_col == drd_attr_up

    # Table check: DRD may use the alias name OR the physical table name.
    # Build alias map for this step to check both.
    step_obj = model.step(step_id)
    alias_evidence = ""
    table_matches_physical = (odi_table == drd_table_up)
    table_matches_alias = False

    if step_obj is not None:
        amap = _alias_map_for_step(step_obj)
        # Does drd_table_up match an alias that points to odi_table?
        if drd_table_up in amap:
            binding = amap[drd_table_up]
            if binding.ref.table == odi_table:
                table_matches_alias = True
                alias_evidence = f"alias {drd_table_up} -> {binding.ref.fq}"
        # Also: does odi_table have an alias that matches drd_table?
        for alias, binding in amap.items():
            if binding.ref.table == odi_table and alias == drd_table_up:
                table_matches_alias = True
                alias_evidence = f"alias {alias} -> {binding.ref.fq}"
                break

    table_matches = table_matches_physical or table_matches_alias

    # Initialized here so all branches can reference it in the final return.
    pdm_col_name_miss = ""
    mismatch_kind = MismatchKind.NONE

    # ── P0.5: JOIN_DRIFT detector ────────────────────────────────────────────
    # If the DRD col-AD declares required join predicates AND the ODI step's
    # actual ON clauses do not satisfy them, surface that as JOIN_DRIFT.  We
    # check regardless of column/table match so the operator can see the gap.
    join_drift_unsatisfied: list = []
    if step_obj is not None and (_drd_ad_rule.joins or _drd_ad_rule.lookup_pairs):
        odi_on_sqls = [edge.on_sql for edge in step_obj.join_graph if edge.on_sql]
        cmp = compare_drd_ad_joins(_drd_ad_rule, odi_on_sqls)
        if cmp["any_required"] and not cmp["all_satisfied"]:
            join_drift_unsatisfied = cmp["unsatisfied"]

    if col_matches and table_matches:
        verdict = ComparisonVerdict.MATCHED
        explanation = (
            f"MATCHED: ODI source {odi_schema}.{odi_table}.{odi_col} == DRD {drd.source_table}.{drd.source_attr}"
            + (f" (via {alias_evidence})" if alias_evidence else "")
        )
    elif col_matches and not table_matches:
        # Same column name, different table. Could be:
        # - An ODI alias the DRD didn't document (ALIAS_DRIFT_ONLY)
        # - A genuinely different table (REAL_MISMATCH)
        # Heuristic: if the column name is distinct (not generic like "ID"), treat
        # as ALIAS_DRIFT_ONLY; generic names (ID, NAME, CODE, TYPE_CODE) → REAL_MISMATCH.
        _generic = {"ID", "NAME", "CODE", "TYPE_CODE", "FLAG", "STATUS", "TYPE", "SEQ"}
        if drd_attr_up not in _generic:
            verdict = ComparisonVerdict.ALIAS_DRIFT_ONLY
            explanation = (
                f"ALIAS_DRIFT_ONLY: column {odi_col} matches but table differs: "
                f"ODI={odi_table} vs DRD={drd.source_table}"
            )
        else:
            verdict = ComparisonVerdict.REAL_MISMATCH
            mismatch_kind = MismatchKind.TABLE_MISMATCH
            explanation = (
                f"REAL_MISMATCH: generic column {odi_col} with different table: "
                f"ODI={odi_table} vs DRD={drd.source_table}"
            )
    else:
        verdict = ComparisonVerdict.REAL_MISMATCH
        # Distinguish: same table + different column == COLUMN_MISMATCH;
        # different table (any column) == TABLE_MISMATCH.
        mismatch_kind = (
            MismatchKind.COLUMN_MISMATCH if table_matches else MismatchKind.TABLE_MISMATCH
        )
        explanation = (
            f"REAL_MISMATCH: ODI={odi_table}.{odi_col} vs DRD={drd.source_table}.{drd.source_attr}"
        )
        # PDM dual-column check: if the ODI column IS in the PDM source table AND
        # the DRD column is NOT, then ODI uses the authoritative name and DRD uses
        # an alias/expanded form — upgrade to ALIAS_DRIFT_ONLY.
        if kb is not None:
            try:
                src_ref = TableRef(schema=odi_schema, table=odi_table)
                odi_in_pdm = kb.column_exists(src_ref, odi_col)
                drd_in_pdm = kb.column_exists(src_ref, drd_attr_up)
                if odi_in_pdm and not drd_in_pdm:
                    verdict = ComparisonVerdict.ALIAS_DRIFT_ONLY
                    pdm_col_name_miss = odi_col
                    explanation = (
                        f"ALIAS_DRIFT_ONLY (PDM): ODI={odi_table}.{odi_col} is in PDM; "
                        f"DRD={drd.source_table}.{drd.source_attr} is not — "
                        f"DRD uses non-canonical name (PDM authoritative: {odi_col})"
                    )
            except Exception as exc:
                logger.warning("KB column lookup failed for %s.%s: %s", odi_table, odi_col, exc)

    # If MATCHED (or ALIAS_DRIFT_ONLY) but JOIN_DRIFT exists, upgrade to
    # REAL_MISMATCH + JOIN_DRIFT.  If already a column/table mismatch, JOIN_DRIFT
    # takes priority for the kind label because the join shape is upstream of
    # the column projection (operator can fix the join first, then re-verify).
    if join_drift_unsatisfied:
        if verdict in (ComparisonVerdict.MATCHED, ComparisonVerdict.ALIAS_DRIFT_ONLY):
            verdict = ComparisonVerdict.REAL_MISMATCH
        mismatch_kind = MismatchKind.JOIN_DRIFT
        _missing = ", ".join(
            f"{p.left}={p.right}" for p in join_drift_unsatisfied
        )
        explanation = (
            f"JOIN_DRIFT: DRD requires join predicate(s) not present in ODI: {_missing}"
        )

    return ComparisonResult(
        verdict=verdict,
        target_col=col,
        drd_schema=drd.source_schema,
        drd_table=drd.source_table,
        drd_attr=drd.source_attr,
        odi_schema=odi_schema,
        odi_table=odi_table,
        odi_col=odi_col,
        odi_expr_sql=src.expr_sql,
        odi_step=step_id,
        explanation=explanation,
        alias_evidence=alias_evidence,
        pdm_col_name=pdm_col_name_miss,
        mismatch_kind=mismatch_kind,
        drd_logic=_drd_logic_raw,
        odi_logic=_odi_logic_raw,
    )


# ── Batch comparison ──────────────────────────────────────────────────────────

def compare_drd_rows_to_model(
    drd_rows: list[dict],
    model: ODIModel,
    kb: Optional["KBLookup"] = None,
) -> list[ComparisonResult]:
    """Compare a list of DRD mapping rows against an ODIModel.

    Each row is a dict with keys: physical_name, source_schema, source_table,
    source_attribute, transformation (from parse_drd_file output).

    Returns one ComparisonResult per non-strike-through DRD row.
    Rows with empty physical_name (target column) are skipped.
    Pass kb to enable PDM enrichment (pdm_confirmed / pdm_target_confirmed /
    pdm_col_name fields on each ComparisonResult).
    """
    # Pre-compute staging tables and final insert columns once for the whole batch
    staging_tables = _get_staging_table_names(model)
    final_insert_cols: frozenset = frozenset(norm(c) for c in model.final_insert_columns)
    results: list[ComparisonResult] = []
    for row in drd_rows:
        drd = DrdClaim.from_dict(row)
        if not drd.target_col:
            continue
        results.append(
            compare_drd_odi(
                drd, model,
                _staging_tables=staging_tables,
                kb=kb,
                _final_insert_cols=final_insert_cols,
            )
        )
    return results


def comparison_summary(results: list[ComparisonResult]) -> dict:
    """Aggregate comparison results into a summary dict for the UI."""
    total = len(results)
    by_verdict: dict[str, int] = {}
    for r in results:
        key = r.verdict.value
        by_verdict[key] = by_verdict.get(key, 0) + 1

    mismatches = [r for r in results if r.verdict == ComparisonVerdict.REAL_MISMATCH]
    unresolvable = [r for r in results if r.verdict == ComparisonVerdict.UNRESOLVABLE]

    # PDM enrichment counts: how many of the "problem" verdicts are actually
    # explained by the PDM (column exists in target / complex expression but PDM confirms it)
    pdm_confirmed_count = sum(1 for r in results if r.pdm_confirmed)
    pdm_target_confirmed_count = sum(1 for r in results if r.pdm_target_confirmed)

    return {
        "total": total,
        "matched": by_verdict.get("MATCHED", 0),
        "alias_drift_only": by_verdict.get("ALIAS_DRIFT_ONLY", 0),
        "real_mismatch": by_verdict.get("REAL_MISMATCH", 0),
        "unresolvable": by_verdict.get("UNRESOLVABLE", 0),
        "source_missing": by_verdict.get("SOURCE_MISSING", 0),
        "ok_count": by_verdict.get("MATCHED", 0) + by_verdict.get("ALIAS_DRIFT_ONLY", 0),
        "error_count": by_verdict.get("REAL_MISMATCH", 0) + by_verdict.get("UNRESOLVABLE", 0) + by_verdict.get("SOURCE_MISSING", 0),
        "pdm_confirmed_count": pdm_confirmed_count,
        "pdm_target_confirmed_count": pdm_target_confirmed_count,
        "mismatch_targets": [r.target_col for r in mismatches],
        "unresolvable_targets": [r.target_col for r in unresolvable],
    }
