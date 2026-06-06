"""Step-2 (revised) proof: BIG Analyze-style v15 tiles (incl Matched), no purple frame,
severity filter, and Emitted SQL relocated below the grid."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
SS = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/e2e_screenshots")
SS.mkdir(parents=True, exist_ok=True)
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
AVY_XML = f"{TX}/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
AVY_DRD = f"{TX}/DRD_Activity_Fact.xlsx"

out = {}
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, channel="msedge")
    pg = b.new_context(viewport={"width": 1500, "height": 1150}).new_page()
    pg.goto(APP, wait_until="networkidle", timeout=30000)

    # Emitted SQL relocated below the grid? (DOM order: grid precedes sql)
    out["sql_after_grid"] = pg.evaluate(
        "(()=>{const s=document.getElementById('odi-sql-section'),g=document.getElementById('odi-grid-section');"
        "if(!s||!g)return 'missing';return !!(g.compareDocumentPosition(s)&Node.DOCUMENT_POSITION_FOLLOWING);})()")

    pg.query_selector("#odi-xml-file").set_input_files(AVY_XML)
    pg.query_selector("#odi-drd-file").set_input_files(AVY_DRD)
    pg.query_selector("#odi-v15-btn").click()
    pg.wait_for_selector("#odi-v15-result", state="visible", timeout=60000)
    time.sleep(1.5)

    out["big_tiles"] = pg.eval_on_selector_all(
        "#odi-v15-bigtiles .v15-bigtile", "els=>els.map(e=>e.innerText.replace(/\\s+/g,' ').trim())")
    out["purple_frame"] = pg.eval_on_selector(
        "#odi-v15-result", "el=>getComputedStyle(el).borderColor")
    pg.query_selector("#odi-v15-result").scroll_into_view_if_needed()
    time.sleep(0.3)
    pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2b_v15_big_tiles.png"))

    # click "Real gap" tile -> filter
    rg = pg.query_selector("#odi-v15-bigtiles .v15-bigtile[data-v15sev='real_gap']")
    if rg:
        rg.click()
        time.sleep(0.8)
        out["after_realgap_shown"] = pg.query_selector("#odi-v15-shown").inner_text()
        out["after_realgap_rows"] = pg.eval_on_selector_all("#odi-v15-diffbody tr", "els=>els.length")
        pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2b_v15_filtered_realgap.png"))

    b.close()

print("RESULT:", out)
for f in sorted(SS.glob("step2b_*.png")):
    print("  shot:", f)
