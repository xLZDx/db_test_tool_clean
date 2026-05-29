"""Regression Lab indexing, validation, grouping, and AI search workflows."""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.regression_lab import RegressionCatalogItem, RegressionLabConfig
from app.models.schema_object import ColumnProfile, LineageEdge, SchemaObject
from app.models.test_case import TestCase, TestCaseFolder, TestFolder
from app.services.ai_service import (
    _build_chat_call_args,
    _chat_completion_with_fallback,
    _get_client_and_model,
)
from app.services.sql_pattern_validation import validate_sql_pattern
from app.services.tfs_service import (
    build_tfs_work_item_web_url,
    download_tfs_attachment_text,
    fetch_work_item_context,
)
from app.services.tfs_test_management_service import (
    cache_test_plan,
    cache_test_point,
    cache_test_suite,
    get_test_plans,
    get_test_points,
    get_test_suites,
    lookup_work_item,
)

logger = logging.getLogger(__name__)

REGRESSION_MAIN_GROUPS = [
    "Transactions",
    "Positions",
    "Balances",
    "AML",
    "Performance/CF Generation",
    "Other",
]

GROUP_RULES: Dict[str, Tuple[str, ...]] = {
    "Transactions": ("txn", "transaction", "transfer", "movement", "activity", "money xfer", "posting", "postings", "trade"),
    "Positions": ("position", "tax lot", "taxlot", "lot", "holding", "security position"),
    "Balances": ("balance", "sub bal", "sub_balance", "ending bal", "beginning bal", "rtc balance"),
    "AML": ("aml", "kyc", "sanction", "watchlist", "compliance", "alert"),
    "Performance/CF Generation": ("performance", "cash flow", "cf generation", "irr", "return", "benchmark"),
}

GROUP_PRIORITY: Tuple[str, ...] = (
    "Balances",
    "Positions",
    "Transactions",
    "AML",
    "Performance/CF Generation",
    "Other",
)

CONTEXT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("RTC Balances", ("rtc", "balance", "sub bal", "sub_balance")),
    ("Tax Lots", ("tax lot", "taxlot", "lot")),
    ("Positions", ("position", "holding")),
    ("Transfers", ("transfer", "xfer", "movement")),
    ("Cash Flows", ("cash flow", "cf generation", "cashflow")),
    ("AML Controls", ("aml", "kyc", "sanction", "watchlist")),
)

DEFAULT_PROJECT_FILTERS: Dict[str, Dict[str, List[str]]] = {
    "CDSINTEGRATION": {
        "area_paths": ["CDSIntegration\\CDSCCAL"],
        "iteration_paths": ["CDSIntegration\\CCAL"],
    }
}

DEFAULT_EXCLUSION_KEYWORDS = ["archive", "arch", "junk"]


def _norm_text(value: str | None) -> str:
    return (value or "").strip()


def _norm_upper(value: str | None) -> str:
    return _norm_text(value).upper().replace('"', "")


def _json_dumps(value) -> str:
    return json.dumps(value or [], ensure_ascii=True)


def _json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = _norm_text(str(value))
    if not text:
        return None
    # Support ISO and mm/dd/yyyy-like forms.
    for candidate in [text, text.replace("Z", "+00:00")]:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            parsed = datetime.strptime(text[:10], fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _dt_to_iso(value: Optional[datetime]) -> str:
    if not value:
        return ""
    dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _build_plan_web_url(project: str, plan_id: Optional[int]) -> str:
    if not project or not plan_id:
        return ""
    base = (settings.TFS_BASE_URL or "").rstrip("/")
    collection = (settings.TFS_COLLECTION or "").strip("/")
    if not base or not collection:
        return ""
    return f"{base}/{collection}/{project}/_testManagement?r=2&_a=overview&planId={int(plan_id)}"


def _build_suite_web_url(project: str, plan_id: Optional[int], suite_id: Optional[int]) -> str:
    if not project or not plan_id or not suite_id:
        return ""
    base = (settings.TFS_BASE_URL or "").rstrip("/")
    collection = (settings.TFS_COLLECTION or "").strip("/")
    if not base or not collection:
        return ""
    return f"{base}/{collection}/{project}/_testManagement?r=2&_a=runCharts&planId={int(plan_id)}&suiteId={int(suite_id)}"


def _default_filters_for_project(project: str) -> Dict[str, List[str]]:
    key = _norm_upper(project)
    return {
        "area_paths": list((DEFAULT_PROJECT_FILTERS.get(key) or {}).get("area_paths") or []),
        "iteration_paths": list((DEFAULT_PROJECT_FILTERS.get(key) or {}).get("iteration_paths") or []),
    }


async def get_or_create_regression_config(db: AsyncSession, project: str) -> RegressionLabConfig:
    existing = await db.execute(select(RegressionLabConfig).where(RegressionLabConfig.project == project))
    cfg = existing.scalar_one_or_none()
    if cfg:
        return cfg
    defaults = _default_filters_for_project(project)
    cfg = RegressionLabConfig(
        project=project,
        default_area_paths_json=_json_dumps(defaults.get("area_paths") or []),
        default_iteration_paths_json=_json_dumps(defaults.get("iteration_paths") or []),
        exclusion_keywords_json=_json_dumps(DEFAULT_EXCLUSION_KEYWORDS),
        excluded_item_ids_json=_json_dumps([]),
        excluded_plan_ids_json=_json_dumps([]),
        excluded_suite_ids_json=_json_dumps([]),
        min_changed_date=None,
        include_archived=False,
    )
    db.add(cfg)
    await db.commit()
    await db.refresh(cfg)
    return cfg


def _effective_filters_from_config(
    cfg: RegressionLabConfig,
    *,
    area_paths: Sequence[str],
    iteration_paths: Sequence[str],
    min_changed_date: Optional[datetime],
) -> Dict[str, object]:
    defaults = _default_filters_for_project(cfg.project)
    stored_area = _json_loads(cfg.default_area_paths_json, [])
    stored_iteration = _json_loads(cfg.default_iteration_paths_json, [])
    effective_area = list(area_paths or stored_area or (defaults.get("area_paths") or []))
    effective_iteration = list(iteration_paths or stored_iteration or (defaults.get("iteration_paths") or []))
    effective_min_date = min_changed_date or cfg.min_changed_date
    exclusion_keywords = [
        _norm_text(keyword).lower()
        for keyword in (_json_loads(cfg.exclusion_keywords_json, []) or DEFAULT_EXCLUSION_KEYWORDS)
        if _norm_text(keyword)
    ]
    return {
        "area_paths": effective_area,
        "iteration_paths": effective_iteration,
        "min_changed_date": effective_min_date,
        "exclusion_keywords": exclusion_keywords,
        "excluded_item_ids": set(int(x) for x in _json_loads(cfg.excluded_item_ids_json, []) if str(x).isdigit()),
        "excluded_plan_ids": set(int(x) for x in _json_loads(cfg.excluded_plan_ids_json, []) if str(x).isdigit()),
        "excluded_suite_ids": set(int(x) for x in _json_loads(cfg.excluded_suite_ids_json, []) if str(x).isdigit()),
        "include_archived": bool(cfg.include_archived),
    }


def _is_archive_like(text: str, exclusion_keywords: Sequence[str]) -> bool:
    value = _norm_text(text).lower()
    return any(keyword in value for keyword in exclusion_keywords if keyword)


def _is_archive_match_for_item(item: RegressionCatalogItem, exclusion_keywords: Sequence[str]) -> bool:
    return any(
        _is_archive_like(value, exclusion_keywords)
        for value in [
            item.plan_name,
            item.suite_name,
            item.suite_path,
            item.title,
            item.iteration_path,
            item.area_path,
            item.tags,
        ]
    )


def _strip_markup(text: str | None) -> str:
    raw = str(text or "")
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = raw.replace("\r", "\n")
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    raw = re.sub(r"[ \t]{2,}", " ", raw)
    return raw.strip()


def _extract_expected_results_text(steps_xml: str | None) -> str:
    blocks = re.findall(
        r"<parameterizedString[^>]*>([\s\S]*?)</parameterizedString>",
        steps_xml or "",
        flags=re.IGNORECASE,
    )
    cleaned = [_strip_markup(block) for block in blocks]
    expected_lines: List[str] = []
    for index in range(1, len(cleaned), 2):
        value = cleaned[index]
        if value:
            expected_lines.append(value)
    return "\n\n".join(expected_lines).strip()


def _build_regression_local_test_description(item: RegressionCatalogItem, sql: str) -> str:
    parts = [
        f"<div><strong>Imported from Regression Lab</strong></div>",
        f"<div><strong>Project:</strong> {item.project}</div>",
        f"<div><strong>Plan:</strong> {item.plan_name or item.plan_id or '-'}</div>",
        f"<div><strong>Suite:</strong> {item.suite_path or item.suite_name or '-'}</div>",
        f"<div><strong>TFS Test Point:</strong> {item.test_point_id or '-'}</div>",
        f"<div><strong>TFS Test Case:</strong> {item.test_case_id}</div>",
    ]
    if item.validation_summary:
        parts.append(f"<div><strong>Regression Validation:</strong> {item.validation_summary}</div>")
    if item.expected_results_text:
        parts.append(f"<div><strong>Expected Result:</strong><pre>{item.expected_results_text}</pre></div>")
    parts.append(f"<div><strong>SQL</strong><pre>{sql}</pre></div>")
    return "".join(parts)


def _extract_sql_candidates(text: str | None) -> List[str]:
    raw = _strip_markup(text)
    if not raw:
        return []
    pattern = re.compile(
        r"(?is)(?:^|\n|\s)(select|with)\b[\s\S]*?(?:;|$)"
    )
    found: List[str] = []
    seen = set()
    for match in pattern.finditer(raw):
        sql = match.group(0).strip().lstrip(":").strip()
        sql = re.sub(r";\s*$", "", sql)
        if not sql:
            continue

        sql_u = sql.upper()
        if sql_u.startswith("WITH"):
            # Guard against prose like "with no SQL" being treated as a CTE.
            if "SELECT" not in sql_u:
                continue
        elif sql_u.startswith("SELECT"):
            # Keep SELECT snippets only when they look query-like.
            if " FROM " not in f" {sql_u} " and not re.fullmatch(r"SELECT\s+\d+(?:\.\d+)?", sql_u):
                continue

        key = re.sub(r"\s+", " ", sql).strip().lower()
        if key not in seen:
            seen.add(key)
            found.append(sql)
    return found


def _matches_prefix(value: str | None, prefixes: Sequence[str]) -> bool:
    if not prefixes:
        return True
    text = _norm_text(value).lower()
    return any(text.startswith(prefix.lower()) for prefix in prefixes if prefix)


def _matches_tags(tags_text: str | None, tags_filter: Sequence[str]) -> bool:
    if not tags_filter:
        return True
    text = _norm_text(tags_text).lower()
    return all(tag.lower() in text for tag in tags_filter if tag)


def _best_group_for_text(text: str) -> str:
    combined = _norm_text(text).lower()
    if not combined:
        return "Other"

    scores: Dict[str, int] = {}
    for group, keywords in GROUP_RULES.items():
        score = 0
        for keyword in keywords:
            if keyword and keyword in combined:
                score += 4 if " " in keyword else 3
        scores[group] = score

    best_score = max(scores.values(), default=0)
    if best_score <= 0:
        return "Other"

    best_groups = {group for group, score in scores.items() if score == best_score}
    for group in GROUP_PRIORITY:
        if group in best_groups:
            return group
    return "Other"


def _compute_domain_group(*parts: str) -> Tuple[str, str]:
    combined = " ".join(_norm_text(part).lower() for part in parts if part).strip()
    group = _best_group_for_text(combined)
    if group != "Other":
        for context_name, context_keywords in CONTEXT_RULES:
            if any(keyword in combined for keyword in context_keywords):
                return group, context_name
        return group, group
    for context_name, context_keywords in CONTEXT_RULES:
        if any(keyword in combined for keyword in context_keywords):
            return "Other", context_name
    return "Other", "Unclassified"


def _parse_work_item_id_from_url(url: str | None) -> Optional[int]:
    text = _norm_text(url)
    match = re.search(r"/workItems/(\d+)|/workitems/edit/(\d+)|/wit/workItems/(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    for group in match.groups():
        if group:
            return int(group)
    return None


async def _resolve_related_work_items(project: str, related_items: Sequence[dict]) -> Tuple[List[int], List[str]]:
    resolved_ids: List[int] = []
    resolved_titles: List[str] = []
    for item in related_items or []:
        work_item_id = item.get("id")
        if not work_item_id:
            continue
        resolved_ids.append(int(work_item_id))
        try:
            details = await lookup_work_item(int(work_item_id))
        except Exception:
            details = {}
        wi_type = _norm_text(details.get("type"))
        title = _norm_text(details.get("title"))
        if wi_type in {"Product Backlog Item", "Requirement", "User Story", "Bug", "Task"}:
            resolved_titles.append(f"{wi_type} {work_item_id}: {title}".strip())
        elif title:
            resolved_titles.append(f"{work_item_id}: {title}")
    return resolved_ids, resolved_titles


async def _ensure_folder(db: AsyncSession, folder_name: str) -> Optional[TestFolder]:
    name = _norm_text(folder_name)
    if not name:
        return None
    existing = await db.execute(select(TestFolder).where(TestFolder.name == name))
    folder = existing.scalar_one_or_none()
    if folder:
        return folder
    folder = TestFolder(name=name)
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


def _suite_path_map(suites: Sequence[dict]) -> Dict[int, str]:
    by_id = {int(s.get("id") or 0): s for s in suites if s.get("id")}
    cache: Dict[int, str] = {}

    def _resolve(suite_id: int) -> str:
        if suite_id in cache:
            return cache[suite_id]
        suite = by_id.get(suite_id) or {}
        name = _norm_text(suite.get("name")) or f"Suite {suite_id}"
        parent_id = (suite.get("parent") or {}).get("id")
        if parent_id and int(parent_id) in by_id and int(parent_id) != suite_id:
            path = f"{_resolve(int(parent_id))} / {name}"
        else:
            path = name
        cache[suite_id] = path
        return path

    for suite_id in by_id:
        _resolve(suite_id)
    return cache


async def sync_regression_catalog(
    db: AsyncSession,
    *,
    project: str,
    area_paths: Sequence[str],
    iteration_paths: Sequence[str],
    tags: Sequence[str],
    suite_name_contains: str = "",
    min_changed_date: Optional[datetime] = None,
    max_cases: int = 400,
) -> dict:
    cfg = await get_or_create_regression_config(db, project)
    effective = _effective_filters_from_config(
        cfg,
        area_paths=area_paths,
        iteration_paths=iteration_paths,
        min_changed_date=min_changed_date,
    )
    area_paths = effective["area_paths"]
    iteration_paths = effective["iteration_paths"]
    min_changed_date = effective["min_changed_date"]
    exclusion_keywords = effective["exclusion_keywords"]
    excluded_item_ids = effective["excluded_item_ids"]
    excluded_plan_ids = effective["excluded_plan_ids"]
    excluded_suite_ids = effective["excluded_suite_ids"]
    include_archived = bool(effective["include_archived"])

    plans = await get_test_plans(project)
    if not plans:
        return {
            "project": project,
            "indexed": 0,
            "skipped": 0,
            "plans_scanned": 0,
            "suites_scanned": 0,
            "points_scanned": 0,
        }

    now = datetime.now(timezone.utc)
    suite_name_filter = _norm_text(suite_name_contains).lower()
    indexed = 0
    skipped = 0
    plans_scanned = 0
    suites_scanned = 0
    points_scanned = 0

    for plan_data in plans:
        plan_id = int(plan_data.get("id") or 0)
        plan_name = _norm_text(plan_data.get("name")) or f"Plan {plan_id}"
        plan_area = _norm_text((plan_data.get("area") or {}).get("path") or (plan_data.get("area") or {}).get("name"))
        plan_iteration_raw = plan_data.get("iteration")
        if isinstance(plan_iteration_raw, dict):
            plan_iteration = _norm_text(plan_iteration_raw.get("path") or plan_iteration_raw.get("name"))
        else:
            plan_iteration = _norm_text(plan_iteration_raw)
        if plan_id in excluded_plan_ids:
            skipped += 1
            continue
        if not include_archived and (_is_archive_like(plan_name, exclusion_keywords) or _is_archive_like(plan_iteration, exclusion_keywords) or _is_archive_like(plan_area, exclusion_keywords)):
            skipped += 1
            continue
        if area_paths and plan_area and not _matches_prefix(plan_area, area_paths):
            continue
        if iteration_paths and plan_iteration and not _matches_prefix(plan_iteration, iteration_paths):
            continue

        plans_scanned += 1
        await cache_test_plan(db, project, plan_data)
        suites = await get_test_suites(project, plan_id)
        suites_scanned += len(suites)
        suite_paths = _suite_path_map(suites)

        for suite_data in suites:
            suite_name = _norm_text(suite_data.get("name"))
            suite_id = int(suite_data.get("id") or 0)
            if suite_id in excluded_suite_ids:
                skipped += 1
                continue
            if not include_archived and (_is_archive_like(suite_name, exclusion_keywords) or _is_archive_like(suite_paths.get(suite_id, suite_name), exclusion_keywords)):
                skipped += 1
                continue
            if suite_name_filter and suite_name_filter not in suite_name.lower():
                continue
            await cache_test_suite(db, project, plan_id, suite_data)
            points = await get_test_points(project, plan_id, suite_id)
            points_scanned += len(points)

            for point_data in points:
                if indexed >= max_cases:
                    break
                await cache_test_point(db, project, plan_id, suite_id, point_data)
                test_point = point_data.get("testPoint") or {}
                test_case = point_data.get("testCase") or {}
                test_point_id = int(test_point.get("id") or point_data.get("id") or 0)
                test_case_id = int(test_case.get("id") or 0)
                if not test_case_id:
                    skipped += 1
                    continue
                if test_case_id in excluded_item_ids:
                    skipped += 1
                    continue
                tc_title_hint = _norm_text(test_case.get("name"))
                if not include_archived and _is_archive_like(tc_title_hint, exclusion_keywords):
                    skipped += 1
                    continue

                try:
                    context = await fetch_work_item_context(test_case_id, project=project)
                except Exception as exc:
                    logger.warning("Regression sync failed to fetch TFS context for test case %s: %s", test_case_id, exc)
                    skipped += 1
                    continue

                if not _matches_prefix(context.get("area_path"), area_paths):
                    skipped += 1
                    continue
                if not _matches_prefix(context.get("iteration_path"), iteration_paths):
                    skipped += 1
                    continue
                if not _matches_tags(context.get("tags"), tags):
                    skipped += 1
                    continue

                changed_dt = _parse_dt(context.get("changed_date"))
                if min_changed_date and changed_dt and changed_dt < min_changed_date:
                    skipped += 1
                    continue
                title = _norm_text(context.get("title")) or tc_title_hint
                if not include_archived and any(
                    _is_archive_like(value, exclusion_keywords)
                    for value in [title, context.get("iteration_path"), context.get("area_path"), context.get("tags")]
                ):
                    skipped += 1
                    continue

                attachment_names: List[str] = []
                attachment_chunks: List[str] = []
                for attachment in context.get("attachments", []):
                    attachment_name = _norm_text(attachment.get("name")) or "attachment"
                    attachment_names.append(attachment_name)
                    try:
                        attachment_text = await download_tfs_attachment_text(attachment.get("url"), attachment_name)
                    except Exception as exc:
                        attachment_text = f"[Attachment download failed: {exc}]"
                    if attachment_text:
                        attachment_chunks.append(attachment_text[:12000])

                related_ids, related_titles = await _resolve_related_work_items(project, context.get("related_work_items", []))
                combined_text = "\n\n".join(
                    part for part in [
                        context.get("title"),
                        context.get("description_text"),
                        context.get("acceptance_criteria"),
                        context.get("test_steps_text"),
                        "\n\n".join(attachment_chunks),
                    ]
                    if _norm_text(part)
                )
                sql_candidates = _extract_sql_candidates(combined_text)
                expected_results_text = _extract_expected_results_text(context.get("test_steps_xml"))
                suite_path = suite_paths.get(suite_id, suite_name)
                domain_group, domain_context = _compute_domain_group(
                    suite_path,
                    context.get("title"),
                    context.get("tags"),
                    context.get("description_text"),
                    context.get("test_steps_text"),
                )

                existing_q = await db.execute(
                    select(RegressionCatalogItem).where(
                        RegressionCatalogItem.project == project,
                        RegressionCatalogItem.test_point_id == test_point_id,
                    )
                )
                item = existing_q.scalar_one_or_none()
                if not item:
                    item = RegressionCatalogItem(
                        project=project,
                        plan_id=plan_id,
                        plan_name=plan_name,
                        suite_id=suite_id,
                        suite_name=suite_name,
                        suite_path=suite_path,
                        parent_suite_id=(suite_data.get("parent") or {}).get("id"),
                        test_point_id=test_point_id,
                        test_case_id=test_case_id,
                        title=_norm_text(context.get("title")) or _norm_text(test_case.get("name")) or f"TFS Test Case {test_case_id}",
                    )
                    db.add(item)

                item.plan_id = plan_id
                item.plan_name = plan_name
                item.suite_id = suite_id
                item.suite_name = suite_name
                item.suite_path = suite_path
                item.parent_suite_id = (suite_data.get("parent") or {}).get("id")
                item.test_case_id = test_case_id
                item.title = _norm_text(context.get("title")) or item.title
                item.work_item_type = _norm_text(context.get("work_item_type"))
                item.state = _norm_text(context.get("state")) or _norm_text(test_point.get("state"))
                item.priority = context.get("priority")
                item.owner = _norm_text(context.get("assigned_to"))
                item.automation_status = _norm_text((test_point.get("automationStatus") or {}).get("name") or point_data.get("automationStatus"))
                item.area_path = _norm_text(context.get("area_path"))
                item.iteration_path = _norm_text(context.get("iteration_path"))
                item.tags = _norm_text(context.get("tags"))
                item.description_text = _norm_text(context.get("description_text"))
                item.steps_text = _norm_text(context.get("test_steps_text"))
                item.expected_results_text = expected_results_text
                item.attachment_names_json = _json_dumps(attachment_names)
                item.attachment_text = "\n\n".join(attachment_chunks)[:30000]
                item.hyperlink_urls_json = _json_dumps([h.get("url") for h in context.get("hyperlinks", []) if h.get("url")])
                item.linked_requirement_ids_json = _json_dumps(related_ids)
                item.linked_requirement_titles_json = _json_dumps(related_titles)
                item.sql_candidates_json = _json_dumps(sql_candidates)
                item.test_case_web_url = build_tfs_work_item_web_url(project, test_case_id)
                item.test_plan_web_url = _build_plan_web_url(project, plan_id)
                item.test_suite_web_url = _build_suite_web_url(project, plan_id, suite_id)
                item.created_date = _parse_dt(context.get("created_date"))
                item.changed_date = changed_dt
                item.domain_group = domain_group
                item.domain_context = domain_context
                item.last_synced_at = now
                if not item.indexed_at:
                    item.indexed_at = now
                indexed += 1
            if indexed >= max_cases:
                break
        if indexed >= max_cases:
            break

    await db.commit()
    return {
        "project": project,
        "indexed": indexed,
        "skipped": skipped,
        "plans_scanned": plans_scanned,
        "suites_scanned": suites_scanned,
        "points_scanned": points_scanned,
        "area_paths": area_paths,
        "iteration_paths": iteration_paths,
        "min_changed_date": _dt_to_iso(min_changed_date),
        "max_cases": max_cases,
    }


def _extract_table_aliases(sql: str) -> Tuple[Dict[str, str], List[str]]:
    alias_map: Dict[str, str] = {}
    tables: List[str] = []
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([A-Z0-9_\."]+)(?:\s+(?:AS\s+)?([A-Z][A-Z0-9_]*))?',
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(sql or ""):
        table_token = _norm_upper(match.group(1))
        alias = _norm_upper(match.group(2))
        if table_token:
            tables.append(table_token)
            if alias and alias not in {"ON", "WHERE", "LEFT", "RIGHT", "INNER", "FULL", "GROUP", "ORDER"}:
                alias_map[alias] = table_token
    return alias_map, tables


def _extract_column_references(sql: str, alias_map: Dict[str, str]) -> Dict[str, List[str]]:
    columns_by_table: Dict[str, List[str]] = defaultdict(list)
    for alias, column in re.findall(r"\b([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)\b", (sql or "").upper()):
        table_token = alias_map.get(alias)
        if table_token and column not in columns_by_table[table_token]:
            columns_by_table[table_token].append(column)
    return columns_by_table


def _split_table_token(token: str) -> Tuple[str, str]:
    cleaned = _norm_upper(token)
    parts = [part for part in cleaned.split('.') if part]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", parts[-1] if parts else ""


async def validate_regression_catalog(
    db: AsyncSession,
    *,
    datasource_id: int,
    project: str,
    item_ids: Sequence[int] | None = None,
) -> dict:
    query = select(RegressionCatalogItem).where(RegressionCatalogItem.project == project)
    if item_ids:
        query = query.where(RegressionCatalogItem.id.in_([int(x) for x in item_ids]))
    result = await db.execute(query)
    items = result.scalars().all()
    if not items:
        return {"validated": 0, "project": project, "datasource_id": datasource_id}

    objects_r = await db.execute(select(SchemaObject).where(SchemaObject.datasource_id == datasource_id))
    objects = objects_r.scalars().all()
    if not objects:
        raise ValueError("No PDM/schema catalog found for the selected datasource. Run schema analysis first.")

    object_ids = [obj.id for obj in objects]
    columns_r = await db.execute(select(ColumnProfile).where(ColumnProfile.schema_object_id.in_(object_ids)))
    columns = columns_r.scalars().all()

    object_map: Dict[str, SchemaObject] = {}
    columns_by_object_id: Dict[int, set] = defaultdict(set)
    for obj in objects:
        key = f"{_norm_upper(obj.schema_name)}.{_norm_upper(obj.object_name)}" if obj.schema_name else _norm_upper(obj.object_name)
        object_map[key] = obj
        object_map[_norm_upper(obj.object_name)] = obj
    for column in columns:
        columns_by_object_id[column.schema_object_id].add(_norm_upper(column.column_name))

    lineage_r = await db.execute(select(LineageEdge).where((LineageEdge.source_datasource_id == datasource_id) | (LineageEdge.target_datasource_id == datasource_id)))
    lineages = lineage_r.scalars().all()
    lineage_tables = {
        _norm_upper(edge.source_table) for edge in lineages if edge.source_table
    } | {
        _norm_upper(edge.target_table) for edge in lineages if edge.target_table
    }

    status_counts = Counter()
    for item in items:
        sql_candidates = _json_loads(item.sql_candidates_json, [])
        if not sql_candidates:
            item.validation_status = "no_sql"
            item.validation_score = 0
            item.validation_summary = "No read-only SQL was extracted from the indexed test case."
            item.validation_details_json = _json_dumps({"sql_count": 0})
            status_counts[item.validation_status] += 1
            continue

        syntax_errors: List[str] = []
        referenced_tables: List[str] = []
        matched_tables: List[str] = []
        missing_tables: List[str] = []
        matched_columns: List[str] = []
        missing_columns: List[str] = []

        for sql in sql_candidates:
            syntax_errors.extend(validate_sql_pattern(sql))
            alias_map, tables = _extract_table_aliases(sql)
            columns_by_table = _extract_column_references(sql, alias_map)
            for table_token in tables:
                if table_token not in referenced_tables:
                    referenced_tables.append(table_token)
                schema_name, object_name = _split_table_token(table_token)
                lookup_key = f"{schema_name}.{object_name}" if schema_name else object_name
                schema_object = object_map.get(lookup_key) or object_map.get(object_name)
                if schema_object:
                    canonical = f"{_norm_upper(schema_object.schema_name)}.{_norm_upper(schema_object.object_name)}" if schema_object.schema_name else _norm_upper(schema_object.object_name)
                    if canonical not in matched_tables:
                        matched_tables.append(canonical)
                    object_columns = columns_by_object_id.get(schema_object.id, set())
                    for column_name in columns_by_table.get(table_token, []):
                        if column_name in object_columns:
                            matched_columns.append(f"{canonical}.{column_name}")
                        else:
                            missing_columns.append(f"{canonical}.{column_name}")
                else:
                    missing_tables.append(lookup_key)

        matched_columns = sorted(set(matched_columns))
        missing_columns = sorted(set(missing_columns))
        missing_tables = sorted(set(missing_tables))
        matched_tables = sorted(set(matched_tables))
        referenced_tables = sorted(set(referenced_tables))

        syntax_penalty = 20 if syntax_errors else 0
        table_score = 50 if referenced_tables and not missing_tables else (25 if matched_tables else 0)
        column_score = 30 if matched_columns and not missing_columns else (15 if matched_columns else 0)
        lineage_score = 20 if any(_split_table_token(token)[1] in lineage_tables for token in referenced_tables) else 0
        score = max(0, min(100, table_score + column_score + lineage_score - syntax_penalty))

        if missing_tables:
            status = "failed"
        elif syntax_errors or missing_columns:
            status = "partial"
        else:
            status = "passed"

        summary_parts = []
        if matched_tables:
            summary_parts.append(f"Matched {len(matched_tables)} table/view reference(s)")
        if missing_tables:
            summary_parts.append(f"Missing {len(missing_tables)} table/view reference(s)")
        if missing_columns:
            summary_parts.append(f"Missing {len(missing_columns)} column reference(s)")
        if syntax_errors:
            summary_parts.append(f"Detected {len(syntax_errors)} SQL pattern issue(s)")
        if lineage_score:
            summary_parts.append("Found mapping/lineage coverage for referenced objects")

        item.validation_status = status
        item.validation_score = score
        item.validation_summary = "; ".join(summary_parts) or "Validation completed."
        item.validation_details_json = _json_dumps({
            "syntax_errors": syntax_errors,
            "referenced_tables": referenced_tables,
            "matched_tables": matched_tables,
            "missing_tables": missing_tables,
            "matched_columns": matched_columns[:50],
            "missing_columns": missing_columns[:50],
            "lineage_match": bool(lineage_score),
            "sql_count": len(sql_candidates),
        })
        status_counts[status] += 1

    await db.commit()
    return {
        "validated": len(items),
        "project": project,
        "datasource_id": datasource_id,
        "status_counts": dict(status_counts),
    }


async def list_regression_catalog(
    db: AsyncSession,
    *,
    project: str,
    group: str = "",
    status: str = "",
    search_text: str = "",
    area_path: str = "",
    iteration_path: str = "",
    plan_name: str = "",
    suite_name: str = "",
    owner: str = "",
    title: str = "",
    tags: str = "",
    min_changed_date: Optional[datetime] = None,
    include_excluded: bool = False,
) -> List[RegressionCatalogItem]:
    cfg = await get_or_create_regression_config(db, project)
    effective = _effective_filters_from_config(
        cfg,
        area_paths=[] if area_path else [],
        iteration_paths=[] if iteration_path else [],
        min_changed_date=min_changed_date,
    )
    excluded_item_ids = effective["excluded_item_ids"]
    excluded_plan_ids = effective["excluded_plan_ids"]
    excluded_suite_ids = effective["excluded_suite_ids"]
    exclusion_keywords = effective["exclusion_keywords"]
    include_archived = bool(effective["include_archived"])

    result = await db.execute(
        select(RegressionCatalogItem)
        .where(RegressionCatalogItem.project == project)
        .order_by(RegressionCatalogItem.plan_name.asc(), RegressionCatalogItem.suite_path.asc(), RegressionCatalogItem.title.asc())
    )
    items = result.scalars().all()
    text_filter = _norm_text(search_text).lower()
    area_filter = _norm_text(area_path)
    iteration_filter = _norm_text(iteration_path)
    defaults = _default_filters_for_project(project)
    default_area_filters = _json_loads(cfg.default_area_paths_json, []) or (defaults.get("area_paths") or [])
    default_iteration_filters = _json_loads(cfg.default_iteration_paths_json, []) or (defaults.get("iteration_paths") or [])
    plan_filter = _norm_text(plan_name).lower()
    suite_filter = _norm_text(suite_name).lower()
    owner_filter = _norm_text(owner).lower()
    title_filter = _norm_text(title).lower()
    tags_filter = _norm_text(tags).lower()
    effective_min_changed = min_changed_date or cfg.min_changed_date
    filtered: List[RegressionCatalogItem] = []
    for item in items:
        excluded = False
        if item.test_case_id in excluded_item_ids:
            excluded = True
        elif (item.plan_id or 0) in excluded_plan_ids:
            excluded = True
        elif (item.suite_id or 0) in excluded_suite_ids:
            excluded = True
        elif not include_archived and _is_archive_match_for_item(item, exclusion_keywords):
            excluded = True
        if excluded and not include_excluded:
            continue

        if group and item.domain_group != group:
            continue
        if status and (item.validation_status or "") != status:
            continue
        if area_filter and area_filter.lower() not in _norm_text(item.area_path).lower():
            continue
        if not area_filter and default_area_filters and not _matches_prefix(item.area_path, default_area_filters):
            continue
        if iteration_filter and iteration_filter.lower() not in _norm_text(item.iteration_path).lower():
            continue
        if not iteration_filter and default_iteration_filters and not _matches_prefix(item.iteration_path, default_iteration_filters):
            continue
        if plan_filter and plan_filter not in _norm_text(item.plan_name).lower():
            continue
        if suite_filter and suite_filter not in _norm_text(item.suite_name).lower() and suite_filter not in _norm_text(item.suite_path).lower():
            continue
        if owner_filter and owner_filter not in _norm_text(item.owner).lower():
            continue
        if title_filter and title_filter not in _norm_text(item.title).lower():
            continue
        if tags_filter and tags_filter not in _norm_text(item.tags).lower():
            continue
        if effective_min_changed and item.changed_date:
            item_dt = item.changed_date if item.changed_date.tzinfo else item.changed_date.replace(tzinfo=timezone.utc)
            min_dt = effective_min_changed if effective_min_changed.tzinfo else effective_min_changed.replace(tzinfo=timezone.utc)
            if item_dt < min_dt:
                continue
        haystack = " ".join([
            _norm_text(item.title),
            _norm_text(item.plan_name),
            _norm_text(item.suite_path),
            _norm_text(item.tags),
            _norm_text(item.domain_context),
            _norm_text(item.description_text),
            _norm_text(item.steps_text),
            _norm_text(item.attachment_text),
        ]).lower()
        if text_filter and text_filter not in haystack:
            continue
        filtered.append(item)
    return filtered


async def get_regression_groups(db: AsyncSession, *, project: str) -> List[dict]:
    items = await list_regression_catalog(db, project=project)
    suites_by_group: Dict[str, List[dict]] = defaultdict(list)
    suites: Dict[Tuple[int, int, str], dict] = {}
    for item in items:
        key = (int(item.plan_id or 0), int(item.suite_id or 0), item.suite_path or item.suite_name or "Unassigned Suite")
        suite = suites.setdefault(
            key,
            {
                "plan_id": int(item.plan_id or 0),
                "plan_name": item.plan_name or "Unassigned Plan",
                "suite_id": int(item.suite_id or 0),
                "suite_name": item.suite_name or "Unassigned Suite",
                "suite_path": item.suite_path or item.suite_name or "Unassigned Suite",
                "count": 0,
                "group_votes": Counter(),
                "test_plan_web_url": item.test_plan_web_url or "",
                "test_suite_web_url": item.test_suite_web_url or "",
            },
        )
        suite["count"] += 1
        suite["group_votes"][item.domain_group or "Other"] += 1

    for suite in suites.values():
        suite_group = _best_group_for_text(" ".join([
            _norm_text(suite.get("plan_name")),
            _norm_text(suite.get("suite_name")),
            _norm_text(suite.get("suite_path")),
        ]))
        if suite_group == "Other":
            suite_group = suite["group_votes"].most_common(1)[0][0] if suite["group_votes"] else "Other"
        suites_by_group[suite_group].append(suite)

    payload = []
    for group_name in REGRESSION_MAIN_GROUPS:
        grouped_suites = sorted(
            suites_by_group.get(group_name, []),
            key=lambda suite: (-int(suite["count"]), str(suite["plan_name"]).lower(), str(suite["suite_path"]).lower()),
        )
        payload.append({
            "group": group_name,
            "count": sum(int(suite["count"]) for suite in grouped_suites),
            "suite_count": len(grouped_suites),
            "suites": grouped_suites,
        })
    return payload


async def get_regression_report(db: AsyncSession, *, project: str) -> dict:
    items = await list_regression_catalog(db, project=project)
    total = len(items)
    status_counts = Counter((item.validation_status or "not_validated") for item in items)
    group_counts = Counter((item.domain_group or "Other") for item in items)
    plan_counts = Counter()
    suite_counts = Counter()
    plan_meta: Dict[str, dict] = {}
    suite_meta: Dict[str, dict] = {}
    for item in items:
        plan_key = f"{int(item.plan_id or 0)}|{item.plan_name or 'Unassigned Plan'}"
        suite_key = f"{int(item.plan_id or 0)}|{int(item.suite_id or 0)}|{item.suite_path or item.suite_name or 'Unassigned Suite'}"
        plan_counts[plan_key] += 1
        suite_counts[suite_key] += 1
        if plan_key not in plan_meta:
            plan_meta[plan_key] = {
                "plan_id": int(item.plan_id or 0),
                "plan_name": item.plan_name or "Unassigned Plan",
                "test_plan_web_url": item.test_plan_web_url or "",
            }
        if suite_key not in suite_meta:
            suite_meta[suite_key] = {
                "plan_id": int(item.plan_id or 0),
                "plan_name": item.plan_name or "Unassigned Plan",
                "suite_id": int(item.suite_id or 0),
                "suite_name": item.suite_name or "Unassigned Suite",
                "suite_path": item.suite_path or item.suite_name or "Unassigned Suite",
                "test_suite_web_url": item.test_suite_web_url or "",
            }
    good = status_counts.get("passed", 0)
    bad = status_counts.get("failed", 0) + status_counts.get("partial", 0)
    return {
        "project": project,
        "total_indexed": total,
        "validated": sum(status_counts.get(key, 0) for key in ("passed", "partial", "failed", "no_sql")),
        "good_count": good,
        "bad_count": bad,
        "good_ratio": round((good / total) * 100, 1) if total else 0.0,
        "status_counts": dict(status_counts),
        "group_counts": dict(group_counts),
        "top_plans": [
            {
                **plan_meta[key],
                "count": value,
            }
            for key, value in plan_counts.most_common(10)
        ],
        "top_suites": [
            {
                **suite_meta[key],
                "count": value,
            }
            for key, value in suite_counts.most_common(10)
        ],
    }


async def get_regression_settings(db: AsyncSession, *, project: str) -> dict:
    cfg = await get_or_create_regression_config(db, project)
    defaults = _default_filters_for_project(project)
    default_areas = _json_loads(cfg.default_area_paths_json, []) or (defaults.get("area_paths") or [])
    default_iterations = _json_loads(cfg.default_iteration_paths_json, []) or (defaults.get("iteration_paths") or [])
    exclusion_keywords = _json_loads(cfg.exclusion_keywords_json, []) or DEFAULT_EXCLUSION_KEYWORDS
    return {
        "project": project,
        "default_area_paths": default_areas,
        "default_iteration_paths": default_iterations,
        "exclusion_keywords": exclusion_keywords,
        "excluded_item_ids": _json_loads(cfg.excluded_item_ids_json, []),
        "excluded_plan_ids": _json_loads(cfg.excluded_plan_ids_json, []),
        "excluded_suite_ids": _json_loads(cfg.excluded_suite_ids_json, []),
        "min_changed_date": _dt_to_iso(cfg.min_changed_date),
        "include_archived": bool(cfg.include_archived),
    }


async def update_regression_settings(
    db: AsyncSession,
    *,
    project: str,
    default_area_paths: Optional[Sequence[str]] = None,
    default_iteration_paths: Optional[Sequence[str]] = None,
    exclusion_keywords: Optional[Sequence[str]] = None,
    min_changed_date: Optional[datetime] = None,
    include_archived: Optional[bool] = None,
) -> dict:
    cfg = await get_or_create_regression_config(db, project)
    if default_area_paths is not None:
        cfg.default_area_paths_json = _json_dumps([_norm_text(v) for v in default_area_paths if _norm_text(v)])
    if default_iteration_paths is not None:
        cfg.default_iteration_paths_json = _json_dumps([_norm_text(v) for v in default_iteration_paths if _norm_text(v)])
    if exclusion_keywords is not None:
        cleaned = [_norm_text(v).lower() for v in exclusion_keywords if _norm_text(v)]
        cfg.exclusion_keywords_json = _json_dumps(cleaned or DEFAULT_EXCLUSION_KEYWORDS)
    if min_changed_date is not None:
        cfg.min_changed_date = min_changed_date
    if include_archived is not None:
        cfg.include_archived = bool(include_archived)
    await db.commit()
    return await get_regression_settings(db, project=project)


async def add_exclusions_by_filters(
    db: AsyncSession,
    *,
    project: str,
    mode: str,
    group: str = "",
    status: str = "",
    search_text: str = "",
    area_path: str = "",
    iteration_path: str = "",
    plan_name: str = "",
    suite_name: str = "",
    owner: str = "",
    title: str = "",
    tags: str = "",
    min_changed_date: Optional[datetime] = None,
) -> dict:
    cfg = await get_or_create_regression_config(db, project)
    items = await list_regression_catalog(
        db,
        project=project,
        group=group,
        status=status,
        search_text=search_text,
        area_path=area_path,
        iteration_path=iteration_path,
        plan_name=plan_name,
        suite_name=suite_name,
        owner=owner,
        title=title,
        tags=tags,
        min_changed_date=min_changed_date,
        include_excluded=True,
    )
    excluded_item_ids = set(int(x) for x in _json_loads(cfg.excluded_item_ids_json, []) if str(x).isdigit())
    excluded_plan_ids = set(int(x) for x in _json_loads(cfg.excluded_plan_ids_json, []) if str(x).isdigit())
    excluded_suite_ids = set(int(x) for x in _json_loads(cfg.excluded_suite_ids_json, []) if str(x).isdigit())

    mode_norm = _norm_text(mode).lower() or "item"
    touched = 0
    if mode_norm == "plan":
        for item in items:
            if item.plan_id:
                excluded_plan_ids.add(int(item.plan_id))
                touched += 1
    elif mode_norm == "suite":
        for item in items:
            if item.suite_id:
                excluded_suite_ids.add(int(item.suite_id))
                touched += 1
    else:
        for item in items:
            excluded_item_ids.add(int(item.test_case_id))
            touched += 1

    cfg.excluded_item_ids_json = _json_dumps(sorted(excluded_item_ids))
    cfg.excluded_plan_ids_json = _json_dumps(sorted(excluded_plan_ids))
    cfg.excluded_suite_ids_json = _json_dumps(sorted(excluded_suite_ids))
    await db.commit()
    return {
        "project": project,
        "mode": mode_norm,
        "affected": touched,
        "excluded_item_count": len(excluded_item_ids),
        "excluded_plan_count": len(excluded_plan_ids),
        "excluded_suite_count": len(excluded_suite_ids),
    }


def _serialize_catalog_item(item: RegressionCatalogItem) -> dict:
    return {
        "id": item.id,
        "project": item.project,
        "plan_id": item.plan_id,
        "plan_name": item.plan_name,
        "suite_id": item.suite_id,
        "suite_name": item.suite_name,
        "suite_path": item.suite_path,
        "test_point_id": item.test_point_id,
        "test_case_id": item.test_case_id,
        "title": item.title,
        "state": item.state,
        "priority": item.priority,
        "owner": item.owner,
        "area_path": item.area_path,
        "iteration_path": item.iteration_path,
        "tags": item.tags,
        "domain_group": item.domain_group,
        "domain_context": item.domain_context,
        "validation_status": item.validation_status,
        "validation_score": item.validation_score,
        "validation_summary": item.validation_summary,
        "sql_candidates": _json_loads(item.sql_candidates_json, []),
        "linked_requirement_ids": _json_loads(item.linked_requirement_ids_json, []),
        "linked_requirement_titles": _json_loads(item.linked_requirement_titles_json, []),
        "attachment_names": _json_loads(item.attachment_names_json, []),
        "test_case_web_url": item.test_case_web_url or "",
        "test_plan_web_url": item.test_plan_web_url or "",
        "test_suite_web_url": item.test_suite_web_url or "",
        "created_date": _dt_to_iso(item.created_date),
        "changed_date": _dt_to_iso(item.changed_date),
        "promoted_local_test_count": item.promoted_local_test_count or 0,
    }


def _heuristic_search_score(item: RegressionCatalogItem, query: str) -> int:
    terms = [term for term in re.split(r"\s+", query.lower()) if term]
    haystacks = [
        (_norm_text(item.title).lower(), 8),
        (_norm_text(item.domain_context).lower(), 6),
        (_norm_text(item.suite_path).lower(), 5),
        (_norm_text(item.tags).lower(), 4),
        (_norm_text(item.description_text).lower(), 3),
        (_norm_text(item.steps_text).lower(), 3),
        (_norm_text(item.attachment_text).lower(), 2),
    ]
    score = 0
    for term in terms:
        for haystack, weight in haystacks:
            if term in haystack:
                score += weight
    return score


async def run_search_agent(
    db: AsyncSession,
    *,
    project: str,
    query: str,
    group: str = "",
    status: str = "",
    area_path: str = "",
    iteration_path: str = "",
    plan_name: str = "",
    suite_name: str = "",
    owner: str = "",
    title: str = "",
    tags: str = "",
) -> dict:
    items = await list_regression_catalog(
        db,
        project=project,
        group=group,
        status=status,
        area_path=area_path,
        iteration_path=iteration_path,
        plan_name=plan_name,
        suite_name=suite_name,
        owner=owner,
        title=title,
        tags=tags,
    )
    ranked = sorted(
        ((item, _heuristic_search_score(item, query)) for item in items),
        key=lambda pair: (pair[1], pair[0].validation_score or -1),
        reverse=True,
    )
    ranked_matches = [(item, score) for item, score in ranked if score > 0]
    matches = [_serialize_catalog_item(item) for item, _score in ranked_matches[:50]]
    if not matches:
        return {
            "analysis": "No indexed regression cases matched the search terms.",
            "matches": [],
            "total_matches": 0,
        }

    context_lines = []
    for index, match in enumerate(matches[:12], start=1):
        context_lines.append(
            f"{index}. {match['title']} | group={match['domain_group']} | suite={match['suite_path']} | "
            f"validation={match['validation_status'] or 'not_validated'} | sql_count={len(match['sql_candidates'])} | "
            f"linked_pbis={','.join(str(x) for x in match['linked_requirement_ids'][:5]) or '-'}"
        )

    provider = "githubcopilot"
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {
            "analysis": "\n".join([
                f"Search agent fallback: {cfg_error}",
                "Top matching indexed cases:",
                *context_lines,
            ]),
            "matches": matches,
        }

    prompt = (
        "You are the Regression Search Agent for the DB Testing Tool. "
        "Given indexed TFS regression tests, identify the best regression candidates for the user's request. "
        "Prioritize business fit, SQL availability, validation quality, and linked PBI relevance. "
        "Return no more than 8 short plain-text lines total. Include: top fit, key gaps, and recommended next action. "
        "Do not dump the candidate list back verbatim because the UI shows the matches separately.\n\n"
        f"User request: {query}\n\n"
        "Indexed candidates:\n"
        + "\n".join(context_lines)
    )
    try:
        messages = [{"role": "user", "content": prompt}]
        call_args = _build_chat_call_args(messages, 0.2, 900, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        analysis = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        analysis = "\n".join([
            f"Search agent fallback due to AI error: {exc}",
            f"Showing the top {min(len(matches), 50)} ranked matches below.",
            *context_lines,
        ])
    return {
        "analysis": analysis,
        "matches": matches,
        "total_matches": len(ranked_matches),
    }


async def run_validation_agent(
    db: AsyncSession,
    *,
    project: str,
    datasource_id: int,
    item_ids: Sequence[int] | None = None,
) -> dict:
    validation = await validate_regression_catalog(
        db,
        datasource_id=datasource_id,
        project=project,
        item_ids=item_ids,
    )
    report = await get_regression_report(db, project=project)
    summary = (
        f"Validated {validation.get('validated', 0)} indexed regression case(s) against datasource {datasource_id}. "
        f"Passed: {report['status_counts'].get('passed', 0)}, partial: {report['status_counts'].get('partial', 0)}, "
        f"failed: {report['status_counts'].get('failed', 0)}, no_sql: {report['status_counts'].get('no_sql', 0)}."
    )
    return {
        "summary": summary,
        "validation": validation,
        "report": report,
    }


async def promote_regression_items_to_local_tests(
    db: AsyncSession,
    *,
    item_ids: Sequence[int],
    source_datasource_id: Optional[int],
    target_datasource_id: Optional[int],
) -> dict:
    if not item_ids:
        return {"created_count": 0, "folders": []}
    result = await db.execute(select(RegressionCatalogItem).where(RegressionCatalogItem.id.in_([int(x) for x in item_ids])))
    items = result.scalars().all()
    created = []
    folder_names = set()

    for item in items:
        sql_candidates = _json_loads(item.sql_candidates_json, [])
        if not sql_candidates:
            continue
        folder_name = f"Regression Lab - {item.domain_group or 'Other'} - {item.domain_context or item.suite_name or 'General'}"
        folder = await _ensure_folder(db, folder_name)
        if folder:
            folder_names.add(folder.name)
        for index, sql in enumerate(sql_candidates, start=1):
            test_name = item.title if len(sql_candidates) == 1 else f"{item.title} [SQL {index}]"
            
            # CORRECTNESS FIX: Ensure expected_result is properly formatted JSON
            # The test executor expects expected_result to be valid JSON (int, float, or dict)
            expected_result = None
            if item.expected_results_text:
                try:
                    # Try to parse - if it's already JSON, this will work
                    parsed = json.loads(item.expected_results_text)
                    expected_result = item.expected_results_text
                except (json.JSONDecodeError, TypeError):
                    # If it's not JSON, try to convert it to a number
                    try:
                        val = float(item.expected_results_text) if item.expected_results_text else None
                        expected_result = str(val) if val is not None else None
                    except (ValueError, TypeError):
                        # Can't convert - leave as None, test will execute without assertion
                        expected_result = None
            
            local_test = TestCase(
                name=test_name,
                test_type="custom_sql",
                source_datasource_id=source_datasource_id,
                target_datasource_id=target_datasource_id,
                source_query=sql,
                target_query=None,
                expected_result=expected_result,
                severity="medium",
                description=_build_regression_local_test_description(item, sql),
                is_active=True,
                is_ai_generated=False,
            )
            db.add(local_test)
            await db.flush()
            if folder:
                await _assign_test_to_folder(db, local_test.id, folder.id)
            created.append({"id": local_test.id, "name": local_test.name, "folder_name": folder.name if folder else None})
        item.promoted_local_test_count = (item.promoted_local_test_count or 0) + len(sql_candidates)

    await db.commit()
    return {"created_count": len(created), "tests": created, "folders": sorted(folder_names)}


async def get_regression_distinct_values(
    db: AsyncSession,
    *,
    project: str,
    filter_text: str = "",
) -> dict:
    """Return distinct iterations, areas, plans, and suites from the indexed catalog."""
    result = await db.execute(
        select(RegressionCatalogItem)
        .where(RegressionCatalogItem.project == project)
    )
    items = result.scalars().all()
    filter_lower = _norm_text(filter_text).lower()

    iterations: Dict[str, int] = Counter()
    areas: Dict[str, int] = Counter()
    plans: Dict[str, dict] = {}
    suites: Dict[str, dict] = {}

    for item in items:
        iter_path = _norm_text(item.iteration_path)
        area_path = _norm_text(item.area_path)
        plan_key = f"{item.plan_id or 0}|{item.plan_name or ''}"
        suite_key = f"{item.plan_id or 0}|{item.suite_id or 0}|{item.suite_name or ''}"

        if iter_path:
            iterations[iter_path] += 1
        if area_path:
            areas[area_path] += 1
        if plan_key not in plans:
            plans[plan_key] = {"plan_id": item.plan_id, "plan_name": item.plan_name or "", "count": 0}
        plans[plan_key]["count"] += 1
        if suite_key not in suites:
            suites[suite_key] = {
                "plan_id": item.plan_id,
                "suite_id": item.suite_id,
                "suite_name": item.suite_name or "",
                "suite_path": item.suite_path or "",
                "plan_name": item.plan_name or "",
                "count": 0,
            }
        suites[suite_key]["count"] += 1

    def _filter_dict(d, key_fn=None):
        if not filter_lower:
            return d
        if key_fn:
            return {k: v for k, v in d.items() if filter_lower in key_fn(k, v).lower()}
        return {k: v for k, v in d.items() if filter_lower in k.lower()}

    filtered_iterations = _filter_dict(iterations)
    filtered_areas = _filter_dict(areas)
    filtered_plans = _filter_dict(plans, lambda k, v: v.get("plan_name", ""))
    filtered_suites = _filter_dict(suites, lambda k, v: f"{v.get('suite_name', '')} {v.get('suite_path', '')} {v.get('plan_name', '')}")

    return {
        "project": project,
        "iterations": [{"path": k, "count": v} for k, v in sorted(filtered_iterations.items(), key=lambda x: x[0])],
        "areas": [{"path": k, "count": v} for k, v in sorted(filtered_areas.items(), key=lambda x: x[0])],
        "plans": sorted(filtered_plans.values(), key=lambda x: x.get("plan_name", "").lower()),
        "suites": sorted(filtered_suites.values(), key=lambda x: f"{x.get('plan_name', '')}{x.get('suite_name', '')}".lower()),
    }
