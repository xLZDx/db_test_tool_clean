"""Extract tile counts and SQL header lines for both CLOSE and OPEN scenarios."""
import os
from playwright.sync_api import sync_playwright

APP_URL = "http://127.0.0.1:8550/mappings"
CLOSE_XML = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\closed lot.csv"
OPEN_XML  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
OPEN_DRD  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\Open_lot.csv"
SCREENSHOT_DIR = r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_screenshots"

def run_scenario(page, xml_path, drd_path, label):
    page.goto(APP_URL, wait_until="networkidle")
    page.wait_for_timeout(500)

    # Attach XML
    xml_input = page.locator("#odi-xml-file")
    xml_input.set_input_files(xml_path)
    page.wait_for_timeout(300)

    # Attach DRD
    drd_input = page.locator("#odi-drd-file")
    drd_input.set_input_files(drd_path)
    page.wait_for_timeout(300)

    # Click Analyze
    page.locator("button:has-text('Analyze')").first.click()

    # Wait for results
    page.wait_for_function(
        "() => document.body.innerText.includes('INSERT INTO') || document.body.innerText.includes('MERGE')",
        timeout=30000
    )
    page.wait_for_timeout(1000)

    page.screenshot(path=os.path.join(SCREENSHOT_DIR, f"tiles_{label}.png"))

    # Dump all tile/badge/stat elements
    print(f"\n{'='*60}")
    print(f"SCENARIO: {label}")
    print(f"{'='*60}")

    # Try various selectors for numeric tile values
    selectors_to_try = [
        ".tile-count",
        ".count-tile",
        ".stat-number",
        ".metric-value",
        "[class*='tile'] .number",
        "[class*='tile'] .value",
        "[class*='count']",
        "[class*='badge']",
        ".kpi",
        ".summary-tile",
        ".result-tile",
        ".tile",
    ]

    print("\n--- Attempting common tile selectors ---")
    for sel in selectors_to_try:
        try:
            els = page.locator(sel).all()
            if els:
                texts = [e.inner_text().strip() for e in els[:15]]
                non_empty = [t for t in texts if t]
                if non_empty:
                    print(f"  {sel}: {non_empty[:8]}")
        except Exception:
            pass

    # Dump the odi-val-card full inner text
    print("\n--- Full odi-val-card inner text ---")
    try:
        card_text = page.locator("#odi-val-card").inner_text()
        print(card_text[:3000])
    except Exception as e:
        print(f"  Error: {e}")

    # Try to find tile data via JS evaluation
    print("\n--- JS evaluation of tile data ---")
    try:
        tile_data = page.evaluate("""() => {
            const results = {};
            // Look for elements containing just a number adjacent to a label
            const allEls = document.querySelectorAll('[id*="tile"], [id*="count"], [id*="matched"], [id*="mismatch"], [id*="missing"], [id*="unresolvable"], [id*="alias"], [id*="extra"]');
            allEls.forEach(el => {
                results[el.id || el.className] = el.innerText.trim().substring(0, 80);
            });
            return results;
        }""")
        print(f"  tile_data by id/class pattern: {tile_data}")
    except Exception as e:
        print(f"  JS eval error: {e}")

    # Dump all elements with a numeric-looking text content
    print("\n--- Elements with numeric text (1-4 digits) ---")
    try:
        numeric_els = page.evaluate("""() => {
            const results = [];
            const all = document.querySelectorAll('*');
            for (const el of all) {
                const direct = Array.from(el.childNodes)
                    .filter(n => n.nodeType === 3)
                    .map(n => n.textContent.trim())
                    .join('');
                if (/^\\d{1,4}$/.test(direct) && el.children.length === 0) {
                    results.push({
                        tag: el.tagName,
                        id: el.id,
                        cls: el.className.substring(0, 60),
                        text: direct,
                        parent_text: el.parentElement ? el.parentElement.innerText.trim().substring(0, 80) : ''
                    });
                }
                if (results.length > 40) break;
            }
            return results;
        }""")
        for r in numeric_els:
            print(f"  <{r['tag']} id='{r['id']}' class='{r['cls']}'>{r['text']}</{r['tag']}> | parent: {r['parent_text'][:60]}")
    except Exception as e:
        print(f"  Numeric el scan error: {e}")

    # Get the SQL header lines
    print("\n--- SQL Header (first 10 lines) ---")
    try:
        sql_text = page.evaluate("""() => {
            const pres = document.querySelectorAll('pre, code, [class*="sql"], [id*="sql"], textarea');
            for (const el of pres) {
                const t = el.innerText || el.value || '';
                if (t.includes('INSERT INTO') || t.includes('MERGE')) return t;
            }
            return null;
        }""")
        if sql_text:
            for i, line in enumerate(sql_text.split('\n')[:10]):
                print(f"  {i+1}: {line}")
        else:
            # Try body text search
            body = page.inner_text("body")
            lines = body.split('\n')
            for i, line in enumerate(lines):
                if '-- Generated Oracle INSERT' in line or '-- IKM style' in line or 'INSERT INTO' in line:
                    start = max(0, i-1)
                    for j, ln in enumerate(lines[start:start+10]):
                        print(f"  {j+1}: {ln}")
                    break
    except Exception as e:
        print(f"  SQL error: {e}")

    # Get verdict summary from body
    print("\n--- Verdict count search in body text ---")
    try:
        body = page.inner_text("body")
        lines = body.split('\n')
        verdict_lines = [l for l in lines if any(v in l.upper() for v in ['MATCHED', 'MISMATCH', 'MISSING', 'UNRESOLVABLE', 'ALIAS', 'EXTRA'])]
        # Find lines that look like summary counts (short lines with numbers)
        for l in verdict_lines[:30]:
            if len(l.strip()) < 100:
                print(f"  {repr(l.strip())}")
    except Exception as e:
        print(f"  Error: {e}")


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    run_scenario(page, CLOSE_XML, CLOSE_DRD, "CLOSE")
    run_scenario(page, OPEN_XML, OPEN_DRD, "OPEN")
    browser.close()
