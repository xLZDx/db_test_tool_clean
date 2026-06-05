"""
E2E test v2: precise selectors for ODI vs DRD grid verification.
"""
import sys, time, json
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

XML_PATH = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
DRD_PATH = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
APP_URL  = "http://127.0.0.1:8550/mappings"

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1800, "height": 1000})
        page.goto(APP_URL, wait_until="networkidle", timeout=30000)
        print("[1] Page loaded")

        # Attach files
        page.locator("#odi-xml-file").set_input_files(XML_PATH)
        page.locator("#odi-drd-file").set_input_files(DRD_PATH)
        # Clear target fields
        page.locator("#odi-target-schema").fill("")
        page.locator("#odi-target-table").fill("")
        time.sleep(0.3)
        print("[2] Files attached, targets blank")

        # Click Analyze
        page.locator("#odi-analyze-btn").click()
        print("[3] Analyze clicked, waiting for grid section to appear...")

        # Wait for odi-grid-section to become visible (display != none)
        try:
            page.wait_for_function(
                "() => { const el = document.getElementById('odi-grid-section'); return el && el.style.display !== 'none' && el.offsetParent !== null; }",
                timeout=120000
            )
            print("[3] odi-grid-section is visible")
        except PlaywrightTimeoutError:
            print("[3] TIMEOUT waiting for odi-grid-section -- checking summary instead")
            try:
                page.wait_for_function(
                    "() => { const el = document.getElementById('odi-summary'); return el && el.style.display !== 'none'; }",
                    timeout=30000
                )
                print("[3] odi-summary visible")
            except Exception:
                print("[3] Neither grid nor summary appeared")

        time.sleep(1)

        # Take screenshot
        page.screenshot(path=r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_v2_after_analyze.png", full_page=True)
        print("[4] Screenshot saved: e2e_v2_after_analyze.png")

        # ---- TILE COUNTS ----
        print("\n====== TILE COUNTS ======")
        tile_ids = {
            "Matched":      "odi-s-matched",
            "Alias Drift":  "odi-s-alias",
            "Mismatch":     "odi-s-mismatch",
            "Unresolvable": "odi-s-unresolvable",
            "Missing":      "odi-s-missing",
            "ODI Extra":    "odi-s-extra",
        }
        tile_results = {}
        for label, tid in tile_ids.items():
            text = page.evaluate(f"() => {{ const el = document.getElementById('{tid}'); return el ? el.innerText.trim() : 'NOT FOUND'; }}")
            tile_results[label] = text
            print(f"  [{label}] = {repr(text)}")

        # ---- FILTER VERDICT ----
        print("\n====== FILTER: set to All ======")
        filter_options = page.evaluate("() => { const sel = document.getElementById('odi-filter-verdict'); if (!sel) return []; return Array.from(sel.options).map(o => ({value: o.value, text: o.text})); }")
        print(f"  Filter options: {filter_options}")
        # Select the "all" / empty option to see all rows
        page.evaluate("""() => {
            const sel = document.getElementById('odi-filter-verdict');
            if (sel) {
                // pick first option (usually 'All')
                sel.selectedIndex = 0;
                sel.dispatchEvent(new Event('change'));
            }
        }""")
        time.sleep(0.5)

        # ---- TABLE HEADERS ----
        print("\n====== GRID HEADERS ======")
        headers = page.evaluate("""() => {
            const section = document.getElementById('odi-grid-section');
            if (!section) return 'odi-grid-section not found';
            const ths = section.querySelectorAll('th, thead td');
            return Array.from(ths).map(th => th.innerText.trim());
        }""")
        print(f"  Headers: {headers}")

        # ---- TOTAL ROW COUNT ----
        row_count = page.evaluate("() => { const tb = document.getElementById('odi-grid-tbody'); return tb ? tb.querySelectorAll('tr').length : 0; }")
        print(f"\n  Total rows in odi-grid-tbody: {row_count}")

        # ---- WASH_SALE_TP ROW ----
        print("\n====== WASH_SALE_TP ROW ======")

        # Get all rows as array of arrays (innerText per cell)
        all_rows_summary = page.evaluate("""() => {
            const tb = document.getElementById('odi-grid-tbody');
            if (!tb) return [];
            return Array.from(tb.querySelectorAll('tr')).map((row, idx) => {
                const cells = Array.from(row.querySelectorAll('td'));
                return { idx: idx, texts: cells.map(c => c.innerText.trim()) };
            });
        }""")

        # Find WASH_SALE_TP row index
        wash_idx = None
        for row in all_rows_summary:
            if any("WASH_SALE_TP" in t for t in row["texts"]):
                wash_idx = row["idx"]
                print(f"  Found WASH_SALE_TP at row index {wash_idx}")
                print(f"  Row texts (innerText, first 300 chars each): {[t[:300] for t in row['texts']]}")
                break
        if wash_idx is None:
            print("  WASH_SALE_TP NOT FOUND in visible rows")
            # Show first 10 rows to debug
            for row in all_rows_summary[:10]:
                print(f"  Row {row['idx']}: {[t[:80] for t in row['texts']]}")

        # Now get FULL content of each cell in the WASH_SALE_TP row
        # including data attributes, title, and all text
        wash_full = page.evaluate("""() => {
            const tb = document.getElementById('odi-grid-tbody');
            if (!tb) return null;
            const rows = tb.querySelectorAll('tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'WASH_SALE_TP' || t.includes('WASH_SALE_TP'))) {
                    return cells.map((c, i) => ({
                        cell_index: i,
                        innerText: c.innerText,
                        textContent: c.textContent,
                        title: c.getAttribute('title'),
                        'data-full': c.getAttribute('data-full'),
                        'data-value': c.getAttribute('data-value'),
                        'data-raw': c.getAttribute('data-raw'),
                        innerHTML_snippet: c.innerHTML.substring(0, 1000)
                    }));
                }
            }
            return null;
        }""")

        print("\n  === WASH_SALE_TP full cell data ===")
        if wash_full:
            for cell in wash_full:
                idx = cell["cell_index"]
                print(f"\n  --- Cell[{idx}] ---")
                print(f"  innerText: {repr(cell['innerText'])}")
                if cell.get("title"):
                    print(f"  title: {repr(cell['title'])}")
                if cell.get("data-full"):
                    print(f"  data-full: {repr(cell['data-full'])}")
                if cell.get("data-value"):
                    print(f"  data-value: {repr(cell['data-value'])}")
                if cell.get("data-raw"):
                    print(f"  data-raw: {repr(cell['data-raw'])}")
                print(f"  innerHTML_snippet: {cell['innerHTML_snippet']}")
        else:
            print("  WASH_SALE_TP row NOT found")

        # ---- ADJ_COST ROW ----
        print("\n====== ADJ_COST ROW ======")
        adj_full = page.evaluate("""() => {
            const tb = document.getElementById('odi-grid-tbody');
            if (!tb) return null;
            const rows = tb.querySelectorAll('tr');
            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                const texts = cells.map(c => c.innerText.trim());
                if (texts.some(t => t === 'ADJ_COST' || t.includes('ADJ_COST'))) {
                    return cells.map((c, i) => ({
                        cell_index: i,
                        innerText: c.innerText,
                        title: c.getAttribute('title'),
                        'data-full': c.getAttribute('data-full'),
                        'data-value': c.getAttribute('data-value'),
                    }));
                }
            }
            return null;
        }""")

        if adj_full:
            for cell in adj_full:
                idx = cell["cell_index"]
                print(f"\n  --- ADJ_COST Cell[{idx}] ---")
                print(f"  innerText: {repr(cell['innerText'])}")
                if cell.get("title"):
                    print(f"  title: {repr(cell['title'])}")
                if cell.get("data-full"):
                    print(f"  data-full: {repr(cell['data-full'])}")
        else:
            print("  ADJ_COST NOT FOUND -- looking for any CASE row")
            # Find first row that has CASE in any cell
            for row in all_rows_summary:
                if any("CASE" in t for t in row["texts"]):
                    print(f"  First CASE row idx={row['idx']}: {[t[:200] for t in row['texts']]}")
                    break

        # ---- MISMATCH FILTER ----
        print("\n====== MISMATCH FILTER TEST ======")
        # Set filter to mismatches only if option exists
        mismatch_opt = None
        for opt in filter_options:
            if "mismatch" in opt.get("text","").lower() or "mismatch" in opt.get("value","").lower():
                mismatch_opt = opt
                break
        if mismatch_opt:
            page.evaluate(f"""() => {{
                const sel = document.getElementById('odi-filter-verdict');
                sel.value = '{mismatch_opt['value']}';
                sel.dispatchEvent(new Event('change'));
            }}""")
            time.sleep(0.5)
            mismatch_rows = page.evaluate("""() => {
                const tb = document.getElementById('odi-grid-tbody');
                if (!tb) return [];
                return Array.from(tb.querySelectorAll('tr'))
                    .filter(r => r.style.display !== 'none')
                    .map((row, idx) => {
                        const cells = Array.from(row.querySelectorAll('td'));
                        return cells.map(c => c.innerText.trim());
                    });
            }""")
            print(f"  Rows visible under mismatch filter: {len(mismatch_rows)}")
            for r in mismatch_rows[:5]:
                print(f"    {[t[:100] for t in r]}")
        else:
            print(f"  No mismatch filter option found. Options: {filter_options}")

        # Final screenshot
        page.screenshot(path=r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_v2_final.png", full_page=True)
        browser.close()

    return {"tile_results": tile_results, "headers": headers, "wash_full": wash_full, "row_count": row_count}


if __name__ == "__main__":
    run()
