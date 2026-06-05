"""Per-datasource FK relationship map -- a persistent knowledge base of join
relationships ``base_table.fk_col -> ref_table.ref_col [+ scheme]`` that the
control-table join-derivation engine consults as a PRINCIPLED FALLBACK when a DRD's
join prose is unclear / typo'd, instead of giving up (``ON 1=0``) or mis-keying.

Sources (priority, applied by callers): (1) DRD-declared joins -- learned via
``upsert_join`` on every CLEAR resolution; (2) PDM foreign-key metadata --
``extract_from_pdm``; (3) naming conventions (caller-side). The ODI is NEVER a
source: it is the validation oracle, and DRD<->ODI join divergences are SURFACED,
not absorbed (operator 2026-06-05). Where nothing resolves a join, the caller flags
``DRD_UNDERSPECIFIED`` -- it does not fill from the ODI.

Storage: ``data/local_kb/fk_map_ds_<N>.json``. Generic: NO hardcoded table/column
names live in this module -- the map is DATA, learned per database.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.schema_kb_service import _kb_dir  # reused; monkeypatchable in tests

_SCHEMA_VERSION = 1


def _fk_map_path(datasource_id: int) -> Path:
    return _kb_dir() / f"fk_map_ds_{datasource_id}.json"


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().upper()


def _fq(schema: Optional[str], table: Optional[str]) -> str:
    schema_u, table_u = _norm(schema), _norm(table)
    if "." in table_u:  # caller passed a qualified name as the table
        return table_u
    return f"{schema_u}.{table_u}" if schema_u else table_u


def _bare(fq: str) -> str:
    return _norm(fq).split(".")[-1]


def new_fk_map(datasource_id: int) -> Dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "datasource_id": int(datasource_id),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "joins": {},  # base_fq -> { fk_col -> entry }
    }


def load_fk_map(datasource_id: int) -> Dict[str, Any]:
    p = _fk_map_path(datasource_id)
    if not p.exists():
        return new_fk_map(datasource_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return new_fk_map(datasource_id)
    if not isinstance(data, dict) or not isinstance(data.get("joins"), dict):
        return new_fk_map(datasource_id)
    return data


def save_fk_map(datasource_id: int, fk_map: Dict[str, Any]) -> Path:
    p = _fk_map_path(datasource_id)
    out = dict(fk_map)
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)  # atomic on same filesystem
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return p


def upsert_join(
    fk_map: Dict[str, Any],
    base_schema: Optional[str],
    base_table: Optional[str],
    fk_col: Optional[str],
    ref_schema: Optional[str],
    ref_table: Optional[str],
    ref_col: Optional[str],
    *,
    scheme_filter: Optional[str] = None,
    project_default: Optional[str] = None,
    source: str = "drd",
) -> Dict[str, Any]:
    """Add or reinforce one join relationship. Incomplete relationships (missing
    base/fk/ref table/col) are IGNORED -- the map only stores resolvable joins.
    ``seen_count`` increments and ``sources`` accumulate on repeat (the learning)."""
    base_fq = _fq(base_schema, base_table)
    fk_col_u = _norm(fk_col)
    ref_table_u = _norm(ref_table)
    ref_col_u = _norm(ref_col)
    if not base_fq or not _bare(base_fq) or not fk_col_u or not ref_table_u or not ref_col_u:
        return fk_map
    joins = fk_map.setdefault("joins", {})
    tbl = joins.setdefault(base_fq, {})
    existing = tbl.get(fk_col_u) or {}
    tbl[fk_col_u] = {
        "ref_schema": _norm(ref_schema),
        "ref_table": ref_table_u,
        "ref_col": ref_col_u,
        "scheme_filter": (scheme_filter.strip() if isinstance(scheme_filter, str) and scheme_filter.strip() else None),
        "project_default": (_norm(project_default) or None),
        "sources": sorted(set(existing.get("sources", []) + [source])),
        "seen_count": int(existing.get("seen_count", 0)) + 1,
    }
    return fk_map


def resolve(fk_map: Dict[str, Any], base_table: Optional[str], fk_col: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a join by base table (qualified ``SCHEMA.TABLE`` or bare ``TABLE``)
    and FK column. Exact qualified match wins; else a unique bare-table match."""
    joins = fk_map.get("joins", {}) or {}
    fk_col_u = _norm(fk_col)
    if not fk_col_u:
        return None
    base_u = _norm(base_table)
    if base_u in joins and fk_col_u in (joins[base_u] or {}):
        return joins[base_u][fk_col_u]
    # bare-table fallback: accept only if exactly one base_fq (with this fk_col) matches
    bare = _bare(base_u)
    hits = [
        cols[fk_col_u]
        for base_fq, cols in joins.items()
        if _bare(base_fq) == bare and fk_col_u in (cols or {})
    ]
    return hits[0] if len(hits) == 1 else None


def resolve_by_ref(fk_map: Dict[str, Any], ref_table: Optional[str]) -> List[Dict[str, Any]]:
    """All join entries that reference ``ref_table`` (bare or qualified)."""
    ref_bare = _bare(ref_table or "")
    out: List[Dict[str, Any]] = []
    for base_fq, cols in (fk_map.get("joins", {}) or {}).items():
        for fk_col, entry in (cols or {}).items():
            if _bare(entry.get("ref_table") or "") == ref_bare and ref_bare:
                out.append({"base_fq": base_fq, "fk_col": _norm(fk_col), **entry})
    return out
