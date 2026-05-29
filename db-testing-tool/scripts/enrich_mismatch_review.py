"""Enrich the operator-annotated REAL_MISMATCH CSV with:

  * full untruncated DRD rule text (operator: "the DRD rule is cutting")
  * human_explanation column (plain-language description of the problem)
  * recommendation column (semantic match / real drift / need clarification)
  * for CASH_* / SEC_* rows: the actual APA_CASH / APA_SECURITY filter from
    STEP1 (operator: "what is in APA_CASH agregation clouse ???")

Preserves the operator's Review decision column verbatim.  Read-only --
takes the annotated CSV as input + the live ODI XML / DRD xlsx, emits a
new CSV next to the input.
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


# ── APA_CASH / APA_SECURITY filter extraction (one-shot per run) ─────────────

def _extract_apa_filters(model) -> Tuple[str, str]:
    """Return (apa_cash_filter, apa_security_filter) -- the actual STAGING
    REGEXP_LIKE predicates that filter APA records into the cash / security
    alias bindings.  Walks every staging step + MERGE; whichever step has
    the predicate wins."""
    cash, sec = "", ""
    cash_re = re.compile(
        r"REGEXP_LIKE\s*\(\s*APA_CASH\.APA_TP_CD\s*(?:\(\s*\+\s*\))?\s*,"
        r"\s*'([^']+)'",
        re.IGNORECASE,
    )
    sec_re = re.compile(
        r"REGEXP_LIKE\s*\(\s*APA_SECURITY\.APA_TP_CD\s*(?:\(\s*\+\s*\))?\s*,"
        r"\s*'([^']+)'",
        re.IGNORECASE,
    )
    blobs = [s.select_sql or "" for s in model.staging_steps]
    blobs.append(model.final_select_sql or "")
    for sql in blobs:
        if not cash:
            m = cash_re.search(sql)
            if m:
                cash = f"REGEXP_LIKE(APA_CASH.APA_TP_CD, '{m.group(1)}')"
        if not sec:
            m = sec_re.search(sql)
            if m:
                sec = f"REGEXP_LIKE(APA_SECURITY.APA_TP_CD, '{m.group(1)}')"
        if cash and sec:
            break
    return cash, sec


# ── Full DRD rule lookup ──────────────────────────────────────────────────────

def _build_drd_full_rule_map(augmented_rows) -> Dict[str, str]:
    """Return {target_col_upper: full_transformation_text_unstripped}."""
    out: Dict[str, str] = {}
    for r in augmented_rows or []:
        col = (r.get("column") or "").upper()
        if not col:
            continue
        trans = (r.get("transformation") or "").strip()
        etl = (r.get("etl_block_body") or "").strip()
        if trans and etl:
            full = trans + "\n\n[ETL Notes block]:\n" + etl
        else:
            full = trans or etl or ""
        out[col] = full
    return out


# ── Explanation generator ────────────────────────────────────────────────────

# Matches "STEPn_LABEL[kind]: expr" segments in the compact chain text.
_CHAIN_SEGMENT_RE = re.compile(
    r"\*?\s*(STEP\d+|MERGE(?:_USING)?)\s*\[(\w+)\]:\s*(.+?)(?=\s*->|\Z)",
    re.DOTALL,
)
# Matches alias.col references inside an expression (case-insensitive).
_COL_REF_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_$#]*)\.([A-Za-z][A-Za-z0-9_$#]*)\b",
)
_SQL_KEYWORD_PREFIXES = frozenset({
    "CASE", "WHEN", "THEN", "ELSE", "END", "SUM", "COUNT", "MAX", "MIN",
    "AVG", "NVL", "COALESCE", "DECODE", "TO_DATE", "TO_NUMBER", "TO_CHAR",
    "TRIM", "SUBSTR", "REGEXP_LIKE", "REGEXP_REPLACE", "CAST", "EXTRACT",
    "EXISTS", "AND", "OR", "NOT", "NULL", "IS", "FROM", "WHERE", "ON",
    "BETWEEN", "IN", "LIKE", "LISTAGG", "ROW_NUMBER",
})


def _find_source_attribute_match(
    chain_text: str, drd_attr_up: str,
) -> Tuple[str, str, str]:
    """Return (step_label, expr_kind, matching_ref) if any chain entry's
    expression references ``drd_attr_up`` as a bare column name.  Empty
    tuple (``"", "", ""``) when no match.
    """
    if not chain_text or not drd_attr_up:
        return "", "", ""
    drd_clean = drd_attr_up.strip().upper().lstrip(".")
    for seg in _CHAIN_SEGMENT_RE.finditer(chain_text):
        step_label, kind, expr = seg.group(1), seg.group(2), seg.group(3).strip()
        for m in _COL_REF_RE.finditer(expr):
            alias = m.group(1).upper()
            col = m.group(2).upper()
            if alias in _SQL_KEYWORD_PREFIXES:
                continue
            # Direct equality OR role-prefix-stripped equality (e.g.
            # OFST_AR_DIM_ID == AR_DIM_ID after stripping OFST_)
            if col == drd_clean:
                return step_label, kind, f"{alias}.{col}"
            if col.endswith("_" + drd_clean):
                return step_label, kind, f"{alias}.{col}"
            if drd_clean.endswith("_" + col):
                return step_label, kind, f"{alias}.{col}"
    return "", "", ""


def _classify_and_explain(
    row: dict,
    drd_full: str,
    apa_cash_filter: str,
    apa_security_filter: str,
    source_attr_index: Optional[Dict[str, list]] = None,
) -> Tuple[str, str]:
    """Return (human_explanation, recommendation) for one row.

    Pattern-based; uses target column name conventions + drd_source +
    odi_staging_chain text to classify.  Generic -- no hardcoded
    business-domain identifiers beyond the documented ODI patterns
    APACSH/APASEC/CL_VAL/PDM-name-drift.
    """
    tgt = (row.get("target_col") or "").upper()
    kind = (row.get("mismatch_kind") or "").upper()
    drd_src = (row.get("drd_source") or "")
    chain = (row.get("odi_staging_chain") or "")
    rule_lower = drd_full.lower()
    chain_upper = chain.upper()

    # Operator-locked (2026-05-29): source-attribute-first matching.
    # Extract the DRD's stated source attribute (the bare column name on the
    # source side) and scan the ODI chain text for an entry that references
    # the same column.  If ANY entry does, this is a SEMANTIC_MATCH on the
    # source attribute -- the target column name divergence is just naming.
    drd_attr_up = ""
    if drd_src:
        drd_attr_up = drd_src.strip().split(".")[-1].upper()
    src_match_step, src_match_kind, src_match_ref = _find_source_attribute_match(
        chain, drd_attr_up,
    )
    if src_match_step:
        return (
            f"SOURCE_ATTRIBUTE_MATCH: DRD's source attribute "
            f"'{drd_attr_up}' is projected by ODI step {src_match_step} "
            f"as '{src_match_ref}' (expression kind: {src_match_kind}).  "
            f"The target column name differs but the source data column "
            f"is the same.  Semantic match.",
            f"SEMANTIC_MATCH -- ODI step {src_match_step} projects "
            f"{src_match_ref} which IS the DRD's source attribute",
        )

    # CROSS-COLUMN source-attribute lookup moved AFTER the more specific
    # patterns (APA_CASH/APA_SECURITY/PDM name-drift).  See pattern N
    # below near the end of the function.

    # ── Pattern 1: known PDM abbreviation drift ──────────────────────────
    # Operator already confirmed YLD/YTW/YTW_CD/TM_PRC_DISCRETION_F are
    # spec-vs-physical name choices, not real bugs.
    name_drift_pairs = [
        ("YIELD_TO_WORST", "YTW"), ("YIELD", "YLD"),
        ("DISCRETION", "DSCTN"),
    ]
    drd_attr_up = drd_src.split(".")[-1].upper() if drd_src else ""
    for spec_name, phys_name in name_drift_pairs:
        if spec_name in drd_attr_up and (phys_name in tgt or phys_name in chain_upper):
            return (
                f"PDM name-drift: DRD spec uses friendly column name "
                f"'{spec_name}' but the physical Oracle column is "
                f"'{phys_name}'.  Same data; spec just hasn't been updated "
                f"to match the abbreviated physical name.  Cross-check with "
                f"PDM to confirm the physical name.",
                "SEMANTIC_MATCH -- update DRD spec to physical name or "
                "accept the abbreviation",
            )

    # ── Pattern 2: CASH_* columns sourced from APA_CASH ──────────────────
    # ODI filters APA records to APACSH-prefixed rows in STEP1 via the
    # REGEXP_LIKE predicate.  DRD source CCAL_REPL_OWNER.APA.X means
    # "from APA, after applying the APACSH filter documented in ETL Notes".
    if tgt.startswith(("CASH_", "CSH_")) and "APA_CASH" in chain_upper:
        filter_clause = apa_cash_filter or "REGEXP_LIKE(APA_TP_CD, '^APACSH...')"
        cl_val_note = ""
        if "CL_VAL" in drd_src.upper():
            cl_val_note = (
                "  DRD requests a JOIN to CL_VAL lookup table; ODI "
                "denormalises the code/name directly into the APA_CASH "
                "alias so no separate lookup is needed -- same value, "
                "different SQL shape."
            )
        return (
            f"APA_CASH aggregation: ODI filters the APA table to APACSH-"
            f"prefixed records in STEP1 with the predicate "
            f"`{filter_clause}` and projects them through the "
            f"APA_CASH alias.  Plus the documented APACSH04/APACSH11 "
            f"dual-record resolution when both codes exist for the same "
            f"transaction.  The DRD's 'Use APACSH logic from ETL Notes' "
            f"maps exactly to this STEP1 aggregation.{cl_val_note}",
            "SEMANTIC_MATCH -- ODI implements the APACSH selection via "
            "STEP1 alias-binding + filter",
        )

    # ── Pattern 3: SEC_*/SCR_* columns sourced from APA_SECURITY ─────────
    if tgt.startswith(("SEC_", "SCR_")) and "APA_SECURITY" in chain_upper:
        filter_clause = apa_security_filter or "REGEXP_LIKE(APA_TP_CD, '^APASEC...')"
        cl_val_note = ""
        if "CL_VAL" in drd_src.upper():
            cl_val_note = (
                "  DRD requests a JOIN to CL_VAL lookup table; ODI "
                "denormalises the code/name directly into the APA_SECURITY "
                "alias so no separate lookup is needed -- same value, "
                "different SQL shape."
            )
        return (
            f"APA_SECURITY aggregation: ODI filters the APA table to "
            f"APASEC-prefixed records in STEP1 with the predicate "
            f"`{filter_clause}` and projects them through the "
            f"APA_SECURITY alias.  Plus the APASEC81/83/91/93/94 sub-code "
            f"projections for specific measures.  The DRD's 'Use APASEC "
            f"logic from ETL Notes' maps exactly to this STEP1 "
            f"aggregation.{cl_val_note}",
            "SEMANTIC_MATCH -- ODI implements the APASEC selection via "
            "STEP1 alias-binding + filter",
        )

    # ── Pattern 4: WHERE/CASE filter -- DRD says WHERE, ODI does CASE WHEN ─
    if "CASE WHEN" in chain_upper and (
        "where" in rule_lower or "if " in rule_lower or "for " in rule_lower
    ):
        return (
            "DRD specifies a row-level filter (`WHERE x = ...` or `IF x = "
            "... THEN`); ODI implements it as a `CASE WHEN ... THEN col "
            "ELSE NULL END` so the staging table retains every TXN row "
            "and the filter is applied at projection time.  Semantically "
            "equivalent.",
            "SEMANTIC_MATCH -- ODI's CASE WHEN realises the DRD WHERE/IF",
        )

    # ── Pattern 5: multi-branch IF/ELSE conditional in DRD ───────────────
    if "if " in rule_lower and "else" in rule_lower and "CASE" in chain_upper:
        return (
            "DRD describes a multi-branch IF/THEN/ELSE conditional; ODI "
            "implements it as a CASE WHEN with the same branches.  "
            "Verify each branch's predicate + projection matches the "
            "DRD's intent column-by-column.",
            "LIKELY_MATCH -- verify each CASE branch lines up with DRD's "
            "IF/ELSE",
        )

    # ── Pattern 6: DRD requires a JOIN ODI's parsed graph lacks ──────────
    if kind == "JOIN_DRIFT":
        return (
            "JOIN_DRIFT: the DRD describes a JOIN to a lookup/dimension "
            "table whose ON predicate doesn't match anything in ODI's "
            "parsed staging join graph.  Either: (a) the ODI parser "
            "missed a JOIN that DOES exist in the XML (parser gap), or "
            "(b) the JOIN was never implemented (real ODI gap).  Inspect "
            "the full XML around the column's STEP for the actual JOIN.",
            "OPERATOR_REVIEW -- inspect the column's STEP in ODI XML to "
            "distinguish parser gap from real gap",
        )

    # ── Pattern 7: APPLICABLE_FILTER_DRIFT ──────────────────────────────
    if kind == "APPLICABLE_FILTER_DRIFT":
        return (
            "DRD says 'Applicable only for <CODE>'; ODI projects an "
            "unfiltered expression without the CODE filter.  Genuine "
            "drift -- ODI returns values for rows that DRD excludes.",
            "REAL_DRIFT -- ODI needs the CASE WHEN <discrim> = '<CODE>' "
            "wrapper added",
        )

    # ── Pattern 8: COLUMN_MISMATCH (same table, different col) ──────────
    if kind == "COLUMN_MISMATCH":
        return (
            "COLUMN_MISMATCH: same source table on both sides but "
            "different column.  Could be a typo in the DRD spec, an "
            "abbreviation drift, or ODI legitimately projecting a "
            "different column.  Needs operator clarification.",
            "OPERATOR_REVIEW -- confirm the intended physical column",
        )

    # ── Pattern 9: real ODI gap -- no STEPn computes a real expression ───
    # A real gap is when every STEPn entry is passthrough AND the only
    # non-passthrough entries are MERGE's trivial ``S.<col>`` reference
    # (which is just the WHEN NOT MATCHED INSERT VALUES alias).  In that
    # case ODI never derives the column anywhere -- it propagates NULL
    # from the bottom of the staging chain.
    real_gap = True
    for seg in _CHAIN_SEGMENT_RE.finditer(chain):
        step_label, seg_kind, expr = seg.group(1), seg.group(2), seg.group(3).strip()
        if seg_kind == "passthrough":
            continue
        if step_label == "MERGE" and seg_kind == "column_ref" \
                and expr.upper().startswith("S."):
            # trivial WHEN NOT MATCHED INSERT VALUES S.X mapping
            continue
        # Any other kind -> there IS a real derivation somewhere
        real_gap = False
        break
    if real_gap and chain.strip():
        # Before flagging as REAL_ODI_GAP, do the CROSS-COLUMN source check:
        # does the DRD source attribute appear under a DIFFERENT target
        # column elsewhere in ODI?  If yes, refine the explanation -- ODI
        # has the source data, just doesn't bind the role-specific table
        # for this target.  If no, plain REAL_ODI_GAP.
        siblings = []
        if drd_attr_up and source_attr_index:
            siblings = source_attr_index.get(drd_attr_up, [])
            if not siblings:
                # Prefix-modulo lookup (e.g. drd says AR_CGY_CD; index has
                # BKR_AR_CGY_CD)
                for key, vals in source_attr_index.items():
                    if key == drd_attr_up:
                        continue
                    if key.endswith("_" + drd_attr_up) or drd_attr_up.endswith("_" + key):
                        siblings = vals
                        break
            siblings = [s for s in siblings if s[0] != tgt]
        if siblings:
            sample = siblings[0]
            sib_target, sib_step, sib_alias = sample
            extras = ""
            if len(siblings) > 1:
                others = ", ".join(s[0] for s in siblings[1:4])
                extras = f"  Other targets projecting the same column: {others}."
            return (
                f"REAL ODI GAP (with source available): DRD's source "
                f"attribute '{drd_attr_up}' is not projected into THIS "
                f"target ({tgt}) anywhere in ODI -- but the SAME source "
                f"column IS projected by ODI at step {sib_step} into "
                f"sibling target '{sib_target}' from alias '{sib_alias}'."
                f"{extras}  ODI has the source data; the gap is that ODI "
                f"doesn't bind the role-specific table (e.g. BKR_AR_DIM "
                f"for broker, APA_SECURITY for security) to populate "
                f"this target.",
                f"REAL_ODI_GAP_WITH_SOURCE_AVAILABLE -- DRD source "
                f"'{drd_attr_up}' exists in ODI (used by '{sib_target}'); "
                f"ODI just doesn't join the role-specific table for '{tgt}'",
            )
        return (
            "ODI never derives this column anywhere in the chain: every "
            "STEPn entry is a pass-through reference and the MERGE just "
            "copies S.<col> from the USING subquery (which is itself "
            "reading the pass-through value).  In production the column "
            "will be NULL unless an out-of-scope UPDATE populates it.  "
            "Real ODI gap -- the DRD's stated source attribute "
            f"'{drd_attr_up}' is never projected at any step "
            "(also not under any sibling target).",
            "REAL_ODI_GAP -- column declared but never derived; DRD source "
            f"'{drd_attr_up}' is absent from the entire ODI chain",
        )

    # ── Pattern 10: full-rule-truncated columns (BKR_AR_ID etc.) ─────────
    if len(drd_full) > 400 and ("if " in rule_lower and "else" in rule_lower):
        return (
            "DRD rule is a long multi-branch conditional with several "
            "alternative JOIN paths.  ODI implements its own variant in "
            "STEP1 -- compare the full DRD rule (now un-truncated above) "
            "branch by branch against the ODI CASE WHEN.",
            "OPERATOR_REVIEW -- branch-by-branch comparison required",
        )

    # ── Default: generic TRANSFORMATION_DRIFT ────────────────────────────
    return (
        "DRD describes a derivation rule (transformation, lookup or "
        "conditional projection); ODI's authoritative step projects a "
        "different shape.  No obvious pattern match -- needs column-"
        "specific review against the ODI XML.",
        "OPERATOR_REVIEW",
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def _build_source_attr_index(model) -> Dict[str, list]:
    """Walk model.column_derivations across ALL target columns and build an
    index ``{source_col_upper: [(target_col, step_label, source_alias), ...]}``.

    This lets the enrichment script answer: "for DRD source attribute X, does
    ODI project X anywhere in the chain (regardless of which TARGET column
    that chain belongs to)?"  Many BKR_* / SEC_* / CASH_* target columns
    are sourced from the same physical column as a non-prefixed sibling.
    """
    idx: Dict[str, list] = {}
    derivs = getattr(model, "column_derivations", None) or {}
    for tgt, chain in derivs.items():
        for d in chain:
            if not d.source_col:
                continue
            if d.expr_kind not in ("column_ref", "function", "case_when",
                                   "agg", "subquery"):
                # pass-through / literal / unknown - skip
                continue
            key = d.source_col.upper()
            idx.setdefault(key, []).append(
                (tgt, d.step_label, d.source_alias or "")
            )
    return idx


def main(in_csv: pathlib.Path, out_csv: pathlib.Path) -> int:
    print(f"Loading {in_csv}")
    with open(in_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"  {len(rows)} rows (operator-annotated)")

    # ── Source of fresh walker output ───────────────────────────────────
    # The detail_mismatch_report.py rewrites data/MISMATCH_TABLE.csv each
    # run with the latest walker chain text.  Overlay it onto the operator's
    # annotated rows so we keep the Review-decision column but pick up the
    # newest chain data + classifier results.
    fresh_csv = ROOT / "data" / "MISMATCH_TABLE.csv"
    fresh_chain: Dict[str, str] = {}
    fresh_kind: Dict[str, str] = {}
    fresh_drd_rule: Dict[str, str] = {}
    if fresh_csv.exists():
        try:
            with open(fresh_csv, encoding="utf-8") as f:
                for fr in csv.DictReader(f):
                    fresh_chain[fr["target_col"]] = fr.get("odi_staging_chain", "")
                    fresh_kind[fr["target_col"]] = fr.get("mismatch_kind", "")
                    fresh_drd_rule[fr["target_col"]] = fr.get("drd_rule", "")
            print(f"  overlay from fresh {fresh_csv.name}: {len(fresh_chain)} rows")
        except Exception as e:  # noqa: BLE001
            print(f"  (could not overlay fresh CSV: {e})")

    print("Loading ODI XML + DRD xlsx ...")
    drd_path = ROOT / "DRD_Activity_Fact.xlsx"
    odi_path = ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
    result = generate_v9(
        drd_bytes=drd_path.read_bytes(),
        drd_filename=drd_path.name,
        odi_xml_bytes=odi_path.read_bytes(),
        target_schema="ALY_FACT_OWNER",
        target_table="AVY_FACT_SIDE",
    )
    model = OdiXmlParser().parse_bytes(odi_path.read_bytes())

    apa_cash_filter, apa_security_filter = _extract_apa_filters(model)
    print(f"  APA_CASH filter: {apa_cash_filter}")
    print(f"  APA_SECURITY filter: {apa_security_filter}")

    # Index every source column ODI actually projects, across all targets.
    # Lets the classifier detect cross-column source matches.
    source_attr_index = _build_source_attr_index(model)
    print(f"  source-attribute index: {len(source_attr_index)} distinct source columns")

    drd_full_map = _build_drd_full_rule_map(result.augmented_drd_rows)

    # Build the enriched rows.  Process the union of operator-annotated rows
    # AND fresh walker rows, indexed by target_col.  Annotated rows for
    # columns that the fresh walker now MATCHES are kept but flagged.
    annotated_by_col: Dict[str, dict] = {}
    for r in rows:
        annotated_by_col[(r.get("target_col") or "").upper()] = r
    all_targets = set(annotated_by_col.keys()) | set(fresh_chain.keys())
    out_rows = []
    for tgt in sorted(all_targets):
        annotated = annotated_by_col.get(tgt)
        chain = fresh_chain.get(tgt, "")
        mismatch_kind = fresh_kind.get(tgt) or (
            annotated.get("mismatch_kind", "") if annotated else ""
        )
        # Use fresh chain text if available; falls back to operator's CSV.
        chain_for_classify = chain or (
            annotated.get("odi_staging_chain", "") if annotated else ""
        )
        # Synthesize a row dict for the classifier
        classify_row = {
            "target_col": tgt,
            "mismatch_kind": mismatch_kind,
            "drd_source": annotated.get("drd_source", "") if annotated else "",
            "odi_staging_chain": chain_for_classify,
        }
        # Lookup full DRD rule
        drd_full = drd_full_map.get(tgt, "")
        if not drd_full and annotated:
            drd_full = annotated.get("drd_rule", "")
        explanation, recommendation = _classify_and_explain(
            classify_row, drd_full, apa_cash_filter, apa_security_filter,
            source_attr_index=source_attr_index,
        )
        # If the column was annotated but is no longer in fresh mismatches,
        # flag it as "now matched" so operator sees the change.
        was_mismatch_now_matched = bool(annotated) and tgt not in fresh_chain
        out_rows.append({
            "target_col": tgt if annotated else (fresh_chain.get(tgt) and tgt),
            "mismatch_kind": mismatch_kind,
            "drd_source": classify_row["drd_source"],
            "drd_rule_full": drd_full,
            "odi_staging_chain": chain_for_classify,
            "odi_join_predicates": annotated.get("odi_join_predicates", "") if annotated else "",
            "Review decision": annotated.get("Review decision", "") if annotated else "",
            "now_matched": "YES" if was_mismatch_now_matched else "",
            "human_explanation": explanation,
            "recommendation": recommendation,
        })

    # Write
    field_order = [
        "target_col", "mismatch_kind", "drd_source", "drd_rule_full",
        "odi_staging_chain", "odi_join_predicates", "Review decision",
        "now_matched", "human_explanation", "recommendation",
    ]
    print(f"Writing {out_csv}")
    try:
        with open(out_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=field_order, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(out_rows)
        target = out_csv
    except PermissionError:
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        target = out_csv.with_name(out_csv.stem + f"_{ts}.csv")
        with open(target, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=field_order, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(out_rows)
        print(f"  (primary path locked; wrote to {target})")
    print(f"  {len(out_rows)} rows written + 2 new columns "
          f"(human_explanation, recommendation)")

    # Sanity tally
    from collections import Counter
    rec = Counter(r["recommendation"].split(" --")[0] for r in out_rows)
    print("\nRecommendation tally (head):")
    for k, v in sorted(rec.items(), key=lambda x: -x[1]):
        print(f"  {k:25s} {v}")
    return 0


if __name__ == "__main__":
    in_csv = pathlib.Path(
        sys.argv[1] if len(sys.argv) > 1
        else ROOT / "data" / "MISMATCH_TABLE_20260529T180420.csv"
    )
    out_csv = in_csv.with_name(in_csv.stem + "_enriched.csv")
    sys.exit(main(in_csv, out_csv))
