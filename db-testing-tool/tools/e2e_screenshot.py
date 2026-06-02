"""Phase 7.19.13 E2E screenshot driver -- real GUI flow via system Edge.

Drives the Control Table Tests Generator end-to-end and captures
screenshots as proof:
  1. open dashboard /tfs
  2. open Control Table Tests modal
  3. upload DRD_Activity_Fact.xlsx, set target/source/DS/grain
  4. Create Empty Control Table (DDL) + Generate Insert Statement and Tests
  5. screenshot Step 3 comparison grid + Insert SQL

Output PNGs -> data/e2e_screenshots/.
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8553"
DRD = str(Path("DRD_Activity_Fact.xlsx").resolve())
OUT = Path("data/e2e_screenshots")
OUT.mkdir(parents=True, exist_ok=True)


def shot(page, name):
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"  screenshot -> {p}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1200})
        page.set_default_timeout(120000)

        print("1. open /tfs")
        page.goto(f"{BASE}/mappings", wait_until="networkidle")
        time.sleep(1.5)

        print("2. open Control Table Tests modal")
        page.click("button[onclick=\"openControlTableModal()\"]")
        page.wait_for_selector("#modal-control-table", state="visible")
        # DS selects are populated when the modal opens -- wait for options.
        page.wait_for_function(
            "document.querySelector('#ct-source-ds') && document.querySelector('#ct-source-ds').options.length > 0",
            timeout=30000,
        )
        time.sleep(0.5)

        print("3. fill fields + upload DRD")
        page.set_input_files("#ct-drd-file", DRD)
        page.fill("#ct-target", "TRANSACTIONS_OWNER.AVY_FACT_SIDE")
        page.fill("#ct-source-table", "ENTERPRISE_SEMANTIC_OWNER.AVY_FACT")
        page.fill("#ct-grain", "Refer to [ETL Notes] tab")
        # Select FREEPDB1_LOCAL (ds id 2) on both selects.
        page.select_option("#ct-source-ds", value="2")
        page.select_option("#ct-target-ds", value="2")
        time.sleep(1.0)
        shot(page, "01_step1_loaded")

        print("4a. Create Empty Control Table (DDL)")
        page.click("button[onclick=\"createEmptyControlTableFromPdm()\"]")
        time.sleep(5.0)

        print("4b. Generate Insert Statement and Tests")
        page.click("button[onclick=\"generateControlTableTests()\"]")
        # Wait for Step 3 output to become visible + comparison summary populated.
        page.wait_for_selector("#ct-output", state="visible", timeout=120000)
        page.wait_for_function(
            "(document.querySelector('#ct-compare-summary')||{}).textContent && "
            "document.querySelector('#ct-compare-summary').textContent.toLowerCase().includes('mismatch')",
            timeout=120000,
        )
        time.sleep(2.0)
        shot(page, "02_step3_comparison_grid")

        # Capture the comparison summary text as proof.
        summary = page.eval_on_selector("#ct-compare-summary", "el => el.textContent") if page.query_selector("#ct-compare-summary") else ""
        print(f"  COMPARISON SUMMARY: {summary.strip()[:200]}")

        # Switch to Insert SQL tab.
        print("5. Insert SQL view")
        try:
            page.click("button[onclick=\"setControlTableStep3Tab('insert')\"]")
            time.sleep(1.5)
            shot(page, "03_generated_insert_sql")
        except Exception as e:
            print(f"  (Insert SQL tab: {e})")

        browser.close()
        print("DONE")


if __name__ == "__main__":
    main()
