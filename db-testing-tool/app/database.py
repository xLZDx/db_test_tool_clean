"""SQLAlchemy engine and session setup (async + sync)."""
from typing import Dict

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine
from app.config import settings

# Keep SQLite writes more resilient under concurrent API requests.
_sqlite_async_args = {"timeout": 30} if settings.DATABASE_URL.startswith("sqlite") else {}
_sqlite_sync_args = {"timeout": 30} if settings.SYNC_DATABASE_URL.startswith("sqlite") else {}

# Async engine for FastAPI endpoints
engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args=_sqlite_async_args)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# Sync engine for migrations / initial setup
sync_engine = create_engine(settings.SYNC_DATABASE_URL, echo=False, connect_args=_sqlite_sync_args)

class Base(DeclarativeBase):
    pass


REGRESSION_SQLITE_COLUMN_BACKFILL: Dict[str, Dict[str, str]] = {
    "regression_catalog_items": {
        "parent_suite_id": "INTEGER",
        "attachment_names_json": "TEXT",
        "attachment_text": "TEXT",
        "hyperlink_urls_json": "TEXT",
        "linked_requirement_ids_json": "TEXT",
        "linked_requirement_titles_json": "TEXT",
        "sql_candidates_json": "TEXT",
        "test_case_web_url": "TEXT",
        "test_plan_web_url": "TEXT",
        "test_suite_web_url": "TEXT",
        "created_date": "DATETIME",
        "changed_date": "DATETIME",
        "domain_group": "TEXT",
        "domain_context": "TEXT",
        "validation_status": "TEXT",
        "validation_score": "INTEGER",
        "validation_summary": "TEXT",
        "validation_details_json": "TEXT",
        "promoted_local_test_count": "INTEGER DEFAULT 0",
        "indexed_at": "DATETIME",
        "last_synced_at": "DATETIME",
    },
    "regression_lab_config": {
        "default_area_paths_json": "TEXT",
        "default_iteration_paths_json": "TEXT",
        "exclusion_keywords_json": "TEXT",
        "excluded_item_ids_json": "TEXT",
        "excluded_plan_ids_json": "TEXT",
        "excluded_suite_ids_json": "TEXT",
        "min_changed_date": "DATETIME",
        "include_archived": "BOOLEAN DEFAULT 0",
        "updated_at": "DATETIME",
    },
    # Phase 2: scope correction rules per DRD/DB + audit who confirmed a learned fix.
    "control_table_correction_rules": {
        "datasource_id": "INTEGER",
        "confirmed_by": "VARCHAR(255)",
    },
}


async def _backfill_sqlite_columns(conn) -> None:
    if not settings.DATABASE_URL.startswith("sqlite"):
        return

    for table_name, columns in REGRESSION_SQLITE_COLUMN_BACKFILL.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table_name})")
        existing_columns = {row[1] for row in result.fetchall()}
        if not existing_columns:
            continue
        for column_name, ddl in columns.items():
            if column_name in existing_columns:
                continue
            await conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    # Phase 2: after the datasource_id/confirmed_by columns exist, normalise legacy
    # issue_type NULLs to '' and ensure the datasource-scoped unique index exists.
    # (Legacy rows have datasource_id NULL -> SQLite treats NULLs as distinct, so
    # index creation never fails on pre-existing duplicates; new datasource-scoped
    # writes get real uniqueness.)
    res = await conn.exec_driver_sql("PRAGMA table_info(control_table_correction_rules)")
    ctcr_cols = {row[1] for row in res.fetchall()}
    if ctcr_cols:
        if "issue_type" in ctcr_cols:
            await conn.exec_driver_sql(
                "UPDATE control_table_correction_rules SET issue_type='' WHERE issue_type IS NULL"
            )
        if "datasource_id" in ctcr_cols:
            await conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ct_correction_rule "
                "ON control_table_correction_rules "
                "(datasource_id, target_table, target_column, issue_type)"
            )

async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session

async def init_db():
    """Create all tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _backfill_sqlite_columns(conn)
