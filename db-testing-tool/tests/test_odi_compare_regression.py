"""Regression tests for the ODI-vs-DRD comparison panel.

Every case here reproduces an issue the operator demonstrated on 2026-06-03
(target auto-detect, DRD-typo alias drift via PDM, both-NULL match, ODI_EXTRA
targeting the real table, MERGE-only compare not crashing).  Per the project
rule "every fix ships a regression test", these are permanent guards so the
NEXT fix cannot silently re-break them.

Fixtures: the real taxlot + AVY scenario files shipped in the repo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# Real fixtures (extracted from data/taxlot.zip into data/taxlot/ + the AVY files at repo root).
_CLOSE_XML = _ROOT / "data" / "taxlot" / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
_CLOSE_DRD = _ROOT / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
_OPEN_XML = _ROOT / "data" / "taxlot" / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
_OPEN_DRD = _ROOT / "data" / "taxlot" / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"
_AVY_XML = _ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
# NOTE (operator 2026-06-03): use ONLY the DRD .xlsx -- the Open_lot/closed-lot
# CSV/xlsx mapping extracts were intentionally deleted ("используй только ДРД").

_missing = [p.name for p in (_CLOSE_XML, _CLOSE_DRD, _OPEN_XML) if not p.exists()]
pytestmark = pytest.mark.skipif(
    bool(_missing), reason=f"taxlot fixtures missing (extract data/taxlot.zip): {_missing}"
)


def _parse(xml_path: Path, ts: str = "", tt: str = ""):
    from app.sql_model.odi_parser import OdiXmlParser
    return OdiXmlParser(target_schema=ts, target_table=tt).parse_text(
        xml_path.read_text(encoding="ISO-8859-1")
    )


# ── Target auto-detect (operator #1/#2/#5/#6: CLOSE/OPEN showed AVY) ──────────

def test_close_target_autodetects_to_taxlot_not_avy():
    m = _parse(_CLOSE_XML)
    assert (m.target.schema, m.target.table) == (
        "TAXLOTS_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT"
    ), f"CLOSE target should auto-detect from XML, got {m.target.schema}.{m.target.table}"
    assert m.target.table != "AVY_FACT_SIDE"


def test_open_target_autodetects_to_taxlot_not_avy():
    m = _parse(_OPEN_XML)
    assert (m.target.schema, m.target.table) == (
        "TAXLOTS_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT"
    ), f"OPEN target should auto-detect, got {m.target.schema}.{m.target.table}"


def test_explicit_caller_target_still_wins():
    m = _parse(_CLOSE_XML, ts="IKOROSTELEV", tt="MY_CONTROL_TBL")
    assert (m.target.schema, m.target.table) == ("IKOROSTELEV", "MY_CONTROL_TBL")


# ── CLOSE FROM parsing: ANSI joins + CL_VAL inline subqueries resolve ─────────

def test_close_resolves_acg_tp_dim_and_cl_val_subqueries():
    m = _parse(_CLOSE_XML)
    s = m.staging_steps[0]
    aliases = {b.alias for b in (s.source_bindings or [])}
    # ACG_TP_DIM (simple ANSI join) + CL_VAL inline-subquery slices must register
    assert "ACG_TP_DIM" in aliases, aliases
    assert any(a.startswith("CL_VAL") for a in aliases), aliases
    # every column mapping resolves its ODI source (no ALIAS_NOT_IN_JOIN_GRAPH)
    from app.sql_model.types import ResolvedColumn
    unresolved = [c.target_col for c in (s.column_mappings or [])
                  if not isinstance(c.source, ResolvedColumn)]
    assert not unresolved, f"unresolved ODI sources: {unresolved}"


# ── Comparison verdicts (operator-shown rows) ────────────────────────────────

def _compare(xml_path: Path, drd_path: Path):
    from app.services.v9_pipeline import generate_v9
    v9 = generate_v9(
        drd_bytes=drd_path.read_bytes(),
        drd_filename=drd_path.name,
        odi_xml_bytes=xml_path.read_bytes(),
        target_schema="",
        target_table="",
    )
    return {r["target_col"]: r for r in v9.comparison_rows}, v9


def test_close_drd_typo_column_is_alias_drift_via_pdm():
    """ACG_TP_NM: DRD typo AC_TP_DSC vs ODI correct ACG_TP_DSC -> ALIAS_DRIFT_ONLY."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    r = rows.get("ACG_TP_NM")
    assert r is not None
    assert r["verdict"] == "ALIAS_DRIFT_ONLY", f"got {r['verdict']}: {r.get('explanation')}"


def test_close_both_null_is_matched_not_unresolvable():
    """DRD says NULL and ODI projects NULL -> they AGREE -> MATCHED."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    for col in ("ADJ_COST_NOT_ACRT_F", "LIFE_TO_DTD_MKT_DCN", "PREM_AMT"):
        r = rows.get(col)
        assert r is not None, col
        assert r["verdict"] == "MATCHED", (
            f"{col}: DRD={r.get('drd_logic')!r} ODI={r.get('odi_logic')!r} "
            f"both NULL must be MATCHED, got {r['verdict']}"
        )


def test_odi_extra_references_real_target_not_avy():
    """ODI_EXTRA rows must reference the taxlot target, never AVY_FACT_SIDE."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    extras = [r for r in rows.values() if r["verdict"] == "ODI_EXTRA"]
    for r in extras:
        blob = json.dumps(r)
        assert "AVY_FACT_SIDE" not in blob, f"ODI_EXTRA still references AVY: {r['target_col']}"


def test_merge_only_open_compare_does_not_crash():
    """OPEN is MERGE-only (0 staging steps); compare must still return rows,
    not blow up on the secondary sql_emitter emit."""
    rows, v9 = _compare(_OPEN_XML, _OPEN_DRD)
    assert len(rows) > 0
    # emit degraded to a note (not a crash)
    assert "no staging steps" in v9.insert_sql or "INSERT" in v9.insert_sql


# ── Frontend guards (operator 2026-06-03: AVY hardcode + reset-on-new-file) ───

def test_odi_panel_js_has_no_avy_fallback():
    """The ODI-panel JS must NOT fall back to AVY_FACT_SIDE/IKOROSTELEV when the
    target field is blank -- blank means 'auto-detect from XML'."""
    html = (_ROOT / "app" / "templates" / "mappings.html").read_text(encoding="utf-8")
    assert "|| 'AVY_FACT_SIDE'" not in html, "ODI JS still falls back to AVY_FACT_SIDE"
    assert "|| 'IKOROSTELEV'" not in html, "ODI JS still falls back to IKOROSTELEV"


def test_odi_panel_resets_on_new_file():
    """A new attached file must reset the panel (counts/grid/sql/target/badge)."""
    html = (_ROOT / "app" / "templates" / "mappings.html").read_text(encoding="utf-8")
    assert "_odiResetPanel" in html
    assert "addEventListener('change', window._odiResetPanel)" in html


def test_reset_does_not_hide_the_whole_card():
    """_odiResetPanel must hide only result sub-sections, NEVER the card
    container (odi-val-card) -- else the whole ODI card vanishes on file-select."""
    html = (_ROOT / "app" / "templates" / "mappings.html").read_text(encoding="utf-8")
    # the reset hide-list (the array fed to forEach(... style.display='none'))
    # must not contain the card container id
    import re
    m = re.search(r"_odiResetPanel\s*=\s*function\(\)\s*\{(.*?)\};", html, re.S)
    assert m, "_odiResetPanel function not found"
    body = m.group(1)
    # check the actual hide-list ARRAY literal (the one fed to forEach that sets
    # display='none'), not surrounding comments.
    arr = re.search(r"\[([^\]]*)\]\.forEach\(id => \{[^}]*style\.display\s*=\s*'none'", body, re.S)
    assert arr, "hide-list array not found"
    assert "odi-val-card" not in arr.group(1), "reset must NOT hide odi-val-card (the whole card)"


def test_odi_compare_endpoint_target_defaults_blank():
    """The /scenario/compare endpoint must default target_schema/table to blank
    (auto-detect), never IKOROSTELEV/AVY_FACT_SIDE."""
    odi = (_ROOT / "app" / "routers" / "odi.py").read_text(encoding="utf-8")
    assert 'Query(default="IKOROSTELEV")' not in odi
    assert 'Query(default="AVY_FACT_SIDE")' not in odi


# ── Gate 1 (2026-06-03): MERGE inner-SELECT extraction + literal-NULL verdicts ─
# The OPEN/MERGE scenario exposed three Phase-1 bugs the operator demonstrated:
#   (1) first bare pass-through column captured the leading `select` keyword,
#   (2) a multi-line CASE...END captured only its tail `END`,
#   (3) literal NULL projections shown UNRESOLVABLE instead of MATCHED/MISMATCH.

def test_merge_inner_projection_extractor_no_keyword_leak_and_whole_case():
    """`_merge_inner_projection_map` must return the REAL inner-SELECT binding:
    no leading `select` keyword on the first column, the WHOLE multi-line CASE
    (not just `END`), and literal NULL surfaced as NULL.  Fixture-free, generic."""
    from app.sql_model.comparator import _merge_inner_projection_map
    merge_sql = (
        "merge into TGT_OWNER.T T\n"
        "using (\n"
        "  select COL_A, COL_B, COL_C, COL_D, IND_UPDATE from (\n"
        "    select   DIM_X.SRC_CD COL_A,\n"
        "      NULL COL_B,\n"
        "      case\n"
        "        when SRC.F = 'Y'\n"
        "        then 'W' else NULL\n"
        "      end COL_C,\n"
        "      LK1.LK_NM COL_D\n"
        "    from SRC SRC, DIM_X DIM_X, LK1 LK1\n"
        "  )\n"
        ") S on (T.COL_A = S.COL_A)\n"
        "when matched then update set COL_B = S.COL_B\n"
    )
    mp = _merge_inner_projection_map(merge_sql)
    # (1) first column: real binding, NOT the keyword 'select'
    assert mp.get("COL_A") == "DIM_X.SRC_CD", mp.get("COL_A")
    assert mp.get("COL_A", "").upper() != "SELECT"
    # (3) literal NULL surfaced as NULL (not dropped, not 'END')
    assert mp.get("COL_B", "").upper() == "NULL", mp.get("COL_B")
    # (2) multi-line CASE captured whole, NOT just the tail 'END'
    case_expr = (mp.get("COL_C") or "").upper()
    assert case_expr.startswith("CASE") and case_expr.endswith("END"), mp.get("COL_C")
    assert case_expr != "END"
    # normal qualified ref still resolves
    assert mp.get("COL_D") == "LK1.LK_NM", mp.get("COL_D")


# ── OPEN (DRD .xlsx) parser/structural -- DRD-independent (ODI binding) ───────

def test_open_drd_src_stm_cd_shows_real_binding_not_select_keyword():
    """SRC_STM_CD ODI logic = the real inner binding, never the leaked `select`."""
    rows, _ = _compare(_OPEN_XML, _OPEN_DRD)
    r = rows["SRC_STM_CD"]
    assert (r.get("odi_logic") or "").strip().upper() != "SELECT", r.get("odi_logic")
    assert "SRC_STM_DIM.SRC_STM_CD" in (r.get("odi_logic") or "")


def test_open_drd_wash_sale_tp_captures_whole_case_not_end():
    """WASH_SALE_TP ODI logic must be the whole CASE, never the bare tail `END`."""
    rows, _ = _compare(_OPEN_XML, _OPEN_DRD)
    odi = (rows["WASH_SALE_TP"].get("odi_logic") or "").strip().upper()
    assert odi != "END", odi
    assert "CASE" in odi


def test_open_drd_no_row_resolves_to_a_bare_sql_keyword():
    """Generic guard: no ODI logic may be a bare SQL keyword (extraction artifact)."""
    rows, _ = _compare(_OPEN_XML, _OPEN_DRD)
    bad = {"SELECT", "END", "FROM", "WHERE", "USING", "MERGE"}
    offenders = {
        c: r.get("odi_logic")
        for c, r in rows.items()
        if (r.get("odi_logic") or "").strip().upper() in bad
    }
    assert not offenders, f"ODI logic resolved to bare SQL keywords: {offenders}"


# ── Phase 3a verdicts -- the CLOSE DRD .xlsx carries the rich transformation
#    rules ("populate as", "Use value-", "Always NULL", lookups, audit). ───────

def test_close_constant_rule_matches_odi_literal():
    """3a-A: DRD 'Use value- Closed' + ODI literal 'Closed' -> MATCHED (no DB)."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    r = rows["POS_CLS_TP"]
    assert r["verdict"] == "MATCHED", (r.get("drd_logic"), r.get("odi_logic"), r["verdict"])


def test_close_always_null_rule_matches_odi_null():
    """3a: DRD 'Always NULL' + ODI literal NULL -> MATCHED (both intend null)."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    for col in ("OPN_TXN_EV_TP", "ORIG_EV_TP", "ORIG_TXN_TP_CD"):
        r = rows[col]
        assert (r.get("odi_logic") or "").strip().upper() == "NULL", (col, r.get("odi_logic"))
        assert r["verdict"] == "MATCHED", (col, r.get("drd_logic"), r["verdict"])


def test_close_odi_null_vs_real_derivation_is_mismatch():
    """3a-C: ODI hardcodes NULL but DRD names a real derivation (no null-mandate)
    -> REAL_MISMATCH (was UNRESOLVABLE on the staging-step path)."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    r = rows["LOSS_NOT_ALWD_F"]
    assert (r.get("odi_logic") or "").strip().upper() == "NULL"
    assert r["verdict"] == "REAL_MISMATCH", (r.get("drd_logic"), r["verdict"])


def test_close_lookup_rule_stays_unresolvable_for_db_review():
    """3a Category B: a fixed-key lookup ('use SRC_STM_ID as 6 and get code/name')
    needs DB verification -> stays UNRESOLVABLE, NOT auto-matched."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    for col in ("SRC_STM_CD", "SRC_STM_NM"):
        assert rows[col]["verdict"] == "UNRESOLVABLE", (col, rows[col]["verdict"])


def test_close_audit_and_typod_rules_stay_unresolvable():
    """Category D (audit: SYSDATE / #GLOBAL session) + a typo'd rule
    ('popualte as 6') are NOT 100%-certain -> left UNRESOLVABLE for operator
    review (operator rule: leave <100%-certain cases reviewable, do not auto-fix)."""
    rows, _ = _compare(_CLOSE_XML, _CLOSE_DRD)
    for col in ("SRC_STM_ID", "SESN_NUM", "CRT_DTM"):
        assert rows[col]["verdict"] == "UNRESOLVABLE", (col, rows[col]["verdict"])


def test_extract_drd_constant_unit():
    """3a constant extractor: plain constants parse; lookups return None (Category B)."""
    from app.sql_model.comparator import _extract_drd_constant
    assert _extract_drd_constant("populate as 6") == "6"
    assert _extract_drd_constant("Use value- Closed") == "CLOSED"
    assert _extract_drd_constant("Always NULL") == "NULL"
    assert _extract_drd_constant("default 'X'") == "X"
    assert _extract_drd_constant("Use SRC_STM_ID as 6 and get code") is None
    assert _extract_drd_constant("look up CL_VAL.CL_VAL_NM") is None
    assert _extract_drd_constant("SBC_WASH_SALE_AMT") is None


# ── Gate 2 (2026-06-03): faithful INSERT emit for MERGE + Simple-Insert ───────
# Operator: CLOSE INSERT was "very strange" + the first staging step carried a
# hard-coded `SSDS_AVY_FACT_STEP1_STG` name even though CLOSE is not an AVY
# scenario.  Simple-Insert and MERGE-only now each emit a faithful direct INSERT
# (no CTE wrap, no AVY name); the AVY multi-step path keeps its WITH-CTE form.

def _emit(xml_path: Path):
    from app.sql_model.odi_parser import OdiXmlParser
    from app.sql_model.sql_emitter import emit_insert
    m = OdiXmlParser(target_schema="", target_table="").parse_text(
        xml_path.read_text(encoding="ISO-8859-1")
    )
    return emit_insert(m, strict=False), m


def test_close_simple_insert_is_faithful_no_avy_name_no_cte():
    """CLOSE (Simple-Insert IKM) emits a direct INSERT INTO target (...) SELECT
    ... FROM ... -- NOT wrapped in a CTE, and NEVER named SSDS_AVY_FACT_STEP*."""
    res, m = _emit(_CLOSE_XML)
    sql = res.sql
    assert "SSDS_AVY" not in sql, "Simple-Insert emit still leaks the AVY staging name"
    assert "WITH SSDS_AVY" not in sql and "\nWITH " not in sql, "should not CTE-wrap a Simple-Insert"
    assert f"INSERT INTO {m.target.fq}" in sql
    assert "Simple-Insert (faithful)" in sql
    # faithful = the real joins are present, not a placeholder
    assert "FROM" in sql.upper() and "JOIN" in sql.upper()


def test_open_merge_only_emits_faithful_insert_from_using():
    """OPEN (MERGE-only IKM) previously emitted NOTHING ('no staging steps').
    It must now emit INSERT INTO target (...) SELECT <inner bindings> FROM
    <inner joins>, with the real bindings (not S.<col> pass-throughs)."""
    res, m = _emit(_OPEN_XML)
    sql = res.sql
    assert "SSDS_AVY" not in sql
    assert "no staging steps" not in sql, "MERGE-only must emit a real INSERT now"
    assert f"INSERT INTO {m.target.fq}" in sql
    assert "MERGE (faithful column-mapping, from USING)" in sql
    # real inner bindings present (not the S.<col> WHEN-MATCHED alias)
    assert "SRC_STM_DIM.SRC_STM_CD" in sql
    assert "FROM" in sql.upper() and "JOIN" in sql.upper()


def test_merge_emit_surfaces_upsert_semantics_caveat():
    """Gate-2 finalize: a MERGE (WHEN MATCHED + WHEN NOT MATCHED) INSERT must
    NOT be presented as a silent 'faithful/OK' row-replay.  The EmitResult must
    carry a warning AND the header must print a CAVEAT line, because the INSERT
    reproduces ALL source rows, not only the unmatched ones."""
    res, _ = _emit(_OPEN_XML)
    assert res.warnings, "MERGE emit must surface the upsert-semantics caveat in warnings"
    assert any("WHEN MATCHED" in w or "only INSERTs unmatched" in w for w in res.warnings), res.warnings
    assert "-- CAVEAT:" in res.sql, "header must print the upsert caveat"


def test_simple_insert_emit_has_no_spurious_caveat():
    """A Simple-Insert IKM has no MERGE matched/unmatched split -> no upsert
    caveat should be emitted (only the real arity/unresolved warnings, if any)."""
    res, _ = _emit(_CLOSE_XML)
    assert "-- CAVEAT:" not in res.sql
    assert not any("WHEN MATCHED" in w for w in res.warnings)


def test_sql_emitter_is_ascii_only_in_strings():
    """Windows-ASCII rule: the emitter's console-reachable strings (EmitError /
    header / warnings) must be ASCII -- no em-dash etc."""
    src = (_ROOT / "app" / "sql_model" / "sql_emitter.py").read_text(encoding="utf-8")
    bad = sorted({ch for ch in src if ord(ch) > 127})
    assert not bad, f"non-ASCII chars in sql_emitter.py: {[hex(ord(c)) for c in bad]}"


def test_merge_projection_map_is_cached_and_consistent():
    """Gate-2 finalize: the projection parse is lru-cached (thread-safe) and
    returns a fresh, equal dict each call (callers may treat it as their own)."""
    from app.sql_model.comparator import (
        _merge_inner_projection_map, _merge_inner_projection_items,
    )
    fs = (_OPEN_XML.read_text(encoding="ISO-8859-1"))
    # the underlying cached parse must return the SAME tuple object (cache hit)
    from app.sql_model.odi_parser import OdiXmlParser
    m = OdiXmlParser(target_schema="", target_table="").parse_text(fs)
    merge_sql = m.final_select_sql or ""
    a = _merge_inner_projection_items(merge_sql)
    b = _merge_inner_projection_items(merge_sql)
    assert a is b, "lru_cache should return the same cached tuple"
    d1 = _merge_inner_projection_map(merge_sql)
    d2 = _merge_inner_projection_map(merge_sql)
    assert d1 == d2 and d1 is not d2, "map must be a fresh dict each call, equal content"
    assert d1.get("SRC_STM_CD") == "SRC_STM_DIM.SRC_STM_CD"


def test_merge_only_insert_column_count_matches_target_list():
    """The MERGE INSERT column list == the WHEN NOT MATCHED INSERT columns."""
    res, m = _emit(_OPEN_XML)
    # every final_insert_column appears in the emitted column list
    for col in m.final_insert_columns:
        assert col in res.sql, f"{col} missing from MERGE INSERT"


def test_avy_emit_still_uses_with_cte_not_regressed():
    """The AVY multi-step path must keep its WITH-CTE shape (Gate 2 must not
    regress it).  AVY genuinely names its staging steps SSDS_AVY_FACT_STEP*."""
    if not _AVY_XML.exists():
        pytest.skip("AVY fixture missing")
    res, m = _emit(_AVY_XML)
    sql = res.sql
    assert sql.lstrip().startswith("--") or "WITH " in sql
    # AVY has staging steps -> CTE form (the SSDS_AVY name is correct HERE)
    assert len(m.staging_steps) >= 1
    assert "WITH " in sql, "AVY multi-step emit must keep the WITH-CTE form"
