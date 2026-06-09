"""
Pytest checks for AVY v18 generated insert package.

Run from the root of this extracted ZIP package:

    python -m pytest 04_validation_tests/test_avy_artifacts.py

These tests validate artifact contract, not Oracle runtime execution.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def read_csv(rel: str):
    with (ROOT / rel).open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_generated_insert_exists_and_is_not_empty():
    sql = ROOT / "01_generated_insert" / "generated_insert_select_candidate.sql"
    assert sql.exists()
    text = sql.read_text(encoding="utf-8", errors="ignore")
    assert len(text) > 1000
    assert "INSERT" in text.upper()
    assert "SELECT" in text.upper()


def test_final_consistency_summary_clean_for_avy():
    summary = read_json("05_raw_validation_artifacts/final_consistency_summary.json")
    assert summary.get("mapping_rows") == 373
    assert summary.get("validation_error_counts") in ({}, None)
    assert summary.get("schema_kb_validation_error_counts") in ({}, None)


def test_hardcode_gate_passed():
    gate = read_json("05_raw_validation_artifacts/hardcode_gate_report.json")
    assert gate.get("status") == "PASS"
    assert gate.get("finding_count") == 0


def test_tri_compare_all_generated_rows_match_drd_contract():
    rows = read_csv("05_raw_validation_artifacts/tri_compare_report.csv")
    assert len(rows) == 373
    bad = [
        r for r in rows
        if r.get("same_mismatch_as_drd_odi", "") not in {"Y", ""}
    ]
    assert not bad, f"Rows with unexpected same_mismatch_as_drd_odi: {bad[:5]}"


def test_api_manifest_exists_and_reports_business_status():
    manifest = read_json("03_api_reports/manifest.json")
    assert manifest.get("process_status") == "ARTIFACTS_GENERATED"
    assert "business_status" in manifest
    assert manifest.get("reports", {}).get("step4", {}).get("json")
