"""TFS / Azure DevOps integration endpoints."""
import asyncio
import html
import re
import time
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.test_case import TestCase, TestFolder, TestCaseFolder
from app.models.tfs_workitem import TfsWorkItem
from app.models.tfs_test_management import TfsTestRun, TfsTestResult, TfsTestPoint
from app.models.datasource import DataSource
from app.connectors.factory import get_connector_from_model
from app.services.tfs_service import (
    create_work_item, sync_work_item, auto_create_bugs_for_batch,
    update_work_item, run_wiql_query, _get_projects,
    get_saved_queries, run_saved_query, get_cds_preset_queries,
    build_tfs_work_item_web_url, get_tfs_web_context,
)
from app.services.tfs_test_management_service import (
    get_test_plans, get_test_suites, get_test_points,
    cache_test_plan, cache_test_suite, cache_test_point,
    create_test_run, update_test_result, get_test_run_details,
    lookup_work_item, get_result_ids_for_run, complete_test_run,
    get_test_case_details, get_test_plan,
    create_test_plan_record, create_test_suite_record,
    create_test_case_work_item, add_test_cases_to_suite,
    get_classification_nodes,
)
from pydantic import BaseModel
from typing import Dict, List, Optional
import json

router = APIRouter(prefix="/api/tfs", tags=["tfs"])


class CreateBugRequest(BaseModel):
    title: str
    description: str = ""
    repro_steps: str = ""
    test_run_ids: List[int] = []
    severity: str = "3 - Medium"
    priority: int = 2
    assigned_to: str = ""
    state: str = "New"
    work_item_type: str = "Bug"
    area_path: str = ""
    iteration_path: str = ""
    tags: str = "AutoTest"
    project: str = ""


class UpdateBugRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    state: Optional[str] = None
    assigned_to: Optional[str] = None
    priority: Optional[int] = None
    severity: Optional[str] = None
    area_path: Optional[str] = None
    iteration_path: Optional[str] = None
    tags: Optional[str] = None


class AutoBugRequest(BaseModel):
    batch_id: str


class WiqlRequest(BaseModel):
    project: str
    query: str


class CreateTestRunRequest(BaseModel):
    project: str
    plan_id: int
    run_name: str
    test_point_ids: List[int] = []
    environment: str = "CDSQA"


class UpdateTestResultRequest(BaseModel):
    outcome: str  # passed, failed, blocked, notRun
    result_id: Optional[int] = None   # TFS result ID (from result_id_map returned on run create)
    comment: str = ""
    error_msg: str = ""
    duration_ms: float = 0


class ExecuteTestPointRequest(BaseModel):
    datasource_id: int
    skip_destructive: bool = True


class CreateTestPlanRequest(BaseModel):
    project: str
    name: str
    description: str = ""
    area_path: str = ""
    iteration_path: str = ""
    start_date: str = ""
    end_date: str = ""


class CreateTestSuiteRequest(BaseModel):
    project: str
    plan_id: int
    name: str = ""
    parent_suite_id: Optional[int] = None
    suite_type: str = "StaticTestSuite"
    requirement_id: Optional[int] = None


class ImportLocalTestsToSuiteRequest(BaseModel):
    project: str
    plan_id: int
    suite_name: str = ""
    destination_suite_id: Optional[int] = None
    parent_suite_id: Optional[int] = None
    local_test_ids: List[int]
    area_path: str = ""
    iteration_path: str = ""
    assigned_to: str = ""
    state: str = "Design"


class ImportTfsPointsToLocalRequest(BaseModel):
    project: str
    plan_id: int
    suite_id: int
    test_point_ids: List[int]
    folder_name: str = ""
    source_datasource_id: Optional[int] = None
    target_datasource_id: Optional[int] = None


def _strip_markup(text: str) -> str:
    raw = html.unescape(html.unescape(text or ""))
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = raw.replace("\r", "\n")
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _extract_sql_candidates(text: str) -> List[str]:
    if not text:
        return []
    cleaned = _strip_markup(text)
    pattern = re.compile(
        r"(?is)(?:^|\n|\s)(select|with|insert|update|delete|merge|create|drop|truncate|alter|load)\b[\s\S]*?(?:;|$)"
    )
    found: List[str] = []
    for m in pattern.finditer(cleaned):
        sql = m.group(0).strip().lstrip(':').strip()
        sql = re.sub(r";\s*$", "", sql)
        if sql:
            found.append(sql)
    return found


def _is_readonly_sql(sql: str) -> bool:
    text = (sql or "").strip()
    if not text:
        return False
    lowered = text.lower().lstrip()
    return lowered.startswith(("select", "with", "show", "describe", "desc", "explain"))


def _is_destructive_sql(sql: str) -> bool:
    lowered = (sql or "").lower()
    return bool(re.search(r"\b(drop|truncate|create|alter|insert|update|delete|merge|load)\b", lowered))


def _extract_sql_from_test_case_payload(payload: dict) -> List[str]:
    # Pull text from common TFS fields where SQL may be stored.
    fields = payload.get("fields", {}) if isinstance(payload, dict) else {}
    steps_raw = fields.get("Microsoft.VSTS.TCM.Steps", "")
    # Steps are XML with multiple <parameterizedString> blocks. Parse each block first
    # to avoid mixing action and expected-result text into one SQL string.
    step_blocks = re.findall(r"<parameterizedString[^>]*>([\s\S]*?)</parameterizedString>", steps_raw or "", flags=re.IGNORECASE)
    text_sources = [
        *step_blocks,
        fields.get("System.Description", ""),
        fields.get("System.Title", ""),
    ]
    sqls: List[str] = []
    for txt in text_sources:
        sqls.extend(_extract_sql_candidates(txt))
    # Preserve order, remove duplicates.
    deduped: List[str] = []
    seen = set()
    for s in sqls:
        key = s.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _build_local_test_description(test: TestCase) -> str:
    parts: List[str] = []
    if test.description:
        parts.append(f"<div>{html.escape(test.description)}</div>")
    parts.append(f"<div><strong>Generated from DB Testing Tool</strong></div>")
    parts.append(f"<div><strong>Type:</strong> {html.escape(test.test_type or 'custom_sql')}</div>")
    parts.append(f"<div><strong>Severity:</strong> {html.escape(test.severity or 'medium')}</div>")
    if test.expected_result:
        parts.append(f"<div><strong>Expected:</strong><pre>{html.escape(test.expected_result)}</pre></div>")
    if test.source_query:
        parts.append(f"<div><strong>Source SQL</strong><pre>{html.escape(test.source_query)}</pre></div>")
    if test.target_query:
        parts.append(f"<div><strong>Target SQL</strong><pre>{html.escape(test.target_query)}</pre></div>")
    return "".join(parts)


def _build_local_test_steps_xml(test: TestCase) -> str:
    steps: List[str] = []
    next_step_id = 2

    def _add_step(action_text: str, expected_text: str) -> None:
        nonlocal next_step_id
        steps.append(
            f'<step id="{next_step_id}" type="ValidateStep">'
            f'<parameterizedString isformatted="true">{html.escape(action_text)}</parameterizedString>'
            f'<parameterizedString isformatted="true">{html.escape(expected_text)}</parameterizedString>'
            f'<description/>'
            f'</step>'
        )
        next_step_id += 2

    if test.source_query:
        expected = "Capture source result for comparison." if test.target_query else "SQL executes successfully."
        _add_step(test.source_query, expected)
    if test.target_query:
        expected = test.expected_result or (
            "Compare target result against source result."
            if test.source_query else "SQL executes successfully."
        )
        _add_step(test.target_query, expected)
    if not steps and test.description:
        _add_step(test.description, test.expected_result or "Review test description.")

    return f'<steps id="0" last="{max(next_step_id - 1, 0)}">{"".join(steps)}</steps>'


async def _ensure_local_folder(db: AsyncSession, folder_name: str) -> Optional[TestFolder]:
    normalized = (folder_name or "").strip()
    if not normalized:
        return None
    existing = await db.execute(select(TestFolder).where(TestFolder.name == normalized))
    folder = existing.scalar_one_or_none()
    if folder:
        return folder
    folder = TestFolder(name=normalized)
    db.add(folder)
    await db.flush()
    return folder


async def _assign_test_to_folder(db: AsyncSession, test_id: int, folder_id: int) -> None:
    existing = await db.execute(select(TestCaseFolder).where(TestCaseFolder.test_case_id == test_id))
    link = existing.scalar_one_or_none()
    if link:
        link.folder_id = folder_id
    else:
        db.add(TestCaseFolder(test_case_id=test_id, folder_id=folder_id))


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("/workitems")
async def list_workitems(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TfsWorkItem).order_by(TfsWorkItem.id.desc()))
    items = result.scalars().all()
    return [_wi_dict(i) for i in items]


@router.get("/workitems/{local_id}")
async def get_workitem(local_id: int, db: AsyncSession = Depends(get_db)):
    wi = await db.get(TfsWorkItem, local_id)
    if not wi:
        raise HTTPException(404, "Work item not found")
    return _wi_dict(wi)


@router.post("/workitems")
async def create_bug(body: CreateBugRequest, db: AsyncSession = Depends(get_db)):
    wi = await create_work_item(
        db, body.title, body.description, body.repro_steps,
        body.test_run_ids, body.severity, body.area_path,
        body.iteration_path, body.tags, body.project,
        body.priority, body.assigned_to, body.state, body.work_item_type,
    )
    return {"id": wi.id, "tfs_id": wi.tfs_id, "state": wi.state}


@router.put("/workitems/{local_id}")
async def update_bug(local_id: int, body: UpdateBugRequest, db: AsyncSession = Depends(get_db)):
    try:
        wi = await update_work_item(
            db, local_id,
            title=body.title, description=body.description,
            state=body.state, assigned_to=body.assigned_to,
            priority=body.priority, severity=body.severity,
            area_path=body.area_path, iteration_path=body.iteration_path,
            tags=body.tags,
        )
        return {"id": wi.id, "tfs_id": wi.tfs_id, "state": wi.state, "status": "updated"}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/workitems/{local_id}")
async def delete_workitem(local_id: int, db: AsyncSession = Depends(get_db)):
    wi = await db.get(TfsWorkItem, local_id)
    if not wi:
        raise HTTPException(404, "Work item not found")
    await db.delete(wi)
    await db.commit()
    return {"deleted": True}


@router.post("/workitems/{local_id}/sync")
async def sync_wi(local_id: int, db: AsyncSession = Depends(get_db)):
    wi = await sync_work_item(db, local_id)
    if not wi:
        raise HTTPException(404, "Work item not found")
    return {"id": wi.id, "tfs_id": wi.tfs_id, "state": wi.state, "assigned_to": wi.assigned_to}


@router.get("/work-item-context/{item_id}")
async def get_work_item_context(item_id: int, project: str = "", refresh: str = ""):
    """Fetch a TFS/Azure DevOps work item's full details (description, acceptance criteria,
    attachments, hyperlinks) for use in AI-powered test generation enrichment."""
    from app.services.tfs_service import fetch_work_item_context
    import logging as _log
    try:
        ctx = await fetch_work_item_context(item_id, project=project)
        return ctx
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.getLogger(__name__).error("work-item-context error for %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/work-item-full-context/{item_id}")
async def get_work_item_full_context(item_id: int, project: str = ""):
    """Fetch work item + download all attachment text content for comprehensive AI analysis."""
    from app.services.tfs_service import fetch_work_item_full_context
    import logging as _log
    try:
        ctx = await fetch_work_item_full_context(item_id, project=project)
        return ctx
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log.getLogger(__name__).error("work-item-full-context error for %s: %s", item_id, e)
        raise HTTPException(status_code=500, detail=str(e))


# ── TFS Queries ──────────────────────────────────────────────────────

@router.get("/projects")
async def list_projects():
    """Return the configured TFS project list."""
    return {"projects": _get_projects()}


@router.get("/config")
async def get_tfs_config():
    return get_tfs_web_context()


@router.post("/query")
async def wiql_query(body: WiqlRequest):
    """Run a WIQL query against TFS."""
    results = await run_wiql_query(body.project, body.query)
    return {"count": len(results), "items": results}


@router.post("/auto-bugs")
async def auto_bugs(body: AutoBugRequest, db: AsyncSession = Depends(get_db)):
    items = await auto_create_bugs_for_batch(db, body.batch_id)
    return {"created": len(items), "items": [{"id": i.id, "tfs_id": i.tfs_id} for i in items]}


# ── Saved Queries ────────────────────────────────────────────────────

@router.get("/saved-queries/{project}")
async def list_saved_queries(project: str, folder: str = "", depth: int = 2):
    """Return the saved query tree for a TFS project."""
    queries = await get_saved_queries(project, folder_path=folder, depth=depth)
    return {"queries": queries}


@router.get("/saved-queries/{project}/run/{query_id}")
async def execute_saved_query(project: str, query_id: str):
    """Execute a saved TFS query by its GUID and return results."""
    result = await run_saved_query(project, query_id)
    return result


@router.get("/preset-queries")
async def list_preset_queries():
    """Return pre-built CDSIntegration queries based on the standard template."""
    return {"queries": get_cds_preset_queries()}


@router.get("/classification-nodes/{project}")
async def list_classification_nodes(project: str, structure_group: str, depth: int = 6):
    nodes = await get_classification_nodes(project, structure_group=structure_group, depth=depth)
    return {"nodes": nodes, "count": len(nodes), "structure_group": structure_group}


# ── Test Management (Test Plans, Suites, Points) ──────────────────────

@router.get("/test-plans/{project}")
async def list_test_plans(project: str, db: AsyncSession = Depends(get_db)):
    """List all active test plans for a project."""
    try:
        plans_data = await get_test_plans(project)
        
        # Cache plans locally
        cached_plans = []
        for plan_data in plans_data:
            cached = await cache_test_plan(db, project, plan_data)
            cached_plans.append({
                "id": cached.plan_id,
                "name": cached.name,
                "state": cached.state,
                "description": cached.description,
                "owner": cached.owner or "",
                "created_date": str(cached.created_date) if cached.created_date else None,
                "root_suite_id": cached.root_suite_id,
            })

        cached_plans.sort(
            key=lambda p: (p.get("created_date") or "", p.get("id") or 0),
            reverse=True,
        )
        
        return {"plans": cached_plans, "count": len(cached_plans)}
    except Exception as e:
        raise HTTPException(500, f"Error listing test plans: {str(e)}")


@router.post("/test-plans")
async def create_plan(body: CreateTestPlanRequest, db: AsyncSession = Depends(get_db)):
    """Create a TFS test plan and cache it locally."""
    try:
        created = await create_test_plan_record(
            body.project,
            body.name,
            body.description,
            body.area_path,
            body.iteration_path,
            body.start_date,
            body.end_date,
        )
        cached = await cache_test_plan(db, body.project, created)
        return {
            "id": cached.id,
            "plan_id": cached.plan_id,
            "name": cached.name,
            "root_suite_id": cached.root_suite_id,
            "project": cached.project,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error creating test plan: {str(e)}")


@router.get("/lookup/{item_id}")
async def lookup_tfs_item(item_id: int):
    """Look up a TFS work item by ID to identify its type/title.
    Used to give helpful feedback when a user enters a suite ID thinking it is a plan ID."""
    result = await lookup_work_item(item_id)
    if not result:
        raise HTTPException(404, f"Work item {item_id} not found")
    return result


@router.get("/test-suites/{project}/{plan_id}")
async def list_test_suites(project: str, plan_id: int, parent_suite_id: int = None, 
                          db: AsyncSession = Depends(get_db)):
    """List test suites for a plan (hierarchical)."""
    try:
        suites_data = await get_test_suites(project, plan_id, parent_suite_id)
        
        # Cache suites locally
        cached_suites = []
        for suite_data in suites_data:
            cached = await cache_test_suite(db, project, plan_id, suite_data)
            cached_suites.append({
                "id": cached.suite_id,
                "name": cached.name,
                "type": cached.suite_type,
                "parent": suite_data.get("parent", {}).get("id"),
                "test_case_count": cached.test_case_count,
                "is_heavy": cached.is_heavy,
                "children": [],  # Client will recursively load children
            })
        
        return {"suites": cached_suites, "count": len(cached_suites)}
    except Exception as e:
        raise HTTPException(500, f"Error listing test suites: {str(e)}")


@router.post("/test-suites")
async def create_suite(body: CreateTestSuiteRequest, db: AsyncSession = Depends(get_db)):
    """Create a TFS test suite and cache it locally."""
    try:
        created = await create_test_suite_record(
            project=body.project,
            plan_id=body.plan_id,
            name=body.name,
            parent_suite_id=body.parent_suite_id,
            suite_type=body.suite_type,
            requirement_id=body.requirement_id,
        )
        cached = await cache_test_suite(db, body.project, body.plan_id, created)
        return {
            "id": cached.id,
            "suite_id": cached.suite_id,
            "plan_id": cached.plan_id,
            "name": cached.name,
            "suite_type": cached.suite_type,
            "project": cached.project,
            "parent_suite_id": cached.parent_suite_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error creating test suite: {str(e)}")


@router.get("/test-points/{project}/{plan_id}/{suite_id}")
async def list_test_points(project: str, plan_id: int, suite_id: int,
                          db: AsyncSession = Depends(get_db)):
    """List test points (test cases) in a suite."""
    try:
        points_data = await get_test_points(project, plan_id, suite_id)
        
        # Cache test points locally
        cached_points = []
        for point_data in points_data:
            cached = await cache_test_point(db, project, plan_id, suite_id, point_data)
            test_case = point_data.get("testCase", {})
            test_point = point_data.get("testPoint", {})
            cached_points.append({
                "test_point_id": cached.test_point_id,
                "test_case_id": cached.test_case_id,
                "title": cached.title,
                "state": cached.state,
                "priority": cached.priority,
                "owner": cached.owner,
                "automation_status": cached.automation_status,
            })
        
        return {"test_points": cached_points, "count": len(cached_points)}
    except Exception as e:
        raise HTTPException(500, f"Error listing test points: {str(e)}")


@router.post("/test-suites/import-local-tests")
async def import_local_tests_to_suite(body: ImportLocalTestsToSuiteRequest,
                                      db: AsyncSession = Depends(get_db)):
    """Create a new static suite under a plan, create TFS test cases from local tests,
    and add those TFS test cases to the suite."""
    if not body.local_test_ids:
        raise HTTPException(400, "No local test IDs provided")

    tests_result = await db.execute(select(TestCase).where(TestCase.id.in_(body.local_test_ids)))
    tests = tests_result.scalars().all()
    tests_by_id = {t.id: t for t in tests}
    ordered_tests = [tests_by_id[test_id] for test_id in body.local_test_ids if test_id in tests_by_id]
    if not ordered_tests:
        raise HTTPException(404, "Selected local tests were not found")

    try:
        destination_suite_id: Optional[int] = None
        destination_suite_name = ""

        if body.destination_suite_id:
            destination_suite_id = int(body.destination_suite_id)
            suites_data = await get_test_suites(body.project, body.plan_id)
            destination_suite = next((s for s in suites_data if int(s.get("id") or 0) == destination_suite_id), None)
            if not destination_suite:
                raise HTTPException(404, f"Destination suite {destination_suite_id} was not found in plan {body.plan_id}")
            destination_suite_name = str(destination_suite.get("name") or "")
            await cache_test_suite(db, body.project, body.plan_id, destination_suite)
        else:
            if not (body.suite_name or "").strip():
                raise HTTPException(400, "suite_name is required when destination_suite_id is not provided")

            created_suite = await create_test_suite_record(
                project=body.project,
                plan_id=body.plan_id,
                name=body.suite_name,
                parent_suite_id=body.parent_suite_id,
                suite_type="StaticTestSuite",
            )
            if not created_suite or not created_suite.get("id"):
                raise HTTPException(500, "Failed to create destination suite in TFS")

            cached_suite = await cache_test_suite(db, body.project, body.plan_id, created_suite)
            destination_suite_id = int(cached_suite.suite_id)
            destination_suite_name = str(cached_suite.name or body.suite_name)

        created_cases = []
        failures = []
        for test in ordered_tests:
            payload = await create_test_case_work_item(
                project=body.project,
                title=test.name,
                description=_build_local_test_description(test),
                steps_xml=_build_local_test_steps_xml(test),
                area_path=body.area_path,
                iteration_path=body.iteration_path,
                assigned_to=body.assigned_to,
                state=body.state,
            )
            tfs_test_case_id = payload.get("id") if isinstance(payload, dict) else None
            if tfs_test_case_id:
                created_cases.append({
                    "local_test_id": test.id,
                    "tfs_test_case_id": int(tfs_test_case_id),
                    "name": test.name,
                })
            else:
                failures.append({"local_test_id": test.id, "name": test.name, "error": "TFS test case creation failed"})

        added = []
        if created_cases:
            added = await add_test_cases_to_suite(
                body.project,
                body.plan_id,
                destination_suite_id,
                [c["tfs_test_case_id"] for c in created_cases],
            )

        return {
            "project": body.project,
            "plan_id": body.plan_id,
            "suite_id": destination_suite_id,
            "suite_name": destination_suite_name,
            "created_test_case_count": len(created_cases),
            "added_test_case_count": len(added),
            "created_test_cases": created_cases,
            "failed_tests": failures,
            "used_existing_suite": bool(body.destination_suite_id),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error importing local tests to TFS suite: {str(e)}")


@router.post("/test-points/import-local")
async def import_tfs_points_to_local_tests(body: ImportTfsPointsToLocalRequest,
                                           db: AsyncSession = Depends(get_db)):
    if not body.test_point_ids:
        raise HTTPException(400, "No test point IDs provided")

    points_payload = await get_test_points(body.project, body.plan_id, body.suite_id)
    points_by_id = {int((item.get("testPoint") or {}).get("id") or item.get("id") or 0): item for item in points_payload}
    selected_points = [points_by_id[tp_id] for tp_id in body.test_point_ids if tp_id in points_by_id]
    if not selected_points:
        raise HTTPException(404, "Selected test points were not found in TFS")

    folder_name = (body.folder_name or "").strip() or f"TFS Suite {body.suite_id}"
    folder = await _ensure_local_folder(db, folder_name)
    created_tests = []
    skipped_tests = []

    for point_data in selected_points:
        test_point = point_data.get("testPoint") or {}
        test_case = point_data.get("testCase") or {}
        point_id = int(test_point.get("id") or 0)
        test_case_id = int(test_case.get("id") or 0)
        title = test_case.get("name") or f"TFS Test Case {test_case_id}"
        case_payload = await get_test_case_details(body.project, test_case_id)
        sql_list = _extract_sql_from_test_case_payload(case_payload)
        readonly_sql = [sql for sql in sql_list if _is_readonly_sql(sql)]

        if not readonly_sql:
            skipped_tests.append({
                "test_point_id": point_id,
                "test_case_id": test_case_id,
                "title": title,
                "reason": "No read-only SQL found in test case",
            })
            continue

        for index, sql in enumerate(readonly_sql, start=1):
            test_name = title if len(readonly_sql) == 1 else f"{title} [step {index}]"
            local_test = TestCase(
                name=test_name,
                test_type="custom_sql",
                source_datasource_id=body.source_datasource_id,
                target_datasource_id=body.target_datasource_id,
                source_query=sql,
                target_query=None,
                expected_result=None,
                severity="medium",
                description=f"Imported from TFS project {body.project}, plan {body.plan_id}, suite {body.suite_id}, test point {point_id}, test case {test_case_id}.",
                is_active=True,
                is_ai_generated=False,
            )
            db.add(local_test)
            await db.flush()
            if folder:
                await _assign_test_to_folder(db, local_test.id, folder.id)
            created_tests.append({
                "id": local_test.id,
                "name": local_test.name,
                "test_point_id": point_id,
                "test_case_id": test_case_id,
            })

    await db.commit()
    return {
        "folder_id": folder.id if folder else None,
        "folder_name": folder.name if folder else folder_name,
        "created_count": len(created_tests),
        "created_tests": created_tests,
        "skipped_tests": skipped_tests,
    }


@router.post("/test-runs")
async def create_run(body: CreateTestRunRequest, background_tasks: BackgroundTasks,
                     db: AsyncSession = Depends(get_db)):
    """Create a new test run and optionally start execution."""
    try:
        # Create in TFS
        run_id = await create_test_run(body.project, body.plan_id,
                                       body.run_name, body.test_point_ids)

        if run_id <= 0:
            raise HTTPException(400, "Failed to create test run in TFS — verify plan ID and test point IDs exist in the selected project")
        
        # Fetch result IDs from TFS so the frontend can update individual results
        result_id_map = await get_result_ids_for_run(body.project, run_id)

        # Cache locally (plan_id may not be in DB yet — use nullable FK safely)
        test_run = TfsTestRun(
            run_id=run_id,
            plan_id=body.plan_id,
            name=body.run_name,
            project=body.project,
            environment=body.environment,
            state="NotStarted",
            total_tests=len(body.test_point_ids),
            test_point_ids=json.dumps(body.test_point_ids),
        )
        db.add(test_run)
        await db.commit()
        await db.refresh(test_run)

        return {
            "id": test_run.id,
            "run_id": test_run.run_id,
            "name": test_run.name,
            "state": test_run.state,
            "total_tests": test_run.total_tests,
            "result_id_map": result_id_map,   # {test_point_id: tfs_result_id}
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error creating test run: {str(e)}")


@router.post("/test-runs/{run_id}/tests/{test_point_id}/execute")
async def execute_test_point(run_id: int, test_point_id: int,
                             body: ExecuteTestPointRequest,
                             db: AsyncSession = Depends(get_db)):
    """Execute SQL embedded in a TFS test case against the chosen datasource.

    Safety:
    - Only read-only SQL is allowed.
    - Destructive SQL can be auto-skipped (default).
    """
    run_query = await db.execute(select(TfsTestRun).where(TfsTestRun.run_id == run_id))
    run = run_query.scalars().first()
    if not run:
        raise HTTPException(404, "Test run not found")

    ds = await db.get(DataSource, body.datasource_id)
    if not ds:
        raise HTTPException(404, "Datasource not found")

    point_query = await db.execute(
        select(TfsTestPoint).where(
            TfsTestPoint.test_point_id == test_point_id,
            TfsTestPoint.plan_id == run.plan_id,
            TfsTestPoint.project == run.project,
        )
    )
    point = point_query.scalars().first()
    if not point:
        raise HTTPException(404, f"Test point {test_point_id} not found in local cache")

    case_payload = await get_test_case_details(run.project, point.test_case_id)
    sql_list = _extract_sql_from_test_case_payload(case_payload)
    if not sql_list:
        return {
            "outcome": "blocked",
            "comment": "No executable SQL found in TFS test case fields.",
            "duration_ms": 0,
        }

    executed = 0
    skipped = 0
    started = time.perf_counter()
    connector = get_connector_from_model(ds)
    try:
        await asyncio.to_thread(connector.connect)
        for sql in sql_list:
            if body.skip_destructive and _is_destructive_sql(sql):
                skipped += 1
                continue
            if not _is_readonly_sql(sql):
                if body.skip_destructive:
                    skipped += 1
                    continue
                raise RuntimeError("Non read-only SQL found in test case")

            query = re.sub(r";\s*$", "", sql.strip())
            await asyncio.to_thread(connector.execute_query, query)
            executed += 1
    except Exception as e:
        elapsed = int((time.perf_counter() - started) * 1000)
        return {
            "outcome": "failed",
            "comment": f"Execution error: {str(e)}",
            "error_msg": str(e),
            "duration_ms": elapsed,
        }
    finally:
        try:
            await asyncio.to_thread(connector.disconnect)
        except Exception:
            pass

    elapsed = int((time.perf_counter() - started) * 1000)
    if executed == 0 and skipped > 0:
        return {
            "outcome": "blocked",
            "comment": f"Skipped {skipped} destructive/non-read-only SQL step(s).",
            "duration_ms": elapsed,
        }
    if executed == 0:
        return {
            "outcome": "blocked",
            "comment": "No executable read-only SQL found after filtering.",
            "duration_ms": elapsed,
        }
    extra = f"; skipped {skipped}" if skipped else ""
    return {
        "outcome": "passed",
        "comment": f"Executed {executed} SQL step(s){extra} on datasource {ds.name}.",
        "duration_ms": elapsed,
    }


@router.get("/test-runs/{run_id}")
async def get_run_details(run_id: int, db: AsyncSession = Depends(get_db)):
    """Get details of a test run including current status."""
    try:
        from sqlalchemy import and_
        tr = await db.execute(
            select(TfsTestRun).where(TfsTestRun.run_id == run_id)
        )
        run = tr.scalars().first()
        
        if not run:
            raise HTTPException(404, "Test run not found")
        
        # Fetch results
        result_query = await db.execute(
            select(TfsTestResult).where(TfsTestResult.run_id == run_id)
        )
        results = result_query.scalars().all()
        
        return {
            "id": run.id,
            "run_id": run.run_id,
            "name": run.name,
            "state": run.state,
            "total_tests": run.total_tests,
            "passed_count": run.passed_count,
            "failed_count": run.failed_count,
            "blocked_count": run.blocked_count,
            "not_run_count": run.not_run_count,
            "environment": run.environment,
            "started_at": str(run.started_at) if run.started_at else None,
            "completed_at": str(run.completed_at) if run.completed_at else None,
            "results_count": len(results),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error fetching test run: {str(e)}")


@router.put("/test-runs/{run_id}/tests/{test_point_id}/result")
async def update_run_result(run_id: int, test_point_id: int,
                           body: UpdateTestResultRequest,
                           db: AsyncSession = Depends(get_db)):
    """Update result for a single test in a run."""
    try:
        from sqlalchemy import and_
        run_query = await db.execute(
            select(TfsTestRun).where(TfsTestRun.run_id == run_id)
        )
        run = run_query.scalars().first()
        if not run:
            raise HTTPException(404, "Test run not found")

        # result_id should be provided by the frontend (from result_id_map returned on create)
        result_id = body.result_id
        if not result_id:
            # Fallback: fetch from TFS
            mapping = await get_result_ids_for_run(run.project, run_id)
            result_id = mapping.get(test_point_id)
        if not result_id:
            raise HTTPException(404, f"TFS result ID not found for test point {test_point_id}")

        success = await update_test_result(
            run.project, run_id, result_id,
            body.outcome, body.comment, body.error_msg, int(body.duration_ms or 0)
        )
        if not success:
            raise HTTPException(500, "Failed to update test result in TFS")

        # Update local result record
        result_query = await db.execute(
            select(TfsTestResult).where(
                and_(TfsTestResult.run_id == run_id,
                     TfsTestResult.test_point_id == test_point_id)
            )
        )
        result = result_query.scalars().first()
        if not result:
            result = TfsTestResult(
                run_id=run_id,
                test_point_id=test_point_id,
                test_case_id=test_point_id,
            )
            db.add(result)

        result.outcome = body.outcome
        result.comment = body.comment
        result.error_message = body.error_msg
        result.duration_ms = int(body.duration_ms or 0)
        result.state = "Completed"

        if body.outcome.lower() == 'passed':
            run.passed_count = (run.passed_count or 0) + 1
        elif body.outcome.lower() == 'failed':
            run.failed_count = (run.failed_count or 0) + 1
        elif body.outcome.lower() == 'blocked':
            run.blocked_count = (run.blocked_count or 0) + 1

        await db.commit()
        return {
            "result_id": result.id,
            "outcome": result.outcome,
            "state": result.state,
            "run_summary": {
                "passed": run.passed_count,
                "failed": run.failed_count,
                "blocked": run.blocked_count,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error updating test result: {str(e)}")


@router.post("/test-runs/{run_id}/complete")
async def complete_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """Mark a TFS test run as Completed."""
    try:
        run_query = await db.execute(select(TfsTestRun).where(TfsTestRun.run_id == run_id))
        run = run_query.scalars().first()
        if not run:
            raise HTTPException(404, "Test run not found")

        success = await complete_test_run(run.project, run_id)
        if not success:
            raise HTTPException(500, "Failed to mark run as Completed in TFS")

        run.state = "Completed"
        from datetime import datetime, timezone
        run.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return {"run_id": run_id, "state": "Completed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error completing test run: {str(e)}")
        
        return {
            "result_id": result.id,
            "outcome": result.outcome,
            "state": result.state,
            "run_summary": {
                "passed": run.passed_count,
                "failed": run.failed_count,
                "blocked": run.blocked_count,
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Error updating test result: {str(e)}")


# ── Helper ───────────────────────────────────────────────────────────

def _wi_dict(i: TfsWorkItem) -> dict:
    return {
        "id": i.id, "tfs_id": i.tfs_id, "title": i.title,
        "state": i.state, "assigned_to": i.assigned_to,
        "work_item_type": i.work_item_type, "priority": i.priority,
        "description": i.description, "repro_steps": i.repro_steps,
        "failure_signature": i.failure_signature,
        "area_path": i.area_path, "iteration_path": i.iteration_path,
        "tags": i.tags, "project": i.project,
        "web_url": build_tfs_work_item_web_url(i.project or "", i.tfs_id) if i.tfs_id else "",
        "last_synced_at": str(i.last_synced_at) if i.last_synced_at else None,
        "created_at": str(i.created_at) if i.created_at else None,
    }
