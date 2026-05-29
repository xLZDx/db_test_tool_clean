"""Shared helpers, models, and state for the /api/tests router family.

Imported by tests.py, tests_control_table.py, and tests_training.py.
Does NOT define any routes or APIRouter.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.database import async_session
from app.models.test_case import TestCase, TestRun, TestFolder, TestCaseFolder
from app.models.control_table_training import ControlTableCorrectionRule, ControlTableFileState
from app.models.datasource import DataSource
from app.services.test_executor import run_test, run_all_tests
from app.config import DATA_DIR
from pydantic import BaseModel
from typing import Optional, List
import re
from datetime import datetime

# ── Constants / shared state ─────────────────────────────────────────────────

DEFAULT_TEST_FOLDER_NAME = "All Tests"
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
TRAINING_PACK_ROOT = Path(__file__).resolve().parents[2] / "training_packs"

_batch_control: dict = {}
_batch_tasks: dict = {}

# ── Pydantic models ───────────────────────────────────────────────────────────


class TestCreate(BaseModel):
    name: str
    test_type: str
    mapping_rule_id: Optional[int] = None
    source_datasource_id: Optional[int] = None
    target_datasource_id: Optional[int] = None
    source_query: Optional[str] = None
    target_query: Optional[str] = None
    expected_result: Optional[str] = None
    tolerance: float = 0.0
    severity: str = "medium"
    description: Optional[str] = None


class RunRequest(BaseModel):
    test_ids: Optional[List[int]] = None


class StartBatchRequest(BaseModel):
    test_ids: Optional[List[int]] = None


class FolderDatasourceUpdateRequest(BaseModel):
    source_datasource_id: Optional[int] = None
    target_datasource_id: Optional[int] = None


class TrainingEventRequest(BaseModel):
    event_type: str
    entity_type: str = ""
    entity_id: Optional[str] = None
    target_table: str = ""
    source: str = ""
    status: str = ""
    details: dict = {}
    knowledge_refs: List[str] = []


class ControlTableTrainingRuleUpdate(BaseModel):
    replacement_expression: Optional[str] = None
    recommended_source: Optional[str] = None
    issue_type: Optional[str] = None
    notes: Optional[str] = None


class ControlTableTrainingRuleCreate(BaseModel):
    target_table: str
    target_column: str
    replacement_expression: Optional[str] = None
    recommended_source: Optional[str] = None
    issue_type: Optional[str] = None
    notes: Optional[str] = None


class TrainingAutomationRequest(BaseModel):
    interval_seconds: int = 600
    mode: str = "ghc"
    agent_id: Optional[int] = None
    target_table: str = ""
    max_packs_per_cycle: int = 3


class CreateSelectedRequest(BaseModel):
    tests: List[dict]


class FolderCreateRequest(BaseModel):
    name: str


class MoveTestsToFolderRequest(BaseModel):
    test_ids: List[int]
    folder_id: int


class ValidateSqlRequest(BaseModel):
    tests: List[dict]
    datasource_id: Optional[int] = None


class ExportTfsCsvRequest(BaseModel):
    test_ids: List[int]
    area_path: str = ""
    assigned_to: str = ""
    state: str = "Design"


class BulkFolderDeleteRequest(BaseModel):
    folder_ids: List[int]


class BulkDeleteRequest(BaseModel):
    ids: List[int]


# ── Shared helper functions ───────────────────────────────────────────────────


async def _ensure_non_redshift_datasource(db: AsyncSession, datasource_id: int, label: str) -> DataSource:
    ds = await db.get(DataSource, datasource_id)
    if not ds:
        raise HTTPException(status_code=404, detail=f"{label} datasource not found")
    if (ds.db_type or "").strip().lower() == "redshift":
        raise HTTPException(status_code=400, detail="Redshift testing is disabled. Use CDS or LH Oracle datasource.")
    return ds


def _extract_target_table_name(target_query: Optional[str], source_query: Optional[str]) -> Optional[str]:
    """Extract target table name from SQL queries, trying multiple sources."""
    if source_query:
        m = re.search(r'\bLEFT\s+JOIN\s+[\w\.]+\.([\w]+)\s+T\b', source_query, flags=re.IGNORECASE)
        if m:
            return m.group(1)[:255]
        m = re.search(r'\bFROM\s+[\w\.]+\.([\w]+)\s+T\b', source_query, flags=re.IGNORECASE)
        if m:
            return m.group(1)[:255]
    for sql_text in [target_query, source_query]:
        if not sql_text:
            continue
        m = re.search(r'\b(?:FROM|JOIN)\s+(["\w\.]+)', sql_text, flags=re.IGNORECASE)
        if m:
            token = m.group(1).replace('"', '')
            table = token.split('.')[-1]
            if table:
                return table[:255]
    return None


def _normalize_oracle_identifier(name: str) -> str:
    token = (name or "").strip().replace('"', '')
    token = token.replace("`", "")
    token = re.sub(r"[^A-Z0-9_\.]+", "", token.upper())
    token = token.strip(".")
    return token


def _extract_sql_table_tokens(sql_text: str) -> dict:
    sql = str(sql_text or "")
    target_candidates = []
    source_candidates = []

    for m in re.finditer(r'\bINSERT\s+INTO\s+([\w\.\"]+)', sql, flags=re.IGNORECASE):
        token = _normalize_oracle_identifier(m.group(1))
        if token:
            target_candidates.append(token)

    for m in re.finditer(r'\b(?:FROM|JOIN)\s+([\w\.\"]+)', sql, flags=re.IGNORECASE):
        token = _normalize_oracle_identifier(m.group(1))
        if token and token.upper() not in {"DUAL", "SYS"}:
            source_candidates.append(token)

    return {
        "targets": list(dict.fromkeys(target_candidates)),
        "sources": list(dict.fromkeys(source_candidates)),
    }


def _extract_table_like_tokens_from_text(text: str) -> List[str]:
    tokens = re.findall(r'\b([A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*)\b', (text or "").upper())
    seen: dict = {}
    result = []
    for t in tokens:
        if t not in seen:
            seen[t] = True
            result.append(t)
    return result


def _derive_training_context(
    *,
    target_table: str,
    source_tables_csv: str,
    source_sql: str,
    expected_sql: str,
    file_names: List[str],
    file_texts: List[str],
) -> dict:
    explicit_target = _normalize_oracle_identifier(target_table)
    explicit_sources = [
        _normalize_oracle_identifier(item)
        for item in (source_tables_csv or "").split(",")
        if _normalize_oracle_identifier(item)
    ]

    sql_tokens = _extract_sql_table_tokens("\n".join([source_sql or "", expected_sql or ""]))
    text_tokens = _extract_table_like_tokens_from_text("\n".join(file_texts or []))
    name_tokens = _extract_table_like_tokens_from_text("\n".join(file_names or []))

    derived_target = explicit_target
    if not derived_target and sql_tokens["targets"]:
        derived_target = sql_tokens["targets"][0]
    if not derived_target:
        for candidate in text_tokens:
            if "." in candidate:
                derived_target = candidate
                break
    if not derived_target and text_tokens:
        derived_target = text_tokens[0]

    source_candidates = []
    for token in explicit_sources + sql_tokens["sources"] + text_tokens + name_tokens:
        normalized = _normalize_oracle_identifier(token)
        if not normalized:
            continue
        if derived_target and normalized == derived_target:
            continue
        source_candidates.append(normalized)

    deduped_sources = []
    seen: set = set()
    for token in source_candidates:
        if token in seen:
            continue
        seen.add(token)
        deduped_sources.append(token)

    hints = []
    if derived_target and "." not in derived_target:
        hints.append("Target table has no schema prefix; prefer SCHEMA.TABLE for Oracle execution.")
    if "MERGE" in (source_sql or "").upper():
        hints.append("MERGE detected in source SQL; verify join keys and update clauses against DRD grain.")
    if len(deduped_sources) > 8:
        hints.append("Many source tables detected; keep only DRD-critical tables before training replay.")

    return {
        "target_table": derived_target,
        "source_tables": deduped_sources[:20],
        "oracle_normalized": True,
        "hints": hints,
    }


def _derive_control_suite_base_name(suite_name: Optional[str], tests: List[dict]) -> str:
    requested = (suite_name or "").strip()
    for test_def in tests or []:
        name = (test_def.get("name") or "").strip()
        m = re.search(r"\bfor\s+([A-Z0-9_]+)\s*$", name, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()
        m = re.search(r"^([A-Z0-9_]+):\s+.*control\s+vs\s+target$", name, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return requested or "CONTROL_TABLE_SUITE"


async def _ensure_folder(db: AsyncSession, folder_name: str) -> Optional[TestFolder]:
    name = (folder_name or "").strip()
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


async def _create_new_folder(db: AsyncSession, folder_name: str) -> Optional[TestFolder]:
    name = (folder_name or "").strip()
    if not name:
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = TestFolder(name=f"{name}_{timestamp}")
    db.add(folder)
    await db.flush()
    return folder


async def _assign_test_to_folder(db: AsyncSession, test_id: int, folder_id: int):
    existing = await db.execute(select(TestCaseFolder).where(TestCaseFolder.test_case_id == test_id))
    link = existing.scalar_one_or_none()
    if link:
        link.folder_id = folder_id
    else:
        db.add(TestCaseFolder(test_case_id=test_id, folder_id=folder_id))


async def _delete_folder_with_children(db: AsyncSession, folder_id: int) -> dict:
    folder = await db.get(TestFolder, folder_id)
    if not folder:
        return {"deleted": False, "folder_id": folder_id, "tests_deleted": 0, "runs_deleted": 0}

    links_q = await db.execute(select(TestCaseFolder.test_case_id).where(TestCaseFolder.folder_id == folder_id))
    test_ids = [row[0] for row in links_q.all() if row and row[0] is not None]

    runs_deleted = 0
    tests_deleted = 0
    if test_ids:
        runs_res = await db.execute(delete(TestRun).where(TestRun.test_case_id.in_(test_ids)))
        tests_res = await db.execute(delete(TestCase).where(TestCase.id.in_(test_ids)))
        runs_deleted = runs_res.rowcount or 0
        tests_deleted = tests_res.rowcount or 0

    await db.execute(delete(TestCaseFolder).where(TestCaseFolder.folder_id == folder_id))
    await db.delete(folder)
    return {
        "deleted": True,
        "folder_id": folder_id,
        "tests_deleted": tests_deleted,
        "runs_deleted": runs_deleted,
    }


async def _run_batch_background(batch_id: str, test_ids: Optional[List[int]] = None):
    async with async_session() as db:
        if test_ids:
            tests_r = await db.execute(select(TestCase).where(TestCase.id.in_(test_ids), TestCase.is_active == True))
            tests = tests_r.scalars().all()
        else:
            tests_r = await db.execute(select(TestCase).where(TestCase.is_active == True))
            tests = tests_r.scalars().all()

        _batch_control[batch_id]["total"] = len(tests)
        _batch_control[batch_id]["status"] = "running"

        for idx, test in enumerate(tests):
            if _batch_control.get(batch_id, {}).get("stopped"):
                _batch_control[batch_id]["status"] = "stopped"
                return
            _batch_control[batch_id]["current_test_number"] = idx + 1
            _batch_control[batch_id]["current_test_id"] = test.id
            try:
                run = await run_test(db, test.id)
                _batch_control[batch_id]["completed"] += 1
                if run.status == "passed":
                    _batch_control[batch_id]["passed"] += 1
                elif run.status == "failed":
                    _batch_control[batch_id]["failed"] += 1
                else:
                    _batch_control[batch_id]["error"] += 1
            except Exception:
                _batch_control[batch_id]["error"] += 1
                _batch_control[batch_id]["completed"] += 1

        _batch_control[batch_id]["status"] = "completed"
        _batch_control[batch_id]["current_test_number"] = None
        _batch_control[batch_id]["current_test_id"] = None
