"""Phase 0 — empirical sqlglot viability test.

Goal: determine whether sqlglot can parse cleaned ODI SQL blocks.  Build a
minimal preprocessor (strips ODI substitutions, ANSI escapes, hints, Oracle
(+) outer-join markers, comments).  Run on all 5 STEP blocks + MERGE.
Report parse success/failure per block + first error context.

Decision criterion (announced upfront):
  6/6 parsed  -> hybrid viable -> Phase 1: walker on AST
  5/6 parsed  -> hybrid + fallback for the 1 failure
  <= 4/6      -> switch to Option 1 (full custom dissector)

Read-only.  No mutation of project state.  Pure experimentation.
"""
from __future__ import annotations

import pathlib
import re
import sys
from typing import Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sql_model.odi_parser import OdiXmlParser  # noqa: E402

try:
    import sqlglot
    import sqlglot.errors
except ImportError as e:  # noqa: BLE001
    print(f"sqlglot import failed: {e}")
    sys.exit(2)


# ── Preprocessor ────────────────────────────────────────────────────────────

# 1. ODI substitution markers
_ODI_SUBST_RE = re.compile(
    r"<\?=.*?\?>|<%=.*?%>|<@.*?@>",
    re.DOTALL,
)
# 2. ANSI escape codes (CSI sequences)
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 2b. The bare-bracket variants that appear after XML decode loses the ESC byte
_BARE_BRACKET_RE = re.compile(r"\[\d+m")
# 3. Oracle hints  /*+ ... */
_HINT_RE = re.compile(r"/\*\+[^*]*(?:\*(?!/)[^*]*)*\*/")
# 4. Block comments  /* ... */  (after hints already stripped)
_BLOCK_COMMENT_RE = re.compile(r"/\*[^*]*(?:\*(?!/)[^*]*)*\*/")
# 5. Line comments  -- ...
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# 6. Oracle outer-join markers  (+)
_OUTER_JOIN_RE = re.compile(r"\s*\(\s*\+\s*\)")
# 7. ODI variable refs  #SOMETHING or #SSDS.X
_ODI_VAR_RE = re.compile(r"#[A-Za-z][A-Za-z0-9_.]*")
# 8. Stray non-printable chars after decoding
_NONPRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 9. Existing parser's session-substitution artifact:
#    Raw DefTxt has  '<?=odiRef.getSession("X") ?>'
#    After existing resolver: ''X''   (string-literal + identifier + string-literal)
#    Oracle reads this as `''` `X` `''` -- malformed.  Fix: collapse to 'X'.
#    Pattern: TWO single quotes + ALPHA_ID + TWO single quotes, anchored on
#    non-quote chars so we don't mis-match Oracle's legitimate '' escape.
_SESSION_ARTIFACT_RE = re.compile(
    r"(?<!')''([A-Z][A-Z0-9_]*)''(?!')",
)
# 10. Some embedded substitutions become bare-identifier-after-comma artifacts
#    e.g. `,SESSION_NAME,` where the substitution dropped quotes entirely.
#    These are valid identifiers but the user clearly wanted a literal --
#    detection is risky, so we leave them and let sqlglot decide.


def preprocess(sql: str) -> Tuple[str, list[str]]:
    """Strip ODI / dialect noise.  Returns (cleaned_sql, applied_rules)."""
    if not sql:
        return "", []
    applied: list[str] = []
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


# ── Probe runner ────────────────────────────────────────────────────────────

def try_parse(label: str, sql: str) -> dict:
    """Try parsing `sql` with sqlglot Oracle dialect after preprocessing."""
    if not sql:
        return {"label": label, "status": "EMPTY", "applied": []}
    cleaned, applied = preprocess(sql)
    try:
        ast = sqlglot.parse_one(cleaned, dialect="oracle")
    except sqlglot.errors.ParseError as e:
        # Get error context
        msg = str(e)
        # Try to find line/col hint
        return {
            "label": label,
            "status": "PARSE_ERROR",
            "applied": applied,
            "input_len": len(sql),
            "cleaned_len": len(cleaned),
            "error": msg[:300],
        }
    except Exception as e:  # noqa: BLE001
        return {
            "label": label,
            "status": "EXCEPTION",
            "applied": applied,
            "input_len": len(sql),
            "cleaned_len": len(cleaned),
            "error": f"{type(e).__name__}: {str(e)[:280]}",
        }
    # Success.  Extract a few facts to confirm the AST is useful.
    ast_type = type(ast).__name__
    target_cols: list[str] = []
    select_exprs = 0
    if isinstance(ast, sqlglot.exp.Insert):
        # INSERT INTO X (col, col) SELECT ...
        schema_node = ast.this
        if isinstance(schema_node, sqlglot.exp.Schema):
            target_cols = [c.name for c in schema_node.expressions]
        inner = ast.expression
        if isinstance(inner, sqlglot.exp.Select):
            select_exprs = len(inner.expressions)
    elif isinstance(ast, sqlglot.exp.Merge):
        select_exprs = -1  # MERGE structure differs
    return {
        "label": label,
        "status": "PARSED_OK",
        "applied": applied,
        "input_len": len(sql),
        "cleaned_len": len(cleaned),
        "ast_type": ast_type,
        "target_cols_count": len(target_cols),
        "select_exprs_count": select_exprs,
    }


def main() -> int:
    odi_path = ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
    if not odi_path.exists():
        print(f"ODI XML not found: {odi_path}")
        return 2

    model = OdiXmlParser().parse_bytes(odi_path.read_bytes())

    results: list[dict] = []
    for step in model.staging_steps:
        label = f"STEP{step.step_id} ({step.name})"
        results.append(try_parse(label, step.select_sql or ""))
    results.append(try_parse("MERGE", model.final_select_sql or ""))

    # Report
    print("=" * 80)
    print("PHASE 0 -- sqlglot Oracle dialect viability probe")
    print("=" * 80)
    print()
    print(f"{'Block':<45s}  {'Status':<14s}  Input -> Cleaned")
    print("-" * 80)
    n_ok = 0
    for r in results:
        if r.get("status") == "PARSED_OK":
            n_ok += 1
            print(
                f"{r['label']:<45s}  PARSED_OK     "
                f"  {r.get('input_len',0):>7d} -> {r.get('cleaned_len',0):>7d}  "
                f"  target_cols={r.get('target_cols_count','-')}  "
                f"select_exprs={r.get('select_exprs_count','-')}"
            )
        else:
            print(f"{r['label']:<45s}  {r.get('status'):<14s}")

    print()
    print("Preprocessor rules applied per block:")
    for r in results:
        if r.get("applied"):
            print(f"  {r['label']}: {', '.join(r['applied'])}")
    print()

    failures = [r for r in results if r.get("status") not in ("PARSED_OK", "EMPTY")]
    if failures:
        print("FAILURE DETAILS:")
        for r in failures:
            print(f"\n  {r['label']}:")
            print(f"    error: {r.get('error','')}")
    print()
    total = sum(1 for r in results if r.get("status") != "EMPTY")
    print(f"Success rate: {n_ok}/{total}")
    print()
    print("Decision criterion:")
    if n_ok == total:
        print(f"  {n_ok}/{total} (100%) -> HYBRID FULLY VIABLE.  Proceed to Phase 1.")
        return 0
    elif n_ok >= total - 1:
        print(f"  {n_ok}/{total} (>=83%) -> HYBRID + FALLBACK for the failure.  Proceed to Phase 1.")
        return 0
    elif n_ok >= total / 2:
        print(f"  {n_ok}/{total} (>=50%) -> MARGINAL.  Discuss with operator.")
        return 1
    else:
        print(f"  {n_ok}/{total} (<50%) -> SWITCH TO OPTION 1 (full custom dissector).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
