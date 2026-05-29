"""Schema analysis service.

Provides analyze_datasource, get_schema_tree, and compare_schemas.
The analysis itself is delegated to the connector; the KB is managed by
schema_kb_service.  This module wires the two together and is the
single point of call for the schemas router.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# In-memory cache: datasource_id → (mtime_float, parsed_tree_list)
_schema_tree_cache: dict[int, tuple[float, list]] = {}


async def analyze_datasource(
    db: AsyncSession,
    datasource_id: int,
    schema_filter: Optional[str] = None,
    schema_filters: Optional[List[str]] = None,
    operation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Analyze a datasource and return schema statistics.

    Returns a dict with at minimum:
        tables: int, columns: int, schemas: list[str]
    """
    logger.warning(
        "schema_service.analyze_datasource called for ds=%s (stub - no connector yet)",
        datasource_id,
    )
    return {
        "tables": 0,
        "columns": 0,
        "schemas": [],
        "datasource_id": datasource_id,
        "status": "stub",
        "message": (
            "Schema analysis is not yet wired to a connector. "
            "Configure a datasource connection and re-run analysis."
        ),
    }


async def get_schema_tree(
    db: AsyncSession,
    datasource_id: int,
) -> list:
    """Return a hierarchical schema tree for a datasource from the local KB JSON.

    Reads the saved schema_kb_ds_{id}.json file produced by Schema Browser → Save to KB.
    Result is cached in memory and invalidated when the file changes (mtime check).
    Returns list of { schema, tables: [ { name, type, columns: [ {name, data_type, nullable, is_pk} ] } ] }
    Returns empty list (not an error) when no KB exists yet.
    """
    from pathlib import Path
    import json
    from app.config import BASE_DIR

    kb_path = BASE_DIR / "data" / "local_kb" / f"schema_kb_ds_{datasource_id}.json"
    if not kb_path.exists():
        logger.info(
            "schema_service.get_schema_tree: no KB found at %s for ds=%s", kb_path, datasource_id
        )
        return []

    # Cache check — compare file mtime to avoid re-parsing large files
    try:
        mtime = kb_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    cached = _schema_tree_cache.get(datasource_id)
    if cached and cached[0] == mtime:
        logger.debug("schema_service.get_schema_tree: cache hit for ds=%s", datasource_id)
        return cached[1]

    try:
        payload = json.loads(kb_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("schema_service.get_schema_tree: failed to read KB: %s", exc)
        return []

    # Normalise: KB may be wrapped in {pdm: {...}} or be the PDM dict directly
    pdm = payload.get("pdm") if isinstance(payload, dict) else None
    if not pdm or not isinstance(pdm, dict):
        pdm = payload if isinstance(payload, dict) else {}

    schemas_raw = pdm.get("schemas", [])
    result = []
    for schema_block in schemas_raw:
        schema_name = (schema_block.get("schema") or schema_block.get("name") or "").upper()
        if not schema_name:
            continue
        tables_out = []
        for tbl in schema_block.get("tables", []):
            tbl_name = (tbl.get("name") or tbl.get("table_name") or "").upper()
            if not tbl_name:
                continue
            cols_out = []
            for c in tbl.get("columns", []):
                cols_out.append({
                    "name": (c.get("name") or "").upper(),
                    "data_type": c.get("data_type") or c.get("type") or "",
                    "nullable": bool(c.get("nullable", True)),
                    "is_pk": bool(c.get("is_pk") or c.get("primary_key", False)),
                })
            tables_out.append({
                "name": tbl_name,
                "type": tbl.get("type") or tbl.get("object_type") or "TABLE",
                "columns": cols_out,
            })
        result.append({"schema": schema_name, "tables": tables_out})

    _schema_tree_cache[datasource_id] = (mtime, result)
    logger.info(
        "schema_service.get_schema_tree: ds=%s → %d schemas from KB (cached)", datasource_id, len(result)
    )
    return result


async def compare_schemas(
    db: AsyncSession,
    source_datasource_id: int,
    source_schema: Optional[str],
    source_table: Optional[str],
    target_datasource_id: int,
    target_schema: Optional[str],
    target_table: Optional[str],
) -> Dict[str, Any]:
    """Compare two schema objects and return a diff report."""
    logger.warning(
        "schema_service.compare_schemas called src_ds=%s tgt_ds=%s (stub)",
        source_datasource_id,
        target_datasource_id,
    )
    return {
        "source": {"datasource_id": source_datasource_id, "schema": source_schema, "table": source_table},
        "target": {"datasource_id": target_datasource_id, "schema": target_schema, "table": target_table},
        "added_columns": [],
        "removed_columns": [],
        "changed_columns": [],
        "status": "stub",
    }
