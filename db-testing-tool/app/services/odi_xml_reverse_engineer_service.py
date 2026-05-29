"""ODI XML reverse-engineering utilities for 99% DRD/XML parity checks."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

_SQLPLUS_COMMENT_RE = re.compile(r"--")
_ORACLE_SUBVAR_RE = re.compile(r"&&?")


def normalize_dtype(dtype: str) -> str:
    """Normalize an Oracle DDL type while preserving character-length semantics.

    Preserves VARCHAR2(100 CHAR) — dropping ' CHAR' silently changes semantics.
    Only ' BYTE' is dropped because BYTE is the Oracle default and carries no
    extra meaning. Multi-word type names like TIMESTAMP WITH TIME ZONE are left
    intact (spaces inside are preserved).
    """
    dtype = str(dtype or "").strip().upper()
    dtype = dtype.replace("TIMETSTAMP", "TIMESTAMP")
    dtype = dtype.replace(" BYTE", "")  # BYTE is default; safe to normalize away
    # DO NOT strip " CHAR" — VARCHAR2(100 CHAR) uses character-length semantics
    # Normalize whitespace around punctuation only (not inside identifier tokens)
    dtype = re.sub(r"\s*([(),])\s*", r"\1", dtype)
    dtype = re.sub(r"  +", " ", dtype).strip()
    return dtype


def sanitize_oracle_comment(text: str) -> str:
    """Sanitize arbitrary text for safe embedding inside Oracle DDL COMMENT IS '...'."""
    if not text:
        return ""
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = text.replace("\x00", "")
    text = _ORACLE_SUBVAR_RE.sub("and", text)
    text = _SQLPLUS_COMMENT_RE.sub("- -", text)
    text = text.replace("'", "''")
    text = re.sub(r" {2,}", " ", text).strip()
    return text


def extract_def_txt_blocks(xml_text: str) -> List[str]:
    """Extract all DefTxt field values from a decoded ODI XML export string.

    Tries xml.etree.ElementTree first (safe, handles CDATA). Falls back to
    bounded regex for malformed XML documents. The regex caps each match at
    64 KB to avoid ReDoS on adversarial input.
    """
    # Safe path: ElementTree — handles CDATA transparently
    try:
        root = ET.fromstring(xml_text)
        blocks: List[str] = []
        for field in root.iter("Field"):
            if field.get("name") == "DefTxt":
                value = (field.text or "").strip()
                if value:
                    blocks.append(html.unescape(value))
        return blocks
    except ET.ParseError:
        pass

    # Bounded-regex fallback for malformed XML
    blocks = []
    for m in re.finditer(
        r'<Field\s+name="DefTxt"[^>]{0,200}>'
        r'(?:<!\[CDATA\[([\s\S]{0,65536}?)\]\]>|([\s\S]{0,65536}?))'
        r"</Field>",
        xml_text or "",
    ):
        cdata, plain = m.group(1), m.group(2)
        value = html.unescape(cdata if cdata is not None else (plain or ""))
        if value.strip():
            blocks.append(value)
    return blocks


def create_blocks(xml_text: str) -> List[str]:
    return [b for b in extract_def_txt_blocks(xml_text) if "create table" in b.lower()]


def merge_blocks(xml_text: str) -> List[str]:
    return [b for b in extract_def_txt_blocks(xml_text) if re.search(r"\bmerge\s+into\b", b, re.I)]


def _split_col_defs(body: str) -> List[Dict[str, Any]]:
    parts: List[str] = []
    cur = ""
    depth = 0
    for ch in body or "":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())

    out: List[Dict[str, Any]] = []
    for p in parts:
        p = re.sub(r"\s+", " ", p.strip())
        m = re.match(r"^(?:T\.|S\.)?([A-Z0-9_#$]+)\s+(.+?)(?:\s+(NULL|NOT NULL))?$", p, re.I)
        if m and re.search(r"^(NUMBER|VARCHAR2|DATE|TIMESTAMP)", m.group(2), re.I):
            out.append(
                {
                    "column": m.group(1).upper(),
                    "dtype": normalize_dtype(m.group(2)),
                    "nullable": (m.group(3) or "NULL").upper(),
                }
            )
    return out


def extract_create_table_columns(sql: str) -> List[Dict[str, Any]]:
    body = ""
    for m in re.finditer(r"\(", sql or ""):
        pos = m.start()
        nxt = sql[pos + 1 : pos + 140]
        if not re.match(r"\s*\n\s*\t?[A-Z0-9_#]+\s+(NUMBER|VARCHAR2|DATE|TIMESTAMP)", nxt, re.I):
            continue
        depth = 0
        for j in range(pos, len(sql)):
            if sql[j] == "(":
                depth += 1
            elif sql[j] == ")":
                depth -= 1
                if depth == 0:
                    body = sql[pos + 1 : j]
                    break
        if body:
            break
    return _split_col_defs(body)


def extract_final_stage_schema(xml_text: str) -> List[Dict[str, Any]]:
    creates = create_blocks(xml_text)
    return extract_create_table_columns(creates[-1]) if creates else []


def extract_final_merge_insert_columns(xml_text: str) -> List[str]:
    merges = merge_blocks(xml_text)
    if not merges:
        return []
    sql = merges[-1]
    up = sql.upper()
    idx = up.find("WHEN NOT MATCHED")
    if idx < 0:
        idx = up.find("INSERT")
    ins = up.find("INSERT", idx)
    if ins < 0:
        return []
    pos = sql.find("(", ins)
    if pos < 0:
        return []
    depth = 0
    end = -1
    for j in range(pos, len(sql)):
        if sql[j] == "(":
            depth += 1
        elif sql[j] == ")":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end < 0:
        return []
    body = sql[pos + 1 : end]
    cols: List[str] = []
    for token in body.split(","):
        c = re.sub(r"\s+", " ", token.strip())
        c = re.sub(r"^(T|S)\.", "", c, flags=re.I)
        if re.match(r"^[A-Z][A-Z0-9_#]*$", c, re.I):
            cols.append(c.upper())
    return cols


def extract_odi_xml_metadata(xml_bytes: bytes, encoding: str = "ISO-8859-1") -> Dict[str, Any]:
    text = xml_bytes.decode(encoding, errors="replace")
    return {
        "def_txt_count": len(extract_def_txt_blocks(text)),
        "create_block_count": len(create_blocks(text)),
        "merge_block_count": len(merge_blocks(text)),
        "stage_columns": extract_final_stage_schema(text),
        "final_merge_insert_columns": extract_final_merge_insert_columns(text),
    }
