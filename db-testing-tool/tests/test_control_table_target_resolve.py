"""Regression: control-table flow resolves a DRD's logical/DA-team target name
to the physical PDM table name.

Operator 2026-06-03: the DRD "Table Name (From DA Team)" row carries BOTH the
logical name (e.g. cls_tax_lots_fact_rjt) and the physical name
(CLS_TAX_LOTS_NON_BKR_FACT) side by side.  The metadata parser used to keep
only the logical (column C1), so the control-table analyze hit a PDM-miss when
the operator typed/auto-filled the logical name.  The 2-part fix: (1) capture
all table-name cells as candidates; (2) the analyze orchestrator resolves the
candidate that exists in the PDM.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CLOSE_DRD = _ROOT / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"

pytestmark = pytest.mark.skipif(
    not _CLOSE_DRD.exists(), reason="taxlot DRD fixture missing"
)


def test_extract_metadata_captures_both_table_name_candidates():
    """The parser must capture BOTH the logical and physical table names from
    the 'Table Name (From DA Team)' row, not just column C1."""
    from app.services.drd_import_service import extract_drd_metadata
    meta = extract_drd_metadata(_CLOSE_DRD.read_bytes(), _CLOSE_DRD.name, None)
    cands = [c.upper() for c in (meta.get("table_name_candidates") or [])]
    assert "CLS_TAX_LOTS_FACT_RJT" in cands, cands          # logical / DA-team
    assert "CLS_TAX_LOTS_NON_BKR_FACT" in cands, cands       # physical


def _pdm_has_physical() -> bool:
    """True only when the ds_3 PDM (Git-LFS / generated) is present locally."""
    try:
        from app.services.control_table_service import load_target_table_definition
        load_target_table_definition(2, "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _pdm_has_physical(), reason="ds_3 PDM not present (LFS)")
def test_resolve_logical_target_maps_to_physical():
    """Typing the logical name resolves to the physical PDM table; order-
    independent; an already-physical name stays unchanged."""
    from app.services.control_table_service import _resolve_physical_target
    b, fn = _CLOSE_DRD.read_bytes(), _CLOSE_DRD.name
    sch, tbl = _resolve_physical_target(2, "TAXLOT_OWNER", "cls_tax_lots_fact_rjt", b, fn, None)
    assert (sch.upper(), tbl.upper()) == ("TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT")
    # already-physical input is preserved
    sch2, tbl2 = _resolve_physical_target(2, "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT", b, fn, None)
    assert tbl2.upper() == "CLS_TAX_LOTS_NON_BKR_FACT"


@pytest.mark.skipif(not _pdm_has_physical(), reason="ds_3 PDM not present (LFS)")
def test_unresolvable_target_returns_typed_unchanged():
    """When nothing resolves, the typed values are returned unchanged so the
    normal PDM-miss 422 still fires (no silent rewrite)."""
    from app.services.control_table_service import _resolve_physical_target
    b, fn = _CLOSE_DRD.read_bytes(), _CLOSE_DRD.name
    sch, tbl = _resolve_physical_target(2, "NO_SCHEMA", "TOTALLY_MISSING_TBL_XYZ", b, fn, None)
    assert (sch, tbl) == ("NO_SCHEMA", "TOTALLY_MISSING_TBL_XYZ")
