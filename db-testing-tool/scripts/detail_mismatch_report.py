"""Generate a detailed side-by-side DRD vs ODI report for every REAL_MISMATCH
column.  For each mismatched column, dumps:

  * Full DRD rule (transformation + source_attribute + ETL block body)
  * ODI staging chain: which step(s) introduce the column, with the
    actual ODI SELECT-list expression captured per step
  * Mismatch kind + comparator explanation

Output: ``data/MISMATCH_DETAIL.md`` grouped by mismatch_kind for review.

Operator-locked invariants:
  * Generic -- works for any DRD / ODI pair, no business-name hardcoding.
  * Read-only.  Does not mutate the comparator or emitter state.
"""
from __future__ import annotations

import pathlib
import re
import sys
from collections import defaultdict
from typing import Dict, List

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.v9_pipeline import generate_v9  # noqa: E402
from app.sql_model.odi_parser import OdiXmlParser  # noqa: E402


def _proj_tail_re(col: str) -> re.Pattern:
    return re.compile(
        r"(?:^|\n|,)\s*([^,\n]+?)\s+(?:AS\s+)?"
        + re.escape(col)
        + r"\s*(?=,|\n|$)",
        re.IGNORECASE,
    )


def _odi_chain_for(model, col: str) -> List[Dict[str, str]]:
    """Return the ordered ODI derivation chain for ``col``.

    Operator-locked 2026-05-29: prefer ``model.column_derivations`` (the
    deep walker output built on sqlglot AST) when populated.  Falls back
    to the legacy regex-based search only for backward compatibility with
    callers that didn't run the walker.
    """
    if getattr(model, "column_derivations", None):
        chain = model.column_derivations.get((col or "").upper(), [])
        if chain:
            return [
                {
                    "step": d.step_label,
                    "expr": d.expr_sql,
                    "expr_kind": d.expr_kind,
                    "is_authoritative": d.is_authoritative,
                }
                for d in chain
            ]
    # Legacy fallback (regex search) -- used when walker enrichment is
    # disabled or sqlglot is unavailable.
    pat = _proj_tail_re(col)
    chain = []
    for step in model.staging_steps:
        sql = step.select_sql or ""
        m = pat.search(sql)
        if m:
            chain.append({
                "step": f"STEP{step.step_id} ({step.name})",
                "expr": m.group(1).strip(),
            })
    fs = model.final_select_sql or ""
    if fs:
        m = pat.search(fs)
        if m:
            chain.append({"step": "MERGE", "expr": m.group(1).strip()})
    return chain


def _join_summary(model, col: str) -> List[str]:
    """Return per-step join graph entries that mention ``col`` in their ON."""
    rows: List[str] = []
    target_up = col.upper()
    for step in model.staging_steps:
        for edge in step.join_graph:
            on_sql = (edge.on_sql or "").upper()
            if f".{target_up}" in on_sql or f" {target_up} " in f" {on_sql} ":
                joined = (edge.joined.ref.fq if edge.joined.ref else "")
                rows.append(
                    f"STEP{step.step_id}: {edge.driving.alias}.{edge.driving.ref.table} "
                    f"LEFT JOIN {joined} {edge.joined.alias} "
                    f"ON {edge.on_sql[:120].strip()}"
                )
    return rows


def _truncate(text: str, n: int = 400) -> str:
    if not text:
        return ""
    t = text.strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + " ..."


def _md_code(text: str) -> str:
    """Wrap a code block in markdown fences with safety for backticks."""
    if not text:
        return ""
    if "```" in text:
        text = text.replace("```", "ʼʼʼ")
    return "```\n" + text + "\n```"


def main() -> None:
    drd_path = ROOT / "DRD_Activity_Fact.xlsx"
    odi_path = ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
    out_path = ROOT / "data" / "MISMATCH_DETAIL.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("Loading inputs ...")
    result = generate_v9(
        drd_bytes=drd_path.read_bytes(),
        drd_filename=drd_path.name,
        odi_xml_bytes=odi_path.read_bytes(),
        target_schema="ALY_FACT_OWNER",
        target_table="AVY_FACT_SIDE",
    )
    model = OdiXmlParser().parse_bytes(odi_path.read_bytes())

    # Index augmented rows by target column for fast lookup
    drd_by_col: Dict[str, Dict] = {}
    for r in result.augmented_drd_rows:
        c = (r.get("column") or r.get("physical_name") or "").upper()
        if c:
            drd_by_col[c] = r

    mismatched = [
        cr for cr in result.comparison_rows
        if cr.get("verdict") == "REAL_MISMATCH"
    ]

    # Group by mismatch_kind
    by_kind: Dict[str, List[Dict]] = defaultdict(list)
    for cr in mismatched:
        by_kind[cr.get("mismatch_kind") or "(none)"].append(cr)

    print(f"REAL_MISMATCH rows: {len(mismatched)}")
    for k, v in by_kind.items():
        print(f"  {k}: {len(v)}")

    out: List[str] = []
    out.append("# Detailed REAL_MISMATCH report -- DRD vs ODI side-by-side")
    out.append("")
    out.append(f"Generated for: ALY_FACT_OWNER.AVY_FACT_SIDE")
    out.append(f"Total REAL_MISMATCH rows: {len(mismatched)}")
    out.append("")
    out.append("Mismatch kind summary:")
    for k in sorted(by_kind, key=lambda x: -len(by_kind[x])):
        out.append(f"  - **{k}**: {len(by_kind[k])} rows")
    out.append("")
    out.append("---")
    out.append("")

    for kind in sorted(by_kind, key=lambda x: -len(by_kind[x])):
        rows = by_kind[kind]
        out.append(f"## {kind} ({len(rows)} rows)")
        out.append("")
        for cr in rows:
            tgt = cr.get("target_col") or ""
            drd_row = drd_by_col.get(tgt.upper(), {})
            drd_src = (
                f"{drd_row.get('source_schema','')}."
                f"{drd_row.get('source_table','')}."
                f"{drd_row.get('source_attribute','')}"
            ).strip(".")
            trans = drd_row.get("transformation") or ""
            etl_ref = drd_row.get("etl_block_ref") or ""
            etl_body = drd_row.get("etl_block_body") or ""

            chain = _odi_chain_for(model, tgt)
            joins = _join_summary(model, tgt)

            out.append(f"### {tgt}")
            out.append("")
            out.append(f"- **Verdict**: REAL_MISMATCH ({kind})")
            out.append(f"- **Explanation**: {_truncate(cr.get('explanation') or '', 200)}")
            out.append("")
            out.append("**DRD side:**")
            out.append("")
            out.append(f"- Source: `{drd_src or '(empty)'}`")
            if etl_ref:
                out.append(f"- ETL block ref: `{etl_ref}`")
            out.append("- Transformation rule:")
            out.append("")
            out.append(_md_code(_truncate(trans, 800)))
            if etl_body:
                out.append("- ETL block body (excerpt):")
                out.append("")
                out.append(_md_code(_truncate(etl_body, 600)))
            out.append("")
            out.append("**ODI side (staging chain):**")
            out.append("")
            if chain:
                out.append("| Step | Projected expression |")
                out.append("|---|---|")
                for c in chain:
                    expr = c["expr"].replace("\n", " / ").replace("|", "\\|")
                    out.append(f"| {c['step']} | `{_truncate(expr, 200)}` |")
            else:
                out.append("_(column does not appear in ODI staging or MERGE projection)_")
            if joins:
                out.append("")
                out.append("**ODI joins referencing this column in ON clause:**")
                out.append("")
                for j in joins:
                    out.append(f"- {j}")
            out.append("")
            out.append("---")
            out.append("")

    out_path.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {out_path}  ({len(out)} lines)")

    # ── Also write the compact tabular companion ─────────────────────────
    # One row per REAL_MISMATCH column.  Columns:
    #   target | kind | drd_source | drd_rule_compact | odi_chain_compact
    def _compact_rule(text: str) -> str:
        if not text:
            return ""
        # Collapse all whitespace runs to single space.
        t = re.sub(r"\s+", " ", text.strip()).rstrip(";").strip()
        if len(t) > 220:
            t = t[:220].rstrip() + " ..."
        return t

    def _compact_chain(chain: list) -> str:
        if not chain:
            return "ABSENT: column never appears in any ODI step"
        # New format (walker-based): each entry has expr_kind + is_authoritative.
        # Authoritative entry is the deepest non-pass-through step; we render
        # it with a leading '*' marker for at-a-glance scanning.
        has_kinds = any("expr_kind" in c for c in chain)
        if has_kinds:
            # Detect "all pass-through" -> ODI gap label
            non_pt = [c for c in chain if c.get("expr_kind") != "passthrough"]
            if not non_pt:
                last = chain[-1]
                return (
                    f"NO DERIVATION (ODI gap): pass-through "
                    f"{last['expr'][:80] if last.get('expr') else '(empty)'}"
                )
            parts = []
            for c in chain:
                marker = "*" if c.get("is_authoritative") else " "
                kind = c.get("expr_kind", "?")
                expr = re.sub(r"\s+", " ", (c.get("expr") or "").strip())
                if len(expr) > 80:
                    expr = expr[:80].rstrip() + "..."
                parts.append(f"{marker}{c['step']}[{kind}]: {expr}")
            return " -> ".join(parts)
        # Legacy fallback (regex-based chain shape) -- keep existing logic.
        is_passthrough_only = all(
            (not c.get("expr")) or ("_STG_RT." in c.get("expr", "").upper())
            for c in chain
        )
        if is_passthrough_only:
            last = chain[-1]
            return f"NO DERIVATION (ODI gap): pass-through {last.get('expr') or '(empty)'}"
        parts = []
        for c in chain:
            if not c.get("expr"):
                continue
            expr = re.sub(r"\s+", " ", c["expr"].strip())
            if len(expr) > 80:
                expr = expr[:80].rstrip() + "..."
            parts.append(f"{c['step'].split(' ')[0]}: {expr}")
        return " -> ".join(parts) if parts else "NO DERIVATION (ODI gap)"

    md_table: List[str] = []
    md_table.append("# Compact REAL_MISMATCH table (184 rows)")
    md_table.append("")
    md_table.append(
        "Target column | Kind | DRD source | DRD rule (compact) | ODI staging chain"
    )
    md_table.append("---|---|---|---|---")

    csv_rows: List[str] = []
    csv_rows.append("target_col,mismatch_kind,drd_source,drd_rule,odi_staging_chain,odi_join_predicates")

    for kind in sorted(by_kind, key=lambda x: -len(by_kind[x])):
        for cr in by_kind[kind]:
            tgt = cr.get("target_col") or ""
            drd_row = drd_by_col.get(tgt.upper(), {})
            drd_src = (
                f"{drd_row.get('source_schema','')}."
                f"{drd_row.get('source_table','')}."
                f"{drd_row.get('source_attribute','')}"
            ).strip(".")
            rule_compact = _compact_rule(drd_row.get("transformation") or "")
            chain_compact = _compact_chain(_odi_chain_for(model, tgt))
            join_lines = _join_summary(model, tgt)
            join_compact = " ; ".join(
                re.sub(r"\s+", " ", j).strip()
                for j in join_lines[:3]
            )

            # md row: escape pipe + backtick chars
            def _md_cell(s: str) -> str:
                return s.replace("|", "\\|").replace("`", "'")

            md_table.append(
                f"`{tgt}` | {kind} | `{_md_cell(drd_src) or '-'}` | "
                f"{_md_cell(rule_compact) or '-'} | "
                f"{_md_cell(chain_compact)}"
            )

            # CSV: properly quoted
            def _csv_cell(s: str) -> str:
                if s is None:
                    return ""
                s = s.replace('"', '""').replace("\n", " ")
                return f'"{s}"'

            csv_rows.append(
                f"{_csv_cell(tgt)},{_csv_cell(kind)},{_csv_cell(drd_src)},"
                f"{_csv_cell(rule_compact)},{_csv_cell(chain_compact)},"
                f"{_csv_cell(join_compact)}"
            )

    md_table_path = ROOT / "data" / "MISMATCH_TABLE.md"
    md_table_path.write_text("\n".join(md_table), encoding="utf-8")
    print(f"Wrote {md_table_path}  ({len(md_table)} lines)")

    csv_path = ROOT / "data" / "MISMATCH_TABLE.csv"
    try:
        csv_path.write_text("\n".join(csv_rows), encoding="utf-8")
        print(f"Wrote {csv_path}  ({len(csv_rows)} rows incl header)")
    except PermissionError as e:
        # File likely open in Excel; non-fatal -- markdown copies are enough.
        print(f"CSV write skipped (file in use): {e}")


if __name__ == "__main__":
    main()
