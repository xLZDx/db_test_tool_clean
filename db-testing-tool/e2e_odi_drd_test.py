"""
E2E test: ODI vs DRD Validation panel
Tests: CARD-DOES-NOT-DISAPPEAR, CLOSE faithful INSERT, OPEN MERGE faithful INSERT
"""
import sys
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

APP_URL = "http://127.0.0.1:8550/mappings"

CLOSE_XML = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\closed lot.csv"
OPEN_XML  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
OPEN_DRD  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\Open_lot.csv"

SCREENSHOT_DIR = r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

results = {}


def get_tile_counts(page):
    """Read the six count tiles from the DOM."""
    tiles = {}
    # Try to get tile values by label text
    tile_labels = ["Matched", "Alias Drift", "Mismatch", "Unresolvable", "Missing", "ODI Extra"]
    for label in tile_labels:
        try:
            # Look for a tile whose text contains the label
            # Different possible structures
            el = page.locator(f"text={label}").first
            parent = el.locator("xpath=..").first
            # Try to find a number sibling/child
            count_text = parent.inner_text()
            tiles[label] = count_text.strip()
        except Exception as e:
            tiles[label] = f"<error: {e}>"
    return tiles


def get_tile_counts_v2(page):
    """Alternative: scrape all tile containers by class or id."""
    tiles = {}
    # Try stat tiles / count divs
    try:
        # Get all elements that look like stat boxes
        cards = page.locator("[class*='tile'], [class*='stat'], [class*='count'], [class*='badge'], .metric, .kpi-tile").all()
        for c in cards:
            txt = c.inner_text().strip()
            tiles[f"tile_{len(tiles)}"] = txt
    except Exception as e:
        tiles["error"] = str(e)
    return tiles


def get_sql_block(page):
    """Get the emitted SQL block text."""
    selectors = [
        "#emitted-sql",
        "[id*='sql']",
        "pre",
        "code",
        "[class*='sql']",
        "[class*='insert']",
        "textarea",
    ]
    for sel in selectors:
        try:
            els = page.locator(sel).all()
            for el in els:
                txt = el.inner_text().strip()
                if "INSERT" in txt or "MERGE" in txt or "SELECT" in txt:
                    return txt
        except Exception:
            pass
    # Fallback: search all visible text on page for SQL
    try:
        body = page.inner_text("body")
        # Find INSERT or MERGE block
        lines = body.split("\n")
        sql_lines = []
        in_sql = False
        for line in lines:
            if "INSERT INTO" in line or "MERGE" in line:
                in_sql = True
            if in_sql:
                sql_lines.append(line)
                if len(sql_lines) > 30:
                    break
        if sql_lines:
            return "\n".join(sql_lines)
    except Exception:
        pass
    return "<SQL block not found>"


def scrape_all_tile_counts(page):
    """Scrape the page body for the six tile count values."""
    try:
        body = page.inner_text("body")
        return body
    except Exception as e:
        return f"<error: {e}>"


def run_tests():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # ----------------------------------------------------------------
        # TEST 1: CARD-DOES-NOT-DISAPPEAR
        # ----------------------------------------------------------------
        print("\n=== TEST 1: CARD-DOES-NOT-DISAPPEAR ===")
        page.goto(APP_URL, wait_until="networkidle")
        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "t1_initial_load.png"))
        print(f"Page title: {page.title()}")

        # Check card is visible
        card_visible_before = False
        card_html_before = ""
        try:
            # Try by id first
            card = page.locator("#odi-val-card")
            count = card.count()
            print(f"  #odi-val-card count: {count}")
            if count > 0:
                card_visible_before = card.first.is_visible()
                print(f"  Card visible (by #odi-val-card): {card_visible_before}")
            else:
                # Try by heading text
                card_by_text = page.locator("text=ODI vs DRD Validation").first
                card_visible_before = card_by_text.is_visible()
                print(f"  Card visible (by heading text): {card_visible_before}")
        except Exception as e:
            print(f"  Card check error: {e}")
            card_visible_before = False

        # Attach CLOSE XML file
        print("  Attaching CLOSE XML file...")
        try:
            file_input = page.locator("#odi-xml-file")
            if file_input.count() == 0:
                # Try by type=file near the xml label
                file_input = page.locator("input[type='file']").first
            file_input.set_input_files(CLOSE_XML)
            print("  File attached successfully")
            # Small wait
            page.wait_for_timeout(500)
        except Exception as e:
            print(f"  File attach error: {e}")

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "t1_after_file_attach.png"))

        # Check card still visible after file attach
        card_visible_after = False
        try:
            card = page.locator("#odi-val-card")
            if card.count() > 0:
                card_visible_after = card.first.is_visible()
                print(f"  Card visible after attach (by #odi-val-card): {card_visible_after}")
            else:
                card_by_text = page.locator("text=ODI vs DRD Validation").first
                card_visible_after = card_by_text.is_visible()
                print(f"  Card visible after attach (by heading text): {card_visible_after}")
        except Exception as e:
            print(f"  Card check after attach error: {e}")
            card_visible_after = False

        t1_pass = card_visible_before and card_visible_after
        results["TEST1_CARD_NOT_DISAPPEAR"] = {
            "pass": t1_pass,
            "visible_before_attach": card_visible_before,
            "visible_after_attach": card_visible_after,
        }
        print(f"  TEST 1 result: {'PASS' if t1_pass else 'FAIL'}")
        print(f"    before={card_visible_before}, after={card_visible_after}")

        # ----------------------------------------------------------------
        # TEST 2: CLOSE faithful INSERT
        # ----------------------------------------------------------------
        print("\n=== TEST 2: CLOSE faithful INSERT ===")
        page.goto(APP_URL, wait_until="networkidle")
        page.wait_for_timeout(500)

        # Attach CLOSE XML
        try:
            xml_input = page.locator("#odi-xml-file")
            if xml_input.count() == 0:
                xml_inputs = page.locator("input[type='file']").all()
                print(f"  Found {len(xml_inputs)} file inputs")
                xml_input = xml_inputs[0] if xml_inputs else None
            if xml_input:
                xml_input.set_input_files(CLOSE_XML)
                print(f"  CLOSE XML attached")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  XML attach error: {e}")

        # Attach CLOSE DRD
        try:
            drd_input = page.locator("#odi-drd-file")
            if drd_input.count() == 0:
                xml_inputs = page.locator("input[type='file']").all()
                drd_input = xml_inputs[1] if len(xml_inputs) > 1 else None
            if drd_input:
                drd_input.set_input_files(CLOSE_DRD)
                print(f"  CLOSE DRD attached")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  DRD attach error: {e}")

        # Click Analyze
        print("  Clicking Analyze button...")
        try:
            analyze_btn = page.locator("button:has-text('Analyze')").first
            if analyze_btn.count() == 0:
                analyze_btn = page.locator("button[type='submit']").first
            analyze_btn.click()
            print("  Analyze clicked")
        except Exception as e:
            print(f"  Analyze click error: {e}")

        # Wait for results
        print("  Waiting for results (up to 30s)...")
        try:
            page.wait_for_function(
                "() => document.body.innerText.includes('INSERT INTO') || document.body.innerText.includes('MERGE') || document.body.innerText.includes('no staging')",
                timeout=30000
            )
            print("  Results appeared")
        except Exception as e:
            print(f"  Wait for results timeout/error: {e}")
            page.wait_for_timeout(5000)

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "t2_close_results.png"))

        # Get SQL block
        sql_close = get_sql_block(page)
        print(f"\n  === CLOSE SQL (first 10 lines) ===")
        sql_close_lines = sql_close.split("\n")
        for i, ln in enumerate(sql_close_lines[:10]):
            print(f"  {i+1}: {ln}")

        # Get full page body for tile counts
        body_close = scrape_all_tile_counts(page)

        # Extract tile counts from page
        print("\n  === Extracting tile counts ===")
        tile_counts_close = {}
        for label in ["Matched", "Alias Drift", "Alias", "Mismatch", "Unresolvable", "Missing", "ODI Extra"]:
            try:
                # Find the count associated with the label
                el = page.locator(f"text={label}").first
                if el.count() > 0 or el.is_visible():
                    parent_text = el.locator("xpath=..").inner_text()
                    tile_counts_close[label] = parent_text.strip()
                    print(f"    {label}: {parent_text.strip()[:60]}")
            except Exception as e:
                tile_counts_close[label] = f"<{e}>"

        # Assertions
        must_contain_close = [
            "INSERT INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT",
            "Simple-Insert (faithful)",
        ]
        must_not_contain_close = [
            "SSDS_AVY",
            "WITH SSDS_AVY",
            "no staging steps",
        ]

        close_checks = {}
        for m in must_contain_close:
            found = m in sql_close or m in body_close
            close_checks[f"MUST CONTAIN: {m}"] = found
            print(f"  {'OK' if found else 'FAIL'} MUST CONTAIN: {m[:60]}")

        for m in must_not_contain_close:
            absent = m not in sql_close and m not in body_close
            close_checks[f"MUST NOT CONTAIN: {m}"] = absent
            print(f"  {'OK' if absent else 'FAIL'} MUST NOT CONTAIN: {m[:60]}")

        t2_pass = all(close_checks.values())
        results["TEST2_CLOSE_INSERT"] = {
            "pass": t2_pass,
            "sql_first_6_lines": "\n".join(sql_close_lines[:6]),
            "checks": close_checks,
            "tile_counts": tile_counts_close,
        }
        print(f"\n  TEST 2 result: {'PASS' if t2_pass else 'FAIL'}")

        # ----------------------------------------------------------------
        # TEST 3: OPEN MERGE faithful INSERT
        # ----------------------------------------------------------------
        print("\n=== TEST 3: OPEN MERGE faithful INSERT ===")
        page.goto(APP_URL, wait_until="networkidle")
        page.wait_for_timeout(500)

        # Attach OPEN XML
        try:
            xml_input = page.locator("#odi-xml-file")
            if xml_input.count() == 0:
                xml_inputs = page.locator("input[type='file']").all()
                xml_input = xml_inputs[0] if xml_inputs else None
            if xml_input:
                xml_input.set_input_files(OPEN_XML)
                print(f"  OPEN XML attached")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  XML attach error: {e}")

        # Attach OPEN DRD
        try:
            drd_input = page.locator("#odi-drd-file")
            if drd_input.count() == 0:
                xml_inputs = page.locator("input[type='file']").all()
                drd_input = xml_inputs[1] if len(xml_inputs) > 1 else None
            if drd_input:
                drd_input.set_input_files(OPEN_DRD)
                print(f"  OPEN DRD attached")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  DRD attach error: {e}")

        # Click Analyze
        print("  Clicking Analyze button...")
        try:
            analyze_btn = page.locator("button:has-text('Analyze')").first
            analyze_btn.click()
            print("  Analyze clicked")
        except Exception as e:
            print(f"  Analyze click error: {e}")

        # Wait for results
        print("  Waiting for results (up to 30s)...")
        try:
            page.wait_for_function(
                "() => document.body.innerText.includes('INSERT INTO') || document.body.innerText.includes('MERGE') || document.body.innerText.includes('no staging')",
                timeout=30000
            )
            print("  Results appeared")
        except Exception as e:
            print(f"  Wait for results timeout/error: {e}")
            page.wait_for_timeout(5000)

        page.screenshot(path=os.path.join(SCREENSHOT_DIR, "t3_open_results.png"))

        # Get SQL block
        sql_open = get_sql_block(page)
        print(f"\n  === OPEN SQL (first 10 lines) ===")
        sql_open_lines = sql_open.split("\n")
        for i, ln in enumerate(sql_open_lines[:10]):
            print(f"  {i+1}: {ln}")

        body_open = scrape_all_tile_counts(page)

        # Extract tile counts from page
        print("\n  === Extracting tile counts ===")
        tile_counts_open = {}
        for label in ["Matched", "Alias Drift", "Alias", "Mismatch", "Unresolvable", "Missing", "ODI Extra"]:
            try:
                el = page.locator(f"text={label}").first
                if el.count() > 0 or el.is_visible():
                    parent_text = el.locator("xpath=..").inner_text()
                    tile_counts_open[label] = parent_text.strip()
                    print(f"    {label}: {parent_text.strip()[:60]}")
            except Exception as e:
                tile_counts_open[label] = f"<{e}>"

        # Assertions
        must_contain_open = [
            "INSERT INTO TAXLOTS_OWNER.OPN_TAX_LOTS_NON_BKR_FACT",
            "MERGE (faithful, from USING)",
            "SRC_STM_DIM.SRC_STM_CD",
        ]
        must_not_contain_open = [
            "no staging steps",
            "SSDS_AVY",
        ]

        open_checks = {}
        for m in must_contain_open:
            found = m in sql_open or m in body_open
            open_checks[f"MUST CONTAIN: {m}"] = found
            print(f"  {'OK' if found else 'FAIL'} MUST CONTAIN: {m[:80]}")

        for m in must_not_contain_open:
            absent = m not in sql_open and m not in body_open
            open_checks[f"MUST NOT CONTAIN: {m}"] = absent
            print(f"  {'OK' if absent else 'FAIL'} MUST NOT CONTAIN: {m[:80]}")

        t3_pass = all(open_checks.values())
        results["TEST3_OPEN_INSERT"] = {
            "pass": t3_pass,
            "sql_first_6_lines": "\n".join(sql_open_lines[:6]),
            "checks": open_checks,
            "tile_counts": tile_counts_open,
        }
        print(f"\n  TEST 3 result: {'PASS' if t3_pass else 'FAIL'}")

        # ----------------------------------------------------------------
        # Additional: dump raw page body sections for debugging
        # ----------------------------------------------------------------
        print("\n=== RAW PAGE BODY SAMPLE (OPEN, last 2000 chars) ===")
        print(body_open[-2000:] if len(body_open) > 2000 else body_open)

        browser.close()

    # Final summary
    print("\n" + "="*60)
    print("FINAL SUMMARY")
    print("="*60)
    for name, r in results.items():
        status = "PASS" if r["pass"] else "FAIL"
        print(f"  {name}: {status}")
        if not r["pass"]:
            if "checks" in r:
                for k, v in r["checks"].items():
                    if not v:
                        print(f"    FAILED: {k}")

    print("\nScreenshots saved to:", SCREENSHOT_DIR)
    return results


if __name__ == "__main__":
    run_tests()
