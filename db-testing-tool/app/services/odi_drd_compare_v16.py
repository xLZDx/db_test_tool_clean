#!/usr/bin/env python3
"""
odi_drd_compare_v16.py -- in-memory port of the v16.6 generic rule-proof comparator.

This is the app-service port of the standalone tool
``odi_drd_compare_tool_v16_6_generic_rule_proof`` (base v16.4 delta-safe engine
+ generic, no-hardcode rule-proof overlay).  It is the single shared engine for
both consumers:
  * the "ODI vs DRD Validation" quick panel (ODI vs ODI and/or vs DRD), and
  * the control-table "DRD / ODI / Manual SQL Compare" panel.

Port rules baked in (per the 4-agent review of the integration plan):
  * ``base`` is repointed to the app's integrated v15 engine
    (``app.services.odi_drd_compare_v15``), which is a superset of the bundled
    ``compare_drd_odi_universal`` -- no rewrite, just a wiring swap.
  * Pure in-memory: bytes are written to a private tempdir ONLY so openpyxl /
    ElementTree can read a real path; ALL outputs are returned as dicts.  No
    18-file disk dump, no out-dir, so the "rmtree before read" bug class
    (just fixed in the v15 endpoint) cannot recur here.
  * Empty-mapping / empty-final-lineage -> raise (no silent "no differences").
  * Single-ODI + DRD -> v16.6 Mode 1 review output; delta/proof fields are
    OMITTED, never returned empty-but-present (which would read as "no changes").
  * XML decoded with errors="replace" (never silently drop bytes).
  * ``resolve_one`` int() guard narrowed to (ValueError, TypeError).
  * No fixture/business hardcodes anywhere in the proof layer (matches the
    v16.6 NO_HARDCODE audit); evidence terms are derived from the DRD row +
    resolved ODI lineage.

The CLI tails (argparse/main/run) of the standalone files are intentionally
NOT ported -- callers use ``compare_two_odi_against_drd``.
"""
from __future__ import annotations

import gc as _gc
import re
import shutil as _shutil
import tempfile as _tempfile
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services import odi_drd_compare_v15 as base

__VERSION__ = "16.6.1-canonical-contract"

ENGINE_DELTA = "v16-generic-rule-proof"
ENGINE_ODI_VS_ODI = "odi-vs-odi"

# Comparison modes surfaced to the panel (operator-locked, 2026-06-07):
#   MODE_ODI_VS_DRD   -- ODI #1 vs DRD (no ODI #2).  Mapping Logic = DRD,
#                        ODI Logic = ODI #1.  differences = v16.6 review rows.
#   MODE_ODI_VS_ODI   -- ODI #1 vs ODI #2 (DRD optional / ignored).  Pure
#                        XML-vs-XML on RESOLVED per-column SQL; full blocks
#                        where they differ.  Mapping Logic = ODI #1, ODI Logic
#                        = ODI #2.  Handles structurally-different XMLs via the
#                        multi-step lineage resolver (_resolve_final).
#   MODE_ODI_VS_ODI_WITH_DRD -- both supplied: delta + proof (control-table
#                        panel) PLUS the XML-vs-XML differences PLUS each ODI's
#                        own mismatch set vs the DRD (acceptance: two ODI
#                        versions yield DIFFERENT mismatches vs the same DRD).
MODE_ODI_VS_DRD = "odi1_vs_drd"
MODE_ODI_VS_ODI = "odi1_vs_odi2"
MODE_ODI_VS_ODI_WITH_DRD = "odi1_vs_odi2_with_drd"


# ======================================================================================
# Thin wrappers over the v15 base helpers
# ======================================================================================

def _clean(v) -> str:
    return base.clean_text(v)


def _norm_space(v) -> str:
    return base.normalize_space(v)


def _ident(v) -> str:
    return base.normalize_identifier(v)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().upper()


def _canonical(expr: str, limit: int = 12000) -> str:
    e = _clean(expr)
    if len(e) > limit:
        e = e[:limit]
    e = re.sub(r"/\*.*?\*/", " ", e, flags=re.S)
    e = re.sub(r"--.*?$", " ", e, flags=re.M)
    e = re.sub(r"\s+", " ", e).strip().upper()
    e = re.sub(r"\s+AS\s+[A-Z_][A-Z0-9_#$]*$", "", e)
    return e


def _short(expr: str, n: int = 1600) -> str:
    e = _norm_space(expr)
    return e if len(e) <= n else e[: n - 3] + "..."


def _sql_blocks_text(sql_blocks: List[Dict[str, str]], limit: int = 500_000) -> str:
    """Render emitted ODI SQL blocks for the GUI review pane.

    This replaces the old public /compare-v15 artifact read. v16.6 already has
    the parsed ODI blocks in memory, so Mode 1 can surface the same operator
    evidence without calling the legacy endpoint.
    """
    parts: List[str] = []
    for b in sql_blocks or []:
        sql = _clean(b.get("sql", ""))
        if not sql:
            continue
        header = (
            "-- " + "=" * 88 + "\n"
            f"-- Step {b.get('step_no', '')} | Task {b.get('task_no', '')}"
            + (f" | {b.get('task_name', '')}" if b.get("task_name") else "")
            + "\n"
            "-- " + "=" * 88
        )
        parts.append(header + "\n" + base.normalize_odi_sql(sql).rstrip())
    text = "\n\n".join(parts)
    return text if len(text) <= limit else text[:limit] + "\n-- ...truncated (over 500KB)..."


# ======================================================================================
# v16.4 delta-safe engine (ported; pure functions)
# ======================================================================================

def _is_passthrough(expr: str):
    e = _norm_space(expr).strip("() ")
    m = re.match(
        r"^([A-Za-z_][A-Za-z0-9_#$]*)\.([A-Za-z_][A-Za-z0-9_#$]*)(?:\s+(?:AS\s+)?[A-Za-z_][A-Za-z0-9_#$]*)?$",
        e,
        flags=re.I,
    )
    if m:
        return _ident(m.group(1)), _ident(m.group(2))
    return None


def _build_stage_index(lineage: List[Dict[str, str]]):
    idx = defaultdict(list)
    for r in lineage:
        c = _ident(r.get("target_column", ""))
        if c:
            idx[c].append(r)
    return idx


def _resolve_one(col, final_row, lineage, stage_idx, max_depth: int = 8) -> Dict[str, str]:
    current = final_row
    path: List[str] = []
    visited = set()
    for _ in range(max_depth):
        expr = _clean(current.get("expression", ""))
        step = current.get("step_no", "")
        task = current.get("task_no", "")
        key = (step, task, _ident(current.get("target_column", col)), expr[:300].upper())
        if key in visited:
            break
        visited.add(key)
        path.append(
            f"step={step}/task={task}/col={_ident(current.get('target_column', col))}/expr={_short(expr, 180)}"
        )
        pt = _is_passthrough(expr)
        if not pt:
            break
        _, src_col = pt
        best = None
        best_score = -1
        expr_u = expr.upper().strip()
        for r in stage_idx.get(src_col, []):
            if r is current:
                continue
            rexpr = _clean(r.get("expression", ""))
            if not rexpr or rexpr.upper().strip() == expr_u:
                continue
            score = 0
            ru = rexpr.upper()
            if "CASE" in ru:
                score += 50
            if not _is_passthrough(rexpr):
                score += 25
            try:
                score += max(0, 1000 - int(r.get("step_no", "999"))) / 1000
            except (ValueError, TypeError):
                pass  # non-numeric step_no: just skip the ordering tie-breaker
            if score > best_score:
                best_score = score
                best = r
        if best is None:
            break
        current = best
    return {
        "target_column": _ident(col),
        "final_expression": final_row.get("expression", ""),
        "resolved_expression": _clean(current.get("expression", final_row.get("expression", ""))),
        "resolved_logic_full": _short(
            current.get("xml_logic_full", final_row.get("xml_logic_full", "")), 3000
        ),
        "resolved_step": current.get("step_no", ""),
        "resolved_task": current.get("task_no", ""),
        "resolution_depth": str(max(0, len(path) - 1)),
        "lineage_path": "\n".join(path),
    }


def _resolve_final(final_lineage, all_lineage) -> Dict[str, Dict[str, str]]:
    idx = _build_stage_index(all_lineage)
    out: Dict[str, Dict[str, str]] = {}
    for fr in final_lineage:
        c = _ident(fr.get("target_column", ""))
        if c:
            out[c] = _resolve_one(c, fr, all_lineage, idx)
    return out


def _area_cols(area: str) -> List[str]:
    cols = re.findall(r"`([^`]+)`", area or "")
    if not cols and re.fullmatch(r"[A-Z0-9_#$]+", area or ""):
        cols = [area]
    return [_ident(c) for c in cols if _ident(c)]


def _v15_compare(mapping_rows, final_lineage, all_lineage, sql_blocks, detection, profile):
    column_diff = base.compare_columns(mapping_rows, final_lineage)
    logic_rows = [] if profile == "avy" else base.build_logic_diff_candidates(
        mapping_rows, all_lineage, sql_blocks
    )
    raw: List[Dict[str, str]] = []
    used_curated = False
    if profile == "avy":
        raw = base.build_avy_review_rules_diff(column_diff, logic_rows, detection)
        used_curated = bool(raw)
    if not raw:
        raw = base.build_full_drd_vs_odi_xml_rules_diff(column_diff, logic_rows)
    if profile == "generic" or used_curated:
        mismatches, equivalent = raw, []
    else:
        mismatches, equivalent = base.split_mismatch_and_equivalent_rows(raw)

    by: Dict[str, Dict[str, str]] = {}
    for r in column_diff:
        c = _ident(r.get("target_column", ""))
        if not c:
            continue
        s = r.get("status", "")
        cls = (
            "MISSING_IN_ODI"
            if s == "MAPPING_ONLY"
            else ("ODI_ONLY" if s == "XML_ONLY" else "IN_BOTH_NO_REVIEW")
        )
        by[c] = {"target_column": c, "v15_class": cls, "v15_reason": s, "difference_type": "", "area": ""}
    for r in mismatches:
        for c in _area_cols(r.get("Area / Columns", "")):
            if c in by:
                by[c].update(
                    {
                        "v15_class": "REVIEW_REQUIRED",
                        "v15_reason": r.get("Conclusion", "") or r.get("Difference Type", ""),
                        "difference_type": r.get("Difference Type", ""),
                        "area": r.get("Area / Columns", ""),
                    }
                )
    for r in equivalent:
        for c in _area_cols(r.get("Area / Columns", "")):
            if c in by:
                by[c].update(
                    {
                        "v15_class": "MATCH_EQUIVALENT",
                        "v15_reason": r.get("Conclusion", "MATCH_EQUIVALENT"),
                        "difference_type": "MATCH_EQUIVALENT",
                        "area": r.get("Area / Columns", ""),
                    }
                )
    return by, column_diff, mismatches, equivalent


def _infer_profile(detection, requested: str) -> str:
    if requested != "auto":
        return requested
    blob = (
        detection.target_table_from_sheet + " " + " ".join(detection.target_resources_from_xml)
    ).upper()
    if "AVY_FACT" in blob:
        return "avy"
    if "TAX_LOT" in blob or "TAXLOTS" in blob:
        return "taxlot"
    return "generic"


def _xml_delta(orig_res, fixed_res) -> List[Dict[str, str]]:
    rows = []
    for c in sorted(set(orig_res) | set(fixed_res)):
        o = orig_res.get(c)
        f = fixed_res.get(c)
        if o and not f:
            st = "REMOVED_IN_FIXED"
        elif f and not o:
            st = "ADDED_IN_FIXED"
        else:
            st = (
                "CHANGED"
                if _canonical(o.get("resolved_expression", "")) != _canonical(f.get("resolved_expression", ""))
                else "UNCHANGED"
            )
        rows.append(
            {
                "target_column": c,
                "xml_delta_status": st,
                "original_final_expression": o.get("final_expression", "") if o else "",
                "fixed_final_expression": f.get("final_expression", "") if f else "",
                "original_resolved_expression": o.get("resolved_expression", "") if o else "",
                "fixed_resolved_expression": f.get("resolved_expression", "") if f else "",
                "original_resolution_depth": o.get("resolution_depth", "") if o else "",
                "fixed_resolution_depth": f.get("resolution_depth", "") if f else "",
                "original_lineage_path": o.get("lineage_path", "") if o else "",
                "fixed_lineage_path": f.get("lineage_path", "") if f else "",
            }
        )
    return rows


def _delta_report(orig_v15, fixed_v15, xdelta) -> List[Dict[str, str]]:
    xd = {r["target_column"]: r for r in xdelta}
    cols = sorted(set(orig_v15) | set(fixed_v15) | set(xd))
    closed = {"IN_BOTH_NO_REVIEW", "MATCH_EQUIVALENT"}
    open_ = {"REVIEW_REQUIRED", "MISSING_IN_ODI"}
    rows = []
    for c in cols:
        o = orig_v15.get(c, {"v15_class": "NO_FINAL_OR_DRD"})
        f = fixed_v15.get(c, {"v15_class": "NO_FINAL_OR_DRD"})
        os_ = o.get("v15_class", "")
        fs = f.get("v15_class", "")
        xmlst = xd.get(c, {}).get("xml_delta_status", "")
        if os_ in open_ and fs in closed:
            ds = "FIXED_BY_FINAL_COMPARE"
        elif os_ in open_ and fs in open_ and xmlst == "CHANGED":
            ds = "FIX_CANDIDATE_UPSTREAM_CHANGED"
        elif os_ in open_ and fs in open_:
            ds = "STILL_OPEN"
        elif os_ in closed and fs in open_:
            ds = "NEW_REGRESSION_BY_FINAL_COMPARE"
        elif xmlst == "CHANGED":
            ds = "UPSTREAM_CHANGED_NO_FINAL_MISMATCH"
        else:
            ds = "UNCHANGED"
        rows.append(
            {
                "target_column": c,
                "delta_status": ds,
                "original_v15_class": os_,
                "fixed_v15_class": fs,
                "original_reason": o.get("v15_reason", ""),
                "fixed_reason": f.get("v15_reason", ""),
                "original_difference_type": o.get("difference_type", ""),
                "fixed_difference_type": f.get("difference_type", ""),
                "xml_delta_status": xmlst,
                "original_resolved_expression": xd.get(c, {}).get("original_resolved_expression", ""),
                "fixed_resolved_expression": xd.get(c, {}).get("fixed_resolved_expression", ""),
                "original_lineage_path": xd.get(c, {}).get("original_lineage_path", ""),
                "fixed_lineage_path": xd.get(c, {}).get("fixed_lineage_path", ""),
            }
        )
    return rows


def _block_name(b: Dict[str, str]) -> str:
    """Best human label for a SQL block (the parser exposes task_name_1..3)."""
    for k in ("task_name", "task_name_1", "task_name_2", "task_name_3"):
        v = (b.get(k) or "").strip()
        if v:
            return v
    return ""


def _sql_block_diff(orig_blocks, fixed_blocks, excerpt_len: int = 2000) -> List[Dict[str, str]]:
    def key(b):
        return (b.get("step_no", ""), b.get("task_no", ""), _block_name(b))

    ob = {key(b): b for b in orig_blocks}
    fb = {key(b): b for b in fixed_blocks}
    rows = []
    for k in sorted(set(ob) | set(fb)):
        o = ob.get(k, {})
        f = fb.get(k, {})
        if o and not f:
            st = "REMOVED_IN_FIXED"
        elif f and not o:
            st = "ADDED_IN_FIXED"
        else:
            st = "CHANGED" if _canonical(o.get("sql", "")) != _canonical(f.get("sql", "")) else "UNCHANGED"
        rows.append(
            {
                "step_no": k[0],
                "task_no": k[1],
                "task_name": k[2],
                "sql_delta_status": st,
                "original_sql_excerpt": _short(o.get("sql", ""), excerpt_len),
                "fixed_sql_excerpt": _short(f.get("sql", ""), excerpt_len),
            }
        )
    return rows


# ======================================================================================
# v16.6 generic rule-proof overlay (ported; NO fixture hardcodes)
# ======================================================================================

_SQL_NOISE = {
    "CASE", "WHEN", "THEN", "ELSE", "END", "FROM", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "SELECT", "WHERE", "AND", "OR", "ON", "AS", "NULL", "IS", "NOT", "IN", "THE", "FOR", "USE",
    "TO", "WITH", "VALUE", "VALUES", "LOOKUP", "PARSE", "EXTRACT", "SOURCE", "TARGET",
}
_COMPONENT_NOISE = {"ID", "CD", "NM", "TP", "DT", "YR"}


def _identifiers(text: str) -> List[str]:
    """Generic identifier extractor. Does not use fixture values."""
    toks = []
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_#$]*", text or ""):
        u = _ident(t)
        if not u or u in _SQL_NOISE:
            continue
        toks.append(u)
    expanded = []
    for t in toks:
        expanded.append(t)
        for part in t.split("_"):
            if len(part) >= 2 and part not in _COMPONENT_NOISE:
                expanded.append(part)
    out = []
    seen = set()
    for t in expanded:
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _snippets(xml: str, terms: Iterable[str], radius: int = 3500, max_parts: int = 12) -> str:
    parts = []
    seen = set()
    for term in sorted(set(t for t in terms if t), key=len, reverse=True):
        if len(term) < 2:
            continue
        try:
            pattern = re.escape(term)
        except re.error:
            continue
        for m in re.finditer(pattern, xml, flags=re.I):
            s = max(0, m.start() - radius)
            e = min(len(xml), m.end() + radius)
            keyse = (s, e)
            if keyse not in seen:
                parts.append(xml[s:e])
                seen.add(keyse)
            if len(parts) >= max_parts:
                return "\n".join(parts)
    return "\n".join(parts)


def _drd_text(col: str, drdrow: Dict[str, str]) -> str:
    return " ".join(
        [
            col or "",
            drdrow.get("source_1", "") or "",
            drdrow.get("source_2", "") or "",
            drdrow.get("source_3", "") or "",
            drdrow.get("drd_rule", "") or "",
        ]
    )


def _source_attr(drdrow: Dict[str, str]) -> str:
    return _ident(drdrow.get("source_3", ""))


def _source_table(drdrow: Dict[str, str]) -> str:
    return _ident(drdrow.get("source_2", ""))


def _target_suffix(col: str) -> str:
    parts = _ident(col).split("_")
    return parts[-1] if parts else ""


def _source_guard_requirements(text: str) -> List[Tuple[str, str]]:
    """Find generic source guard predicates: <identifier> = <number>."""
    req = []
    for name, val in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_#$]*(?:\.[A-Za-z_][A-Za-z0-9_#$]*)?)\s*=\s*(\d+)\b", text or ""
    ):
        nm = _ident(name.split(".")[-1])
        if nm and val:
            req.append((nm, val))
    return list(dict.fromkeys(req))


def _required_checks_from_drd(col: str, drdrow: Dict[str, str]) -> List[Dict[str, str]]:
    """Build proof checks from DRD row only. No fixture tokens."""
    text_raw = _drd_text(col, drdrow)
    text = _norm(text_raw)
    checks: List[Dict[str, str]] = []
    attr = _source_attr(drdrow)
    tbl = _source_table(drdrow)
    suffix = _target_suffix(col)

    for guard_col, guard_val in _source_guard_requirements(text_raw):
        checks.append({"type": "source_guard", "identifier": guard_col, "value": guard_val})

    if re.search(r"\b(PARSE|EXTRACT|SPLIT|SUBSTR|SUBSTRING|LAST\s+TWO|DIGITS?)\b", text, flags=re.I):
        checks.append({"type": "parse_logic", "identifier": attr})

    if re.search(r"\b(LOOKUP|LOOK\s+UP|JOIN|DIMENSION|REFERENCE)\b", text, flags=re.I) or tbl:
        if re.search(r"\b(LOOKUP|LOOK\s+UP|JOIN|DIMENSION)\b", text, flags=re.I):
            checks.append({"type": "lookup_logic", "source_table": tbl, "source_attr": attr})

    if suffix in {"YR", "YEAR"}:
        checks.append({"type": "year_logic", "identifier": attr})

    if suffix in {"DT", "DATE"} and attr:
        checks.append({"type": "source_attr_present", "identifier": attr})

    if re.search(r"\b(CURRENCY|CCY)\b", text, flags=re.I) and attr:
        checks.append({"type": "source_attr_present", "identifier": attr})

    if suffix in {"CD", "CODE", "NM", "NAME"} and attr and any(c["type"] == "lookup_logic" for c in checks):
        checks.append({"type": "source_attr_present", "identifier": attr})

    out = []
    seen = set()
    for c in checks:
        key = tuple(sorted(c.items()))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _terms_from_context(col: str, drdrow: Dict[str, str], resolved_expr: str, lineage_path: str = "") -> List[str]:
    text = " ".join([_drd_text(col, drdrow), resolved_expr or "", lineage_path or ""])
    return _identifiers(text)


def _has_identifier(evidence: str, token: str) -> bool:
    token = _ident(token)
    if not token:
        return False
    return re.search(
        r"(?<![A-Z0-9_#$])" + re.escape(token) + r"(?![A-Z0-9_#$])", evidence, flags=re.I
    ) is not None


def _evaluate_check(check: Dict[str, str], evidence: str) -> Tuple[bool, str]:
    ev = _norm(evidence)
    typ = check["type"]

    if typ == "source_guard":
        guard_col = check.get("identifier", "")
        guard_val = check.get("value", "")
        ok = (
            _has_identifier(ev, guard_col)
            and re.search(r"\b" + re.escape(guard_val) + r"\b", ev) is not None
            and "CASE" in ev
        )
        return ok, f"requires CASE guard {guard_col}={guard_val}"

    if typ == "parse_logic":
        ok = any(fn in ev for fn in ["SUBSTR", "SUBSTRING", "REGEXP", "INSTR"])
        return ok, "requires parse function such as SUBSTR/REGEXP/INSTR"

    if typ == "lookup_logic":
        tbl = check.get("source_table", "")
        attr = check.get("source_attr", "")
        ok = (attr and _has_identifier(ev, attr)) or (tbl and _has_identifier(ev, tbl))
        ok = ok and ("JOIN" in ev or "SELECT" in ev or "LOOKUP" in ev or "CL_" in ev or "DIM" in ev)
        return ok, f"requires lookup/join evidence from DRD source table/attribute {tbl}.{attr}"

    if typ == "year_logic":
        ok = any(fn in ev for fn in ["SUBSTR", "SUBSTRING", "REGEXP"]) and any(
            tok in ev for tok in ["TO_NUMBER", "YEAR", "'20'", "'19'", "20||", "19||"]
        )
        return ok, "requires parse + year/century/numeric construction evidence"

    if typ == "source_attr_present":
        attr = check.get("identifier", "")
        ok = bool(attr) and _has_identifier(ev, attr)
        return ok, f"requires source/output attribute {attr}"

    return False, f"unknown check {typ}"


def _prove(
    col: str, drdrow: Dict[str, str], xml_text: str, resolved_expr: str, lineage_path: str = ""
) -> Tuple[bool, List[Dict[str, object]], str, List[str]]:
    checks = _required_checks_from_drd(col, drdrow)
    terms = _terms_from_context(col, drdrow, resolved_expr, lineage_path)
    resolved_terms = [
        t
        for t in _identifiers((resolved_expr or "") + " " + (lineage_path or ""))
        if t not in {"INLINE", "VIEW", "STEP", "TASK", "COL", "EXPR"}
    ]
    local_xml = "\n".join(
        [
            _snippets(xml_text, [col], radius=30000, max_parts=30),
            _snippets(xml_text, resolved_terms[:25], radius=8000, max_parts=10),
        ]
    )
    evidence = _norm("\n".join([resolved_expr or "", lineage_path or "", local_xml]))

    if not checks:
        return False, [], evidence[:4000], terms

    rows: List[Dict[str, object]] = []
    passed = True
    for c in checks:
        ok, detail = _evaluate_check(c, evidence)
        rows.append({"type": c.get("type"), "detail": detail, "passed": ok, "derived_check": c})
        passed = passed and ok
    return passed, rows, evidence[:4000], terms


def reclassify_delta(
    delta_rows: List[Dict[str, str]],
    drd_by_target: Dict[str, Dict[str, str]],
    original_xml: str,
    fixed_xml: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """In-memory generic rule-proof. Promotes FIX_CANDIDATE_UPSTREAM_CHANGED ->
    FIXED_BY_RESOLVED_RULE_PROOF only when the fixed ODI satisfies DRD-derived
    proof checks AND the original does not.  Returns (new_delta_rows, proof_rows)."""
    new_rows: List[Dict[str, str]] = []
    proof_rows: List[Dict[str, str]] = []
    for r in delta_rows:
        col = r.get("target_column", "")
        nr = dict(r)
        dr = drd_by_target.get(col)
        if r.get("delta_status") == "FIX_CANDIDATE_UPSTREAM_CHANGED" and dr:
            opass, ochecks, oevidence, oterms = _prove(
                col, dr, original_xml, r.get("original_resolved_expression", ""), r.get("original_lineage_path", "")
            )
            fpass, fchecks, fevidence, fterms = _prove(
                col, dr, fixed_xml, r.get("fixed_resolved_expression", ""), r.get("fixed_lineage_path", "")
            )
            if fpass and not opass:
                nr["delta_status"] = "FIXED_BY_RESOLVED_RULE_PROOF"
                nr["fixed_reason"] = (
                    "Fixed resolved ODI lineage satisfies DRD-derived generic proof checks; original did not."
                )
            proof_rows.append(
                {
                    "target_column": col,
                    "original_proof_passed": "Y" if opass else "N",
                    "fixed_proof_passed": "Y" if fpass else "N",
                    "original_checks": ochecks,
                    "fixed_checks": fchecks,
                    "original_terms_derived_from_input": oterms[:80],
                    "fixed_terms_derived_from_input": fterms[:80],
                    "original_evidence_excerpt": oevidence,
                    "fixed_evidence_excerpt": fevidence,
                }
            )
        new_rows.append(nr)
    return new_rows, proof_rows


# ======================================================================================
# In-memory I/O + orchestration
# ======================================================================================

def _args_shim(
    *,
    profile: str = "auto",
    target_table: str = "",
    mapping_sheet: str = "",
    target_col: str = "",
    source_cols: str = "",
    rule_col: str = "",
    header_row: Optional[int] = None,
) -> SimpleNamespace:
    # header_row MUST default to None (auto-detect); 0/"" would mis-detect.
    return SimpleNamespace(
        profile=profile,
        target_table=target_table,
        mapping_sheet=mapping_sheet,
        target_col=target_col,
        source_cols=source_cols,
        rule_col=rule_col,
        header_row=header_row,
    )


def _load_odi(xml_path: Path):
    objects = base.parse_odi_objects(xml_path)
    targets = base.extract_target_resources_from_xml(objects)
    _, _, blocks = base.extract_odi_summary(objects)
    lineage = base.build_odi_lineage(blocks)
    final = base.select_final_target_lineage(lineage)
    return targets, blocks, lineage, final


def _load_drd(xlsx_path: Path, xml_targets: List[str], args: SimpleNamespace):
    from openpyxl import load_workbook

    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    detection = base.auto_detect_mapping(
        wb,
        xml_targets=xml_targets,
        target_table_override=args.target_table or "",
        mapping_sheet_override=args.mapping_sheet or "",
        header_row_override=args.header_row,
        target_col_override=args.target_col or "",
        source_cols_override=args.source_cols or "",
        rule_col_override=args.rule_col or "",
    )
    rows, notes = base.extract_mapping_from_xlsx(xlsx_path, detection)
    return detection, rows, notes


def _summary_counts(by: Dict[str, Dict[str, str]]) -> Dict[str, int]:
    return dict(Counter(r.get("v15_class", "") for r in by.values()))


def _drd_logic(col: str, mapping_by_target: Dict[str, Dict[str, str]]) -> str:
    """Best-effort DRD-side rule/expr text for a column (generic, no hardcode)."""
    r = mapping_by_target.get(_ident(col)) or mapping_by_target.get(col) or {}
    rule = (r.get("drd_rule") or "").strip()
    if rule:
        return rule
    srcs = [r.get("source_1", ""), r.get("source_2", ""), r.get("source_3", "")]
    return " | ".join(s for s in (x.strip() for x in srcs) if s)


def _diffs_from_v15_mismatches(
    mismatches: List[Dict[str, str]],
    mapping_by_target: Dict[str, Dict[str, str]],
    resolved_by_target: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """Unified difference rows for ODI-vs-DRD (Mode 1).

    Mapping Logic column = DRD; ODI Logic column = the ODI under test (the
    RESOLVED per-column expression when available, else the v15 logic field).
    ``resolved_by_target`` carries that ODI's resolved lineage, so the same DRD
    compared against two different ODIs surfaces each ODI's own logic.  Keeps
    the raw v15 fields too so the legacy review table still renders.
    """
    resolved_by_target = resolved_by_target or {}
    out: List[Dict[str, str]] = []
    for r in mismatches:
        area = r.get("Area / Columns", "")
        cols = _area_cols(area)
        col = cols[0] if cols else _ident(area)
        rres = resolved_by_target.get(_ident(col)) or resolved_by_target.get(col) or {}
        resolved_logic = rres.get("resolved_expression", "")
        odi_logic = resolved_logic or r.get("ODI XML Logic", "")
        out.append(
            {
                "target_column": col,
                "status": r.get("Difference Type", "") or "DIFFERENCE",
                "mapping_logic_label": "DRD",
                "mapping_logic": r.get("Mapping Logic", "") or _drd_logic(col, mapping_by_target),
                "odi_logic_label": "ODI",
                "odi_logic": odi_logic,
                "odi_resolved_logic": resolved_logic,
                "odi_lineage": rres.get("lineage_path", ""),
                "conclusion": r.get("Conclusion", ""),
                "recommended_action": r.get("Recommended Action", ""),
                # raw v15 fields preserved for the legacy review table:
                "Area / Columns": area,
                "Difference Type": r.get("Difference Type", ""),
                "Mapping Logic": r.get("Mapping Logic", ""),
                "ODI XML Logic": r.get("ODI XML Logic", ""),
                "Conclusion": r.get("Conclusion", ""),
                "Recommended Action": r.get("Recommended Action", ""),
            }
        )
    return out


def _severity_for_review_row(row: Dict[str, str]) -> str:
    dt = (row.get("Difference Type", "") or row.get("status", "") or "").lower()
    c = (row.get("Conclusion", "") or row.get("conclusion", "") or "").lower()
    if "missing implementation" in dt or "missing target column" in dt:
        return "missing"
    if "structural mismatch" in dt or "confirmed structural" in c or "structural gap" in c:
        return "real_gap"
    if "odi-only" in c or "environment" in c or "target risk" in c or "xml-only column" in dt:
        return "odi_only"
    if (
        "structural difference" in c
        or "structural lineage" in c
        or "operationally specific" in c
        or "more detailed" in dt
        or "xml-only exception" in dt
        or "journal source" in dt
        or "where-vs-case" in dt
        or "join filter moved" in dt
        or "acceptable" in c
    ):
        return "structural"
    return "logic_drift"


def _review_table_shape(
    *,
    differences: List[Dict[str, str]],
    column_diff: List[Dict[str, str]],
    sql_blocks: List[Dict[str, str]],
    detection_human: Dict[str, Any],
) -> Dict[str, Any]:
    """Add the v16.6 Mode-1 fields consumed by the existing GUI review table."""
    statuses = [r.get("status", "") for r in column_diff or []]
    summary = {
        "mapping_columns": sum(1 for s in statuses if s in ("IN_BOTH", "MAPPING_ONLY")),
        "in_both": statuses.count("IN_BOTH"),
        "mapping_only": statuses.count("MAPPING_ONLY"),
        "xml_only": statuses.count("XML_ONLY"),
    }
    for r in differences:
        r["severity"] = _severity_for_review_row(r)
    diff_sev = [r.get("severity", "") for r in differences]
    bucket_counts = {
        "missing": diff_sev.count("missing"),
        "real_gap": diff_sev.count("real_gap"),
        "logic_drift": diff_sev.count("logic_drift"),
        "structural": diff_sev.count("structural"),
        "odi_extra": summary["xml_only"] + diff_sev.count("odi_only"),
    }
    bucket_counts["matched"] = max(
        summary["in_both"]
        - (diff_sev.count("logic_drift") + diff_sev.count("structural") + diff_sev.count("odi_only")),
        0,
    )
    return {
        "summary": summary,
        "bucket_counts": bucket_counts,
        "detection": detection_human,
        "column_diff_count": len(column_diff or []),
        "sql": _sql_blocks_text(sql_blocks),
    }


_XML_STATUS_HUMAN = {
    "CHANGED": "Both ODIs resolve this column to a concrete transform and they differ.",
    "NO_RESOLVED_LINEAGE": "One ODI routes this column through an inline view / staging, so the per-column logic is not directly resolvable -- review the full SQL block.",
    "ONLY_IN_ODI1": "Column produced by ODI #1 only (absent in ODI #2).",
    "ONLY_IN_ODI2": "Column produced by ODI #2 only (absent in ODI #1).",
}

# Action priority (lower = more actionable, surfaced first).
_XML_STATUS_ORDER = {
    "CHANGED": 0,
    "ONLY_IN_ODI1": 1,
    "ONLY_IN_ODI2": 2,
    "NO_RESOLVED_LINEAGE": 3,
}


def _classify_xml_change(orig_expr: str, fixed_expr: str) -> str:
    """Per-column resolved-SQL difference status (matches the standalone v16.6).

    CHANGED when both ODIs produced a resolved expression and they differ (even
    if one side is still an inline-view pointer -- that IS a difference);
    UNCHANGED when equal; NO_RESOLVED_LINEAGE when a side produced no resolved
    expression at all.  The clean vs noisy output is controlled by column
    SELECTION (the DRD review set), not by this classifier."""
    o = (orig_expr or "").strip()
    f = (fixed_expr or "").strip()
    if not o or not f:
        return "NO_RESOLVED_LINEAGE"
    return "CHANGED" if _canonical(o) != _canonical(f) else "UNCHANGED"


def _diffs_from_xml_delta(
    xdelta: List[Dict[str, str]], selected_set: Optional[set] = None
) -> List[Dict[str, str]]:
    """Unified difference rows for ODI-vs-ODI.

    Mapping Logic column = ODI #1 (resolved, FULL block); ODI Logic column =
    ODI #2 (resolved, FULL block).  UNCHANGED columns are dropped.  Each row is
    tagged ``selected`` = the column is in the DRD review set (when a DRD is
    present); the UI defaults to the selected set (the standalone's chosen
    columns) and offers "show all".  When ``selected_set`` is None every row is
    marked selected (no DRD -> show all).
    """
    out: List[Dict[str, str]] = []
    for r in xdelta:
        raw = r.get("xml_delta_status", "")
        if raw == "UNCHANGED":
            continue
        o = r.get("original_resolved_expression", "")
        f = r.get("fixed_resolved_expression", "")
        if raw == "ADDED_IN_FIXED":
            st = "ONLY_IN_ODI2"
        elif raw == "REMOVED_IN_FIXED":
            st = "ONLY_IN_ODI1"
        else:  # CHANGED
            st = _classify_xml_change(o, f)
        col = r.get("target_column", "")
        out.append(
            {
                "target_column": col,
                "status": st,
                "selected": True if selected_set is None else (_ident(col) in selected_set),
                "mapping_logic_label": "ODI #1",
                # FULL per-column resolved SQL block (untruncated):
                "mapping_logic": o,
                "odi_logic_label": "ODI #2",
                "odi_logic": f,
                "conclusion": _XML_STATUS_HUMAN.get(st, ""),
                "mapping_final": r.get("original_final_expression", ""),
                "odi_final": r.get("fixed_final_expression", ""),
                "mapping_lineage": r.get("original_lineage_path", ""),
                "odi_lineage": r.get("fixed_lineage_path", ""),
            }
        )
    out.sort(key=lambda d: (_XML_STATUS_ORDER.get(d["status"], 9), d["target_column"]))
    return out


def _review_column_set(*by_targets: Dict[str, Dict[str, str]]) -> set:
    """Columns flagged REVIEW_REQUIRED / MISSING_IN_ODI in any v15 classification
    -- the DRD review set the standalone resolves (its `selected_resolved_columns`)."""
    sel = set()
    for by in by_targets:
        for col, r in (by or {}).items():
            if r.get("v15_class") in ("REVIEW_REQUIRED", "MISSING_IN_ODI"):
                sel.add(_ident(col))
    return sel


def compare_two_odi_against_drd(
    drd_bytes: bytes,
    odi1_bytes: bytes,
    odi2_bytes: Optional[bytes] = None,
    *,
    profile: str = "auto",
    target_table: str = "",
    mapping_sheet: str = "",
    target_col: str = "",
    source_cols: str = "",
    rule_col: str = "",
    header_row: Optional[int] = None,
) -> Dict[str, Any]:
    """Compare ODI(s) against a DRD and/or each other, fully in memory.

    Three modes, auto-selected from which inputs are present:

    * DRD + ODI #1, no ODI #2  -> MODE_ODI_VS_DRD (engine=ENGINE_DELTA).
      Quick v16.6 ODI-vs-DRD classification; ``differences`` = review rows
      (Mapping Logic = DRD, ODI Logic = ODI #1).  Keeps ``v16_by_target``.
    * ODI #1 + ODI #2, no DRD  -> MODE_ODI_VS_ODI (engine="odi-vs-odi").
      Pure XML-vs-XML on RESOLVED per-column SQL; ``differences`` carries the
      FULL per-column blocks where ODI #1 and ODI #2 differ (Mapping Logic =
      ODI #1, ODI Logic = ODI #2).  Structurally-different XMLs (extra inline
      view / aggregation) are normalised by the lineage resolver.
    * DRD + ODI #1 + ODI #2    -> MODE_ODI_VS_ODI_WITH_DRD
      (engine="v16-generic-rule-proof").  The control-table delta + rule-proof,
      PLUS the XML-vs-XML ``differences``, PLUS ``odi1_vs_drd`` / ``odi2_vs_drd``
      (each ODI's own mismatch set vs the same DRD -- they differ).

    Raises ValueError if neither a DRD nor an ODI #2 is supplied, on empty DRD
    mapping, or on empty ODI final lineage (never a silent "no differences").
    """
    if not odi1_bytes:
        raise ValueError("ODI XML #1 is empty.")
    has_drd = bool(drd_bytes)
    has_odi2 = bool(odi2_bytes)
    if not has_drd and not has_odi2:
        raise ValueError(
            "Provide a DRD (ODI vs DRD) or a second ODI (ODI vs ODI) to compare against."
        )

    args = _args_shim(
        profile=profile,
        target_table=target_table,
        mapping_sheet=mapping_sheet,
        target_col=target_col,
        source_cols=source_cols,
        rule_col=rule_col,
        header_row=header_row,
    )

    td = _tempfile.mkdtemp(prefix="v16_")
    tdp = Path(td)
    try:
        odi1_path = tdp / "odi1.xml"
        odi1_path.write_bytes(odi1_bytes)
        o1_targets, o1_blocks, o1_lineage, o1_final = _load_odi(odi1_path)
        if not o1_final:
            raise ValueError(
                "ODI XML #1 produced 0 final-target lineage rows -- the XML may be missing a target-load step."
            )

        if has_odi2:
            odi2_path = tdp / "odi2.xml"
            odi2_path.write_bytes(odi2_bytes)
            o2_targets, o2_blocks, o2_lineage, o2_final = _load_odi(odi2_path)
            if not o2_final:
                raise ValueError(
                    "ODI XML #2 produced 0 final-target lineage rows -- the XML may be missing a target-load step."
                )
            xml_targets = list(dict.fromkeys(o1_targets + o2_targets))
        else:
            xml_targets = list(o1_targets)

        # ---- Load DRD only when supplied (Mode 2 is pure XML-vs-XML) ----
        detection = None
        mapping_rows: List[Dict[str, str]] = []
        if has_drd:
            drd_path = tdp / "drd.xlsx"
            drd_path.write_bytes(drd_bytes)
            detection, mapping_rows, _notes = _load_drd(drd_path, xml_targets, args)
            if not mapping_rows:
                raise ValueError(
                    "DRD extraction produced 0 mapping rows -- check the mapping sheet / header detection."
                )
            resolved_profile = _infer_profile(detection, profile)
            detection_human = detection.as_human()
            mapping_by_target = {r["target_column"]: r for r in mapping_rows}
        else:
            resolved_profile = profile if profile != "auto" else "generic"
            detection_human = {}
            mapping_by_target = {}

        # ================= MODE 2: pure ODI #1 vs ODI #2 (no DRD) =================
        if has_odi2 and not has_drd:
            orig_res = _resolve_final(o1_final, o1_lineage)
            fixed_res = _resolve_final(o2_final, o2_lineage)
            xdelta = _xml_delta(orig_res, fixed_res)
            # No DRD -> no review set to focus on -> every diff is "selected"
            # (the UI shows them all; attach a DRD to focus on the review set).
            differences = _diffs_from_xml_delta(xdelta, selected_set=None)
            # FULL blocks where the two ODIs differ (authoritative "полные блоки
            # где несовпадение"): keep only non-UNCHANGED, untruncated.
            sdiff = [
                r for r in _sql_block_diff(o1_blocks, o2_blocks, excerpt_len=60000)
                if r["sql_delta_status"] != "UNCHANGED"
            ]
            return {
                "engine": ENGINE_ODI_VS_ODI,
                "version": __VERSION__,
                "mode": MODE_ODI_VS_ODI,
                "profile_resolved": resolved_profile,
                "detection": detection_human,
                "mapping_rows": 0,
                "differences": differences,
                # Canonical stable field names (Gate A): keep legacy fields too.
                "resolved_xml_delta_rows": differences,
                "drd_vs_odi1_rows": [],
                "drd_vs_odi2_rows": [],
                "delta_report_rows": [],
                "proof_rows": [],
                "sql_block_diff_rows": sdiff,
                "has_selection": False,
                "xml_delta": xdelta,
                "sql_block_diff": sdiff,
                "summary": {
                    "difference_counts": dict(Counter(d["status"] for d in differences)),
                    "columns_compared": len(set(orig_res) | set(fixed_res)),
                    "differences_total": len(differences),
                    "changed_blocks": len(sdiff),
                },
            }

        # ================= MODE 1: ODI #1 vs DRD (no ODI #2) =================
        if has_drd and not has_odi2:
            by1, cd1, m1, _e1 = _v15_compare(
                mapping_rows, o1_final, o1_lineage, o1_blocks, detection, resolved_profile
            )
            o1_res = _resolve_final(o1_final, o1_lineage)
            differences = _diffs_from_v15_mismatches(m1, mapping_by_target, o1_res)
            review_shape = _review_table_shape(
                differences=differences,
                column_diff=cd1,
                sql_blocks=o1_blocks,
                detection_human=detection_human,
            )
            review_shape["summary"]["v16_class_counts"] = _summary_counts(by1)
            review_shape["summary"]["difference_count"] = len(differences)
            return {
                "engine": ENGINE_DELTA,
                "version": __VERSION__,
                "mode": MODE_ODI_VS_DRD,
                "profile_resolved": resolved_profile,
                "detection": detection_human,
                "mapping_rows": len(mapping_rows),
                "differences": differences,
                # Canonical stable field names (Gate A): keep legacy fields too.
                "drd_vs_odi1_rows": differences,
                "drd_vs_odi2_rows": [],
                "resolved_xml_delta_rows": [],
                "delta_report_rows": [],
                "proof_rows": [],
                "sql_block_diff_rows": [],
                "v16_by_target": by1,
                **review_shape,
            }

        # ============ BOTH: ODI #1 + ODI #2 + DRD -> delta + proof ============
        orig_v15, _ocd, om, _oe = _v15_compare(
            mapping_rows, o1_final, o1_lineage, o1_blocks, detection, resolved_profile
        )
        fixed_v15, _fcd, fm, _fe = _v15_compare(
            mapping_rows, o2_final, o2_lineage, o2_blocks, detection, resolved_profile
        )
        orig_res = _resolve_final(o1_final, o1_lineage)
        fixed_res = _resolve_final(o2_final, o2_lineage)
        xdelta = _xml_delta(orig_res, fixed_res)
        delta = _delta_report(orig_v15, fixed_v15, xdelta)
        # FULL blocks where the two ODIs differ (non-UNCHANGED, untruncated).
        sdiff = [
            r for r in _sql_block_diff(o1_blocks, o2_blocks, excerpt_len=60000)
            if r["sql_delta_status"] != "UNCHANGED"
        ]
        # DRD review set -> the standalone's selected_resolved_columns. The
        # ODI-vs-ODI resolved delta defaults to these; "show all" reveals the rest.
        review_set = _review_column_set(orig_v15, fixed_v15)
        odi_vs_odi_diffs = _diffs_from_xml_delta(xdelta, review_set)

        drd_by_target = {r["target_column"]: r for r in mapping_rows}
        original_xml = odi1_bytes.decode("utf-8", errors="replace")
        fixed_xml = odi2_bytes.decode("utf-8", errors="replace")
        delta, proof = reclassify_delta(delta, drd_by_target, original_xml, fixed_xml)

        # Acceptance: each ODI's OWN mismatch set vs the same DRD -- enriched
        # with that ODI's resolved per-column logic, so two versions surface
        # their own logic against the same DRD even when the v15-final classes
        # coincide (upstream-only changes).
        odi1_vs_drd = _diffs_from_v15_mismatches(om, drd_by_target, orig_res)
        odi2_vs_drd = _diffs_from_v15_mismatches(fm, drd_by_target, fixed_res)

        delta_counts = dict(Counter(r.get("delta_status", "") for r in delta))
        return {
            "engine": ENGINE_DELTA,
            "version": __VERSION__,
            "mode": MODE_ODI_VS_ODI_WITH_DRD,
            "profile_resolved": resolved_profile,
            "detection": detection_human,
            "mapping_rows": len(mapping_rows),
            "delta": delta,
            "proof": proof,
            "sql_block_diff": sdiff,
            "differences": odi_vs_odi_diffs,
            # Canonical stable field names (Gate A): keep legacy fields too.
            "drd_vs_odi1_rows": odi1_vs_drd,
            "drd_vs_odi2_rows": odi2_vs_drd,
            "resolved_xml_delta_rows": odi_vs_odi_diffs,
            "delta_report_rows": delta,
            "proof_rows": proof,
            "sql_block_diff_rows": sdiff,
            "has_selection": True,
            "selected_count": sum(1 for d in odi_vs_odi_diffs if d.get("selected")),
            "odi1_vs_drd": odi1_vs_drd,
            "odi2_vs_drd": odi2_vs_drd,
            "v15_original": orig_v15,
            "v15_fixed": fixed_v15,
            "summary": {
                "delta_status_counts": delta_counts,
                "fixed_by_resolved_rule_proof": [
                    r["target_column"] for r in delta if r.get("delta_status") == "FIXED_BY_RESOLVED_RULE_PROOF"
                ],
                "fix_candidate_upstream_changed": [
                    r["target_column"] for r in delta if r.get("delta_status") == "FIX_CANDIDATE_UPSTREAM_CHANGED"
                ],
                "still_open": [r["target_column"] for r in delta if r.get("delta_status") == "STILL_OPEN"],
                "new_regression": [
                    r["target_column"] for r in delta if "REGRESSION" in r.get("delta_status", "")
                ],
                "original_v15_class_counts": _summary_counts(orig_v15),
                "fixed_v15_class_counts": _summary_counts(fixed_v15),
                "odi1_vs_drd_count": len(odi1_vs_drd),
                "odi2_vs_drd_count": len(odi2_vs_drd),
            },
        }
    finally:
        _gc.collect()  # release openpyxl read_only file handles before rmtree (Windows lock)
        _shutil.rmtree(tdp, ignore_errors=True)
