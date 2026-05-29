"""Training packs, pipeline, automation, and folder-management endpoints.

All routes are registered on _tr_router (no prefix) and included by tests.py
into the main /api/tests router.
"""
import json
import re
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.control_table_training import ControlTableCorrectionRule
from app.models.datasource import DataSource
from app.models.test_case import TestCase, TestCaseFolder, TestFolder
from app.services.control_table_service import extract_sql_expression_map, normalize_sql_expr
from app.services.training_automation_service import (
    get_training_automation_status,
    run_training_automation_cycle,
    start_training_automation_loop,
    stop_training_automation_loop,
)
from app.routers.tests_utils import (
    DEFAULT_TEST_FOLDER_NAME,
    FIXTURE_ROOT,
    TRAINING_PACK_ROOT,
    FolderDatasourceUpdateRequest,
    TrainingAutomationRequest,
    TrainingEventRequest,
    _assign_test_to_folder,
    _create_new_folder,
    _derive_training_context,
    _ensure_folder,
    _ensure_non_redshift_datasource,
)
from datetime import datetime

_tr_router = APIRouter()


# ── Pydantic models (training-specific) ──────────────────────────────────────


class TrainingPipelineRequest(BaseModel):
    target_table: str
    source_tables: str = ""
    source_sql: str = ""
    expected_sql: str = ""
    drd_context: str = ""
    max_iterations: int = 5
    columns: List[str] = []
    mode: str = "ghc"
    agent_id: Optional[int] = None


# ── Private helpers ───────────────────────────────────────────────────────────


def _build_training_summary(iterations, target_table):
    """Build human-readable training summary."""
    if not iterations:
        return "No iterations completed."
    parts = [f"Training pipeline for {target_table}:"]
    for it in iterations:
        parts.append(
            f"  Iteration {it['iteration']}: {it['match_count']}/{it['total_columns']} columns matched "
            f"({it['mismatch_count']} mismatches)"
        )
    final = iterations[-1]
    if final["match_count"] == final["total_columns"]:
        parts.append(f"  → SUCCESS: All {final['total_columns']} columns matched!")
    else:
        parts.append(f"  → PARTIAL: {final['match_count']}/{final['total_columns']} columns matched after {len(iterations)} iterations")
        mismatches = [r for r in final.get("results", []) if r["status"] == "mismatch"]
        if mismatches:
            parts.append("  Remaining mismatches:")
            for m in mismatches[:10]:
                parts.append(f"    {m['column']}: got '{m['generated'][:60]}' expected '{m['expected'][:60]}'")
    return "\n".join(parts)


# ── Routes ────────────────────────────────────────────────────────────────────


@_tr_router.get("/folders")
async def list_folders(db: AsyncSession = Depends(get_db)):
    folders_r = await db.execute(select(TestFolder).order_by(TestFolder.name.asc()))
    folders = folders_r.scalars().all()
    counts_r = await db.execute(
        select(TestCaseFolder.folder_id, func.count(TestCaseFolder.test_case_id)).group_by(TestCaseFolder.folder_id)
    )
    counts = {row[0]: row[1] for row in counts_r.all()}
    return [{"id": f.id, "name": f.name, "test_count": counts.get(f.id, 0)} for f in folders]


@_tr_router.post("/folders/{folder_id}/datasource")
async def update_folder_datasource(
    folder_id: int,
    body: FolderDatasourceUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    folder = await db.get(TestFolder, folder_id)
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    result = await db.execute(
        select(TestCase).join(TestCaseFolder, TestCaseFolder.test_case_id == TestCase.id).where(TestCaseFolder.folder_id == folder_id)
    )
    tests = result.scalars().all()
    if not tests:
        return {"updated": 0, "folder_id": folder.id, "folder_name": folder.name}
    for test in tests:
        if body.source_datasource_id is not None:
            test.source_datasource_id = body.source_datasource_id
        if body.target_datasource_id is not None:
            test.target_datasource_id = body.target_datasource_id
    await db.commit()
    return {
        "updated": len(tests),
        "folder_id": folder.id,
        "folder_name": folder.name,
        "source_datasource_id": body.source_datasource_id,
        "target_datasource_id": body.target_datasource_id,
    }


@_tr_router.post("/training-packs")
async def save_training_pack(
    target_table: str = Form(...),
    source_tables: str = Form(""),
    notes: str = Form(""),
    reference_sql: str = Form(""),
    validation_sql: str = Form(""),
    source_datasource_id: str = Form(""),
    target_datasource_id: str = Form(""),
    drd_files: List[UploadFile] = File(default=[]),
):
    target_table_u = (target_table or "").strip().upper()
    if not target_table_u:
        raise HTTPException(status_code=400, detail="target_table is required")

    slug = re.sub(r"[^A-Z0-9_]+", "_", target_table_u).strip("_") or "TRAINING_PACK"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pack_dir = TRAINING_PACK_ROOT / f"{slug}_{timestamp}"
    pack_dir.mkdir(parents=True, exist_ok=True)

    saved_files = []
    for upload in drd_files or []:
        filename = Path(upload.filename or "upload.bin").name
        content = await upload.read()
        (pack_dir / filename).write_bytes(content)
        saved_files.append(filename)

    metadata = {
        "target_table": target_table_u,
        "source_tables": [item.strip() for item in (source_tables or "").split(",") if item.strip()],
        "notes": notes or "",
        "reference_sql": reference_sql or "",
        "validation_sql": validation_sql or "",
        "source_datasource_id": source_datasource_id or "",
        "target_datasource_id": target_datasource_id or "",
        "saved_files": saved_files,
        "created_at": datetime.now().isoformat(),
        "questions": [
            "Confirm which SQL blocks are setup-only versus validation-critical.",
            "Confirm whether multi-step SQL should be preserved as separate setup tests before attribute validations.",
            "Confirm the datasource pair to use when the DRD covers more than one source stream.",
        ],
    }
    (pack_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if reference_sql:
        (pack_dir / "reference.sql").write_text(reference_sql, encoding="utf-8")
    if validation_sql:
        (pack_dir / "validation.sql").write_text(validation_sql, encoding="utf-8")

    return {
        "saved": True,
        "target_table": target_table_u,
        "pack_id": pack_dir.name,
        "pack_dir": str(pack_dir),
        "saved_files": saved_files,
        "questions": metadata["questions"],
    }


@_tr_router.post("/training-packs/derive-context")
async def derive_training_pack_context(
    target_table: str = Form(""),
    source_tables: str = Form(""),
    source_sql: str = Form(""),
    expected_sql: str = Form(""),
    drd_files: List[UploadFile] = File(default=[]),
):
    file_names: List[str] = []
    file_texts: List[str] = []
    for upload in drd_files or []:
        filename = Path(upload.filename or "upload.bin").name
        file_names.append(filename)
        try:
            content = await upload.read()
            if not content:
                continue
            if filename.lower().endswith((".txt", ".md", ".sql", ".csv", ".json", ".xml")):
                file_texts.append(content[:200000].decode("utf-8", errors="ignore"))
        except Exception:
            continue

    context = _derive_training_context(
        target_table=target_table,
        source_tables_csv=source_tables,
        source_sql=source_sql,
        expected_sql=expected_sql,
        file_names=file_names,
        file_texts=file_texts,
    )
    return {"derived": True, **context, "file_count": len(file_names)}


@_tr_router.post("/training-events")
async def save_training_event(body: TrainingEventRequest):
    TRAINING_PACK_ROOT.mkdir(parents=True, exist_ok=True)
    event_file = TRAINING_PACK_ROOT / "training_events.jsonl"
    payload = {
        "event_type": body.event_type,
        "entity_type": body.entity_type,
        "entity_id": body.entity_id,
        "target_table": (body.target_table or "").strip().upper(),
        "source": body.source,
        "status": body.status,
        "details": body.details or {},
        "knowledge_refs": body.knowledge_refs or [],
        "created_at": datetime.now().isoformat(),
    }
    with event_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return {"saved": True, "event_file": str(event_file), "event_type": body.event_type}


@_tr_router.get("/training-automation/status")
async def training_automation_status():
    return get_training_automation_status()


@_tr_router.post("/training-automation/start")
async def training_automation_start(body: TrainingAutomationRequest):
    return await start_training_automation_loop(body.model_dump())


@_tr_router.post("/training-automation/stop")
async def training_automation_stop():
    return await stop_training_automation_loop()


@_tr_router.post("/training-automation/run-once")
async def training_automation_run_once(body: TrainingAutomationRequest):
    return await run_training_automation_cycle(body.model_dump())


@_tr_router.post("/training-pipeline/run")
async def run_training_pipeline(body: TrainingPipelineRequest):
    from app.services.ai_service import ai_chat
    import json as _json

    target_table = (body.target_table or "").strip()
    expected_sql = (body.expected_sql or "").strip()
    source_sql = (body.source_sql or "").strip()
    if not target_table or not expected_sql:
        raise HTTPException(422, "target_table and expected_sql are required")

    expected_map = extract_sql_expression_map(expected_sql)
    if not expected_map:
        raise HTTPException(422, "Could not parse column expressions from expected SQL")

    columns_to_train = [c.upper() for c in body.columns] if body.columns else list(expected_map.keys())
    iterations = []
    current_sql = source_sql or ""

    drd_context_dict = {}
    if body.drd_context:
        try:
            drd_context_dict = _json.loads(body.drd_context)
        except Exception:
            pass

    for iteration in range(1, body.max_iterations + 1):
        prompt_parts = [
            f"Generate an Oracle INSERT...SELECT SQL statement for target table: {target_table}",
            f"Columns to populate: {', '.join(columns_to_train)}",
        ]
        if source_sql:
            prompt_parts.append(f"Reference SQL (may be partial or incorrect):\n{source_sql}")
        if drd_context_dict:
            prompt_parts.append(f"DRD context: {_json.dumps(drd_context_dict, indent=2)}")
        if iterations:
            last = iterations[-1]
            mismatches = [r for r in last.get("results", []) if r["status"] == "mismatch"]
            if mismatches:
                prompt_parts.append("Previous iteration mismatches to fix:")
                for mm in mismatches[:15]:
                    prompt_parts.append(f"  Column {mm['column']}: generated '{mm.get('generated', '')[:80]}' but expected '{mm.get('expected', '')[:80]}'")

        prompt = "\n".join(prompt_parts)
        try:
            ai_response = await ai_chat(
                messages=[{"role": "user", "content": prompt}],
                agent_id=body.agent_id,
                mode=body.mode,
            )
            generated_sql = (ai_response.get("content") or ai_response.get("message") or "").strip()
            sql_match = re.search(r"```(?:sql)?\s*(.*?)```", generated_sql, re.DOTALL | re.IGNORECASE)
            if sql_match:
                generated_sql = sql_match.group(1).strip()
        except Exception as e:
            iterations.append({
                "iteration": iteration,
                "error": str(e),
                "match_count": 0,
                "mismatch_count": len(columns_to_train),
                "total_columns": len(columns_to_train),
                "results": [],
            })
            break

        current_sql = generated_sql
        generated_map = extract_sql_expression_map(generated_sql)
        results = []
        match_count = 0
        for col in columns_to_train:
            exp_expr = expected_map.get(col, "")
            gen_expr = generated_map.get(col, "")
            status = "match" if normalize_sql_expr(exp_expr) == normalize_sql_expr(gen_expr) else "mismatch"
            if status == "match":
                match_count += 1
            results.append({
                "column": col,
                "expected": exp_expr,
                "generated": gen_expr,
                "status": status,
            })

        iterations.append({
            "iteration": iteration,
            "match_count": match_count,
            "mismatch_count": len(columns_to_train) - match_count,
            "total_columns": len(columns_to_train),
            "results": results,
            "generated_sql": generated_sql,
        })

        if match_count == len(columns_to_train):
            break

    return {
        "target_table": target_table,
        "iterations": len(iterations),
        "final_sql": current_sql,
        "iteration_details": iterations,
        "summary": _build_training_summary(iterations, target_table),
        "success": iterations[-1]["match_count"] == iterations[-1]["total_columns"] if iterations else False,
    }


@_tr_router.get("/training-pipeline/rules")
async def list_training_pipeline_rules(target_table: str = "", db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select as sa_select, or_
    stmt = sa_select(ControlTableCorrectionRule)
    if target_table:
        tbl_upper = target_table.strip().upper()
        bare = tbl_upper.rsplit(".", 1)[-1] if "." in tbl_upper else tbl_upper
        stmt = stmt.where(or_(
            ControlTableCorrectionRule.target_table == tbl_upper,
            ControlTableCorrectionRule.target_table == bare,
        ))
    stmt = stmt.order_by(ControlTableCorrectionRule.target_table, ControlTableCorrectionRule.target_column)
    result = await db.execute(stmt)
    rules = result.scalars().all()
    return [
        {
            "id": r.id,
            "target_table": r.target_table,
            "target_column": r.target_column,
            "issue_type": r.issue_type,
            "source_attribute": r.source_attribute,
            "recommended_source": r.recommended_source,
            "replacement_expression": r.replacement_expression,
            "notes": r.notes,
            "created_at": str(r.created_at) if r.created_at else None,
        }
        for r in rules
    ]


@_tr_router.post("/test-suites/generate")
async def generate_test_suite_from_sql(
    target_table: str = Form(...),
    target_schema: str = Form(...),
    ddl_sql: str = Form(""),
    insert_sql: str = Form(""),
    validation_sql: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Generate a test suite folder with DDL, INSERT, and validation test cases."""
    if not insert_sql.strip():
        raise HTTPException(status_code=400, detail="insert_sql required")
    
    target_table_u = (target_table or "").strip().upper()
    if not target_table_u:
        raise HTTPException(status_code=400, detail="target_table required")
    
    # Create test folder for the suite
    suite_name = f"{target_table_u}_FullCoverage"
    suite_folder = await _ensure_folder(db, suite_name)
    
    created_tests = []
    
    # DDL test
    if ddl_sql.strip():
        ddl_test = TestCase(
            name=f"{target_table_u}_DDL_Create",
            test_type="ddl",
            source_query=ddl_sql,
            expected_result="table_created",
            is_active=True,
        )
        db.add(ddl_test)
        await db.flush()
        if suite_folder:
            await _assign_test_to_folder(db, ddl_test.id, suite_folder.id)
        created_tests.append({"name": ddl_test.name, "type": "ddl"})
    
    # INSERT test
    insert_test = TestCase(
        name=f"{target_table_u}_INSERT_Data",
        test_type="insert",
        source_query=insert_sql,
        expected_result="rows_inserted",
        is_active=True,
    )
    db.add(insert_test)
    await db.flush()
    if suite_folder:
        await _assign_test_to_folder(db, insert_test.id, suite_folder.id)
    created_tests.append({"name": insert_test.name, "type": "insert"})
    
    # Validation test
    if validation_sql.strip():
        val_test = TestCase(
            name=f"{target_table_u}_Validate",
            test_type="validation",
            source_query=validation_sql,
            expected_result="validation_passed",
            is_active=True,
        )
        db.add(val_test)
        await db.flush()
        if suite_folder:
            await _assign_test_to_folder(db, val_test.id, suite_folder.id)
        created_tests.append({"name": val_test.name, "type": "validation"})
    
    await db.commit()
    return {
        "suite_id": suite_folder.id if suite_folder else None,
        "suite_name": suite_name,
        "target_table": target_table_u,
        "tests_created": len(created_tests),
        "tests": created_tests,
        "status": "created",
    }
