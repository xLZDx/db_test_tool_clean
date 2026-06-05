"""
E2E Playwright test for ODI vs DRD Validation card.
Tests A (OPEN) and B (CLOSE) with expected tile counts and row verdicts.
"""

import sys
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

BASE_URL = "http://127.0.0.1:8550/mappings"

OPEN_XML  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
OPEN_DRD  = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx"
CLOSE_XML = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot\DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"

EXPECTED_OPEN = {
    "matched": 65, "alias": 0, "mismatch": 1,
    "unresolvable": 0, "missing": 0, "extra": 0
}
EXPECTED_CLOSE = {
    "matched": 77, "alias": 0, "mismatch": 1,
    "unresolvable": 4, "missing": 1, "extra": 1
}

EXPECTED_OPEN_ROWS = {
    "SRC_RCRD_TP_CD": "MATCHED",
    "WASH_SALE_TP":   "REAL_MISMATCH",
}
EXPECTED_CLOSE_ROWS = {
    "ACG_TP_NM":                    "MATCHED",
    "TAX_LOT_EXT_REFR_KEY_QTRNY":   "MATCHED",
    "SESN_NUM":                     "MATCHED",
    "CRT_DTM":                      "MATCHED",
    "SRC_STM_CD":                   "UNRESOLVABLE",
    "SRC_STM_ID":                   "UNRESOLVABLE",
}

VERDICT_FILTER_MAP = {
    "MATCHED":        "MATCHED",
    "REAL_MISMATCH":  "REAL_MISMATCH",
    "UNRESOLVABLE":   "UNRESOLVABLE",
    "ALIAS_DRIFT_ONLY": "ALIAS_DRIFT_ONLY",
    "SOURCE_MISSING": "SOURCE_MISSING",
    "ODI_EXTRA":      "ODI_EXTRA",
}


def run_analysis(page, xml_path, drd_path, label):
    """Load the page, attach files, click Analyze, wait for grid, return data."""
    print(f"\n  Loading {BASE_URL} for {label}...")
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_selector("#odi-xml-file", timeout=15000)

    # Expand the card if collapsed (click header if body not visible)
    body = page.locator("#odi-val-body")
    if not body.is_visible():
        page.locator("#odi-val-card .card-header").click()
        page.wait_for_timeout(500)

    # Attach XML file
    page.locator("#odi-xml-file").set_input_files(xml_path)
    print(f"  XML attached: {Path(xml_path).name}")

    # Attach DRD file
    page.locator("#odi-drd-file").set_input_files(drd_path)
    print(f"  DRD attached: {Path(drd_path).name}")

    # Clear target fields (leave blank for auto-detect)
    page.locator("#odi-target-schema").fill("")
    page.locator("#odi-target-table").fill("")

    # Intercept the API response to know exactly when analysis completes
    # by waiting for the grid section to become visible after clicking Analyze.
    # Strategy: wait for #odi-grid-section to appear (state="visible").
    # The loading div starts hidden -> becomes visible briefly -> hidden again.
    # We cannot rely on the loading spinner; we wait for the result container.
    print("  Clicking Analyze...")
    # First ensure the grid is hidden so our wait is fresh
    page.evaluate("document.getElementById('odi-grid-section').style.display='none'")
    page.evaluate("document.getElementById('odi-summary').style.display='none'")
    page.locator("#odi-analyze-btn").click()

    # Wait for the summary tiles div to become visible (analysis complete)
    page.wait_for_selector("#odi-summary", state="visible", timeout=90000)
    page.wait_for_selector("#odi-grid-section", state="visible", timeout=90000)
    # Give JS a moment to fully render all rows
    page.wait_for_timeout(1000)
    print("  Analysis complete, grid visible.")


def read_tiles(page):
    """Read the 6 summary tile counts."""
    tiles = {}
    for tile_id, key in [
        ("odi-s-matched",      "matched"),
        ("odi-s-alias",        "alias"),
        ("odi-s-mismatch",     "mismatch"),
        ("odi-s-unresolvable", "unresolvable"),
        ("odi-s-missing",      "missing"),
        ("odi-s-extra",        "extra"),
    ]:
        text = page.locator(f"#{tile_id}").inner_text().strip()
        try:
            tiles[key] = int(text)
        except ValueError:
            tiles[key] = text
    return tiles


def find_row_verdict(page, target_col, filter_verdict_value):
    """
    Filter the grid by the verdict, then search for target_col in rows.
    Returns (verdict_text, odi_source_text) or (None, None) if not found.
    """
    # Set filter
    page.locator("#odi-filter-verdict").select_option(filter_verdict_value)
    time.sleep(0.5)

    # Scan grid rows
    rows = page.locator("#odi-grid-tbody tr").all()
    for row in rows:
        cells = row.locator("td").all()
        if not cells:
            continue
        col_name = cells[0].inner_text().strip()
        if col_name == target_col:
            verdict_text = cells[4].inner_text().strip() if len(cells) > 4 else ""
            odi_source   = cells[2].inner_text().strip() if len(cells) > 2 else ""
            return verdict_text, odi_source

    # Not found under this filter - try "All verdicts"
    page.locator("#odi-filter-verdict").select_option("")
    time.sleep(0.5)
    rows = page.locator("#odi-grid-tbody tr").all()
    for row in rows:
        cells = row.locator("td").all()
        if not cells:
            continue
        col_name = cells[0].inner_text().strip()
        if col_name == target_col:
            verdict_text = cells[4].inner_text().strip() if len(cells) > 4 else ""
            odi_source   = cells[2].inner_text().strip() if len(cells) > 2 else ""
            return verdict_text, odi_source

    return None, None


def normalize_verdict(verdict_text):
    """Normalize displayed verdict text to the internal key."""
    v = verdict_text.upper().strip()
    # Possible display forms: MATCHED, REAL_MISMATCH, MISMATCH, UNRESOLVABLE, etc.
    if "MISMATCH" in v and "REAL" in v:
        return "REAL_MISMATCH"
    if "MISMATCH" in v:
        return "REAL_MISMATCH"
    if "UNRESOLVABLE" in v:
        return "UNRESOLVABLE"
    if "ALIAS" in v and "DRIFT" in v:
        return "ALIAS_DRIFT_ONLY"
    if "MISSING" in v:
        return "SOURCE_MISSING"
    if "EXTRA" in v:
        return "ODI_EXTRA"
    if "MATCHED" in v:
        return "MATCHED"
    return v


def compare_tiles(actual, expected, label):
    """Compare tile counts, return list of (key, actual, expected, pass/fail)."""
    results = []
    for key in ["matched", "alias", "mismatch", "unresolvable", "missing", "extra"]:
        a = actual.get(key, "N/A")
        e = expected.get(key, "N/A")
        results.append((key, a, e, a == e))
    return results


def main():
    all_pass = True
    report_lines = []

    def log(line=""):
        print(line)
        report_lines.append(line)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        # ------------------------------------------------------------------ #
        # TEST A — OPEN
        # ------------------------------------------------------------------ #
        log("=" * 70)
        log("TEST A (OPEN)")
        log("=" * 70)

        try:
            run_analysis(page, OPEN_XML, OPEN_DRD, "TEST A OPEN")

            # Screenshot
            ss_path_a = r"D:\test 2\db-test-tool-analysis\db-testing-tool\test_a_open_result.png"
            page.screenshot(path=ss_path_a, full_page=False)
            log(f"  Screenshot saved: {ss_path_a}")

            # Tile counts
            tiles_a = read_tiles(page)
            log("\nTile Counts (TEST A — OPEN):")
            log(f"  {'Metric':<16} {'Actual':>8} {'Expected':>10} {'Pass/Fail':>10}")
            log("  " + "-" * 50)
            tile_results_a = compare_tiles(tiles_a, EXPECTED_OPEN, "A")
            for key, actual, expected, passed in tile_results_a:
                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_pass = False
                log(f"  {key:<16} {str(actual):>8} {str(expected):>10} {status:>10}")

            # Row verdicts
            log("\nRow Verdicts (TEST A — OPEN):")
            log(f"  {'Column':<36} {'Actual Verdict':<22} {'Expected':<22} {'ODI Source':<40} {'Pass/Fail':>10}")
            log("  " + "-" * 140)

            row_results_a = {}
            for col, exp_verdict in EXPECTED_OPEN_ROWS.items():
                # Use the expected verdict as the filter value
                filter_val = VERDICT_FILTER_MAP.get(exp_verdict, "")
                verdict_txt, odi_src = find_row_verdict(page, col, filter_val)

                if verdict_txt is None:
                    row_results_a[col] = ("NOT FOUND", None)
                    log(f"  {col:<36} {'NOT FOUND':<22} {exp_verdict:<22} {'N/A':<40} {'FAIL':>10}")
                    all_pass = False
                else:
                    norm = normalize_verdict(verdict_txt)
                    passed = (norm == exp_verdict)
                    if not passed:
                        all_pass = False
                    status = "PASS" if passed else "FAIL"
                    row_results_a[col] = (norm, odi_src)
                    log(f"  {col:<36} {norm:<22} {exp_verdict:<22} {odi_src[:38]:<40} {status:>10}")

        except Exception as e:
            log(f"  ERROR during TEST A: {e}")
            all_pass = False
            import traceback
            traceback.print_exc()

        # ------------------------------------------------------------------ #
        # TEST B — CLOSE
        # ------------------------------------------------------------------ #
        log("")
        log("=" * 70)
        log("TEST B (CLOSE)")
        log("=" * 70)

        try:
            run_analysis(page, CLOSE_XML, CLOSE_DRD, "TEST B CLOSE")

            # Screenshot
            ss_path_b = r"D:\test 2\db-test-tool-analysis\db-testing-tool\test_b_close_result.png"
            page.screenshot(path=ss_path_b, full_page=False)
            log(f"  Screenshot saved: {ss_path_b}")

            # Tile counts
            tiles_b = read_tiles(page)
            log("\nTile Counts (TEST B — CLOSE):")
            log(f"  {'Metric':<16} {'Actual':>8} {'Expected':>10} {'Pass/Fail':>10}")
            log("  " + "-" * 50)
            tile_results_b = compare_tiles(tiles_b, EXPECTED_CLOSE, "B")
            for key, actual, expected, passed in tile_results_b:
                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_pass = False
                log(f"  {key:<16} {str(actual):>8} {str(expected):>10} {status:>10}")

            # Row verdicts
            log("\nRow Verdicts (TEST B — CLOSE):")
            log(f"  {'Column':<36} {'Actual Verdict':<22} {'Expected':<22} {'Pass/Fail':>10}")
            log("  " + "-" * 100)

            row_results_b = {}
            for col, exp_verdict in EXPECTED_CLOSE_ROWS.items():
                filter_val = VERDICT_FILTER_MAP.get(exp_verdict, "")
                verdict_txt, odi_src = find_row_verdict(page, col, filter_val)

                if verdict_txt is None:
                    row_results_b[col] = ("NOT FOUND", None)
                    log(f"  {col:<36} {'NOT FOUND':<22} {exp_verdict:<22} {'FAIL':>10}")
                    all_pass = False
                else:
                    norm = normalize_verdict(verdict_txt)
                    passed = (norm == exp_verdict)
                    if not passed:
                        all_pass = False
                    status = "PASS" if passed else "FAIL"
                    row_results_b[col] = (norm, odi_src)
                    log(f"  {col:<36} {norm:<22} {exp_verdict:<22} {status:>10}")

        except Exception as e:
            log(f"  ERROR during TEST B: {e}")
            all_pass = False
            import traceback
            traceback.print_exc()

        browser.close()

    log("")
    log("=" * 70)
    overall = "PASS" if all_pass else "FAIL"
    log(f"OVERALL RESULT: {overall}")
    log("=" * 70)

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
