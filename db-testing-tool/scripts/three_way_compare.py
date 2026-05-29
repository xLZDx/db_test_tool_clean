"""3-way deviation report: DRD spec vs ODI implementation vs Generated v9.

Operator-locked (2026-05-29):
  * Uses the shared rule engine (``app.sql_model.drd_rules``) for derivation
    detection -- the SAME logic the emitter uses to produce the v9 SQL.
  * Walks ODI's full multi-step staging chain (STEP1..STEPN + MERGE) so the
    operator sees what each step contributes to a target column.
  * Generic -- no hardcoded business / column / schema names.

For each of N target columns the report shows:
  | TARGET | DRD (transformation + ETL block) | ODI multi-step trace | GEN v9 projection | verdict |

Output files:
  data/THREE_WAY_COMPARISON.json   -- machine-readable, all evidence
  data/THREE_WAY_COMPARISON.md     -- human-readable summary + grid
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.sql_model.comparator import compare_drd_rows_to_model
from app.sql_model.drd_first_emitter import emit_insert_drd_first
from app.sql_model.drd_multi_sheet import parse_all_sheets, SheetRole
from app.sql_model.drd_rules import (
    extract_applicable_only_code,
    extract_exists_derived_flag,
    find_discriminator_for_code,
)
from app.sql_model.etl_block_index import (
    build_block_index,
    find_block_references,
    resolve_block_body,
)
from app.sql_model.odi_parser import OdiXmlParser
from app.sql_model.oracle_validator import validate_oracle_sql
from app.sql_model.types import MismatchKind, ODIModel
from app.services.control_table_service import (
    build_control_table_ddl,
    load_target_table_definition,
)


# ── ODI multi-step trace per column ──────────────────────────────────────────

_PROJ_RE_CACHE: Dict[str, re.Pattern] = {}


def _proj_re(col: str) -> re.Pattern:
    key = col.upper()
    pat = _PROJ_RE_CACHE.get(key)
    if pat is None:
        pat = re.compile(
            r"(?:^|\n|,)\s*([^,\n]+?)\s+(?:AS\s+)?"
            + re.escape(key)
            + r"\s*(?=,|\n|$)",
            re.IGNORECASE,
        )
        _PROJ_RE_CACHE[key] = pat
    return pat


def odi_chain_for_column(model: ODIModel, col: str) -> List[Dict[str, str]]:
    """Walk every staging step (STEP1..STEPN) + MERGE block; return a list
    of ``{step: 'STEP3', source_expr: '...'}`` items showing what each layer
    projects for ``col``.  Generic text search; no hardcoded names."""
    out: List[Dict[str, str]] = []
    if not model or not col:
        return out
    pat = _proj_re(col)
    for step in model.staging_steps:
        sql = step.select_sql or ""
        m = pat.search(sql)
        if m is not None:
            expr = m.group(1).strip()
            if expr.upper() != col.upper():
                out.append({"step": step.name or f"STEP{step.step_id}", "source_expr": expr})
    fs = model.final_select_sql or ""
    if fs:
        m = pat.search(fs)
        if m is not None:
            expr = m.group(1).strip()
            if expr.upper() != col.upper():
                out.append({"step": "MERGE", "source_expr": expr})
    return out


# ── Gen v9 source-expr lookup ─────────────────────────────────────────────────

_GEN_LINE_RE = re.compile(
    r"^\s+(.+?)\s+AS\s+([A-Z][A-Z0-9_]*)(?:,|\s)",
    re.IGNORECASE,
)


def parse_gen_projections(sql: str) -> Dict[str, Dict[str, str]]:
    """Return ``{target_col: {'source_expr': ..., 'provenance': ...}}`` for every
    AS-aliased projection in the SELECT list of the generated INSERT."""
    out: Dict[str, Dict[str, str]] = {}
    for ln in sql.splitlines():
        m = _GEN_LINE_RE.match(ln)
        if not m:
            continue
        source_expr = m.group(1).strip()
        target = m.group(2).strip().upper()
        # Extract provenance from trailing -- [PROV] note
        prov = ""
        if "--" in ln:
            tail = ln.split("--", 1)[1].strip()
            pm = re.match(r"\[([A-Z_]+)\]", tail)
            if pm:
                prov = pm.group(1)
        if target not in out:
            out[target] = {"source_expr": source_expr, "provenance": prov}
    return out


# ── Per-column 3-way classification ──────────────────────────────────────────

def classify_row(
    *,
    target: str,
    drd_row: Dict[str, Any],
    odi_chain: List[Dict[str, str]],
    gen_proj: Optional[Dict[str, str]],
    all_etl_text: str,
    comp_result: Optional[Any] = None,
) -> Dict[str, Any]:
    """Apply the SHARED rule engine to decide:
      * what DRD expects (applicable-only / EXISTS-derived / pass-through)
      * what ODI actually does (top of multi-step chain)
      * whether GEN reproduces the DRD intent

    Returns a structured dict suitable for both the JSON dump and the
    markdown table.
    """
    transformation = (drd_row.get("transformation") or "")
    raw_source_attr = (drd_row.get("source_attribute") or "").strip()
    # Apply the same PAREN-NOTE strip the comparator uses (drops "(FROM T2)",
    # "(FROM T)" alias-hint annotations) so the classifier sees the same bare
    # column the comparator does.
    import re as _re
    drd_source_attr = _re.sub(r"\s+\([A-Z0-9_ ]+\)\s*$", "", raw_source_attr).upper()
    drd_source_table = (drd_row.get("source_table") or "").strip().upper().split("\n")[0]
    drd_source_schema = (drd_row.get("source_schema") or "").strip().upper()
    etl_ref = (drd_row.get("etl_block_ref") or "").strip().upper()
    etl_body = (drd_row.get("etl_block_body") or "").strip()

    # Shared rule engine: what category of DRD rule applies?
    ap_code = extract_applicable_only_code(transformation)
    exists_spec = extract_exists_derived_flag(transformation)

    drd_intent: str
    drd_expected_shape: str
    if exists_spec is not None:
        drd_intent = "EXISTS_DERIVED_FLAG"
        drd_expected_shape = (
            f"CASE WHEN EXISTS(SELECT 1 FROM {exists_spec['table']} WHERE ...) "
            f"THEN '{exists_spec['set_value']}' ELSE NULL END"
        )
    elif ap_code:
        discrim = (
            find_discriminator_for_code(etl_body, ap_code)
            or find_discriminator_for_code(all_etl_text, ap_code)
        )
        drd_intent = "APPLICABLE_FILTER" if discrim else "APPLICABLE_FILTER_NO_DISCRIM"
        if discrim:
            drd_expected_shape = (
                f"CASE WHEN {discrim[0]}.{discrim[1]} = '{ap_code}' THEN <source> ELSE NULL END"
            )
        else:
            drd_expected_shape = f"<source> (filtered for {ap_code})"
    elif drd_source_attr and drd_source_table:
        drd_intent = "PHYSICAL"
        drd_expected_shape = f"{drd_source_table}.{drd_source_attr}"
    elif transformation:
        drd_intent = "PROSE_RULE"
        drd_expected_shape = transformation[:80]
    else:
        drd_intent = "EMPTY"
        drd_expected_shape = ""

    # ODI source-of-truth: prefer the comparator's already-resolved
    # (odi_table, odi_col, odi_logic) since it walked the staging chain back
    # to a real source.  Fall back to raw text chain only when not available.
    odi_resolved_table = ""
    odi_resolved_col = ""
    odi_logic = ""
    if comp_result is not None:
        odi_resolved_table = (getattr(comp_result, "odi_table", "") or "").upper()
        odi_resolved_col = (getattr(comp_result, "odi_col", "") or "").upper()
        odi_logic = (getattr(comp_result, "odi_logic", "") or "").strip()
    odi_top = odi_logic
    # Always also pick the DEEPEST non-pass-through staging-step expression
    # so the report can still surface derivation chains visually.
    odi_deepest_derivation = ""
    if odi_chain:
        for c in odi_chain:
            expr = c["source_expr"].strip()
            # A "pass-through" expression is just <staging_table>.<col>; we
            # detect by checking the table prefix matches one of the model's
            # own staging step names -- but for the report level we just
            # take the first expression that doesn't END with the same col
            # ref (a heuristic for non-trivial derivation).
            if "CASE" in expr.upper() or "(" in expr:
                odi_deepest_derivation = expr
                break
    odi_top_up = odi_top.upper()

    # Does ODI implement the DRD intent?
    odi_has_case = "CASE" in odi_top_up
    odi_has_max_case = "MAX" in odi_top_up and "CASE" in odi_top_up
    odi_has_exists = "EXISTS" in odi_top_up
    odi_has_code = bool(ap_code) and (f"'{ap_code}'" in odi_top_up)
    # Shared subset-CTE prefix detector: ODI subset-CTEs (e.g. APA_CASH,
    # APA_SECURITY) rename columns with role prefixes (SEC_, CASH_, OFST_,
    # ...).  Use the comparator's _odi_expr_references_column helper which
    # already implements the generic prefix-modulo match.
    from app.sql_model.comparator import _odi_expr_references_column as _ref
    drd_attr_for_check = drd_source_attr or target
    odi_semantic_match = bool(odi_top) and _ref(odi_top, drd_attr_for_check)
    drd_odi_agree: str
    if drd_intent == "EXISTS_DERIVED_FLAG":
        # MAX(CASE WHEN <cond> THEN '<V>') is the aggregate-equivalent of
        # EXISTS(SELECT 1 ... WHERE <cond>) THEN '<V>'.  Treat as match.
        drd_odi_agree = "YES" if (odi_has_exists or odi_has_max_case) else "NO_ODI_PASS_THROUGH"
    elif drd_intent == "APPLICABLE_FILTER":
        # Either ODI does the CASE itself OR uses a subset-CTE that's
        # pre-filtered by the same code (detected via prefix-modulo match
        # because the subset rename uses role prefixes).
        if odi_has_case and odi_has_code:
            drd_odi_agree = "YES"
        elif odi_semantic_match:
            drd_odi_agree = "SUBSET_CTE_MATCH"
        else:
            drd_odi_agree = "NO_ODI_UNFILTERED"
    elif drd_intent == "PHYSICAL":
        bare_drd_tbl = drd_source_table.split(".")[-1]
        odi_tbl_bare = odi_resolved_table.split(".")[-1]
        # Detect "ODI underspecified": ODI's top projection is a bare
        # ``<staging_or_anything>.<target_col>`` -- the staging chain never
        # connects it to a real source.  Reclassify so the operator sees this
        # is an ODI gap, NOT a true table/column mismatch.
        odi_pass_through_re = re.compile(r"^([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)$", re.I)
        is_odi_underspecified = False
        if not odi_resolved_col and odi_top:
            m = odi_pass_through_re.match(odi_top.strip())
            if m:
                ref_table = m.group(1).upper()
                ref_col = m.group(2).upper()
                if ref_col == target and ("_STG" in ref_table or "_RT" in ref_table or "STAGING" in ref_table):
                    is_odi_underspecified = True

        # Semantic-equivalence detector (shared comparator helper): the ODI
        # text expression may wrap the same underlying column ref in NVL /
        # COALESCE / CASE / subset-CTE alias -- treat as MATCH when the
        # leaf column name lines up with the DRD source_attribute.
        from app.sql_model.comparator import _odi_expr_references_column
        semantic_match = (
            bool(drd_source_attr) and _odi_expr_references_column(odi_top, drd_source_attr)
        )

        if is_odi_underspecified:
            drd_odi_agree = "ODI_UNDERSPECIFIED"
        elif odi_resolved_col and odi_resolved_col == drd_source_attr and odi_tbl_bare == bare_drd_tbl:
            drd_odi_agree = "YES"
        elif semantic_match:
            # ODI wraps the same column in NVL/COALESCE/CASE/subset-CTE alias.
            drd_odi_agree = "SEMANTIC_MATCH"
        elif odi_resolved_col == drd_source_attr and odi_resolved_col:
            drd_odi_agree = "ALIAS_DRIFT_ONLY"
        elif odi_tbl_bare == bare_drd_tbl and odi_tbl_bare:
            drd_odi_agree = "TABLE_OK_COL_DIFF"
        else:
            drd_odi_agree = "DIFFERENT"
    elif drd_intent == "PROSE_RULE":
        drd_odi_agree = "MANUAL_VERIFY"
    else:
        drd_odi_agree = "NO_DRD_RULE"

    # Does GEN v9 implement the DRD intent?
    gen_expr = (gen_proj or {}).get("source_expr", "")
    gen_prov = (gen_proj or {}).get("provenance", "")
    gen_up = gen_expr.upper()
    gen_has_case = "CASE" in gen_up
    gen_has_exists = "EXISTS" in gen_up
    gen_has_code = bool(ap_code) and (f"'{ap_code}'" in gen_up)
    if drd_intent == "EXISTS_DERIVED_FLAG":
        drd_gen_agree = "YES" if gen_has_exists else "NO_GEN_MISS"
    elif drd_intent == "APPLICABLE_FILTER":
        drd_gen_agree = "YES" if (gen_has_case and gen_has_code) else "NO_GEN_UNFILTERED"
    elif drd_intent == "PHYSICAL":
        # Generic: GEN must reference the same source column name
        drd_gen_agree = "YES" if drd_source_attr in gen_up else "PARTIAL_OR_UNKNOWN"
    elif drd_intent == "PROSE_RULE":
        drd_gen_agree = (
            "FOLLOWED_AS_FALLBACK"
            if gen_prov in {"DRD_EXISTS_DERIVED_FLAG", "DRD_PHYSICAL", "DRD_PHYSICAL_CASE"}
            else "MANUAL_VERIFY"
        )
    elif drd_intent == "EMPTY":
        drd_gen_agree = "NA"
    else:
        drd_gen_agree = "MANUAL_VERIFY"

    # 3-way summary verdict (operator-locked: distinguish "ODI doesn't
    # populate" from "ODI projects from a real but different table"; flag
    # ETL audit cols as system-managed not a drift).
    is_etl_default = (gen_prov == "ETL_DEFAULT")
    if drd_intent in ("EMPTY",):
        verdict = "DRD_EMPTY"
    elif is_etl_default:
        # Gen system-manages an audit/ETL column. Whatever DRD says, this is
        # intentional. The operator can override etl_column_defaults if not.
        verdict = "ETL_DEFAULT_OK"
    elif drd_odi_agree in ("YES", "SEMANTIC_MATCH", "SUBSET_CTE_MATCH") and drd_gen_agree == "YES":
        verdict = "ALL_AGREE"
    elif drd_odi_agree == "ODI_UNDERSPECIFIED" and drd_gen_agree == "YES":
        verdict = "GEN_RESOLVES_ODI_UNDERSPEC"  # ODI gap, Gen fills it from DRD
    elif drd_odi_agree == "ALIAS_DRIFT_ONLY" and drd_gen_agree == "YES":
        verdict = "ALIAS_DRIFT_GEN_OK"
    elif drd_odi_agree != "YES" and drd_gen_agree == "YES":
        verdict = "GEN_FIXES_ODI_DRIFT"
    elif drd_odi_agree == "YES" and drd_gen_agree != "YES":
        verdict = "GEN_DRIFTS_FROM_DRD"
    else:
        verdict = "ALL_DIFFER"

    return {
        "target": target,
        "drd_intent": drd_intent,
        "drd_expected_shape": drd_expected_shape,
        "drd_source": (
            f"{drd_source_schema}.{drd_source_table}.{drd_source_attr}"
            if drd_source_table else "(none)"
        ),
        "drd_transformation_excerpt": transformation.strip()[:160].replace("\n", " / "),
        "etl_block_ref": etl_ref,
        "odi_chain_count": len(odi_chain),
        "odi_resolved_source": (
            f"{odi_resolved_table}.{odi_resolved_col}" if odi_resolved_col else ""
        ),
        "odi_top_expr": odi_top[:140],
        "odi_deepest_derivation": odi_deepest_derivation[:140],
        "odi_chain": [
            {"step": c["step"], "expr": c["source_expr"][:120]} for c in odi_chain
        ],
        "gen_provenance": gen_prov,
        "gen_source_expr": gen_expr[:140],
        "drd_odi_agree": drd_odi_agree,
        "drd_gen_agree": drd_gen_agree,
        "verdict": verdict,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("Loading inputs via shared v9 pipeline (same as GUI) ...")
    from app.services.v9_pipeline import generate_v9
    drd_bytes = open(ROOT / "DRD_Activity_Fact.xlsx", "rb").read()
    xml_bytes = open(ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml", "rb").read()
    v9 = generate_v9(
        drd_bytes=drd_bytes, drd_filename="DRD_Activity_Fact.xlsx",
        odi_xml_bytes=xml_bytes,
        target_schema="IKOROSTELEV", target_table="AVY_FACT_SIDE",
    )

    # Save v9 baseline (same as GUI will return)
    (ROOT / "data" / "AVY_FACT_SIDE__INSERT_v9.sql").write_text(v9.insert_sql, encoding="utf-8")

    # Re-build ancillary inputs for the per-column classification
    model = OdiXmlParser().parse_bytes(xml_bytes)
    ms = parse_all_sheets(drd_bytes)
    all_etl_text = "\n".join(
        r.description for r in ms.extracted_rules if r.role == SheetRole.ETL_NOTES
    )
    aug = v9.augmented_drd_rows
    tdef = load_target_table_definition(3, "IKOROSTELEV", "AVY_FACT_SIDE")

    print("Comparing DRD vs ODI ...")
    from app.sql_model.comparator import compare_drd_rows_to_model
    cmp_results = compare_drd_rows_to_model(aug, model)

    print("Using v9 INSERT from shared pipeline ...")
    gen_proj_map = parse_gen_projections(v9.insert_sql)

    # Validation already done inside generate_v9 -- echo it.
    val_dict = v9.oracle_validation
    print(f"  v9 INSERT valid: {val_dict.get('is_valid')} stmts={val_dict.get('statements_checked')}")
    # Synthesize a tiny object compatible with the rest of the report code
    class _Val:
        is_valid = val_dict.get("is_valid")
        statements_checked = val_dict.get("statements_checked", 0)
    val = _Val()
    class _Gen:
        sql = v9.insert_sql
        column_count = v9.column_count
        join_count = v9.join_count
        provenance_summary = v9.provenance
    gen = _Gen()

    # Build per-column 3-way rows
    print(f"Classifying {len(tdef.get('columns', []))} target columns ...")
    by_col_drd_row = {r.get("column", "").upper(): r for r in aug}
    by_col_cmp = {r.target_col.upper(): r for r in cmp_results}
    rows_out: List[Dict[str, Any]] = []
    for col_def in tdef.get("columns", []) or []:
        target = (col_def.get("name") or "").strip().upper()
        if not target:
            continue
        drd_row = by_col_drd_row.get(target, {})
        odi_chain = odi_chain_for_column(model, target)
        gen_proj = gen_proj_map.get(target)
        rows_out.append(classify_row(
            target=target,
            drd_row=drd_row,
            odi_chain=odi_chain,
            gen_proj=gen_proj,
            all_etl_text=all_etl_text,
            comp_result=by_col_cmp.get(target),
        ))

    # Summaries
    summary = {
        "total_columns": len(rows_out),
        "verdict_counts": dict(Counter(r["verdict"] for r in rows_out)),
        "drd_intent_counts": dict(Counter(r["drd_intent"] for r in rows_out)),
        "drd_odi_agree": dict(Counter(r["drd_odi_agree"] for r in rows_out)),
        "drd_gen_agree": dict(Counter(r["drd_gen_agree"] for r in rows_out)),
        "generated_insert": {
            "valid_oracle": val.is_valid,
            "cols": gen.column_count,
            "joins": gen.join_count,
            "provenance": gen.provenance_summary,
        },
    }

    out_json = ROOT / "data" / "THREE_WAY_COMPARISON.json"
    out_md = ROOT / "data" / "THREE_WAY_COMPARISON.md"
    out_json.write_text(
        json.dumps({"summary": summary, "rows": rows_out}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {out_json}")

    # Markdown table
    md = ["# 3-way comparison: DRD vs ODI vs Generated v9", ""]
    md.append("## Summary")
    md.append("")
    md.append(f"- Total columns: **{summary['total_columns']}**")
    md.append(f"- Generated INSERT valid (sqlglot oracle): **{val.is_valid}**")
    md.append(f"- Generated joins: **{gen.join_count}**, "
              f"cols: **{gen.column_count}**")
    md.append("")
    md.append("### Verdict counts")
    for k, v in sorted(summary["verdict_counts"].items(), key=lambda x: -x[1]):
        md.append(f"- `{k}`: **{v}**")
    md.append("")
    md.append("### DRD intent counts (shared rule engine)")
    for k, v in sorted(summary["drd_intent_counts"].items(), key=lambda x: -x[1]):
        md.append(f"- `{k}`: **{v}**")
    md.append("")
    md.append("### DRD <-> ODI agreement")
    for k, v in sorted(summary["drd_odi_agree"].items(), key=lambda x: -x[1]):
        md.append(f"- `{k}`: **{v}**")
    md.append("")
    md.append("### DRD <-> GEN v9 agreement")
    for k, v in sorted(summary["drd_gen_agree"].items(), key=lambda x: -x[1]):
        md.append(f"- `{k}`: **{v}**")
    md.append("")

    md.append("## Per-column matrix (sample of interesting verdicts)")
    md.append("")
    md.append("| TARGET | INTENT | VERDICT | DRD/ODI | DRD/GEN | DRD source | ODI resolved | GEN v9 |")
    md.append("|---|---|---|---|---|---|---|---|")
    # Show every column where verdict != ALL_AGREE and DRD_EMPTY first; cap at 60 rows.
    interesting = [r for r in rows_out if r["verdict"] not in ("ALL_AGREE", "DRD_EMPTY")]
    interesting.sort(key=lambda r: (r["verdict"], r["target"]))
    for r in interesting[:60]:
        md.append(
            f"| {r['target']} | {r['drd_intent']} | {r['verdict']} | "
            f"{r['drd_odi_agree']} | {r['drd_gen_agree']} | "
            f"`{r['drd_source']}` | "
            f"`{r.get('odi_resolved_source', '') or r['odi_top_expr'][:60]}` | "
            f"`{r['gen_source_expr'][:60]}` |"
        )

    md.append("")
    md.append("## Full per-column data: see `data/THREE_WAY_COMPARISON.json`")
    out_md.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {out_md}")

    print()
    print("Headline counts:")
    for k, v in sorted(summary["verdict_counts"].items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
