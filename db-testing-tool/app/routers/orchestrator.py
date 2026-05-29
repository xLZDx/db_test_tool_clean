"""Unified orchestrator router.

Provides two endpoints:
  POST /api/orchestrator/99-match/run       — v9: DRD/XML 99% parity scoring
  POST /api/orchestrator/pdm-aware/generate — v10: PDM-cache enrichment + SQL generation

Both are non-blocking (heavy work is dispatched via asyncio.to_thread).
File size limits and config allowlists are enforced before any processing.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import DATA_DIR
from app.services.drd_import_service import parse_drd_file
from app.services.drd_pdm_enrichment_service import DRDPDMEnrichmentService
from app.services.orchestrator_99_service import run_99_orchestration
from app.services.schema_kb_service import _kb_dir
from app.services.semantic_alias_quality_gate_service import SemanticAliasQualityGateService
from app.services.statement_mode_generation_service import StatementModeGenerationService

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])

_DRD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_XML_MAX_BYTES = 5 * 1024 * 1024   # 5 MB

_DEFAULT_DRD_FIELDS = [
    "logical_name",
    "physical_name",
    "source_schema",
    "source_table",
    "source_attribute",
    "transformation",
    "notes",
    "target_datatype_oracle",
    "target_nullable_oracle",
]

# Keys the client is allowed to pass in config_json for the v10 endpoint.
# 'pdm_cache.local_kb_dir' is always injected server-side and is not overridable.
_V10_ALLOWED_CONFIG_KEYS = frozenset({"table", "sql_generation", "outputs"})

# Keys allowed for the v9 endpoint.
_V9_ALLOWED_CONFIG_KEYS = frozenset({"table", "match_policy", "outputs", "drd", "xml"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_config(raw: str, label: str) -> Dict[str, Any]:
    """Parse and return a config dict from a JSON string, raising 400 on error."""
    try:
        return json.loads(raw) if (raw or "").strip() else {}
    except Exception as exc:
        raise HTTPException(400, f"Invalid {label}: {exc}") from exc


def _filter_config(user_config: Dict[str, Any], allowed_keys) -> Dict[str, Any]:
    """Return only the keys in *allowed_keys* from user_config (shallow)."""
    return {k: v for k, v in user_config.items() if k in allowed_keys}


def _build_v10_config(
    user_config: Dict[str, Any],
    target_schema: str,
    target_table: str,
) -> Dict[str, Any]:
    """Build a v10 service config with safe KB dir injection.

    The ``pdm_cache.local_kb_dir`` is always set to the resolved absolute path
    from schema_kb_service — it is never accepted from user-supplied config.
    """
    config = _filter_config(user_config, _V10_ALLOWED_CONFIG_KEYS)
    # Inject absolute KB dir — user cannot override this
    config["pdm_cache"] = {"local_kb_dir": str(_kb_dir())}
    # Inject target table name from form params when not explicitly provided
    if target_schema or target_table:
        config.setdefault("table", {})
        config["table"].setdefault("name", f"{target_schema}.{target_table}")
    return config


def _adapt_drd_rows_for_v10(column_mappings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Bridge parse_drd_file field names to v10 service conventions.

    parse_drd_file uses 'physical_name' and 'target_datatype_oracle';
    the v10 PDM/SQL services expect 'column' and 'dtype'.
    """
    adapted = []
    for r in column_mappings:
        row = dict(r)
        row.setdefault("column", row.get("physical_name", ""))
        row.setdefault("dtype", row.get("target_datatype_oracle", ""))
        adapted.append(row)
    return adapted


def _run_pdm_pipeline(
    config: Dict[str, Any],
    rows: List[Dict[str, Any]],
    xml_bytes: Optional[bytes],
):
    """Run the full v10 PDM enrichment + SQL generation + quality gate pipeline.

    Designed to be executed inside asyncio.to_thread since all three steps
    involve either file I/O (cache load) or CPU-bound processing.

    Returns (enriched_rows, pdm_resolutions, cache_summary, generated, quality).
    """
    enricher = DRDPDMEnrichmentService(config)
    enriched_rows, pdm_resolutions, cache_summary = enricher.enrich_rows(rows)

    generator = StatementModeGenerationService(config)
    generated = generator.generate_all(enriched_rows)

    gate = SemanticAliasQualityGateService()
    quality = gate.evaluate(generated, xml_bytes, config)

    return enriched_rows, pdm_resolutions, cache_summary, generated, quality


# ---------------------------------------------------------------------------
# v9: 99% DRD/XML parity endpoint
# ---------------------------------------------------------------------------

@router.post("/99-match/run")
async def run_99_match(
    drd_file: UploadFile = File(...),
    xml_file: UploadFile = File(...),
    config_json: str = Form(""),
):
    """Score DRD column schema against an ODI scenario XML export for 99% parity.

    Accepts an optional ``config_json`` to override table name, DRD layout,
    XML encoding, column aliases, helper-column exclusions, and the pass
    threshold (default 99.0 %).
    """
    if not (drd_file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "drd_file must be .xlsx or .xls")
    if not (xml_file.filename or "").lower().endswith(".xml"):
        raise HTTPException(400, "xml_file must be an ODI .xml export")

    user_config = _parse_config(config_json, "config_json")

    drd_bytes = await drd_file.read()
    xml_bytes = await xml_file.read()

    if len(drd_bytes) > _DRD_MAX_BYTES:
        raise HTTPException(413, "DRD file exceeds 10 MB limit")
    if len(xml_bytes) > _XML_MAX_BYTES:
        raise HTTPException(413, "XML file exceeds 5 MB limit")

    try:
        return await asyncio.to_thread(run_99_orchestration, drd_bytes, xml_bytes, user_config)
    except Exception as exc:
        raise HTTPException(500, f"99% orchestration failed: {exc}") from exc


# ---------------------------------------------------------------------------
# v10: PDM-aware SQL generation endpoint
# ---------------------------------------------------------------------------

@router.post("/pdm-aware/generate")
async def pdm_aware_generate(
    drd_file: UploadFile = File(...),
    xml_file: Optional[UploadFile] = File(None),
    config_json: str = Form("{}"),
    target_schema: str = Form(""),
    target_table: str = Form(""),
    source_datasource_id: int = Form(0),
    target_datasource_id: int = Form(0),
    sheet_name: str = Form(""),
):
    """PDM-aware DRD SQL generation with optional XML quality gate.

    Flow:
      1. Parse DRD Excel via canonical parse_drd_file()
      2. Enrich rows against the local PDM KB cache (PDMCacheResolver)
      3. Generate all SQL statement modes (source_select, insert_select, cte, merge)
      4. Run optional XML quality gate when xml_file is supplied

    The ``pdm_cache.local_kb_dir`` is always resolved server-side; it cannot
    be overridden via config_json.
    """
    if not (drd_file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "drd_file must be .xlsx or .xls")

    user_config = _parse_config(config_json, "config_json")
    config = _build_v10_config(user_config, target_schema, target_table)

    drd_bytes = await drd_file.read()
    if len(drd_bytes) > _DRD_MAX_BYTES:
        raise HTTPException(413, "DRD file exceeds 10 MB limit")

    xml_bytes: Optional[bytes] = None
    if xml_file and (xml_file.filename or "").strip():
        if not xml_file.filename.lower().endswith(".xml"):
            raise HTTPException(400, "xml_file must be an ODI .xml export")
        xml_bytes = await xml_file.read()
        if len(xml_bytes) > _XML_MAX_BYTES:
            raise HTTPException(413, "XML file exceeds 5 MB limit")

    # Step 1: Parse DRD (blocking pandas/openpyxl I/O)
    try:
        parse_result = await asyncio.to_thread(
            parse_drd_file,
            file_bytes=drd_bytes,
            filename=drd_file.filename or "drd.xlsx",
            selected_fields=_DEFAULT_DRD_FIELDS,
            target_schema=target_schema,
            target_table=target_table,
            source_datasource_id=source_datasource_id or 0,
            target_datasource_id=target_datasource_id or 0,
            sheet_name=sheet_name.strip() or None,
        )
    except Exception as exc:
        raise HTTPException(422, f"DRD parse failed: {exc}") from exc

    column_mappings = parse_result.get("column_mappings", [])
    if not column_mappings:
        raise HTTPException(422, "No column mappings found in DRD file")

    rows = _adapt_drd_rows_for_v10(column_mappings)

    # Steps 2-4: PDM enrichment → SQL generation → quality gate (all in one thread)
    try:
        enriched_rows, pdm_resolutions, cache_summary, generated, quality = (
            await asyncio.to_thread(_run_pdm_pipeline, config, rows, xml_bytes)
        )
    except Exception as exc:
        raise HTTPException(500, f"PDM pipeline failed: {exc}") from exc

    plan = generated.get("plan", {})
    return {
        "status": quality.get("status"),
        "parse_result": {
            "total_rows": len(column_mappings),
            "errors": parse_result.get("errors", []),
        },
        "pdm_resolution": {
            "resolutions": pdm_resolutions,
            "cache_summary": cache_summary,
        },
        "sql": {
            "source_select": generated.get("source_select"),
            "insert_select": generated.get("insert_select"),
            "cte": generated.get("cte"),
            "merge": generated.get("merge"),
        },
        "plan": {
            "primary_source": plan.get("primary_pair"),
            "joins": plan.get("joins", []),
            "unresolved": generated.get("unresolved", []),
        },
        "quality_gate": quality,
    }
