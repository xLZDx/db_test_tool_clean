"""Tests for app.sql_model.etl_block_index -- generic ETL-block extraction.

No hard-coded business / table names.  Uses synthetic DRD-style content.
"""
from __future__ import annotations

import pytest

from app.sql_model.drd_multi_sheet import (
    DrdMultiSheetResult,
    ExtractionRule,
    SheetRole,
)
from app.sql_model.etl_block_index import (
    EtlBlock,
    EtlBlockIndex,
    build_block_index,
    find_block_references,
    resolve_block_body,
)


def _rule(sheet: str, desc: str, raw_first: str | None = None):
    """Build a single ExtractionRule with a controlled first cell."""
    return ExtractionRule(
        sheet=sheet,
        role=SheetRole.ETL_NOTES,
        rule_type="note",
        target_col=None,
        description=desc,
        raw_row=[raw_first if raw_first is not None else desc],
    )


def _multi_sheet_with_rules(rules):
    return DrdMultiSheetResult(extracted_rules=rules)


def test_empty_input_returns_empty_index():
    idx = build_block_index(_multi_sheet_with_rules([]))
    assert idx.blocks == {}


def test_colon_header_with_inline_body():
    rules = [
        _rule("ETL Notes", "APACSH: regexp_like(cv.cl_val_code,'^APACSH[0-7][0-9]')"),
        _rule("ETL Notes", "join ccal_repl_owner.apa ap on ap.exec_id = t.txn_id"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert "APACSH" in idx
    b = idx.get("APACSH")
    assert b.sheet == "ETL Notes"
    assert "regexp_like" in b.body
    # Continuation row appended
    assert "join ccal_repl_owner.apa" in b.body


def test_bare_header_followed_by_sql_body():
    rules = [
        _rule("ETL Notes", "APASEC", raw_first="APASEC"),
        _rule("ETL Notes", "select x from y where regexp_like(z,'^APASEC[0-9]+')"),
        _rule("ETL Notes", "join other_table o on o.id = x.id"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert "APASEC" in idx
    assert "regexp_like" in idx.get("APASEC").body


def test_bare_header_without_sql_followup_not_block():
    """A lone upper-case word followed by prose must NOT become a block."""
    rules = [
        _rule("ETL Notes", "BACKLOG"),
        _rule("ETL Notes", "Discussion about future enhancements"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert "BACKLOG" not in idx


def test_two_distinct_blocks_in_same_sheet():
    rules = [
        _rule("ETL Notes", "FIRST: select 1 from dual"),
        _rule("ETL Notes", "where col = 'a'"),
        _rule("ETL Notes", "SECOND: select 2 from dual"),
        _rule("ETL Notes", "where col = 'b'"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert "FIRST" in idx and "SECOND" in idx
    assert "select 1" in idx.get("FIRST").body
    assert "select 2" in idx.get("SECOND").body


def test_find_block_references_use_phrase():
    rules = [_rule("ETL Notes", "APACSH: stuff", raw_first="APACSH: stuff")]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    refs = find_block_references("Use APACSH logic from 'ETL Notes' tab", idx)
    assert refs == ["APACSH"]


def test_find_block_references_unknown_name_filtered():
    rules = [_rule("ETL Notes", "APACSH: x", raw_first="APACSH: x")]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    # 'NONEXISTENT' is not in index -> filtered out
    refs = find_block_references("Use NONEXISTENT logic from notes", idx)
    assert refs == []


def test_resolve_block_body_returns_block_content():
    rules = [
        _rule("ETL Notes", "APACSH: select * from apa where regexp_like(...)"),
        _rule("ETL Notes", "join cl_val cv on cv.id = apa.tp_id"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    body = resolve_block_body("Use APACSH logic from 'ETL Notes' tab", idx)
    assert body is not None
    assert "regexp_like" in body


def test_resolve_block_body_none_when_no_reference():
    rules = [_rule("ETL Notes", "APACSH: x", raw_first="APACSH: x")]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert resolve_block_body("just a plain text without references", idx) is None


def test_generic_works_for_arbitrary_block_names():
    """No hard-coded names: any uppercase identifier acts as a block."""
    rules = [
        _rule("ETL Notes", "WIDGET_LOOKUP: select w.* from widgets w"),
        _rule("ETL Notes", "where w.active_flag = 'Y'"),
    ]
    idx = build_block_index(_multi_sheet_with_rules(rules))
    assert "WIDGET_LOOKUP" in idx
    refs = find_block_references("apply WIDGET_LOOKUP logic from sheet WidgetNotes", idx)
    assert refs == ["WIDGET_LOOKUP"]
