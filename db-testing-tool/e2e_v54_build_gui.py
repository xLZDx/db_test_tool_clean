"""Gate G3 GUI proof -- v5.4 DRD-driven build wired into Step-3 (real Edge).

For CLOSE + OPEN + AVY: open the control-table modal, upload DRD + target + ds,
Create Empty + Generate (reach Step-3), switch to the Insert SQL tab, upload the
ODI XML, click 'Build v5.4', and assert:
  - #ct-insert-sql gets a clean DRD-driven INSERT (has INSERT INTO, no `O.`,
    no odi_final_source);
  - #ct-v54-result shows the 3-way: built-from-DRD == total, differences ONLY
    vs ODI (the success criterion).
Screenshots -> e2e_screenshots/v54_<scen>.png ; report -> e2e_v54_report.md
"""
import json
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool")
TX = ROOT / "data" / "taxlot"
BASE = "http://127.0.0.1:8550"
SHOT = ROOT / "e2e_screenshots"
SHOT.mkdir(exist_ok=True)

SC = [
    ("CLOSE", TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
     TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT"),
    ("OPEN", TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
     TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER.OPN_TAX_LOTS_NON_BKR_FACT"),
    ("AVY", TX / "DRD_Activity_Fact.xlsx",
     TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
     "TRANSACTIONS_OWNER.AVY_FACT"),
]


def run(page, scen, drd, odi, target):
    out = {"scenario": scen}
    page.goto(f"{BASE}/mappings", wait_until="networkidle")
    time.sleep(0.6)
    page.click("button[onclick=\"openControlTableModal()\"]")
    page.wait_for_selector("#modal-control-table", state="visible")
    page.wait_for_function(
        "document.querySelector('#ct-source-ds') && document.querySelector('#ct-source-ds').options.length > 0",
        timeout=30000)
    page.set_input_files("#ct-drd-file", str(drd))
    page.fill("#ct-target", target)
    page.select_option("#ct-source-ds", value="2")
    page.select_option("#ct-target-ds", value="2")
    time.sleep(0.5)
    page.click("button[onclick=\"createEmptyControlTableFromPdm()\"]")
    time.sleep(4.0)
    page.click("button[onclick=\"generateControlTableTests()\"]")
    page.wait_for_selector("#ct-output", state="visible", timeout=120000)
    page.wait_for_function(
        "(document.querySelector('#ct-compare-summary')||{}).textContent && "
        "document.querySelector('#ct-compare-summary').textContent.toLowerCase().includes('mismatch')",
        timeout=120000)
    # switch to Insert SQL tab
    page.click("#ct-tab-btn-insert", force=True)
    time.sleep(0.5)
    # upload ODI + click Build v5.4
    page.set_input_files("#ct-v54-odi-file", str(odi))
    time.sleep(0.3)
    page.locator("#ct-v54-btn").scroll_into_view_if_needed(timeout=10000)
    page.click("#ct-v54-btn", force=True)
    page.wait_for_function(
        "() => { const r=document.getElementById('ct-v54-result');"
        "return r && /DRD-driven/.test(r.innerHTML) && !/Building DRD-driven/.test(r.innerHTML); }",
        timeout=120000)
    time.sleep(0.6)
    sql = page.eval_on_selector("#ct-insert-sql", "el => el.value")
    result_txt = page.eval_on_selector("#ct-v54-result", "el => el.innerText")
    out["insert_has_INSERT_INTO"] = "INSERT INTO" in sql.upper()
    out["insert_has_O_dot"] = bool(re.search(r"\bO\.", sql))
    out["insert_has_odi_final"] = "odi_final_source" in sql
    out["joins"] = len(re.findall(r"\bJOIN\b", sql))
    out["result_text"] = " ".join(result_txt.split())[:240]
    out["three_way_ok"] = ("differences ONLY vs ODI" in result_txt) or ("3-way OK" in result_txt)
    page.screenshot(path=str(SHOT / f"v54_{scen}.png"), full_page=True)
    out["PASS"] = (out["insert_has_INSERT_INTO"] and not out["insert_has_O_dot"]
                   and not out["insert_has_odi_final"] and out["joins"] > 0 and out["three_way_ok"])
    return out


def main():
    import sys
    only = {a.upper() for a in sys.argv[1:]}
    scenarios = [s for s in SC if (not only or s[0] in only)]
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1680, "height": 1300})
        page.set_default_timeout(120000)
        for scen, drd, odi, tgt in scenarios:
            print(f"\n=== {scen} ===")
            try:
                r = run(page, scen, drd, odi, tgt)
            except Exception as e:
                r = {"scenario": scen, "fatal": str(e)[:200]}
            for k, v in r.items():
                print(f"  {k}: {v}")
            results.append(r)
        browser.close()
    (ROOT / "e2e_v54_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    md = ["# G3 v5.4 build GUI proof\n",
          "| Scenario | PASS | INSERT INTO | no O. | no odi_final | joins | 3-way |",
          "|---|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r.get('scenario')} | {r.get('PASS')} | {r.get('insert_has_INSERT_INTO')} | "
                  f"{not r.get('insert_has_O_dot', True)} | {not r.get('insert_has_odi_final', True)} | "
                  f"{r.get('joins')} | {r.get('three_way_ok')} |")
    (ROOT / "e2e_v54_report.md").write_text("\n".join(md), encoding="utf-8")
    print("\nALL PASS:", all(r.get("PASS") for r in results))


if __name__ == "__main__":
    main()
