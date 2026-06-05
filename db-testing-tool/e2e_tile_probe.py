"""
Quick Playwright probe using the discovered tile IDs: odi-s-matched, odi-s-alias, etc.
Also verifies SRC_STM_DIM.SRC_STM_CD presence in SQL body and checks for em-dash in full SQL.
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

APP_URL = "http://127.0.0.1:8550/mappings"
OPEN_XML = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
OPEN_DRD = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/Open_lot.csv"
CLOSE_XML = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml"
CLOSE_DRD = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot/closed lot.csv"

TILE_IDS = {
    "matched":      "odi-s-matched",
    "alias":        "odi-s-alias",
    "mismatch":     "odi-s-mismatch",
    "unresolvable": "odi-s-unresolvable",
    "missing":      "odi-s-missing",
    "odi_extra":    "odi-s-extra",
}

def run(page, xml_path, drd_path, label):
    page.goto(APP_URL, wait_until="networkidle", timeout=30000)
    page.query_selector("#odi-xml-file").set_input_files(xml_path)
    time.sleep(0.3)
    page.query_selector("#odi-drd-file").set_input_files(drd_path)
    time.sleep(0.3)
    page.query_selector("#odi-analyze-btn").click()

    # Wait for SQL block to appear
    deadline = time.time() + 90
    while time.time() < deadline:
        el = page.query_selector("#odi-val-card pre")
        if el and len(el.inner_text().strip()) > 100:
            break
        time.sleep(0.5)

    time.sleep(1.5)  # settle

    # Read tiles
    tiles = {}
    for label_key, elem_id in TILE_IDS.items():
        el = page.query_selector(f"#{elem_id}")
        tiles[label_key] = el.inner_text().strip() if el else "NOT FOUND"

    # Read full SQL
    sql_el = page.query_selector("#odi-val-card pre")
    sql_text = sql_el.inner_text().strip() if sql_el else ""

    return tiles, sql_text

def check_em_dash(text):
    for ch in ["—", "–", "‒"]:
        if ch in text:
            return False, repr(ch)
    return True, None

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context(viewport={"width": 1600, "height": 900}).new_page()

    print("\n=== OPEN (MERGE) tile counts + SQL verification ===")
    open_tiles, open_sql = run(page, OPEN_XML, OPEN_DRD, "open")
    print("Tile counts:", open_tiles)

    expected_open = {"matched": "51", "alias": "0", "mismatch": "7", "unresolvable": "3", "missing": "0", "odi_extra": "5"}
    for k, exp in expected_open.items():
        obs = open_tiles[k]
        status = "PASS" if obs == exp else "FAIL"
        print(f"  {status}: tile {k}: expected={exp}, observed={obs}")

    # SRC_STM_DIM.SRC_STM_CD check
    if "SRC_STM_DIM.SRC_STM_CD" in open_sql:
        print("  PASS: SRC_STM_DIM.SRC_STM_CD found in SQL")
        # Find the line
        for line in open_sql.split('\n'):
            if "SRC_STM_DIM" in line:
                print(f"    context line: {line.strip()[:120]}")
                break
    else:
        print("  FAIL: SRC_STM_DIM.SRC_STM_CD NOT found in SQL")

    em_ok, em_ch = check_em_dash(open_sql)
    print(f"  {'PASS' if em_ok else 'FAIL'}: em-dash check (em_ok={em_ok})")

    print("\n=== CLOSE (Simple-Insert) tile counts + SQL verification ===")
    close_tiles, close_sql = run(page, CLOSE_XML, CLOSE_DRD, "close")
    print("Tile counts:", close_tiles)

    for k in TILE_IDS:
        obs = close_tiles[k]
        print(f"  tile {k}: {obs}")

    em_ok2, em_ch2 = check_em_dash(close_sql)
    print(f"  {'PASS' if em_ok2 else 'FAIL'}: em-dash check (em_ok={em_ok2})")

    if "-- CAVEAT:" in close_sql:
        print("  FAIL: spurious CAVEAT found in CLOSE SQL")
    else:
        print("  PASS: no CAVEAT in CLOSE SQL")

    browser.close()

print("\nDone.")
