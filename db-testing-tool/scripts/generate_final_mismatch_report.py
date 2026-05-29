"""Generate a clean, focused report with ONLY the remaining REAL_MISMATCH
columns -- no overlay from any prior operator-annotated CSV.

Outputs:
  data/MISMATCH_FINAL.csv          tabular, Excel-friendly
  data/MISMATCH_FINAL.md           markdown table for at-a-glance review
  data/MISMATCH_FINAL_DETAIL.md    per-column expanded view

Each row carries:
  target_col, mismatch_kind, drd_source, drd_rule_full,
  odi_authoritative_step, odi_authoritative_expr, odi_full_chain,
  human_explanation, recommendation
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

# Reuse the classifier helpers from the existing enrichment script
from enrich_mismatch_review import (  # noqa: E402
    _build_drd_full_rule_map,
    _build_source_attr_index,
    _classify_and_explain,
    _extract_apa_filters,
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
    apa_cash_filter, apa_security_filter = _extract_apa_filters(model)
    source_attr_index = _build_source_attr_index(model)

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

        # Run the classifier (treat as if not annotated)
        classify_row = {
            "target_col": tgt,
            "mismatch_kind": cr.get("mismatch_kind", ""),
            "drd_source": drd_src,
            "odi_staging_chain": chain_compact,
        }
        explanation, recommendation = _classify_and_explain(
            classify_row, drd_full, apa_cash_filter, apa_security_filter,
            source_attr_index=source_attr_index,
        )
        out_rows.append({
            "target_col": tgt,
            "mismatch_kind": cr.get("mismatch_kind", ""),
            "drd_source": drd_src,
            "drd_rule_full": drd_full,
            "odi_authoritative_step": auth_step,
            "odi_authoritative_expr": auth_expr,
            "odi_full_chain": chain_compact,
            "human_explanation": explanation,
            "recommendation": recommendation,
        })

    # ── CSV ──
    field_order = [
        "target_col", "mismatch_kind", "drd_source", "drd_rule_full",
        "odi_authoritative_step", "odi_authoritative_expr",
        "odi_full_chain", "human_explanation", "recommendation",
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
    from collections import Counter
    rec = Counter(r["recommendation"].split(" --")[0] for r in out_rows)
    md_lines.append("Recommendation tally:")
    for k, v in sorted(rec.items(), key=lambda x: -x[1]):
        md_lines.append(f"  - **{k}**: {v}")
    md_lines.append("")
    md_lines.append("| Target | Kind | DRD source | ODI authoritative | Recommendation |")
    md_lines.append("|---|---|---|---|---|")
    for r in out_rows:
        rec_short = r["recommendation"][:80]
        md_lines.append(
            f"| `{_md_cell(r['target_col'])}` | "
            f"{_md_cell(r['mismatch_kind'])} | "
            f"`{_md_cell(r['drd_source'])}` | "
            f"`{_md_cell(r['odi_authoritative_step'])}: "
            f"{_md_cell(r['odi_authoritative_expr'][:60])}` | "
            f"{_md_cell(rec_short)} |"
        )
    (ROOT / "data" / "MISMATCH_FINAL.md").write_text(
        "\n".join(md_lines), encoding="utf-8",
    )
    print(f"Wrote {ROOT / 'data' / 'MISMATCH_FINAL.md'} ({len(md_lines)} lines)")

    # ── Per-column expanded view ──
    det: list = []
    det.append(f"# Remaining REAL_MISMATCH columns ({len(out_rows)}) -- detailed")
    det.append("")
    for r in sorted(out_rows, key=lambda x: x["mismatch_kind"] + x["target_col"]):
        det.append(f"## {r['target_col']}")
        det.append("")
        det.append(f"- **Verdict**: REAL_MISMATCH ({r['mismatch_kind']})")
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
        det.append(f"**Human explanation:** {r['human_explanation']}")
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
    print("Recommendation tally:")
    for k, v in sorted(rec.items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
