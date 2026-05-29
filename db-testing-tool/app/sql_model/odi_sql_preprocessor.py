"""Preprocessor for ODI-export SQL blocks.

Strips non-SQL noise so that a downstream Oracle parser (sqlglot in our
case) can ingest the SQL successfully.  Pure text transforms -- no Oracle
parsing here; the parser is consulted only AFTER preprocessing.

Rule set (operator-locked 2026-05-29 -- per Phase 0 empirical probe):

  1. ODI substitution markers: ``<?=...?>`` / ``<%=...%>`` / ``<@...@>``
  2. ANSI escape codes (CSI sequences): ``\\x1b[...m``
  3. Bare bracket codes (``[4m``, ``[0m``) left over after CDATA decode
  4. Oracle hints: ``/*+ ... */``
  5. Block comments: ``/* ... */`` (after hints stripped)
  6. Line comments: ``-- ...``
  7. Oracle 9i ``(+)`` outer-join markers
  8. ODI variable refs: ``#NAMESPACE.VAR`` (placeholderised)
  9. Non-printable control chars (``\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f``)
 10. Session-substitution artifact: the upstream ``_resolve_session`` in
     ``app/sql_model/odi_template_resolver.py`` substitutes
     ``<?=odiRef.getSession("X") ?>`` with ``'X'`` while it is already
     INSIDE a single-quoted host literal, producing the malformed
     ``''X''`` token (string-literal + identifier + string-literal) that
     breaks every Oracle parser.  Collapse to ``'X'``.

Phase 0 empirical probe verified all 6 ODI SQL blocks (STEP1-5 + MERGE)
of the test scenario parse cleanly through sqlglot after this
preprocessor.

The preprocessor is GENERIC -- no business-domain identifiers.  Tests
use arbitrary WIDGET / GADGET / ZED placeholder names.
"""
from __future__ import annotations

import re
from typing import Tuple

# ── Rule patterns (compiled once at import time) ─────────────────────────────

# 1. ODI substitution markers
_ODI_SUBST_RE = re.compile(
    r"<\?=.*?\?>|<%=.*?%>|<@.*?@>",
    re.DOTALL,
)
# 2. ANSI escape codes (CSI sequences)
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 3. Bare bracket variants left over after CDATA decode (no ESC byte)
_BARE_BRACKET_RE = re.compile(r"\[\d+m")
# 4. Oracle hints  /*+ ... */
_HINT_RE = re.compile(r"/\*\+[^*]*(?:\*(?!/)[^*]*)*\*/")
# 5. Block comments  /* ... */  (after hints already stripped)
_BLOCK_COMMENT_RE = re.compile(r"/\*[^*]*(?:\*(?!/)[^*]*)*\*/")
# 6. Line comments  -- ...
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# 7. Oracle 9i outer-join markers  (+)
_OUTER_JOIN_RE = re.compile(r"\s*\(\s*\+\s*\)")
# 8. ODI variable refs  #SOMETHING or #SSDS.X
_ODI_VAR_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_.]*")
# 9. Non-printable chars
_NONPRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 10. Session-substitution artifact (operator-locked):
#     Pattern: TWO single quotes + ALPHA_ID + TWO single quotes, anchored
#     on non-quote chars so we don't mis-match Oracle's legitimate ``''``
#     embedded-quote escape inside a longer literal.
_SESSION_ARTIFACT_RE = re.compile(
    r"(?<!')''([A-Z][A-Z0-9_]*)''(?!')",
)


def preprocess(sql: str) -> Tuple[str, list]:
    """Apply all rules in order.

    Returns ``(cleaned_sql, applied_rule_names)``.  ``applied_rule_names``
    lists ONLY the rules that actually matched something -- useful for
    diagnostics and tests.

    The function is idempotent: ``preprocess(preprocess(x)[0])`` yields the
    same SQL as ``preprocess(x)[0]`` (modulo the applied-rules list).
    """
    if not sql:
        return "", []
    applied: list = []
    out = sql

    if _ODI_SUBST_RE.search(out):
        out = _ODI_SUBST_RE.sub("'ODI_SUBST'", out)
        applied.append("odi_substitutions")

    if _ANSI_ESC_RE.search(out):
        out = _ANSI_ESC_RE.sub("", out)
        applied.append("ansi_escapes")

    if _BARE_BRACKET_RE.search(out):
        out = _BARE_BRACKET_RE.sub("", out)
        applied.append("bare_bracket_codes")

    if _HINT_RE.search(out):
        out = _HINT_RE.sub(" ", out)
        applied.append("oracle_hints")

    if _BLOCK_COMMENT_RE.search(out):
        out = _BLOCK_COMMENT_RE.sub(" ", out)
        applied.append("block_comments")

    if _LINE_COMMENT_RE.search(out):
        out = _LINE_COMMENT_RE.sub("", out)
        applied.append("line_comments")

    if _OUTER_JOIN_RE.search(out):
        out = _OUTER_JOIN_RE.sub("", out)
        applied.append("oracle_outer_join_markers")

    if _ODI_VAR_RE.search(out):
        out = _ODI_VAR_RE.sub("ODI_VAR", out)
        applied.append("odi_variable_refs")

    if _NONPRINTABLE_RE.search(out):
        out = _NONPRINTABLE_RE.sub("", out)
        applied.append("nonprintable_chars")

    if _SESSION_ARTIFACT_RE.search(out):
        out = _SESSION_ARTIFACT_RE.sub(r"'\1'", out)
        applied.append("session_artifact_fix")

    return out, applied


# Public rule-name constants so tests + consumers can compare without
# hardcoding strings.
RULE_ODI_SUBSTITUTIONS = "odi_substitutions"
RULE_ANSI_ESCAPES = "ansi_escapes"
RULE_BARE_BRACKETS = "bare_bracket_codes"
RULE_ORACLE_HINTS = "oracle_hints"
RULE_BLOCK_COMMENTS = "block_comments"
RULE_LINE_COMMENTS = "line_comments"
RULE_OUTER_JOIN_MARKERS = "oracle_outer_join_markers"
RULE_ODI_VARIABLE_REFS = "odi_variable_refs"
RULE_NONPRINTABLE_CHARS = "nonprintable_chars"
RULE_SESSION_ARTIFACT_FIX = "session_artifact_fix"

ALL_RULE_NAMES = (
    RULE_ODI_SUBSTITUTIONS,
    RULE_ANSI_ESCAPES,
    RULE_BARE_BRACKETS,
    RULE_ORACLE_HINTS,
    RULE_BLOCK_COMMENTS,
    RULE_LINE_COMMENTS,
    RULE_OUTER_JOIN_MARKERS,
    RULE_ODI_VARIABLE_REFS,
    RULE_NONPRINTABLE_CHARS,
    RULE_SESSION_ARTIFACT_FIX,
)
