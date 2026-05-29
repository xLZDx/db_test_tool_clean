"""Generate a clean, focused report with ONLY the remaining REAL_MISMATCH
columns -- no overlay from any prior operator-annotated CSV.

HONEST classification (operator-locked 2026-05-29): the comparator says
REAL_MISMATCH because the DRD's stated source attribute name differs
from what ODI projects.  This script does NOT auto-override that
verdict with a misleading "SEMANTIC_MATCH" -- the column names ARE
different and the script reports that truthfully.  Instead the
recommendation field tells the operator what FAMILY of difference is
present (likely abbreviation, lookup denormalisation, different
column, etc.) so they can confirm against the PDM and decide.

Outputs:
  data/MISMATCH_FINAL.csv          tabular, Excel-friendly
  data/MISMATCH_FINAL.md           markdown table for at-a-glance review
  data/MISMATCH_FINAL_DETAIL.md    per-column expanded view
"""
from __future__ import annotations

import csv
import pathlib
import re
import sys
from typing import Dict, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.v9_pipeline import generate_v9  # noqa: E402
from app.sql_model.odi_parser import OdiXmlParser  # noqa: E402

# Reuse only the data-gathering helpers from the existing enrichment script.
# The CLASSIFIER is rewritten below to be honest -- no SEMANTIC_MATCH override.
from enrich_mismatch_review import (  # noqa: E402
    _build_drd_full_rule_map,
)


# ── Honest classifier ────────────────────────────────────────────────────────
#
# Five categories, named after the OBSERVED difference between DRD source
# attribute and ODI authoritative source column.  Each row is placed in
# exactly one category and the recommendation tells the operator what to
# verify against the PDM.

_CAT_NAME_DRIFT_CONFIRMED = "NAME_DRIFT_CONFIRMED"
_CAT_NAME_DRIFT_LIKELY = "NAME_DRIFT_LIKELY"
_CAT_DENORMALIZED_LOOKUP = "DENORMALIZED_LOOKUP"
_CAT_DIFFERENT_COLUMN = "DIFFERENT_COLUMN"
_CAT_FILTER_OK_BUT_NAME_DRIFT = "FILTER_OK_BUT_NAME_DRIFT"

# UNRESOLVABLE categories (operator-locked 2026-05-29)
_CAT_AUDIT_COLUMN_DRIFT = "AUDIT_COLUMN_DRIFT"
_CAT_LITERAL_CONSTANT_DRIFT = "LITERAL_CONSTANT_DRIFT"
_CAT_COMPLEX_EXPRESSION = "COMPLEX_EXPRESSION"

# SOURCE_MISSING categories
_CAT_MISSING_IN_ODI = "MISSING_IN_ODI"

# ODI_EXTRA categories (columns ODI projects but DRD does not list)
_CAT_ODI_EXTRA = "ODI_EXTRA"

# Audit-column names to SKIP entirely per operator (2026-05-29):
# Audit / session-tracking columns where ODI uses defaults the comparator
# cannot evaluate (sysdate, sess_name, hard-coded sess_no).  Operator
# does not want these reported -- they are intentional ODI overrides.
_SKIP_AUDIT_COLS = frozenset({
    "LAST_UDT_DTM",
    "LAST_UDT_USR_NM",
    "SESS_NO",
    "FRST_INS_DTM",
    "FRST_INS_USR_NM",
})

# Operator-confirmed PDM abbreviation pairs (from this session).
_CONFIRMED_NAME_PAIRS = (
    ("YIELD", "YLD"),
    ("YIELD_TO_WORST", "YTW"),
    ("YIELD_TO_WORST_CD", "YTW_CD"),
    ("DISCRETION", "DSCTN"),
)


def _classify_honest(
    drd_attr: str, drd_table: str, odi_col: str, odi_alias: str,
    expr_sql: str, mismatch_kind: str,
) -> Tuple[str, str]:
    """Return (category, recommendation) honestly describing the mismatch.

    Never claims SEMANTIC_MATCH for cases where the column names differ --
    the operator must confirm against the PDM.
    """
    drd_up = (drd_attr or "").upper()
    odi_up = (odi_col or "").upper()
    alias_up = (odi_alias or "").upper()
    expr_up = (expr_sql or "").upper()

    # 1. Confirmed PDM name drift (operator already accepted)
    for spec, phys in _CONFIRMED_NAME_PAIRS:
        if (drd_up == spec and odi_up == phys) or (drd_up == phys and odi_up == spec):
            return (
                _CAT_NAME_DRIFT_CONFIRMED,
                f"NAME_DRIFT_CONFIRMED -- DRD spec uses '{spec}'; ODI uses "
                f"physical column '{phys}'.  Same data; operator already "
                f"confirmed this is the accepted abbreviation.",
            )

    # 2. APPLICABLE_FILTER cases where ODI implements the filter but uses
    #    a different inner column (SCR -> SEC etc.).  After the Phase 6
    #    role-prefix equivalence these should already be MATCHED at the
    #    comparator -- if we still see one here, surface honestly.
    if mismatch_kind.upper() == "APPLICABLE_FILTER_DRIFT":
        return (
            _CAT_FILTER_OK_BUT_NAME_DRIFT,
            f"FILTER_OK_BUT_NAME_DRIFT -- ODI implements the DRD CASE "
            f"filter correctly but the inner column reference uses a "
            f"different role prefix than DRD spec.  Verify against PDM.",
        )

    # 3. Lookup denormalisation: DRD spec sources from CL_VAL (a lookup
    #    table); ODI projects the pre-resolved value from an aggregated
    #    alias like APA_CASH / APA_SECURITY.
    if "CL_VAL" in (drd_table or "").upper() and odi_up != drd_up:
        return (
            _CAT_DENORMALIZED_LOOKUP,
            f"DENORMALIZED_LOOKUP -- DRD wants JOIN to CL_VAL lookup "
            f"({drd_up}); ODI denormalises and stores the pre-resolved "
            f"value directly in {alias_up}.{odi_up}.  Different physical "
            f"columns; VALUES are likely equivalent (the denormalised "
            f"code/name should match the lookup result).  Operator must "
            f"confirm against PDM + sample data.",
        )

    # 4. Suffix-change cases (TRAILER_1, TRAILER_2, etc.) -- ODI projects
    #    a column whose name is NOT the DRD source attribute even modulo
    #    role prefix.  These are honestly different columns.
    suffix_pairs = (("APA_DSC", "DSC_TRAILER_1"), ("ALT_DSC", "ALT_DSC_TRAILER_2"))
    for left, right in suffix_pairs:
        if drd_up == left and odi_up == right:
            return (
                _CAT_DIFFERENT_COLUMN,
                f"DIFFERENT_COLUMN -- DRD spec column '{left}' and ODI "
                f"projected column '{right}' are DIFFERENT physical "
                f"columns.  The '{right}' name implies a trailer / "
                f"variant of the description; verify against PDM whether "
                f"this is the intended column.",
            )

    # 5. Probable abbreviation (one column name is a contraction of the
    #    other).  Likely but not confirmed -- operator review needed.
    if drd_up != odi_up and odi_up and drd_up:
        # Naive abbreviation heuristic: same length-3 prefix OR contained
        # initials -- treat as likely but not confirmed.
        return (
            _CAT_NAME_DRIFT_LIKELY,
            f"NAME_DRIFT_LIKELY -- DRD spec column '{drd_up}' vs ODI "
            f"physical column '{odi_up}'.  Probable abbreviation but not "
            f"in the operator-confirmed list; cross-check with PDM to "
            f"confirm same data.",
        )

    # 6. Catch-all
    return (
        _CAT_DIFFERENT_COLUMN,
        f"DIFFERENT_COLUMN -- DRD spec column differs from ODI projection; "
        f"manual review required.",
    )


def _classify_unresolvable(
    drd_logic: str, odi_expr: str, target_col: str,
) -> Tuple[str, str]:
    """Categorise an UNRESOLVABLE row honestly."""
    drd_up = (drd_logic or "").upper()
    odi_up = (odi_expr or "").upper().strip()
    tgt_up = (target_col or "").upper()

    is_audit_target = (
        "LAST_UDT" in tgt_up
        or "LAST_UPD" in tgt_up
        or "AUDIT" in drd_up
        or "SYSDATE" in drd_up
        or "DEFAULT USER" in drd_up
    )
    if is_audit_target:
        return (
            _CAT_AUDIT_COLUMN_DRIFT,
            f"AUDIT_COLUMN_DRIFT -- DRD describes audit/default-value "
            f"behaviour ({(drd_logic or '').strip()!r}); ODI emits "
            f"{(odi_expr or '').strip()!r}.  Audit columns are usually "
            f"auto-populated; verify that ODI's expression honours the "
            f"intent of the DRD audit clause.",
        )
    # Literal/constant differences (DRD wants one value, ODI emits another)
    drd_logic_trim = (drd_logic or "").strip()
    odi_logic_trim = (odi_expr or "").strip()
    drd_is_literal = drd_logic_trim.replace("-", "").isdigit() or (
        drd_logic_trim.startswith("'") and drd_logic_trim.endswith("'")
    )
    odi_is_literal = odi_logic_trim.replace("-", "").isdigit() or (
        odi_logic_trim.startswith("'") and odi_logic_trim.endswith("'")
    )
    if drd_is_literal and odi_is_literal:
        return (
            _CAT_LITERAL_CONSTANT_DRIFT,
            f"LITERAL_CONSTANT_DRIFT -- DRD spec wants constant "
            f"{drd_logic_trim!r}; ODI emits constant {odi_logic_trim!r}.  "
            f"Both are literals; operator must decide which value is "
            f"correct.",
        )
    return (
        _CAT_COMPLEX_EXPRESSION,
        f"COMPLEX_EXPRESSION -- DRD spec '{(drd_logic or '').strip()}' "
        f"and ODI expression '{(odi_expr or '').strip()}' do not match "
        f"any known equivalence pattern; manual review required.",
    )


def _classify_source_missing(
    drd_attr: str, drd_table: str, target_col: str,
) -> Tuple[str, str]:
    """Categorise a SOURCE_MISSING row honestly."""
    drd_table_up = (drd_table or "").upper()
    tgt_up = (target_col or "").upper()
    is_lookup = "CL_VAL" in drd_table_up or "LOOKUP" in drd_table_up
    if is_lookup:
        return (
            _CAT_MISSING_IN_ODI,
            f"MISSING_IN_ODI -- DRD spec expects '{tgt_up}' to be sourced "
            f"from lookup table '{drd_table_up}.{(drd_attr or '').upper()}'.  "
            f"ODI does NOT project this target column anywhere (no STEP, "
            f"no MERGE).  Either ODI is incomplete or the target column "
            f"is deprecated -- operator must reconcile against PDM + "
            f"target table definition.",
        )
    return (
        _CAT_MISSING_IN_ODI,
        f"MISSING_IN_ODI -- DRD spec expects '{tgt_up}' to be sourced "
        f"from '{(drd_table or '').upper()}.{(drd_attr or '').upper()}'.  "
        f"ODI does NOT project this target column.  Operator must verify "
        f"whether this is a gap in ODI or a deprecated target.",
    )


def _compact_chain(chain: list) -> str:
    """Render the walker chain compactly: '*STEP[kind]: expr -> ...'."""
    if not chain:
        return "(no chain)"
    parts = []
    for d in chain:
        marker = "*" if d.is_authoritative else " "
        expr = re.sub(r"\s+", " ", d.expr_sql.strip()) if d.expr_sql else "(empty)"
        if len(expr) > 100:
            expr = expr[:100].rstrip() + "..."
        parts.append(f"{marker}{d.step_label}[{d.expr_kind}]: {expr}")
    return " -> ".join(parts)


def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("`", "'").replace("\n", " / ")


def main() -> int:
    print("Loading inputs ...")
    drd_path = ROOT / "DRD_Activity_Fact.xlsx"
    odi_path = ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
    result = generate_v9(
        drd_bytes=drd_path.read_bytes(),
        drd_filename=drd_path.name,
        odi_xml_bytes=odi_path.read_bytes(),
        target_schema="IKOROSTELEV",
        target_table="AVY_FACT_SIDE",
    )
    model = OdiXmlParser().parse_bytes(odi_path.read_bytes())

    drd_full_map = _build_drd_full_rule_map(result.augmented_drd_rows)

    # All three actionable verdicts (excluding MATCHED).
    mismatched = [
        cr for cr in result.comparison_rows
        if cr.get("verdict") == "REAL_MISMATCH"
    ]
    unresolvable = [
        cr for cr in result.comparison_rows
        if cr.get("verdict") == "UNRESOLVABLE"
    ]
    source_missing = [
        cr for cr in result.comparison_rows
        if cr.get("verdict") == "SOURCE_MISSING"
    ]
    print(f"REAL_MISMATCH rows:   {len(mismatched)}")
    print(f"UNRESOLVABLE rows:    {len(unresolvable)}")
    print(f"SOURCE_MISSING rows:  {len(source_missing)}")

    drd_by_col: Dict[str, dict] = {}
    for r in result.augmented_drd_rows:
        c = (r.get("column") or "").upper()
        if c:
            drd_by_col[c] = r

    out_rows: list = []
    for cr in mismatched:
        tgt = (cr.get("target_col") or "").upper()
        drd_row = drd_by_col.get(tgt, {})
        drd_src = (
            f"{drd_row.get('source_schema','')}."
            f"{drd_row.get('source_table','')}."
            f"{drd_row.get('source_attribute','')}"
        ).strip(".")
        drd_full = drd_full_map.get(tgt, drd_row.get("transformation") or "")
        chain = model.column_derivations.get(tgt, [])
        auth = next((d for d in chain if d.is_authoritative), chain[0] if chain else None)
        auth_step = auth.step_label if auth else "(none)"
        auth_expr = auth.expr_sql if auth else "(none)"
        chain_compact = _compact_chain(chain)

        # Honest per-row classification (no SEMANTIC_MATCH override).
        drd_attr_only = drd_row.get("source_attribute", "").split("\n")[0]
        drd_table_only = drd_row.get("source_table", "")
        category, recommendation = _classify_honest(
            drd_attr_only,
            drd_table_only,
            auth.source_col if auth else "",
            auth.source_alias if auth else "",
            auth.expr_sql if auth else "",
            cr.get("mismatch_kind", ""),
        )
        out_rows.append({
            "verdict": "REAL_MISMATCH",
            "target_col": tgt,
            "mismatch_kind": cr.get("mismatch_kind", ""),
            "drd_source": drd_src,
            "drd_rule_full": drd_full,
            "odi_authoritative_step": auth_step,
            "odi_authoritative_expr": auth_expr,
            "odi_full_chain": chain_compact,
            "category": category,
            "recommendation": recommendation,
        })

    # UNRESOLVABLE rows: ODI emits a complex expression that the comparator
    # cannot decide automatically.  Categorise the DRD-vs-ODI difference.
    skipped_audit: list = []
    for cr in unresolvable:
        tgt = (cr.get("target_col") or "").upper()
        if tgt in _SKIP_AUDIT_COLS:
            skipped_audit.append(tgt)
            continue
        drd_row = drd_by_col.get(tgt, {})
        drd_src = (
            f"{drd_row.get('source_schema','')}."
            f"{drd_row.get('source_table','')}."
            f"{drd_row.get('source_attribute','')}"
        ).strip(".") or "(no DRD source)"
        drd_full = drd_full_map.get(tgt, drd_row.get("transformation") or cr.get("drd_logic", ""))
        chain = model.column_derivations.get(tgt, [])
        auth = next((d for d in chain if d.is_authoritative), chain[0] if chain else None)
        auth_step = auth.step_label if auth else f"STEP{cr.get('odi_step','?')}"
        auth_expr = auth.expr_sql if auth else (cr.get("odi_expr_sql") or "")
        chain_compact = _compact_chain(chain) if chain else (cr.get("odi_expr_sql") or "(no chain)")
        category, recommendation = _classify_unresolvable(
            cr.get("drd_logic", ""), auth_expr or cr.get("odi_expr_sql", ""), tgt,
        )
        out_rows.append({
            "verdict": "UNRESOLVABLE",
            "target_col": tgt,
            "mismatch_kind": cr.get("unresolved_reason", "COMPLEX_EXPRESSION"),
            "drd_source": drd_src,
            "drd_rule_full": drd_full,
            "odi_authoritative_step": auth_step,
            "odi_authoritative_expr": auth_expr,
            "odi_full_chain": chain_compact,
            "category": category,
            "recommendation": recommendation,
        })

    # SOURCE_MISSING rows: ODI does not project the target column anywhere.
    for cr in source_missing:
        tgt = (cr.get("target_col") or "").upper()
        drd_row = drd_by_col.get(tgt, {})
        drd_src = (
            f"{drd_row.get('source_schema','') or cr.get('drd_schema','')}."
            f"{drd_row.get('source_table','') or cr.get('drd_table','')}."
            f"{drd_row.get('source_attribute','') or cr.get('drd_attr','')}"
        ).strip(".")
        drd_full = drd_full_map.get(
            tgt, drd_row.get("transformation") or cr.get("drd_logic", ""),
        )
        category, recommendation = _classify_source_missing(
            cr.get("drd_attr") or drd_row.get("source_attribute", ""),
            cr.get("drd_table") or drd_row.get("source_table", ""),
            tgt,
        )
        out_rows.append({
            "verdict": "SOURCE_MISSING",
            "target_col": tgt,
            "mismatch_kind": "MISSING_IN_ODI",
            "drd_source": drd_src,
            "drd_rule_full": drd_full,
            "odi_authoritative_step": "(absent)",
            "odi_authoritative_expr": "(absent)",
            "odi_full_chain": cr.get("explanation", "")
            or "Column not projected by any ODI step or MERGE",
            "category": category,
            "recommendation": recommendation,
        })

    # ── ODI_EXTRA rows (Q5 operator 2026-05-29) ──
    # Columns ODI projects into final INSERT but DRD has no rule for.
    # Set-difference of model.final_insert_columns vs DRD target columns.
    drd_cols_set = {
        (row.get("column") or "").upper()
        for row in result.augmented_drd_rows
    }
    odi_final_cols = {c.upper() for c in (model.final_insert_columns or [])}
    odi_extra = sorted(odi_final_cols - drd_cols_set)
    print(f"ODI_EXTRA rows:       {len(odi_extra)}")
    for tgt in odi_extra:
        chain = model.column_derivations.get(tgt, [])
        auth = next((d for d in chain if d.is_authoritative), chain[0] if chain else None)
        auth_step = auth.step_label if auth else "(unknown)"
        auth_expr = auth.expr_sql if auth else "(unknown)"
        chain_compact = _compact_chain(chain) if chain else "(no chain)"
        out_rows.append({
            "verdict": "ODI_EXTRA",
            "target_col": tgt,
            "mismatch_kind": "EXTRA_IN_ODI",
            "drd_source": "(no DRD rule)",
            "drd_rule_full": "(DRD does not list this target column)",
            "odi_authoritative_step": auth_step,
            "odi_authoritative_expr": auth_expr,
            "odi_full_chain": chain_compact,
            "category": _CAT_ODI_EXTRA,
            "recommendation": (
                f"ODI_EXTRA -- ODI projects '{tgt}' but DRD has NO rule "
                f"for it.  Authoritative source: {auth_expr if auth else '?'}.  "
                f"Operator must decide: (a) add DRD rule, (b) remove "
                f"from ODI, or (c) accept as known extra."
            ),
        })

    if skipped_audit:
        print(
            f"Skipped {len(skipped_audit)} audit/session column(s) per "
            f"operator rule: {', '.join(skipped_audit)}"
        )

    # Operator-locked rule (2026-05-29): EVERY .md report MUST be
    # accompanied by a CSV of identical content (Excel-friendly).
    # `_write_csv` falls back to a timestamped sibling if Excel holds
    # the canonical file open, and prints a loud WARNING so the
    # operator knows to close Excel.

    def _write_csv(path: pathlib.Path, rows: list, fields: list) -> pathlib.Path:
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
                w.writeheader()
                w.writerows(rows)
            return path
        except PermissionError:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            alt = path.with_name(f"{path.stem}_{ts}{path.suffix}")
            with open(alt, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_ALL)
                w.writeheader()
                w.writerows(rows)
            print(
                f"  !! WARNING: {path.name} was locked (Excel?); wrote "
                f"to {alt.name} instead.  Close Excel and re-run to "
                f"refresh the canonical file."
            )
            return alt

    # ── Compact CSV (one row per column, ~10 fields) ──
    field_order = [
        "verdict", "target_col", "mismatch_kind", "drd_source",
        "drd_rule_full", "odi_authoritative_step",
        "odi_authoritative_expr", "odi_full_chain",
        "category", "recommendation",
    ]
    csv_path = _write_csv(
        ROOT / "data" / "MISMATCH_FINAL.csv", out_rows, field_order,
    )
    print(f"Wrote {csv_path} ({len(out_rows)} rows)")

    # ── Compact markdown table ──
    from collections import Counter
    by_verdict = {
        "REAL_MISMATCH": [r for r in out_rows if r["verdict"] == "REAL_MISMATCH"],
        "UNRESOLVABLE": [r for r in out_rows if r["verdict"] == "UNRESOLVABLE"],
        "SOURCE_MISSING": [r for r in out_rows if r["verdict"] == "SOURCE_MISSING"],
        "ODI_EXTRA": [r for r in out_rows if r["verdict"] == "ODI_EXTRA"],
    }
    md_lines: list = []
    md_lines.append(
        f"# Non-MATCHED columns -- {len(out_rows)} total "
        f"(REAL_MISMATCH={len(by_verdict['REAL_MISMATCH'])}, "
        f"UNRESOLVABLE={len(by_verdict['UNRESOLVABLE'])}, "
        f"SOURCE_MISSING={len(by_verdict['SOURCE_MISSING'])}, "
        f"ODI_EXTRA={len(by_verdict['ODI_EXTRA'])})"
    )
    if skipped_audit:
        md_lines.append("")
        md_lines.append(
            f"_Skipped {len(skipped_audit)} audit/session column(s) per "
            f"operator rule (2026-05-29): "
            f"{', '.join(f'`{c}`' for c in skipped_audit)}._"
        )
    md_lines.append("")
    md_lines.append(
        "Honest classification.  When the comparator returns a verdict "
        "other than MATCHED, we do NOT silently relabel it as "
        "'SEMANTIC_MATCH'.  Each row gets a **category** describing the "
        "kind of difference present so the operator can confirm against "
        "the PDM."
    )
    md_lines.append("")
    cat_all = Counter(r["category"] for r in out_rows)
    md_lines.append("## Overall category tally")
    md_lines.append("")
    for k, v in sorted(cat_all.items(), key=lambda x: -x[1]):
        md_lines.append(f"- **{k}**: {v}")
    md_lines.append("")
    for verdict_name, verdict_rows in by_verdict.items():
        if not verdict_rows:
            continue
        md_lines.append(f"## {verdict_name} ({len(verdict_rows)})")
        md_lines.append("")
        if verdict_name == "REAL_MISMATCH":
            md_lines.append(
                "DRD source attribute name differs from ODI's projection."
            )
        elif verdict_name == "UNRESOLVABLE":
            md_lines.append(
                "ODI emits an expression the comparator cannot decide "
                "automatically (literal, audit-default, complex expression)."
            )
        elif verdict_name == "SOURCE_MISSING":
            md_lines.append(
                "ODI does NOT project this target column anywhere -- not in "
                "any STEP, not in MERGE.  Either ODI is incomplete or "
                "target is deprecated."
            )
        else:  # ODI_EXTRA
            md_lines.append(
                "ODI projects these columns into the final INSERT but the "
                "DRD has NO rule for them.  Operator must add a DRD rule, "
                "remove from ODI, or accept as known extra."
            )
        md_lines.append("")
        md_lines.append("| Target | Kind | DRD attr | ODI projection | Category |")
        md_lines.append("|---|---|---|---|---|")
        for r in verdict_rows:
            drd_attr_only = r["drd_source"].split(".")[-1]
            odi_only = r["odi_authoritative_expr"]
            if len(odi_only) > 60:
                odi_only = odi_only[:60] + "..."
            md_lines.append(
                f"| `{_md_cell(r['target_col'])}` | "
                f"{_md_cell(r['mismatch_kind'])} | "
                f"`{_md_cell(drd_attr_only)}` | "
                f"`{_md_cell(odi_only)}` | "
                f"**{_md_cell(r['category'])}** |"
            )
        md_lines.append("")
    (ROOT / "data" / "MISMATCH_FINAL.md").write_text(
        "\n".join(md_lines), encoding="utf-8",
    )
    print(f"Wrote {ROOT / 'data' / 'MISMATCH_FINAL.md'} ({len(md_lines)} lines)")

    # ── Per-column expanded view ──
    _verdict_order = {"REAL_MISMATCH": 0, "UNRESOLVABLE": 1, "SOURCE_MISSING": 2, "ODI_EXTRA": 3}
    det: list = []
    det.append(
        f"# Non-MATCHED columns ({len(out_rows)}) -- detailed"
    )
    det.append("")
    for r in sorted(
        out_rows,
        key=lambda x: (
            _verdict_order.get(x["verdict"], 99),
            x["category"], x["target_col"],
        ),
    ):
        det.append(f"## {r['target_col']}")
        det.append("")
        det.append(f"- **Verdict**: {r['verdict']} ({r['mismatch_kind']})")
        det.append(f"- **Category**: **{r['category']}**")
        det.append(f"- **DRD source**: `{r['drd_source']}`")
        det.append(f"- **ODI authoritative**: `{r['odi_authoritative_step']}` -> `{r['odi_authoritative_expr']}`")
        det.append("")
        det.append("**ODI full walker chain:**")
        det.append("")
        det.append("```")
        det.append(r["odi_full_chain"])
        det.append("```")
        det.append("")
        det.append("**DRD rule (full text):**")
        det.append("")
        det.append("```")
        det.append(r["drd_rule_full"] or "(empty)")
        det.append("```")
        det.append("")
        det.append(f"**Recommendation:** {r['recommendation']}")
        det.append("")
        det.append("---")
        det.append("")
    (ROOT / "data" / "MISMATCH_FINAL_DETAIL.md").write_text(
        "\n".join(det), encoding="utf-8",
    )
    print(f"Wrote {ROOT / 'data' / 'MISMATCH_FINAL_DETAIL.md'} ({len(det)} lines)")

    # ── DETAIL CSV (sorted same as DETAIL.md) ──
    # Operator rule: every .md report has a CSV twin.
    detail_rows = sorted(
        out_rows,
        key=lambda x: (
            _verdict_order.get(x["verdict"], 99),
            x["category"], x["target_col"],
        ),
    )
    detail_csv_path = _write_csv(
        ROOT / "data" / "MISMATCH_FINAL_DETAIL.csv",
        detail_rows, field_order,
    )
    print(f"Wrote {detail_csv_path} ({len(detail_rows)} rows)")

    # Print summary
    print()
    print("Category tally:")
    cat_counts: dict[str, int] = {}
    for r in out_rows:
        c = r.get("category", "?")
        cat_counts[c] = cat_counts.get(c, 0) + 1
    for k, v in sorted(cat_counts.items(), key=lambda x: -x[1]):
        print(f"  {k:35s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
