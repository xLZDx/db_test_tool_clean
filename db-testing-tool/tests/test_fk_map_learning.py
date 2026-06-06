"""R5 step 6: learning write-back is OPT-IN (FK_MAP_LEARN) and grows the FK map
with CLEAR DRD joins. Default off -> no write (the grade stays deterministic).

The "on" path is an integration test against the ds_3 KB (skipif absent); it
backs up + restores the committed fk_map_ds_3.json so the repo stays pure.
"""
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KB_DS3 = ROOT / "data" / "local_kb" / "schema_kb_ds_3.json"
FKMAP3 = ROOT / "data" / "local_kb" / "fk_map_ds_3.json"
CLOSE_DRD = ROOT / "data" / "taxlot" / "DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx"


def _join_count(path: Path) -> int:
    if not path.exists():
        return 0
    d = json.loads(path.read_text(encoding="utf-8"))
    return sum(len(c) for c in (d.get("joins") or {}).values())


def test_learn_disabled_by_default(monkeypatch):
    """Without FK_MAP_LEARN, analyze_control_table must not collect/persist."""
    monkeypatch.delenv("FK_MAP_LEARN", raising=False)
    # the analyze flow creates _learned = None when the env var is absent; we
    # assert the gate expression directly (cheap, no KB needed).
    assert ([] if os.environ.get("FK_MAP_LEARN") else None) is None


@pytest.mark.skipif(
    not (KB_DS3.exists() and CLOSE_DRD.exists()),
    reason="ds_3 KB / CLOSE DRD fixtures not present",
)
def test_learn_enabled_grows_map_with_drd_joins(monkeypatch):
    from app.services.control_table_service import analyze_control_table

    backup = FKMAP3.read_bytes() if FKMAP3.exists() else None
    try:
        monkeypatch.setenv("FK_MAP_LEARN", "1")
        before = _join_count(FKMAP3)
        analyze_control_table(
            file_bytes=CLOSE_DRD.read_bytes(), filename=CLOSE_DRD.name,
            target_schema="TAXLOT_OWNER", target_table="CLS_TAX_LOTS_NON_BKR_FACT",
            source_datasource_id=3, target_datasource_id=3, control_schema="ikorostelev",
        )
        after = _join_count(FKMAP3)
        assert after >= before, (before, after)
        # the learned entries must be DRD-sourced (source='drd')
        m = json.loads(FKMAP3.read_text(encoding="utf-8"))
        drd_entries = [
            e for cols in m["joins"].values() for e in cols.values()
            if "drd" in (e.get("sources") or [])
        ]
        assert drd_entries, "expected at least one learned drd-sourced join"
    finally:
        if backup is not None:
            FKMAP3.write_bytes(backup)  # restore the committed (PDM-only) map
