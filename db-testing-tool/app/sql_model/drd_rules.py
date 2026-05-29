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


# ── Unimplementable-rule prose detector ──────────────────────────────────────
#
# Some DRD cells describe a derivation that the spec author has explicitly
# called out as not auto-generatable from the source schema as-is.  Common
# wording (no business names hardcoded):
#
#     "Parse to extract <thing>"
#     "Lookup <X> (does not exist today)"
#     "Translation table required (TBD)"
#     "Manual mapping required"
#     "Derive based on <prose...>"  (only when no JOIN is given)
#
# When these markers are present, the emitter must NOT fall through to a
# DRD_PHYSICAL pass-through of the row's stated source_table.column_attr --
# that would generate SQL that compiles but returns garbage / NULL.  Better
# to emit NULL with the operator-visible note.

# Accept either digits ("first 3 chars") OR spelled-out cardinals
# ("first three chars").  Cardinals capped at "ten" to keep the regex tight.
_CARDINAL_OR_DIGITS = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
)

_UNIMPLEMENTABLE_RULE_RE = re.compile(
    r"(?:"
    r"\bparse\s+(?:to\s+)?extract\b"             # "parse to extract ..."
    r"|\bdoes\s+not\s+exist\s+(?:today|yet)\b"    # "does not exist today"
    r"|\btranslation\s+table\s+required\b"
    r"|\bmanual\s+(?:mapping|translation)\s+required\b"
    r"|\btbd\b\s*[,;:.]?\s*(?:manual|lookup|translation)?"
    r"|\bsubstring\s+of\s+"                       # "substring of <X>"
    r"|\bfirst\s+" + _CARDINAL_OR_DIGITS + r"\s+(?:chars|characters|digits)\b"
    r"|\blast\s+" + _CARDINAL_OR_DIGITS + r"\s+(?:chars|characters|digits)\b"
    r"|\badd\s+century\s+part\b"
    r")",
    re.IGNORECASE,
)


def is_unimplementable_prose_rule(text: str) -> bool:
    """Return True if the DRD transformation cell contains markers that
    indicate the rule CANNOT be auto-generated from the source schema as-is.
    Generic patterns, no business-domain identifiers.

    Examples (all generic):
        "Parse to extract <X>"                       -> True
        "Lookup <X> (does not exist today)"           -> True
        "First three digits of <X>"                   -> True
        "Add century part"                            -> True
        "Manual mapping required"                     -> True
        "Just take TABLE.COLUMN"                      -> False
    """
    if not text:
        return False
    return _UNIMPLEMENTABLE_RULE_RE.search(text) is not None


# ── SUBSTR-based parse detector ──────────────────────────────────────────────
#
# Some DRD cells describe a parse-and-emit operation that IS auto-generatable
# as an Oracle ``SUBSTR(...)`` (with optional concat-prefix for century-year).
# Examples (generic, no business names):
#
#     "For T.SRC_STM_ID = 60, use TRADE_NUMBER.  Parse to extract first three
#      chars / characters / digits."
#     -> CASE WHEN T.SRC_STM_ID = 60 THEN SUBSTR(<col>, 1, 3) ELSE NULL END
#
#     "Last two digits, add century part"
#     -> '20' || SUBSTR(<col>, LENGTH(<col>)-1, 2)
#
# When BOTH a filter condition (``For X.Y = Z``) AND a parse rule are present,
# the emitted expression wraps the SUBSTR in a CASE WHEN ... ELSE NULL END.

_CARDINAL_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _cardinal_to_int(token: str) -> Optional[int]:
    if not token:
        return None
    t = token.strip().lower()
    if t.isdigit():
        return int(t)
    return _CARDINAL_MAP.get(t)


_SUBSTR_FIRST_RE = re.compile(
    r"\bfirst\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:chars|characters|digits)\b",
    re.IGNORECASE,
)
_SUBSTR_LAST_RE = re.compile(
    r"\blast\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:chars|characters|digits)\b",
    re.IGNORECASE,
)
_ADD_CENTURY_RE = re.compile(r"\badd\s+century\s+part\b", re.IGNORECASE)

# "For <ALIAS>.<COL> = <VALUE>" / "When <ALIAS>.<COL> = <VALUE>"
_FILTER_COND_RE = re.compile(
    r"\b(?:for|when)\s+"
    rf"({_IDENT}\.{_IDENT})\s*"
    r"=\s*"
    r"(\d+|'[^']*'|[A-Z][A-Z0-9_]+)",
    re.IGNORECASE,
)


def extract_substring_parse_spec(text: str) -> Optional[Dict[str, Any]]:
    """Detect a SUBSTR-based parse rule in DRD prose.

    Returns a dict ``{kind, length, add_century, filter_alias_col,
    filter_value, raw}`` when a match is found, ``None`` otherwise.

    ``kind`` is ``"first"`` or ``"last"``.  When ``add_century`` is True the
    composer prepends ``'20' ||`` to the SUBSTR.  Filter is optional.

    Generic -- no business-domain identifiers in the patterns.
    """
    if not text:
        return None
    first_m = _SUBSTR_FIRST_RE.search(text)
    last_m = _SUBSTR_LAST_RE.search(text)
    if not first_m and not last_m:
        return None
    if first_m:
        kind = "first"
        length = _cardinal_to_int(first_m.group(1))
        raw = first_m.group(0)
    else:
        kind = "last"
        length = _cardinal_to_int(last_m.group(1))
        raw = last_m.group(0)
    if not length or length <= 0 or length > 50:
        return None
    add_century = bool(_ADD_CENTURY_RE.search(text))
    spec: Dict[str, Any] = {
        "kind": kind,
        "length": length,
        "add_century": add_century,
        "filter_alias_col": None,
        "filter_value": None,
        "raw": raw,
    }
    fm = _FILTER_COND_RE.search(text)
    if fm:
        spec["filter_alias_col"] = fm.group(1)
        val = fm.group(2)
        spec["filter_value"] = val
    return spec


def compose_substring_parse_expr(spec: Dict[str, Any], base_col: str) -> str:
    """Build ``[CASE WHEN <filter>] [<century>||] SUBSTR(<base_col>, ...) [END]``.

    Pure string formatting -- caller supplies the fully-qualified base column
    reference (e.g. ``t.TRD_NUM``) that has been already alias-rewritten to
    fit the surrounding query.
    """
    length = int(spec["length"])
    if spec["kind"] == "first":
        substr = f"SUBSTR({base_col}, 1, {length})"
    else:
        # "last N chars" -- prefer LENGTH-based form so the column may be
        # variable-length without breaking.
        substr = f"SUBSTR({base_col}, LENGTH({base_col}) - {length - 1}, {length})"
    body = f"'20' || {substr}" if spec.get("add_century") else substr
    filt = spec.get("filter_alias_col")
    val = spec.get("filter_value")
    if filt and val is not None:
        # Quote string values; keep numeric literals as-is.
        val_s = str(val)
        if val_s and val_s[0] not in "'-" and not val_s.replace(".", "", 1).isdigit():
            val_s = f"'{val_s}'"
        return f"CASE WHEN {filt} = {val_s} THEN {body} ELSE NULL END"
    return body


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
