"""Semantic alias quality gate.

XML is optional. When XML is unavailable, this service returns DRD_ONLY_GENERATED.
When XML is available, it can compare generated DRD logic against normalized ODI/XML logic.

The important v10 behavior is that XML does not drive generation. PDM cache + DRD drive
generation; XML is only a validation/comparison gate.
"""
from __future__ import annotations

from typing import Any, Dict, List


class SemanticAliasQualityGateService:
    def evaluate(self, generated: Dict[str, Any], xml_bytes: bytes | None = None, config: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if not xml_bytes:
            return {
                "status": "DRD_ONLY_GENERATED",
                "xml_available": False,
                "schema_score": None,
                "transformation_logic_score": None,
                "statement_modes": ["source_select", "insert_select", "cte", "merge"],
                "counts": {
                    "generated_joins": len(generated.get("plan", {}).get("joins", [])),
                    "unresolved_transformations": len(generated.get("unresolved", []))
                },
                "notes": ["XML not supplied. SQL generation used DRD + PDM cache only; XML quality gate skipped."]
            }
        # Full XML comparison should reuse the v9 XML extractor and semantic alias comparator.
        return {
            "status": "XML_GATE_AVAILABLE_REUSE_V9_COMPARATOR",
            "xml_available": True,
            "notes": ["Wire this method to the existing v9 normalized XML semantic alias comparator in the app integration layer."]
        }
