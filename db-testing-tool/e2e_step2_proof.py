"""Step-2 GUI proof: v15 dynamic per-type tiles + click-filter + sort (AVY)."""
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
    pg.query_selector("#odi-xml-file").set_input_files(AVY_XML)
    pg.query_selector("#odi-drd-file").set_input_files(AVY_DRD)
    pg.query_selector("#odi-v15-btn").click()
    pg.wait_for_selector("#odi-v15-result", state="visible", timeout=60000)
    time.sleep(1.5)

    out["tiles"] = pg.eval_on_selector_all(
        "#odi-v15-typetiles .v15-tile", "els=>els.map(e=>e.innerText.replace(/\\s+/g,' ').trim())")
    out["rows_all"] = pg.eval_on_selector_all("#odi-v15-diffbody tr", "els=>els.length")
    pg.query_selector("#odi-v15-result").scroll_into_view_if_needed()
    time.sleep(0.3)
    pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2_v15_tiles_all.png"))

    # click first real type tile (index 1, after the All tile) -> filter
    tnodes = pg.query_selector_all("#odi-v15-typetiles .v15-tile")
    if len(tnodes) > 1:
        out["clicked_tile"] = tnodes[1].inner_text().replace("\n", " ").strip()
        tnodes[1].click()
        time.sleep(0.8)
        out["rows_filtered"] = pg.eval_on_selector_all("#odi-v15-diffbody tr", "els=>els.length")
        out["shown_text"] = pg.query_selector("#odi-v15-shown").inner_text()
        pg.query_selector("#odi-v15-result").screenshot(path=str(SS / "step2_v15_tiles_filtered.png"))

    b.close()

print("RESULT:", out)
for f in sorted(SS.glob("step2_*.png")):
    print("  shot:", f)
