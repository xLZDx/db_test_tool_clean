"""Diagnose: did SQL move break? collapse? resize? (read-only probe)"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
AVY_XML = f"{TX}/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
AVY_DRD = f"{TX}/DRD_Activity_Fact.xlsx"
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1500, "height": 1100}).new_page()
    pg.goto(APP, wait_until="networkidle", timeout=30000)

    # card / body resize + collapse mechanics
    out["card_resize"] = pg.eval_on_selector("#odi-val-card", "el=>getComputedStyle(el).resize")
    out["body_resize"] = pg.eval_on_selector("#odi-val-body", "el=>getComputedStyle(el).resize")
    out["sql_parent_at_load"] = pg.eval_on_selector("#odi-sql-section", "el=>el.parentElement && el.parentElement.id")
    out["sql_in_grid"] = pg.evaluate(
        "(()=>{const g=document.getElementById('odi-grid-section'),s=document.getElementById('odi-sql-section');"
        "return !!(g&&s&&g.contains(s));})()")

    # collapse: click header, check body display
    pg.eval_on_selector("#odi-val-card .card-header", "el=>el.click()")
    time.sleep(0.3)
    out["body_display_after_collapse"] = pg.eval_on_selector("#odi-val-body", "el=>getComputedStyle(el).display")
    pg.eval_on_selector("#odi-val-card .card-header", "el=>el.click()")
    time.sleep(0.3)
    out["body_display_after_expand"] = pg.eval_on_selector("#odi-val-body", "el=>getComputedStyle(el).display")

    # run Analyze -> does SQL show + where (below grid)?
    pg.query_selector("#odi-xml-file").set_input_files(AVY_XML)
    pg.query_selector("#odi-drd-file").set_input_files(AVY_DRD)
    pg.query_selector("#odi-analyze-btn").click()
    time.sleep(6)
    out["sql_display_after_analyze"] = pg.eval_on_selector("#odi-sql-section", "el=>getComputedStyle(el).display")
    out["sql_y"] = pg.eval_on_selector("#odi-sql-section", "el=>Math.round(el.getBoundingClientRect().top)")
    out["grid_y"] = pg.eval_on_selector("#odi-grid-section", "el=>Math.round(el.getBoundingClientRect().top)")
    out["sql_pre_len"] = pg.eval_on_selector("#odi-sql-pre", "el=>(el.textContent||'').length")
    b.close()
print("PROBE:", out)
