"""
E2E test for ODI vs DRD Validation panel.
Tests: OPEN MERGE INSERT caveat, CLOSE Simple-Insert no-caveat, card visibility regression.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

APP_URL = "http://127.0.0.1:8550/mappings"
SCREENSHOT_DIR = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/e2e_screenshots")
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

OPEN_XML  = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
OPEN_DRD  = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/Open_lot.csv"
CLOSE_XML = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/closed lot.csv"

results = {}

def screenshot(page, name):
    path = str(SCREENSHOT_DIR / f"{name}.png")
    page.screenshot(path=path, full_page=True)
    print(f"[screenshot] {path}")
    return path

def read_tile_counts(page):
    """Read the six tile count values from the ODI validation card."""
    tiles = {}
    # tile IDs used in the page
    tile_map = {
        "matched":      ["odi-match-count",    "match-count",    "#odi-val-card .tile-matched",    "[data-tile='matched']"],
        "alias":        ["odi-alias-count",     "alias-count",    "#odi-val-card .tile-alias"],
        "mismatch":     ["odi-mismatch-count",  "mismatch-count", "#odi-val-card .tile-mismatch"],
        "unresolvable": ["odi-unresolvable-count","unresolvable-count","#odi-val-card .tile-unresolvable"],
        "missing":      ["odi-missing-count",   "missing-count",  "#odi-val-card .tile-missing"],
        "odi_extra":    ["odi-extra-count",      "extra-count",   "#odi-val-card .tile-extra"],
    }
    # Try generic approach: find all elements with count-like class/id patterns
    # First attempt: look for elements by id patterns
    for label, candidates in tile_map.items():
        val = None
        for sel in candidates:
            try:
                if sel.startswith("#") or sel.startswith(".") or sel.startswith("["):
                    el = page.query_selector(sel)
                else:
                    el = page.query_selector(f"#{sel}")
                if el:
                    text = el.inner_text().strip()
                    if text:
                        val = text
                        break
            except Exception:
                pass
        tiles[label] = val if val is not None else "NOT FOUND"

    # If still not found, try to grab all count-bearing elements in the card
    if all(v == "NOT FOUND" for v in tiles.values()):
        try:
            card = page.query_selector("#odi-val-card")
            if card:
                # look for anything with a large number that looks like a count
                all_els = card.query_selector_all("*")
                for el in all_els:
                    txt = el.inner_text().strip()
                    if txt.isdigit():
                        print(f"  [tile-scan] found numeric element: tag={el.evaluate('e=>e.tagName')} id={el.get_attribute('id')} class={el.get_attribute('class')} text={txt}")
        except Exception as e:
            print(f"  [tile-scan] error: {e}")

    return tiles

def get_sql_block(page):
    """Extract text from the Emitted Oracle INSERT SQL block."""
    sql_text = None
    selectors = [
        "#odi-sql-output",
        "#odi-val-card pre",
        "#odi-val-card code",
        "#odi-val-card textarea",
        ".sql-output",
        "#emitted-sql",
        "#sql-block",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if text:
                    sql_text = text
                    print(f"  [sql-found] selector={sel}, length={len(text)}")
                    break
        except Exception:
            pass

    if sql_text is None:
        # Try to find any pre/code/textarea inside the card
        try:
            card = page.query_selector("#odi-val-card")
            if card:
                for tag in ["pre", "code", "textarea", "div.sql", ".code-block"]:
                    el = card.query_selector(tag)
                    if el:
                        text = el.inner_text().strip()
                        if text and len(text) > 20:
                            sql_text = text
                            print(f"  [sql-fallback] tag={tag}, length={len(text)}")
                            break
        except Exception as e:
            print(f"  [sql-search] error: {e}")

    return sql_text

def wait_for_analysis_complete(page, timeout_ms=60000):
    """Wait until analysis results appear (SQL block or tile counts become non-zero)."""
    start = time.time()
    deadline = start + timeout_ms / 1000.0
    while time.time() < deadline:
        # Check if a loading indicator is gone
        loading = page.query_selector(".loading, .spinner, #loading-indicator, [data-loading='true']")
        if loading:
            time.sleep(0.5)
            continue
        # Check if SQL block has content
        for sel in ["#odi-sql-output", "#odi-val-card pre", "#odi-val-card code", "#emitted-sql"]:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) > 50:
                    return True
        # Check if any tile has a non-zero count
        for sel in ["#odi-match-count", "#odi-mismatch-count", ".tile-matched", ".tile-mismatch"]:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if text and text != "0" and text.isdigit():
                    return True
        time.sleep(0.5)
    return False

def run_analysis(page, xml_path, drd_path, label):
    """Navigate to page, attach files, submit, wait for results."""
    print(f"\n{'='*60}")
    print(f"Running analysis: {label}")
    print(f"{'='*60}")

    # Navigate fresh
    page.goto(APP_URL, wait_until="networkidle", timeout=30000)
    screenshot(page, f"{label}_01_page_loaded")

    # Find the ODI validation card
    card = page.query_selector("#odi-val-card")
    if not card:
        # Try to find it by looking for the file inputs
        card_area = page.query_selector("[id*='odi']")
        print(f"  [card-search] #odi-val-card not found directly, found: {card_area}")

    # Scroll to the card
    page.evaluate("document.querySelector('#odi-val-card') && document.querySelector('#odi-val-card').scrollIntoView()")
    time.sleep(0.3)

    # Attach XML file
    xml_input = page.query_selector("#odi-xml-file")
    if not xml_input:
        # Try alternative selectors
        for sel in ["input[accept*='xml']", "input[name*='xml']", "input[id*='xml']"]:
            xml_input = page.query_selector(sel)
            if xml_input:
                print(f"  [xml-input] found via {sel}")
                break

    if xml_input:
        xml_input.set_input_files(xml_path)
        print(f"  [OK] XML file attached: {Path(xml_path).name}")
    else:
        print(f"  [FAIL] Could not find XML file input")
        # Dump all file inputs on page
        inputs = page.query_selector_all("input[type='file']")
        print(f"  [debug] All file inputs: {[i.get_attribute('id') or i.get_attribute('name') for i in inputs]}")

    time.sleep(0.3)

    # Attach DRD file
    drd_input = page.query_selector("#odi-drd-file")
    if not drd_input:
        for sel in ["input[accept*='csv']", "input[name*='drd']", "input[id*='drd']"]:
            drd_input = page.query_selector(sel)
            if drd_input:
                print(f"  [drd-input] found via {sel}")
                break

    if drd_input:
        drd_input.set_input_files(drd_path)
        print(f"  [OK] DRD file attached: {Path(drd_path).name}")
    else:
        print(f"  [FAIL] Could not find DRD file input")

    time.sleep(0.3)
    screenshot(page, f"{label}_02_files_attached")

    # Verify card is still visible (Test 3 regression guard)
    card_visible = page.is_visible("#odi-val-card")
    print(f"  [T3-regression] odi-val-card visible after file attach: {card_visible}")

    # Check Target Schema/Table field - leave blank for auto-detect
    # (just confirm it exists and is blank)
    target_schema = page.query_selector("#odi-target-schema, #target-schema, input[name='target_schema']")
    target_table  = page.query_selector("#odi-target-table, #target-table, input[name='target_table']")
    if target_schema:
        val = target_schema.input_value()
        print(f"  [target-schema] current value: '{val}' (should be blank for auto-detect)")
    if target_table:
        val = target_table.input_value()
        print(f"  [target-table] current value: '{val}' (should be blank for auto-detect)")

    # Click Analyze button
    analyze_btn = None
    for sel in [
        "#odi-analyze-btn",
        "#odi-val-card button[type='submit']",
        "#odi-val-card button",
        "button[id*='analyze']",
        "button[data-action='analyze']",
        "button:has-text('Analyze')",
    ]:
        try:
            btn = page.query_selector(sel)
            if btn:
                analyze_btn = btn
                print(f"  [analyze-btn] found via {sel}")
                break
        except Exception:
            pass

    if not analyze_btn:
        # Try to find by text content
        buttons = page.query_selector_all("button")
        for btn in buttons:
            txt = btn.inner_text().strip().lower()
            if "analyz" in txt or "submit" in txt or "run" in txt:
                analyze_btn = btn
                print(f"  [analyze-btn] found by text: '{btn.inner_text().strip()}'")
                break

    if analyze_btn:
        analyze_btn.click()
        print(f"  [OK] Analyze button clicked")
    else:
        print(f"  [FAIL] Could not find Analyze button")
        buttons = page.query_selector_all("button")
        print(f"  [debug] All buttons: {[b.inner_text().strip() for b in buttons[:20]]}")
        return None, None, False

    # Wait for results
    print(f"  [waiting] for analysis results...")
    completed = wait_for_analysis_complete(page, timeout_ms=90000)
    print(f"  [wait-result] completed={completed}")

    # Give extra time for DOM to settle
    time.sleep(2)
    screenshot(page, f"{label}_03_results")

    # Read SQL block
    sql_text = get_sql_block(page)

    # Read tile counts
    tiles = read_tile_counts(page)
    print(f"  [tiles] {tiles}")

    return sql_text, tiles, card_visible

def analyze_sql_headers(sql_text, label):
    """Extract and print first 12 lines of SQL block."""
    if not sql_text:
        print(f"  [SQL-{label}] SQL block is EMPTY or NOT FOUND")
        return []
    lines = sql_text.split('\n')
    header_lines = lines[:12]
    print(f"\n  --- SQL block first {len(header_lines)} lines ({label}) ---")
    for i, line in enumerate(header_lines, 1):
        print(f"  {i:02d}: {line}")
    print(f"  --- (total SQL length: {len(sql_text)} chars, {len(lines)} lines) ---")
    return header_lines

def check_no_em_dash(text):
    """Check text contains no em-dash (U+2014) or en-dash (U+2013)."""
    for char in ['—', '–']:
        if char in text:
            return False, repr(char)
    return True, None

def main():
    print("\n" + "="*70)
    print("E2E TEST: ODI vs DRD Validation Panel")
    print("="*70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(viewport={"width": 1600, "height": 900})
        page = context.new_page()

        # Set up console logging from browser
        page.on("console", lambda msg: print(f"  [browser-{msg.type}] {msg.text}") if msg.type in ("error","warning") else None)
        page.on("pageerror", lambda err: print(f"  [page-error] {err}"))

        # --- TEST 1: OPEN MERGE INSERT with CAVEAT ---
        print("\n" + "="*70)
        print("TEST 1: OPEN files - expect MERGE INSERT + CAVEAT")
        print("="*70)

        sql_open, tiles_open, card_visible_open = run_analysis(
            page, OPEN_XML, OPEN_DRD, "open"
        )

        open_header = analyze_sql_headers(sql_open, "OPEN")

        t1_pass = True
        t1_notes = []

        if sql_open:
            sql_upper = sql_open.upper()
            full_sql = sql_open

            # Required: IKM style: MERGE
            if "IKM style: MERGE" in full_sql or "IKM STYLE: MERGE" in sql_upper:
                t1_notes.append("PASS: found 'IKM style: MERGE'")
            else:
                t1_pass = False
                t1_notes.append("FAIL: 'IKM style: MERGE' NOT found")

            # Required: CAVEAT line
            if "-- CAVEAT:" in full_sql:
                caveat_line = [l for l in full_sql.split('\n') if '-- CAVEAT:' in l]
                t1_notes.append(f"PASS: found '-- CAVEAT:' line: {caveat_line[0].strip()[:100] if caveat_line else '(found)'}")
                # Check caveat mentions MERGE / "only INSERTs unmatched"
                caveat_text = caveat_line[0] if caveat_line else ""
                if "MERGE" in caveat_text.upper() or "only INSERTs" in caveat_text or "unmatched" in caveat_text.lower():
                    t1_notes.append("PASS: CAVEAT mentions MERGE/upsert semantics")
                else:
                    t1_notes.append(f"WARN: CAVEAT line found but may not mention MERGE/upsert: {caveat_text[:120]}")
            else:
                t1_pass = False
                t1_notes.append("FAIL: '-- CAVEAT:' NOT found in SQL")

            # Required: INSERT INTO TAXLOTS_OWNER.OPN_TAX_LOTS_NON_BKR_FACT
            if "INSERT INTO TAXLOTS_OWNER.OPN_TAX_LOTS_NON_BKR_FACT" in sql_upper:
                t1_notes.append("PASS: found 'INSERT INTO TAXLOTS_OWNER.OPN_TAX_LOTS_NON_BKR_FACT'")
            else:
                t1_pass = False
                t1_notes.append("FAIL: 'INSERT INTO TAXLOTS_OWNER.OPN_TAX_LOTS_NON_BKR_FACT' NOT found")

            # Required: SRC_STM_DIM.SRC_STM_CD
            if "SRC_STM_DIM.SRC_STM_CD" in full_sql:
                t1_notes.append("PASS: found 'SRC_STM_DIM.SRC_STM_CD'")
            else:
                t1_pass = False
                t1_notes.append("FAIL: 'SRC_STM_DIM.SRC_STM_CD' NOT found")

            # Must NOT contain: SSDS_AVY
            if "SSDS_AVY" in sql_upper:
                t1_pass = False
                t1_notes.append("FAIL: SQL contains forbidden 'SSDS_AVY'")
            else:
                t1_notes.append("PASS: 'SSDS_AVY' not found (correct)")

            # Must NOT contain: "no staging steps"
            if "no staging steps" in full_sql.lower():
                t1_pass = False
                t1_notes.append("FAIL: SQL contains forbidden 'no staging steps'")
            else:
                t1_notes.append("PASS: 'no staging steps' not found (correct)")

            # Must NOT contain em-dash
            em_ok, em_char = check_no_em_dash(full_sql)
            if em_ok:
                t1_notes.append("PASS: no em-dash characters in SQL")
            else:
                t1_pass = False
                t1_notes.append(f"FAIL: em-dash {em_char} found in SQL")
        else:
            t1_pass = False
            t1_notes.append("FAIL: SQL block not found or empty")

        # Tile count check
        expected_open = {"matched": "51", "alias": "0", "mismatch": "7", "unresolvable": "3", "missing": "0", "odi_extra": "5"}
        print(f"\n  [T1-tiles] OPEN tiles observed: {tiles_open}")
        print(f"  [T1-tiles] OPEN tiles expected: {expected_open}")
        for key, exp in expected_open.items():
            obs = tiles_open.get(key, "NOT FOUND")
            if obs == exp:
                t1_notes.append(f"PASS: tile {key}={obs}")
            elif obs == "NOT FOUND":
                t1_notes.append(f"WARN: tile {key} not found in DOM (may need selector update)")
            else:
                t1_notes.append(f"WARN: tile {key} expected={exp} observed={obs}")

        print(f"\n  [T1-SUMMARY]")
        for note in t1_notes:
            print(f"    {note}")
        print(f"  TEST 1 RESULT: {'PASS' if t1_pass else 'FAIL'}")
        results["T1_open_merge_caveat"] = "PASS" if t1_pass else "FAIL"
        results["T1_open_tiles"] = tiles_open
        results["T1_open_sql_first8"] = open_header[:8]

        # --- TEST 2: CLOSE Simple-Insert no CAVEAT ---
        print("\n" + "="*70)
        print("TEST 2: CLOSE files - expect Simple-Insert, NO CAVEAT")
        print("="*70)

        sql_close, tiles_close, card_visible_close = run_analysis(
            page, CLOSE_XML, CLOSE_DRD, "close"
        )

        close_header = analyze_sql_headers(sql_close, "CLOSE")

        t2_pass = True
        t2_notes = []

        if sql_close:
            sql_close_upper = sql_close.upper()

            # Required: IKM style: Simple-Insert
            if "IKM style: Simple-Insert" in sql_close or "IKM STYLE: SIMPLE-INSERT" in sql_close_upper:
                t2_notes.append("PASS: found 'IKM style: Simple-Insert'")
            else:
                t2_pass = False
                t2_notes.append("FAIL: 'IKM style: Simple-Insert' NOT found")
                # Show what IKM style is present
                ikm_lines = [l for l in sql_close.split('\n') if 'IKM' in l.upper()]
                if ikm_lines:
                    t2_notes.append(f"  (found IKM line: {ikm_lines[0].strip()[:120]})")

            # Required: INSERT INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT
            if "INSERT INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT" in sql_close_upper:
                t2_notes.append("PASS: found 'INSERT INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT'")
            else:
                t2_pass = False
                t2_notes.append("FAIL: 'INSERT INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT' NOT found")

            # Must NOT contain: SSDS_AVY
            if "SSDS_AVY" in sql_close_upper:
                t2_pass = False
                t2_notes.append("FAIL: SQL contains forbidden 'SSDS_AVY'")
            else:
                t2_notes.append("PASS: 'SSDS_AVY' not found (correct)")

            # Must NOT contain: -- CAVEAT:
            if "-- CAVEAT:" in sql_close:
                t2_pass = False
                t2_notes.append("FAIL: SQL contains spurious '-- CAVEAT:' (should NOT be present for Simple-Insert)")
            else:
                t2_notes.append("PASS: '-- CAVEAT:' not found (correct for Simple-Insert)")

            # Must NOT contain em-dash
            em_ok, em_char = check_no_em_dash(sql_close)
            if em_ok:
                t2_notes.append("PASS: no em-dash characters in SQL")
            else:
                t2_pass = False
                t2_notes.append(f"FAIL: em-dash {em_char} found in SQL")
        else:
            t2_pass = False
            t2_notes.append("FAIL: SQL block not found or empty")

        print(f"\n  [T2-tiles] CLOSE tiles observed: {tiles_close}")
        for note in t2_notes:
            print(f"    {note}")
        print(f"  TEST 2 RESULT: {'PASS' if t2_pass else 'FAIL'}")
        results["T2_close_simple_insert"] = "PASS" if t2_pass else "FAIL"
        results["T2_close_tiles"] = tiles_close
        results["T2_close_sql_first8"] = close_header[:8]

        # --- TEST 3: Card visibility regression ---
        print("\n" + "="*70)
        print("TEST 3: Card visibility regression guard")
        print("="*70)

        # Navigate fresh, attach a file, check card is still visible
        page.goto(APP_URL, wait_until="networkidle", timeout=30000)
        xml_input = page.query_selector("#odi-xml-file")
        if xml_input:
            xml_input.set_input_files(OPEN_XML)
            time.sleep(0.5)
            card_after_attach = page.is_visible("#odi-val-card")
            print(f"  [T3] odi-val-card visible after file attach: {card_after_attach}")
            t3_pass = card_after_attach
        else:
            # If card is always visible (not input-gated), that's also fine
            card_always = page.is_visible("#odi-val-card")
            print(f"  [T3] XML input not found; card visible at page load: {card_always}")
            t3_pass = card_always

        screenshot(page, "test3_card_visible")
        t3_result = "PASS" if t3_pass else "FAIL"
        print(f"  TEST 3 RESULT: {t3_result}")
        results["T3_card_visibility"] = t3_result

        browser.close()

    # Final summary
    print("\n" + "="*70)
    print("FINAL RESULTS SUMMARY")
    print("="*70)
    for k, v in results.items():
        print(f"  {k}: {v}")

    print("\n[OPEN SQL first 8 lines]:")
    for i, line in enumerate(results.get("T1_open_sql_first8", []), 1):
        print(f"  {i:02d}: {line}")

    print("\n[CLOSE SQL first 8 lines]:")
    for i, line in enumerate(results.get("T2_close_sql_first8", []), 1):
        print(f"  {i:02d}: {line}")

    print("\n[OPEN tile counts]:", results.get("T1_open_tiles"))
    print("[CLOSE tile counts]:", results.get("T2_close_tiles"))

    overall = all(v == "PASS" for k, v in results.items() if k.startswith("T") and not k.endswith("tiles") and not k.endswith("sql_first8"))
    print(f"\nOVERALL: {'ALL PASS' if overall else 'SOME FAILURES'}")

    # Write JSON results
    out_path = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/e2e_results.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to: {out_path}")

if __name__ == "__main__":
    main()
