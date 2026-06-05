"""R5 step 1 (2026-06-06): build the per-datasource FK map from PDM metadata.

ISOLATED, zero generator risk: reads the saved PDM knowledge base via
``load_schema_kb_payload`` (NOT ``_index_from_kb_payload``, which discards FK
data) and records every PDM foreign-key relationship into the persistent FK map
``data/local_kb/fk_map_ds_<N>.json`` as ``source="pdm"`` joins.

PDM FK data lives at ``payload["sources"][i]["pdm"]["relationships"]`` -- a list
of ``{from_schema, from_table, from_column, to_schema, to_table, to_column,
constraint_name}`` (verified on ds_3: 177 relationships). Tables (for VIEW skip)
live at ``pdm["schemas"][j]["tables"][k]`` with a ``type`` field.

Using the correct envelope keys avoids the B1 trap (wrong keys -> empty map) and
the B2 trap (envelope nesting under ``sources``/``pdm``). VIEWs are skipped: a
view's "FK" is not a base-table join key. The ODI is never a source here.

This module only EXTRACTS + persists. Priority/conflict hardening is step 2;
generator integration is step 4. Nothing here touches the control-table
generator or the comparison path, so the grade harness + GUI are unaffected.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Set, Tuple

from app.services.fk_map_service import (
    _norm,
    new_fk_map,
    save_fk_map,
    upsert_join,
)
from app.services.schema_kb_service import load_schema_kb_payload


def _collect_views(pdm: Dict[str, Any]) -> Set[Tuple[str, str]]:
    """Return the set of (schema, table) names whose PDM object is a VIEW."""
    views: Set[Tuple[str, str]] = set()
    for sch in (pdm.get("schemas") or []):
        if not isinstance(sch, dict):
            continue
        sch_name = _norm(sch.get("schema"))
        for tbl in (sch.get("tables") or []):
            if not isinstance(tbl, dict):
                continue
            kind = _norm(tbl.get("type")) or _norm(tbl.get("object_type"))
            if kind == "VIEW":
                tname = _norm(tbl.get("name"))
                tschema = _norm(tbl.get("schema")) or sch_name
                if tname:
                    views.add((tschema, tname))
    return views


def _is_view(views: Set[Tuple[str, str]], schema: str, table: str) -> bool:
    # match on (schema, table) or, defensively, on bare table when schema differs
    if (schema, table) in views:
        return True
    return any(t == table for (_s, t) in views) and not schema


def extract_from_pdm(
    datasource_id: int,
    *,
    kb_loader: Optional[Callable[[Optional[int]], Dict[str, Any]]] = None,
    save: bool = True,
) -> Dict[str, Any]:
    """Build the FK map for ``datasource_id`` from PDM relationship metadata.

    ``kb_loader`` is injectable for tests (defaults to ``load_schema_kb_payload``).
    Returns a stats dict; the FK map is persisted when ``save`` is True.
    """
    loader = kb_loader or load_schema_kb_payload
    payload = loader(datasource_id) or {}
    sources = payload.get("sources") or []

    fk_map = new_fk_map(datasource_id)
    relationships_total = 0
    skipped_view = 0
    skipped_incomplete = 0

    for src in sources:
        if not isinstance(src, dict):
            continue
        pdm = src.get("pdm") or {}
        if not isinstance(pdm, dict):
            continue
        views = _collect_views(pdm)
        for rel in (pdm.get("relationships") or []):
            if not isinstance(rel, dict):
                continue
            relationships_total += 1
            from_schema = _norm(rel.get("from_schema"))
            from_table = _norm(rel.get("from_table"))
            from_column = _norm(rel.get("from_column"))
            to_schema = _norm(rel.get("to_schema"))
            to_table = _norm(rel.get("to_table"))
            to_column = _norm(rel.get("to_column"))

            if _is_view(views, from_schema, from_table):
                skipped_view += 1
                continue
            if not (from_table and from_column and to_table and to_column):
                skipped_incomplete += 1
                continue

            upsert_join(
                fk_map,
                from_schema, from_table, from_column,
                to_schema, to_table, to_column,
                source="pdm",
            )

    joins_written = sum(len(cols or {}) for cols in (fk_map.get("joins") or {}).values())

    saved_path = None
    if save:
        saved_path = str(save_fk_map(datasource_id, fk_map))

    return {
        "datasource_id": int(datasource_id),
        "relationships_total": relationships_total,
        "skipped_view": skipped_view,
        "skipped_incomplete": skipped_incomplete,
        "joins_written": joins_written,
        "base_tables": len(fk_map.get("joins") or {}),
        "saved_path": saved_path,
        "fk_map": fk_map,
    }


if __name__ == "__main__":  # pragma: no cover - manual CLI
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Build fk_map_ds_<N>.json from PDM metadata")
    ap.add_argument("--ds", type=int, required=True, help="datasource id")
    ap.add_argument("--no-save", action="store_true", help="dry run (do not write the map)")
    args = ap.parse_args()
    stats = extract_from_pdm(args.ds, save=not args.no_save)
    stats.pop("fk_map", None)
    print(json.dumps(stats, indent=2))
