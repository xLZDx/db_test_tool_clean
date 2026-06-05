"""R5 step 3 GUI proof -- DRD_UNDERSPECIFIED bucket renders in the generator
validation panel. Real browser (system Edge). AVY (ds=2) produces 95 neutralized
(ON 1=0) joins -> the panel must show 'DRD_UNDERSPECIFIED (95)'.
Asserts on #ct-validation-panel innerHTML (does not need the heavy 369-col grid).
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool")
TX = ROOT / "data" / "taxlot"
BASE = "http://127.0.0.1:8550"
SHOT = ROOT / "e2e_screenshots"
SHOT.mkdir(exist_ok=True)


def main():
    drd = TX / "DRD_Activity_Fact.xlsx"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1300})
        page.set_default_timeout(120000)
        page.goto(f"{BASE}/mappings", wait_until="networkidle")
        time.sleep(0.8)
        page.click("button[onclick=\"openControlTableModal()\"]")
        page.wait_for_selector("#modal-control-table", state="visible")
        page.wait_for_function(
            "document.querySelector('#ct-source-ds') && document.querySelector('#ct-source-ds').options.length > 0",
            timeout=30000)
        page.set_input_files("#ct-drd-file", str(drd))
        page.fill("#ct-target", "TRANSACTIONS_OWNER.AVY_FACT")
        page.select_option("#ct-source-ds", value="2")
        page.select_option("#ct-target-ds", value="2")
        time.sleep(0.6)
        page.click("button[onclick=\"createEmptyControlTableFromPdm()\"]")
        time.sleep(4.0)
        page.click("button[onclick=\"generateControlTableTests()\"]")
        # validation panel populates right after analyze returns
        page.wait_for_function(
            "() => { const p=document.getElementById('ct-validation-panel');"
            "return p && /DRD_UNDERSPECIFIED/.test(p.innerHTML); }",
            timeout=120000)
        html = page.eval_on_selector("#ct-validation-panel", "el => el.innerHTML")
        import re
        m = re.search(r"DRD_UNDERSPECIFIED \((\d+)\)", html)
        count = int(m.group(1)) if m else -1
        print("panel contains DRD_UNDERSPECIFIED:", "DRD_UNDERSPECIFIED" in html)
        print("rendered count:", count)
        page.screenshot(path=str(SHOT / "drd_underspecified_avy.png"), full_page=True)
        # authoritative check: the validation panel rendered from the live endpoint
        # response (res.drd_underspecified). The bucket count must equal the 95
        # neutralized AVY joins the endpoint returns for ds=2.
        ok = ("DRD_UNDERSPECIFIED" in html) and count == 95
        print("RESULT:", "PASS" if ok else "FAIL")
        browser.close()


if __name__ == "__main__":
    main()
