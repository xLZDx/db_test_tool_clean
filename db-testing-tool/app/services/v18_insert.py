#!/usr/bin/env python3
"""App-native, hardened wrapper around the vendored v18.0 insert builder.

Gate V1 (2026-06-09): the control-table generator must emit the KB-resolved
v18 INSERT (not the v5.4 heuristic that leaked prose joins / wrong owners).

This module deliberately does NOT use the v18 scaffold ``service_adapter.py`` /
``fastapi_router.py`` (those take caller-supplied filesystem paths, have no auth,
leak subprocess stderr into HTTP 500s, and run a 3600s blocking subprocess on the
event loop -- all flagged in the V1 review). Instead it exposes one pure-sync
function the FastAPI layer calls via ``asyncio.to_thread`` with server-controlled
paths only.

Pinned canonical v18 tree (git-tracked). The stray copies under
``temp files/New folder/v18_unzipped/`` and ``temp files/OLD/`` are NOT used.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Single pinned location for the v18 tool (review: pin ONE copy; fail loud if absent).
V18_TOOL_ROOT = (
    _REPO_ROOT
    / "temp files"
    / "New folder"
    / "drd_odi_insert_universal_tool_v18_0"
    / "drd_odi_insert_universal_tool_v18_0"
)
_INSERT_SCRIPT = V18_TOOL_ROOT / "insert_builder" / "universal_insert_builder.py"
_RESOLUTION_PROFILE = V18_TOOL_ROOT / "insert_builder" / "profiles" / "lh_ds3_resolution_profile.json"

# Default schema KB. ds_1 and ds_3 are equivalent for KBLookup, but the v18
# resolution profile is "lh_ds3", so the matching KB is ds_3.
_DEFAULT_SCHEMA_KB = _REPO_ROOT / "data" / "local_kb" / "schema_kb_ds_3.json"

# Audit/system columns are legitimately populated by ETL/triggers, not by the
# DRD mapping; a NULL there is acceptable. Business columns emitted as NULL are
# the real "stub" the operator rejects (classified, not hidden -- see Gate V2).
_AUDIT_STUB_COLUMNS = frozenset({
    "CRT_DTM", "CRT_USR_NM", "LAST_UDT_DTM", "LAST_UDT_USR_NM",
    "CREATE_DTM", "CREATE_USER", "UPDATE_DTM", "UPDATE_USER",
    "LOAD_DTM", "ETL_BATCH_ID",
})

_DEFAULT_TIMEOUT_S = 120


class V18BuildError(RuntimeError):
    """Raised when the v18 builder cannot produce a valid INSERT.

    The message is ASCII-only and never contains a server filesystem path, so it
    is safe to surface (the FastAPI layer maps it to HTTP 422).
    """


def build_v18_insert_to_dir(
    drd_path: Path,
    out_dir: Path,
    *,
    target_schema: str,
    target_table: str,
    profile: str = "auto",
    schema_kb: Optional[Path] = None,
    control_schema: Optional[str] = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Run the vendored v18 insert builder and return its result.

    All paths are server-controlled by the caller; nothing here comes from an
    HTTP body except ``target_schema`` / ``target_table`` / ``profile`` (plain
    identifiers passed as argv, never shell-interpolated).

    ``control_schema`` (optional): when given, the generated INSERT is retargeted
    to ``<control_schema>.<table>`` (the user's own control table). Driven by the
    same config as the rest of the control-table flow; NOT hardcoded.

    Returns a dict with: engine, generated_sql, returncode, stub_columns,
    stub_count, business_stub_columns, audit_stub_columns, hardcode_gate,
    hardcode_gate_failed, target (effective), production_target, control_schema.

    Raises V18BuildError if the tool is missing, times out, fails to start, or
    produces no INSERT statement (fail-loud -- never returns a stub silently).
    """
    if not _INSERT_SCRIPT.exists():
        raise V18BuildError(
            "v18 insert builder is not available in this deployment "
            "(expected under temp files/New folder/drd_odi_insert_universal_tool_v18_0)."
        )
    if not str(target_table).strip():
        raise V18BuildError("v18 build requires a target table.")
    if not str(target_schema).strip():
        # v18 emits INSERT INTO schema.table; without a schema the SQL gate
        # rejects it. Fail loud with guidance rather than producing junk.
        raise V18BuildError(
            "v18 build requires a target schema (owner) so it can emit a "
            "schema-qualified INSERT INTO owner.table."
        )

    kb = Path(schema_kb) if schema_kb else _DEFAULT_SCHEMA_KB
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd: List[str] = [
        sys.executable, "-B", str(_INSERT_SCRIPT),
        "--xlsx", str(drd_path),
        "--out", str(out_dir),
        "--profile", (profile or "auto"),
        "--target-schema", str(target_schema).strip(),
        "--target-table", str(target_table).strip(),
        "--schema-kb", str(kb),
    ]
    if _RESOLUTION_PROFILE.exists():
        cmd += ["--resolution-profile", str(_RESOLUTION_PROFILE)]
    cmd += ["--quiet"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(_INSERT_SCRIPT.parent),  # so sibling imports (profile_engine_renderer) resolve
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise V18BuildError(f"v18 build timed out after {timeout_s}s.") from exc
    except (FileNotFoundError, OSError) as exc:
        raise V18BuildError(f"v18 build could not start: {type(exc).__name__}.") from exc

    gen = out_dir / "generated_insert_select_candidate.sql"
    sql = gen.read_text(encoding="utf-8") if gen.exists() else ""
    if "INSERT INTO" not in sql.upper():
        # Fail loud (matches build-v54 contract). Do NOT leak the temp path.
        raise V18BuildError(
            "v18 builder produced no INSERT (the DRD layout/target could not be "
            "resolved; check sheet/header/columns, --profile, or the target owner)."
        )

    # Optional control-schema retarget. The same config the rest of the
    # control-table flow uses (request `control_schema`, settings "Default
    # Control Schema") -- NOT hardcoded. v18 emits the production owner; when a
    # control schema is given we retarget INSERT INTO <owner>.<table> ->
    # <control_schema>.<table> so the row lands in the user's own control table
    # (where they hold full privileges; no GRANT/DBA needed). The SELECT side is
    # untouched. Empty/None => keep the production target (back-compat).
    production_target = f"{str(target_schema).strip()}.{str(target_table).strip()}"
    effective_target = production_target
    cs = (control_schema or "").strip()
    if cs:
        sql = re.sub(
            r"(INSERT\s+INTO\s+)[A-Za-z0-9_$#]+(\s*\.\s*[A-Za-z0-9_$#]+)",
            lambda m: m.group(1) + cs + m.group(2),
            sql,
            count=1,
            flags=re.I,
        )
        effective_target = f"{cs}.{str(target_table).strip()}"

    gate_report: Dict[str, Any] = {}
    gate_path = out_dir / "hardcode_gate_report.json"
    if gate_path.exists():
        try:
            gate_report = json.loads(gate_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            gate_report = {"_parse_error": True}

    # NULL-stub extraction + classification (Gate V2 acts on business stubs).
    all_stubs = [c.upper() for c in re.findall(r"NULL\s+AS\s+([A-Z0-9_$#]+)", sql, re.I)]
    audit_stubs = [c for c in all_stubs if c in _AUDIT_STUB_COLUMNS]
    business_stubs = [c for c in all_stubs if c not in _AUDIT_STUB_COLUMNS]

    # rc != 0 with a real INSERT present == the v18 hardcode gate flagged the
    # package (code-quality), not a bad SQL. Surface it; do not fail the build.
    hardcode_gate_failed = bool(proc.returncode != 0 and sql)

    return {
        "engine": "v18-insert-builder",
        "generated_sql": sql,
        "returncode": proc.returncode,
        "stub_columns": all_stubs,
        "stub_count": len(all_stubs),
        "business_stub_columns": business_stubs,
        "audit_stub_columns": audit_stubs,
        "hardcode_gate": gate_report,
        "hardcode_gate_failed": hardcode_gate_failed,
        "target": effective_target,
        "production_target": production_target,
        "control_schema": cs or None,
    }
