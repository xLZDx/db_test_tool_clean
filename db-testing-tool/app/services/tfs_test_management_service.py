"""TFS Test Management & Execution Service - Azure DevOps REST API Integration."""
import aiohttp
import base64
import json
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.models.tfs_test_management import (
    TfsTestPlan, TfsTestSuite, TfsTestPoint, TfsTestRun, TfsTestResult
)

logger = logging.getLogger(__name__)


def _headers() -> dict:
    """Generate Azure DevOps REST API headers with PAT auth."""
    token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _wit_headers() -> dict:
    """Generate work item PATCH headers with PAT auth."""
    token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json-patch+json",
    }


def _api_url(path: str, project: str | None = None, collection: str | None = None) -> str:
    """Build Azure DevOps REST API URL."""
    base = settings.TFS_BASE_URL.rstrip("/")
    coll = collection or settings.TFS_COLLECTION
    proj = project or ""
    if proj:
        return f"{base}/{coll}/{proj}/_apis/{path}"
    else:
        return f"{base}/{coll}/_apis/{path}"


def _get_projects() -> list:
    """Return list of configured TFS projects."""
    raw = settings.TFS_PROJECT.strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


async def get_test_plans(project: str) -> List[Dict]:
    """Fetch all test plans for a project from Azure DevOps.
    
    Returns:
        List of dicts: {id, name, description, state, areaPath, createdDate, ...}
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        logger.warning("TFS not configured - cannot fetch test plans")
        return []
    
    try:
        # Azure DevOps API endpoint for test plans
        url = _api_url("test/plans?api-version=5.0", project=project)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Failed to fetch test plans ({resp.status}): {text}")
                    return []
                
                data = await resp.json()
                plans = data.get("value", [])
                logger.info(f"Fetched {len(plans)} test plans for project {project}")
                # Batch-enrich owner/date fields via work items API (on-prem TFS omits these on /test/plans)
                assigned_to_map = await _fetch_plan_metadata([p["id"] for p in plans])
                for p in plans:
                    meta = assigned_to_map.get(p["id"], {})
                    owner = meta.get("owner")
                    created = meta.get("created_date")
                    changed = meta.get("changed_date")
                    if p.get("owner") is None and owner:
                        p["owner"] = {"uniqueName": owner, "displayName": owner}
                    if not p.get("createdDate"):
                        p["createdDate"] = created or changed
                logger.info(f"Enriched {len(assigned_to_map)} plan owners for project {project}")
                return plans
    
    except Exception as e:
        logger.exception(f"Error fetching test plans: {e}")
        return []


async def get_test_plan(project: str, plan_id: int) -> Dict:
    """Fetch a single test plan by ID."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}

    try:
        url = _api_url(f"test/plans/{plan_id}?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    return await resp.json()
                text = await resp.text()
                logger.warning(f"Could not fetch test plan {plan_id} ({resp.status}): {text}")
    except Exception as e:
        logger.exception(f"Error fetching test plan {plan_id}: {e}")
    return {}


async def create_test_plan_record(project: str, name: str, description: str = "",
                                  area_path: str = "", iteration_path: str = "",
                                  start_date: str = "", end_date: str = "") -> Dict:
    """Create a TFS test plan via the on-prem 5.0 API."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}

    body: Dict = {"name": name}
    if description:
        body["description"] = description
    if area_path:
        body["area"] = {"name": area_path}
    if iteration_path:
        body["iteration"] = iteration_path
    if start_date:
        body["startDate"] = start_date
    if end_date:
        body["endDate"] = end_date

    variants = []
    base_body: Dict = {"name": name}
    if description:
        base_body["description"] = description
    if start_date:
        base_body["startDate"] = start_date
    if end_date:
        base_body["endDate"] = end_date

    variant1 = dict(base_body)
    if area_path:
        variant1["area"] = {"name": area_path}
    if iteration_path:
        variant1["iteration"] = iteration_path
    variants.append(variant1)

    variant2 = dict(base_body)
    if area_path:
        variant2["areaPath"] = area_path
    if iteration_path:
        variant2["iteration"] = {"name": iteration_path}
    variants.append(variant2)

    variant3 = dict(base_body)
    if area_path:
        variant3["area"] = {"path": area_path, "name": area_path}
    if iteration_path:
        variant3["iteration"] = {"path": iteration_path, "name": iteration_path}
    variants.append(variant3)

    last_error = ""
    try:
        url = _api_url("test/plans?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            for payload in variants:
                async with session.post(url, headers=_headers(), json=payload, ssl=False,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status in (200, 201):
                        return await resp.json()
                    text = await resp.text()
                    last_error = f"TFS returned {resp.status}: {text}"
                    logger.error(f"Failed to create test plan ({resp.status}): {text}")
    except Exception as e:
        logger.exception(f"Error creating test plan: {e}")
        raise RuntimeError(str(e)) from e
    raise RuntimeError(last_error or "Unknown TFS error while creating test plan")


async def create_test_suite_record(project: str, plan_id: int, name: str = "",
                                   parent_suite_id: Optional[int] = None,
                                   suite_type: str = "StaticTestSuite",
                                   requirement_id: Optional[int] = None) -> Dict:
    """Create a static or requirement-based suite under a test plan."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}

    plan = await get_test_plan(project, plan_id)
    root_suite = plan.get("rootSuite") or {}
    root_id = root_suite.get("id")
    root_parent = int(root_id) if root_id else None

    resolved_parent = parent_suite_id or root_parent

    # TFS does not allow creating a suite under non-static parent suites.
    # If the selected parent is not static (or cannot be resolved), fall back to plan root.
    if parent_suite_id:
        try:
            suites = await get_test_suites(project, plan_id)
            selected_parent = next((s for s in suites if int(s.get("id") or 0) == int(parent_suite_id)), None)
            selected_parent_type = str((selected_parent or {}).get("suiteType") or "")
            if not selected_parent or selected_parent_type.lower() != "statictestsuite":
                logger.warning(
                    "Parent suite %s is not static or not found (type=%s). Falling back to root suite %s for plan %s",
                    parent_suite_id,
                    selected_parent_type,
                    root_parent,
                    plan_id,
                )
                resolved_parent = root_parent
        except Exception as ex:
            logger.warning("Could not validate parent suite %s for plan %s: %s", parent_suite_id, plan_id, ex)
            resolved_parent = root_parent

    if not resolved_parent:
        logger.error(f"Unable to resolve parent suite for plan {plan_id}")
        raise RuntimeError(f"Unable to resolve parent suite for plan {plan_id}")

    suite_type_norm = (suite_type or "StaticTestSuite").strip()
    body: Dict = {"suiteType": suite_type_norm}
    if suite_type_norm.lower() == "requirementtestsuite":
        if not requirement_id:
            logger.error("Requirement suite creation requested without requirement_id")
            raise RuntimeError("Requirement suite creation requested without requirement_id")
        body["requirementIds"] = [int(requirement_id)]
    else:
        body["name"] = name

    last_error = ""
    try:
        url = _api_url(f"test/plans/{plan_id}/suites/{resolved_parent}?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_headers(), json=body, ssl=False,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    if isinstance(data, dict) and isinstance(data.get("value"), list):
                        return (data.get("value") or [{}])[0]
                    return data
                text = await resp.text()
                last_error = f"TFS returned {resp.status}: {text}"
                logger.error(f"Failed to create test suite ({resp.status}): {text}")
    except Exception as e:
        logger.exception(f"Error creating test suite: {e}")
        raise RuntimeError(str(e)) from e
    raise RuntimeError(last_error or "Unknown TFS error while creating test suite")


async def get_classification_nodes(project: str, structure_group: str, depth: int = 6) -> List[Dict]:
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return []

    structure = (structure_group or "").strip().lower()
    if structure not in {"areas", "iterations"}:
        return []

    try:
        url = _api_url(f"wit/classificationnodes/{structure}?$depth={int(depth)}&api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Failed to fetch {structure} classification nodes ({resp.status}): {text}")
                    return []
                root = await resp.json()

        results: List[Dict] = []

        def _walk(node: Dict, parent_path: str = "") -> None:
            name = str(node.get("name") or "").strip()
            if not name:
                return
            path = f"{parent_path}\\{name}" if parent_path else name
            results.append({
                "name": name,
                "path": path,
                "id": node.get("id"),
                "has_children": bool(node.get("children")),
            })
            for child in node.get("children", []) or []:
                _walk(child, path)

        _walk(root)
        return results
    except Exception as e:
        logger.exception(f"Error fetching {structure_group} classification nodes: {e}")
        return []


async def create_test_case_work_item(project: str, title: str, description: str = "",
                                     steps_xml: str = "", area_path: str = "",
                                     iteration_path: str = "", assigned_to: str = "",
                                     state: str = "Design") -> Dict:
    """Create a TFS Test Case work item for later suite assignment."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}

    body: List[Dict] = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
    ]
    if description:
        body.append({"op": "add", "path": "/fields/System.Description", "value": description})
    if steps_xml:
        body.append({"op": "add", "path": "/fields/Microsoft.VSTS.TCM.Steps", "value": steps_xml})
    if area_path:
        body.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
    if iteration_path:
        body.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
    if assigned_to:
        body.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assigned_to})
    if state:
        body.append({"op": "add", "path": "/fields/System.State", "value": state})

    try:
        url = _api_url("wit/workitems/$Test%20Case?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_wit_headers(), json=body, ssl=False,
                                    timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                text = await resp.text()
                logger.error(f"Failed to create TFS test case ({resp.status}): {text}")
    except Exception as e:
        logger.exception(f"Error creating TFS test case: {e}")
    return {}


async def add_test_cases_to_suite(project: str, plan_id: int, suite_id: int,
                                  test_case_ids: List[int]) -> List[Dict]:
    """Add existing TFS test case work items to a suite."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT or not test_case_ids:
        return []

    normalized_ids = [int(x) for x in test_case_ids if x]
    if not normalized_ids:
        return []

    # Keep URL length safe for TFS/IIS by batching test case IDs.
    chunk_size = 20
    added_all: List[Dict] = []

    try:
        async with aiohttp.ClientSession() as session:
            for start in range(0, len(normalized_ids), chunk_size):
                chunk = normalized_ids[start:start + chunk_size]
                ids_csv = ",".join(str(x) for x in chunk)
                url = _api_url(
                    f"test/plans/{plan_id}/suites/{suite_id}/testcases/{ids_csv}?api-version=5.0",
                    project=project,
                )
                async with session.post(url, headers=_headers(), ssl=False,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        values = data.get("value", []) if isinstance(data, dict) else []
                        if isinstance(values, list):
                            added_all.extend(values)
                        continue
                    text = await resp.text()
                    logger.error(
                        "Failed to add test case chunk to suite (%s). plan=%s suite=%s start=%s size=%s: %s",
                        resp.status,
                        plan_id,
                        suite_id,
                        start,
                        len(chunk),
                        text,
                    )
    except Exception as e:
        logger.exception(f"Error adding test cases to suite: {e}")
    return added_all


async def _fetch_plan_metadata(plan_ids: List[int]) -> Dict[int, Dict[str, str]]:
    """Batch-fetch metadata from WI API for plan IDs.
    Returns dict {plan_id: {owner, created_date, changed_date}}. Batches <=200 IDs per request."""
    if not plan_ids:
        return {}
    result: Dict[int, Dict[str, str]] = {}
    BATCH = 200
    try:
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(plan_ids), BATCH):
                chunk = plan_ids[i:i + BATCH]
                ids_param = ",".join(str(x) for x in chunk)
                url = _api_url(
                    f"wit/workitems?ids={ids_param}&fields=System.AssignedTo,System.CreatedDate,System.ChangedDate&api-version=5.0"
                )
                async with session.get(url, headers=_headers(), ssl=False,
                                       timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        for wi in (await resp.json()).get("value", []):
                            fields = wi.get("fields", {})
                            at = fields.get("System.AssignedTo") or {}
                            unique = at.get("uniqueName", "") if isinstance(at, dict) else str(at)
                            result[wi["id"]] = {
                                "owner": unique,
                                "created_date": fields.get("System.CreatedDate", ""),
                                "changed_date": fields.get("System.ChangedDate", ""),
                            }
    except Exception as e:
        logger.warning(f"Could not batch-fetch plan metadata: {e}")
    return result


async def lookup_work_item(item_id: int) -> Dict:
    """Look up a TFS work item to identify its type/title (used when a plan ID returns no results)."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}
    try:
        url = _api_url(f"wit/workitems/{item_id}?fields=System.WorkItemType,System.Title,System.AreaPath&api-version=5.0")
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    fields = (await resp.json()).get("fields", {})
                    return {
                        "id": item_id,
                        "type": fields.get("System.WorkItemType", ""),
                        "title": fields.get("System.Title", ""),
                        "area_path": fields.get("System.AreaPath", ""),
                    }
    except Exception as e:
        logger.warning(f"Could not lookup work item {item_id}: {e}")
    return {}


async def resolve_plan_project(plan_id: int, preferred_project: str = None) -> Optional[str]:
    """Find which configured TFS project actually contains a given test plan.

    This is needed because plans fetched via a team-alias URL (e.g. 'Lighthouse')
    may physically reside in a different Team Project (e.g. 'CDSIntegration').
    The preferred_project is tried first.
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return preferred_project

    all_projects = _get_projects()
    projects_to_try = ([preferred_project] if preferred_project and preferred_project in all_projects else []) + \
                      [p for p in all_projects if p != preferred_project]

    for try_project in projects_to_try:
        try:
            url = _api_url(f"test/plans/{plan_id}?api-version=5.0", project=try_project)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=_headers(), ssl=False,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return try_project
        except Exception:
            pass
    return preferred_project or (all_projects[0] if all_projects else None)


async def get_test_suites(project: str, plan_id: int, parent_suite_id: int = None) -> Tuple[List[Dict], str]:
    """Recursively fetch test suites for a plan.
    
    Args:
        project: TFS project name (preferred; will try other configured projects on 404)
        plan_id: Test plan ID
        parent_suite_id: Filter to suites under parent (None = root)
    
    Returns:
        Tuple of (suites list, actual_project used) where actual_project may differ
        from the requested project if a cross-project fallback resolved the plan.
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return [], project

    # Build ordered list: try requested project first, then others
    all_projects = _get_projects()
    projects_to_try = [project] + [p for p in all_projects if p != project]

    for try_project in projects_to_try:
        try:
            url = _api_url(f"test/plans/{plan_id}/suites?api-version=5.0", project=try_project)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=_headers(), ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 404 and try_project != projects_to_try[-1]:
                        # Plan not in this project; try next
                        logger.debug(f"Plan {plan_id} not found in project '{try_project}', trying fallback")
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Failed to fetch test suites for plan {plan_id} in '{try_project}' ({resp.status}): {text}")
                        if try_project != projects_to_try[-1]:
                            continue
                        return [], project

                    data = await resp.json()
                    suites = data.get("value", [])

                    if parent_suite_id is not None:
                        suites = [s for s in suites if s.get("parent", {}).get("id") == parent_suite_id]

                    if try_project != project:
                        logger.info(f"Resolved plan {plan_id} suites via fallback project '{try_project}' (requested '{project}')")
                    logger.info(f"Fetched {len(suites)} test suites for plan {plan_id} in '{try_project}'")
                    return suites, try_project

        except Exception as e:
            logger.exception(f"Error fetching test suites for plan {plan_id} in '{try_project}': {e}")
            if try_project != projects_to_try[-1]:
                continue

    return [], project


async def get_test_points(project: str, plan_id: int, suite_id: int) -> Tuple[List[Dict], str]:
    """Fetch all test points (test cases) in a suite.

    Args:
        project: TFS project name (preferred; will try other configured projects on 404)
        plan_id: Test plan ID
        suite_id: Suite ID

    Returns:
        Tuple of (test_points list, actual_project used).
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return [], project

    all_projects = _get_projects()
    projects_to_try = [project] + [p for p in all_projects if p != project]

    for try_project in projects_to_try:
        try:
            # On Azure DevOps Server/TFS, the stable endpoint is `/points` (not `/testpoint`).
            url = _api_url(f"test/plans/{plan_id}/suites/{suite_id}/points?api-version=5.0", project=try_project)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=_headers(), ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 404 and try_project != projects_to_try[-1]:
                        logger.debug(f"Suite {suite_id}/plan {plan_id} not found in '{try_project}', trying fallback")
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Failed to fetch test points for suite {suite_id} in '{try_project}' ({resp.status}): {text}")
                        if try_project != projects_to_try[-1]:
                            continue
                        return [], project

                    data = await resp.json()
                    points = data.get("value", [])
                    if try_project != project:
                        logger.info(f"Resolved suite {suite_id} test points via fallback project '{try_project}' (requested '{project}')")
                    logger.info(f"Fetched {len(points)} test points for suite {suite_id} in '{try_project}'")
                    return points, try_project

        except Exception as e:
            logger.exception(f"Error fetching test points for suite {suite_id} in '{try_project}': {e}")
            if try_project != projects_to_try[-1]:
                continue

    return [], project


async def get_test_case_details(project: str, test_case_id: int) -> Dict:
    """Fetch detailed info about a test case including parameters.
    
    Returns:
        Dict: {id, name, description, parameterSets, ...}
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}
    
    try:
        # On on-prem TFS, test case step content is reliably available via WIT fields.
        url = _api_url(
            f"wit/workitems/{test_case_id}?fields="
            f"System.Title,System.Description,Microsoft.VSTS.TCM.Steps,Microsoft.VSTS.TCM.Parameters"
            f"&api-version=5.0",
            project=project,
        )
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logger.warning(f"Could not fetch test case {test_case_id}: {resp.status}")
                    return {}
    
    except Exception as e:
        logger.exception(f"Error fetching test case details: {e}")
        return {}


async def create_test_run(project: str, plan_id: int, name: str, 
                          test_point_ids: List[int] = None) -> int:
    """Create a new test run in TFS.
    
    Returns:
        TFS Test Run ID, or -1 if failed
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        logger.warning("TFS not configured - cannot create test run")
        return -1
    
    try:
        url = _api_url(f"test/runs?api-version=5.0", project=project)
        
        body = {
            "name": name,
            "plan": {"id": plan_id},
        }
        
        # If specific test points provided, add them
        if test_point_ids:
            body["pointIds"] = test_point_ids
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_headers(), json=body, ssl=False, 
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    run_id = data.get("id")
                    logger.info(f"Created test run #{run_id} with name '{name}'")
                    return run_id
                else:
                    text = await resp.text()
                    logger.error(f"Failed to create test run ({resp.status}): {text}")
                    return -1
    
    except Exception as e:
        logger.exception(f"Error creating test run: {e}")
        return -1


_OUTCOME_MAP = {
    'passed': 'Passed', 'pass': 'Passed',
    'failed': 'Failed', 'fail': 'Failed',
    'blocked': 'Blocked', 'block': 'Blocked',
    'notrun': 'NotRun', 'not_run': 'NotRun',
    'inconclusive': 'Inconclusive',
}


async def get_result_ids_for_run(project: str, run_id: int) -> Dict[int, int]:
    """Fetch TFS result entries for a run and return {test_point_id: result_id}."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}
    try:
        url = _api_url(f"test/runs/{run_id}/results?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    mapping: Dict[int, int] = {}
                    for r in (await resp.json()).get("value", []):
                        tp_id = r.get("testPoint", {}).get("id")
                        result_id = r.get("id")
                        if tp_id and result_id:
                            mapping[int(tp_id)] = int(result_id)
                    return mapping
    except Exception as e:
        logger.warning(f"Could not fetch result IDs for run {run_id}: {e}")
    return {}


async def update_test_result(project: str, run_id: int, result_id: int,
                             outcome: str, comment: str = "",
                             error_msg: str = "", duration_ms: int = 0) -> bool:
    """Update a single test result using TFS batch-PATCH on /results.

    Args:
        project: TFS project
        run_id: TFS Test Run ID
        result_id: TFS Result ID (from get_result_ids_for_run)
        outcome: 'passed', 'failed', 'blocked', 'notRun', 'inconclusive'
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return False

    tfs_outcome = _OUTCOME_MAP.get(outcome.lower(), 'NotRun')
    item: Dict = {"id": result_id, "state": "Completed", "outcome": tfs_outcome}
    if comment:
        item["comment"] = comment
    if error_msg:
        item["errorMessage"] = error_msg
    if duration_ms:
        item["durationInMs"] = int(duration_ms)

    try:
        url = _api_url(f"test/runs/{run_id}/results?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=_headers(), json=[item], ssl=False,
                                     timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Updated TFS result {result_id} for run {run_id}: {tfs_outcome}")
                    return True
                else:
                    text = await resp.text()
                    logger.error(f"Failed to update test result ({resp.status}): {text}")
                    return False
    except Exception as e:
        logger.exception(f"Error updating test result: {e}")
        return False


async def complete_test_run(project: str, run_id: int) -> bool:
    """Mark a TFS test run as Completed."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return False
    try:
        url = _api_url(f"test/runs/{run_id}?api-version=5.0", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.patch(url, headers=_headers(), json={"state": "Completed"},
                                     ssl=False, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in (200, 204):
                    logger.info(f"Marked TFS run {run_id} as Completed")
                    return True
                else:
                    text = await resp.text()
                    logger.error(f"Failed to complete run ({resp.status}): {text}")
                    return False
    except Exception as e:
        logger.exception(f"Error completing test run: {e}")
        return False


async def get_test_run_details(project: str, run_id: int) -> Dict:
    """Fetch details of a test run including its results.
    
    Returns:
        Dict with run metadata and results
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {}
    
    try:
        url = _api_url(f"test/runs/{run_id}?api-version=5.0", project=project)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                  timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data
                else:
                    logger.warning(f"Could not fetch test run {run_id}: {resp.status}")
                    return {}
    
    except Exception as e:
        logger.exception(f"Error fetching test run details: {e}")
        return {}


async def get_test_run_results(project: str, run_id: int) -> List[Dict]:
    """Fetch all test results for a run.
    
    Returns:
        List of result dicts
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return []
    
    try:
        url = _api_url(f"test/runs/{run_id}/results?api-version=5.0", project=project)
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers(), ssl=False,
                                  timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("value", [])
                else:
                    logger.warning(f"Could not fetch test run results {run_id}: {resp.status}")
                    return []
    
    except Exception as e:
        logger.exception(f"Error fetching test run results: {e}")
        return []


# ── Database helpers for caching & tracking ──────────────────────────────────

async def cache_test_plan(db: AsyncSession, project: str, plan_data: Dict) -> TfsTestPlan:
    """Cache a test plan locally."""
    existing = await db.execute(
        select(TfsTestPlan).where(
            TfsTestPlan.plan_id == plan_data.get("id"),
            TfsTestPlan.project == project
        )
    )
    plan = existing.scalars().first()
    
    # iteration may be a plain string (on-prem TFS) or dict {"path": "..."}
    _iteration = plan_data.get("iteration", "")
    iteration_path = _iteration if isinstance(_iteration, str) else (_iteration or {}).get("path", "")
    # area may be {"name": "..."} (on-prem) or {"path": "..."} (cloud)
    _area = plan_data.get("area") or {}
    area_path = _area.get("path") or _area.get("name", "")
    # rootSuite id may be a string
    _root_suite = plan_data.get("rootSuite") or {}
    _root_suite_id = _root_suite.get("id")
    root_suite_id = int(_root_suite_id) if _root_suite_id else None

    _owner_obj = plan_data.get("owner") or {}
    owner_str = _owner_obj.get("displayName", "") or _owner_obj.get("uniqueName", "")
    created_raw = plan_data.get("createdDate")
    created_dt = None
    if isinstance(created_raw, datetime):
        created_dt = created_raw
    elif isinstance(created_raw, str) and created_raw.strip():
        try:
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            created_dt = None

    if not plan:
        plan = TfsTestPlan(
            plan_id=plan_data.get("id"),
            name=plan_data.get("name", ""),
            project=project,
            state=plan_data.get("state", "Active"),
            description=plan_data.get("description", ""),
            area_path=area_path,
            iteration_path=iteration_path,
            owner=owner_str,
            created_date=created_dt,
            root_suite_id=root_suite_id,
        )
        db.add(plan)
    else:
        plan.name = plan_data.get("name", plan.name)
        plan.state = plan_data.get("state", plan.state)
        plan.description = plan_data.get("description", plan.description)
        if owner_str:
            plan.owner = owner_str
        if created_dt:
            plan.created_date = created_dt
    
    plan.last_synced_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(plan)
    return plan


async def cache_test_suite(db: AsyncSession, project: str, plan_id: int, 
                           suite_data: Dict) -> TfsTestSuite:
    """Cache a test suite locally."""
    existing = await db.execute(
        select(TfsTestSuite).where(
            TfsTestSuite.suite_id == suite_data.get("id"),
            TfsTestSuite.project == project
        )
    )
    suite = existing.scalars().first()
    
    parent_id = None
    parent = suite_data.get("parent", {})
    if parent:
        parent_id = parent.get("id")
    
    is_heavy = False
    title = suite_data.get("name", "")
    heavy_suites = ["Regression", "Archive", "Archiv"]
    for heavy_name in heavy_suites:
        if heavy_name.lower() in title.lower():
            is_heavy = True
            break
    
    if not suite:
        suite = TfsTestSuite(
            suite_id=suite_data.get("id"),
            plan_id=plan_id,
            parent_suite_id=parent_id,
            name=title,
            project=project,
            suite_type=suite_data.get("suiteType", "StaticTestSuite"),
            test_case_count=suite_data.get("testCaseCount", 0),
            is_heavy=is_heavy,
        )
        db.add(suite)
    else:
        suite.name = title
        suite.parent_suite_id = parent_id
        suite.test_case_count = suite_data.get("testCaseCount", suite.test_case_count)
        suite.is_heavy = is_heavy
    
    suite.last_synced_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(suite)
    return suite


async def cache_test_point(db: AsyncSession, project: str, plan_id: int,
                           suite_id: int, point_data: Dict) -> TfsTestPoint:
    """Cache a test point (test case) locally."""
    test_case = point_data.get("testCase", {})
    test_point = point_data.get("testPoint", {})

    # TFS may return either `{ testPoint: {id,...}, testCase: {...} }`
    # or flat point shape `{ id, state, assignedTo, testCase: {...} }`.
    point_id = test_point.get("id") or point_data.get("id")
    point_state = test_point.get("state") or point_data.get("state", "Active")
    assigned_to = (test_case.get("owner", {}) or {}).get("displayName", "")
    if not assigned_to:
        assigned_to = (point_data.get("assignedTo", {}) or {}).get("displayName", "")
    
    existing = await db.execute(
        select(TfsTestPoint).where(
            TfsTestPoint.test_point_id == point_id,
            TfsTestPoint.project == project
        )
    )
    tp = existing.scalars().first()
    
    if not tp:
        tp = TfsTestPoint(
            test_point_id=point_id,
            test_case_id=test_case.get("id"),
            suite_id=suite_id,
            plan_id=plan_id,
            project=project,
            title=test_case.get("name", ""),
            description=test_case.get("description", ""),
            state=point_state,
            priority=test_case.get("priority", 3),
            automation_status=test_case.get("automationStatus", ""),
            owner=assigned_to,
        )
        db.add(tp)
    else:
        tp.title = test_case.get("name", tp.title)
        tp.description = test_case.get("description", tp.description)
        tp.state = point_state
        if assigned_to:
            tp.owner = assigned_to
    
    tp.last_synced_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(tp)
    return tp
