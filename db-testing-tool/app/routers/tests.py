"""Test case and test run endpoints.

This module is the main /api/tests router. Heavy sections are extracted into:
  - tests_control_table.py  (control-table analysis, comparison, training rules)
  - tests_training.py       (training packs, pipeline, automation, folder/datasource)

Shared helpers and models live in tests_utils.py.
"""
import asyncio
import csv
import io
import uuid
from collections import Counter
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from app.database import get_db
from app.models.datasource import DataSource
from app.models.test_case import TestCase, TestRun, TestFolder, TestCaseFolder
from app.connectors.factory import get_connector_from_model
from app.services.test_generator import (
    create_selected_tests,
    generate_tests_for_all_rules,
    generate_tests_for_rule,
    preview_tests_for_rule,
)
from app.services.test_executor import run_test, run_all_tests
from app.services.sql_pattern_validation import validate_sql_pattern
from app.services.training_automation_service import (
    get_training_automation_status,
    run_training_automation_cycle,
    start_training_automation_loop,
    stop_training_automation_loop,
)
from app.routers.tests_utils import (
    DEFAULT_TEST_FOLDER_NAME,
    BulkDeleteRequest,
    BulkFolderDeleteRequest,
    CreateSelectedRequest,
    ExportTfsCsvRequest,
    FolderCreateRequest,
    MoveTestsToFolderRequest,
    RunRequest,
    StartBatchRequest,
    TestCreate,
    ValidateSqlRequest,
    _assign_test_to_folder,
    _batch_control,
    _batch_tasks,
    _create_new_folder,
    _delete_folder_with_children,
    _ensure_folder,
    _extract_target_table_name,
    _run_batch_background,
)
from app.routers.tests_control_table import _ct_router
from app.routers.tests_training import _tr_router

import re
from datetime import datetime

router = APIRouter(prefix="/api/tests", tags=["tests"])
router.include_router(_ct_router)
router.include_router(_tr_router)


# Legacy contract shim: keep training automation models/routes visible in tests.py.
class TrainingAutomationRequest(BaseModel):
    interval_seconds: int = 600
    mode: str = "ghc"
    agent_id: Optional[int] = None
    target_table: str = ""
    max_packs_per_cycle: int = 3


@router.get("/training-automation/status")
async def training_automation_status_compat():
    return get_training_automation_status()


@router.post("/training-automation/start")
async def training_automation_start_compat(body: TrainingAutomationRequest):
    return await start_training_automation_loop(body.model_dump())


@router.post("/training-automation/stop")
async def training_automation_stop_compat():
    return await stop_training_automation_loop()


@router.post("/training-automation/run-once")
async def training_automation_run_once_compat(body: TrainingAutomationRequest):
    return await run_training_automation_cycle(body.model_dump())


# == Dashboard stats ==

@router.get("/dashboard-stats")
async def dashboard_stats(db: AsyncSession = Depends(get_db)):
    total_tests = (await db.execute(select(func.count(TestCase.id)))).scalar() or 0
    total_runs = (await db.execute(select(func.count(TestRun.id)))).scalar() or 0
    passed = (await db.execute(select(func.count(TestRun.id)).where(TestRun.status == "passed"))).scalar() or 0
    failed = (await db.execute(select(func.count(TestRun.id)).where(TestRun.status == "failed"))).scalar() or 0
    errors = (await db.execute(select(func.count(TestRun.id)).where(TestRun.status == "error"))).scalar() or 0
    return {
        "total_tests": total_tests,
        "total_runs": total_runs,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": round(passed / total_runs * 100, 1) if total_runs else 0,
    }


# == Runs / Results ==

@router.get("/runs")
async def list_runs(batch_id: str = None, limit: int = 100, db: AsyncSession = Depends(get_db)):
    q = select(TestRun).order_by(TestRun.id.desc()).limit(limit)
    if batch_id:
        q = q.where(TestRun.batch_id == batch_id)
    result = await db.execute(q)
    runs = result.scalars().all()
    return [
        {
            "id": r.id, "test_case_id": r.test_case_id,
            "batch_id": r.batch_id, "status": r.status,
            "mismatch_count": r.mismatch_count,
            "execution_time_ms": r.execution_time_ms,
            "error_message": r.error_message,
            "actual_result": r.actual_result,
            "executed_at": str(r.executed_at) if r.executed_at else None,
        }
        for r in runs
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: int, db: AsyncSession = Depends(get_db)):
    r = await db.get(TestRun, run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    return {
        "id": r.id, "test_case_id": r.test_case_id,
        "batch_id": r.batch_id, "status": r.status,
        "source_result": r.source_result,
        "target_result": r.target_result,
        "actual_result": r.actual_result,
        "mismatch_count": r.mismatch_count,
        "mismatch_sample": r.mismatch_sample,
        "execution_time_ms": r.execution_time_ms,
        "error_message": r.error_message,
        "executed_at": str(r.executed_at) if r.executed_at else None,
    }


# == Generation ==

@router.post("/generate-all")
async def generate_for_all(connection_id: int = None, db: AsyncSession = Depends(get_db)):
    count = await generate_tests_for_all_rules(db, connection_id)
    if count > 0:
        folder = await _ensure_folder(db, DEFAULT_TEST_FOLDER_NAME)
        if folder:
            all_tests_r = await db.execute(select(TestCase.id))
            all_test_ids = [row[0] for row in all_tests_r.all()]
            linked_r = await db.execute(select(TestCaseFolder.test_case_id))
            linked_ids = {row[0] for row in linked_r.all()}
            for test_id in all_test_ids:
                if test_id not in linked_ids:
                    await _assign_test_to_folder(db, test_id, folder.id)
            await db.commit()
    return {"count": count}


@router.post("/generate/{rule_id}")
async def generate_for_rule(rule_id: int, connection_id: int = None, db: AsyncSession = Depends(get_db)):
    tests = await generate_tests_for_rule(db, rule_id, connection_id)
    folder = await _ensure_folder(db, DEFAULT_TEST_FOLDER_NAME)
    if folder:
        for t in tests:
            await _assign_test_to_folder(db, t.id, folder.id)
        await db.commit()
    return {"count": len(tests), "tests": [{"id": t.id, "name": t.name} for t in tests]}


@router.post("/preview/{rule_id}")
async def preview_for_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    defs = await preview_tests_for_rule(db, rule_id)
    return {"tests": defs}


@router.post("/create-selected")
async def create_selected(body: CreateSelectedRequest, db: AsyncSession = Depends(get_db)):
    created = await create_selected_tests(db, body.tests)
    folder_name = DEFAULT_TEST_FOLDER_NAME
    if created:
        target_tables = set()
        for t in created:
            tgt_table = _extract_target_table_name(t.target_query, t.source_query)
            if not tgt_table and t.name:
                m = re.search(r"[->]\s*([\w]+(?:\.[\w]+)*)", t.name)
                if m:
                    parts = m.group(1).split(".")
                    tgt_table = parts[-2] if len(parts) >= 2 else parts[-1]
            if tgt_table:
                target_tables.add(tgt_table)
        if target_tables:
            counts = Counter(
                _extract_target_table_name(t.target_query, t.source_query) or DEFAULT_TEST_FOLDER_NAME
                for t in created
            )
            folder_name = counts.most_common(1)[0][0]
    folder = await _create_new_folder(db, folder_name)
    if folder:
        for t in created:
            await _assign_test_to_folder(db, t.id, folder.id)
    await db.commit()
    return {"count": len(created), "tests": [{"id": t.id, "name": t.name} for t in created]}


@router.post("/validate-sql")
async def validate_sql(body: ValidateSqlRequest, db: AsyncSession = Depends(get_db)):
    connectors = {}
    ds_cache = {}

    async def _get_connector_for_ds(ds_id):
        if not ds_id:
            return None
        if ds_id in connectors:
            return connectors[ds_id]
        ds = ds_cache.get(ds_id)
        if not ds:
            ds = await db.get(DataSource, ds_id)
            if not ds:
                return None
            if (ds.db_type or "").strip().lower() == "redshift":
                return None
            ds_cache[ds_id] = ds
        connector = get_connector_from_model(ds)
        if not hasattr(connector, "validate_sql_batch"):
            return None
        await asyncio.to_thread(connector.connect)
        connectors[ds_id] = connector
        return connector

    results = []
    for idx, t in enumerate(body.tests):
        name = t.get("name", "")
        src_sql = (t.get("source_query") or "").strip()
        tgt_sql = (t.get("target_query") or "").strip()
        src_ds_id = t.get("source_datasource_id") or body.datasource_id
        tgt_ds_id = t.get("target_datasource_id") or body.datasource_id
        errs = []
        pattern_src = validate_sql_pattern(src_sql)
        pattern_tgt = validate_sql_pattern(tgt_sql)
        if pattern_src:
            errs.append("source: " + "; ".join(pattern_src))
        if pattern_tgt:
            errs.append("target: " + "; ".join(pattern_tgt))
        if src_sql and not pattern_src:
            src_connector = await _get_connector_for_ds(int(src_ds_id) if src_ds_id else None)
            if src_connector:
                src_err = (await asyncio.to_thread(src_connector.validate_sql_batch, [src_sql]))[0]
                if src_err:
                    errs.append("source: " + str(src_err))
        if tgt_sql and not pattern_tgt:
            tgt_connector = await _get_connector_for_ds(int(tgt_ds_id) if tgt_ds_id else None)
            if tgt_connector:
                tgt_err = (await asyncio.to_thread(tgt_connector.validate_sql_batch, [tgt_sql]))[0]
                if tgt_err:
                    errs.append("target: " + str(tgt_err))
        results.append({
            "index": idx, "name": name,
            "valid": len(errs) == 0,
            "error": " | ".join(errs) if errs else None,
        })
    for conn in connectors.values():
        try:
            await asyncio.to_thread(conn.disconnect)
        except Exception:
            pass
    valid_count = sum(1 for r in results if r["valid"])
    invalid_count = sum(1 for r in results if not r["valid"])
    return {"results": results, "valid_count": valid_count, "invalid_count": invalid_count}


@router.post("/export-tfs-csv")
async def export_tfs_csv(body: ExportTfsCsvRequest, db: AsyncSession = Depends(get_db)):
    tests = []
    for test_id in body.test_ids:
        tc = await db.get(TestCase, test_id)
        if tc:
            tests.append(tc)
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(["ID", "Work Item Type", "Title", "Test Step", "Step Action", "Step Expected", "Area Path", "Assigned To", "State"])
    for t in tests:
        writer.writerow(["", "Test Case", t.name or "", "", "", "", body.area_path, body.assigned_to, body.state])
        step = 1
        if t.source_query:
            writer.writerow(["", "", "", str(step), t.source_query, t.expected_result or "", "", "", ""])
            step += 1
        if t.target_query:
            writer.writerow(["", "", "", str(step), t.target_query, "", "", "", ""])
    csv_bytes = output.getvalue().encode("utf-8-sig")
    filename = f"tfs_tests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([csv_bytes]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# == Folder management ==

@router.post("/folders")
async def create_folder(body: FolderCreateRequest, db: AsyncSession = Depends(get_db)):
    folder = await _ensure_folder(db, body.name)
    await db.commit()
    return {"id": folder.id, "name": folder.name}


@router.post("/folders/move")
async def move_tests_to_folder(body: MoveTestsToFolderRequest, db: AsyncSession = Depends(get_db)):
    moved = 0
    for test_id in body.test_ids:
        tc = await db.get(TestCase, test_id)
        if not tc:
            continue
        await _assign_test_to_folder(db, test_id, body.folder_id)
        moved += 1
    await db.commit()
    return {"moved": moved, "folder_id": body.folder_id}


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: int, db: AsyncSession = Depends(get_db)):
    result = await _delete_folder_with_children(db, folder_id)
    if not result.get("deleted"):
        raise HTTPException(404, "Folder not found")
    await db.commit()
    return result


@router.post("/folders/bulk-delete")
async def bulk_delete_folders(body: BulkFolderDeleteRequest, db: AsyncSession = Depends(get_db)):
    deleted_folders = 0
    deleted_tests = 0
    deleted_runs = 0
    for folder_id in body.folder_ids or []:
        result = await _delete_folder_with_children(db, folder_id)
        if result.get("deleted"):
            deleted_folders += 1
            deleted_tests += int(result.get("tests_deleted") or 0)
            deleted_runs += int(result.get("runs_deleted") or 0)
    await db.commit()
    return {
        "deleted_folders": deleted_folders,
        "deleted_tests": deleted_tests,
        "deleted_runs": deleted_runs,
    }


# == Batch execution ==

@router.post("/run-batch")
async def execute_batch(body: RunRequest, db: AsyncSession = Depends(get_db)):
    summary = await run_all_tests(db, body.test_ids)
    return summary


@router.post("/run-batch/start")
async def start_batch(body: StartBatchRequest):
    batch_id = str(uuid.uuid4())[:12]
    task = asyncio.create_task(_run_batch_background(batch_id, body.test_ids))
    _batch_tasks[batch_id] = task
    _batch_control[batch_id] = {
        "batch_id": batch_id, "status": "starting",
        "total": 0, "completed": 0, "passed": 0,
        "failed": 0, "error": 0, "stopped": False,
        "current_test_number": None, "current_test_id": None,
    }
    return {"batch_id": batch_id, "status": "starting"}


@router.get("/run-batch/status/{batch_id}")
async def get_batch_status(batch_id: str, db: AsyncSession = Depends(get_db)):
    state = _batch_control.get(batch_id)
    if not state:
        q = await db.execute(
            select(TestRun.status, func.count(TestRun.id))
            .where(TestRun.batch_id == batch_id)
            .group_by(TestRun.status)
        )
        groups = dict(q.all())
        total = sum(groups.values())
        if total == 0:
            raise HTTPException(404, "Batch not found")
        return {
            "batch_id": batch_id, "status": "completed",
            "total": total, "completed": total,
            "passed": groups.get("passed", 0),
            "failed": groups.get("failed", 0),
            "error": groups.get("error", 0),
            "stopped": False, "current_test_number": None, "current_test_id": None,
        }
    return state


@router.post("/run-batch/stop/{batch_id}")
async def stop_batch(batch_id: str):
    state = _batch_control.get(batch_id)
    if not state:
        raise HTTPException(404, "Batch not found")
    state["stopped"] = True
    state["status"] = "stopping"
    task = _batch_tasks.get(batch_id)
    if task and not task.done():
        task.cancel()
    return {"batch_id": batch_id, "status": "stopping"}


@router.post("/run/{test_id}")
async def execute_test(test_id: int, db: AsyncSession = Depends(get_db)):
    run = await run_test(db, test_id)
    return {
        "run_id": run.id, "batch_id": run.batch_id,
        "status": run.status, "mismatch_count": run.mismatch_count,
        "execution_time_ms": run.execution_time_ms,
        "error_message": run.error_message, "actual_result": run.actual_result,
    }


# == Test Cases CRUD ==

@router.get("")
async def list_tests(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TestCase).order_by(TestCase.id.asc()))
    items = result.scalars().all()
    folder_links_r = await db.execute(select(TestCaseFolder))
    folder_links = folder_links_r.scalars().all()
    test_to_folder = {fl.test_case_id: fl.folder_id for fl in folder_links}
    folders_r = await db.execute(select(TestFolder))
    folders = folders_r.scalars().all()
    folder_names = {f.id: f.name for f in folders}

    # Fetch latest run per test in ONE query using a correlated subquery — avoids N+1
    from sqlalchemy import text
    latest_runs_r = await db.execute(
        text(
            "SELECT tr.* FROM test_runs tr "
            "INNER JOIN ("
            "  SELECT test_case_id, MAX(id) AS max_id FROM test_runs GROUP BY test_case_id"
            ") latest ON tr.id = latest.max_id"
        )
    )
    latest_run_map: dict[int, Any] = {}
    for row in latest_runs_r.mappings():
        latest_run_map[row["test_case_id"]] = row

    payload = []
    for t in items:
        lr = latest_run_map.get(t.id)
        folder_id = test_to_folder.get(t.id)
        payload.append({
            "id": t.id, "name": t.name, "test_type": t.test_type,
            "mapping_rule_id": t.mapping_rule_id,
            "source_datasource_id": t.source_datasource_id,
            "target_datasource_id": t.target_datasource_id,
            "source_query": t.source_query,
            "target_query": t.target_query,
            "severity": t.severity,
            "is_active": t.is_active,
            "is_ai_generated": t.is_ai_generated,
            "description": t.description,
            "last_run_status": lr["status"] if lr else "untested",
            "last_run_at": str(lr["executed_at"]) if lr and lr["executed_at"] else None,
            "last_run_batch_id": lr["batch_id"] if lr else None,
            "last_error_message": lr["error_message"] if lr else None,
            "folder_id": folder_id,
            "folder_name": folder_names.get(folder_id) if folder_id else None,
        })
    payload.sort(key=lambda x: ((x.get("folder_name") or "~ungrouped").lower(), x["id"]))
    return payload


@router.post("")
async def create_test(body: TestCreate, db: AsyncSession = Depends(get_db)):
    if body.source_datasource_id:
        src_ds = await db.get(DataSource, body.source_datasource_id)
        if not src_ds:
            raise HTTPException(400, f"Source datasource {body.source_datasource_id} does not exist")
    if body.target_datasource_id:
        tgt_ds = await db.get(DataSource, body.target_datasource_id)
        if not tgt_ds:
            raise HTTPException(400, f"Target datasource {body.target_datasource_id} does not exist")
    tc = TestCase(**body.model_dump())
    db.add(tc)
    await db.flush()
    folder = await _ensure_folder(db, DEFAULT_TEST_FOLDER_NAME)
    if folder:
        await _assign_test_to_folder(db, tc.id, folder.id)
    await db.commit()
    await db.refresh(tc)
    return {"id": tc.id, "name": tc.name, "status": "created"}


@router.post("/bulk-delete")
async def bulk_delete_tests(body: BulkDeleteRequest, db: AsyncSession = Depends(get_db)):
    deleted = 0
    for test_id in body.ids:
        await db.execute(delete(TestRun).where(TestRun.test_case_id == test_id))
        await db.execute(delete(TestCaseFolder).where(TestCaseFolder.test_case_id == test_id))
        tc = await db.get(TestCase, test_id)
        if tc:
            await db.delete(tc)
            deleted += 1
    await db.commit()
    return {"deleted": deleted}


@router.post("/runs/bulk-delete")
async def bulk_delete_runs(body: BulkDeleteRequest, db: AsyncSession = Depends(get_db)):
    deleted = 0
    for run_id in body.ids:
        run = await db.get(TestRun, run_id)
        if run:
            await db.delete(run)
            deleted += 1
    await db.commit()
    return {"deleted": deleted}


@router.post("/runs/clear")
async def clear_runs(batch_id: str = None, db: AsyncSession = Depends(get_db)):
    if batch_id:
        result = await db.execute(delete(TestRun).where(TestRun.batch_id == batch_id))
    else:
        result = await db.execute(delete(TestRun))
    await db.commit()
    return {"deleted": result.rowcount or 0, "batch_id": batch_id}


@router.post("/runs/clear-all-statuses")
async def clear_all_run_statuses(db: AsyncSession = Depends(get_db)):
    result = await db.execute(delete(TestRun))
    await db.commit()
    return {"deleted": result.rowcount or 0}


@router.get("/{test_id}")
async def get_test(test_id: int, db: AsyncSession = Depends(get_db)):
    tc = await db.get(TestCase, test_id)
    if not tc:
        raise HTTPException(404, "Test not found")
    latest_run_q = await db.execute(
        select(TestRun).where(TestRun.test_case_id == tc.id).order_by(TestRun.id.desc()).limit(1)
    )
    latest_run = latest_run_q.scalar_one_or_none()
    return {
        "id": tc.id, "name": tc.name, "test_type": tc.test_type,
        "mapping_rule_id": tc.mapping_rule_id,
        "source_datasource_id": tc.source_datasource_id,
        "target_datasource_id": tc.target_datasource_id,
        "source_query": tc.source_query, "target_query": tc.target_query,
        "expected_result": tc.expected_result, "tolerance": tc.tolerance,
        "severity": tc.severity, "is_active": tc.is_active,
        "is_ai_generated": tc.is_ai_generated, "description": tc.description,
        "last_run_status": latest_run.status if latest_run else "untested",
        "last_run_at": str(latest_run.executed_at) if latest_run and latest_run.executed_at else None,
        "last_error_message": latest_run.error_message if latest_run else None,
        "last_actual_result": latest_run.actual_result if latest_run else None,
        "last_mismatch_count": latest_run.mismatch_count if latest_run else 0,
    }


@router.put("/{test_id}")
async def update_test(test_id: int, body: TestCreate, db: AsyncSession = Depends(get_db)):
    tc = await db.get(TestCase, test_id)
    if not tc:
        raise HTTPException(404, "Test not found")
    for key, value in body.model_dump().items():
        setattr(tc, key, value)
    await db.commit()
    await db.refresh(tc)
    return {"id": tc.id, "name": tc.name, "status": "updated"}


@router.delete("/{test_id}")
async def delete_test(test_id: int, db: AsyncSession = Depends(get_db)):
    tc = await db.get(TestCase, test_id)
    if not tc:
        raise HTTPException(404, "Test not found")
    await db.execute(delete(TestRun).where(TestRun.test_case_id == test_id))
    await db.execute(delete(TestCaseFolder).where(TestCaseFolder.test_case_id == test_id))
    await db.delete(tc)
    await db.commit()
    return {"deleted": True}
