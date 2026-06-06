"""Phase 2 chunk 1: ControlTableCorrectionRule datasource-scope migration.

Verifies the model shape (datasource_id + confirmed_by + non-null issue_type +
datasource-scoped unique constraint), that the constraint is enforced per
datasource (a fix learned on one DRD cannot collide with another), and that the
legacy backfill (ALTER + issue_type NULL->'' + unique index) is safe on an old
table that has duplicate NULL-datasource rows.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models.control_table_training import ControlTableCorrectionRule


def test_model_has_phase2_columns_and_unique_constraint():
    cols = ControlTableCorrectionRule.__table__.columns
    assert "datasource_id" in cols
    assert "confirmed_by" in cols
    assert cols["issue_type"].nullable is False
    uqs = [
        c for c in ControlTableCorrectionRule.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert uqs, "no UniqueConstraint on ControlTableCorrectionRule"
    cols_in_uq = {col.name for uq in uqs for col in uq.columns}
    assert {"datasource_id", "target_table", "target_column", "issue_type"} <= cols_in_uq


def test_unique_constraint_enforced_per_datasource(tmp_path: Path):
    eng = create_engine(f"sqlite:///{tmp_path/'mig.db'}")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        s.add(ControlTableCorrectionRule(datasource_id=1, target_table="T", target_column="C", issue_type=""))
        s.commit()
        # same 4-tuple -> rejected
        s.add(ControlTableCorrectionRule(datasource_id=1, target_table="T", target_column="C", issue_type=""))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # different datasource -> allowed (scope isolation)
        s.add(ControlTableCorrectionRule(datasource_id=2, target_table="T", target_column="C", issue_type=""))
        s.commit()
        n = s.query(ControlTableCorrectionRule).filter_by(target_table="T", target_column="C").count()
        assert n == 2


def test_legacy_backfill_is_safe_with_dup_null_rows(tmp_path: Path):
    """Old table (no datasource_id/confirmed_by, nullable issue_type) with two
    duplicate NULL-datasource rows must migrate without error: columns added,
    issue_type backfilled, unique index created (NULLs distinct -> no clash)."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE control_table_correction_rules ("
        "  id INTEGER PRIMARY KEY, target_table TEXT NOT NULL, target_column TEXT NOT NULL,"
        "  issue_type TEXT, source_attribute TEXT, recommended_source TEXT,"
        "  replacement_expression TEXT, notes TEXT,"
        "  created_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    cur.execute("INSERT INTO control_table_correction_rules (target_table,target_column,issue_type) VALUES ('T','C',NULL)")
    cur.execute("INSERT INTO control_table_correction_rules (target_table,target_column,issue_type) VALUES ('T','C',NULL)")
    conn.commit()

    # Mirror the migration (database.py _backfill + _db_migrate.py).
    cur.execute("PRAGMA table_info(control_table_correction_rules)")
    have = {r[1] for r in cur.fetchall()}
    if "datasource_id" not in have:
        cur.execute("ALTER TABLE control_table_correction_rules ADD COLUMN datasource_id INTEGER")
    if "confirmed_by" not in have:
        cur.execute("ALTER TABLE control_table_correction_rules ADD COLUMN confirmed_by TEXT")
    cur.execute("UPDATE control_table_correction_rules SET issue_type='' WHERE issue_type IS NULL")
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_ct_correction_rule "
        "ON control_table_correction_rules (datasource_id, target_table, target_column, issue_type)"
    )
    conn.commit()

    cur.execute("PRAGMA table_info(control_table_correction_rules)")
    cols = {r[1] for r in cur.fetchall()}
    assert {"datasource_id", "confirmed_by"} <= cols
    cur.execute("SELECT DISTINCT issue_type FROM control_table_correction_rules")
    assert cur.fetchall() == [("",)]  # all backfilled to ''
    cur.execute("SELECT count(*) FROM sqlite_master WHERE type='index' AND name='uq_ct_correction_rule'")
    assert cur.fetchone()[0] == 1
    # legacy NULL-datasource dups survive (NULLs distinct) -- no data loss on migrate
    cur.execute("SELECT count(*) FROM control_table_correction_rules")
    assert cur.fetchone()[0] == 2
    conn.close()
