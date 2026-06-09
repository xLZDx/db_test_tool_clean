#!/usr/bin/env python3
"""Backend adapter: call v18.0 from your existing app without making the tool a second source of truth."""
from __future__ import annotations
import json, subprocess, sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

@dataclass
class FullCycleRequest:
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
    quiet: bool = True
    fail_on_business_status: str = ""

@dataclass
class FullCycleResponse:
    process_status: str
    business_status: dict
    manifest_path: str
    run_summary_path: str
    returncode: int
    stdout_tail: str
    stderr_tail: str

def run_full_cycle(request: FullCycleRequest, tool_root: Optional[str] = None, timeout_sec: int = 3600) -> FullCycleResponse:
    root = Path(tool_root).resolve() if tool_root else Path(__file__).resolve().parents[1]
    script = root / "full_cycle_drd_odi_insert.py"
    args = [sys.executable, "-B", str(script), "--out", request.out, "--report-mode", request.report_mode]
    for opt, val in [
        ("--xlsx", request.xlsx), ("--original-xml", request.original_xml), ("--fixed-xml", request.fixed_xml),
        ("--existing-compare-out", request.existing_compare_out), ("--existing-insert-out", request.existing_insert_out),
        ("--schema-kb", request.schema_kb), ("--profile", request.profile), ("--target-schema", request.target_schema),
        ("--target-table", request.target_table), ("--mapping-sheet", request.mapping_sheet), ("--target-col", request.target_col),
        ("--source-cols", request.source_cols), ("--rule-col", request.rule_col),
        ("--fail-on-business-status", request.fail_on_business_status),
    ]:
        if val:
            args += [opt, val]
    if request.quiet:
        args += ["--quiet"]
    proc = subprocess.run(args, cwd=str(root), text=True, capture_output=True, timeout=timeout_sec)
    out_path = Path(request.out).expanduser().resolve()
    manifest_path = out_path / "final_reports" / "api" / "manifest.json"
    run_summary_path = out_path / "full_cycle_run_summary.json"
    process_status = "FAILED_ENGINE" if proc.returncode not in (0, 3) else "ARTIFACTS_GENERATED"
    business_status = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        process_status = manifest.get("process_status", process_status)
        business_status = manifest.get("business_status", {})
    return FullCycleResponse(process_status, business_status, str(manifest_path) if manifest_path.exists() else "", str(run_summary_path) if run_summary_path.exists() else "", proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:])

def to_json_response(response: FullCycleResponse) -> dict:
    return asdict(response)
