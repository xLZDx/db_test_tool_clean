"""Shared, generic DRD derivation-rule engine.

Operator-locked invariants (2026-05-29):
  * Every DRD-side rule lives HERE.  The SQL emitter and the DRD vs ODI
    comparator both import from this module; never duplicate.
  * Pure pattern detection -- no hard-coded table / column / schema / domain
    names.  Detectors describe shape (``<alias>.<col>`` near ``regexp_like``
    or ``BETWEEN``), never specific identifiers.
  * The same rule that EMITS SQL also computes the EXPECTED expression for
    comparison.  When the emitter wraps in ``CASE WHEN``, the comparator
    flags ODI as ``APPLICABLE_FILTER_DRIFT`` if ODI doesn't match.

Public surface:
  * ``extract_applicable_only_code(text) -> Optional[str]``
  * ``find_discriminator_for_code(haystack, code) -> Optional[(alias, col)]``
  * ``compute_drd_expected_expr(row, etl_text, fallback_source_expr) -> Optional[str]``

The same arbitrary-name unit tests exercise every detector to prove
genericness (no AVY_FACT / APACSH / etc names live in the regexes).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


# ── Generic identifier shape (no specific names!) ────────────────────────────

_IDENT = r"[A-Za-z][A-Za-z0-9_$#]*"


# ── "Applicable only for <CODE>" detector ────────────────────────────────────
#
# Operator-supplied DRD cells often carry an extra filter constraint of the
# shape ``Applicable only for <CODE>`` (or ``Applicable only to <CODE>``).
# The CODE is a generic uppercase identifier -- whatever the business team
# wrote.  We don't care what it MEANS; we only care that it's an exact value
# the downstream expression must filter on.

_APPLICABLE_ONLY_RE = re.compile(
    r"\bapplicable\s+only\s+(?:for|to)\s+([A-Z][A-Z0-9_]+)",
    re.IGNORECASE,
)


def extract_applicable_only_code(text: str) -> Optional[str]:
    """Return the captured upper-case code, or None if the text doesn't have
    an ``Applicable only for <CODE>`` clause.  Generic for any CODE shape."""
    if not text:
        return None
    m = _APPLICABLE_ONLY_RE.search(text)
    return m.group(1).upper() if m else None


# ── Discriminator extractors ─────────────────────────────────────────────────
#
# Three shapes detect a "what column gates the filter" reference.  Each is a
# pure pattern -- the detected identifier (alias.col) is returned verbatim.

_DISCRIMINATOR_REGEXP_RE = re.compile(
    r"\bregexp_like\s*\(\s*"
    rf"({_IDENT})\.({_IDENT})\s*,\s*"
    r"'\^?([A-Z][A-Z0-9_]+)",
    re.IGNORECASE,
)

_DISCRIMINATOR_EQ_RE = re.compile(
    rf"\b({_IDENT})\.({_IDENT})\s*=\s*'([A-Z][A-Z0-9_]+)'",
    re.IGNORECASE,
)

_DISCRIMINATOR_BETWEEN_RE = re.compile(
    rf"\b({_IDENT})\.({_IDENT})"
    r"\s+between\s+'([A-Z][A-Z0-9_]*?)\d+'\s+and\s+'([A-Z][A-Z0-9_]*?)\d+'",
    re.IGNORECASE,
)


def find_discriminator_for_code(
    haystack: str,
    code: str,
) -> Optional[Tuple[str, str]]:
    """Return ``(alias, col)`` of a discriminator whose pattern matches ``code``.

    Generic prefix-match: the value/pattern captured in ``haystack`` must
    either equal ``code`` exactly OR be a string-prefix of it (so a regex
    pattern ``^WIDGET[A-Z0-9]+`` matches code ``WIDGET42``).  No hardcoded
    block / table / column names.
    """
    if not haystack or not code:
        return None
    code_up = code.upper()

    # 1) regexp_like(<alias.col>, '^<PREFIX>...')
    for m in _DISCRIMINATOR_REGEXP_RE.finditer(haystack):
        alias, col, prefix = m.group(1), m.group(2), m.group(3).upper()
        if code_up.startswith(prefix):
            return (alias, col)

    # 2) <alias.col> = '<EXACT>'
    for m in _DISCRIMINATOR_EQ_RE.finditer(haystack):
        alias, col, val = m.group(1), m.group(2), m.group(3).upper()
        if val == code_up:
            return (alias, col)

    # 3) <alias.col> BETWEEN '<PREFIX_LO>NN' and '<PREFIX_HI>NN'
    for m in _DISCRIMINATOR_BETWEEN_RE.finditer(haystack):
        alias, col, lo_prefix, hi_prefix = (
            m.group(1), m.group(2), m.group(3).upper(), m.group(4).upper()
        )
        if lo_prefix == hi_prefix and code_up.startswith(lo_prefix):
            return (alias, col)

    return None


# ── Expected-expression composer (shared by emitter + comparator) ────────────

def compose_case_when_expr(
    discriminator: Tuple[str, str],
    code: str,
    inner_expr: str,
) -> str:
    """Build ``CASE WHEN <alias>.<col> = '<CODE>' THEN <inner_expr> ELSE NULL END``.

    Pure string formatting.  Caller is responsible for any alias rewriting
    needed to make ``<alias>`` valid in the surrounding query.
    """
    alias, col = discriminator
    return (
        f"CASE WHEN {alias}.{col.upper()} = '{code}' "
        f"THEN {inner_expr} ELSE NULL END"
    )


def compute_drd_expected_expr(
    row: Dict[str, Any],
    etl_haystack: str,
    fallback_source_expr: str,
) -> Optional[str]:
    """Compute what the DRD wants the column's projection to look like.

    Returns:
      * ``CASE WHEN ...`` string when ``Applicable only for <CODE>`` is
        present AND a discriminator can be found.
      * ``None`` when the DRD rule is just a plain pass-through (no
        applicable-only-for clause, no discriminator) -- caller should
        treat the absence of a rule as "no expected derivation".
    """
    transformation = (row.get("transformation") or "") if isinstance(row, dict) else ""
    code = extract_applicable_only_code(transformation)
    if not code:
        return None
    # Try the row's own ETL block body first, then the global haystack
    block_body = (row.get("etl_block_body") or "") if isinstance(row, dict) else ""
    discriminator = (
        find_discriminator_for_code(block_body, code)
        or find_discriminator_for_code(etl_haystack, code)
    )
    if discriminator is None:
        return None
    return compose_case_when_expr(discriminator, code, fallback_source_expr)


# ── Existence-derived flag rule ──────────────────────────────────────────────
#
# Many DRD cells describe derived flags / codes with a natural-language EXISTS
# pattern such as:
#
#   "If there is a record in CCAL_REPL_OWNER.TXN_RLTNP table with
#    TXN.TXN_ID = TXN_RLTNP.TRGT_TXN_ID
#    and TXN_RLTNP_TP_ID = 69 (Cancel)
#    and TXN_RLTNP.TRGT_TXN_ID <> TXN_RLTNP.SRC_TXN_ID
#    then set to 'Y' for both TRGT_TXN_ID and SRC_TXN_ID."
#
# Generic shape:
#   "If there is a record in <FQ_TABLE> ... with <predicates> ... then set to '<VALUE>' ..."
#
# Output:
#   CASE WHEN EXISTS (SELECT 1 FROM <FQ_TABLE> WHERE <predicates>) THEN '<VALUE>'
#        ELSE NULL END
#
# No specific column / business-domain names live in the regex.

_FQ_TABLE_2 = rf"{_IDENT}(?:\.{_IDENT}){{0,2}}"

_EXISTS_DERIVED_FLAG_RE = re.compile(
    r"\b(?:if\s+there\s+is\s+a\s+record\s+in|if\s+a\s+record\s+exists\s+in|"
    r"when\s+there\s+is\s+a\s+record\s+in)\s+"
    rf"({_FQ_TABLE_2})\b"
    r"(?:\s+table)?"
    r"(.+?)"
    r"\bthen\s+set\s+to\s+'([^']{1,30})'",
    re.IGNORECASE | re.DOTALL,
)

# ``<alias>.<col> <op> <alias>.<col>``  /  ``<alias>.<col> <op> '<val>'``
# /  ``<alias>.<col> <op> <number>``  -- one equality / comparison predicate.
_PRED_LINE_RE = re.compile(
    rf"({_IDENT}(?:\.{_IDENT})?)\s*"
    r"(=|<>|!=|>=|<=|>|<)\s*"
    rf"(?:'([^']*)'|(-?\d+(?:\.\d+)?)|({_IDENT}(?:\.{_IDENT})?))",
    re.IGNORECASE,
)


def _extract_predicates(text: str) -> list[str]:
    """Best-effort extraction of equality/comparison predicates from a free-text
    DRD chunk.  Returns one predicate per match in source order.
    """
    out: list[str] = []
    seen: set = set()
    for m in _PRED_LINE_RE.finditer(text or ""):
        lhs = m.group(1)
        op = m.group(2)
        rhs = m.group(3) if m.group(3) is not None else (m.group(4) or m.group(5) or "")
        if not rhs:
            continue
        if m.group(3) is not None:
            rhs_sql = f"'{rhs}'"
        elif m.group(4) is not None:
            rhs_sql = rhs
        else:
            rhs_sql = rhs
        pred = f"{lhs} {op} {rhs_sql}"
        key = pred.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(pred)
    return out


def extract_exists_derived_flag(text: str) -> Optional[Dict[str, Any]]:
    """Detect an EXISTS-style derived-flag DRD rule.

    Returns a dict ``{table, predicates, set_value, raw}`` when a match is
    found, ``None`` otherwise.  Generic for any table name / column names /
    value.
    """
    if not text:
        return None
    m = _EXISTS_DERIVED_FLAG_RE.search(text)
    if not m:
        return None
    fq_table = m.group(1).strip()
    body = m.group(2) or ""
    set_value = m.group(3).strip()
    predicates = _extract_predicates(body)
    if not predicates:
        return None
    # Normalize table fq -- last 2 segments
    parts = [p for p in fq_table.split(".") if p]
    if len(parts) > 2:
        fq_table = ".".join(parts[-2:])
    return {
        "table": fq_table.upper(),
        "predicates": predicates,
        "set_value": set_value,
        "raw": m.group(0),
    }


def compose_exists_case_expr(spec: Dict[str, Any], else_value: str = "NULL") -> str:
    """Build ``CASE WHEN EXISTS (SELECT 1 FROM <T> WHERE <preds>) THEN '<V>' ELSE <ELSE> END``.

    ``else_value`` accepts ``NULL`` (default) or a quoted literal like ``'N'``.
    """
    where_sql = " AND ".join(spec["predicates"])
    set_value = spec["set_value"]
    set_sql = f"'{set_value}'" if not (
        set_value.startswith("'") and set_value.endswith("'")
    ) else set_value
    return (
        f"CASE WHEN EXISTS (SELECT 1 FROM {spec['table']} WHERE {where_sql}) "
        f"THEN {set_sql} ELSE {else_value} END"
    )


# ── T-alias hint detector ────────────────────────────────────────────────────
#
# DRD source_attribute cells sometimes carry a trailing parenthetical hint that
# disambiguates self-joins, e.g.:
#
#     TXN.SRC_STM_ID (FROM T2)
#     SUM_AMT (FROM T1)
#     CCY_CD (FROM T_RELATED)
#
# The hint says: "this column projects from the Nth reference to the source
# table (or from a specific alias), not the base reference."  We don't care
# WHICH alias — only that the cell signals a non-base reference.  The emitter
# uses this signal to prefer the ODI staging chain's alias verbatim instead of
# re-aliasing to the canonical base.
#
# Generic shape: ``(FROM <ident>)`` at end of cell, with ``ident`` being any
# alphanumeric token (``T2``, ``T_RELATED``, ``TRGT``, etc.).

_T_ALIAS_HINT_RE = re.compile(
    r"\(\s*FROM\s+(" + _IDENT + r")\s*\)\s*$",
    re.IGNORECASE,
)


def extract_t_alias_hint(text: str) -> Optional[str]:
    """Return the captured alias-hint token (upper-cased), or ``None``.

    Examples:
        "TXN.SRC_STM_ID (FROM T2)"       -> "T2"
        "CCY_CD (FROM T_RELATED)"         -> "T_RELATED"
        "AMT (from t1)"                   -> "T1"
        "X.Y"                             -> None
        "SUM(CASE WHEN ... )"             -> None  (not a trailing FROM-hint)

    The detector is anchored at end-of-string and requires ``FROM`` as the
    parenthetical's first token, so Oracle function calls like
    ``TO_DATE(LOAD_DT,'YYYYMMDD')`` are NOT matched.
    """
    if not text:
        return None
    m = _T_ALIAS_HINT_RE.search(text)
    return m.group(1).upper() if m else None


# ── Generic ETL-default placeholder map (operator-overridable) ───────────────
#
# Conventional system-managed audit columns.  This is a DEFAULT; callers can
# pass their own dict to override / extend without touching this module.

DEFAULT_ETL_COLUMN_VALUES: Dict[str, str] = {
    "CRT_DTM": "SYSDATE",
    "LAST_UDT_DTM": "SYSDATE",
    "CRT_USR_NM": "'ETL'",
    "LAST_UDT_USR_NM": "'ETL'",
    "ACTV_F": "'Y'",
    "BATCH_DT": "TRUNC(SYSDATE)",
    "SESS_NO": "0",
}
