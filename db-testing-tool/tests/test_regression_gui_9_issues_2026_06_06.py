"""Regression suite for the 9 GUI issues reported by the operator on 2026-06-06.

Each test encodes the DESIRED (post-fix) behaviour and is marked
``xfail(strict=True)`` so that:

  * the normal suite stays GREEN (the bugs show up as ``xfailed``, not failures);
  * running ``pytest <thisfile> --runxfail`` reproduces every bug RED on demand
    (the operator asked for a suite that "detects all this by itself");
  * once a fix lands the strict marker flips the test to a hard failure
    (``XPASS``) until the ``xfail`` is removed -- so a silent fix cannot rot.

Detector strategy
-----------------
* BACKEND issues (#1, #9b) are real FastAPI ``TestClient`` calls -- functional
  proof of endpoint behaviour.
* FRONTEND/DOM issues (#2, #3, #5, #6, #7, #9a, #10) are source-contract
  assertions against the rendered ``mappings.html`` / service source. These are
  fast deterministic detectors; they are NOT a substitute for a real browser
  (Playwright) functional proof -- building that harness is a line item in the
  fix plan (PLAN_FIX_9_GUI_ISSUES_2026-06-06.md).

Evidence anchors verified at authoring time (2026-06-06):
  #1  /scenario/compare-v15 hardcodes profile="generic" (odi.py:1079) -> v15 keeps all
      262 raw rows (odi_drd_compare_v15.py:2258). Under profile="avy"/auto v15 returns
      the curated 14 review rows (build_avy_review_rules_diff:2251) = operator final_v15.
  #2  _odiRenderV15 writes only odi-v15-* (separate lesser panel); never the
      legacy 6 verdict tiles / sortable grid that Analyze (_odiRender) uses.
  #3  _odiResetPanel hides [odi-summary, odi-sql-section, odi-grid-section,
      odi-static-section, odi-multisheet-panel, odi-fix-panel, odi-xe-result] --
      NOT odi-v15-result -> stale v15 output lingers on new file.
  #5  buildV54Insert calls setCtSqlPaneView('ct-insert-sql','all') -> wrap height
      = full scrollHeight (~1565 lines) -> SQL terminal stretches the page.
  #6  control_table_service.analyze_control_table (the generator behind
      /control-table/analyze used by Steps 1-3) emits the OLD insert; v5.4
      (universal_insert_builder_v54) is wired ONLY to the opt-in /build-v54.
  #7  the Comparison-Grid action row has no inline ODI upload feeding
      compareControlTableStatements -> ODI column shows "(run ODI compare)".
  #9  the second SQL pane is labelled "Manual Insert Statement" (want
      "ODI XML Compare") and /build-v54 returns no ODI-derived clean SQL.
  #10 the DRD/ODI + 2-XML doc-compare card EXISTS (ct-sect-doc-compare, handler
      runControlTableDocCompare, route /control-table/compare-docs) but is buried
      INSIDE the Step-3 Comparison-Grid sub-tab below the up-to-369-row grid.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "app" / "templates" / "mappings.html"
CT_SVC = ROOT / "app" / "services" / "control_table_service.py"
TX = ROOT / "data" / "taxlot"
AVY_DRD = TX / "DRD_Activity_Fact.xlsx"
AVY_ODI = TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# v15 curated AVY review diff = 14 rows (operator final_v15 regression). The GUI
# endpoint currently forces profile="generic" -> 262 raw rows; auto/avy -> 14.
AVY_REVIEW_ROWS = 14

client = TestClient(app)
HTML = TPL.read_text(encoding="utf-8")

_AVY = pytest.mark.skipif(
    not (AVY_DRD.exists() and AVY_ODI.exists()), reason="AVY taxlot fixtures absent"
)


def _between(text: str, start: str, end: str) -> str:
    """Return the slice of ``text`` from ``start`` up to (not incl.) ``end``."""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i:j] if j > 0 else text[i:]


def _window(text: str, anchor: str, size: int = 6000) -> str:
    """Return a window of ``text`` starting at ``anchor`` (function/handler body)."""
    i = text.find(anchor)
    return text[i : i + size] if i >= 0 else ""


def _pyfunc(src: str, name: str) -> str:
    """Return a python function body: from ``def <name>`` to the next top-level def."""
    i = src.find(f"def {name}")
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 1)
    return src[i:j] if j > 0 else src[i:]


# -- Group A -- ODI vs DRD Validation: v15 must equal Analyze ------------------

@_AVY
def test_issue1_v15_returns_curated_review_rows():
    # FIXED 2026-06-06 (step 1): /scenario/compare-v15 now passes profile="auto"
    # (was hardcoded "generic" -> 262). De-xfailed -> permanent regression test.
    with AVY_DRD.open("rb") as fd, AVY_ODI.open("rb") as fo:
        r = client.post(
            "/api/odi/scenario/compare-v15",
            files={
                "xml_file": ("avy.xml", fo, "text/xml"),
                "drd_file": ("avy.xlsx", fd, MIME_XLSX),
            },
        )
    assert r.status_code == 200, r.text
    d = r.json()
    diffs = d.get("differences", [])
    # v15 DOES find these under avy/auto; the GUI forces generic -> 262. After the
    # one-line profile fix the endpoint returns the curated review rows.
    assert len(diffs) == AVY_REVIEW_ROWS, (
        f"v15 returned {len(diffs)} review rows; expected {AVY_REVIEW_ROWS} "
        f"(GUI forces profile=generic; pass auto)"
    )


@_AVY
def test_issue2_v15_dynamic_status_tiles_filter_sort():
    # FIXED step 2 (operator-revised #2): v15 keeps its OWN table style but gains dynamic
    # per-Difference-Type tiles (colored by severity bucket), click-to-filter, and sortable
    # headers -- NOT the v9 6-verdict screen. Backend exposes type_counts + per-row severity.
    with AVY_DRD.open("rb") as fd, AVY_ODI.open("rb") as fo:
        r = client.post(
            "/api/odi/scenario/compare-v15",
            files={"xml_file": ("avy.xml", fo, "text/xml"),
                   "drd_file": ("avy.xlsx", fd, MIME_XLSX)},
        )
    assert r.status_code == 200, r.text
    d = r.json()
    tc = d.get("type_counts")
    assert isinstance(tc, list) and tc, "endpoint must return non-empty type_counts"
    assert all({"type", "count", "severity"} <= set(t) for t in tc), "type_count needs type/count/severity"
    assert sum(t["count"] for t in tc) == len(d.get("differences", [])), "type_counts must cover every diff row"
    assert all(row.get("severity") for row in d.get("differences", [])), "every diff row needs a severity bucket"
    # frontend wiring: dynamic tiles container + click-filter + sort handlers
    assert "odi-v15-typetiles" in HTML, "missing dynamic type-tiles container"
    assert "v15FilterType" in HTML and "v15SortBy" in HTML, "missing v15 filter/sort wiring"


def test_issue3_reset_clears_v15_result():  # FIXED step 1 (de-xfailed)
    body = _window(HTML, "_odiResetPanel =", 1200)
    assert "odi-v15-result" in body, (
        "_odiResetPanel must hide odi-v15-result (and its tiles/diffbody) on new file"
    )


# -- Group B -- v5.4 builder GUI breakage -------------------------------------

def test_issue5_v54_does_not_force_show_all():  # FIXED step 1 (de-xfailed)
    body = _window(HTML, "window.buildV54Insert")
    forces_all = (
        "setCtSqlPaneView('ct-insert-sql', 'all')" in body
        or 'setCtSqlPaneView("ct-insert-sql", "all")' in body
    )
    assert not forces_all, (
        "buildV54Insert must not force the 'all' view (it stretches the SQL terminal)"
    )


@pytest.mark.xfail(strict=True, reason="Issue #6: the control-table generator "
                   "(analyze_control_table) still emits the OLD insert; v5.4 is "
                   "only the opt-in /build-v54 button and never reaches saved tests")
def test_issue6_generator_uses_v54_builder():
    # Slice the analyze_control_table BODY (not the whole file) so a top-of-file
    # dead import cannot satisfy this. (Functional proof -- assert the analyze
    # endpoint's generated_insert_sql carries the v5.4 header -- is deferred to
    # the Group E browser/TestClient harness, which needs a registered ds.)
    src = CT_SVC.read_text(encoding="utf-8")
    body = _pyfunc(src, "analyze_control_table")
    assert body, "analyze_control_table not found in control_table_service.py"
    assert ("build_to_dir" in body) or ("universal_insert_builder_v54" in body), (
        "the generator (analyze_control_table) does not call the v5.4 builder; "
        "v5.4 stays opt-in (/build-v54) and never reaches saved tests"
    )


# -- Group C -- Comparison Grid 3-way with ODI --------------------------------

@pytest.mark.xfail(strict=True, reason="Issue #7: the Comparison-Grid action row "
                   "has no inline ODI upload; the ODI column shows '(run ODI "
                   "compare)' and cannot do the 3-way compare in place")
def test_issue7_comparison_grid_has_inline_odi_upload():
    grid_actions = _between(HTML, '<div id="ct-tab-compare"', 'id="ct-compare-results"')
    assert 'type="file"' in grid_actions, (
        "the Comparison-Grid action row has no inline ODI file upload for the 3-way compare"
    )


# -- Group D -- ODI -> clean SQL into a renamed compare pane -------------------

def test_issue9a_manual_pane_renamed_to_odi_xml_compare():  # FIXED step 1 (de-xfailed)
    assert "ODI XML Compare" in HTML, (
        "the second SQL pane was not renamed to 'ODI XML Compare'"
    )


@_AVY
@pytest.mark.xfail(strict=True, reason="Issue #9: loading an ODI file via v5.4 must "
                   "reverse-engineer it to clean SQL for the 'ODI XML Compare' box; "
                   "/build-v54 returns no ODI-derived SQL today")
def test_issue9b_v54_returns_odi_derived_sql():
    with AVY_DRD.open("rb") as fd, AVY_ODI.open("rb") as fo:
        r = client.post(
            "/api/tests/control-table/build-v54",
            files={
                "drd_file": ("avy.xlsx", fd, MIME_XLSX),
                "odi_file": ("avy.xml", fo, "text/xml"),
            },
            data={"target_schema": "TRANSACTIONS_OWNER",
                  "target_table": "AVY_FACT", "profile": "avy"},
        )
    assert r.status_code == 200, r.text
    d = r.json()
    odi_sql = (d.get("odi_sql") or "")
    assert "SELECT" in odi_sql.upper(), (
        "build-v54 did not return ODI-derived clean SQL for the 'ODI XML Compare' box"
    )


# -- Group E -- restore (un-bury) the doc-compare card ------------------------

@pytest.mark.xfail(strict=True, reason="Issue #10: the DRD/ODI + 2-XML doc-compare "
                   "card EXISTS but is buried inside the Step-3 Comparison-Grid "
                   "sub-tab below the up-to-369-row grid; lift it to a standalone card")
def test_issue10_doc_compare_card_reachable_standalone():
    # LOUD NOTE: the card is NOT deleted -- ct-sect-doc-compare markup, the
    # runControlTableDocCompare handler, and the /control-table/compare-docs route
    # all exist. The bug is placement: it lives only inside ct-tab-compare. It
    # should be its own top-level CT card so it never disappears behind the grid.
    assert "ct-sect-doc-compare" in HTML, "doc-compare card markup must exist"
    tab_compare = _between(HTML, '<div id="ct-tab-compare"', '<div id="ct-tab-insert"')
    tab_insert = _between(HTML, '<div id="ct-tab-insert"', 'id="ct-sect-pdm"')
    assert "ct-sect-doc-compare" not in tab_compare, (
        "doc-compare card is buried inside the Comparison-Grid sub-tab; "
        "lift it to a standalone, always-reachable card"
    )
    assert "ct-sect-doc-compare" not in tab_insert, (
        "doc-compare card must not be buried inside the Insert-SQL sub-tab either"
    )


# -- Security (SEC-1, fixed step 1): /compare-v15 must reject a non-.xml xml_file ------

@pytest.mark.skipif(not AVY_DRD.exists(), reason="AVY DRD absent")
def test_sec1_compare_v15_rejects_non_xml():
    with AVY_DRD.open("rb") as fd:
        r = client.post(
            "/api/odi/scenario/compare-v15",
            files={
                "xml_file": ("not_xml.txt", b"<not really xml>", "text/plain"),
                "drd_file": ("avy.xlsx", fd, MIME_XLSX),
            },
        )
    assert r.status_code == 422, r.text
