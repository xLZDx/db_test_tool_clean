"""Schema provider — answers `has_column(schema, table, col)` for the
comparator-driven emitter's JOIN ON-clause validator.

Operator-locked architecture (2026-05-30 Phase 7.13):

  The emitter's Path 2 (DIM `<base>_ID = base.<base>_ID`) and Path 3.5
  (fact-extension `<base>_ID`) inference paths produce ON predicates
  WITHOUT verifying the predicted columns actually exist in either
  table.  Result: silent wrong SQL that fails at runtime with
  ORA-00904.

  This provider loads the PDM from `data/local_kb/schema_kb_ds_*.json`
  (the same files the comparator uses) so the emitter can verify EVERY
  ON-predicate column reference BEFORE emitting.  If validation fails,
  the emitter downgrades the JOIN to CROSS JOIN + TODO marker.

  Live-DB fallback (Phase 7.14, deferred): if PDM is incomplete OR
  caller supplies an oracledb connection, validate against ALL_TAB_COLUMNS.
  PDM-only is the safe default for offline operation.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set


_log = logging.getLogger(__name__)


class SchemaProvider:
    """Answers existence queries for schema.table.column triples.

    Backed by Git-LFS-pulled `schema_kb_ds_*.json` files in
    `data/local_kb/`.  Lookups are case-insensitive.  Missing schemas
    / tables / columns return False (NOT raise) so the emitter can
    fall back to CROSS JOIN cleanly.
    """

    def __init__(self, kb_dir: Optional[Path] = None,
                 preferred_ds_id: Optional[int] = None):
        if kb_dir is None:
            kb_dir = Path(__file__).resolve().parent.parent.parent / "data" / "local_kb"
        self._kb_dir = kb_dir
        # tables: dict[(schema_upper, table_upper)] -> set of column names (upper)
        self._tables: Dict[tuple, Set[str]] = {}
        # When preferred_ds_id is set (default from env var
        # PDM_PREFERRED_DS_ID), the PDM file for that DS is loaded
        # ALONE.  All other on-disk KBs are skipped.  Operator-locked
        # 2026-05-30 Phase 7.14: lets the live-regenerated PDM (ds_99)
        # take authoritative precedence over the legacy production
        # ds_3 dump when validating JOINs against live FREEPDB1.
        import os
        if preferred_ds_id is None:
            env_val = os.environ.get("PDM_PREFERRED_DS_ID", "").strip()
            if env_val.isdigit():
                preferred_ds_id = int(env_val)
        self._preferred_ds_id = preferred_ds_id
        self._loaded = False
        self._metadata_unavailable = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True  # mark first so we don't re-attempt on failure
        if not self._kb_dir.exists():
            _log.warning("SchemaProvider: KB dir %s does not exist", self._kb_dir)
            return
        # Apply preferred-DS filter: if set, only load that one file.
        if self._preferred_ds_id is not None:
            target = self._kb_dir / f"schema_kb_ds_{self._preferred_ds_id}.json"
            if target.exists():
                _log.info(
                    "SchemaProvider: loading ONLY preferred ds_%d (ignoring others)",
                    self._preferred_ds_id,
                )
                paths = [target]
            else:
                _log.warning(
                    "SchemaProvider: PDM_PREFERRED_DS_ID=%d but %s does not exist; "
                    "falling back to glob",
                    self._preferred_ds_id, target,
                )
                paths = sorted(self._kb_dir.glob("schema_kb_ds_*.json"))
        else:
            paths = sorted(self._kb_dir.glob("schema_kb_ds_*.json"))
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8")
                if text.lstrip().startswith("version https://git-lfs"):
                    _log.warning(
                        "SchemaProvider: %s is a Git LFS pointer (%d bytes); "
                        "run `git lfs pull` to enable JOIN validation against it.",
                        path.name, len(text),
                    )
                    self._metadata_unavailable = True
                    continue
                payload = json.loads(text)
            except (OSError, json.JSONDecodeError) as exc:
                _log.warning("SchemaProvider: cannot load %s: %s", path.name, exc)
                self._metadata_unavailable = True
                continue
            schemas = (payload.get("pdm") or {}).get("schemas") or []
            for sch_block in schemas:
                schema_name = (sch_block.get("schema") or "").strip().upper()
                if not schema_name:
                    continue
                for tbl_block in sch_block.get("tables") or []:
                    table_name = (tbl_block.get("name") or "").strip().upper()
                    if not table_name:
                        continue
                    cols = {
                        (c.get("name") or "").strip().upper()
                        for c in (tbl_block.get("columns") or [])
                        if c.get("name")
                    }
                    self._tables[(schema_name, table_name)] = cols
        _log.info(
            "SchemaProvider loaded %d tables from %s",
            len(self._tables), self._kb_dir,
        )

    def has_column(self, schema: str, table: str, col: str) -> bool:
        """Return True iff `schema.table.col` is recorded in any loaded PDM.

        Operator-locked semantics (Phase 7.16 round 2 fix):
        - If at least one KB loaded successfully AND table is missing: return
          False (strict — emitter must downgrade JOIN).
        - If NO KB could be loaded at all (Git LFS pointer dev-checkout, all
          files missing, parse errors on every file): emit a ONE-TIME WARNING
          and return True (legacy permissive behaviour preserved so dev
          checkouts without `git lfs pull` don't downgrade every JOIN).
          Operator can suppress permissive fallback via env var
          `PDM_STRICT_MODE=1` -- when set, missing tables ALWAYS return False
          regardless of LFS state, and the dev must run `git lfs pull` first.
        """
        self._load()
        key = (
            (schema or "").strip().upper(),
            (table or "").strip().upper(),
        )
        cols = self._tables.get(key)
        if cols is None:
            if self._tables:
                # At least one KB loaded; missing means missing.
                return False
            # No KB loaded at all -- permissive fallback.
            if self._metadata_unavailable:
                import os
                if os.environ.get("PDM_STRICT_MODE", "").strip() in {"1", "true", "yes"}:
                    return False
                self._warn_permissive_once()
                return True
            # KB dir exists but had zero files -- treat as strict (no LFS issue).
            return False
        c = (col or "").strip().upper().strip('"')
        return c in cols

    def _warn_permissive_once(self) -> None:
        if getattr(self, "_warned_permissive", False):
            return
        self._warned_permissive = True
        _log.warning(
            "SchemaProvider in PERMISSIVE FALLBACK MODE: no KB loaded (LFS "
            "pointers / parse errors); has_column() returns True for unknown "
            "tables.  This degrades JOIN-validation correctness.  Run "
            "`git lfs pull` OR set PDM_STRICT_MODE=1 to fail-loud."
        )

    def has_table(self, schema: str, table: str) -> bool:
        self._load()
        key = (
            (schema or "").strip().upper(),
            (table or "").strip().upper(),
        )
        return key in self._tables

    def columns_for(self, schema: str, table: str) -> Set[str]:
        self._load()
        return set(self._tables.get(
            ((schema or "").upper(), (table or "").upper()), ()
        ))

    def candidate_fk_columns(self, schema: str, table: str, base_bare: str) -> list:
        """Return candidate FK column names that EXIST in
        `schema.table` and look like FKs to `base_bare`.  Tried in
        priority order:

          1. `<base_bare>_ID`     (e.g. TXN_ID)
          2. `<base_bare>`        (e.g. TXN)
          3. `<base_bare>_KEY`
          4. `<base_bare>_CD`
          5. `<base_bare>_CODE`
          6. literal "ID" (often used as PK on lookup tables)
        """
        self._load()
        cols = self.columns_for(schema, table)
        if not cols:
            return []
        candidates = [
            f"{base_bare}_ID",
            f"{base_bare}",
            f"{base_bare}_KEY",
            f"{base_bare}_CD",
            f"{base_bare}_CODE",
            "ID",
        ]
        return [c for c in candidates if c.upper() in cols]


# Module-level singleton -- emitter import is cheap; KB load is lazy
# on first lookup.
_default_provider: Optional[SchemaProvider] = None


def default_provider() -> SchemaProvider:
    global _default_provider
    if _default_provider is None:
        _default_provider = SchemaProvider()
    return _default_provider


def validate_on_predicate(
    on_sql: str,
    alias_to_fq: Dict[str, str],
    provider: Optional[SchemaProvider] = None,
) -> tuple:
    """Validate every `<alias>.<col>` reference in `on_sql` against the
    provider.  Returns `(valid: bool, failures: list[(alias, col, reason)])`.

    `alias_to_fq` maps alias -> "schema.table".  Aliases NOT in the map
    are skipped (unknown — could be operator-supplied; don't false-fail).
    Quoted columns ("CHECK") are unquoted before lookup.
    """
    if provider is None:
        provider = default_provider()
    if not on_sql:
        return True, []
    failures: list = []
    ref_re = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$#]*)\.(\"[A-Z0-9_]+\"|[A-Za-z_][A-Za-z0-9_$#]*)")
    for m in ref_re.finditer(on_sql):
        alias = m.group(1).upper()
        col = m.group(2).strip('"').upper()
        fq = alias_to_fq.get(alias)
        if not fq:
            continue
        if "." in fq:
            sch, tab = fq.split(".", 1)
        else:
            sch, tab = "", fq
        if not provider.has_table(sch, tab):
            continue  # table not in PDM -> can't validate; allow.
        if not provider.has_column(sch, tab, col):
            failures.append((alias, col, f"column not in {sch}.{tab}"))
    return (len(failures) == 0, failures)
