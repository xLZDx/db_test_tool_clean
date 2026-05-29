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
_DEFAULT_TARGET_SCHEMA = "IKOROSTELEV"
_DEFAULT_TARGET_TABLE = "AVY_FACT_SIDE"

# ── Block classification patterns ─────────────────────────────────────────────
_RE_STEP_NUM = re.compile(r"SSDS_AVY_FACT_STEP(\d)_STG", re.I)
_RE_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
_RE_MERGE_INTO = re.compile(r"\bMERGE\s+INTO\b", re.I)
_RE_DECLARE_BEGIN = re.compile(r"^\s*(?:DECLARE|BEGIN)\b", re.I)
_RE_INSERT_INTO = re.compile(r"\bINSERT\b[^;]{0,200}?\bINTO\b", re.I | re.DOTALL)


def _classify_block(raw: str) -> str:
    """Return block classification string for routing."""
    has_step = bool(_RE_STEP_NUM.search(raw))
    has_create = bool(_RE_CREATE_TABLE.search(raw))
    has_merge = bool(_RE_MERGE_INTO.search(raw))
    has_insert = bool(_RE_INSERT_INTO.search(raw))

    if has_step and has_create:
        return "STEP_DDL"
    if has_step and has_merge:
        return "MERGE"
    if has_step and has_insert:
        return "STEP_INSERT"
    if bool(_RE_DECLARE_BEGIN.match(raw)):
        return "UTILITY"
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


def _parse_from_clause(from_text: str) -> list[AliasBinding]:
    """Parse a comma-joined FROM clause into AliasBinding objects."""
    bindings: list[AliasBinding] = []
    parts = _split_at_depth_zero(from_text, ",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _RE_TABLE_ALIAS.match(part)
        if not m:
            continue
        g1, g2, g3 = m.group(1), m.group(2), m.group(3)
        if g2:
            schema, table = g1.upper(), g2.upper()
            alias = g3.upper() if g3 else table
        else:
            schema, table = "", g1.upper()
            alias = g3.upper() if g3 else table
        try:
            ref = TableRef(schema=schema, table=table)
            bindings.append(AliasBinding(alias=alias, ref=ref))
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
        _logger.warning(
            "_extract_merge_insert_columns: 'WHEN NOT MATCHED' not found in MERGE SQL"
            " -- falling back to block start; column list may be wrong"
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
        self._target = TableRef(schema=target_schema, table=target_table)
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
        model = ODIModel(target=self._target)
        step_inserts: dict[int, str] = {}
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
            elif cls == "MERGE":
                model.final_select_sql = resolved
                model.final_insert_columns = _extract_merge_insert_columns(resolved)

        model.notes = notes

        # Build staging steps in order
        for step_num in sorted(step_inserts):
            sql = step_inserts[step_num]
            step = self._parse_step_insert(step_num, sql)
            model.staging_steps.append(step)

        if not model.staging_steps:
            raise ValueError(
                "ERROR_NO_STEP_BLOCKS: no STEP_INSERT blocks found in ODI XML"
            )
        if not model.final_select_sql:
            raise ValueError(
                "ERROR_NO_MERGE_BLOCK: no MERGE block found in ODI XML"
            )

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
