"""AI-assisted endpoints."""
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi import Depends

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB hard limit before buffering into memory


async def _read_upload_checked(file: UploadFile, max_bytes: int = _MAX_UPLOAD_BYTES) -> bytes:
    """Read an UploadFile with a hard size limit before the full read."""
    # Check Content-Length header if available to fail fast
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(413, f"Upload too large ({file.size} bytes > {max_bytes} limit)")
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, f"Upload exceeds {max_bytes // 1024 // 1024} MB limit")
    return data
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import asyncio
import csv
import io
import json
import time
import uuid
from app.services.ai_service import (
    ai_extract_rules,
    ai_suggest_tests,
    ai_triage_failures,
    ai_analyze_sql,
    ai_chat,
    ai_compare_mapping_with_sql,
    ai_generate_tests_from_mapping_with_kb,
)
from app.services.agent_service import build_combined_agent_prompt
from app.services.copilot_auth_service import (
    start_device_flow,
    poll_device_flow,
    get_copilot_status,
    logout_copilot,
)
from app.database import get_db
from app.models.agent_profile import AgentProfile
from app.services.drd_import_service import parse_drd_file, generate_drd_tests
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/api/ai", tags=["ai"])

# In-memory store for background training-reproduce jobs (per process lifetime).
_TRAINING_JOBS: dict = {}  # job_id -> {status, result, error, started_at}


def _decode_text(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            return file_bytes.decode(encoding)
        except Exception:
            continue
    return ""


def _attachment_to_text(file_bytes: bytes, filename: str, max_chars: int = 20000, max_rows: int = 250) -> tuple[str, str]:
    name = (filename or "attachment").lower()

    if name.endswith(".csv"):
        text = _decode_text(file_bytes)
        if not text:
            return "", "Could not decode CSV text."
        reader = csv.reader(io.StringIO(text))
        rows = []
        for idx, row in enumerate(reader):
            if idx >= max_rows:
                break
            rows.append("\t".join("" if c is None else str(c) for c in row))
        joined = "\n".join(rows)
        note = ""
        if len(joined) > max_chars:
            joined = joined[:max_chars]
            note = f"Truncated to first {max_chars} chars."
        return joined, note

    if name.endswith(".xlsx") or name.endswith(".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
            ws = wb.active
            rows = []
            for idx, row in enumerate(ws.iter_rows(values_only=True)):
                if idx >= max_rows:
                    break
                rows.append("\t".join("" if c is None else str(c) for c in row))
            header = f"Sheet: {ws.title}\n"
            content = header + "\n".join(rows)
            note = ""
            if len(content) > max_chars:
                content = content[:max_chars]
                note = f"Truncated to first {max_chars} chars."
            return content, note
        except Exception as e:
            return "", f"Excel parse failed: {str(e)}"

    if name.endswith(".json"):
        text = _decode_text(file_bytes)
        if not text:
            return "", "Could not decode JSON text."
        try:
            obj = json.loads(text)
            content = json.dumps(obj, indent=2)
        except Exception:
            content = text
        note = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            note = f"Truncated to first {max_chars} chars."
        return content, note

    text = _decode_text(file_bytes)
    if text:
        note = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            note = f"Truncated to first {max_chars} chars."
        return text, note

    return "", "Binary file attached (content not inlined)."


def _test_category(test_def: dict) -> str:
    if (test_def.get("test_type") or "").lower() == "row_count":
        return "row_count"
    mapping_type = (test_def.get("mapping_type") or "").lower().strip()
    if mapping_type in {"direct", "complex"}:
        return mapping_type
    tr = (test_def.get("transformation_rule") or test_def.get("transformation") or "").strip()
    return "complex" if tr else "direct"


def _sample_tests_by_category(test_defs: List[dict], per_category: int = 2) -> List[dict]:
    per_category = max(1, int(per_category or 2))
    row_counts = [t for t in test_defs if _test_category(t) == "row_count"]
    direct = [t for t in test_defs if _test_category(t) == "direct"]
    complex_tests = [t for t in test_defs if _test_category(t) == "complex"]

    sampled = []
    if row_counts:
        sampled.append(row_counts[0])  # only one row-count test
    sampled.extend(direct[:per_category])
    sampled.extend(complex_tests[:per_category])
    return sampled


class SqlInput(BaseModel):
    sql_text: str
    agent_ids: Optional[List[int]] = []
    task_hint: Optional[str] = ""


class TriageInput(BaseModel):
    failures: List[dict]


class SuggestInput(BaseModel):
    mapping_rule: dict
    schema_info: Optional[dict] = {}
    agent_ids: Optional[List[int]] = []
    task_hint: Optional[str] = ""


class ChatInput(BaseModel):
    messages: List[dict]
    context: Optional[str] = ""
    provider: Optional[str] = ""
    agent_ids: Optional[List[int]] = []
    task_hint: Optional[str] = ""
    attachments: Optional[List[dict]] = []  # [{name, type, content, size}, ...]


class CopilotPollInput(BaseModel):
    device_code: str


class MappingSqlCompareInput(BaseModel):
    mapping_text: Optional[str] = ""
    mapping_rows: Optional[List[dict]] = []
    sql_text: str
    source_table: Optional[str] = ""
    target_table: Optional[str] = ""
    single_db_testing: bool = True
    cross_db_optional: bool = True
    agent_ids: Optional[List[int]] = []
    task_hint: Optional[str] = ""


async def _resolve_agent_prompt(db: AsyncSession, agent_ids: Optional[List[int]], task_hint: str) -> str:
    if not agent_ids:
        return ""
    result = await db.execute(select(AgentProfile).where(AgentProfile.id.in_(agent_ids)))
    agents = result.scalars().all()
    payload = [
        {
            "name": a.name,
            "role": a.role,
            "domains": a.domains,
            "system_prompt": a.system_prompt,
            "is_active": a.is_active,
        }
        for a in agents
    ]
    return build_combined_agent_prompt(payload, task_hint=task_hint)


@router.post("/extract-rules")
async def extract_rules(body: SqlInput, db: AsyncSession = Depends(get_db)):
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "SQL mapping extraction")
    return await ai_extract_rules(body.sql_text, agent_prompt)


@router.post("/suggest-tests")
async def suggest_tests(body: SuggestInput, db: AsyncSession = Depends(get_db)):
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "test case design")
    return await ai_suggest_tests(body.mapping_rule, body.schema_info, agent_prompt)


@router.post("/triage")
async def triage(body: TriageInput):
    return await ai_triage_failures(body.failures)


@router.post("/analyze-sql")
async def analyze_sql(body: SqlInput, db: AsyncSession = Depends(get_db)):
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "SQL deep analysis")
    return await ai_analyze_sql(body.sql_text, agent_prompt)


@router.post("/chat")
async def chat(body: ChatInput, db: AsyncSession = Depends(get_db)):
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "general assistant")
    
    # Build attachment context to pass into message system
    attachment_context = ""
    if body.attachments:
        attachment_lines = [f"Attached files ({len(body.attachments)}):"]
        for att in body.attachments:
            name = att.get("name", "unknown")
            size = att.get("size", 0)
            content = att.get("content", "")
            attachment_lines.append(f"- {name} ({size} bytes)")
            if content:
                preview = content[:1000] if len(content) > 1000 else content
                attachment_lines.append(f"  Content preview: {preview}{'...' if len(content) > 1000 else ''}")
        attachment_context = "\n".join(attachment_lines)
    
    combined_context = ""
    if body.context:
        combined_context = body.context
    if attachment_context:
        combined_context = f"{combined_context}\n\n{attachment_context}" if combined_context else attachment_context
    
    return await ai_chat(
        body.messages,
        combined_context,
        body.provider or "",
        agent_prompt,
        body.attachments or [],
    )


async def _run_training_reproduce_job(job_id: str, messages: list, context: str, provider: str, agent_prompt: str, attachments: list):
    """Background task: run ai_chat and store the result in _TRAINING_JOBS."""
    try:
        result = await ai_chat(messages, context, provider, agent_prompt, attachments)
        _TRAINING_JOBS[job_id].update({"status": "done", "result": result})
    except Exception as exc:
        _TRAINING_JOBS[job_id].update({"status": "error", "error": str(exc)})


@router.post("/training-reproduce-async")
async def training_reproduce_async(body: ChatInput, db: AsyncSession = Depends(get_db)):
    """Start a background SQL Dev reproduction job. Returns {job_id} immediately; poll GET endpoint for result."""
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "sql dev reproduction training")
    job_id = uuid.uuid4().hex[:12]
    _TRAINING_JOBS[job_id] = {"status": "running", "result": None, "error": None, "started_at": time.time()}
    # Prune old completed jobs (keep last 50)
    done_ids = [k for k, v in _TRAINING_JOBS.items() if v.get("status") != "running"]
    for old_id in done_ids[:-49]:
        _TRAINING_JOBS.pop(old_id, None)
    # Resolve context upfront (same logic as /chat)
    combined_context = body.context or ""
    if body.attachments:
        att_lines = [f"Attachment: {a.get('name','file')} — {a.get('content','')[:800]}" for a in body.attachments if a.get("content")]
        if att_lines:
            combined_context += "\n\n" + "\n".join(att_lines)
    asyncio.create_task(_run_training_reproduce_job(job_id, body.messages, combined_context, body.provider or "", agent_prompt, body.attachments or []))
    return {"job_id": job_id}


@router.get("/training-reproduce-async/{job_id}")
async def training_reproduce_async_status(job_id: str):
    """Poll a background training reproduce job. Returns {status, result, error}."""
    job = _TRAINING_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "elapsed_seconds": round(time.time() - job.get("started_at", time.time()), 1),
    }


@router.post("/compare-mapping-sql")
async def compare_mapping_sql(body: MappingSqlCompareInput, db: AsyncSession = Depends(get_db)):
    agent_prompt = await _resolve_agent_prompt(db, body.agent_ids, body.task_hint or "mapping to SQL comparison")
    return await ai_compare_mapping_with_sql(
        mapping_rows=body.mapping_rows,
        sql_text=body.sql_text,
        source_table=body.source_table or "",
        target_table=body.target_table or "",
        single_db_testing=body.single_db_testing,
        cross_db_optional=body.cross_db_optional,
        mapping_text=body.mapping_text or "",
        agent_system_prompt=agent_prompt,
        provider_override="githubcopilot",
    )


@router.post("/import-mapping-tests")
async def import_mapping_tests(
    file: UploadFile = File(...),
    selected_fields: str = "",
    target_schema: str = "",
    target_table: str = "",
    source_datasource_id: int = 1,
    target_datasource_id: int = 1,
    sql_text: str = "",
    generation_mode: str = "sample",
    sample_per_category: int = 2,
    source_table: str = "",
    agent_ids: str = "",
    task_hint: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Import mapping CSV/Excel and generate SQL tests.

    Behavior:
    - If SQL is missing -> generate tests from mapping only.
    - If SQL provided -> compare mapping vs SQL and return AI/heuristic suggested tests.
    """
    allowed = ('.csv', '.xlsx', '.xls')
    if not any((file.filename or "").lower().endswith(ext) for ext in allowed):
        raise HTTPException(400, "File must be CSV or Excel format (.csv, .xlsx, .xls)")

    file_bytes = await _read_upload_checked(file)
    fields_list = [f.strip() for f in selected_fields.split(",") if f.strip()]
    if not fields_list:
        fields_list = [
            "logical_name", "physical_name", "source_schema", "source_table",
            "source_attribute", "transformation", "notes",
        ]

    parse_result = parse_drd_file(
        file_bytes=file_bytes,
        filename=file.filename or "mapping.csv",
        selected_fields=fields_list,
        target_schema=target_schema,
        target_table=target_table,
        source_datasource_id=source_datasource_id,
        target_datasource_id=target_datasource_id,
        default_source_table=source_table,
    )

    rows = parse_result.get("column_mappings", [])
    if not rows:
        return {
            "status": "error",
            "mode": "mapping_only",
            "message": "No mapping rows parsed from file",
            "errors": parse_result.get("errors", []),
            "suggested_tests": [],
        }

    if not (sql_text or "").strip():
        mapping_tests = generate_drd_tests(
            column_mappings=rows,
            target_schema=target_schema,
            target_table=target_table,
            source_datasource_id=source_datasource_id,
            target_datasource_id=target_datasource_id,
            default_source_table=source_table,
        )
        all_tests = mapping_tests
        selected_tests = all_tests if generation_mode == "all" else _sample_tests_by_category(all_tests, sample_per_category)
        return {
            "status": "success",
            "mode": "mapping_only",
            "generation_mode": generation_mode,
            "message": (
                f"Generated {len(selected_tests)} SQL test(s) from mapping file "
                f"({'full set' if generation_mode == 'all' else 'sample: 2 per direct/complex category'})"
            ),
            "suggested_tests": selected_tests,
            "total_available": len(all_tests),
            "category_counts": {
                "row_count": len([t for t in all_tests if _test_category(t) == "row_count"]),
                "direct": len([t for t in all_tests if _test_category(t) == "direct"]),
                "complex": len([t for t in all_tests if _test_category(t) == "complex"]),
            },
            "errors": parse_result.get("errors", []),
            "stats": parse_result.get("stats", {}),
        }

    # SQL provided -> run mapping-vs-SQL compare and use AI/heuristic suggestions
    parsed_agent_ids = []
    for raw in (agent_ids or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            parsed_agent_ids.append(int(raw))
    agent_prompt = await _resolve_agent_prompt(db, parsed_agent_ids, task_hint or "mapping to SQL comparison")

    compare_result = await ai_compare_mapping_with_sql(
        mapping_rows=rows,
        sql_text=sql_text,
        source_table=source_table,
        target_table=(f"{target_schema}.{target_table}" if target_schema else target_table),
        single_db_testing=True,
        cross_db_optional=True,
        mapping_text="",
        agent_system_prompt=agent_prompt,
        provider_override="githubcopilot",
    )

    suggested = compare_result.get("suggested_tests") or []
    hydrated = []
    for t in suggested:
        hydrated.append({
            "name": t.get("name") or "AI Mapping Validation",
            "test_type": t.get("test_type") or "value_match",
            "source_datasource_id": source_datasource_id,
            "target_datasource_id": target_datasource_id,
            "source_query": t.get("source_query") or "",
            "target_query": t.get("target_query") or "",
            "severity": t.get("severity") or "medium",
            "description": t.get("description") or "",
            "source_field": t.get("source_field") or "",
            "target_field": t.get("field") or t.get("target_field") or "",
            "transformation_rule": t.get("transformation") or t.get("transformation_rule") or "",
            "mapping_type": t.get("mapping_type") or ("complex" if (t.get("transformation") or t.get("transformation_rule")) else "direct"),
        })

    all_tests = hydrated
    selected_tests = all_tests if generation_mode == "all" else _sample_tests_by_category(all_tests, sample_per_category)

    return {
        "status": "success",
        "mode": "sql_compare",
        "generation_mode": generation_mode,
        "message": (
            f"Generated {len(selected_tests)} suggested SQL test(s) using mapping + SQL comparison "
            f"({'full set' if generation_mode == 'all' else 'sample: 2 per direct/complex category'})"
        ),
        "suggested_tests": selected_tests,
        "total_available": len(all_tests),
        "category_counts": {
            "row_count": len([t for t in all_tests if _test_category(t) == "row_count"]),
            "direct": len([t for t in all_tests if _test_category(t) == "direct"]),
            "complex": len([t for t in all_tests if _test_category(t) == "complex"]),
        },
        "compare": compare_result,
        "errors": parse_result.get("errors", []),
        "stats": parse_result.get("stats", {}),
    }


@router.get("/copilot/status")
async def copilot_status():
    return get_copilot_status()


@router.post("/copilot/device/start")
async def copilot_device_start():
    return await start_device_flow()


@router.post("/copilot/device/poll")
async def copilot_device_poll(body: CopilotPollInput):
    return await poll_device_flow(body.device_code)


@router.post("/copilot/logout")
async def copilot_logout():
    return logout_copilot()


@router.post("/attachment-text")
async def attachment_text(file: UploadFile = File(...), max_chars: int = 20000):
    """Extract attachment text for AI chat context (supports xlsx/xls/csv/json/text)."""
    file_bytes = await _read_upload_checked(file)
    content, note = _attachment_to_text(file_bytes, file.filename or "attachment", max_chars=max(2000, min(max_chars, 120000)))
    return {
        "name": file.filename or "attachment",
        "size": len(file_bytes),
        "type": file.content_type or "application/octet-stream",
        "content": content,
        "note": note,
    }


@router.post("/extract-rules-from-mapping-file")
async def extract_rules_from_mapping_file(
    file: UploadFile = File(...),
    target_table: str = "",
    sql_text: str = "",
    agent_ids: str = "",
    task_hint: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Extract mapping rules from uploaded CSV/Excel file using AI and optional SQL comparison."""
    from app.services.drd_import_service import parse_drd_file
    from app.services.ai_service import ai_generate_mapping_rules_from_rows, ai_compare_mapping_with_sql

    allowed = ('.csv', '.xlsx', '.xls')
    if not any((file.filename or "").lower().endswith(ext) for ext in allowed):
        raise HTTPException(400, "File must be CSV or Excel format (.csv, .xlsx, .xls)")

    file_bytes = await _read_upload_checked(file)
    parse_result = parse_drd_file(
        file_bytes=file_bytes,
        filename=file.filename or "mapping.csv",
        selected_fields=[
            "logical_name", "physical_name", "source_schema", "source_table",
            "source_attribute", "transformation", "notes",
        ],
        target_schema="",
        target_table=target_table,
        source_datasource_id=1,
        target_datasource_id=1,
    )
    
    rows = parse_result.get("column_mappings", [])
    if not rows:
        return {
            "error": "No mapping rows parsed from file",
            "errors": parse_result.get("errors", []),
        }
    
    # Count direct/complex mappings
    direct_count = sum(1 for r in rows if not any(k in (r.get("transformation") or "").lower() for k in ["join", "lookup", "case"]))
    complex_count = len(rows) - direct_count
    
    # Use AI to extract/validate mapping rules
    ai_rules_resp = await ai_generate_mapping_rules_from_rows(
        mapping_rows=rows,
        target_schema="",
        target_table=target_table,
        source_datasource_id=1,
        target_datasource_id=1,
        provider_override="githubcopilot",
    )
    
    rules = ai_rules_resp.get("rules", []) if isinstance(ai_rules_resp, dict) else []
    comparison = None
    
    # If SQL provided, run comparison
    if sql_text.strip():
        parsed_agent_ids = []
        for raw in (agent_ids or "").split(","):
            raw = raw.strip()
            if raw.isdigit():
                parsed_agent_ids.append(int(raw))
        agent_prompt = await _resolve_agent_prompt(db, parsed_agent_ids, task_hint or "mapping vs SQL comparison")
        
        comparison = await ai_compare_mapping_with_sql(
            mapping_rows=rows,
            sql_text=sql_text,
            source_table="",
            target_table=target_table,
            single_db_testing=True,
            cross_db_optional=True,
            agent_system_prompt=agent_prompt,
            provider_override="githubcopilot",
        )
    
    return {
        "status": "success",
        "rules": rules or [],
        "direct_count": direct_count,
        "complex_count": complex_count,
        "total_rows_parsed": len(rows),
        "comparison": comparison,
        "errors": parse_result.get("errors", []),
    }


@router.post("/generate-tests-from-mapping-file")
async def generate_tests_from_mapping_file(
    file: UploadFile = File(...),
    target_table: str = "",
    generation_mode: str = "sample",
    sample_per_category: int = 2,
    agent_ids: str = "",
    task_hint: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Generate test cases from uploaded mapping file using AI analysis."""
    from app.services.drd_import_service import parse_drd_file, generate_drd_tests

    allowed = ('.csv', '.xlsx', '.xls')
    if not any((file.filename or "").lower().endswith(ext) for ext in allowed):
        raise HTTPException(400, "File must be CSV or Excel format (.csv, .xlsx, .xls)")

    file_bytes = await _read_upload_checked(file)
    parse_result = parse_drd_file(
        file_bytes=file_bytes,
        filename=file.filename or "mapping.csv",
        selected_fields=[
            "logical_name", "physical_name", "source_schema", "source_table",
            "source_attribute", "transformation", "notes",
        ],
        target_schema="",
        target_table=target_table,
        source_datasource_id=1,
        target_datasource_id=1,
    )
    
    rows = parse_result.get("column_mappings", [])
    if not rows:
        return {
            "error": "No mapping rows parsed from file",
            "errors": parse_result.get("errors", []),
        }
    
    # Generate baseline test definitions from local generator KB
    baseline_test_defs = generate_drd_tests(
        column_mappings=rows,
        target_schema="",
        target_table=target_table,
        source_datasource_id=1,
        target_datasource_id=1,
    )

    # Refine/augment with GitHub Copilot AI while keeping KB baseline as grounding context
    parsed_agent_ids = []
    for raw in (agent_ids or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            parsed_agent_ids.append(int(raw))
    agent_prompt = await _resolve_agent_prompt(db, parsed_agent_ids, task_hint or "mapping file test generation")
    ai_tests_resp = await ai_generate_tests_from_mapping_with_kb(
        mapping_rows=rows,
        kb_tests=baseline_test_defs,
        target_table=target_table,
        agent_system_prompt=agent_prompt,
        provider_override="githubcopilot",
    )
    test_defs = ai_tests_resp.get("tests") if isinstance(ai_tests_resp, dict) else None
    if not isinstance(test_defs, list) or not test_defs:
        test_defs = baseline_test_defs
    
    # Sample if requested
    if generation_mode == "sample":
        row_counts = [t for t in test_defs if t.get("test_type") == "row_count"]
        direct = [t for t in test_defs if _test_category(t) == "direct"]
        complex_tests = [t for t in test_defs if _test_category(t) == "complex"]
        
        selected = []
        if row_counts:
            selected.append(row_counts[0])
        selected.extend(direct[:sample_per_category])
        selected.extend(complex_tests[:sample_per_category])
        test_defs = selected
    
    category_counts = {
        "row_count": len([t for t in test_defs if t.get("test_type") == "row_count"]),
        "direct": len([t for t in test_defs if _test_category(t) == "direct"]),
        "complex": len([t for t in test_defs if _test_category(t) == "complex"]),
    }
    
    return {
        "status": "success",
        "tests": test_defs,
        "category_counts": category_counts,
        "generation_mode": generation_mode,
        "total_available": len(test_defs),
        "ai_provider": "githubcopilot",
        "baseline_total": len(baseline_test_defs),
        "errors": parse_result.get("errors", []),
    }
