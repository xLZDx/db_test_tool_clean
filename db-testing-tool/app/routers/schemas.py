"""Schema analysis endpoints."""
import io
import csv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.database import async_session
from app.services.schema_service import analyze_datasource, get_schema_tree, compare_schemas
from app.services.schema_kb_service import build_and_save_schema_kb
from app.services.operation_control import (
    register_operation,
    request_stop,
    get_operation,
    mark_completed,
    mark_stopped,
    mark_failed,
    add_notification,
)
from app.services.schema_task_queue import enqueue_schema_task, get_queue_depth, get_queue_health
from app.models.datasource import DataSource
from app.connectors.factory import get_connector_from_model
from app.config import BASE_DIR
from pydantic import BaseModel
from typing import Optional, List
import uuid

router = APIRouter(prefix="/api/schemas", tags=["schemas"])


class AnalyzeRequest(BaseModel):
    datasource_id: int
    schema_filter: Optional[str] = None
    schema_filters: Optional[List[str]] = None
    save_to_kb: bool = False
    operation_id: Optional[str] = None
    background: bool = False


class PdmRequest(BaseModel):
    datasource_id: int
    schemas: Optional[List[str]] = None
    save_to_kb: bool = True
    operation_id: Optional[str] = None
    background: bool = False


class CompareRequest(BaseModel):
    source_datasource_id: int
    source_schema: str
    source_table: str
    target_datasource_id: int
    target_schema: str
    target_table: str


@router.post("/analyze")
async def analyze(body: AnalyzeRequest, db: AsyncSession = Depends(get_db)):
    op_id = body.operation_id or f"analyze_{uuid.uuid4().hex[:10]}"
    body.operation_id = op_id
    register_operation(op_id, "analyze")
    if body.background:
        depth = await enqueue_schema_task(
            op_id,
            "analyze",
            lambda: _run_analyze_job(
                body.datasource_id,
                body.schema_filter,
                body.schema_filters,
                op_id,
            ),
        )
        return {
            "status": "accepted",
            "operation_id": op_id,
            "queue_depth": depth,
        }
    try:
        return await _run_analyze_inline(db, body)
    except RuntimeError as e:
        if "stopped" in str(e).lower():
            mark_stopped(body.operation_id, "Analyze stopped")
            return {"status": "stopped", "message": str(e)}
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))
    except Exception as e:
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))


@router.get("/hint-tables/{datasource_id}")
async def hint_tables(datasource_id: int):
    """Return {OWNER.TABLE: [COL,...]} from the small hint index — instant read."""
    from app.services.schema_kb_service import (
        _hint_index_path, _load_hint_index, _kb_json_path, _build_hint_index_from_kb
    )
    import threading

    # Fast path: small index file exists
    tables = await _run_blocking(_load_hint_index, datasource_id)
    if tables:
        return {"tables": tables, "count": len(tables), "loading": False}

    # No index yet — does the full KB exist?
    kb_exists = await _run_blocking(lambda: _kb_json_path(datasource_id).exists())
    if not kb_exists:
        return {"tables": {}, "count": 0, "loading": False}

    # KB exists but no index — build it in a background thread (non-blocking for request)
    def _bg_build():
        try:
            _build_hint_index_from_kb(datasource_id)
        except Exception:
            pass

    t = threading.Thread(target=_bg_build, daemon=True)
    t.start()
    return {"tables": {}, "count": 0, "loading": True}


@router.get("/catalog/{datasource_id}")
async def schema_catalog(datasource_id: int, db: AsyncSession = Depends(get_db)):
    ds = await db.get(DataSource, datasource_id)
    if not ds:
        raise HTTPException(404, f"DataSource {datasource_id} not found")

    connector = get_connector_from_model(ds)
    if connector is None:
        raise HTTPException(422, f"Unsupported datasource type: {ds.db_type}")
    try:
        connector.connect()
        schemas = connector.get_schemas()
        return {
            "datasource_id": datasource_id,
            "schemas": schemas,
            "count": len(schemas),
        }
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        connector.disconnect()


@router.post("/pdm")
async def generate_pdm(body: PdmRequest, db: AsyncSession = Depends(get_db)):
    op_id = body.operation_id or f"pdm_{uuid.uuid4().hex[:10]}"
    body.operation_id = op_id
    register_operation(op_id, "pdm")
    if body.background:
        depth = await enqueue_schema_task(
            op_id,
            "pdm",
            lambda: _run_pdm_job(body.datasource_id, body.schemas, body.save_to_kb, op_id),
        )
        return {
            "status": "accepted",
            "operation_id": op_id,
            "queue_depth": depth,
        }
    try:
        return await _run_pdm_inline(db, body)
    except RuntimeError as e:
        if "stopped" in str(e).lower():
            mark_stopped(body.operation_id, "PDM generation stopped")
            return {"status": "stopped", "message": str(e)}
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))
    except Exception as e:
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))


@router.post("/kb/save")
async def save_schema_kb(body: PdmRequest, db: AsyncSession = Depends(get_db)):
    op_id = body.operation_id or f"kb_{uuid.uuid4().hex[:10]}"
    body.operation_id = op_id
    register_operation(op_id, "kb-save")
    if body.background:
        depth = await enqueue_schema_task(
            op_id,
            "kb-save",
            lambda: _run_kb_save_job(body.datasource_id, body.schemas, op_id),
        )
        return {
            "status": "accepted",
            "operation_id": op_id,
            "queue_depth": depth,
        }
    try:
        return await _run_kb_save_inline(db, body)
    except RuntimeError as e:
        if "stopped" in str(e).lower():
            mark_stopped(body.operation_id, "Local KB save stopped")
            return {"status": "stopped", "message": str(e)}
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))
    except Exception as e:
        mark_failed(body.operation_id, str(e))
        raise HTTPException(500, str(e))


@router.get("/operation/{operation_id}")
async def operation_status(operation_id: str):
    state = get_operation(operation_id)
    if not state:
        raise HTTPException(404, f"Operation {operation_id} not found")
    return state


@router.post("/stop/{operation_id}")
async def stop_operation(operation_id: str):
    request_stop(operation_id)
    return {"status": "ok", "message": f"Stop requested for {operation_id}"}


@router.get("/tree/{datasource_id}")
async def schema_tree(datasource_id: int, db: AsyncSession = Depends(get_db)):
    tree = await get_schema_tree(db, datasource_id)
    return tree


@router.post("/compare")
async def compare(body: CompareRequest, db: AsyncSession = Depends(get_db)):
    result = await compare_schemas(
        db,
        body.source_datasource_id, body.source_schema, body.source_table,
        body.target_datasource_id, body.target_schema, body.target_table,
    )
    return result


@router.get("/pdm-pdf/{datasource_id}")
async def pdm_pdf(
    datasource_id: int,
    schemas: str = "",
    db: AsyncSession = Depends(get_db),
):
    """Generate a PDF of the PDM (Physical Data Model) for a datasource.

    Uses the locally saved KB JSON. If it doesn't exist, returns 404.
    Pass ?schemas=SCHEMA1,SCHEMA2 to filter.
    """
    from app.services.schema_kb_service import _load_existing_payload

    payload = await _run_blocking(_load_existing_payload, datasource_id)
    if not payload:
        raise HTTPException(404, "No saved PDM for this datasource. Run 'Create PDM' / 'Save to Local KB' first.")

    pdm_payload = payload.get("pdm") if isinstance(payload, dict) else None
    if not isinstance(pdm_payload, dict) or not pdm_payload:
        raise HTTPException(404, "Saved KB does not contain a valid PDM payload. Regenerate PDM first.")

    # Optional schema filter
    schema_filter = {s.strip().upper() for s in schemas.split(",") if s.strip()} if schemas else set()

    ds_info = pdm_payload.get("datasource", {})
    generated_at = pdm_payload.get("generated_at", payload.get("generated_at", ""))

    try:
        pdf_bytes = _build_pdm_pdf(pdm_payload, schema_filter, ds_info, generated_at)
    except Exception as exc:
        raise HTTPException(500, f"PDF generation failed: {exc}") from exc

    ds_name = (ds_info.get("name") or str(datasource_id)).replace(" ", "_")
    filename = f"PDM_{ds_name}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _run_blocking(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)


@router.get("/saved-pdm/{datasource_id}")
async def get_saved_pdm(datasource_id: int):
    from app.services.schema_kb_service import _load_existing_payload

    payload = await _run_blocking(_load_existing_payload, datasource_id)
    if not payload:
        raise HTTPException(404, "No saved PDM for this datasource.")

    pdm = payload.get("pdm") if isinstance(payload, dict) else {}
    schemas = pdm.get("schemas", []) if isinstance(pdm, dict) else []
    stats = {
        "schemas": len(schemas),
        "tables": sum(len(s.get("tables", []) or []) for s in schemas),
        "columns": sum(
            len(t.get("columns", []) or [])
            for s in schemas
            for t in (s.get("tables", []) or [])
        ),
        "relationships": len((pdm.get("relationships", []) if isinstance(pdm, dict) else []) or []),
    }

    kb_dir = BASE_DIR / "data" / "local_kb"
    return {
        "status": "ok",
        "pdm": pdm,
        "ldm": payload.get("ldm", {}),
        "stats": stats,
        "paths": {
            "json_path": str(kb_dir / f"schema_kb_ds_{datasource_id}.json"),
            "markdown_path": str(kb_dir / f"schema_kb_ds_{datasource_id}.md"),
        },
    }


@router.get("/pdm-history")
async def list_pdm_history():
    from app.services.schema_kb_service import load_schema_kb_payload

    merged = await _run_blocking(load_schema_kb_payload)
    sources = merged.get("sources", []) if isinstance(merged, dict) else []
    rows = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        pdm = item.get("pdm") if isinstance(item.get("pdm"), dict) else {}
        ds = pdm.get("datasource") if isinstance(pdm.get("datasource"), dict) else {}
        schemas = pdm.get("schemas", []) if isinstance(pdm, dict) else []
        ds_id = item.get("datasource_id") or ds.get("id")
        if not ds_id:
            continue
        rows.append({
            "datasource_id": int(ds_id),
            "datasource_name": ds.get("name") or f"DS {ds_id}",
            "db_type": ds.get("db_type") or "unknown",
            "generated_at": item.get("generated_at") or pdm.get("generated_at") or "",
            "schemas": len(schemas),
            "tables": sum(len(s.get("tables", []) or []) for s in schemas),
            "columns": sum(
                len(t.get("columns", []) or [])
                for s in schemas
                for t in (s.get("tables", []) or [])
            ),
            "relationships": len((pdm.get("relationships", []) if isinstance(pdm, dict) else []) or []),
        })

    rows.sort(key=lambda r: ((r.get("db_type") or "").upper(), (r.get("datasource_name") or "").upper()))
    grouped = {}
    for row in rows:
        key = (row.get("db_type") or "unknown").upper()
        grouped.setdefault(key, []).append(row)

    return {
        "status": "ok",
        "count": len(rows),
        "items": rows,
        "grouped": grouped,
    }


@router.get("/pdm-csv/{datasource_id}")
async def pdm_csv(datasource_id: int, schemas: str = ""):
    from app.services.schema_kb_service import _load_existing_payload

    payload = await _run_blocking(_load_existing_payload, datasource_id)
    if not payload:
        raise HTTPException(404, "No saved PDM for this datasource. Run 'Create PDM' / 'Save to Local KB' first.")

    pdm_payload = payload.get("pdm") if isinstance(payload, dict) else None
    if not isinstance(pdm_payload, dict) or not pdm_payload:
        raise HTTPException(404, "Saved KB does not contain a valid PDM payload. Regenerate PDM first.")

    schema_filter = {s.strip().upper() for s in schemas.split(",") if s.strip()} if schemas else set()
    ds_info = pdm_payload.get("datasource", {}) if isinstance(pdm_payload.get("datasource"), dict) else {}

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "datasource_id",
        "datasource_name",
        "db_type",
        "schema",
        "table",
        "table_type",
        "column",
        "data_type",
        "nullable",
        "is_pk",
    ])

    for schema_block in pdm_payload.get("schemas", []) or []:
        schema_name = schema_block.get("schema") or ""
        if schema_filter and schema_name.upper() not in schema_filter:
            continue
        for table in schema_block.get("tables", []) or []:
            table_name = table.get("name") or ""
            table_type = table.get("type") or ""
            for col in table.get("columns", []) or []:
                writer.writerow([
                    datasource_id,
                    ds_info.get("name") or "",
                    ds_info.get("db_type") or "",
                    schema_name,
                    table_name,
                    table_type,
                    col.get("name") or "",
                    col.get("data_type") or "",
                    "Y" if col.get("nullable", True) else "N",
                    "Y" if col.get("is_pk", False) else "N",
                ])

    ds_name = (ds_info.get("name") or str(datasource_id)).replace(" ", "_")
    filename = f"PDM_{ds_name}.csv"
    return StreamingResponse(
        iter([buffer.getvalue().encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/queue/status")
async def schema_queue_status():
    health = get_queue_health()
    return {
        "status": "ok",
        "queue_depth": get_queue_depth(),
        "worker_count": health.get("worker_count", 0),
        "active_workers": health.get("active_workers", 0),
        "active_operation_ids": health.get("active_operation_ids", []),
        "workers_started": bool(health.get("workers_started", False)),
    }


@router.post("/scan-owner")
async def scan_owner_schemas(body: AnalyzeRequest):
    """Start background scan for schemas ending with _OWNER and save PDM/KB including view SQL for training."""
    op_id = body.operation_id or f"scan_owner_{uuid.uuid4().hex[:10]}"
    register_operation(op_id, "scan_owner")
    depth = await enqueue_schema_task(
        op_id,
        "scan_owner",
        lambda: _run_scan_owner_job(body.datasource_id, op_id),
    )
    return {"status": "accepted", "operation_id": op_id, "queue_depth": depth}


async def _run_analyze_inline(db: AsyncSession, body: AnalyzeRequest):
    add_notification(body.operation_id, "Analyze started")
    stats = await analyze_datasource(
        db,
        body.datasource_id,
        body.schema_filter,
        body.schema_filters,
        body.operation_id,
    )
    ds = await db.get(DataSource, body.datasource_id)
    if not ds:
        raise ValueError(f"DataSource {body.datasource_id} not found")
    kb_payload = await _run_blocking(build_and_save_schema_kb, ds, body.schema_filters, body.operation_id)
    kb_info = {
        "stats": kb_payload.get("stats", {}),
        "paths": kb_payload.get("paths", {}),
        "auto_saved": True,
    }
    mark_completed(body.operation_id, "Analyze finished")
    return {"status": "ok", **stats, "kb": kb_info}


async def _run_analyze_job(
    datasource_id: int,
    schema_filter: Optional[str],
    schema_filters: Optional[List[str]],
    operation_id: Optional[str],
):
    async with async_session() as db:
        add_notification(operation_id, "Background analyze started")
        ds = await db.get(DataSource, datasource_id)
        if not ds:
            raise ValueError(f"DataSource {datasource_id} not found")

        selected = schema_filters
        if not selected and schema_filter:
            selected = [schema_filter]

        payload = await _run_blocking(build_and_save_schema_kb, ds, selected, operation_id)
        stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
        add_notification(
            operation_id,
            f"Analyze summary: schemas={stats.get('schemas', 0)}, tables={stats.get('tables', 0)}, columns={stats.get('columns', 0)}",
        )
        mark_completed(operation_id, "Background analyze finished")


async def _run_pdm_inline(db: AsyncSession, body: PdmRequest):
    add_notification(body.operation_id, "PDM generation started")
    ds = await db.get(DataSource, body.datasource_id)
    if not ds:
        raise ValueError(f"DataSource {body.datasource_id} not found")

    payload = await _run_blocking(build_and_save_schema_kb, ds, body.schemas, body.operation_id)
    if not body.save_to_kb:
        payload["paths"] = {}
    mark_completed(body.operation_id, "PDM generation finished")
    return {"status": "ok", **payload}


async def _run_pdm_job(datasource_id: int, schemas: Optional[List[str]], save_to_kb: bool, operation_id: Optional[str]):
    async with async_session() as db:
        body = PdmRequest(
            datasource_id=datasource_id,
            schemas=schemas,
            save_to_kb=save_to_kb,
            operation_id=operation_id,
            background=False,
        )
        await _run_pdm_inline(db, body)


async def _run_kb_save_inline(db: AsyncSession, body: PdmRequest):
    add_notification(body.operation_id, "Local KB save started")
    ds = await db.get(DataSource, body.datasource_id)
    if not ds:
        raise ValueError(f"DataSource {body.datasource_id} not found")

    payload = await _run_blocking(build_and_save_schema_kb, ds, body.schemas, body.operation_id)
    mark_completed(body.operation_id, "Local KB save finished")
    return {
        "status": "ok",
        "message": "Local DB knowledge base updated",
        "stats": payload.get("stats", {}),
        "paths": payload.get("paths", {}),
    }


async def _run_kb_save_job(datasource_id: int, schemas: Optional[List[str]], operation_id: Optional[str]):
    async with async_session() as db:
        body = PdmRequest(
            datasource_id=datasource_id,
            schemas=schemas,
            save_to_kb=True,
            operation_id=operation_id,
            background=False,
        )
        await _run_kb_save_inline(db, body)


async def _run_scan_owner_job(datasource_id: int, operation_id: str):
    try:
        add_notification(operation_id, "_OWNER schema scan started")
        async with async_session() as db:
            ds = await db.get(DataSource, datasource_id)
        if not ds:
            raise ValueError(f"DataSource {datasource_id} not found")

        owner_schemas = await _run_blocking(_fetch_owner_schemas, ds)

        if not owner_schemas:
            add_notification(operation_id, "No schemas ending with _OWNER found")
            mark_completed(operation_id, "No _OWNER schemas found")
            return

        payload = await _run_blocking(build_and_save_schema_kb, ds, owner_schemas, operation_id)
        pdm = payload.get("pdm", {})
        tables = 0
        views = 0
        indexes = 0
        constraints = 0
        for s in pdm.get("schemas", []) or []:
            for t in s.get("tables", []) or []:
                ttype = (t.get("type") or "").upper()
                if "VIEW" in ttype:
                    views += 1
                else:
                    tables += 1
                indexes += len(t.get("indexes", []) or [])
                constraints += len(t.get("constraints", []) or [])

        add_notification(
            operation_id,
            f"Scan summary: schemas={len(owner_schemas)}, tables={tables}, views={views}, indexes={indexes}, constraints={constraints}",
        )
        mark_completed(operation_id, "_OWNER schema scan finished")
    except RuntimeError as e:
        if "stopped" in str(e).lower():
            mark_stopped(operation_id, "_OWNER schema scan stopped")
            return
        mark_failed(operation_id, str(e))
    except Exception as e:
        mark_failed(operation_id, str(e))


def _fetch_owner_schemas(ds: DataSource) -> List[str]:
    connector = get_connector_from_model(ds)
    if connector is None:
        raise RuntimeError(f"Unsupported datasource type: {ds.db_type}")
    try:
        connector.connect()
        all_schemas = connector.get_schemas()
        return [s for s in all_schemas if (s or "").upper().endswith("_OWNER")]
    finally:
        connector.disconnect()


def _build_pdm_pdf(payload: dict, schema_filter: set, ds_info: dict, generated_at: str) -> bytes:
    """Build a formatted PDF for the PDM payload using reportlab."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
        )
        from reportlab.lib.enums import TA_LEFT, TA_CENTER
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing PDF dependency 'reportlab'. Install workspace dependencies from db-testing-tool/requirements.txt."
        ) from exc

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=f"PDM – {ds_info.get('name', '')}",
    )

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=6, textColor=colors.HexColor("#1e3a5f"))
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13, spaceAfter=4, textColor=colors.HexColor("#1e3a5f"))
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=10, spaceAfter=3, textColor=colors.HexColor("#334155"))
    normal = ParagraphStyle("normal", parent=styles["Normal"], fontSize=8, leading=11)
    meta = ParagraphStyle("meta", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#64748b"))
    code_style = ParagraphStyle("code_style", parent=styles["Code"], fontSize=7, leading=10,
                                fontName="Courier", backColor=colors.HexColor("#f8fafc"))

    col_header_bg = colors.HexColor("#1e3a5f")
    col_alt_row = colors.HexColor("#f1f5f9")
    col_border = colors.HexColor("#cbd5e1")

    story = []

    # Cover / title
    story.append(Paragraph(f"Physical Data Model", h1))
    story.append(Paragraph(f"Datasource: <b>{ds_info.get('name', '')}</b>  |  Host: {ds_info.get('host', '')}  |  DB: {ds_info.get('database', ds_info.get('db_type', ''))}", meta))
    story.append(Paragraph(f"Generated: {generated_at}", meta))
    story.append(Spacer(1, 0.4 * cm))
    story.append(HRFlowable(width="100%", thickness=1, color=col_border))
    story.append(Spacer(1, 0.3 * cm))

    for schema_block in payload.get("schemas", []):
        sname = schema_block.get("schema", "")
        if schema_filter and sname.upper() not in schema_filter:
            continue
        tables = schema_block.get("tables", [])
        story.append(Paragraph(f"Schema: {sname}  ({len(tables)} objects)", h2))
        story.append(Spacer(1, 0.15 * cm))

        for tbl in tables:
            tname = tbl.get("name", "")
            ttype = tbl.get("type", "TABLE")
            columns = tbl.get("columns", [])
            pks = set(tbl.get("primary_keys", []))
            fks = {fk.get("column"): fk for fk in tbl.get("foreign_keys", [])}
            story.append(Paragraph(f"{tname}  <font color='#64748b' size='8'>({ttype}, {len(columns)} cols)</font>", h3))

            if columns:
                # Table: Name | Type | Nullable | PK/FK | Comments
                col_widths = [5 * cm, 4.5 * cm, 1.5 * cm, 1.8 * cm, 11 * cm]
                header = [
                    Paragraph("<b>Column</b>", ParagraphStyle("th", parent=normal, textColor=colors.white, fontName="Helvetica-Bold")),
                    Paragraph("<b>Type</b>", ParagraphStyle("th", parent=normal, textColor=colors.white, fontName="Helvetica-Bold")),
                    Paragraph("<b>Null</b>", ParagraphStyle("th", parent=normal, textColor=colors.white, fontName="Helvetica-Bold")),
                    Paragraph("<b>Keys</b>", ParagraphStyle("th", parent=normal, textColor=colors.white, fontName="Helvetica-Bold")),
                    Paragraph("<b>Comments</b>", ParagraphStyle("th", parent=normal, textColor=colors.white, fontName="Helvetica-Bold")),
                ]
                data = [header]
                for i, col in enumerate(columns):
                    cname = col.get("name") or ""
                    ctype = col.get("data_type") or ""
                    nullable = "Y" if col.get("nullable", True) else "N"
                    keys = []
                    if cname in pks:
                        keys.append("PK")
                    if cname in fks:
                        ref = fks[cname]
                        keys.append(f"FK→{ref.get('ref_table', '')}")
                    comments = col.get("comments") or col.get("description") or ""
                    bg = col_alt_row if i % 2 else colors.white
                    row = [
                        Paragraph(escapeHtmlReportlab(cname), normal),
                        Paragraph(escapeHtmlReportlab(ctype), normal),
                        Paragraph(nullable, normal),
                        Paragraph(", ".join(keys), normal),
                        Paragraph(escapeHtmlReportlab(str(comments)[:200]), normal),
                    ]
                    data.append(row)

                tbl_widget = Table(data, colWidths=col_widths, repeatRows=1)
                tbl_widget.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), col_header_bg),
                    ("GRID", (0, 0), (-1, -1), 0.4, col_border),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("PADDING", (0, 0), (-1, -1), 3),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, col_alt_row]),
                ]))
                story.append(tbl_widget)
            else:
                story.append(Paragraph("<i>No column information available.</i>", meta))
            story.append(Spacer(1, 0.3 * cm))

        story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()


def escapeHtmlReportlab(text: str) -> str:
    """Escape HTML special characters for reportlab Paragraph."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
