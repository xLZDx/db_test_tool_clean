"""v10 orchestrator router skeleton.

This router shows the intended integration points. In the full db-testing-tool app,
connect DRD parsing to the existing DRD parser or the canonical extractor used by v9.
"""
from __future__ import annotations

import json
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services.drd_pdm_enrichment_service import DRDPDMEnrichmentService
from app.services.statement_mode_generation_service import StatementModeGenerationService
from app.services.semantic_alias_quality_gate_service import SemanticAliasQualityGateService

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])


@router.post("/pdm-aware/generate")
async def pdm_aware_generate(
    drd_file: UploadFile = File(...),
    xml_file: UploadFile | None = File(None),
    config_json: str = Form("{}"),
):
    try:
        config = json.loads(config_json or "{}")
    except Exception as exc:
        raise HTTPException(400, f"Invalid config_json: {exc}") from exc

    # Replace this placeholder with the app's canonical DRD parser.
    # The service expects rows with: column, dtype, source_schema, source_table,
    # source_attribute, logical_name, transformation.
    raise HTTPException(501, "Wire this endpoint to existing DRD parser, then call DRDPDMEnrichmentService + StatementModeGenerationService")
