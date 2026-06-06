"""Step-2 (v3) proof: distinct tile colors, Missing filters to its own rows,
fullscreen toggle, SQL restored to its visible position."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
SS = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/e2e_screenshots")
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
AVY_XML = f"{TX}/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
AVY_DRD = f"{TX}/DRD_Activity_Fact.xlsx"
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1500, "height": 1150}).new_page()
    pg.goto(APP, wait_until="networkidle", timeout=30000)

    out["card_resize"] = pg.eval_on_selector("#odi-val-card", "el=>getComputedStyle(el).resize")
    out["sql_after_grid"] = pg.evaluate(
        "(()=>{const g=document.getElementById('odi-grid-section'),s=document.getElementById('odi-sql-section');"
        "return !!(g.compareDocumentPosition(s)&Node.DOCUMENT_POSITION_FOLLOWING);})()")

    pg.query_selector("#odi-xml-file").set_input_files(AVY_XML)
    pg.query_selector("#odi-drd-file").set_input_files(AVY_DRD)
    pg.query_selector("#odi-v15-btn").click()
    pg.wait_for_selector("#odi-v15-result", state="visible", timeout=60000)
    time.sleep(1.2)

    # tiles: label + count + color
    out["tiles"] = pg.eval_on_selector_all("#odi-v15-bigtiles .v15-bigtile",
        "els=>els.map(e=>({t:e.innerText.replace(/\\s+/g,' ').trim(), c:getComputedStyle(e.querySelector('div')).color}))")
    pg.query_selector("#odi-v15-result").scroll_into_view_if_needed(); time.sleep(0.3)
    pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2c_tiles_colors.png"))

    # click Missing -> should show ONLY missing rows (not all 14)
    miss = pg.query_selector("#odi-v15-bigtiles .v15-bigtile[data-v15sev='missing']")
    if miss:
        miss.click(); time.sleep(0.7)
        out["missing_shown"] = pg.query_selector("#odi-v15-shown").inner_text()
        out["missing_rows"] = pg.eval_on_selector_all("#odi-v15-diffbody tr", "els=>els.length")
        pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2c_missing_filter.png"))

    # fullscreen toggle
    pg.query_selector("#odi-val-fs").click(); time.sleep(0.5)
    out["fs_on"] = pg.eval_on_selector("#odi-val-card", "el=>el.classList.contains('odi-fs')")
    pg.screenshot(path=str(SS / "step2c_fullscreen.png"))
    pg.query_selector("#odi-val-fs").click(); time.sleep(0.3)
    out["fs_off"] = pg.eval_on_selector("#odi-val-card", "el=>el.classList.contains('odi-fs')")
    b.close()
print("RESULT:", out)
for f in sorted(SS.glob("step2c_*.png")):
    print("  shot:", f)
