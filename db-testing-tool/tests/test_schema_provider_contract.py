from __future__ import annotations

import json

from app.sql_model.schema_provider import SchemaProvider


def test_schema_provider_has_column_returns_false_for_missing_table(tmp_path):
    kb = tmp_path / "schema_kb_ds_1.json"
    kb.write_text(json.dumps({"sources": []}), encoding="utf-8")

    provider = SchemaProvider(kb_dir=tmp_path)

    assert provider.has_table("S", "T") is False
    assert provider.has_column("S", "T", "C") is False


def test_schema_provider_has_column_true_only_for_present_column(tmp_path):
    kb = tmp_path / "schema_kb_ds_1.json"
    kb.write_text(
        json.dumps({
            "pdm": {
                "schemas": [
                    {
                        "schema": "APP",
                        "tables": [
                            {"name": "CUSTOMER", "columns": [{"name": "ID"}]}
                        ],
                    }
                ]
            }
        }),
        encoding="utf-8",
    )

    provider = SchemaProvider(kb_dir=tmp_path)

    assert provider.has_table("APP", "CUSTOMER") is True
    assert provider.has_column("APP", "CUSTOMER", "ID") is True
    assert provider.has_column("APP", "CUSTOMER", "MISSING") is False
