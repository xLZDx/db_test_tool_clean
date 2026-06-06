"""Control table training models."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, UniqueConstraint
from sqlalchemy.sql import func
from app.database import Base


class ControlTableTraining(Base):
    __tablename__ = "control_table_training"

    id = Column(Integer, primary_key=True)
    target_table = Column(String(255), nullable=False)
    training_data = Column(Text, nullable=True)
    rules_count = Column(Integer, default=0)


class ControlTableCorrectionRule(Base):
    __tablename__ = "control_table_correction_rules"
    # Phase 2: a learned correction rule auto-applies to FUTURE generations, so it
    # MUST be scoped per datasource/DRD (a fix learned on AVY must not leak to
    # CLOSE/OPEN). datasource_id is part of the natural key; the DB-level unique
    # constraint replaces the prior select-then-insert-only guard (race -> dup rows).
    __table_args__ = (
        UniqueConstraint(
            "datasource_id", "target_table", "target_column", "issue_type",
            name="uq_ct_correction_rule",
        ),
    )

    id = Column(Integer, primary_key=True)
    datasource_id = Column(Integer, nullable=True)  # Phase 2: scope per DRD/DB
    target_table = Column(String(255), nullable=False)
    target_column = Column(String(255), nullable=False)
    issue_type = Column(String(100), nullable=False, server_default="", default="")
    source_attribute = Column(String(255), nullable=True)
    recommended_source = Column(String(100), nullable=True)
    replacement_expression = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    confirmed_by = Column(String(255), nullable=True)  # Phase 2: audit of learned fix
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ControlTableFileState(Base):
    __tablename__ = "control_table_file_states"

    id = Column(Integer, primary_key=True)
    target_table = Column(String(255), nullable=False)
    file_fingerprint = Column(String(64), nullable=False)
    file_name = Column(String(1024), nullable=True)
    final_insert_sql = Column(Text, nullable=False)
    last_applied_decisions = Column(Text, nullable=True)
