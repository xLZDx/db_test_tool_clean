"""
GENERATOR GUI harness -- the MANDATORY clean-slate Playwright test on ALL 3 DRDs.

Drives the real "ODI vs DRD Validation" panel at /mappings exactly as the operator
does: attach ODI scenario XML + DRD mapping file, click Analyze, then read the
REAL emitted INSERT target line (#odi-sql-pre) + the 6 verdict tiles + status
badge from the live DOM. This is the harness that was missing (todo #7); it
replaces the stale e2e_odi_drd_test.py (which pointed at deleted CSV DRDs).

Run AFTER a clean-slate uvicorn restart so the code under test is actually loaded.

Usage:
    .venv/Scripts/python.exe e2e_generator_3drd_gui.py
Outputs:
    e2e_generator_3drd_report.json   (machine)
    e2e_generator_3drd_report.md     (human)  + .csv twin
    e2e_screenshots/gen_<scen>.png   (proof per scenario)
"""
import json
import os
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool")
TAXLOT = ROOT / "data" / "taxlot"
APP_URL = "http://127.0.0.1:8550/mappings"
SHOT_DIR = ROOT / "e2e_screenshots"
SHOT_DIR.mkdir(exist_ok=True)

# (scenario, ODI scenario XML, DRD mapping file, expected physical target substring)
SCENARIOS = [
    ("CLOSE",
     TAXLOT / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     TAXLOT / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
     "CLS_TAX_LOTS_NON_BKR_FACT"),
    ("OPEN",
     TAXLOT / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     TAXLOT / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
     "OPN_TAX_LOTS_NON_BKR_FACT"),
    ("AVY",
     TAXLOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
     TAXLOT / "DRD_Activity_Fact.xlsx",
     None),  # AVY target not asserted yet -- this is the FK-map/D work
]

TILE_IDS = {
    "matched": "#odi-s-matched",
    "alias_drift": "#odi-s-alias",
    "mismatch": "#odi-s-mismatch",
    "unresolvable": "#odi-s-unresolvable",
    "missing": "#odi-s-missing",
    "odi_extra": "#odi-s-extra",
}


def _txt(page, sel):
    try:
        loc = page.locator(sel)
        if loc.count() == 0:
            return None
        return loc.first.inner_text().strip()
    except Exception as e:
        return f"<err:{e}>"


def run_scenario(page, scen, xml_path, drd_path, expect_target):
    out = {"scenario": scen, "xml": xml_path.name, "drd": drd_path.name,
           "expect_target": expect_target}
    page.goto(APP_URL, wait_until="networkidle")
    # Ensure the panel body is expanded
    try:
        if not page.locator("#odi-val-body").is_visible():
            page.locator("text=ODI vs DRD Validation").first.click()
            page.wait_for_timeout(300)
    except Exception:
        pass

    page.locator("#odi-xml-file").set_input_files(str(xml_path))
    page.wait_for_timeout(200)
    page.locator("#odi-drd-file").set_input_files(str(drd_path))
    page.wait_for_timeout(200)
    page.locator("#odi-analyze-btn").click()

    # Wait for the emitted SQL pre to fill OR an error/status to settle
    sql = ""
    try:
        page.wait_for_function(
            "() => { const p=document.querySelector('#odi-sql-pre');"
            " return p && p.innerText && p.innerText.includes('INSERT'); }",
            timeout=45000)
        sql = page.locator("#odi-sql-pre").inner_text().strip()
    except Exception as e:
        out["wait_error"] = str(e)
        sql = _txt(page, "#odi-sql-pre") or ""

    out["status_badge"] = _txt(page, "#odi-status-badge")
    out["sql_first_line"] = (sql.splitlines()[0].strip() if sql else "<empty>")
    out["sql_len_chars"] = len(sql)

    # Extract emitted physical target from the INSERT INTO line
    m = re.search(r"INSERT\s+INTO\s+([A-Za-z0-9_.\"]+)", sql, re.IGNORECASE)
    out["emitted_target"] = m.group(1) if m else None

    # 6 tiles
    out["tiles"] = {k: _txt(page, sel) for k, sel in TILE_IDS.items()}

    # Field-level (what the operator sees typed in the target box)
    try:
        out["target_table_field"] = page.locator("#odi-target-table").input_value()
    except Exception:
        out["target_table_field"] = None

    # Dead-join smell test
    out["dead_joins_on_1_eq_0"] = sql.count("1 = 0") + sql.count("1=0")

    # Verdict for this scenario
    if expect_target:
        et = (out["emitted_target"] or "").upper()
        out["target_ok"] = expect_target.upper() in et
        out["target_is_reject_table"] = "_RJT" in et or "FACT_RJT" in et
    else:
        out["target_ok"] = None
        out["target_is_reject_table"] = None

    page.screenshot(path=str(SHOT_DIR / f"gen_{scen}.png"), full_page=True)
    return out


def main():
    results = []
    with sync_playwright() as p:
        _shell = r"C:\Users\koros\AppData\Local\ms-playwright\chromium_headless_shell-1217\chrome-headless-shell-win64\chrome-headless-shell.exe"
        _full = r"C:\Users\koros\AppData\Local\ms-playwright\chromium-1217\chrome-win64\chrome.exe"
        _exe = _shell if os.path.exists(_shell) else (_full if os.path.exists(_full) else None)
        browser = p.chromium.launch(headless=True, executable_path=_exe)
        page = browser.new_context(viewport={"width": 1500, "height": 1100}).new_page()
        for scen, xml, drd, tgt in SCENARIOS:
            print(f"\n=== {scen} ===")
            r = run_scenario(page, scen, xml, drd, tgt)
            for k in ("status_badge", "sql_first_line", "emitted_target",
                      "target_ok", "target_is_reject_table",
                      "dead_joins_on_1_eq_0", "tiles", "target_table_field"):
                print(f"  {k}: {r.get(k)}")
            results.append(r)
        browser.close()

    (ROOT / "e2e_generator_3drd_report.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")

    # MD + CSV twin
    md = ["# Generator GUI test -- 3 DRDs (clean-slate uvicorn)\n",
          f"_ran: {time.strftime('%Y-%m-%d %H:%M:%S')} local_\n",
          "| Scenario | Emitted target | F3 target OK | Reject-table? | Dead 1=0 joins | Matched | Mismatch | Unresolvable | Missing | Status |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    csv = ["scenario,emitted_target,target_ok,reject_table,dead_joins,matched,mismatch,unresolvable,missing,status"]
    for r in results:
        t = r["tiles"]
        md.append(f"| {r['scenario']} | {r.get('emitted_target')} | {r.get('target_ok')} "
                  f"| {r.get('target_is_reject_table')} | {r.get('dead_joins_on_1_eq_0')} "
                  f"| {t.get('matched')} | {t.get('mismatch')} | {t.get('unresolvable')} "
                  f"| {t.get('missing')} | {r.get('status_badge')} |")
        csv.append(f"{r['scenario']},{r.get('emitted_target')},{r.get('target_ok')},"
                   f"{r.get('target_is_reject_table')},{r.get('dead_joins_on_1_eq_0')},"
                   f"{t.get('matched')},{t.get('mismatch')},{t.get('unresolvable')},"
                   f"{t.get('missing')},{r.get('status_badge')}")
    (ROOT / "e2e_generator_3drd_report.md").write_text("\n".join(md), encoding="utf-8")
    (ROOT / "e2e_generator_3drd_report.csv").write_text("\n".join(csv), encoding="utf-8")
    print("\nWrote e2e_generator_3drd_report.{json,md,csv} + e2e_screenshots/gen_*.png")


if __name__ == "__main__":
    main()
