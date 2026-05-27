import os
from app.services.control_table_service import select_expr_for_column, fallback_non_nullable_expression


def test_select_prefers_drd_when_present():
    col_name = "COL1"
    row = {"drd_expression": "1", "source_attribute": "SA"}
    baseline = {"COL1": "BASE_EXPR"}
    col_def = {"name": "COL1", "nullable": True}
    expr, prov = select_expr_for_column(col_name, row, baseline, col_def, drd_candidate="1")
    assert prov == "DRD"
    assert expr.strip() == "1"


def test_select_uses_baseline_when_drd_null():
    col_name = "COL2"
    row = {"drd_expression": "NULL", "source_attribute": "SB"}
    baseline = {"COL2": "BASE_EXPR"}
    col_def = {"name": "COL2", "nullable": True}
    expr, prov = select_expr_for_column(col_name, row, baseline, col_def, drd_candidate="NULL")
    assert prov == "BASELINE"
    assert isinstance(expr, str) and expr.strip() != ""


def test_fallback_non_nullable():
    val = fallback_non_nullable_expression("ID", "NUMBER", is_pk=False)
    assert val == "0"

