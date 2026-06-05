"""Unit tests for the per-datasource FK relationship map (fk_map_service).

The FK map is the persistent join-knowledge base used as a principled fallback in
control-table join derivation (operator 2026-06-05). These tests cover the core
storage + upsert(learning) + resolve API in isolation (no PDM/DRD/ODI dependency)."""
from __future__ import annotations

import json

import pytest

from app.services import fk_map_service as fk


@pytest.fixture(autouse=True)
def _isolate_kb_dir(tmp_path, monkeypatch):
    # redirect the map file into a temp dir so tests never touch the real local_kb
    monkeypatch.setattr(fk, "_kb_dir", lambda: tmp_path)
    return tmp_path


def test_new_and_load_default_when_absent():
    m = fk.load_fk_map(7)
    assert m["datasource_id"] == 7
    assert m["joins"] == {}
    assert m["schema_version"] == fk._SCHEMA_VERSION


def test_upsert_then_resolve_exact_qualified():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "TAXLOT_STG_OWNER", "OPN_TAX_LOTS_NONBKR_TGT", "src_rcrd_tp_id",
                   "CCAL_REPL_OWNER", "CL_VAL", "cl_val_id",
                   project_default="cl_val_code", source="drd")
    e = fk.resolve(m, "TAXLOT_STG_OWNER.OPN_TAX_LOTS_NONBKR_TGT", "SRC_RCRD_TP_ID")
    assert e is not None
    assert e["ref_table"] == "CL_VAL" and e["ref_col"] == "CL_VAL_ID"
    assert e["project_default"] == "CL_VAL_CODE"
    assert e["seen_count"] == 1 and e["sources"] == ["drd"]


def test_upsert_is_idempotent_learning_bumps_count_and_merges_sources():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "S", "BASE", "FK", "RS", "REF", "PK", source="drd")
    fk.upsert_join(m, "S", "BASE", "FK", "RS", "REF", "PK", source="pdm")
    e = fk.resolve(m, "S.BASE", "FK")
    assert e["seen_count"] == 2
    assert e["sources"] == ["drd", "pdm"]


def test_incomplete_upsert_is_ignored():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "S", "BASE", "FK", "RS", "", "PK")   # missing ref_table
    fk.upsert_join(m, "S", "BASE", "", "RS", "REF", "PK")  # missing fk_col
    fk.upsert_join(m, "S", "", "FK", "RS", "REF", "PK")    # missing base_table
    assert m["joins"] == {}


def test_resolve_bare_table_unique_match():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "COMMON_OWNER", "SRC_STM_DIM", "SRC_STM_ID", "COMMON_OWNER", "SRC_STM_DIM", "SRC_STM_ID")
    # only one base_fq carries this fk -> bare lookup resolves
    assert fk.resolve(m, "SRC_STM_DIM", "SRC_STM_ID") is not None


def test_resolve_bare_table_ambiguous_returns_none():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "SCH_A", "BASE", "FK", "RS", "REF1", "PK")
    fk.upsert_join(m, "SCH_B", "BASE", "FK", "RS", "REF2", "PK")
    # two distinct schemas carry BASE.FK -> bare lookup is ambiguous -> None
    assert fk.resolve(m, "BASE", "FK") is None
    # but the qualified lookups are unambiguous
    assert fk.resolve(m, "SCH_A.BASE", "FK")["ref_table"] == "REF1"
    assert fk.resolve(m, "SCH_B.BASE", "FK")["ref_table"] == "REF2"


def test_resolve_by_ref():
    m = fk.new_fk_map(3)
    fk.upsert_join(m, "S", "T1", "FK1", "RS", "CL_VAL", "CL_VAL_ID")
    fk.upsert_join(m, "S", "T2", "FK2", "RS", "CL_VAL", "CL_VAL_ID")
    fk.upsert_join(m, "S", "T3", "FK3", "RS", "OTHER", "ID")
    hits = fk.resolve_by_ref(m, "CL_VAL")
    assert {h["base_fq"] for h in hits} == {"S.T1", "S.T2"}


def test_save_load_round_trip(tmp_path):
    m = fk.new_fk_map(9)
    fk.upsert_join(m, "S", "BASE", "FK", "RS", "REF", "PK", scheme_filter="CL_SCM_ID=86")
    p = fk.save_fk_map(9, m)
    assert p.exists() and p.name == "fk_map_ds_9.json"
    m2 = fk.load_fk_map(9)
    e = fk.resolve(m2, "S.BASE", "FK")
    assert e["ref_table"] == "REF" and e["scheme_filter"] == "CL_SCM_ID=86"


def test_corrupt_file_falls_back_to_default(tmp_path):
    (tmp_path / "fk_map_ds_5.json").write_text("{ not json", encoding="utf-8")
    m = fk.load_fk_map(5)
    assert m["joins"] == {} and m["datasource_id"] == 5


def test_save_is_atomic_no_tmp_left_behind(tmp_path):
    m = fk.new_fk_map(1)
    fk.upsert_join(m, "S", "B", "F", "RS", "R", "P")
    fk.save_fk_map(1, m)
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []
