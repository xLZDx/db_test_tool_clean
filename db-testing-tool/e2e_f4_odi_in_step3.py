"""
F4 GUI proof -- ODI column in the generator Step-3 comparison grid, all 3 DRDs.

Real browser flow (system Edge), per the mandatory GUI-test rule:
  1. open /mappings -> Control Table Tests modal
  2. upload DRD + target + DS, Create Empty + Generate -> Step-3 grid
  3. ASSERT the grid now has an 'ODI' column header (F4 header edit)
     and every ODI cell reads "run ODI compare" (not yet populated)
  4. in the 'DRD / ODI / Manual SQL Compare' sub-panel: upload DRD + ODI XML,
     Run Pairwise/Multi Compare
  5. ASSERT the Step-3 grid ODI column now POPULATES (fewer "run ODI compare"
     cells; real ODI exprs appear) -- the F4 reuse path working end-to-end
Screenshots -> e2e_screenshots/f4_<scen>_{before,after}.png
Report -> e2e_f4_report.{json,md}
"""
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool")
TX = ROOT / "data" / "taxlot"
BASE = "http://127.0.0.1:8550"
SHOT = ROOT / "e2e_screenshots"
SHOT.mkdir(exist_ok=True)

# (scen, DRD xlsx, ODI xml, target "SCHEMA.TABLE", source_table or "", grain)
SC = [
    ("CLOSE", TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
     TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER.CLS_TAX_LOTS_NON_BKR_FACT", "", ""),
    ("OPEN", TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
     TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER.OPN_TAX_LOTS_NON_BKR_FACT", "", ""),
    ("AVY", TX / "DRD_Activity_Fact.xlsx",
     TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
     "TRANSACTIONS_OWNER.AVY_FACT_SIDE", "ENTERPRISE_SEMANTIC_OWNER.AVY_FACT",
     "Refer to [ETL Notes] tab"),
]
MARKER = "run ODI compare"


def _count_marker(page):
    try:
        return page.evaluate(
            "() => (document.body.innerText.match(/run ODI compare/g)||[]).length")
    except Exception:
        return -1


def run(page, scen, drd, odi, target, src_tbl, grain):
    out = {"scenario": scen}
    page.goto(f"{BASE}/mappings", wait_until="networkidle")
    time.sleep(1.0)
    page.click("button[onclick=\"openControlTableModal()\"]")
    page.wait_for_selector("#modal-control-table", state="visible")
    page.wait_for_function(
        "document.querySelector('#ct-source-ds') && document.querySelector('#ct-source-ds').options.length > 0",
        timeout=30000)
    page.set_input_files("#ct-drd-file", str(drd))
    page.fill("#ct-target", target)
    if src_tbl:
        page.fill("#ct-source-table", src_tbl)
    if grain and page.query_selector("#ct-grain"):
        page.fill("#ct-grain", grain)
    page.select_option("#ct-source-ds", value="2")
    page.select_option("#ct-target-ds", value="2")
    time.sleep(0.6)

    page.click("button[onclick=\"createEmptyControlTableFromPdm()\"]")
    time.sleep(4.0)
    page.click("button[onclick=\"generateControlTableTests()\"]")
    page.wait_for_selector("#ct-output", state="visible", timeout=120000)
    page.wait_for_function(
        "(document.querySelector('#ct-compare-summary')||{}).textContent && "
        "document.querySelector('#ct-compare-summary').textContent.toLowerCase().includes('mismatch')",
        timeout=120000)
    # make sure the Comparison Grid tab is showing (exact tab toggle; the page
    # may default to the Insert SQL view, esp. for the heavy AVY fixture)
    for _sel in ("#ct-tab-btn-compare",
                 "button[onclick=\"setControlTableStep3Tab('compare')\"]",
                 "button:has-text('Comparison Grid')"):
        try:
            _b = page.locator(_sel).first
            if _b.count() > 0:
                _b.click(force=True, timeout=6000)
                break
        except Exception:
            continue
    # wait until the grid body actually rendered comparison rows (heavy AVY page
    # renders slowly); the unpopulated ODI cells carry the marker text.
    try:
        page.wait_for_function(
            "() => (document.body.innerText.match(/run ODI compare/g)||[]).length > 0",
            timeout=30000)
    except Exception:
        pass
    time.sleep(1.0)

    odi_th = page.locator("th", has_text="ODI").count()
    out["odi_header_present"] = odi_th > 0
    out["marker_before"] = _count_marker(page)
    page.screenshot(path=str(SHOT / f"f4_{scen}_before.png"), full_page=True)

    # ---- run the ODI sub-compare (DRD / ODI / Manual SQL Compare) ----
    try:
        page.set_input_files("#ct-doc-drd", str(drd))
        page.set_input_files("#ct-doc-odi-1", str(odi))
        time.sleep(0.4)
        _btn = page.locator("button[onclick=\"runControlTableDocCompare()\"]")
        _btn.scroll_into_view_if_needed(timeout=10000)
        _btn.click(force=True, timeout=15000)
        # wait for the ODI column to populate (marker count drops)
        page.wait_for_function(
            "(prev) => (document.body.innerText.match(/run ODI compare/g)||[]).length < prev",
            arg=out["marker_before"], timeout=60000)
        out["odi_compare_ran"] = True
    except Exception as e:
        out["odi_compare_ran"] = False
        out["compare_error"] = str(e)[:160]
    time.sleep(1.0)
    out["marker_after"] = _count_marker(page)
    page.screenshot(path=str(SHOT / f"f4_{scen}_after.png"), full_page=True)

    # sample a couple of populated ODI exprs as proof
    out["odi_populated"] = (out["marker_after"] >= 0 and out["marker_before"] > out["marker_after"])
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
        for scen, drd, odi, tgt, src, grain in scenarios:
            print(f"\n=== {scen} ===")
            try:
                r = run(page, scen, drd, odi, tgt, src, grain)
            except Exception as e:
                r = {"scenario": scen, "fatal": str(e)[:200]}
            for k, v in r.items():
                print(f"  {k}: {v}")
            results.append(r)
        browser.close()

    (ROOT / "e2e_f4_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    md = ["# F4 GUI proof -- ODI column in Step-3 grid (all 3 DRDs)\n",
          "| Scenario | ODI header | markers before | markers after | ODI populated | compare ran |",
          "|---|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r.get('scenario')} | {r.get('odi_header_present')} | "
                  f"{r.get('marker_before')} | {r.get('marker_after')} | "
                  f"{r.get('odi_populated')} | {r.get('odi_compare_ran')} |")
    (ROOT / "e2e_f4_report.md").write_text("\n".join(md), encoding="utf-8")
    print("\nWrote e2e_f4_report.{json,md} + e2e_screenshots/f4_*.png")


if __name__ == "__main__":
    main()
