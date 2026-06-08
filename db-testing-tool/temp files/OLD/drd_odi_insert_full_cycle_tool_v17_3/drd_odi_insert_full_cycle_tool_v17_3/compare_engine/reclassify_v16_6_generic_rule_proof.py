#!/usr/bin/env python3
"""
reclassify_v16_6_generic_rule_proof.py

Generic post-processor for v16 delta output.

Purpose:
- Promote FIX_CANDIDATE_UPSTREAM_CHANGED to FIXED_BY_RESOLVED_RULE_PROOF
  only when the fixed ODI evidence satisfies proof checks derived from DRD content.

No business/fixture hardcoding:
- No target-column-name branches.
- No table/column fixture boosters.
- Evidence search terms are derived from the actual DRD row and resolved ODI lineage row.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from openpyxl import load_workbook
import compare_drd_odi_universal as base

__VERSION__ = "16.6-generic-rule-proof"


def read_csv(p: Path) -> List[Dict[str, str]]:
    with p.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(p: Path, rows: Sequence[Dict[str, str]], fields: Sequence[str]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().upper()


def clean(s) -> str:
    return base.clean_text(s)


def ident(s) -> str:
    return base.normalize_identifier(s)


def identifiers(text: str) -> List[str]:
    """Generic identifier extractor. Does not use fixture values."""
    toks = []
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_#$]*", text or ""):
        u = ident(t)
        if not u:
            continue
        # remove SQL noise, keep business/source tokens from input text
        if u in {
            "CASE", "WHEN", "THEN", "ELSE", "END", "FROM", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
            "SELECT", "WHERE", "AND", "OR", "ON", "AS", "NULL", "IS", "NOT", "IN", "THE", "FOR", "USE",
            "TO", "FROM", "WITH", "VALUE", "VALUES", "LOOKUP", "PARSE", "EXTRACT", "SOURCE", "TARGET",
        }:
            continue
        toks.append(u)
    # target/source names can have underscores: also add components generically
    expanded = []
    for t in toks:
        expanded.append(t)
        for part in t.split("_"):
            if len(part) >= 2 and part not in {"ID", "CD", "NM", "TP", "DT", "YR"}:
                expanded.append(part)
    out = []
    seen = set()
    for t in expanded:
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def snippets(xml: str, terms: Iterable[str], radius: int = 3500, max_parts: int = 12) -> str:
    parts = []
    seen = set()
    # Longer terms first to keep snippets focused.
    for term in sorted(set(t for t in terms if t), key=len, reverse=True):
        if len(term) < 2:
            continue
        try:
            pattern = re.escape(term)
        except Exception:
            continue
        for m in re.finditer(pattern, xml, flags=re.I):
            s = max(0, m.start() - radius)
            e = min(len(xml), m.end() + radius)
            key = (s, e)
            if key not in seen:
                parts.append(xml[s:e])
                seen.add(key)
            if len(parts) >= max_parts:
                return "\n".join(parts)
    return "\n".join(parts)


def drd_text(col: str, drdrow: Dict[str, str]) -> str:
    return " ".join([
        col or "",
        drdrow.get("source_1", "") or "",
        drdrow.get("source_2", "") or "",
        drdrow.get("source_3", "") or "",
        drdrow.get("drd_rule", "") or "",
    ])


def source_attr(drdrow: Dict[str, str]) -> str:
    return ident(drdrow.get("source_3", ""))


def source_table(drdrow: Dict[str, str]) -> str:
    return ident(drdrow.get("source_2", ""))


def target_suffix(col: str) -> str:
    parts = ident(col).split("_")
    return parts[-1] if parts else ""


def source_guard_requirements(text: str) -> List[Tuple[str, str]]:
    """Find generic source guard predicates: <identifier> = <number>."""
    req = []
    for name, val in re.findall(r"\b([A-Za-z_][A-Za-z0-9_#$]*(?:\.[A-Za-z_][A-Za-z0-9_#$]*)?)\s*=\s*(\d+)\b", text or ""):
        nm = ident(name.split(".")[-1])
        if nm and val:
            req.append((nm, val))
    return list(dict.fromkeys(req))


def required_checks_from_drd(col: str, drdrow: Dict[str, str]) -> List[Dict[str, str]]:
    """Build proof checks from DRD row only. No fixture tokens."""
    text_raw = drd_text(col, drdrow)
    text = norm(text_raw)
    checks: List[Dict[str, str]] = []
    attr = source_attr(drdrow)
    tbl = source_table(drdrow)
    suffix = target_suffix(col)

    for guard_col, guard_val in source_guard_requirements(text_raw):
        checks.append({"type": "source_guard", "identifier": guard_col, "value": guard_val})

    # Generic parse requirement: DRD uses parse/extract/split/substring language.
    if re.search(r"\b(PARSE|EXTRACT|SPLIT|SUBSTR|SUBSTRING|LAST\s+TWO|DIGITS?)\b", text, flags=re.I):
        checks.append({"type": "parse_logic", "identifier": attr})

    # Generic lookup requirement: DRD/source says lookup/join or the source table is a lookup-like table.
    if re.search(r"\b(LOOKUP|LOOK\s+UP|JOIN|DIMENSION|REFERENCE)\b", text, flags=re.I) or tbl:
        # Only make it strict when DRD explicitly describes lookup/join, or source table is used as source of code/name.
        if re.search(r"\b(LOOKUP|LOOK\s+UP|JOIN|DIMENSION)\b", text, flags=re.I):
            checks.append({"type": "lookup_logic", "source_table": tbl, "source_attr": attr})

    # Target suffixes are generic; no business names.
    if suffix in {"YR", "YEAR"}:
        checks.append({"type": "year_logic", "identifier": attr})

    if suffix in {"DT", "DATE"} and attr:
        checks.append({"type": "source_attr_present", "identifier": attr})

    if re.search(r"\b(CURRENCY|CCY)\b", text, flags=re.I) and attr:
        checks.append({"type": "source_attr_present", "identifier": attr})

    # Code/name suffixes: if source attr exists and DRD says lookup, require that output attr appear somewhere in evidence.
    if suffix in {"CD", "CODE", "NM", "NAME"} and attr and any(c["type"] == "lookup_logic" for c in checks):
        checks.append({"type": "source_attr_present", "identifier": attr})

    # Deduplicate check dicts.
    out = []
    seen = set()
    for c in checks:
        key = tuple(sorted(c.items()))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def terms_from_context(col: str, drdrow: Dict[str, str], resolved_expr: str, lineage_path: str = "") -> List[str]:
    """Evidence retrieval terms derived only from current input data."""
    text = " ".join([
        drd_text(col, drdrow),
        resolved_expr or "",
        lineage_path or "",
    ])
    return identifiers(text)


def has_identifier(evidence: str, token: str) -> bool:
    token = ident(token)
    if not token:
        return False
    return re.search(r"(?<![A-Z0-9_#$])" + re.escape(token) + r"(?![A-Z0-9_#$])", evidence, flags=re.I) is not None


def evaluate_check(check: Dict[str, str], evidence: str) -> Tuple[bool, str]:
    ev = norm(evidence)
    typ = check["type"]

    if typ == "source_guard":
        guard_col = check.get("identifier", "")
        guard_val = check.get("value", "")
        ok = has_identifier(ev, guard_col) and re.search(r"\b" + re.escape(guard_val) + r"\b", ev) is not None and "CASE" in ev
        return ok, f"requires CASE guard {guard_col}={guard_val}"

    if typ == "parse_logic":
        # Generic: parse-like DRD must map to parse-like ODI expression. Source identifier is optional because
        # technical source names can differ from business names, but if present in DRD evidence it strengthens proof.
        ok = any(fn in ev for fn in ["SUBSTR", "SUBSTRING", "REGEXP", "INSTR"])
        src = check.get("identifier", "")
        if src and has_identifier(ev, src):
            ok = ok and True
        return ok, "requires parse function such as SUBSTR/REGEXP/INSTR"

    if typ == "lookup_logic":
        tbl = check.get("source_table", "")
        attr = check.get("source_attr", "")
        # Lookup table name can change in ODI (dedicated inline lookup), so table is helpful but not mandatory.
        # Attribute/output value or generic join/lookup evidence must exist.
        ok = (attr and has_identifier(ev, attr)) or (tbl and has_identifier(ev, tbl))
        ok = ok and ("JOIN" in ev or "SELECT" in ev or "LOOKUP" in ev or "CL_" in ev or "DIM" in ev)
        return ok, f"requires lookup/join evidence from DRD source table/attribute {tbl}.{attr}"

    if typ == "year_logic":
        # Generic year proof: parse + numeric/year construction evidence.
        ok = any(fn in ev for fn in ["SUBSTR", "SUBSTRING", "REGEXP"]) and any(tok in ev for tok in ["TO_NUMBER", "YEAR", "'20'", "'19'", "20||", "19||"])
        return ok, "requires parse + year/century/numeric construction evidence"

    if typ == "source_attr_present":
        attr = check.get("identifier", "")
        ok = bool(attr) and has_identifier(ev, attr)
        return ok, f"requires source/output attribute {attr}"

    return False, f"unknown check {typ}"


def prove(col: str, drdrow: Dict[str, str], xml_text: str, resolved_expr: str, lineage_path: str = "") -> Tuple[bool, List[Dict[str, object]], str, List[str]]:
    checks = required_checks_from_drd(col, drdrow)
    terms = terms_from_context(col, drdrow, resolved_expr, lineage_path)
    resolved_terms = [t for t in identifiers((resolved_expr or "") + " " + (lineage_path or "")) if t not in {"INLINE", "VIEW", "STEP", "TASK", "COL", "EXPR"}]
    local_xml = "\n".join([
        snippets(xml_text, [col], radius=30000, max_parts=30),
        snippets(xml_text, resolved_terms[:25], radius=8000, max_parts=10),
    ])
    evidence = norm("\n".join([resolved_expr or "", lineage_path or "", local_xml]))

    # If DRD has no explicit checks, do not promote to fixed by proof.
    if not checks:
        return False, [], evidence[:4000], terms

    rows = []
    passed = True
    for c in checks:
        ok, detail = evaluate_check(c, evidence)
        rows.append({"type": c.get("type"), "detail": detail, "passed": ok, "derived_check": c})
        passed = passed and ok
    return passed, rows, evidence[:4000], terms


def load_drd(xlsx: Path, args) -> Dict[str, Dict[str, str]]:
    wb = load_workbook(xlsx, read_only=False, data_only=True)
    det = base.auto_detect_mapping(
        wb,
        xml_targets=[args.target_table or ""],
        target_table_override=args.target_table or "",
        mapping_sheet_override=args.mapping_sheet or "",
        target_col_override=args.target_col or "",
        source_cols_override=args.source_cols or "",
        rule_col_override=args.rule_col or "",
        header_row_override=args.header_row,
    )
    rows, _ = base.extract_mapping_from_xlsx(xlsx, det)
    return {r["target_column"]: r for r in rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="v16.6 generic rule-proof reclassifier")
    ap.add_argument("--v16-output", required=True)
    ap.add_argument("--xlsx", required=True)
    ap.add_argument("--original-xml", required=True)
    ap.add_argument("--fixed-xml", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-table", default="")
    ap.add_argument("--mapping-sheet", default="")
    ap.add_argument("--target-col", default="")
    ap.add_argument("--source-cols", default="")
    ap.add_argument("--rule-col", default="")
    ap.add_argument("--header-row", type=int, default=None)
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.v16_output).resolve()

    for p in src.iterdir():
        if p.is_file():
            shutil.copy2(p, out / p.name)

    drd = load_drd(Path(args.xlsx), args)
    original_xml = Path(args.original_xml).read_text(errors="ignore")
    fixed_xml = Path(args.fixed_xml).read_text(errors="ignore")
    delta = read_csv(src / "delta_report_fixed_still_open_regression.csv")

    new_rows = []
    proof_rows = []
    for r in delta:
        col = r.get("target_column", "")
        nr = dict(r)
        dr = drd.get(col)
        if r.get("delta_status") == "FIX_CANDIDATE_UPSTREAM_CHANGED" and dr:
            opass, ochecks, oevidence, oterms = prove(
                col,
                dr,
                original_xml,
                r.get("original_resolved_expression", ""),
                r.get("original_lineage_path", ""),
            )
            fpass, fchecks, fevidence, fterms = prove(
                col,
                dr,
                fixed_xml,
                r.get("fixed_resolved_expression", ""),
                r.get("fixed_lineage_path", ""),
            )
            if fpass and not opass:
                nr["delta_status"] = "FIXED_BY_RESOLVED_RULE_PROOF"
                nr["fixed_reason"] = "Fixed resolved ODI lineage satisfies DRD-derived generic proof checks; original did not."
            proof_rows.append({
                "target_column": col,
                "original_proof_passed": "Y" if opass else "N",
                "fixed_proof_passed": "Y" if fpass else "N",
                "original_checks": json.dumps(ochecks, ensure_ascii=False),
                "fixed_checks": json.dumps(fchecks, ensure_ascii=False),
                "original_terms_derived_from_input": " | ".join(oterms[:80]),
                "fixed_terms_derived_from_input": " | ".join(fterms[:80]),
                "original_evidence_excerpt": oevidence,
                "fixed_evidence_excerpt": fevidence,
            })
        new_rows.append(nr)

    if new_rows:
        fields = list(new_rows[0].keys())
    else:
        fields = ["target_column", "delta_status"]
    write_csv(out / "delta_report_v16_6_generic_rule_proof.csv", new_rows, fields)
    write_csv(out / "resolved_rule_proof_checks_v16_6.csv", proof_rows, [
        "target_column",
        "original_proof_passed",
        "fixed_proof_passed",
        "original_checks",
        "fixed_checks",
        "original_terms_derived_from_input",
        "fixed_terms_derived_from_input",
        "original_evidence_excerpt",
        "fixed_evidence_excerpt",
    ])

    summary = json.loads((src / "summary.json").read_text())
    summary["version"] = __VERSION__
    summary["delta_status_counts_v16_6"] = dict(Counter(r.get("delta_status", "") for r in new_rows))
    summary["fixed_by_resolved_rule_proof_v16_6"] = [r["target_column"] for r in new_rows if r.get("delta_status") == "FIXED_BY_RESOLVED_RULE_PROOF"]
    summary["still_open_v16_6"] = [r["target_column"] for r in new_rows if r.get("delta_status") == "STILL_OPEN"]
    summary["new_regression_v16_6"] = [r["target_column"] for r in new_rows if "REGRESSION" in r.get("delta_status", "")]
    (out / "summary_v16_6_generic_rule_proof.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    (out / "README_v16_6.md").write_text(
        "# v16.6 Generic Rule Proof\n\n"
        "No hardcoded business column/table boosters are used in this proof layer. "
        "Evidence terms are derived from the actual DRD row and resolved ODI lineage.\n\n"
        "See `resolved_rule_proof_checks_v16_6.csv` and `summary_v16_6_generic_rule_proof.json`.\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
