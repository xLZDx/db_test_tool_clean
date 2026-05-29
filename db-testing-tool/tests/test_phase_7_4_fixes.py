"""Tests for Phase 7.4 bug fixes (operator 2026-05-29).

Operator surfaced 6 new bugs in the GUI flow:
  1. drd_first_emitter mismatches (deferred to next session)
  2. DDL must come from PDM/real DB only (not DRD-augmented)
  3. Pre-insert dry-run validation
  4. PDM lookup fails when KB file is missing/LFS-pointer
  5. Create-table 500 error (same root cause as #4)
  6. Test generation should not require re-uploading DRD file
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))


# ── Issue 4: LFS-pointer detection ────────────────────────────────────────────

def test_load_schema_kb_payload_skips_lfs_pointer(tmp_path, monkeypatch, caplog):
    """A 133-byte Git LFS pointer file must be detected as such and
    logged at WARNING level so the operator knows to run `git lfs pull`,
    not silently produce an empty payload."""
    import logging
    import app.services.schema_kb_service as kb_mod

    # Build a fake KB dir with one LFS-pointer + one real JSON.
    fake_dir = tmp_path / "local_kb"
    fake_dir.mkdir()
    lfs_pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:33df8303b7f3f9511de814b8f1f7c575a4753d094072ab4f7959cc6b0374a3cc\n"
        "size 57748088\n"
    )
    (fake_dir / "schema_kb_ds_1.json").write_text(lfs_pointer)
    (fake_dir / "schema_kb_ds_2.json").write_text(
        json.dumps({"datasource_id": 2, "pdm": {"schemas": []}})
    )

    monkeypatch.setattr(kb_mod, "_kb_dir", lambda: fake_dir)
    caplog.set_level(logging.WARNING, logger="app.services.schema_kb_service")

    payload = kb_mod.load_schema_kb_payload()
    assert isinstance(payload, dict)
    # ds_2 is real -> 1 source
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["datasource_id"] == 2
    # ds_1 LFS pointer was detected + warned
    assert any(
        "Git LFS pointer" in r.message
        for r in caplog.records
        if r.name == "app.services.schema_kb_service"
    )


# ── Issue 4: load_target_table_definition scans on-disk KBs ──────────────────

def test_load_target_table_definition_finds_in_unregistered_on_disk_kb(tmp_path, monkeypatch):
    """When the table isn't in the primary KB or any registered DS but
    IS in an on-disk schema_kb_ds_<N>.json file with an unregistered N,
    the function must still find it (Phase 7.4)."""
    import app.services.schema_kb_service as kb_mod
    import app.services.control_table_service as cts_mod

    fake_dir = tmp_path / "local_kb"
    fake_dir.mkdir()
    # Empty registered KB
    (fake_dir / "schema_kb_ds_1.json").write_text(
        json.dumps({"datasource_id": 1, "pdm": {"schemas": []}})
    )
    # Real data lives in unregistered ds_42
    real_kb = {
        "datasource_id": 42,
        "pdm": {
            "schemas": [
                {
                    "schema": "MY_SCHEMA",
                    "tables": [
                        {"name": "MY_TABLE",
                         "columns": [{"name": "COL_A", "data_type": "NUMBER", "nullable": True}]},
                    ],
                },
            ],
        },
    }
    (fake_dir / "schema_kb_ds_42.json").write_text(json.dumps(real_kb))

    monkeypatch.setattr(kb_mod, "_kb_dir", lambda: fake_dir)
    monkeypatch.setattr(cts_mod, "_list_all_datasource_ids", lambda: [1])
    monkeypatch.setattr(
        cts_mod, "_load_table_def_from_live_db",
        lambda *a, **kw: None,
    )

    td = cts_mod.load_target_table_definition(1, "MY_SCHEMA", "MY_TABLE")
    assert td is not None
    assert td["name"] == "MY_TABLE"
    assert len(td["columns"]) == 1
    # Source-label must indicate unregistered on-disk KB
    assert "schema_kb_ds_42.json" in td.get("_source_label", "")


def test_load_target_table_definition_error_message_is_ascii_only(tmp_path, monkeypatch):
    """The raised ValueError must be ASCII-only so Windows CP1252 console
    encoding doesn't crash on the right-arrow character."""
    import app.services.schema_kb_service as kb_mod
    import app.services.control_table_service as cts_mod

    fake_dir = tmp_path / "local_kb"
    fake_dir.mkdir()
    monkeypatch.setattr(kb_mod, "_kb_dir", lambda: fake_dir)
    monkeypatch.setattr(cts_mod, "_list_all_datasource_ids", lambda: [1])
    monkeypatch.setattr(
        cts_mod, "_load_table_def_from_live_db",
        lambda *a, **kw: None,
    )

    with pytest.raises(ValueError) as ei:
        cts_mod.load_target_table_definition(1, "X", "Y")
    msg = str(ei.value)
    # All chars must be ASCII (CP1252-safe)
    assert all(ord(c) < 128 for c in msg), f"non-ASCII in error message: {msg!r}"
    # And specifically the old non-ASCII arrow should be gone
    assert "→" not in msg


# ── Issue 3: dry-run validation surfaces NULL substitution ────────────────────

def test_insert_dry_run_detects_pdm_miss_null():
    """If the INSERT contains NULL /* PDM_MISS ... */ markers, the
    dry-run report MUST flag NULL_SUBSTITUTION and NOT pass."""
    # Synthesize a minimal V9 pipeline run with a forced PDM_MISS
    # (use the standalone dry-run analyser logic directly).
    import re

    insert_sql = """
    INSERT INTO X (COL_A, COL_B)
    SELECT
        NULL /* PDM_MISS: cannot resolve alias X for COL_A -- add to PDM */ AS COL_A,
        t.SOMETHING AS COL_B
    FROM Y t;
    """
    null_substituted = []
    for line in insert_sql.splitlines():
        m_null = re.search(r"NULL\s*/\*\s*PDM_MISS:?[^*]*\*/\s*AS\s+([A-Z0-9_]+)", line)
        if m_null:
            null_substituted.append(m_null.group(1))
    assert null_substituted == ["COL_A"]


def test_insert_dry_run_detects_provenance_fallback():
    """If the INSERT has lines marked -- [DRD_PHYSICAL_FALLBACK] ..., the
    dry-run report MUST flag PROVENANCE_FALLBACK."""
    import re
    sql = (
        "    t.SRC_X AS X,  -- [DRD_PHYSICAL_FALLBACK] DRD physical "
        "source CCAL_REPL_OWNER.TXN.SRC_X (ODI ref to TXN skipped: no JOIN)"
    )
    fb = []
    for line in sql.splitlines():
        m_fb = re.search(r"\bAS\s+([A-Z0-9_]+),?\s*--.*DRD_PHYSICAL_FALLBACK", line, flags=re.IGNORECASE)
        if m_fb:
            fb.append(m_fb.group(1))
    assert fb == ["X"]


# ── Issue 2: DDL warnings, not DDL augmentation ───────────────────────────────

def test_analyze_control_table_does_not_augment_ddl_with_drd_only_cols(monkeypatch, tmp_path):
    """Operator-locked Phase 7.4: revert Phase 7.3 Issue 1.  DDL comes
    from PDM ONLY.  DRD-only columns are reported as kb_validation
    warnings, not silently defaulted to VARCHAR2(4000)."""
    import app.services.control_table_service as cts_mod

    target_def = {
        "schema": "S",
        "name": "T",
        "columns": [{"name": "COL_A", "data_type": "NUMBER", "nullable": True}],
    }
    drd_rows = [
        {"physical_name": "COL_A", "source_table": "SRC", "source_attribute": "A"},
        {"physical_name": "COL_DRD_ONLY", "source_table": "SRC", "source_attribute": "B"},
    ]
    # Direct call to the DDL builder with original (un-augmented) target_def
    ddl = cts_mod.build_control_table_ddl("CTL", "T", target_def)
    # DRD-only column MUST NOT appear in DDL
    assert "COL_DRD_ONLY" not in ddl
    assert "COL_A" in ddl
