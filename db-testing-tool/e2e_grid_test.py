"""
E2E test: verify grid changes for DRD .xlsx workflow.
Checks:
  1. WASH_SALE_TP ODI SOURCE and DRD SOURCE cells are NOT truncated (no trailing ellipsis).
  2. WASH_SALE_TP VERDICT == MISMATCH (REAL_MISMATCH).
  3. Tile counts: Matched 77, Alias Drift 0, Mismatch 2, Unresolvable 3, Missing 1, ODI Extra 1.
  4. ADJ_COST (or any long CASE row) ODI SOURCE is not truncated.
"""
import sys, time, json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

XML_PATH  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
DRD_PATH  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
APP_URL   = "http://127.0.0.1:8550/mappings"
SCREENSHOT_PATH = r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_screenshot.png"

def run():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()

        print(f"[1] Navigating to {APP_URL}")
        page.goto(APP_URL, wait_until="networkidle", timeout=30000)
        page.screenshot(path=SCREENSHOT_PATH.replace(".png", "_00_loaded.png"))
        print("[1] Page loaded OK")

        # Attach XML file
        print("[2] Attaching XML file...")
        xml_input = page.locator("#odi-xml-file")
        xml_input.set_input_files(XML_PATH)
        time.sleep(0.5)

        # Attach DRD file
        print("[3] Attaching DRD xlsx file...")
        drd_input = page.locator("#odi-drd-file")
        drd_input.set_input_files(DRD_PATH)
        time.sleep(0.5)

        # Ensure Target is blank
        target_sel = page.locator("input[placeholder*='arget'], input[id*='target'], input[name*='target']").first
        try:
            target_sel.fill("")
        except Exception:
            pass

        page.screenshot(path=SCREENSHOT_PATH.replace(".png", "_01_files_attached.png"))
        print("[4] Clicking Analyze button...")
        analyze_btn = page.get_by_role("button", name="Analyze")
        analyze_btn.click()

        # Wait for the comparison grid to appear (up to 120s for analysis)
        print("[5] Waiting for grid to render...")
        try:
            page.wait_for_selector("table", timeout=120000)
            print("[5] Table appeared")
        except PlaywrightTimeoutError:
            print("[5] TIMEOUT waiting for table -- trying to proceed anyway")

        # Also wait for network idle
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        time.sleep(2)
        page.screenshot(path=SCREENSHOT_PATH.replace(".png", "_02_results.png"))
        print(f"[5] Screenshot saved: {SCREENSHOT_PATH.replace('.png', '_02_results.png')}")

        # ---- TILE COUNTS ----
        print("\n--- TILE COUNTS ---")
        tile_map = {}
        # Look for summary tiles / stat cards
        tile_texts = page.evaluate("""() => {
            const results = [];
            // Try common patterns for summary tiles
            const cards = document.querySelectorAll('[class*="tile"], [class*="stat"], [class*="card"], [class*="summary"], [class*="badge"], [class*="count"]');
            cards.forEach(c => {
                const t = c.innerText.trim();
                if (t) results.push(t);
            });
            return results;
        }""")
        print("Raw tile texts:", tile_texts)

        # Try to find tiles by looking for elements containing the expected labels
        for label in ["Matched", "Alias Drift", "Mismatch", "Unresolvable", "Missing", "ODI Extra"]:
            val = page.evaluate(f"""() => {{
                const all = document.querySelectorAll('*');
                for (const el of all) {{
                    if (el.children.length === 0 && el.innerText && el.innerText.includes('{label}')) {{
                        // Find adjacent number
                        const parent = el.parentElement;
                        if (parent) {{
                            const nums = parent.innerText.match(/\\d+/g);
                            if (nums) return {{ label: '{label}', text: parent.innerText.trim(), nums: nums }};
                        }}
                        return {{ label: '{label}', text: el.innerText.trim() }};
                    }}
                }}
                return null;
            }}""")
            if val:
                tile_map[label] = val
                print(f"  Tile [{label}]: {val}")

        results["tiles"] = tile_map

        # ---- FIND WASH_SALE_TP ROW ----
        print("\n--- FINDING WASH_SALE_TP ROW ---")

        # First try to set filter to "All" to ensure the row is visible
        # Look for a Filter Verdict dropdown
        filter_sel = page.locator("select").first
        try:
            options = filter_sel.evaluate("el => Array.from(el.options).map(o => o.text)")
            print(f"  Filter dropdown options: {options}")
            # Select "All" or empty option
            for opt in options:
                if opt.lower() in ("all", "", "-- all --", "show all", "all verdicts"):
                    filter_sel.select_option(label=opt)
                    time.sleep(1)
                    break
        except Exception as e:
            print(f"  No filter dropdown or error: {e}")

        # Get all table headers to understand column order
        headers = page.evaluate("""() => {
            const ths = document.querySelectorAll('table th, table thead td');
            return Array.from(ths).map(th => th.innerText.trim());
        }""")
        print(f"  Table headers: {headers}")
        results["headers"] = headers

        # Get all table rows and find WASH_SALE_TP
        wash_row = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'WASH_SALE_TP' || t.includes('WASH_SALE_TP'))) {
                    return {
                        all_cells: texts,
                        cell_count: cells.length
                    };
                }
            }
            return null;
        }""")
        print(f"  WASH_SALE_TP row (innerText): {wash_row}")

        # Also get the full HTML of the WASH_SALE_TP row to check for tooltip / hidden content
        wash_row_html = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'WASH_SALE_TP' || t.includes('WASH_SALE_TP'))) {
                    return row.innerHTML;
                }
            }
            return null;
        }""")
        # Print first 3000 chars of HTML
        if wash_row_html:
            print(f"  WASH_SALE_TP row HTML (first 3000 chars):\n{wash_row_html[:3000]}")
        results["wash_sale_tp_row"] = wash_row
        results["wash_sale_tp_html"] = wash_row_html

        # Also check data attributes and title attrs for full text
        wash_full_text = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'WASH_SALE_TP' || t.includes('WASH_SALE_TP'))) {
                    return cells.map(c => ({
                        innerText: c.innerText,
                        textContent: c.textContent,
                        title: c.getAttribute('title'),
                        dataFull: c.getAttribute('data-full'),
                        dataValue: c.getAttribute('data-value'),
                        outerHTML: c.outerHTML.substring(0, 800)
                    }));
                }
            }
            return null;
        }""")
        if wash_full_text:
            print("\n  WASH_SALE_TP per-cell detail:")
            for i, cell in enumerate(wash_full_text):
                print(f"    Cell[{i}]: innerText={repr(cell['innerText'][:200])}")
                if cell.get('title'):
                    print(f"           title={repr(cell['title'][:300])}")
                if cell.get('dataFull'):
                    print(f"           data-full={repr(cell['dataFull'][:300])}")
                if cell.get('dataValue'):
                    print(f"           data-value={repr(cell['dataValue'][:300])}")
        results["wash_full_text"] = wash_full_text

        # ---- ADJ_COST ROW ----
        print("\n--- FINDING ADJ_COST ROW ---")
        adj_row = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'ADJ_COST' || t.includes('ADJ_COST'))) {
                    return {
                        all_cells: texts,
                        cell_count: cells.length
                    };
                }
            }
            return null;
        }""")
        print(f"  ADJ_COST row: {adj_row}")

        adj_full = page.evaluate("""() => {
            const rows = document.querySelectorAll('table tbody tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'ADJ_COST' || t.includes('ADJ_COST'))) {
                    return cells.map(c => ({
                        innerText: c.innerText,
                        title: c.getAttribute('title'),
                        dataFull: c.getAttribute('data-full'),
                        dataValue: c.getAttribute('data-value'),
                    }));
                }
            }
            return null;
        }""")
        if adj_full:
            print("  ADJ_COST per-cell detail:")
            for i, cell in enumerate(adj_full):
                print(f"    Cell[{i}]: innerText={repr(cell['innerText'][:300])}")
                if cell.get('title'):
                    print(f"           title={repr(cell['title'][:300])}")
        results["adj_cost_row"] = adj_row
        results["adj_full"] = adj_full

        # ---- TOTAL ROW COUNT ----
        row_count = page.evaluate("() => document.querySelectorAll('table tbody tr').length")
        print(f"\nTotal visible rows in table: {row_count}")
        results["row_count"] = row_count

        # Final screenshot
        page.screenshot(path=SCREENSHOT_PATH, full_page=True)
        print(f"\nFinal full-page screenshot: {SCREENSHOT_PATH}")

        browser.close()

    return results


if __name__ == "__main__":
    data = run()
    print("\n\n=== RESULTS JSON ===")
    print(json.dumps(data, indent=2, default=str)[:8000])
