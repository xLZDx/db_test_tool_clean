"""
One-shot migration: add missing columns and create new tables.
Run with: python app/_db_migrate.py
"""
import sqlite3
import sys
import os

# Resolve the DB path the same way config.py does
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import settings

db_path = settings.SYNC_DATABASE_URL.replace("sqlite:///", "")
print(f"DB: {db_path}")

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 1. Find all existing tables
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
existing_tables = {r[0] for r in cur.fetchall()}
print(f"Existing tables: {sorted(existing_tables)}")

# 2. Add missing columns to test_cases
cur.execute("PRAGMA table_info(test_cases)")
tc_cols = {r[1] for r in cur.fetchall()}
test_cases_new_cols = [
    ("mapping_table",  "TEXT"),
    ("source_filter",  "TEXT"),
    ("target_filter",  "TEXT"),
]
for col, typ in test_cases_new_cols:
    if col not in tc_cols:
        cur.execute(f"ALTER TABLE test_cases ADD COLUMN {col} {typ}")
        print(f"  + test_cases.{col}")

# 3. Create new tables if they don't exist
new_ddl = [
    (
        "test_folders",
        "CREATE TABLE IF NOT EXISTS test_folders ("
        "  id INTEGER PRIMARY KEY, "
        "  name TEXT NOT NULL UNIQUE"
        ")",
    ),
    (
        "test_case_folders",
        "CREATE TABLE IF NOT EXISTS test_case_folders ("
        "  id INTEGER PRIMARY KEY, "
        "  test_case_id INTEGER NOT NULL REFERENCES test_cases(id) ON DELETE CASCADE, "
        "  folder_id    INTEGER NOT NULL REFERENCES test_folders(id) ON DELETE CASCADE, "
        "  UNIQUE(test_case_id, folder_id)"
        ")",
    ),
    (
        "control_table_correction_rules",
        "CREATE TABLE IF NOT EXISTS control_table_correction_rules ("
        "  id INTEGER PRIMARY KEY, "
        "  target_table TEXT NOT NULL, "
        "  target_column TEXT NOT NULL, "
        "  issue_type TEXT, "
        "  source_attribute TEXT, "
        "  recommended_source TEXT, "
        "  replacement_expression TEXT, "
        "  notes TEXT, "
        "  created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
        "  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")",
    ),
    (
        "control_table_file_states",
        "CREATE TABLE IF NOT EXISTS control_table_file_states ("
        "  id INTEGER PRIMARY KEY, "
        "  file_path TEXT NOT NULL UNIQUE, "
        "  file_hash TEXT, "
        "  is_processed INTEGER DEFAULT 0, "
        "  processed_at DATETIME, "
        "  notes TEXT"
        ")",
    ),
    (
        "tfs_test_plans",
        "CREATE TABLE IF NOT EXISTS tfs_test_plans ("
        "  id INTEGER PRIMARY KEY, "
        "  plan_id INTEGER NOT NULL, "
        "  name TEXT NOT NULL DEFAULT '', "
        "  project TEXT NOT NULL, "
        "  state TEXT DEFAULT 'Active', "
        "  description TEXT, "
        "  area_path TEXT, "
        "  iteration_path TEXT, "
        "  owner TEXT, "
        "  created_date DATETIME, "
        "  root_suite_id INTEGER, "
        "  last_synced_at DATETIME, "
        "  UNIQUE(plan_id, project)"
        ")",
    ),
    (
        "tfs_test_suites",
        "CREATE TABLE IF NOT EXISTS tfs_test_suites ("
        "  id INTEGER PRIMARY KEY, "
        "  suite_id INTEGER NOT NULL, "
        "  plan_id INTEGER NOT NULL, "
        "  parent_suite_id INTEGER, "
        "  name TEXT NOT NULL DEFAULT '', "
        "  project TEXT NOT NULL, "
        "  suite_type TEXT DEFAULT 'StaticTestSuite', "
        "  test_case_count INTEGER DEFAULT 0, "
        "  is_heavy INTEGER DEFAULT 0, "
        "  last_synced_at DATETIME, "
        "  UNIQUE(suite_id, project)"
        ")",
    ),
    (
        "tfs_test_points",
        "CREATE TABLE IF NOT EXISTS tfs_test_points ("
        "  id INTEGER PRIMARY KEY, "
        "  test_point_id INTEGER NOT NULL, "
        "  test_case_id INTEGER, "
        "  suite_id INTEGER, "
        "  plan_id INTEGER, "
        "  project TEXT NOT NULL, "
        "  title TEXT, "
        "  description TEXT, "
        "  state TEXT DEFAULT 'Active', "
        "  priority INTEGER DEFAULT 3, "
        "  automation_status TEXT, "
        "  owner TEXT, "
        "  last_synced_at DATETIME, "
        "  UNIQUE(test_point_id, project)"
        ")",
    ),
    (
        "tfs_test_runs",
        "CREATE TABLE IF NOT EXISTS tfs_test_runs ("
        "  id INTEGER PRIMARY KEY, "
        "  run_id INTEGER NOT NULL UNIQUE, "
        "  plan_id INTEGER, "
        "  name TEXT, "
        "  project TEXT NOT NULL, "
        "  environment TEXT, "
        "  state TEXT DEFAULT 'NotStarted', "
        "  total_tests INTEGER DEFAULT 0, "
        "  passed_count INTEGER DEFAULT 0, "
        "  failed_count INTEGER DEFAULT 0, "
        "  blocked_count INTEGER DEFAULT 0, "
        "  not_run_count INTEGER DEFAULT 0, "
        "  test_point_ids TEXT, "
        "  started_at DATETIME, "
        "  completed_at DATETIME"
        ")",
    ),
    (
        "tfs_test_results",
        "CREATE TABLE IF NOT EXISTS tfs_test_results ("
        "  id INTEGER PRIMARY KEY, "
        "  run_id INTEGER NOT NULL, "
        "  test_point_id INTEGER, "
        "  test_case_id INTEGER, "
        "  outcome TEXT, "
        "  comment TEXT, "
        "  error_message TEXT, "
        "  duration_ms INTEGER DEFAULT 0, "
        "  state TEXT DEFAULT 'Active'"
        ")",
    ),
]

for tbl_name, ddl in new_ddl:
    cur.execute(ddl)
    status = "created" if tbl_name not in existing_tables else "already exists"
    print(f"  {tbl_name}: {status}")

# 3b. Migrate control_table_file_states to new schema if still on old layout.
#     Old schema had: file_path, file_hash, is_processed, processed_at, notes
#     New schema needs: target_table, file_fingerprint, file_name, final_insert_sql, last_applied_decisions
cur.execute("PRAGMA table_info(control_table_file_states)")
ctfs_cols = {r[1] for r in cur.fetchall()}
if "file_path" in ctfs_cols and "target_table" not in ctfs_cols:
    # Drop old table and recreate with new schema (no valuable data to preserve)
    cur.execute("DROP TABLE control_table_file_states")
    cur.execute(
        "CREATE TABLE control_table_file_states ("
        "  id INTEGER PRIMARY KEY, "
        "  target_table TEXT NOT NULL, "
        "  file_fingerprint TEXT NOT NULL, "
        "  file_name TEXT, "
        "  final_insert_sql TEXT NOT NULL, "
        "  last_applied_decisions TEXT, "
        "  UNIQUE(target_table, file_fingerprint)"
        ")"
    )
    print("  control_table_file_states: migrated to new schema (target_table, file_fingerprint, final_insert_sql)")
elif "target_table" not in ctfs_cols:
    # Fresh table with correct schema
    cur.execute("DROP TABLE IF EXISTS control_table_file_states")
    cur.execute(
        "CREATE TABLE control_table_file_states ("
        "  id INTEGER PRIMARY KEY, "
        "  target_table TEXT NOT NULL, "
        "  file_fingerprint TEXT NOT NULL, "
        "  file_name TEXT, "
        "  final_insert_sql TEXT NOT NULL, "
        "  last_applied_decisions TEXT, "
        "  UNIQUE(target_table, file_fingerprint)"
        ")"
    )
    print("  control_table_file_states: recreated with correct schema")

# 4. Add agent_profiles table if not present (agent model stub used __tablename__ = "agent_profiles")
if "agent_profiles" not in existing_tables:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS agent_profiles ("
        "  id INTEGER PRIMARY KEY, "
        "  name TEXT NOT NULL, "
        "  role TEXT, "
        "  domains TEXT, "
        "  system_prompt TEXT, "
        "  is_active INTEGER DEFAULT 1"
        ")"
    )
    print("  agent_profiles: created")

conn.commit()
conn.close()
print("\nMigration complete.")
