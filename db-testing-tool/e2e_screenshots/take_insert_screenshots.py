"""
Take screenshots of Insert SQL tab after generation - shows the actual INSERT SQL panel.
"""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

SCREENSHOTS_DIR = Path(__file__).parent
BASE_URL = "http://127.0.0.1:8550"

FIXTURES = [
    {
        "name": "CLOSE",
        "drd_file": str(Path(__file__).parent.parent / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"),
        "target": "TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT",
        "screenshot": str(SCREENSHOTS_DIR / "close_insert_tab.png"),
    },
    {
        "name": "OPEN",
        "drd_file": str(Path(__file__).parent.parent / "data" / "taxlot" / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"),
        "target": "TAXLOT_OWNER.OPN_TAX_LOTS_NON_BKR_FACT",
        "screenshot": str(SCREENSHOTS_DIR / "open_insert_tab.png"),
    },
    {
        "name": "AVY",
        "drd_file": str(Path(__file__).parent.parent / "DRD_Activity_Fact.xlsx"),
        "target": "TRANSACTIONS_OWNER.AVY_FACT_SIDE",
        "screenshot": str(SCREENSHOTS_DIR / "avy_insert_tab.png"),
    },
]

DATASOURCE_ID = "2"


def run_fixture(page, fixture):
    name = fixture["name"]
    print(f"\n=== {name} fixture (insert tab) ===")

    page.goto(f"{BASE_URL}/mappings", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    page.click("button:has-text('Control Table Tests')")
    page.wait_for_selector("#modal-control-table", state="visible", timeout=10000)
    page.wait_for_timeout(800)

    file_input = page.locator("#ct-drd-file")
    file_input.set_input_files(fixture["drd_file"])
    page.wait_for_timeout(1200)

    target_field = page.locator("#ct-target")
    target_field.fill(fixture["target"])

    src_ds = page.locator("#ct-source-ds")
    src_ds.select_option(value=DATASOURCE_ID)
    page.wait_for_timeout(300)

    page.click("button:has-text('Extract DRD')")
    page.wait_for_timeout(2000)

    page.click("button:has-text('Generate Insert Statement and Tests')")

    try:
        page.wait_for_selector("#ct-output:visible", timeout=90000)
        page.wait_for_timeout(2000)
    except Exception as e:
        print(f"  TIMEOUT: {e}")
        page.screenshot(path=fixture["screenshot"])
        return

    # Click the Insert SQL tab to show the INSERT SQL pane
    insert_tab_btn = page.locator("#ct-tab-btn-insert")
    if insert_tab_btn.count() > 0:
        insert_tab_btn.click()
        page.wait_for_timeout(800)

    page.screenshot(path=fixture["screenshot"], full_page=False)
    print(f"  Screenshot saved: {fixture['screenshot']}")

    # Check INSERT value
    insert_sql_el = page.locator("#ct-insert-sql")
    if insert_sql_el.count() > 0:
        val = insert_sql_el.input_value()
        print(f"  INSERT first 150: {val[:150]}")
        print(f"  INSERT present: {bool(val.strip())}")


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda msg: None)

        for fixture in FIXTURES:
            run_fixture(page, fixture)

        browser.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
