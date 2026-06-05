"""
E2E screenshots for Control Table Tests Generator - 3 DRD fixtures.
Uses Python Playwright to drive the real browser GUI.
"""
import os, time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCREENSHOTS_DIR = Path(__file__).parent
BASE_URL = "http://127.0.0.1:8550"

FIXTURES = [
    {
        "name": "CLOSE",
        "drd_file": str(Path(__file__).parent.parent / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"),
        "target": "TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT",
        "screenshot": str(SCREENSHOTS_DIR / "close_fixture_result.png"),
    },
    {
        "name": "OPEN",
        "drd_file": str(Path(__file__).parent.parent / "data" / "taxlot" / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"),
        "target": "TAXLOT_OWNER.OPN_TAX_LOTS_NON_BKR_FACT",
        "screenshot": str(SCREENSHOTS_DIR / "open_fixture_result.png"),
    },
    {
        "name": "AVY",
        "drd_file": str(Path(__file__).parent.parent / "DRD_Activity_Fact.xlsx"),
        "target": "TRANSACTIONS_OWNER.AVY_FACT_SIDE",
        "screenshot": str(SCREENSHOTS_DIR / "avy_fixture_result.png"),
    },
]

DATASOURCE_ID = "2"

def run_fixture(page, fixture):
    name = fixture["name"]
    print(f"\n=== {name} fixture ===")

    # Navigate fresh each time
    page.goto(f"{BASE_URL}/mappings", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # Open the Control Table modal
    page.click("button:has-text('Control Table Tests')")
    page.wait_for_selector("#modal-control-table", state="visible", timeout=10000)
    page.wait_for_timeout(800)

    # Upload DRD file
    file_input = page.locator("#ct-drd-file")
    file_input.set_input_files(fixture["drd_file"])
    page.wait_for_timeout(1200)

    # Set target SCHEMA.TABLE
    target_field = page.locator("#ct-target")
    target_field.fill(fixture["target"])

    # Set source datasource
    src_ds = page.locator("#ct-source-ds")
    src_ds.select_option(value=DATASOURCE_ID)
    page.wait_for_timeout(300)

    # Click "Extract DRD" (previewControlTableDrd) - Step 1 optional but helpful
    page.click("button:has-text('Extract DRD')")
    page.wait_for_timeout(2000)

    # Screenshot after Step 1
    page.screenshot(path=str(SCREENSHOTS_DIR / f"{name.lower()}_step1.png"), full_page=False)
    print(f"  Step 1 screenshot saved")

    # Click "Generate Insert Statement and Tests" (Step 2)
    page.click("button:has-text('Generate Insert Statement and Tests')")

    # Wait for ct-output to appear (up to 60s for large files)
    try:
        page.wait_for_selector("#ct-output:visible", timeout=90000)
        page.wait_for_timeout(2000)
        generated = True
    except Exception as e:
        print(f"  TIMEOUT waiting for ct-output: {e}")
        generated = False

    # Take final screenshot
    page.screenshot(path=fixture["screenshot"], full_page=False)
    print(f"  Result screenshot saved: {fixture['screenshot']}")

    # Check for toasts / errors
    toast_err = page.locator(".toast-error, .toast.error, [class*='toast'][class*='error']")
    toast_warning = page.locator(".toast-warning, .toast.warning")
    error_text = ""
    if toast_err.count() > 0:
        error_text = toast_err.first.inner_text()
        print(f"  ERROR TOAST: {error_text}")
    if toast_warning.count() > 0:
        warn_text = toast_warning.first.inner_text()
        print(f"  WARNING TOAST: {warn_text}")

    # Check insert SQL populated
    insert_sql = page.locator("#ct-insert-sql")
    insert_present = False
    insert_preview = ""
    if insert_sql.count() > 0:
        val = insert_sql.input_value() if insert_sql.count() else ""
        insert_present = bool(val and val.strip())
        insert_preview = val[:120] if val else ""

    # Check DDL populated
    ddl_sql = page.locator("#ct-ddl-sql")
    ddl_present = False
    if ddl_sql.count() > 0:
        val = ddl_sql.input_value()
        ddl_present = bool(val and val.strip())

    # Check for test list rendered in ct-gen-suite-body or ct-suite-tests
    suite_tests = page.locator("#ct-suite-tests, #ct-gen-suite-body, [id*='suite']")
    tests_rendered = suite_tests.count() > 0

    print(f"  ct-output visible: {generated}")
    print(f"  INSERT SQL present: {insert_present}")
    print(f"  INSERT preview: {insert_preview[:100]}")
    print(f"  DDL present: {ddl_present}")
    print(f"  error_text: '{error_text}'")

    return {
        "name": name,
        "generated": generated,
        "insert_present": insert_present,
        "ddl_present": ddl_present,
        "error_text": error_text,
        "screenshot": fixture["screenshot"],
    }


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        # Suppress console noise
        page.on("console", lambda msg: None)

        results = []
        for fixture in FIXTURES:
            result = run_fixture(page, fixture)
            results.append(result)

        browser.close()

    print("\n\n=== SUMMARY ===")
    for r in results:
        print(f"\n{r['name']}:")
        print(f"  Rendered OK: {r['generated']}")
        print(f"  INSERT present: {r['insert_present']}")
        print(f"  DDL present: {r['ddl_present']}")
        print(f"  Error text: '{r['error_text']}'")
        print(f"  Screenshot: {r['screenshot']}")


if __name__ == "__main__":
    main()
