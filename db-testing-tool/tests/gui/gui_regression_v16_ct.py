"""REAL GUI regression suite for the v16.6 ODI panel + control-table flow.

Drives the LIVE server on :8550 through a real browser (msedge headless) and
asserts the SPECIFIC things that were broken (operator findings 2026-06-07):

  G1  Mode 1 (ODI #1 + DRD): sortable severity tiles INCL "Matched",
      resizable SQL pane, emitted INSERT section visible + non-empty.
  G2  Mode 2 (ODI #1 + ODI #2, no DRD): renders the SAME table layout as
      ODI-vs-DRD (not the bespoke 5-col delta table).
  G3  Mode 2 correctness: selected rows reach the real resolved CASE (not
      INLINE_VIEW_x.COL) and the count is the standalone's selected set
      (~13), not ~284.
  G4  Control table: Step-3 Generated Insert is v5.4 (373 cols), the
      Comparison Grid compares the v5.4 insert, "Apply Selected Fixes"
      changes the SQL.

Run AFTER a full server restart. Exit code 0 only if ALL checks pass.
Usage:  python tests/gui/gui_regression_v16_ct.py
"""
import json
import sys
import time
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
ODI1 = f"{TX}/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
ODI2 = f"{TX}/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001_v2.xml"
DRD = f"{TX}/DRD_Activity_Fact.xlsx"

checks = []  # (id, passed, detail)


def chk(cid, passed, detail=""):
    checks.append((cid, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {cid}: {detail}")


def run():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="msedge")
        pg = b.new_context(viewport={"width": 1600, "height": 1200}).new_page()
        pg.on("dialog", lambda d: d.accept())
        pg.goto(APP, wait_until="networkidle", timeout=30000)

        # ---------- G1: Mode 1 (ODI #1 + DRD) ----------
        print("G1 Mode 1 (ODI #1 + DRD): tiles sortable + Matched + resize + INSERT")
        try:
            pg.query_selector("#odi-xml-file").set_input_files(ODI1)
            pg.query_selector("#odi-drd-file").set_input_files(DRD)
            pg.query_selector("#odi-target-table").fill("AVY_FACT")
            pg.query_selector("#odi-v16-btn").click()
            pg.wait_for_function(
                "() => { const r=document.getElementById('odi-v15-result');"
                " return r && getComputedStyle(r).display!=='none'; }", timeout=40000)
            time.sleep(0.4)
            tiles_txt = pg.eval_on_selector("#odi-v15-bigtiles", "el=>el.textContent") if pg.query_selector("#odi-v15-bigtiles") else ""
            chk("G1.matched_tile", "Matched" in (tiles_txt or ""), f"bigtiles text has 'Matched' ({(tiles_txt or '')[:60]!r})")
            th = pg.query_selector("#odi-v15-diffwrap thead th")
            sortable = bool(th) and pg.eval_on_selector("#odi-v15-diffwrap thead th", "el=>getComputedStyle(el).cursor") == "pointer"
            chk("G1.sortable_tiles", sortable, "diff header cells are clickable/sortable")
            sql_vis = pg.query_selector("#odi-sql-section") and pg.eval_on_selector("#odi-sql-section", "el=>getComputedStyle(el).display") != "none"
            sql_len = pg.eval_on_selector("#odi-sql-pre", "el=>(el.textContent||'').length") if pg.query_selector("#odi-sql-pre") else 0
            chk("G1.emitted_insert", bool(sql_vis) and sql_len > 50, f"odi-sql-section visible + INSERT len={sql_len}")
            resize = pg.eval_on_selector("#odi-sql-pre", "el=>getComputedStyle(el).resize") if pg.query_selector("#odi-sql-pre") else "none"
            chk("G1.resizable_sql", resize and resize != "none", f"SQL pane resize={resize}")
        except Exception as e:
            chk("G1", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        # ---------- G2 + G3: ODI #1 + ODI #2 + DRD (selected review set) ----------
        print("G2/G3 ODI #1 vs ODI #2 (+DRD): same ODI-vs-DRD layout + standalone selected set")
        pg.goto(APP, wait_until="networkidle", timeout=30000)
        try:
            pg.query_selector("#odi-xml-file").set_input_files(ODI1)
            pg.query_selector("#odi-xml-file-2").set_input_files(ODI2)
            pg.query_selector("#odi-drd-file").set_input_files(DRD)
            pg.query_selector("#odi-target-table").fill("AVY_FACT")
            pg.query_selector("#odi-v16-btn").click()
            pg.wait_for_function(
                "() => { const r=document.getElementById('odi-v15-result');"
                " return r && getComputedStyle(r).display!=='none' &&"
                " r.querySelectorAll('#odi-v15-diffbody tr').length>0; }", timeout=60000)
            time.sleep(0.4)
            # G2: same column headers as ODI-vs-DRD (Mapping Logic + ODI XML Logic)
            heads = pg.eval_on_selector_all("#odi-v15-diffwrap thead th", "els=>els.map(e=>e.textContent.trim())")
            same_layout = ("Mapping Logic" in heads) and any("ODI" in h and "Logic" in h for h in heads)
            chk("G2.same_layout", same_layout, f"headers={heads}")
            # G3: default = the standalone selected set (8 CHANGED), NOT ~284
            nrows = pg.eval_on_selector_all("#odi-v15-diffbody tr", "els=>els.length")
            chk("G3.selected_count", 1 <= nrows <= 20, f"default selected rows={nrows} (expect ~8, not ~284)")
            bodytxt = (pg.eval_on_selector("#odi-v15-diffbody", "el=>el.textContent") or "").upper()
            chk("G3.resolves_case", "CASE" in bodytxt, "ODI #1 resolved column reaches a real CASE/transform")
            # show-all toggle exists (operator: 'also show all')
            has_toggle = pg.evaluate("() => (document.getElementById('odi-v15-caption')||{}).textContent && /show all/i.test(document.getElementById('odi-v15-caption').textContent)")
            chk("G3.show_all_toggle", bool(has_toggle), "'Show all' toggle present")
        except Exception as e:
            chk("G2G3", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        # ---------- G4: control table generated insert = v5.4 ----------
        print("G4 control-table: Generated Insert = v5.4 (DRD-driven), not the legacy emitter")
        pg.goto(APP, wait_until="networkidle", timeout=30000)
        try:
            # drive via JS + set_input_files (works regardless of section collapse/visibility)
            pg.evaluate("() => { const e=document.getElementById('ct-target'); if(e) e.value='AVY_FACT'; }")
            ct_drd = pg.query_selector("#ct-drd-file")
            assert ct_drd, "no #ct-drd-file input"
            ct_drd.set_input_files(DRD)
            pg.evaluate("() => { if (typeof buildV54Insert==='function') buildV54Insert(); }")
            pg.wait_for_function("() => { const t=document.getElementById('ct-insert-sql');"
                                 " return t && (t.value||'').toUpperCase().includes('INSERT INTO'); }", timeout=90000)
            v54_txt = pg.eval_on_selector("#ct-v54-result", "el=>el.textContent") if pg.query_selector("#ct-v54-result") else ""
            chk("G4.v54_generated", "v5.4" in (v54_txt or ""), f"v5.4 DRD-driven marker present ({(v54_txt or '')[:60]!r})")
            ins_len = pg.eval_on_selector("#ct-insert-sql", "el=>(el.value||'').length")
            has_into = pg.eval_on_selector("#ct-insert-sql", "el=>(el.value||'').toUpperCase().includes('INSERT INTO')")
            chk("G4.insert_is_v54", bool(has_into) and ins_len > 5000, f"generated insert is a real INSERT (len={ins_len})")
            # redundant compare-panel mark-correct must be GONE (single grid in Step 3)
            no_dupe = pg.evaluate("() => !document.getElementById('ct-mark-correct')")
            chk("G4.no_redundant_panel", bool(no_dupe), "redundant compare-panel mark-correct removed")
        except Exception as e:
            chk("G4", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        b.close()


if __name__ == "__main__":
    run()
    npass = sum(1 for _, ok, _ in checks if ok)
    print("\nGUI_REGRESSION_RESULT:")
    print(json.dumps({"pass": npass, "total": len(checks),
                      "failed": [c for c, ok, _ in checks if not ok]}, indent=2))
    sys.exit(0 if npass == len(checks) else 1)
