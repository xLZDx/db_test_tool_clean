"""Regression: POST /control-table/empty must surface a PDM-missing target as a
readable 422 (mirroring /control-table/analyze), NOT an opaque 500.

Operator 2026-06-03 (e2e finding): the /empty handler called
load_target_table_definition without a try/except, so a PDM-miss ValueError
propagated as a bare 500 "Internal Server Error" toast in the GUI -- hiding the
actionable remediation message that /analyze already returned.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def test_control_table_empty_pdm_miss_returns_422_not_500():
    from app.routers.tests_control_table import (
        build_empty_control_table,
        ControlTableEmptyRequest,
    )

    body = ControlTableEmptyRequest(
        target_datasource_id=2,
        target_schema="NO_SUCH_OWNER",
        target_table="DEFINITELY_MISSING_TABLE_XYZ_20260603",
        control_schema="ikorostelev",
    )

    with pytest.raises(HTTPException) as ei:
        asyncio.run(build_empty_control_table(body))

    assert ei.value.status_code == 422, ei.value.status_code
    detail = str(ei.value.detail)
    assert "not found in any saved PDM" in detail, detail
    # the remediation guidance must reach the client (it was lost in the 500)
    assert "Generate PDM" in detail or "schema_kb_ds_" in detail, detail
