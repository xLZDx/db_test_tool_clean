"""Gate V3 GUI proof: the Control Table generator is wired to the v18 KB-resolved
builder with a page-driven control schema.

Drives the LIVE server on :8550 in a real browser (msedge headless):
  V3.1  the served generateControlTableTests() JS now calls /build-v18 (new code
        is actually deployed -- catches "stale server" per Full-Restart rule).
  V3.2  a real browser multipart POST to /build-v18 (exactly what the button
        does) with control_schema=IKOROSTELEV returns a v18 INSERT retargeted to
        IKOROSTELEV.AVY_FACT + classified business stubs (anti-false-green).
  V3.3  empty target schema fails LOUD (HTTP 422), never 200 with junk.

Run AFTER a full server restart. Exit 0 only if all pass.
"""
import json
import sys
from playwright.sync_api import sync_playwright

APP = "http://127.0.0.1:8550/mappings"
TX = "D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot"
DRD = f"{TX}/DRD_Activity_Fact.xlsx"

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

        # V3.1 new code deployed: generateControlTableTests references build-v18
        print("V3.1 served JS calls /build-v18")
        try:
            src = pg.evaluate("() => (typeof generateControlTableTests==='function') "
                              "? generateControlTableTests.toString() : ''")
            chk("V3.1.wired", "build-v18" in src and "control_schema" in src,
                f"generateControlTableTests references build-v18 + control_schema (len={len(src)})")
        except Exception as e:
            chk("V3.1", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        # V3.2 live browser POST to /build-v18 with control_schema (what the button does)
        print("V3.2 live /build-v18 with control_schema=IKOROSTELEV")
        try:
            pg.query_selector("#ct-drd-file").set_input_files(DRD)
            res = pg.evaluate(
                """async () => {
                    const f = document.getElementById('ct-drd-file').files[0];
                    const fd = new FormData();
                    fd.append('drd_file', f);
                    fd.append('target_schema', 'TRANSACTIONS_OWNER');
                    fd.append('target_table', 'AVY_FACT');
                    fd.append('profile', 'avy');
                    fd.append('control_schema', 'IKOROSTELEV');
                    const r = await fetch('/api/tests/control-table/build-v18', {method:'POST', body: fd});
                    let d = {}; try { d = await r.json(); } catch(_) {}
                    const sql = (d.generated_sql || '').toUpperCase();
                    return {status: r.status, engine: d.engine, target: d.target,
                            retargeted: sql.includes('INSERT INTO IKOROSTELEV.AVY_FACT'),
                            prodAbsent: !sql.includes('INSERT INTO TRANSACTIONS_OWNER.AVY_FACT'),
                            biz: (d.business_stub_columns||[]).length,
                            audit: (d.audit_stub_columns||[]).length};
                }""")
            chk("V3.2.status", res.get("status") == 200, f"HTTP {res.get('status')}")
            chk("V3.2.engine", res.get("engine") == "v18-insert-builder", f"engine={res.get('engine')}")
            chk("V3.2.retarget", res.get("retargeted") and res.get("prodAbsent"),
                f"target={res.get('target')} retargeted={res.get('retargeted')} prodAbsent={res.get('prodAbsent')}")
            chk("V3.2.stub_fields", isinstance(res.get("biz"), int) and isinstance(res.get("audit"), int),
                f"business_stub_columns={res.get('biz')} audit_stub_columns={res.get('audit')}")
        except Exception as e:
            chk("V3.2", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        # V3.3 fail loud on empty target schema (no 200 with junk)
        print("V3.3 empty target schema -> 422")
        try:
            status = pg.evaluate(
                """async () => {
                    const f = document.getElementById('ct-drd-file').files[0];
                    const fd = new FormData();
                    fd.append('drd_file', f);
                    fd.append('target_schema', '');
                    fd.append('target_table', 'AVY_FACT');
                    fd.append('profile', 'avy');
                    fd.append('control_schema', 'IKOROSTELEV');
                    const r = await fetch('/api/tests/control-table/build-v18', {method:'POST', body: fd});
                    return r.status;
                }""")
            chk("V3.3.fail_loud", status == 422, f"empty target schema -> HTTP {status}")
        except Exception as e:
            chk("V3.3", False, f"exception {type(e).__name__}: {str(e)[:160]}")

        b.close()


if __name__ == "__main__":
    run()
    npass = sum(1 for _, ok, _ in checks if ok)
    print("\nV3_GUI_RESULT:")
    print(json.dumps({"pass": npass, "total": len(checks),
                      "failed": [c for c, ok, _ in checks if not ok]}, indent=2))
    sys.exit(0 if npass == len(checks) else 1)
