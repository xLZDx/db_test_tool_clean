"""FastAPI application entry point."""
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse
from pathlib import Path
from app.database import init_db
from app.database import async_session
from app.config import settings
from app.services.datasource_bootstrap import sync_datasources_from_env
from app.services.schema_task_queue import ensure_schema_task_workers
from app.services.training_automation_service import restore_training_automation_loop
from app.services.session_watchdog import start_session_watchdog, stop_session_watchdog

# Import models to register with SQLAlchemy
import app.models

# Import routers
from app.routers import (
    datasources, credentials, schemas, tests, tfs, agents, ai,
    chat_assistant, external_tools, odi, regression_lab, system_watchdog, mappings,
    orchestrator,
)

BASE_DIR = Path(__file__).resolve().parent


def _build_static_version() -> str:
    static_dir = BASE_DIR / "static"
    candidates = [
        static_dir / "css" / "app.css",
        static_dir / "js" / "app.js",
    ]
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    if not mtimes:
        return settings.APP_VERSION
    return str(max(mtimes))


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION)

# Static files and templates
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["STATIC_VERSION"] = _build_static_version()

# Include API routers
app.include_router(datasources.router)
app.include_router(schemas.router)
app.include_router(tests.router)
app.include_router(ai.router)
app.include_router(tfs.router)
app.include_router(agents.router)
app.include_router(credentials.router)
app.include_router(external_tools.router)
app.include_router(odi.router)
app.include_router(chat_assistant.router)
app.include_router(regression_lab.router)
app.include_router(system_watchdog.router)
app.include_router(mappings.router)
app.include_router(orchestrator.router)


@app.on_event("startup")
async def startup():
    await init_db()
    await ensure_schema_task_workers()
    await sync_datasources_from_env()
    await restore_training_automation_loop()
    await start_session_watchdog()
    # Restore completed/failed/stopped operation states from disk (survives restarts)
    from app.services.operation_control import restore_persisted_operations
    import asyncio
    await asyncio.to_thread(restore_persisted_operations)
    # Build missing hint indices in background (non-blocking)
    import threading
    def _build_missing_hint_indices():
        try:
            from app.services.schema_kb_service import (
                _kb_dir, _kb_json_path, _hint_index_path, _build_hint_index_from_kb
            )
            kb_dir = _kb_dir()
            for kb_file in kb_dir.glob("schema_kb_ds_*.json"):
                try:
                    ds_id = int(kb_file.stem.replace("schema_kb_ds_", ""))
                    if not _hint_index_path(ds_id).exists():
                        _build_hint_index_from_kb(ds_id)
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_build_missing_hint_indices, daemon=True).start()


@app.on_event("shutdown")
async def shutdown():
    await stop_session_watchdog()


# ── Page routes (serve HTML) ───────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request, "page": "dashboard"})

@app.get("/datasources", response_class=HTMLResponse)
async def page_datasources(request: Request):
    resp = templates.TemplateResponse("datasources.html", {"request": request, "page": "datasources"})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.get("/schema-browser", response_class=HTMLResponse)
async def page_schema_browser(request: Request):
    return templates.TemplateResponse("schema_browser.html", {"request": request, "page": "schema-browser"})

@app.get("/mappings", response_class=HTMLResponse)
async def page_mappings(request: Request):
    return templates.TemplateResponse("mappings.html", {"request": request, "page": "qa-manager"})

@app.get("/tests", response_class=HTMLResponse)
async def page_tests(request: Request):
    return templates.TemplateResponse("tests.html", {"request": request, "page": "tests"})

@app.get("/runs", response_class=HTMLResponse)
async def page_runs(request: Request):
    return templates.TemplateResponse("runs.html", {"request": request, "page": "runs"})

@app.get("/ai-assistant", response_class=HTMLResponse)
async def page_ai(request: Request):
    return templates.TemplateResponse("ai.html", {"request": request, "page": "ai-assistant"})

@app.get("/chat-assistant", response_class=HTMLResponse)
async def page_chat_assistant(request: Request):
    return templates.TemplateResponse("chat_assistant.html", {"request": request, "page": "chat-assistant"})

@app.get("/training-studio", response_class=HTMLResponse)
async def page_training_studio(request: Request):
    return templates.TemplateResponse("training_studio.html", {"request": request, "page": "training-studio"})

@app.get("/tfs", response_class=HTMLResponse)
async def page_tfs(request: Request):
    return templates.TemplateResponse("tfs.html", {"request": request, "page": "tfs"})

@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request, "page": "settings"})

@app.get("/agents", response_class=HTMLResponse)
async def page_agents(request: Request):
    return templates.TemplateResponse("agents.html", {"request": request, "page": "agents"})

@app.get("/external-tools", response_class=HTMLResponse)
async def page_external_tools(request: Request):
    return templates.TemplateResponse("external_tools.html", {"request": request, "page": "external-tools"})

@app.get("/odi", response_class=HTMLResponse)
async def page_odi(request: Request):
    return templates.TemplateResponse("odi.html", {"request": request, "page": "odi"})


@app.get("/regression-lab", response_class=HTMLResponse)
async def page_regression_lab(request: Request):
    return templates.TemplateResponse("regression_lab.html", {"request": request, "page": "regression-lab"})


# ── Download endpoints ──────────────────────────────────────────────────────

@app.get("/download/mapping-rules-template")
async def download_mapping_template():
    """Download basic Excel template for mapping rules import."""
    from app.config import BASE_DIR as CONFIG_BASE_DIR
    template_path = CONFIG_BASE_DIR / "data" / "mapping_rules_template.xlsx"
    if not template_path.exists():
        # Generate if doesn't exist
        from app.services.generate_excel_template import create_sample_template
        create_sample_template()
    return FileResponse(
        path=str(template_path),
        filename="mapping_rules_template.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.get("/api/templates")
async def get_available_templates():
    """Get list of available mapping template types."""
    from app.services.template_manager import template_manager
    templates = template_manager.get_all_templates()
    return {
        "templates": [
            {
                "type": template_type.value,
                "name": template.name,
                "description": template.description,
                "columns": len(template.columns)
            }
            for template_type, template in templates.items()
        ]
    }


@app.get("/download/template/{template_type}")
async def download_template_by_type(template_type: str):
    """Download Excel template for specific template type."""
    from app.services.template_manager import TemplateType
    from app.services.excel_template_generator import ExcelTemplateGenerator
    from app.config import BASE_DIR
    from fastapi import HTTPException
    
    try:
        # Validate template type
        template_enum = TemplateType(template_type.lower())
        
        # Check if template exists, generate if not
        template_path = BASE_DIR / "data" / f"mapping_template_{template_type.lower()}.xlsx"
        if not template_path.exists():
            generator = ExcelTemplateGenerator()
            template_path = generator.generate_template(template_enum)
        
        return FileResponse(
            path=str(template_path),
            filename=f"mapping_template_{template_type.lower()}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
    except ValueError:
        raise HTTPException(400, f"Invalid template type: {template_type}")
    except Exception as e:
        raise HTTPException(500, f"Error generating template: {str(e)}")
