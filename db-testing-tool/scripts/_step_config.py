"""Shared CLI + env config for step2/step3/step5 live-test scripts.

Phase 7.18 (operator 2026-06-01): the step scripts used to hardcode
- `sys / 123456 / SYSDBA` credentials (security risk if pushed)
- `IKOROSTELEV.AVY_FACT_SIDE` target table
- `CCAL_REPL_OWNER.TXN` base table
- `LOADER_TS` tablespace

Per the operator's universal-tool requirement + memory rule
``feedback_db_tool_generic_shared_rule_engine.md``: all DRD-driven rules
live in ONE shared module, used by every consumer, with NO hardcoded
table / schema / domain names.

This module centralises:
  * Oracle connection (env-var first, CLI override, last-resort defaults)
  * Standard CLI args for any step that talks to the live DB
  * Snapshot table name derivation (`<target>_DRD`, `<target>_ODI`)

Operator-locked: defaults preserve current FREEPDB1 dev workflow for
back-compat; production / customer runs MUST set env vars or CLI flags.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional


@dataclass
class StepConfig:
    """All parameters a step script needs to talk to the live DB."""
    # Connection
    dsn: str
    user: str
    password: str
    mode: str  # "DEFAULT" | "SYSDBA" | "SYSOPER"
    # Target table (what the INSERT populates)
    target_schema: str
    target_table: str
    # Snapshot tables (where to copy after INSERT for comparison)
    snapshot_schema: str
    snapshot_table_drd: str   # = <target>_DRD by default
    snapshot_table_odi: str   # = <target>_ODI by default
    # Base table (the FROM root of the INSERT body)
    base_schema: str
    base_table: str
    base_alias: str
    # Row limit + tablespace + optional input override
    row_limit: int
    tablespace: Optional[str]
    drd_input_sql_path: Optional[str]   # override for `data/api_runs/DRD_DRIVEN_INSERT.sql`

    def oracle_mode(self):
        """Return oracledb.SYSDBA / oracledb.SYSOPER / None for the mode."""
        import oracledb
        m = (self.mode or "").strip().upper()
        if m == "SYSDBA":
            return oracledb.SYSDBA
        if m == "SYSOPER":
            return oracledb.SYSOPER
        return None  # DEFAULT


def add_common_args(parser: argparse.ArgumentParser) -> None:
    """Attach the standard step-script flags.  Defaults intentionally match
    the historic FREEPDB1 + AVY_FACT_SIDE workflow so existing scripts
    keep working without flags.  Env vars override defaults; CLI flags
    override env vars."""
    parser.add_argument("--dsn",
        default=os.environ.get("ORA_LIVE_DSN", "localhost:1521/FREEPDB1"),
        help="Oracle DSN (default: env ORA_LIVE_DSN or localhost:1521/FREEPDB1)")
    parser.add_argument("--user",
        default=os.environ.get("ORA_LIVE_USER", "sys"),
        help="Oracle user (default: env ORA_LIVE_USER or sys)")
    parser.add_argument("--password",
        default=os.environ.get("ORA_LIVE_PASSWORD", "123456"),
        help="Oracle password (default: env ORA_LIVE_PASSWORD or 123456 -- "
             "OVERRIDE FOR PRODUCTION)")
    parser.add_argument("--mode",
        default=os.environ.get("ORA_LIVE_MODE", "SYSDBA"),
        choices=["DEFAULT", "SYSDBA", "SYSOPER"],
        help="Connection mode (default: env ORA_LIVE_MODE or SYSDBA)")
    parser.add_argument("--target-schema",
        default=os.environ.get("DBT_TARGET_SCHEMA", "IKOROSTELEV"),
        help="Target table schema (default: env DBT_TARGET_SCHEMA or IKOROSTELEV)")
    parser.add_argument("--target-table",
        default=os.environ.get("DBT_TARGET_TABLE", "AVY_FACT_SIDE"),
        help="Target table (default: env DBT_TARGET_TABLE or AVY_FACT_SIDE)")
    parser.add_argument("--snapshot-schema",
        default=os.environ.get("DBT_SNAPSHOT_SCHEMA", ""),
        help="Snapshot schema (default: same as --target-schema)")
    parser.add_argument("--snapshot-suffix-drd",
        default=os.environ.get("DBT_SNAPSHOT_SUFFIX_DRD", "_DRD"),
        help="Snapshot table suffix for DRD-side (default: _DRD)")
    parser.add_argument("--snapshot-suffix-odi",
        default=os.environ.get("DBT_SNAPSHOT_SUFFIX_ODI", "_ODI"),
        help="Snapshot table suffix for ODI-side (default: _ODI)")
    parser.add_argument("--base-schema",
        default=os.environ.get("DBT_BASE_SCHEMA", "CCAL_REPL_OWNER"),
        help="Base table schema (default: env DBT_BASE_SCHEMA or CCAL_REPL_OWNER)")
    parser.add_argument("--base-table",
        default=os.environ.get("DBT_BASE_TABLE", "TXN"),
        help="Base table (default: env DBT_BASE_TABLE or TXN)")
    parser.add_argument("--base-alias",
        default=os.environ.get("DBT_BASE_ALIAS", "t"),
        help="Base alias used by emitter SQL (default: env DBT_BASE_ALIAS or t)")
    parser.add_argument("--row-limit",
        type=int, default=int(os.environ.get("DBT_ROW_LIMIT", "500")),
        help="ROWNUM cap for the INSERT (default: env DBT_ROW_LIMIT or 500)")
    parser.add_argument("--tablespace",
        default=os.environ.get("DBT_TABLESPACE", "LOADER_TS"),
        help="Tablespace for CREATE TABLE snapshots (default: LOADER_TS; "
             "empty string disables the TABLESPACE clause)")
    parser.add_argument("--drd-input-sql",
        default=os.environ.get("DBT_DRD_INPUT_SQL", ""),
        help="Path to DRD INSERT SQL (default: data/api_runs/DRD_DRIVEN_INSERT.sql)")


def parse_args_to_config(parser: argparse.ArgumentParser) -> StepConfig:
    args = parser.parse_args()
    snapshot_schema = (args.snapshot_schema or args.target_schema).upper()
    return StepConfig(
        dsn=args.dsn,
        user=args.user,
        password=args.password,
        mode=(args.mode or "DEFAULT").upper(),
        target_schema=args.target_schema.upper(),
        target_table=args.target_table.upper(),
        snapshot_schema=snapshot_schema,
        snapshot_table_drd=f"{args.target_table.upper()}{args.snapshot_suffix_drd.upper()}",
        snapshot_table_odi=f"{args.target_table.upper()}{args.snapshot_suffix_odi.upper()}",
        base_schema=args.base_schema.upper(),
        base_table=args.base_table.upper(),
        base_alias=args.base_alias,
        row_limit=int(args.row_limit),
        tablespace=(args.tablespace or None),
        drd_input_sql_path=(args.drd_input_sql or None),
    )


def open_connection(cfg: StepConfig):
    """Open Oracle connection using cfg.user/password/dsn/mode."""
    import oracledb
    kw = {"user": cfg.user, "password": cfg.password, "dsn": cfg.dsn}
    om = cfg.oracle_mode()
    if om is not None:
        kw["mode"] = om
    return oracledb.connect(**kw)


def print_config_banner(cfg: StepConfig, script_name: str) -> None:
    """Print the resolved config so the operator sees exactly what runs."""
    print(f"=== {script_name} ===")
    print(f"  DB: {cfg.user}@{cfg.dsn} mode={cfg.mode}")
    print(f"  Target: {cfg.target_schema}.{cfg.target_table}")
    print(f"  Snapshots: {cfg.snapshot_schema}.{cfg.snapshot_table_drd} + "
          f"{cfg.snapshot_schema}.{cfg.snapshot_table_odi}")
    print(f"  Base:   {cfg.base_schema}.{cfg.base_table} alias={cfg.base_alias}")
    print(f"  Row limit: {cfg.row_limit}  Tablespace: {cfg.tablespace or '(none)'}")
    if cfg.drd_input_sql_path:
        print(f"  DRD input SQL: {cfg.drd_input_sql_path}")
    print()
