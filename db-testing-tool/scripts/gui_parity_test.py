"""GUI parity test: drives the same /api/odi/scenario/compare endpoint that
the UI hits, captures its drd_first_insert SQL, diffs against v9, and saves
both artefacts as a UI test_suite.

Proves the deterministic-emission claim: same files in the GUI -> same v9 SQL.
"""
from __future__ import annotations

import asyncio
import json
import sys
import hashlib
import difflib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.main import app

V9_PATH = ROOT / "data" / "AVY_FACT_SIDE__INSERT_v9.sql"
GUI_OUT_PATH = ROOT / "data" / "AVY_FACT_SIDE__INSERT_v9_via_GUI.sql"
DIFF_PATH = ROOT / "data" / "AVY_FACT_SIDE__INSERT_v9_diff.txt"


def call_gui_endpoint():
    client = TestClient(app)
    xml_path = ROOT / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
    drd_path = ROOT / "DRD_Activity_Fact.xlsx"
    with open(xml_path, "rb") as fxml, open(drd_path, "rb") as fdrd:
        resp = client.post(
            "/api/odi/scenario/compare",
            files={
                "xml_file": (xml_path.name, fxml, "application/xml"),
                "drd_file": (drd_path.name, fdrd,
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            },
            params={"target_schema": "IKOROSTELEV", "target_table": "AVY_FACT_SIDE"},
        )
    resp.raise_for_status()
    return resp.json()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    print("[1/4] POSTing same files to /api/odi/scenario/compare ...")
    body = call_gui_endpoint()
    drd_first = body.get("drd_first_insert") or {}
    gui_sql = drd_first.get("sql") or ""
    if not gui_sql:
        print("ERROR: endpoint returned no drd_first_insert.sql")
        print(json.dumps(body.get("drd_first_insert", {}), indent=2)[:1000])
        return 1
    GUI_OUT_PATH.write_text(gui_sql, encoding="utf-8")
    print(f"      GUI INSERT saved: {GUI_OUT_PATH}  ({len(gui_sql)} chars)")

    print("[2/4] Loading v9 baseline ...")
    if not V9_PATH.exists():
        print(f"ERROR: v9 baseline missing at {V9_PATH}")
        return 2
    v9_sql = V9_PATH.read_text(encoding="utf-8")
    print(f"      v9 baseline    : {V9_PATH}  ({len(v9_sql)} chars)")

    print("[3/4] Comparing ...")
    h_gui = sha256(gui_sql)
    h_v9 = sha256(v9_sql)
    print(f"      GUI SHA-256: {h_gui}")
    print(f"      v9  SHA-256: {h_v9}")
    print(f"      identical bytes: {h_gui == h_v9}")
    print(f"      length GUI={len(gui_sql)}  v9={len(v9_sql)}")

    if h_gui != h_v9:
        diff = list(difflib.unified_diff(
            v9_sql.splitlines(keepends=True),
            gui_sql.splitlines(keepends=True),
            fromfile="v9 (script)", tofile="v9 (GUI endpoint)",
            n=2,
        ))
        DIFF_PATH.write_text("".join(diff), encoding="utf-8")
        print(f"      diff lines : {len(diff)}")
        print(f"      diff saved : {DIFF_PATH}")
        # Print first ~40 diff lines
        print("\n--- diff preview ---")
        for ln in diff[:40]:
            sys.stdout.write(ln)
    else:
        if DIFF_PATH.exists():
            DIFF_PATH.unlink()
        print("      [OK] byte-identical -- the GUI endpoint reproduces v9 exactly.")

    print("[4/4] Validating both outputs against Oracle ...")
    from app.sql_model.oracle_validator import validate_oracle_sql
    v_gui = validate_oracle_sql(gui_sql, run_live=False)
    v_v9 = validate_oracle_sql(v9_sql, run_live=False)
    print(f"      GUI Oracle valid: {v_gui.is_valid}  stmts={v_gui.statements_checked}")
    print(f"      v9  Oracle valid: {v_v9.is_valid}  stmts={v_v9.statements_checked}")

    # ── Save both as a UI test_suite ──
    print("[+] Saving both INSERTs into UI test_suite ...")
    from sqlalchemy import select
    from app.database import async_session
    from app.models.test_case import TestCase, TestFolder, TestCaseFolder

    async def save_to_db():
        suite = "AVY_FACT_SIDE -- GUI parity v9 (script vs endpoint)"
        async with async_session() as db:
            ex = await db.execute(select(TestFolder).where(TestFolder.name == suite))
            old = ex.scalar_one_or_none()
            if old:
                tcs = await db.execute(select(TestCaseFolder).where(TestCaseFolder.folder_id == old.id))
                for tcf in tcs.scalars().all():
                    tc = await db.get(TestCase, tcf.test_case_id)
                    await db.delete(tcf)
                    if tc:
                        await db.delete(tc)
                await db.delete(old)
                await db.commit()
            folder = TestFolder(name=suite)
            db.add(folder)
            await db.commit()
            await db.refresh(folder)
            for name, sql in [
                (f"v9 INSERT (script-generated) -- sha256={h_v9[:12]}", v9_sql),
                (f"v9 INSERT (GUI endpoint)     -- sha256={h_gui[:12]}", gui_sql),
            ]:
                tc = TestCase(
                    name=name, test_type="custom_sql",
                    source_datasource_id=3, target_datasource_id=3,
                    source_query=sql, expected_result="0",
                    severity="high", is_active=True, is_ai_generated=False,
                    description=(
                        f"GUI parity check {('PASS' if h_gui == h_v9 else 'DIFFER')}. "
                        "Same DRD.xlsx + ODI.xml in both paths; the emitter is pure & "
                        "deterministic so outputs must match byte-for-byte."
                    ),
                )
                db.add(tc)
                await db.flush()
                db.add(TestCaseFolder(test_case_id=tc.id, folder_id=folder.id))
            await db.commit()
            print(f"      saved as folder #{folder.id}: {suite!r}")

    asyncio.run(save_to_db())
    return 0 if h_gui == h_v9 else 3


if __name__ == "__main__":
    raise SystemExit(main())
