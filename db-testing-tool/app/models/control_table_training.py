"""Control table training models."""
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
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

    id = Column(Integer, primary_key=True)
    target_table = Column(String(255), nullable=False)
    target_column = Column(String(255), nullable=False)
    issue_type = Column(String(100), nullable=True)
    source_attribute = Column(String(255), nullable=True)
    recommended_source = Column(String(100), nullable=True)
    replacement_expression = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
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
