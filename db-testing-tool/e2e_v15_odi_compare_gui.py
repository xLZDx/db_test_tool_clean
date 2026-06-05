"""R3 GUI proof -- v15 Compare button in the ODI-vs-DRD Validation panel.

Real browser (system Edge), mandatory GUI-test rule. For each of the 3 DRDs:
  1. open /mappings, ensure the 'ODI vs DRD Validation' section is expanded
  2. attach ODI XML + DRD xlsx
  3. click the new 'v15 Compare' button (#odi-v15-btn)
  4. wait for #odi-v15-result, read the 4 count tiles
  5. ASSERT they match the gold reference (AVY 373/369/4/0, CLOSE 84/83/1/0, OPEN 66/66/0/0)
Screenshots -> e2e_screenshots/v15_<scen>.png ; report -> e2e_v15_report.{json,md}
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

SC = [
    ("AVY", TX / "DRD_Activity_Fact.xlsx",
     TX / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
     dict(mapping=373, inboth=369, drdonly=4, odionly=0)),
    ("CLOSE", TX / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
     TX / "SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     dict(mapping=84, inboth=83, drdonly=1, odionly=0)),
    ("OPEN", TX / "DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
     TX / "SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     dict(mapping=66, inboth=66, drdonly=0, odionly=0)),
]


def _txt(page, sel):
    try:
        return int(page.locator(sel).inner_text().strip())
    except Exception:
        return -1


def run(page, scen, drd, odi, exp):
    out = {"scenario": scen, "expected": exp}
    page.goto(f"{BASE}/mappings", wait_until="networkidle")
    time.sleep(0.6)
    # expand the ODI section if its body is hidden
    try:
        vis = page.locator("#odi-val-body").is_visible()
        if not vis:
            page.click("#odi-val-toggle", force=True)
            time.sleep(0.3)
    except Exception:
        pass
    page.set_input_files("#odi-xml-file", str(odi))
    page.set_input_files("#odi-drd-file", str(drd))
    time.sleep(0.3)
    btn = page.locator("#odi-v15-btn")
    btn.scroll_into_view_if_needed(timeout=10000)
    btn.click(force=True, timeout=15000)
    try:
        page.wait_for_selector("#odi-v15-result", state="visible", timeout=120000)
        page.wait_for_function(
            "() => (document.getElementById('odi-v15-mapping')||{}).textContent "
            "&& document.getElementById('odi-v15-mapping').textContent !== '0'",
            timeout=120000)
    except Exception as e:
        out["wait_error"] = str(e)[:160]
    time.sleep(0.5)
    got = dict(
        mapping=_txt(page, "#odi-v15-mapping"),
        inboth=_txt(page, "#odi-v15-inboth"),
        drdonly=_txt(page, "#odi-v15-drdonly"),
        odionly=_txt(page, "#odi-v15-odionly"),
    )
    out["got"] = got
    out["match"] = (got == exp)
    out["layout"] = page.locator("#odi-v15-layout").inner_text()
    out["diff_rows"] = page.locator("#odi-v15-diffbody tr").count()
    page.screenshot(path=str(SHOT / f"v15_{scen}.png"), full_page=True)
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
        for scen, drd, odi, exp in scenarios:
            print(f"\n=== {scen} ===")
            try:
                r = run(page, scen, drd, odi, exp)
            except Exception as e:
                r = {"scenario": scen, "fatal": str(e)[:200]}
            for k, v in r.items():
                print(f"  {k}: {v}")
            results.append(r)
        browser.close()
    (ROOT / "e2e_v15_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    md = ["# R3 v15 Compare GUI proof (all 3 DRDs)\n",
          "| Scenario | expected (m/in/drd/odi) | got | MATCH | diff rows | layout |",
          "|---|---|---|---|---|---|"]
    for r in results:
        e = r.get("expected", {}); g = r.get("got", {})
        md.append(f"| {r.get('scenario')} | "
                  f"{e.get('mapping')}/{e.get('inboth')}/{e.get('drdonly')}/{e.get('odionly')} | "
                  f"{g.get('mapping')}/{g.get('inboth')}/{g.get('drdonly')}/{g.get('odionly')} | "
                  f"{r.get('match')} | {r.get('diff_rows')} | {r.get('layout','')} |")
    (ROOT / "e2e_v15_report.md").write_text("\n".join(md), encoding="utf-8")
    allmatch = all(r.get("match") for r in results)
    print("\nALL MATCH:", allmatch)
    print("Wrote e2e_v15_report.{json,md} + e2e_screenshots/v15_*.png")


if __name__ == "__main__":
    main()
