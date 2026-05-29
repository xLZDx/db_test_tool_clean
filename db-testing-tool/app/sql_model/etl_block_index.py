"""Generic index of "named logic blocks" extracted from non-mapping DRD sheets.

DRD authors typically encode reusable derivation logic (cash/security/
classification subset filters, lookup recipes, etc.) on a dedicated sheet
(often called "ETL Notes") and refer to it from individual column rows with
phrases like ``Use APACSH logic from 'ETL Notes' tab``.

This module is content-agnostic.  It identifies blocks by **structure**, not
by a hard-coded block name:

  * a header row whose first non-empty cell looks like ``<NAME>:``
    (an upper-case identifier followed by a colon), OR
  * a header row containing a single bare upper-case identifier whose next
    row is a SQL-looking continuation line.

It also detects ``Use <NAME> logic from .* tab`` style references in any
free-text cell (DRD transformation / notes / col-AD) so the comparator and
the generator can wire reference -> block content for that row.

No table / column / business-domain names are hard-coded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.sql_model.drd_multi_sheet import (
    DrdMultiSheetResult,
    ExtractionRule,
    SheetRole,
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EtlBlock:
    """One named logic block (e.g. APACSH, APASEC, "Product Information")."""
    name: str                       # canonical upper-case key (e.g. "APACSH")
    display_name: str               # original-case label as it appeared
    sheet: str                      # source sheet name
    body: str                       # concatenated body text
    raw_rules: List[ExtractionRule] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()


@dataclass
class EtlBlockIndex:
    """Index of every named block discovered across non-mapping sheets."""
    blocks: Dict[str, EtlBlock] = field(default_factory=dict)

    def get(self, name: str) -> Optional[EtlBlock]:
        if not name:
            return None
        return self.blocks.get(name.strip().upper())

    def __contains__(self, name: str) -> bool:
        return bool(name) and name.strip().upper() in self.blocks


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------

# A header line: an identifier (case-mixed allowed, must start with a letter)
# followed by a colon at the very start of the cell.  Captures the name only;
# the body is taken from everything after the first colon (possibly multi-line)
# via direct slicing.
_HEADER_COLON_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_ ]{0,40}?)\s*:"
)
# A bare-identifier header (e.g. just "APACSH" alone in a cell, body follows
# on subsequent rows).  Allows up to 2 short words.
_HEADER_BARE_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9_]{1,30}(?:\s+[A-Za-z][A-Za-z0-9_]{1,30})?)\s*$"
)

# Cells that look like SQL bodies (so we know a bare header is followed by content).
_SQL_HINT_RE = re.compile(
    r"\b(?:select|from|where|join|left|right|inner|outer|case|when|then|"
    r"nvl|coalesce|decode|regexp_like|substr|to_char|to_date|null|exists|"
    r"order\s+by|group\s+by|filter|union|having)\b",
    re.IGNORECASE,
)


def _row_first_cell_text(rule: ExtractionRule) -> str:
    """Return the first non-empty cell text from a rule's raw_row."""
    for c in rule.raw_row or []:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            return s
    return ""


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def build_block_index(multi_sheet: DrdMultiSheetResult) -> EtlBlockIndex:
    """Walk every ETL_NOTES / MODEL / UNKNOWN sheet rule and group into blocks.

    The grouping algorithm:
      1. Sort each sheet's rules in source order (preserved by extractor).
      2. When a rule's first cell matches a header regex (``<NAME>:`` or bare
         identifier followed by a body-shaped row), open a new block.
      3. Every following rule is appended to the open block until the next
         header.
      4. Headerless prelude (free-form notes at the top of the sheet) is kept
         under the synthetic block name ``__PRELUDE__`` per sheet but only
         when non-trivial.
    """
    index = EtlBlockIndex()
    # Group rules by sheet so the algorithm sees the sheet's natural order.
    by_sheet: Dict[str, List[ExtractionRule]] = {}
    for r in multi_sheet.extracted_rules:
        if r.role not in (SheetRole.ETL_NOTES, SheetRole.MODEL, SheetRole.UNKNOWN):
            continue
        by_sheet.setdefault(r.sheet, []).append(r)

    for sheet_name, rules in by_sheet.items():
        current_block: Optional[EtlBlock] = None
        # Peek-ahead pattern: a bare-identifier header is only valid if the
        # NEXT non-empty rule looks SQL-ish (otherwise we'd promote random
        # one-word notes to blocks).
        for i, rule in enumerate(rules):
            first = _row_first_cell_text(rule)
            header_name: Optional[str] = None
            inline_body: str = ""

            cm = _HEADER_COLON_RE.match(first)
            if cm:
                cand_name = cm.group(1).strip()
                # Reject implausibly long "names" (DRD authors write whole
                # sentences ending with a colon; those aren't block headers).
                if len(cand_name.split()) <= 3 and len(cand_name) <= 35:
                    # Body = everything in the cell after the first colon;
                    # may span multiple lines inside the same Excel cell.
                    colon_at = first.find(":", cm.end(1) - 1)
                    cand_body = first[colon_at + 1:].strip() if colon_at >= 0 else ""
                    header_name = cand_name.upper().replace(" ", "_")
                    inline_body = cand_body
            if header_name is None:
                bm = _HEADER_BARE_RE.match(first)
                if bm and i + 1 < len(rules):
                    next_first = _row_first_cell_text(rules[i + 1])
                    if next_first and _SQL_HINT_RE.search(next_first):
                        cand = bm.group(1).strip()
                        if cand.upper() not in {"NULL", "TRUE", "FALSE"}:
                            header_name = cand.upper().replace(" ", "_")

            if header_name is not None:
                display = first.rstrip(":").strip()
                key = header_name
                if key in index.blocks:
                    # Same header repeated; append to existing block instead of overwriting.
                    current_block = index.blocks[key]
                else:
                    current_block = EtlBlock(
                        name=key,
                        display_name=display,
                        sheet=sheet_name,
                        body=inline_body,
                        raw_rules=[rule] if inline_body else [],
                    )
                    index.blocks[key] = current_block
                if inline_body:
                    current_block.raw_rules.append(rule)
                continue

            # Not a header -> append to current block (if any).
            if current_block is not None:
                txt = rule.description.strip() if rule.description else first
                if txt:
                    if current_block.body and not current_block.body.endswith("\n"):
                        current_block.body += "\n"
                    current_block.body += txt
                    current_block.raw_rules.append(rule)

    return index


# ---------------------------------------------------------------------------
# Reference detector
# ---------------------------------------------------------------------------

# Match: "Use <NAME> logic from <anything> tab", "See <NAME>", "<NAME> logic"
# Captures the candidate block name.  Generic — works for any NAME and any case.
_USE_PHRASE_RE = re.compile(
    r"\b(?:use|using|see|per|apply|follow)s?\s+"
    r"([A-Za-z][A-Za-z0-9_]{1,30})"
    r"\s*(?:logic|rule|definition|block|method|approach|formula)?\s*"
    r"(?:from\s+['\"]?[\w\s]+['\"]?\s*tab|sheet|notes)?",
    re.IGNORECASE,
)

# Some DRD cells just say "<NAME> logic" without "use" verb.
_NAKED_LOGIC_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_]{1,30})\s+(?:logic|rule|method)\b",
    re.IGNORECASE,
)

# Names like "case", "from", "select" are SQL keywords that get accidentally
# captured by the phrase patterns; never treat them as block names.
_REF_STOPWORDS = {
    "USE", "SEE", "AS", "FOR", "FROM", "WHERE", "WITH", "AND", "OR", "NOT",
    "NULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "END", "ELSE", "IF",
    "EXISTS", "BETWEEN", "JOIN", "LEFT", "INNER", "OUTER", "ON", "BY",
    "SELECT", "INSERT", "UPDATE", "DELETE", "TAB", "SHEET",
}


def find_block_references(text: str, index: EtlBlockIndex) -> List[str]:
    """Return canonical block names referenced by ``text`` that resolve to
    blocks in the index.  Generic: matches any block name in any case.

    Resolution strategy (each per match candidate):
      1. Exact (uppercased) name in index.
      2. Substring fallback: any block whose body contains the candidate name
         as an upper-cased substring (handles e.g. APASEC referenced inside a
         block titled "MAPPING_LOGIC_OF_VARIOUS_APA_TYPES").
    """
    if not text or not index.blocks:
        return []
    refs: List[str] = []
    seen: set = set()
    for pat in (_USE_PHRASE_RE, _NAKED_LOGIC_RE):
        for m in pat.finditer(text):
            cand = m.group(1).strip().upper().replace(" ", "_")
            if not cand or cand in _REF_STOPWORDS or cand in seen:
                continue
            # 1) exact match
            if cand in index:
                seen.add(cand)
                refs.append(cand)
                continue
            # 2) content match — find a block whose body mentions the name
            #    (covers DRD references where the master block has a long
            #    sentence-style header and the referenced sub-name appears
            #    only inside its body).
            best: Optional[str] = None
            best_hits = 0
            for k, blk in index.blocks.items():
                hits = blk.body.upper().count(cand)
                if hits > best_hits:
                    best_hits = hits
                    best = k
            if best is not None:
                seen.add(cand)
                refs.append(best)
    return refs


def resolve_block_body(text: str, index: EtlBlockIndex) -> Optional[str]:
    """If ``text`` references a known block, return that block's body.

    Returns ``None`` when no reference is detected.
    """
    refs = find_block_references(text, index)
    if not refs:
        return None
    block = index.get(refs[0])
    if block is None or block.is_empty:
        return None
    return block.body
