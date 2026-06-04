"""Regression: recover a transformation/lookup rule a DRD author misplaced in the
"Nullable?" column (C25) instead of the "Transformation" column (C26).

Operator 2026-06-04: the CLOSE taxlot DRD puts the CL_VAL scheme rules
("Use SUB_LOT_TXN_EV_TP_ID under CL_VAL_ID where CL_SCM_ID = 86 and pick
CL_VAL_NM") in the Nullable column for several CL_VAL target columns.  The
parser read transformation strictly from C26 and silently dropped the rule, so
the generated INSERT could never honor the scheme -- which made it look like the
"DRD was under-specified" when in fact the DRD carried the rule all along.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.drd_import_service import _is_misplaced_transformation_prose

_ROOT = Path(__file__).resolve().parents[1]
_CLOSE_DRD = _ROOT / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"


def test_is_misplaced_prose_unit():
    # Real Y/N-style nullable flags are NOT misclassified as transformation.
    for flag in ("", "Y", "N", "Yes", "No", "TRUE", "false", "NULL", "Not Null", "1", "0", "N/A"):
        assert _is_misplaced_transformation_prose(flag) is False, flag
    # Prose / lookup rules ARE recovered.
    assert _is_misplaced_transformation_prose("Use SUB_LOT_TXN_EV_TP_ID under CL_VAL_ID where CL_SCM_ID = 86 and pick CL_VAL_NM")
    assert _is_misplaced_transformation_prose("Use SRC_RCRD_TP_ID in CL_VAL and use- CL_VAL_CODE")
    assert _is_misplaced_transformation_prose("Lookup on IMT_PD_DIM")  # has whitespace
    assert _is_misplaced_transformation_prose("CL_SCM_ID=84")          # keyword, no space


pytestmark_drd = pytest.mark.skipif(
    not _CLOSE_DRD.exists(), reason="taxlot CLOSE DRD fixture missing"
)


@pytestmark_drd
def test_close_drd_recovers_cl_val_scheme_rules():
    """The two CL_VAL columns whose rule the author put in the Nullable column
    must now carry that rule in `transformation`; a column with a correctly
    placed rule is unaffected."""
    from app.services.control_table_service import analyze_control_table

    res = analyze_control_table(
        file_bytes=_CLOSE_DRD.read_bytes(),
        filename=_CLOSE_DRD.name,
        target_schema="TAXLOT_OWNER",
        target_table="CLS_TAX_LOTS_NON_BKR_FACT",
        source_datasource_id=2,
        target_datasource_id=2,
        control_schema="ikorostelev",
    )
    rows = {(r.get("column") or "").upper(): r for r in res.get("analysis_rows", [])}

    cls = (rows.get("CLS_TXN_EV_TP", {}).get("transformation") or "").upper()
    assert "CL_SCM_ID = 86" in cls or "CL_SCM_ID=86" in cls, cls
    assert "CL_VAL_NM" in cls, cls

    src = (rows.get("SRC_RCRD_TP_CD", {}).get("transformation") or "").upper()
    assert "CL_VAL_CODE" in src, src

    # correctly-placed rule (C26) is untouched
    opn = (rows.get("OPN_TXN_EV_TP", {}).get("transformation") or "").upper()
    assert "ALWAYS NULL" in opn, opn
