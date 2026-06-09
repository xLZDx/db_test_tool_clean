#!/usr/bin/env python3
"""Optional FastAPI router scaffold for v18.0."""
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import json
from .service_adapter import FullCycleRequest, run_full_cycle, to_json_response

router = APIRouter(tags=["DRD/ODI/INSERT"])

class FullCycleRequestModel(BaseModel):
    out: str
    xlsx: str = ""
    original_xml: str = ""
    fixed_xml: str = ""
    existing_compare_out: str = ""
    existing_insert_out: str = ""
    schema_kb: str = ""
    profile: str = "auto"
    target_schema: str = ""
    target_table: str = ""
    mapping_sheet: str = ""
    target_col: str = ""
    source_cols: str = ""
    rule_col: str = ""
    report_mode: str = "api"
    fail_on_business_status: str = ""

@router.post("/full-cycle")
def full_cycle(req: FullCycleRequestModel):
    response = run_full_cycle(FullCycleRequest(**req.model_dump()))
    if response.returncode not in (0, 3):
        raise HTTPException(status_code=500, detail=to_json_response(response))
    return to_json_response(response)

@router.get("/reports/{run_id}/manifest")
def get_manifest(run_id: str, base_dir: str = "runs"):
    p = Path(base_dir) / run_id / "final_reports" / "api" / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"manifest not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
