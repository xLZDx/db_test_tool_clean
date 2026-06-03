"""ODI XML scenario export -> ODIModel parser.

Design rules (consensus 2026-05-28):
- No live Oracle access.  Everything resolved offline.
- No regex renaming of existing SQL — parse then emit.
- Illegal states are unrepresentable: every alias resolves to one TableRef;
  unresolved expressions become UnresolvedExpr (never emitted as SQL).
- Strike-through rows are ignored upstream (drd_import_service.py:877-883).
- :GLOBAL.GV_* bind vars -> meaningful literals (operator rule 2026-05-28).
- odiRef.getObjectName staging vars (#SSDS.*) -> plain staging table names.

The parser produces an ODIModel containing:
  - One StagingStep per STEP_INSERT block (STEP1..STEP5)
  - Each StagingStep carries: source_bindings, join_graph, column_mappings,
    select_sql (template-resolved)
  - final_insert_columns from the MERGE block
  - final_select_sql = resolved MERGE SQL for fidelity
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional

_logger = logging.getLogger(__name__)

from app.sql_model.odi_template_resolver import resolve_odi_templates
from app.sql_model.types import (
    AliasBinding,
    AliasConflictError,
    ColumnMapping,
    JoinEdge,
    JoinType,
    ODIModel,
    Provenance,
    ResolvedColumn,
    StagingStep,
    TableRef,
    UnresolvedExpr,
    build_alias_map,
)

# ── Defaults ──────────────────────────────────────────────────────────────────
# Phase 7.19.24 (2026-06-02): NO scenario-specific default target.  The target
# is AUTO-DETECTED from the ODI scenario's integration INSERT/MERGE INTO clause
# (was hardcoded to AVY_FACT_SIDE, so every other file emitted "INSERT INTO
# IKOROSTELEV.AVY_FACT_SIDE").  An explicit caller target still wins; blank ->
# auto-detect.  (Re-applied 2026-06-03 after it was lost in a git reset.)
_DEFAULT_TARGET_SCHEMA = ""
_DEFAULT_TARGET_TABLE = ""

# ── Block classification patterns ─────────────────────────────────────────────
_RE_STEP_NUM = re.compile(r"SSDS_AVY_FACT_STEP(\d)_STG", re.I)
_RE_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
_RE_MERGE_INTO = re.compile(r"\bMERGE\s+INTO\b", re.I)
_RE_DECLARE_BEGIN = re.compile(r"^\s*(?:DECLARE|BEGIN)\b", re.I)
_RE_INSERT_INTO = re.compile(r"\bINSERT\b[^;]{0,200}?\bINTO\b", re.I | re.DOTALL)

# Extract the target schema.table from an integration block's INSERT/MERGE INTO
# clause (e.g. "INSERT /*+ APPEND */ INTO TAXLOTS_OWNER.CLS_TAX_LOTS_NON_BKR_FACT
# (...)" -> ("TAXLOTS_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT")).  Tolerates the
# optimizer hint before INTO.
_RE_INTO_TARGET = re.compile(
    r"\b(?:MERGE|INSERT)\b\s*(?:/\*.*?\*/)?\s*\bINTO\s+([A-Z][A-Z0-9_$#]*)(?:\.([A-Z][A-Z0-9_$#]*))?",
    re.I | re.DOTALL,
)


def _extract_into_target(resolved_sql: str) -> tuple[str, str]:
    """Return (schema, table) from the first INSERT/MERGE INTO of a resolved
    integration block.  ('', '') if none.  Caller passes ONLY integration
    blocks (MERGE / final INSERT) -- never utility/log blocks -- so this does
    not pick up an SSDS_SESS_LOG-style logging insert."""
    m = _RE_INTO_TARGET.search(resolved_sql or "")
    if not m:
        return ("", "")
    g1, g2 = m.group(1), m.group(2)
    if g2:
        return (g1.upper(), g2.upper())
    return ("", g1.upper())


def _classify_block(raw: str) -> str:
    """Return block classification string for routing (by CONTENT).

    Phase 7.19.18 (2026-06-02): the classifier no longer requires the
    AVY_FACT-specific ``SSDS_AVY_FACT_STEP<n>_STG`` naming.  MERGE-based
    IKMs (e.g. "IKM Oracle Incremental Update Merge", used by the taxlot
    scenarios) carry their full column mapping in the MERGE itself, with no
    STEP_INSERT staging blocks.  Classifying on content -- MERGE INTO -> a
    MERGE, CREATE TABLE -> STEP_DDL, INSERT INTO -> STEP_INSERT -- works for
    ANY scenario, not just AVY_FACT.  AVY behaviour is preserved: a STEP_INSERT
    block whose step number cannot be extracted is skipped in _build_model
    (n is None), and STEP_DDL is a no-op there -- exactly as before when those
    blocks fell through to UTILITY.
    """
    has_create = bool(_RE_CREATE_TABLE.search(raw))
    has_merge = bool(_RE_MERGE_INTO.search(raw))
    has_insert = bool(_RE_INSERT_INTO.search(raw))

    # MERGE first: a MERGE body legitimately contains INSERT (WHEN NOT
    # MATCHED) and may mention CREATE inside its USING subquery; it must not
    # be misrouted to STEP_INSERT/STEP_DDL.
    if has_merge:
        return "MERGE"
    if has_create:
        return "STEP_DDL"
    if has_insert:
        return "STEP_INSERT"
    return "UTILITY"


def _step_num(raw: str) -> Optional[int]:
    m = _RE_STEP_NUM.search(raw)
    return int(m.group(1)) if m else None


# ── Low-level SQL text extraction ─────────────────────────────────────────────

def _split_at_depth_zero(text: str, sep: str = ",") -> list[str]:
    """Split *text* on *sep* only when parenthesis depth is 0."""
    items: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    sep_len = len(sep)
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and text[i:i + sep_len] == sep:
            items.append("".join(cur).strip())
            cur = []
            i += sep_len
            continue
        cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _extract_paren_body(sql: str, start_search: int = 0) -> tuple[str, int]:
    """Find the first '(' at/after start_search and return (body_inside, end_pos).

    Returns ("", -1) if no matching pair found.
    """
    pos = sql.find("(", start_search)
    if pos < 0:
        return "", -1
    depth = 0
    body_chars: list[str] = []
    for i in range(pos, len(sql)):
        ch = sql[i]
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return "".join(body_chars), i
        body_chars.append(ch)
    return "".join(body_chars), -1


def _extract_insert_columns(sql: str) -> list[str]:
    """Return target column list from INSERT INTO table (...) ..."""
    upper = sql.upper()
    into_idx = upper.find("INTO")
    if into_idx < 0:
        return []
    body, _ = _extract_paren_body(sql, into_idx)
    if not body:
        return []
    cols = [
        c.strip().strip('"').strip("'").upper()
        for c in body.split(",")
    ]
    return [c for c in cols if re.match(r"^[A-Z][A-Z0-9_#$]*$", c)]


def _find_keyword_at_depth0(text: str, keyword: str, start: int = 0) -> int:
    """Return index of *keyword* in *text* at parenthesis depth 0, or -1."""
    upper = text.upper()
    kw_upper = keyword.upper()
    kw_len = len(kw_upper)
    depth = 0
    i = start
    while i < len(upper) - kw_len + 1:
        ch = upper[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and upper[i:i + kw_len] == kw_upper:
            # Ensure it's a word boundary (not part of an identifier)
            before_ok = i == 0 or not (upper[i - 1].isalpha() or upper[i - 1] == "_")
            after_ok = (i + kw_len >= len(upper)
                        or not (upper[i + kw_len].isalpha() or upper[i + kw_len] == "_"))
            if before_ok and after_ok:
                return i
        i += 1
    return -1


def _extract_select_body(full_sql: str) -> str:
    """Return the SELECT ... portion of an INSERT ... SELECT ... statement."""
    upper = full_sql.upper()
    # Skip past INSERT INTO table (col_list)
    into_idx = upper.find("INTO")
    if into_idx < 0:
        sel = _find_keyword_at_depth0(full_sql, "SELECT")
        return full_sql[sel:] if sel >= 0 else full_sql

    # Find closing ) of the column list
    _, col_list_end = _extract_paren_body(full_sql, into_idx)
    search_from = col_list_end + 1 if col_list_end >= 0 else into_idx + 4

    sel_idx = _find_keyword_at_depth0(full_sql, "SELECT", search_from)
    return full_sql[sel_idx:] if sel_idx >= 0 else full_sql[search_from:]


def _split_select_from_where(select_body: str) -> tuple[str, str, str]:
    """Split a SELECT body into (select_list_text, from_text, where_text).

    Respects parenthesis depth so subqueries don't interfere.
    Strips leading hint comment /*+ ... */ from the select_list.
    """
    from_idx = _find_keyword_at_depth0(select_body, "FROM", 6)  # skip 'SELECT'
    if from_idx < 0:
        sel = select_body[6:].strip()  # everything after SELECT
        sel = re.sub(r"^/\*\+[^*]*\*/", "", sel).strip()
        return sel, "", ""

    sel_text = select_body[6:from_idx].strip()
    sel_text = re.sub(r"^/\*\+[^*]*\*/", "", sel_text).strip()

    rest = select_body[from_idx + 4:]
    where_idx = _find_keyword_at_depth0(rest, "WHERE")

    # also look for GROUP BY, ORDER BY, HAVING which end the FROM clause
    for kw in ("GROUP BY", "ORDER BY", "HAVING"):
        idx = _find_keyword_at_depth0(rest, kw)
        if idx >= 0 and (where_idx < 0 or idx < where_idx):
            where_idx = idx
            break

    if where_idx < 0:
        return sel_text, rest.strip(), ""
    return sel_text, rest[:where_idx].strip(), rest[where_idx:].strip()


# ── Alias binding extraction ───────────────────────────────────────────────────

# Matches: SCHEMA.TABLE ALIAS  or  TABLE ALIAS  or  SCHEMA.TABLE
# Oracle identifiers can contain $ # _
_RE_TABLE_ALIAS = re.compile(
    r"^([A-Z][A-Z0-9_$#]*)(?:\.([A-Z][A-Z0-9_$#]*))?(?:\s+([A-Z][A-Z0-9_$#]*))?$",
    re.I,
)


# ANSI JOIN keyword splitter ([LEFT|RIGHT|FULL] [OUTER] JOIN / INNER JOIN /
# CROSS JOIN / JOIN).  Used so a FROM clause written in ANSI-join style (taxlot
# Simple-Insert / Merge IKMs) yields one operand per table, not one giant blob.
_RE_JOIN_SPLIT = re.compile(
    r"\b(?:(?:LEFT|RIGHT|FULL)\s+(?:OUTER\s+)?|INNER\s+|CROSS\s+)?JOIN\b", re.I
)


def _parse_from_clause(from_text: str) -> list[AliasBinding]:
    """Parse a FROM clause into AliasBinding objects.

    Handles BOTH old-style comma joins AND ANSI ``[LEFT|RIGHT|FULL] [OUTER]
    JOIN`` (possibly wrapped in grouping parens, with inline-subquery
    operands -- the taxlot Simple-Insert / Merge IKMs).  Splits on depth-0
    commas AND JOIN keywords; for each operand, drops the trailing
    ``ON <predicate>`` and any wrapping grouping parens, then matches
    ``schema.table alias``.  Inline-subquery operands ``(SELECT ...) alias``
    do not match a simple table ref and are skipped (their columns remain
    UNRESOLVABLE for operator review).  Generic -- no scenario-specific names.
    """
    bindings: list[AliasBinding] = []
    seen: set = set()
    # Split on depth-0 commas first, then on ANSI JOIN keywords.
    raw_parts: list[str] = []
    for comma_part in _split_at_depth_zero(from_text or "", ","):
        raw_parts.extend(_RE_JOIN_SPLIT.split(comma_part))
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        # Drop the ANSI "ON <predicate>" tail -- keep only the table reference.
        m_on = re.search(r"\bON\b", part, re.I)
        if m_on:
            part = part[:m_on.start()].strip()
        # Strip wrapping grouping parens, but a leading "(SELECT" marks an
        # inline subquery (handled by the no-match skip below).
        while part.startswith("(") and not re.match(r"\(\s*SELECT\b", part, re.I):
            part = part[1:].strip()
        part = part.rstrip(")").strip()
        m = _RE_TABLE_ALIAS.match(part)
        if not m:
            # Inline-subquery operand: ``(SELECT ... FROM <schema.table> ...)
            # <alias>``.  Register <alias> -> the single physical table named
            # in the subquery's FROM, so its projected columns resolve (e.g.
            # taxlot CL_VAL filtered slices ``(SELECT ... FROM CL_VAL CL_VAL1
            # WHERE CL_SCM_ID=84) CL_VAL1_1`` -> CCAL_REPL_OWNER.CL_VAL).
            # Fully determinate (the subquery FROM names exactly one table);
            # no guessing.
            sub = re.match(r"\(\s*SELECT\b(.*)\)\s*([A-Z][A-Z0-9_$#]*)\s*$",
                           part, re.IGNORECASE | re.DOTALL)
            if sub:
                inner, outer_alias = sub.group(1), sub.group(2).upper()
                mt = re.search(r"\bFROM\s+([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_$#]*)",
                               inner, re.IGNORECASE)
                if mt and outer_alias not in seen:
                    try:
                        bindings.append(AliasBinding(
                            alias=outer_alias,
                            ref=TableRef(schema=mt.group(1).upper(), table=mt.group(2).upper()),
                        ))
                        seen.add(outer_alias)
                    except ValueError:
                        pass
            continue
        g1, g2, g3 = m.group(1), m.group(2), m.group(3)
        if g2:
            schema, table = g1.upper(), g2.upper()
            alias = g3.upper() if g3 else table
        else:
            schema, table = "", g1.upper()
            alias = g3.upper() if g3 else table
        if alias in seen:
            continue
        try:
            ref = TableRef(schema=schema, table=table)
            bindings.append(AliasBinding(alias=alias, ref=ref))
            seen.add(alias)
        except ValueError:
            pass
    return bindings


# ── Column expression classification ─────────────────────────────────────────

_RE_SIMPLE_COL = re.compile(
    r"^\(?([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_#$]*)\)?$", re.I
)
_RE_LITERAL = re.compile(
    r"^(?:SYSDATE|SYSTIMESTAMP|NULL|'[^']*'|\d+(?:\.\d+)?|"
    r"TO_DATE\b|TO_TIMESTAMP\b|TO_NUMBER\b|TRUNC\(SYSDATE|0\b|1\b)",
    re.I,
)


def _classify_source_expr(
    expr: str,
    alias_map: dict[str, AliasBinding],
) -> "ResolvedColumn | UnresolvedExpr":
    """Return ResolvedColumn or UnresolvedExpr for a SELECT list expression."""
    expr_stripped = expr.strip().strip("(").strip(")").strip()

    # Simple ALIAS.COLUMN (possibly wrapped in one pair of outer parens)
    m = _RE_SIMPLE_COL.match(expr_stripped)
    if m:
        alias = m.group(1).upper()
        col = m.group(2).upper()
        binding = alias_map.get(alias)
        if binding is not None:
            return ResolvedColumn(
                expr_sql=f"{alias}.{col}",
                provenance=Provenance.ODI,
                ref=binding.ref,
                column=col,
                original_expr=expr,
            )
        return UnresolvedExpr(
            original_expr=expr,
            reason="ALIAS_NOT_IN_JOIN_GRAPH",
            detail=f"alias '{alias}' not found in FROM clause",
        )

    # Literal detection (SYSDATE, NULL, quoted string, number, already-resolved bind var)
    if _RE_LITERAL.match(expr_stripped):
        return ResolvedColumn(
            expr_sql=expr,
            provenance=Provenance.LITERAL,
            ref=None,
            column="",
            original_expr=expr,
        )

    # Everything else: complex expression (NVL, CASE, arithmetic, function calls).
    # Provenance = ODI; ref = None because we can't determine a single source table.
    return ResolvedColumn(
        expr_sql=expr,
        provenance=Provenance.ODI,
        ref=None,
        column="",
        original_expr=expr,
    )


# ── Join graph extraction ─────────────────────────────────────────────────────

_RE_JOIN_PRED = re.compile(
    r"([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_#$]*)\s*(?:\(\+\))?\s*="
    r"\s*([A-Z][A-Z0-9_$#]*)\.([A-Z][A-Z0-9_#$]*)\s*(?:\(\+\))?",
    re.I,
)


def _parse_join_edges(
    where_text: str,
    alias_map: dict[str, AliasBinding],
) -> list[JoinEdge]:
    """Extract explicit equality join predicates from the WHERE clause."""
    edges: list[JoinEdge] = []
    for m in _RE_JOIN_PRED.finditer(where_text):
        la, lc, ra, rc = (m.group(i).upper() for i in (1, 2, 3, 4))
        lb = alias_map.get(la)
        rb = alias_map.get(ra)
        if lb is None or rb is None:
            continue
        jt = JoinType.LEFT if "(+)" in m.group(0) else JoinType.INNER
        try:
            edge = JoinEdge(
                join_type=jt,
                driving=lb,
                joined=rb,
                on_sql=m.group(0).strip(),
            )
            edges.append(edge)
        except ValueError:
            pass  # degenerate self-join rejected by JoinEdge.__post_init__
    return edges


# ── Merge INSERT column list ──────────────────────────────────────────────────

def _extract_merge_insert_columns(merge_sql: str) -> list[str]:
    """Return the target column list from WHEN NOT MATCHED THEN INSERT (...)."""
    upper = merge_sql.upper()
    nm_idx = upper.find("WHEN NOT MATCHED")
    if nm_idx < 0:
        # Benign for MERGE-less integration blocks (Simple-Insert taxlot IKMs
        # take final_insert_columns from the INSERT path, not this MERGE
        # helper).  DEBUG, not WARNING, so it does not surface as a GUI
        # "parse warning" on every taxlot analyze.
        _logger.debug(
            "_extract_merge_insert_columns: 'WHEN NOT MATCHED' not found in MERGE SQL"
            " -- falling back to block start (benign for non-MERGE integration)"
        )
        nm_idx = 0
    ins_idx = upper.find("INSERT", nm_idx)
    if ins_idx < 0:
        return []
    body, _ = _extract_paren_body(merge_sql, ins_idx)
    if not body:
        return []
    cols = [
        re.sub(r"^[TS]\.", "", c.strip(), flags=re.I).strip().upper()
        for c in body.split(",")
    ]
    return [c for c in cols if re.match(r"^[A-Z][A-Z0-9_#$]*$", c)]


# ── Main parser ───────────────────────────────────────────────────────────────

class OdiXmlParser:
    """Parse an ODI XML scenario export byte-string into an ODIModel.

    Usage::

        parser = OdiXmlParser()
        with open("scenario.xml", "rb") as f:
            model = parser.parse_bytes(f.read())

    The parser is pure offline — no Oracle DB calls.  All odiRef.* template
    tags and :GLOBAL.GV_* bind variables are resolved to Oracle SQL literals
    before any further processing.
    """

    def __init__(
        self,
        target_schema: str = _DEFAULT_TARGET_SCHEMA,
        target_table: str = _DEFAULT_TARGET_TABLE,
        schema_map: Optional[dict[str, str]] = None,
    ) -> None:
        # Stored as strings; the real TableRef is built in _build_model AFTER
        # the integration target is auto-detected (blank here -> detect).
        self._target_schema = (target_schema or "").strip().upper()
        self._target_table = (target_table or "").strip().upper()
        self._schema_map: dict[str, str] = schema_map or {}

    # ── Public API ────────────────────────────────────────────────────────────

    def parse_bytes(self, xml_bytes: bytes, encoding: str = "ISO-8859-1") -> ODIModel:
        return self.parse_text(xml_bytes.decode(encoding, errors="replace"))

    def parse_text(self, xml_text: str) -> ODIModel:
        blocks = _extract_def_txt_blocks(xml_text)
        return self._build_model(blocks)

    # ── Block extraction ──────────────────────────────────────────────────────

    # ── Model building ────────────────────────────────────────────────────────

    def _build_model(self, raw_blocks: list[str]) -> ODIModel:
        # Placeholder target; resolved after the integration block is found.
        model = ODIModel(target=TableRef(
            self._target_schema or "ODI",
            self._target_table or "_UNRESOLVED_TARGET_",
        ))
        step_inserts: dict[int, str] = {}
        unnumbered_insert: str = ""   # Phase 7.19.20: largest non-AVY INSERT (Simple-Insert IKM)
        had_avy_step = False
        det_schema = ""   # Phase 7.19.24: auto-detected target from integration block
        det_table = ""
        notes: list[str] = []

        for idx, raw in enumerate(raw_blocks):
            cls = _classify_block(raw)
            if cls not in ("STEP_INSERT", "MERGE", "STEP_DDL"):
                notes.append(f"block_{idx:02d}: {cls} (skipped)")
                continue

            resolved = resolve_odi_templates(raw, self._schema_map)

            if cls == "STEP_INSERT":
                n = _step_num(raw)
                if n is not None:
                    had_avy_step = True
                    # Operator-locked (2026-05-29): ODI emits MULTIPLE
                    # STEP_INSERT blocks per step number when the load
                    # has both a base-path and a richer-path variant.
                    # Keep the LARGER block -- it carries more JOINs and
                    # column derivations.  Empirical: STEP5 has 2 blocks
                    # (24598 + 13839 bytes); the 24598 block has the
                    # BKR_AR_DIM joins that produce BKR_AC_* attribute
                    # columns; the smaller is a thin restart path.
                    existing = step_inserts.get(n, "")
                    if len(resolved) > len(existing):
                        step_inserts[n] = resolved
                        notes.append(
                            f"block_{idx:02d}: STEP{n}_INSERT kept "
                            f"(len {len(resolved)}, prev {len(existing)})"
                        )
                    else:
                        notes.append(
                            f"block_{idx:02d}: STEP{n}_INSERT skipped "
                            f"(len {len(resolved)} < kept {len(existing)})"
                        )
                else:
                    # Phase 7.19.20 (2026-06-02): a STEP_INSERT block with no
                    # AVY step name belongs to a "Simple Insert" IKM (e.g. the
                    # CLOSED taxlot scenario): a single INSERT INTO <target>
                    # (cols) SELECT ... FROM <joined sources>.  ODI emits a
                    # base + a smaller _RT restart variant; keep the LARGER --
                    # it IS the integration mapping (promoted below).
                    if len(resolved) > len(unnumbered_insert):
                        unnumbered_insert = resolved
                        notes.append(
                            f"block_{idx:02d}: unnumbered STEP_INSERT kept "
                            f"(len {len(resolved)}) -- Simple-Insert candidate"
                        )
                    else:
                        notes.append(
                            f"block_{idx:02d}: unnumbered STEP_INSERT skipped "
                            f"(len {len(resolved)} <= kept {len(unnumbered_insert)})"
                        )
            elif cls == "MERGE":
                # Phase 7.19.18: keep the LARGER MERGE block (mirrors the
                # STEP_INSERT logic above).  ODI emits a base MERGE plus a
                # smaller restart/_RT variant; the larger one carries the
                # full WHEN MATCHED / WHEN NOT MATCHED column mapping.
                if len(resolved) > len(model.final_select_sql or ""):
                    model.final_select_sql = resolved
                    model.final_insert_columns = _extract_merge_insert_columns(resolved)
                    _ds, _dt = _extract_into_target(resolved)
                    if _dt:
                        det_schema, det_table = _ds, _dt
                    notes.append(f"block_{idx:02d}: MERGE kept (len {len(resolved)})")
                else:
                    notes.append(
                        f"block_{idx:02d}: MERGE skipped "
                        f"(len {len(resolved)} <= kept {len(model.final_select_sql or '')})"
                    )

        # Phase 7.19.20 (2026-06-02): "Simple Insert" IKMs (e.g. the CLOSED
        # taxlot scenario, IKM RJ Oracle Simple Insert) have NO MERGE and NO
        # AVY-named staging steps -- just a single INSERT INTO <target>
        # (cols) SELECT ... FROM <joined sources>.  That INSERT *is* the
        # integration mapping.  Promote the largest unnumbered INSERT to a
        # staging step so the derivation walker + comparator resolve columns
        # from it, and take the target column order from its INSERT clause.
        if unnumbered_insert and not had_avy_step and not model.final_select_sql:
            step_inserts.setdefault(1, unnumbered_insert)
            if not model.final_insert_columns:
                model.final_insert_columns = _extract_insert_columns(unnumbered_insert)
            if not det_table:
                _ds, _dt = _extract_into_target(unnumbered_insert)
                if _dt:
                    det_schema, det_table = _ds, _dt
            notes.append(
                "Simple-Insert IKM: final INSERT used as the integration "
                "mapping (no MERGE block in this scenario)"
            )

        model.notes = notes

        # Build staging steps in order
        for step_num in sorted(step_inserts):
            sql = step_inserts[step_num]
            step = self._parse_step_insert(step_num, sql)
            model.staging_steps.append(step)

        # A model is valid with ANY integration mapping: AVY staging steps +
        # a final MERGE; a MERGE-only IKM (7.19.18); or a Simple-Insert IKM
        # whose single INSERT is itself the mapping (7.19.20).
        if not model.staging_steps and not model.final_select_sql:
            raise ValueError(
                "ERROR_NO_MAPPING_BLOCKS: ODI XML has neither an INSERT/MERGE "
                "integration mapping nor staging steps -- nothing to compare"
            )
        # AVY-style multi-step loads MUST end in a MERGE; AVY steps with no
        # MERGE is an incomplete scenario.  (Simple-Insert + MERGE-only paths
        # legitimately have no MERGE / no AVY steps respectively.)
        if had_avy_step and not model.final_select_sql:
            raise ValueError(
                "ERROR_NO_MERGE_BLOCK: AVY staging steps found but no MERGE "
                "block to integrate them"
            )

        # Phase 7.19.24: resolve the real target.  Caller's explicit value wins;
        # otherwise use the auto-detected target from the integration block;
        # otherwise keep the placeholder so nothing downstream crashes.
        final_schema = self._target_schema or det_schema or model.target.schema
        final_table = self._target_table or det_table or model.target.table
        model.target = TableRef(schema=final_schema, table=final_table)
        if det_table and not self._target_table:
            notes.append(f"target auto-detected: {final_schema}.{final_table}")

        # Phase 1 (2026-05-29): enrich the model with per-column derivation
        # chains via sqlglot AST walk.  Idempotent + degrades to no-op when
        # sqlglot is unavailable, so existing consumers stay working.
        try:
            from app.sql_model.derivation_walker import enrich_model
            enrich_model(model)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("derivation_walker enrichment failed: %s", exc)

        return model

    # ── Step INSERT parsing ───────────────────────────────────────────────────

    def _parse_step_insert(self, step_num: int, sql: str) -> StagingStep:
        step_name = f"SSDS_AVY_FACT_STEP{step_num}_STG"
        step = StagingStep(step_id=step_num, name=step_name, select_sql=sql)

        target_cols = _extract_insert_columns(sql)
        select_body = _extract_select_body(sql)

        if not select_body:
            step.column_mappings = [
                ColumnMapping(tc, UnresolvedExpr(tc, "PARSE_FAILED", "no SELECT body found"))
                for tc in target_cols
            ]
            return step

        sel_text, from_text, _where_text = _split_select_from_where(select_body)

        # Build alias map from FROM clause
        source_bindings = _parse_from_clause(from_text)
        step.source_bindings = source_bindings

        try:
            alias_map = build_alias_map(source_bindings)
        except AliasConflictError:
            # Same alias name bound to two different physical tables — shouldn't
            # happen in well-formed ODI exports; build a permissive map (last wins)
            # and record the issue in notes.
            alias_map = {b.alias: b for b in source_bindings}
            step.select_sql = sql + "\n-- WARNING: alias conflict in FROM clause"

        # Split SELECT expression list
        sel_exprs = _split_at_depth_zero(sel_text, ",") if sel_text else []

        # Build column mappings (positional: target_cols[i] <-> sel_exprs[i])
        mappings: list[ColumnMapping] = []
        for i, col_name in enumerate(target_cols):
            if i < len(sel_exprs):
                source = _classify_source_expr(sel_exprs[i], alias_map)
            else:
                source = UnresolvedExpr(
                    original_expr=col_name,
                    reason="NO_SOURCE_EXPR",
                    detail=f"column #{i} has no corresponding SELECT expression",
                )
            mappings.append(ColumnMapping(target_col=col_name, source=source))

        step.column_mappings = mappings

        # Parse join graph from WHERE clause (best-effort; stored for fidelity)
        step.join_graph = _parse_join_edges(_where_text, alias_map)

        return step


# ── Module-level helper (also used by odi_xml_reverse_engineer_service.py) ────

def _extract_def_txt_blocks(xml_text: str) -> list[str]:
    """Extract all DefTxt field values from a decoded ODI XML export string."""
    try:
        root = ET.fromstring(xml_text)
        blocks: list[str] = []
        for field in root.iter("Field"):
            if field.get("name") == "DefTxt":
                val = (field.text or "").strip()
                if val:
                    blocks.append(html.unescape(val))
        return blocks
    except ET.ParseError as exc:
        _logger.warning("ODI XML ET.ParseError (falling back to regex): %s", exc)

    # Bounded-regex fallback for malformed XML
    blocks = []
    for m in re.finditer(
        r'<Field\s+name="DefTxt"[^>]{0,200}>'
        r'(?:<!\[CDATA\[([\s\S]{0,65536}?)\]\]>|([\s\S]{0,65536}?))'
        r"</Field>",
        xml_text or "",
    ):
        cdata, plain = m.group(1), m.group(2)
        val = html.unescape(cdata if cdata is not None else (plain or ""))
        if val.strip():
            blocks.append(val)
    return blocks
