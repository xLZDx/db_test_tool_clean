"""Gate V9 GUI proof: the Control Table generator's v18 build stages the
wide-projection AVY into a MATERIALIZE'd CTE (Oracle can plan it) while leaving
the small CLOSE/OPEN monoliths untouched.

Drives the LIVE server on :8550 in a real browser (msedge headless), exactly the
multipart POST the Build button issues, for ALL 3 DRDs (operator's "GUI-test every
change on all 3 DRDs" rule):

  V9.AVY    staged===true, generated_sql has "WITH STG AS (", INSERT INTO IKOROSTELEV.AVY_FACT
  V9.CLOSE  staged===false, stage_skip_reason==="below_threshold", no CTE
  V9.OPEN   staged===false, no CTE

Run AFTER a full server restart. Exit 0 only if all pass.
"""
import json
import sys
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
DRDS = [
    ("AVY", f"{TX}/DRD_Activity_Fact.xlsx", "TRANSACTIONS_OWNER", "AVY_FACT", "avy", True),
    ("CLOSE", f"{TX}/DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx", "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT", "taxlot", False),
    ("OPEN", f"{TX}/DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx", "TAXLOT_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT", "taxlot", False),
]

checks = []


def chk(cid, passed, detail=""):
    checks.append((cid, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {cid}: {detail}")


def run():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="msedge")
        pg = b.new_context(viewport={"width": 1400, "height": 1000}).new_page()
        pg.on("dialog", lambda d: d.accept())
        pg.goto(APP, wait_until="networkidle", timeout=30000)

        for label, drd, tsch, ttbl, prof, expect_staged in DRDS:
            print(f"V9.{label} live /build-v18 (expect staged={expect_staged})")
            try:
                pg.query_selector("#ct-drd-file").set_input_files(drd)
                res = pg.evaluate(
                    """async ([tsch, ttbl, prof]) => {
                        const f = document.getElementById('ct-drd-file').files[0];
                        const fd = new FormData();
                        fd.append('drd_file', f);
                        fd.append('target_schema', tsch);
                        fd.append('target_table', ttbl);
                        fd.append('profile', prof);
                        fd.append('control_schema', 'IKOROSTELEV');
                        const r = await fetch('/api/tests/control-table/build-v18', {method:'POST', body: fd});
                        let d = {}; try { d = await r.json(); } catch(_) {}
                        const sql = (d.generated_sql || '').toUpperCase();
                        return {status: r.status, staged: d.staged,
                                skip: d.stage_skip_reason, srcCols: d.stage_source_cols,
                                hasCte: sql.includes('WITH STG AS ('),
                                biz: (d.business_stub_columns||[]).length,
                                nullDrd: (d.null_per_drd_columns||[]).length,
                                target: d.target, len: sql.length};
                    }""", [tsch, ttbl, prof])
                chk(f"V9.{label}.status", res.get("status") == 200, f"HTTP {res.get('status')} target={res.get('target')}")
                chk(f"V9.{label}.staged", res.get("staged") is expect_staged,
                    f"staged={res.get('staged')} (expected {expect_staged}) skip={res.get('skip')} srcCols={res.get('srcCols')}")
                chk(f"V9.{label}.cte", res.get("hasCte") is expect_staged,
                    f"WITH STG AS present={res.get('hasCte')} (expected {expect_staged}); sql_len={res.get('len')}")
                if not expect_staged:
                    chk(f"V9.{label}.skip_reason", res.get("skip") == "below_threshold",
                        f"stage_skip_reason={res.get('skip')}")
                # V4: business stubs reclassified to 0; NULL-per-DRD surfaced instead
                chk(f"V4.{label}.no_business_stubs", res.get("biz") == 0,
                    f"business_stub_columns={res.get('biz')} (expected 0)")
                chk(f"V4.{label}.null_per_drd", res.get("nullDrd") and res.get("nullDrd") > 0,
                    f"null_per_drd_columns={res.get('nullDrd')} (expected >0)")
            except Exception as e:
                chk(f"V9.{label}", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        b.close()


if __name__ == "__main__":
    run()
    npass = sum(1 for _, ok, _ in checks if ok)
    print("\nV9_GUI_RESULT:")
    print(json.dumps({"pass": npass, "total": len(checks),
                      "failed": [c for c, ok, _ in checks if not ok]}, indent=2))
    sys.exit(0 if npass == len(checks) else 1)
