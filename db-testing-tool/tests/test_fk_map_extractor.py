"""R5 step 1 tests for fk_map_extractor.extract_from_pdm.

Functional: injects a synthetic PDM payload (no dependency on the 111MB KB) and
asserts the extractor records base-table FK joins, skips VIEW sources and
incomplete relationships, and writes nothing when save=False. A second test runs
against the real ds_3 KB when present (skipif), asserting the 177-relationship
order of magnitude and that source='pdm' joins resolve.
"""
from pathlib import Path

import pytest

from app.services import fk_map_extractor as ext
from app.services.fk_map_service import resolve

KB_DS3 = Path(__file__).resolve().parents[1] / "data" / "local_kb" / "schema_kb_ds_3.json"


def _synthetic_payload(_ds=None):
    return {"sources": [{"pdm": {
        "schemas": [{"schema": "S1", "tables": [
            {"schema": "S1", "name": "BASE_T", "type": "TABLE"},
            {"schema": "S1", "name": "A_VIEW", "type": "VIEW"},
            {"schema": "S1", "name": "DIM_T", "type": "TABLE"},
        ]}],
        "relationships": [
            {"from_schema": "S1", "from_table": "BASE_T", "from_column": "REF_ID",
             "to_schema": "S1", "to_table": "DIM_T", "to_column": "DIM_ID", "constraint_name": "FK1"},
            # view as the FROM side -> skipped
            {"from_schema": "S1", "from_table": "A_VIEW", "from_column": "X_ID",
             "to_schema": "S1", "to_table": "DIM_T", "to_column": "DIM_ID", "constraint_name": "FK2"},
            # incomplete (no from_column) -> skipped
            {"from_schema": "S1", "from_table": "BASE_T", "from_column": "",
             "to_schema": "S1", "to_table": "DIM_T", "to_column": "DIM_ID"},
        ],
    }}]}


def test_extract_from_synthetic_pdm_records_skips_and_views():
    stats = ext.extract_from_pdm(3, kb_loader=_synthetic_payload, save=False)
    assert stats["relationships_total"] == 3
    assert stats["skipped_view"] == 1
    assert stats["skipped_incomplete"] == 1
    assert stats["joins_written"] == 1
    assert stats["saved_path"] is None  # save=False writes nothing
    fk_map = stats["fk_map"]
    assert fk_map["schema_version"] >= 1
    assert fk_map["datasource_id"] == 3


def test_extract_from_synthetic_resolves_with_pdm_source():
    stats = ext.extract_from_pdm(3, kb_loader=_synthetic_payload, save=False)
    entry = resolve(stats["fk_map"], "S1.BASE_T", "REF_ID")
    assert entry is not None
    assert entry["ref_table"] == "DIM_T"
    assert entry["ref_col"] == "DIM_ID"
    assert "pdm" in entry["sources"]
    # the view-sourced join must NOT be present
    assert resolve(stats["fk_map"], "S1.A_VIEW", "X_ID") is None


def test_save_false_does_not_write(tmp_path, monkeypatch):
    # redirect the KB dir so a stray write would be detectable
    import app.services.fk_map_service as svc
    monkeypatch.setattr(svc, "_kb_dir", lambda: tmp_path)
    ext.extract_from_pdm(7, kb_loader=_synthetic_payload, save=False)
    assert not (tmp_path / "fk_map_ds_7.json").exists()


def test_save_true_writes_map(tmp_path, monkeypatch):
    import app.services.fk_map_service as svc
    monkeypatch.setattr(svc, "_kb_dir", lambda: tmp_path)
    stats = ext.extract_from_pdm(7, kb_loader=_synthetic_payload, save=True)
    out = tmp_path / "fk_map_ds_7.json"
    assert out.exists()
    assert stats["saved_path"] == str(out)


@pytest.mark.skipif(not KB_DS3.exists(), reason="ds_3 KB not present locally")
def test_extract_real_ds3_pdm_relationships():
    stats = ext.extract_from_pdm(3, save=False)
    # ds_3 PDM had 177 relationships at build time; assert the order of magnitude
    assert stats["relationships_total"] >= 100, stats
    assert stats["joins_written"] > 0, stats
    assert stats["base_tables"] > 0, stats
    # every written entry must carry the pdm source
    for cols in stats["fk_map"]["joins"].values():
        for entry in cols.values():
            assert "pdm" in entry["sources"]
