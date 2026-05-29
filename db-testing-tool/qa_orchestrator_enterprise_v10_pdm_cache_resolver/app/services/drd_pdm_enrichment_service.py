"""Enrich canonical DRD rows with PDM cache validated/predicted source references."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.services.pdm_cache_resolver_service import PDMCacheResolver


class DRDPDMEnrichmentService:
    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}
        self.resolver = PDMCacheResolver(self.config)

    def enrich_rows(self, drd_rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        enriched: List[Dict[str, Any]] = []
        resolutions: List[Dict[str, Any]] = []
        for row in drd_rows:
            res = self.resolver.resolve_row(row).to_dict()
            resolutions.append(res)
            out = dict(row)
            out["pdm_resolution_status"] = res["status"]
            out["pdm_resolution_confidence"] = res["confidence"]
            out["pdm_original_source_schema"] = row.get("source_schema", "")
            out["pdm_original_source_table"] = row.get("source_table", "")
            out["pdm_original_source_attribute"] = row.get("source_attribute", "")
            # Only overwrite source mapping when the resolver has a usable candidate.
            if res["status"] in {"VALIDATED_EXACT", "PREDICTED_AUTO_ACCEPT", "PREDICTED_REVIEW_REQUIRED"}:
                out["source_schema"] = res["resolved_schema"] or out.get("source_schema", "")
                out["source_table"] = res["resolved_table"] or out.get("source_table", "")
                out["source_attribute"] = res["resolved_attribute"] or out.get("source_attribute", "")
            enriched.append(out)
        return enriched, resolutions, self.resolver.cache_summary()
