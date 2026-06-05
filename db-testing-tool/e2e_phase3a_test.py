"""
E2E test: Phase-3a transformation-rule verdicts in the ODI vs DRD Validation panel.
Uses Playwright to drive a real browser against http://127.0.0.1:8550/mappings
CLOSE scenario: CLOSE xml + CLOSE drd (xlsx only, no CSV).
"""

import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

APP_URL = "http://127.0.0.1:8550/mappings"
CLOSE_XML = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"
SCREENSHOT_PATH = r"D:\test 2\db-test-tool-analysis\db-testing-tool\e2e_phase3a_screenshot.png"

EXPECTED_TILES = {
    "odi-s-matched":      68,
    "odi-s-alias":         4,
    "odi-s-mismatch":      1,
    "odi-s-unresolvable":  9,
    "odi-s-missing":       1,
    "odi-s-extra":         1,
}

# Filter option values in the #odi-filter-verdict dropdown
FILTER_VALUES = {
    "ALL":          "",
    "MATCHED":      "MATCHED",
    "MISMATCH":     "REAL_MISMATCH",
    "UNRESOLVABLE": "UNRESOLVABLE",
    "MISSING":      "SOURCE_MISSING",
    "EXTRA":        "ODI_EXTRA",
    "ALIAS":        "ALIAS_DRIFT_ONLY",
}

def get_tile_value(page, tile_id):
    loc = page.locator(f"#{tile_id}")
    text = loc.inner_text().strip()
    try:
        return int(text)
    except ValueError:
        return text

def parse_grid_rows(page):
    """
    Parse all visible rows from #odi-grid-tbody.
    Rows are tab-separated cells rendered as inline elements or table cells.
    """
    rows = []
    # The tbody is #odi-grid-tbody; rows are <tr> children
    try:
        page.wait_for_selector("#odi-grid-tbody tr", timeout=15000)
    except PWTimeoutError:
        return rows

    row_els = page.query_selector_all("#odi-grid-tbody tr")
    # Grid columns: TARGET COL | DRD SOURCE | ODI SOURCE | STEP | VERDICT | EXPLANATION | FIX
    # indices:        td[0]     | td[1]      | td[2]      | td[3]| td[4]   | td[5]       | td[6]
    for row_el in row_els:
        cells = row_el.query_selector_all("td")
        if len(cells) >= 5:
            rows.append({
                "target_col":  cells[0].inner_text().strip(),
                "drd_source":  cells[1].inner_text().strip(),
                "odi_source":  cells[2].inner_text().strip(),
                "step":        cells[3].inner_text().strip(),
                "verdict":     cells[4].inner_text().strip(),
                "explanation": cells[5].inner_text().strip() if len(cells) > 5 else "",
            })
    return rows

def set_verdict_filter(page, filter_value):
    """Set the Filter Verdict dropdown using the actual option value strings."""
    page.select_option("#odi-filter-verdict", value=filter_value)
    time.sleep(0.6)

def collect_rows_for_filter(page, filter_value):
    set_verdict_filter(page, filter_value)
    return parse_grid_rows(page)

def get_sql_text(page):
    """Get the full SQL from the odi-sql-pre element."""
    el = page.locator("#odi-sql-pre")
    if el.count() > 0:
        return el.inner_text().strip()
    el2 = page.locator("#odi-sql-wrap")
    if el2.count() > 0:
        return el2.inner_text().strip()
    return ""

def main():
    print("=" * 70)
    print("E2E Phase-3a: ODI vs DRD Validation (CLOSE scenario)")
    print("=" * 70)

    results = {
        "tiles": {},
        "tile_pass": False,
        "rows": {},
        "row_details": {},
        "sql_header_ok": None,
        "ssds_avy_absent": None,
        "card_visible_after_file": None,
        "overall": "FAIL",
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()

        console_errors = []
        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)

        print(f"\n[1] Navigating to {APP_URL}")
        page.goto(APP_URL, wait_until="networkidle", timeout=30000)
        print("    Page loaded OK")

        # Regression guard: card visible before file selection
        card_visible_before = page.locator("#odi-val-card").is_visible()
        print(f"    odi-val-card visible before file selection: {card_visible_before}")

        # ---- Attach files ----
        print("\n[2] Attaching CLOSE XML file...")
        page.locator("#odi-xml-file").set_input_files(CLOSE_XML)
        print(f"    Attached: {Path(CLOSE_XML).name}")

        # Regression guard: card still visible after file selection
        time.sleep(0.4)
        card_visible_after = page.locator("#odi-val-card").is_visible()
        results["card_visible_after_file"] = card_visible_after
        print(f"    odi-val-card visible after file selection: {card_visible_after}")

        print("\n[3] Attaching CLOSE DRD .xlsx file...")
        page.locator("#odi-drd-file").set_input_files(CLOSE_DRD)
        print(f"    Attached: {Path(CLOSE_DRD).name}")

        # Target blank - auto-detect
        target_val = page.locator("#odi-target-table").input_value()
        print(f"    Target input: '{target_val}' (blank = auto-detect)")

        # ---- Click Analyze ----
        print("\n[4] Clicking Analyze button...")
        page.locator("#odi-analyze-btn").click()
        print("    Analyze clicked, waiting for results...")

        # Wait for matched tile to populate
        page.wait_for_function(
            "() => { const el = document.getElementById('odi-s-matched'); return el && el.innerText.trim() !== '' && el.innerText.trim() !== '-' && el.innerText.trim() !== '0'; }",
            timeout=90000
        )
        # Also wait for grid to populate
        try:
            page.wait_for_selector("#odi-grid-tbody tr", timeout=30000)
            print("    Grid rows loaded")
        except PWTimeoutError:
            print("    [WARN] Grid rows timeout - proceeding")
        time.sleep(1.5)
        print("    Results ready")

        # ---- Read tile counts ----
        print("\n[5] Reading tile counts...")
        for tile_id in EXPECTED_TILES:
            val = get_tile_value(page, tile_id)
            results["tiles"][tile_id] = val
            label = tile_id.replace("odi-s-", "").replace("-", " ").title()
            expected = EXPECTED_TILES[tile_id]
            match_icon = "OK" if val == expected else "FAIL"
            print(f"    {label:<15s}: {val:>4}  (expected {expected}) [{match_icon}]")

        # ---- SQL check ----
        print("\n[6] Checking Emitted Oracle INSERT SQL header...")
        sql_text = get_sql_text(page)
        if sql_text:
            lines = sql_text.split("\n")
            # Print first 5 lines for visibility
            for i, line in enumerate(lines[:5]):
                print(f"    SQL line {i+1}: {line[:120]}")
            results["sql_header_ok"] = "IKM style: Simple-Insert (faithful)" in sql_text
            results["ssds_avy_absent"] = "SSDS_AVY" not in sql_text
        else:
            print("    [WARN] SQL text not found")
            results["sql_header_ok"] = None
            results["ssds_avy_absent"] = None
        print(f"    Contains 'IKM style: Simple-Insert (faithful)': {results['sql_header_ok']}")
        print(f"    'SSDS_AVY' absent: {results['ssds_avy_absent']}")

        # ---- Read Comparison Grid rows ----
        print("\n[7] Reading Comparison Grid rows...")
        TARGET_COLS = ["POS_CLS_TP", "OPN_TXN_EV_TP", "ORIG_EV_TP", "LOSS_NOT_ALWD_F",
                       "SRC_STM_CD", "SRC_STM_ID", "SESN_NUM"]
        found_rows = {}

        # Grid title
        grid_title = page.locator("#odi-grid-title").inner_text().strip()
        print(f"    Grid title: {grid_title}")

        # First pass: show all rows
        print("    Pass 1: reading all rows (no filter)...")
        set_verdict_filter(page, FILTER_VALUES["ALL"])
        all_rows = parse_grid_rows(page)
        print(f"    Total rows parsed: {len(all_rows)}")

        for row in all_rows:
            tc = row["target_col"].upper().strip()
            for target in TARGET_COLS:
                if tc == target or tc.startswith(target):
                    found_rows[target] = row

        missing = [t for t in TARGET_COLS if t not in found_rows]
        print(f"    Found: {list(found_rows.keys())}")
        if missing:
            print(f"    Missing after all-rows pass: {missing}")

        # Second pass: filter by specific verdict to find any remaining
        if missing:
            for filter_key, filter_val in [("MATCHED", FILTER_VALUES["MATCHED"]),
                                             ("MISMATCH", FILTER_VALUES["MISMATCH"]),
                                             ("UNRESOLVABLE", FILTER_VALUES["UNRESOLVABLE"])]:
                if not missing:
                    break
                print(f"    Pass 2 [{filter_key}] filter...")
                filtered = collect_rows_for_filter(page, filter_val)
                print(f"    Rows in {filter_key} filter: {len(filtered)}")
                for row in filtered:
                    tc = row["target_col"].upper().strip()
                    for target in missing[:]:
                        if tc == target or tc.startswith(target):
                            found_rows[target] = row
                            missing.remove(target)
                if not missing:
                    break

        # Print what we found
        print("\n    --- Row details found ---")
        for target in TARGET_COLS:
            if target in found_rows:
                row = found_rows[target]
                results["row_details"][target] = row
                print(f"    {target}:")
                print(f"      verdict     : {row['verdict']}")
                print(f"      odi_source  : {row['odi_source'][:80]}")
                print(f"      drd_source  : {row['drd_source'][:80]}")
                if row.get("explanation"):
                    print(f"      explanation : {row['explanation'][:80]}")
            else:
                results["row_details"][target] = None
                print(f"    {target}: NOT FOUND IN GRID")

        # ---- Screenshot ----
        print(f"\n[8] Taking screenshot...")
        set_verdict_filter(page, FILTER_VALUES["ALL"])
        time.sleep(0.5)
        # Scroll to grid section
        grid_sec = page.locator("#odi-grid-section")
        if grid_sec.count() > 0:
            grid_sec.scroll_into_view_if_needed()
            time.sleep(0.3)
        page.screenshot(path=SCREENSHOT_PATH, full_page=False)
        print(f"    Screenshot saved: {SCREENSHOT_PATH}")

        # Console errors
        if console_errors:
            print(f"\n[9] Console errors ({len(console_errors)}):")
            for e in console_errors[:5]:
                print(f"    {e[:100]}")

        browser.close()

    # ---- Evaluate ----
    print("\n" + "=" * 70)
    print("RESULT EVALUATION")
    print("=" * 70)

    # Tile pass
    tile_pass = all(results["tiles"].get(k) == v for k, v in EXPECTED_TILES.items())
    results["tile_pass"] = tile_pass

    # Row verdict pass
    VERDICT_EXPECTED = {
        "POS_CLS_TP":      "MATCHED",
        "OPN_TXN_EV_TP":   "MATCHED",
        "ORIG_EV_TP":      "MATCHED",
        "LOSS_NOT_ALWD_F": "MISMATCH",    # could be REAL_MISMATCH or MISMATCH
        "SRC_STM_CD":      "UNRESOLVABLE",
        "SRC_STM_ID":      "UNRESOLVABLE",
        "SESN_NUM":        "UNRESOLVABLE",
    }

    row_pass = True
    row_results_detail = []
    for target, expected_v in VERDICT_EXPECTED.items():
        row = results["row_details"].get(target)
        if row is None:
            actual_v = "NOT_FOUND"
            ok = False
        else:
            actual_v = row["verdict"].upper().strip()
            # Flexible match: MISMATCH covers REAL_MISMATCH; MATCHED is exact
            if expected_v == "MISMATCH":
                ok = "MISMATCH" in actual_v
            elif expected_v == "UNRESOLVABLE":
                ok = "UNRESOLVABLE" in actual_v
            else:
                ok = expected_v in actual_v or actual_v == expected_v

        if not ok:
            row_pass = False
        odi_src = row["odi_source"][:50] if row else "N/A"
        row_results_detail.append((target, expected_v, actual_v, "PASS" if ok else "FAIL", odi_src))

    sql_ok = results["sql_header_ok"] is True
    ssds_ok = results["ssds_avy_absent"] is True
    card_ok = results["card_visible_after_file"] is True

    overall = tile_pass and row_pass and sql_ok and ssds_ok and card_ok
    results["overall"] = "PASS" if overall else "FAIL"

    # ---- Print final tables ----
    print("\n=== TILE COUNTS ===")
    tile_labels = {
        "odi-s-matched":     "Matched",
        "odi-s-alias":       "Alias Drift",
        "odi-s-mismatch":    "Mismatch",
        "odi-s-unresolvable":"Unresolvable",
        "odi-s-missing":     "Missing",
        "odi-s-extra":       "ODI Extra",
    }
    print(f"{'Metric':<18} {'Actual':>8} {'Expected':>10} {'Status':>8}")
    print("-" * 48)
    for tid, label in tile_labels.items():
        actual = results["tiles"].get(tid, "N/A")
        exp = EXPECTED_TILES[tid]
        status = "PASS" if actual == exp else "FAIL"
        print(f"{label:<18} {str(actual):>8} {str(exp):>10} {status:>8}")

    print(f"\n{'Tile counts overall':<18} {'' :>8} {'' :>10} {'PASS' if tile_pass else 'FAIL':>8}")

    print("\n=== ROW VERDICTS ===")
    print(f"{'TARGET COL':<22} {'EXPECTED':<15} {'ACTUAL':<18} {'STATUS':<6} {'ODI SOURCE (truncated)'}")
    print("-" * 110)
    for target, exp_v, act_v, status, odi_src in row_results_detail:
        print(f"{target:<22} {exp_v:<15} {act_v:<18} {status:<6} {odi_src}")

    print(f"\n{'Row verdicts overall':<22} {'' :<15} {'' :<18} {'PASS' if row_pass else 'FAIL':<6}")

    print("\n=== ADDITIONAL CHECKS ===")
    print(f"SQL header 'IKM style: Simple-Insert (faithful)' present : {'PASS' if sql_ok else 'FAIL'}")
    print(f"'SSDS_AVY' absent from emitted SQL                       : {'PASS' if ssds_ok else 'FAIL'}")
    print(f"odi-val-card visible after file selection (regr. guard)  : {'PASS' if card_ok else 'FAIL'}")

    print(f"\n{'=' * 70}")
    print(f"OVERALL: {results['overall']}")
    print(f"{'=' * 70}")

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
