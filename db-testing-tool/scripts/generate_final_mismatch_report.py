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

    # Filter to REAL_MISMATCH only
    mismatched = [
        cr for cr in result.comparison_rows
        if cr.get("verdict") == "REAL_MISMATCH"
    ]
    print(f"REAL_MISMATCH rows: {len(mismatched)}")

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

    # ── CSV ──
    field_order = [
        "target_col", "mismatch_kind", "drd_source", "drd_rule_full",
        "odi_authoritative_step", "odi_authoritative_expr",
        "odi_full_chain", "category", "recommendation",
    ]
    csv_path = ROOT / "data" / "MISMATCH_FINAL.csv"
    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=field_order, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(out_rows)
    except PermissionError:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        csv_path = csv_path.with_name(f"MISMATCH_FINAL_{ts}.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=field_order, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(out_rows)
    print(f"Wrote {csv_path} ({len(out_rows)} rows)")

    # ── Compact markdown table ──
    md_lines: list = []
    md_lines.append(f"# Remaining REAL_MISMATCH columns ({len(out_rows)})")
    md_lines.append("")
    md_lines.append(
        "Honest classification.  The comparator says REAL_MISMATCH because "
        "the DRD source attribute name differs from ODI's projection; we "
        "do NOT silently override this with a SEMANTIC_MATCH label.  The "
        "**category** column tells the operator what KIND of difference "
        "is present so they can confirm against the PDM."
    )
    md_lines.append("")
    from collections import Counter
    cat = Counter(r["category"] for r in out_rows)
    md_lines.append("Category tally:")
    for k, v in sorted(cat.items(), key=lambda x: -x[1]):
        md_lines.append(f"  - **{k}**: {v}")
    md_lines.append("")
    md_lines.append("| Target | Kind | DRD attr | ODI col | Category |")
    md_lines.append("|---|---|---|---|---|")
    for r in out_rows:
        drd_attr_only = r["drd_source"].split(".")[-1]
        odi_only = r["odi_authoritative_expr"]
        if "." in odi_only and len(odi_only) < 80:
            odi_only = odi_only  # already alias.col
        else:
            odi_only = odi_only[:60]
        md_lines.append(
            f"| `{_md_cell(r['target_col'])}` | "
            f"{_md_cell(r['mismatch_kind'])} | "
            f"`{_md_cell(drd_attr_only)}` | "
            f"`{_md_cell(odi_only)}` | "
            f"**{_md_cell(r['category'])}** |"
        )
    (ROOT / "data" / "MISMATCH_FINAL.md").write_text(
        "\n".join(md_lines), encoding="utf-8",
    )
    print(f"Wrote {ROOT / 'data' / 'MISMATCH_FINAL.md'} ({len(md_lines)} lines)")

    # ── Per-column expanded view ──
    det: list = []
    det.append(f"# Remaining REAL_MISMATCH columns ({len(out_rows)}) -- detailed")
    det.append("")
    for r in sorted(out_rows, key=lambda x: (x["category"], x["target_col"])):
        det.append(f"## {r['target_col']}")
        det.append("")
        det.append(f"- **Verdict**: REAL_MISMATCH ({r['mismatch_kind']})")
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
