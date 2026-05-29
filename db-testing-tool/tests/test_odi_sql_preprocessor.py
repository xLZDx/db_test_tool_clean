"""Tests for the ODI SQL preprocessor.

Operator-locked: generic identifiers only (WIDGET/GADGET/ZED placeholders).
Each rule is verified in isolation plus an idempotency check.
"""
from __future__ import annotations

from app.sql_model.odi_sql_preprocessor import (
    ALL_RULE_NAMES,
    RULE_ANSI_ESCAPES,
    RULE_BARE_BRACKETS,
    RULE_BLOCK_COMMENTS,
    RULE_LINE_COMMENTS,
    RULE_NONPRINTABLE_CHARS,
    RULE_ODI_SUBSTITUTIONS,
    RULE_ODI_VARIABLE_REFS,
    RULE_ORACLE_HINTS,
    RULE_OUTER_JOIN_MARKERS,
    RULE_SESSION_ARTIFACT_FIX,
    preprocess,
)


def test_empty_input_returns_empty():
    out, applied = preprocess("")
    assert out == ""
    assert applied == []


def test_odi_substitution_marker_stripped():
    sql = "SELECT 1, <?=odiRef.getSession(\"WIDGET\") ?>, 3 FROM T"
    out, applied = preprocess(sql)
    assert RULE_ODI_SUBSTITUTIONS in applied
    assert "<?=" not in out
    assert "?>" not in out


def test_oracle_hint_stripped():
    sql = "SELECT /*+ USE_NL(WIDGET) */ a.X FROM WIDGET a"
    out, applied = preprocess(sql)
    assert RULE_ORACLE_HINTS in applied
    assert "/*+" not in out


def test_outer_join_marker_stripped():
    sql = "SELECT a.X FROM A a, B b WHERE a.K = b.K(+)"
    out, applied = preprocess(sql)
    assert RULE_OUTER_JOIN_MARKERS in applied
    assert "(+)" not in out


def test_line_comment_stripped():
    sql = "SELECT a.X  -- a comment\nFROM A a"
    out, applied = preprocess(sql)
    assert RULE_LINE_COMMENTS in applied
    assert "-- a comment" not in out


def test_block_comment_stripped():
    sql = "SELECT a.X /* a block */ FROM A a"
    out, applied = preprocess(sql)
    assert RULE_BLOCK_COMMENTS in applied
    assert "/* a block */" not in out


def test_odi_variable_ref_placeholderised():
    sql = "INSERT INTO #SSDS.SSDS_WIDGET (a) VALUES (1)"
    out, applied = preprocess(sql)
    assert RULE_ODI_VARIABLE_REFS in applied
    assert "#SSDS" not in out
    assert "ODI_VAR" in out


def test_ansi_escape_stripped():
    sql = "SELECT a.\x1b[4mX\x1b[0m FROM T"
    out, applied = preprocess(sql)
    assert RULE_ANSI_ESCAPES in applied
    assert "\x1b" not in out


def test_bare_bracket_codes_stripped():
    sql = "SELECT a.[4mX[0m FROM T"
    out, applied = preprocess(sql)
    assert RULE_BARE_BRACKETS in applied
    assert "[4m" not in out
    assert "[0m" not in out


def test_session_artifact_collapsed_to_single_literal():
    """Operator-locked: the upstream resolver emits ''X'' (malformed).
    Preprocessor must collapse to 'X' so Oracle parses it as a string literal."""
    sql = "SELECT SYSDATE, ''WIDGET_SESS'', 0 FROM DUAL"
    out, applied = preprocess(sql)
    assert RULE_SESSION_ARTIFACT_FIX in applied
    assert "'WIDGET_SESS'" in out
    assert "''WIDGET_SESS''" not in out


def test_session_artifact_does_not_break_legitimate_quote_escape():
    """An Oracle string literal containing an escaped single quote like
    'It''s' must NOT be mangled by the session-artifact rule."""
    sql = "SELECT 'It''s a widget' FROM DUAL"
    out, applied = preprocess(sql)
    # The pattern requires bare identifier with no surrounding non-quote
    # characters; 'It''s' has surrounding non-uppercase content and 's' is
    # lowercase -- should NOT match.
    assert RULE_SESSION_ARTIFACT_FIX not in applied
    assert "It''s a widget" in out


def test_nonprintable_chars_stripped():
    sql = "SELECT a.\x07X FROM T"  # bell character
    out, applied = preprocess(sql)
    assert RULE_NONPRINTABLE_CHARS in applied
    assert "\x07" not in out


def test_idempotent_on_clean_input():
    """Running preprocess twice yields the same SQL."""
    sql = "SELECT a.X FROM WIDGET a JOIN GADGET g ON a.K = g.K"
    out1, applied1 = preprocess(sql)
    out2, applied2 = preprocess(out1)
    assert out1 == out2
    # Already-clean input -> no rules applied on second pass
    assert applied2 == []


def test_all_rule_names_exported():
    """The ALL_RULE_NAMES constant covers every rule the function applies."""
    expected = {
        RULE_ODI_SUBSTITUTIONS, RULE_ANSI_ESCAPES, RULE_BARE_BRACKETS,
        RULE_ORACLE_HINTS, RULE_BLOCK_COMMENTS, RULE_LINE_COMMENTS,
        RULE_OUTER_JOIN_MARKERS, RULE_ODI_VARIABLE_REFS,
        RULE_NONPRINTABLE_CHARS, RULE_SESSION_ARTIFACT_FIX,
    }
    assert set(ALL_RULE_NAMES) == expected


def test_no_business_domain_names_in_module():
    """The preprocessor module must contain ZERO business-domain identifiers.
    Allowed: generic SQL keywords, regex metacharacters, ODI structural names
    (odiRef etc.), and placeholder literals like ODI_SUBST/ODI_VAR."""
    import pathlib
    path = pathlib.Path(__file__).parent.parent / "app" / "sql_model" / "odi_sql_preprocessor.py"
    text = path.read_text(encoding="utf-8")
    # If the operator's specific business identifiers ever leak in (AVY_*,
    # APACSH, SHDW_, etc.), we want to catch it.
    forbidden = ("AVY_", "APACSH", "APASEC", "SHDW_", "CCAL_", "BKR_", "SDIRA")
    for token in forbidden:
        assert token not in text, f"Business identifier '{token}' leaked into preprocessor"
