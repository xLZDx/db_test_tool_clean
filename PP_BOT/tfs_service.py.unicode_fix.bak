"""TFS / Azure DevOps integration service."""
from typing import Optional, List
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.tfs_workitem import TfsWorkItem
from app.models.test_case import TestRun
from app.config import settings
import json, logging, aiohttp, re
from datetime import datetime, timezone
import io

logger = logging.getLogger(__name__)


def _headers() -> dict:
    import base64
    token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json-patch+json",
    }


def _get_projects() -> list:
    """Return list of configured TFS projects."""
    raw = settings.TFS_PROJECT.strip()
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _api_url(path: str, project: str | None = None) -> str:
    base = settings.TFS_BASE_URL.rstrip("/")
    collection = settings.TFS_COLLECTION
    proj = project or (_get_projects()[0] if _get_projects() else "")
    return f"{base}/{collection}/{proj}/_apis/{path}"


def build_tfs_work_item_web_url(project: str, item_id: int | str) -> str:
    base = settings.TFS_BASE_URL.rstrip("/") if settings.TFS_BASE_URL else ""
    collection = settings.TFS_COLLECTION.strip("/")
    proj = (project or (_get_projects()[0] if _get_projects() else "")).strip("/")
    if not base or not collection or not proj or not item_id:
        return ""
    return f"{base}/{collection}/{proj}/_workitems/edit/{item_id}"


def build_tfs_test_run_web_url(project: str, run_id: int | str) -> str:
    base = settings.TFS_BASE_URL.rstrip("/") if settings.TFS_BASE_URL else ""
    collection = settings.TFS_COLLECTION.strip("/")
    proj = (project or (_get_projects()[0] if _get_projects() else "")).strip("/")
    if not base or not collection or not proj or not run_id:
        return ""
    return f"{base}/{collection}/{proj}/_testManagement/runs?_a=resultQuery&runId={run_id}"


def get_tfs_web_context() -> dict:
    return {
        "base_url": settings.TFS_BASE_URL.rstrip("/") if settings.TFS_BASE_URL else "",
        "collection": settings.TFS_COLLECTION.strip("/") if settings.TFS_COLLECTION else "",
        "projects": _get_projects(),
    }


async def create_work_item(db: AsyncSession, title: str, description: str,
                            repro_steps: str, test_run_ids: List[int],
                            severity: str = "3 - Medium",
                            area_path: str = "", iteration_path: str = "",
                            tags: str = "AutoTest",
                            project: str = "",
                            priority: int = 2,
                            assigned_to: str = "",
                            state: str = "New",
                            work_item_type: str = "Bug") -> TfsWorkItem:
    """Create a Bug in TFS/Azure DevOps and store locally."""
    wi = TfsWorkItem(
        title=title,
        work_item_type=work_item_type,
        state=state,
        description=description,
        repro_steps=repro_steps,
        test_run_ids=json.dumps(test_run_ids),
        area_path=area_path,
        iteration_path=iteration_path,
        tags=tags,
        project=project,
        priority=priority,
        assigned_to=assigned_to,
    )

    if settings.TFS_BASE_URL and settings.TFS_PAT:
        try:
            body = [
                {"op": "add", "path": "/fields/System.Title", "value": title},
                {"op": "add", "path": "/fields/System.Description", "value": description},
                {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.ReproSteps", "value": repro_steps},
                {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity},
                {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
                {"op": "add", "path": "/fields/System.Tags", "value": tags},
            ]
            if assigned_to:
                body.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assigned_to})
            if state and state != "New":
                body.append({"op": "add", "path": "/fields/System.State", "value": state})
            if area_path:
                body.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})
            if iteration_path:
                body.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})

            url = _api_url("wit/workitems/$Bug?api-version=7.1", project=project or None)
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=_headers(), json=body, ssl=False) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        wi.tfs_id = data.get("id")
                        wi.state = data.get("fields", {}).get("System.State", "New")
                        wi.last_synced_at = datetime.now(timezone.utc)
                        logger.info(f"Created TFS work item #{wi.tfs_id}")
                    else:
                        text = await resp.text()
                        logger.error(f"TFS create failed ({resp.status}): {text}")
                        wi.description += f"\n\n[TFS sync error: {resp.status}]"
        except Exception as e:
            logger.exception("TFS integration error")
            wi.description += f"\n\n[TFS sync error: {e}]"
    else:
        logger.info("TFS not configured – work item stored locally only.")

    db.add(wi)
    await db.commit()
    await db.refresh(wi)
    return wi


async def sync_work_item(db: AsyncSession, local_id: int) -> TfsWorkItem:
    """Re-sync a local work item with TFS to get latest state."""
    wi = await db.get(TfsWorkItem, local_id)
    if not wi or not wi.tfs_id:
        return wi

    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return wi

    try:
        url = _api_url(f"wit/workitems/{wi.tfs_id}?api-version=7.1", project=None)
        import base64
        token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
        headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    fields = data.get("fields", {})
                    wi.state = fields.get("System.State", wi.state)
                    wi.assigned_to = fields.get("System.AssignedTo", {}).get("displayName", "")
                    wi.last_synced_at = datetime.now(timezone.utc)
                    await db.commit()
    except Exception as e:
        logger.exception("TFS sync error")

    return wi


async def auto_create_bugs_for_batch(db: AsyncSession, batch_id: str) -> List[TfsWorkItem]:
    """Find all failed runs in a batch and create grouped TFS bugs."""
    runs_r = await db.execute(
        select(TestRun).where(TestRun.batch_id == batch_id, TestRun.status == "failed")
    )
    failed_runs = runs_r.scalars().all()
    if not failed_runs:
        return []

    # Group by mapping_rule_id
    groups = {}
    for run in failed_runs:
        tc = await db.get(TestRun.__class__, run.test_case_id)  # avoid lazy load
        key = run.test_case_id
        if key not in groups:
            groups[key] = []
        groups[key].append(run)

    items = []
    for test_case_id, runs in groups.items():
        run_ids = [r.id for r in runs]
        details = "\n".join(
            f"- Run #{r.id}: {r.actual_result} (mismatches={r.mismatch_count})"
            for r in runs
        )
        wi = await create_work_item(
            db,
            title=f"[AutoTest] Batch {batch_id} – Test #{test_case_id} failures",
            description=f"Automated test failures detected.\n\n{details}",
            repro_steps=f"1. Open DB Testing Tool\n2. Go to batch {batch_id}\n3. Review failed tests",
            test_run_ids=run_ids,
            tags="AutoTest,DBTestTool",
        )
        items.append(wi)

    return items


async def update_work_item(db: AsyncSession, local_id: int,
                            title: str = None, description: str = None,
                            state: str = None, assigned_to: str = None,
                            priority: int = None, severity: str = None,
                            area_path: str = None, iteration_path: str = None,
                            tags: str = None) -> TfsWorkItem:
    """Update a work item locally and in TFS."""
    wi = await db.get(TfsWorkItem, local_id)
    if not wi:
        raise ValueError(f"Work item {local_id} not found")

    # Update local fields
    if title is not None: wi.title = title
    if description is not None: wi.description = description
    if state is not None: wi.state = state
    if assigned_to is not None: wi.assigned_to = assigned_to
    if priority is not None: wi.priority = priority
    if area_path is not None: wi.area_path = area_path
    if iteration_path is not None: wi.iteration_path = iteration_path
    if tags is not None: wi.tags = tags

    # Push to TFS if connected
    if wi.tfs_id and settings.TFS_BASE_URL and settings.TFS_PAT:
        try:
            patches = []
            if title is not None:
                patches.append({"op": "replace", "path": "/fields/System.Title", "value": title})
            if description is not None:
                patches.append({"op": "replace", "path": "/fields/System.Description", "value": description})
            if state is not None:
                patches.append({"op": "replace", "path": "/fields/System.State", "value": state})
            if assigned_to is not None:
                patches.append({"op": "replace", "path": "/fields/System.AssignedTo", "value": assigned_to})
            if priority is not None:
                patches.append({"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority})
            if severity is not None:
                patches.append({"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity})
            if area_path is not None:
                patches.append({"op": "replace", "path": "/fields/System.AreaPath", "value": area_path})
            if iteration_path is not None:
                patches.append({"op": "replace", "path": "/fields/System.IterationPath", "value": iteration_path})
            if tags is not None:
                patches.append({"op": "replace", "path": "/fields/System.Tags", "value": tags})

            if patches:
                url = _api_url(f"wit/workitems/{wi.tfs_id}?api-version=7.1", project=wi.project or None)
                async with aiohttp.ClientSession() as session:
                    async with session.patch(url, headers=_headers(), json=patches, ssl=False) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            fields = data.get("fields", {})
                            wi.state = fields.get("System.State", wi.state)
                            wi.assigned_to = fields.get("System.AssignedTo", {}).get("displayName", wi.assigned_to)
                            wi.last_synced_at = datetime.now(timezone.utc)
                        else:
                            text = await resp.text()
                            logger.error(f"TFS update failed ({resp.status}): {text}")
        except Exception as e:
            logger.exception("TFS update error")

    await db.commit()
    await db.refresh(wi)
    return wi


async def run_wiql_query(project: str, wiql: str) -> list:
    """Execute a WIQL query against TFS and return work items."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return []

    try:
        import base64
        token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
        headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

        # Step 1: Run WIQL
        url = _api_url("wit/wiql?api-version=7.1", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers,
                                     json={"query": wiql}, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"WIQL query failed ({resp.status}): {text}")
                    return [{"error": f"TFS returned {resp.status}: {text}"}]
                data = await resp.json()

        wi_refs = data.get("workItems", [])
        if not wi_refs:
            return []

        # Step 2: Fetch details for found work items (batch of up to 200)
        ids = [str(wi["id"]) for wi in wi_refs[:200]]
        ids_param = ",".join(ids)
        fields = "System.Id,System.Title,System.State,System.AssignedTo,System.WorkItemType,Microsoft.VSTS.Common.Priority,System.Tags,System.AreaPath,System.IterationPath,System.CreatedDate"
        detail_url = _api_url(f"wit/workitems?ids={ids_param}&fields={fields}&api-version=7.1", project=project)

        async with aiohttp.ClientSession() as session:
            async with session.get(detail_url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return [{"error": f"TFS detail fetch failed ({resp.status}): {text}"}]
                detail_data = await resp.json()

        results = []
        for item in detail_data.get("value", []):
            f = item.get("fields", {})
            assigned = f.get("System.AssignedTo", {})
            if isinstance(assigned, dict):
                assigned = assigned.get("displayName", "")
            results.append({
                "id": item.get("id"),
                "title": f.get("System.Title", ""),
                "state": f.get("System.State", ""),
                "assigned_to": assigned,
                "work_item_type": f.get("System.WorkItemType", ""),
                "priority": f.get("Microsoft.VSTS.Common.Priority", ""),
                "tags": f.get("System.Tags", ""),
                "area_path": f.get("System.AreaPath", ""),
                "iteration_path": f.get("System.IterationPath", ""),
                "created_date": f.get("System.CreatedDate", ""),
                "web_url": build_tfs_work_item_web_url(project, item.get("id")),
            })
        return results

    except Exception as e:
        logger.exception("WIQL query error")
        return [{"error": str(e)}]


# ── Saved Queries ────────────────────────────────────────────────────

async def get_saved_queries(project: str, folder_path: str = "", depth: int = 2) -> list:
    """Fetch the saved query tree from TFS for a project.
    folder_path: e.g. 'Shared Queries' or 'My Queries' or '' for root.
    depth: 0=this node only, 1=children, 2=children+grandchildren.
    """
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return [{"error": "TFS not configured. Set TFS_BASE_URL and TFS_PAT in .env"}]

    try:
        import base64
        token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

        path_segment = f"/{folder_path}" if folder_path else ""
        url = _api_url(
            f"wit/queries{path_segment}?$depth={depth}&$expand=clauses&api-version=7.1",
            project=project,
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"TFS queries fetch failed ({resp.status}): {text}")
                    return [{"error": f"TFS returned {resp.status}: {text}"}]
                data = await resp.json()

        queries = _flatten_query_tree(data)
        await _enrich_saved_queries_metadata(project, queries)
        return queries

    except Exception as e:
        logger.exception("TFS saved queries error")
        return [{"error": str(e)}]


def _flatten_query_tree(node: dict, parent_path: str = "") -> list:
    """Recursively flatten the TFS query tree into a list."""
    results = []
    name = node.get("name", "")
    path = f"{parent_path}/{name}" if parent_path else name
    is_folder = node.get("isFolder", False)

    modified_by = node.get("lastModifiedBy") or {}
    if isinstance(modified_by, dict):
        modified_display = modified_by.get("displayName") or modified_by.get("uniqueName") or ""
    else:
        modified_display = str(modified_by or "")

    if not is_folder:
        results.append({
            "id": node.get("id", ""),
            "name": name,
            "path": path,
            "wiql": node.get("wiql", ""),
            "query_type": node.get("queryType", "flat"),
            "last_modified_by": modified_display,
            "last_modified_date": node.get("lastModifiedDate", ""),
        })

    for child in node.get("children", []):
        results.extend(_flatten_query_tree(child, path))

    return results


async def _enrich_saved_queries_metadata(project: str, queries: list) -> None:
    """Populate last-modified fields for saved queries via per-query details API."""
    if not queries:
        return

    timeout = aiohttp.ClientTimeout(total=15)
    semaphore = asyncio.Semaphore(8)

    async def enrich_one(session: aiohttp.ClientSession, query: dict) -> None:
        query_id = str(query.get("id") or "").strip()
        if not query_id:
            return
        url = _api_url(f"wit/queries/{query_id}?$expand=all&api-version=7.1", project=project)
        try:
            async with semaphore:
                async with session.get(url, headers=_headers(), ssl=False, timeout=timeout) as resp:
                    if resp.status != 200:
                        return
                    details = await resp.json()
        except Exception:
            return

        modified_by = details.get("lastModifiedBy") or {}
        if isinstance(modified_by, dict):
            display = modified_by.get("displayName") or modified_by.get("uniqueName") or ""
        else:
            display = str(modified_by or "")

        query["last_modified_by"] = display
        query["last_modified_date"] = details.get("lastModifiedDate", query.get("last_modified_date", ""))

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(enrich_one(session, q) for q in queries), return_exceptions=True)


async def run_saved_query(project: str, query_id: str) -> dict:
    """Execute a saved query by its ID and return the results."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        return {"error": "TFS not configured"}

    try:
        import base64
        token = base64.b64encode(f":{settings.TFS_PAT}".encode()).decode()
        headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

        # Step 1: Run the saved query
        url = _api_url(f"wit/wiql/{query_id}?api-version=7.1", project=project)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"error": f"TFS returned {resp.status}: {text}", "items": []}
                data = await resp.json()

        wi_refs = data.get("workItems", [])
        if not wi_refs:
            return {"count": 0, "items": [], "columns": data.get("columns", [])}

        # Step 2: Fetch details (batch of up to 200)
        ids = [str(wi["id"]) for wi in wi_refs[:200]]
        ids_param = ",".join(ids)
        fields = (
            "System.Id,System.Title,System.State,System.AssignedTo,"
            "System.WorkItemType,Microsoft.VSTS.Common.Priority,System.Tags,"
            "System.AreaPath,System.IterationPath,System.CreatedDate,System.ChangedDate,"
            "Microsoft.VSTS.Common.ClosedDate"
        )
        detail_url = _api_url(
            f"wit/workitems?ids={ids_param}&fields={fields}&api-version=7.1",
            project=project,
        )

        async with aiohttp.ClientSession() as session:
            async with session.get(detail_url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"error": f"TFS detail fetch failed ({resp.status}): {text}", "items": []}
                detail_data = await resp.json()

        results = []
        for item in detail_data.get("value", []):
            f = item.get("fields", {})
            assigned = f.get("System.AssignedTo", {})
            if isinstance(assigned, dict):
                assigned = assigned.get("displayName", "")
            results.append({
                "id": item.get("id"),
                "title": f.get("System.Title", ""),
                "state": f.get("System.State", ""),
                "assigned_to": assigned,
                "work_item_type": f.get("System.WorkItemType", ""),
                "priority": f.get("Microsoft.VSTS.Common.Priority", ""),
                "tags": f.get("System.Tags", ""),
                "area_path": f.get("System.AreaPath", ""),
                "iteration_path": f.get("System.IterationPath", ""),
                "created_date": f.get("System.CreatedDate", ""),
                "changed_date": f.get("System.ChangedDate", ""),
                "closed_date": f.get("Microsoft.VSTS.Common.ClosedDate", ""),
                "web_url": build_tfs_work_item_web_url(project, item.get("id")),
            })

        return {"count": len(wi_refs), "items": results, "total": len(wi_refs)}

    except Exception as e:
        logger.exception("TFS run saved query error")
        return {"error": str(e), "items": []}


# ── Pre-built CDSIntegration Queries ────────────────────────────────

def get_cds_preset_queries() -> list:
    """Return pre-built WIQL queries that match the CDSIntegration TFS query template."""
    return [
        {
            "name": "CCAL Active PBIs & Bugs",
            "description": "Product Backlog Items and Bugs under CDSCCAL, CCAL KTLO iterations, not closed",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags], "
                "[System.AreaPath], [System.IterationPath], [System.CreatedDate] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND ("
                "  [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY26' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY25' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\FY25' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\FY26' "
                ") "
                "AND [Microsoft.VSTS.Common.ClosedDate] >= @StartOfDay('-90d') "
                "ORDER BY [System.Id] DESC"
            ),
        },
        {
            "name": "CCAL KTLO FY26 Items",
            "description": "All work items in CCAL KTLO FY26 iteration",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY26' "
                "ORDER BY [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "CCAL KTLO FY25 Items",
            "description": "All work items in CCAL KTLO FY25 iteration",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY25' "
                "ORDER BY [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "CDS FY26 All Work Items",
            "description": "Product Backlog Items and Bugs in FY26 iteration",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.IterationPath] UNDER 'CDSIntegration\\FY26' "
                "ORDER BY [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "CDS FY25 All Work Items",
            "description": "Product Backlog Items and Bugs in FY25 iteration",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.IterationPath] UNDER 'CDSIntegration\\FY25' "
                "ORDER BY [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "Recently Closed (90 days)",
            "description": "PBIs and Bugs closed in the last 90 days across CDS areas",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags], "
                "[Microsoft.VSTS.Common.ClosedDate] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND [Microsoft.VSTS.Common.ClosedDate] >= @StartOfDay('-90d') "
                "ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC"
            ),
        },
        {
            "name": "Active Bugs Only",
            "description": "All active (non-closed) bugs under CDSCCAL area",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[Microsoft.VSTS.Common.Priority], [System.Tags] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] = 'Bug' "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND [System.State] <> 'Closed' "
                "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "My Assigned Items",
            "description": "All PBIs and Bugs assigned to current user",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags], "
                "[System.IterationPath] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AssignedTo] = @me "
                "AND [System.State] <> 'Closed' "
                "ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC"
            ),
        },
        {
            "name": "Unassigned PBIs",
            "description": "Product Backlog Items with no assignee",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], "
                "[Microsoft.VSTS.Common.Priority], [System.IterationPath] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] = 'Product Backlog Item' "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND [System.AssignedTo] = '' "
                "AND [System.State] <> 'Closed' "
                "ORDER BY [System.CreatedDate] DESC"
            ),
        },
        {
            "name": "All CDS Queries (Full Template)",
            "description": "Matches the full TFS query template: PBIs+Bugs, CDSCCAL area, all CCAL KTLO & FY iterations, closed >= 90 days",
            "wiql": (
                "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], "
                "[System.WorkItemType], [Microsoft.VSTS.Common.Priority], [System.Tags], "
                "[System.AreaPath], [System.IterationPath], [Microsoft.VSTS.Common.ClosedDate] "
                "FROM WorkItems "
                "WHERE [System.WorkItemType] IN ('Product Backlog Item', 'Bug') "
                "AND [System.AreaPath] UNDER 'CDSIntegration\\CDSCCAL' "
                "AND ("
                "  [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY26' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\CCAL Performance\\CCAL KTLO FY25' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\FY25' "
                "  OR [System.IterationPath] UNDER 'CDSIntegration\\FY26' "
                ") "
                "AND [Microsoft.VSTS.Common.ClosedDate] >= @StartOfDay('-90d') "
                "ORDER BY [System.Id] DESC"
            ),
        },
    ]


def _html_to_text(html_content: str) -> str:
    """Strip HTML tags and normalise whitespace to plain text."""
    if not html_content:
        return ""
    text = re.sub(r'<[^>]+>', ' ', str(html_content))
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _parse_tfs_test_steps(steps_xml: str) -> str:
    """Extracts raw SQL and expected results from TFS Test Case XML steps."""
    if not steps_xml:
        return ""
    blocks = re.findall(r"<parameterizedString[^>]*>([\s\S]*?)</parameterizedString>", steps_xml, flags=re.IGNORECASE)
    cleaned = [_html_to_text(b) for b in blocks]
    out = []
    step_num = 1
    for i in range(0, len(cleaned), 2):
        action = cleaned[i] if i < len(cleaned) else ""
        expected = cleaned[i+1] if i+1 < len(cleaned) else ""
        out.append(f"Step {step_num}:\nAction: {action}\nExpected Result: {expected}")
        step_num += 1
    return "\n\n".join(out)


async def fetch_work_item_context(item_id: int, project: str = "") -> dict:
    """Fetch TFS/Azure DevOps work item details including description, acceptance criteria,
    attachment metadata, and hyperlinks for use in AI-powered test generation."""
    if not settings.TFS_BASE_URL or not settings.TFS_PAT:
        raise ValueError("TFS not configured (TFS_BASE_URL / TFS_PAT missing)")

    proj = project or (_get_projects()[0] if _get_projects() else "")
    url = _api_url(f"wit/workitems/{item_id}?$expand=all&api-version=7.0", project=proj)
    headers = {
        "Authorization": _headers()["Authorization"],
        "Accept": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, ssl=False) as resp:
            if resp.status == 404:
                raise ValueError(f"Work item {item_id} not found in project '{proj}'")
            if not resp.ok:
                body = await resp.text()
                raise ValueError(f"TFS returned HTTP {resp.status}: {body[:200]}")
            data = await resp.json()

    fields = data.get("fields", {})
    assigned_raw = fields.get("System.AssignedTo", "")
    assigned_to = (
        assigned_raw.get("displayName", "") if isinstance(assigned_raw, dict)
        else str(assigned_raw)
    )

    result: dict = {
        "id": item_id,
        "work_item_type": fields.get("System.WorkItemType", ""),
        "title": fields.get("System.Title", ""),
        "state": fields.get("System.State", ""),
        "description_html": fields.get("System.Description", ""),
        "description_text": _html_to_text(fields.get("System.Description", "")),
        "acceptance_criteria": _html_to_text(
            fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
        ),
        "test_steps_xml": fields.get("Microsoft.VSTS.TCM.Steps", ""),
        "test_steps_text": _parse_tfs_test_steps(fields.get("Microsoft.VSTS.TCM.Steps", "")),
        "tags": fields.get("System.Tags", ""),
        "assigned_to": assigned_to,
        "area_path": fields.get("System.AreaPath", ""),
        "iteration_path": fields.get("System.IterationPath", ""),
        "priority": fields.get("Microsoft.VSTS.Common.Priority", None),
        "attachments": [],
        "hyperlinks": [],
    }

    for rel in data.get("relations", []):
        rel_type = rel.get("rel", "")
        attrs = rel.get("attributes", {})
        rel_url = rel.get("url", "")
        if rel_type == "AttachedFile":
            result["attachments"].append({
                "name": attrs.get("name", "attachment"),
                "url": rel_url,
                "size": attrs.get("resourceSize", 0),
                "content_type": attrs.get("resourceType", ""),
            })
        elif rel_type == "Hyperlink":
            result["hyperlinks"].append({
                "url": rel_url,
                "comment": attrs.get("comment", ""),
            })

    # Also extract embedded URLs from description HTML
    desc_html = fields.get("System.Description", "") or ""
    for m in re.finditer(r'href="([^"]+)"', desc_html):
        link = m.group(1)
        if link not in [h["url"] for h in result["hyperlinks"]]:
            result["hyperlinks"].append({"url": link, "comment": "(embedded in description)"})

    return result


async def download_tfs_attachment(attachment_url: str) -> bytes:
    """Download a TFS attachment by its API URL. Returns raw bytes."""
    if not settings.TFS_PAT:
        raise ValueError("TFS not configured")
    headers = {
        "Authorization": _headers()["Authorization"],
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment_url, headers=headers, ssl=False) as resp:
            if not resp.ok:
                raise ValueError(f"Failed to download attachment: HTTP {resp.status}")
            return await resp.read()


async def download_tfs_attachment_text(attachment_url: str, filename: str = "") -> str:
    """Download a TFS attachment and extract text content.
    Supports: .txt, .sql, .csv, .json, .xml, .md, .xlsx, .xls, .msg"""
    raw = await download_tfs_attachment(attachment_url)
    lower = filename.lower()
    try:
        if lower.endswith((".txt", ".sql", ".csv", ".json", ".xml", ".md", ".log")):
            return raw.decode("utf-8", errors="replace")
        if lower.endswith((".xlsx", ".xls")):
            import io, openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            lines = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                lines.append(f"\n=== Sheet: {sheet_name} ===")
                for row in ws.iter_rows(values_only=True):
                    vals = [str(c) if c is not None else "" for c in row]
                    lines.append(" | ".join(vals))
            wb.close()
            return "\n".join(lines)
        if lower.endswith(".vsdx"):
            import zipfile
            import xml.etree.ElementTree as ET
            chunks = []
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for name in z.namelist():
                    if name.endswith(".xml") and "visio/pages/page" in name:
                        root = ET.fromstring(z.read(name))
                        for elem in root.iter():
                            if elem.tag.endswith('Text'):
                                txt = "".join(elem.itertext()).strip()
                                if txt: chunks.append(txt)
            return "\n".join(chunks)
        if lower.endswith(".pdf"):
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw))
                return "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
            except ImportError:
                return "[pypdf not installed - unable to extract PDF text]"
        if lower.endswith(".docx"):
            try:
                from docx import Document
                doc = Document(io.BytesIO(raw))
                return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
            except ImportError:
                return "[python-docx not installed - unable to extract DOCX text]"
        if lower.endswith(".pptx"):
            try:
                from pptx import Presentation
                prs = Presentation(io.BytesIO(raw))
                chunks = []
                for slide in prs.slides:
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            chunks.append(shape.text.strip())
                return "\n".join(chunks)
            except ImportError:
                return "[python-pptx not installed - unable to extract PPTX text]"
        if lower.endswith(".msg"):
            # Best-effort: extract readable text from .msg binary
            text = raw.decode("utf-8", errors="replace")
            # Remove binary noise, keep printable chars
            cleaned = re.sub(r'[^\x20-\x7E\n\r\t]', ' ', text)
            cleaned = re.sub(r' {3,}', '  ', cleaned)
            return cleaned[:8000]
        # Fallback: try as text
        return raw.decode("utf-8", errors="replace")[:8000]
    except Exception as e:
        return f"[Could not extract text from {filename}: {e}]"


async def fetch_work_item_full_context(item_id: int, project: str = "") -> dict:
    """Enhanced version: fetches work item + downloads all attachment text content."""
    context = await fetch_work_item_context(item_id, project)

    # Download each attachment's text
    for att in context.get("attachments", []):
        try:
            text = await download_tfs_attachment_text(att["url"], att.get("name", ""))
            att["content_text"] = text
        except Exception as e:
            att["content_text"] = f"[Download failed: {e}]"

    # Attempt to fetch content from linked web pages (Confluence/SharePoint/Docs)
    for link in context.get("hyperlinks", []):
        url = link["url"]
        if url.startswith("http"):
            headers_for_link = {}
            # Re-use TFS PAT for links inside the same TFS/Azure DevOps host
            if settings.TFS_BASE_URL and settings.TFS_PAT:
                from urllib.parse import urlparse
                tfs_host = urlparse(settings.TFS_BASE_URL).netloc
                link_host = urlparse(url).netloc
                if tfs_host and tfs_host.lower() in link_host.lower():
                    headers_for_link = _headers()
            try:
                timeout_cfg = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
                    async with session.get(url, headers=headers_for_link, ssl=False) as resp:
                        if resp.status == 200:
                            # No cap — store full content for AI analysis
                            link["content_text"] = await resp.text()
                        else:
                            link["content_text"] = f"[HTTP {resp.status} fetching URL]"
            except Exception as e:
                link["content_text"] = f"[Failed to scrape link: {e}]"

    # Save fully hydrated requirement as training material
    try:
        from app.config import DATA_DIR
        import time
        train_dir = DATA_DIR / "training_corpus" / "tfs_scrapes"
        train_dir.mkdir(parents=True, exist_ok=True)
        dump_file = train_dir / f"tfs_item_{item_id}_{int(time.time())}.json"
        dump_file.write_text(json.dumps(context, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save TFS training corpus: {e}")

    return context
