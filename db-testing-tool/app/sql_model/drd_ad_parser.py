"""Generic parser for DRD column AD ('Transformation / Business Rules / Join Conditions').

The DRD format used by data-architecture teams encodes per-attribute join
chains and business rules as free-form text in one cell (column AD in the
canonical Activity-Fact-style workbook).  Examples (all real, from the AVY
fact DRD - but the parser is content-agnostic):

    Look up using TXN.SRC_TXN_TP=SHDW_TXN_TP.SRC_TXN_TP

    ccal_repl_owner.txn t
    left join ccal_repl_owner.impct_action_lku lk
        ON t.src_actn_code = impct_action_lku.action_code

    ccal_repl_owner.txn t
    left join ccal_repl_owner.cl_val cv ON cv.cl_val_id = t.SRC_CNCL_RSN_ID
        and cs.cl_scm_id = '102'

    select fa.FA_NUM from
    ccal_repl_owner.txn t
    LEFT JOIN ccsi_owner.AR_GRP_SUBDIM fa ON t.AR_ID = fa.AR_ID
        and t.td >= fa.EFF_DT
        and t.td < fa.END_DT;

This module produces a structured ``DrdAdRule`` so downstream code can compare
DRD-declared joins against actual ODI joins (JOIN_DRIFT) and re-emit a real
SQL fragment in the generator (P6).

Design rules (operator-locked):
  * Generic - no hard-coded table / column / schema names.
  * Idempotent - safe to call repeatedly.
  * Never raises - returns an empty ``DrdAdRule`` on unparseable text.
  * Preserves originals - ``raw`` always holds the original cell text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DrdAdJoinPredicate:
    """One equality predicate inside a JOIN ON clause.

    Identifiers preserve their original casing for diff-display but
    ``norm_left`` / ``norm_right`` are upper-cased for matching.
    """
    left: str            # e.g. "t.src_actn_code"
    right: str           # e.g. "impct_action_lku.action_code"
    raw: str             # original source text fragment

    @property
    def norm_left(self) -> str:
        return self.left.strip().upper()

    @property
    def norm_right(self) -> str:
        return self.right.strip().upper()


@dataclass(frozen=True)
class DrdAdJoin:
    """A single JOIN clause parsed out of a DRD col-AD cell."""
    join_type: str                          # "LEFT JOIN", "INNER JOIN", "JOIN"
    fq_table: str                            # "ccal_repl_owner.txn" (UPPER for matching)
    alias: str                               # "t" (UPPER)
    predicates: List[DrdAdJoinPredicate]
    raw: str                                 # full original JOIN ... ON ... text


@dataclass
class DrdAdRule:
    """Structured result of parsing one DRD col-AD cell.

    ``raw`` is always populated.  ``base_table``, ``joins``,
    ``lookup_pairs``, and ``filters`` are filled when parseable; otherwise
    they are empty / None.
    """
    raw: str = ""
    base_table: Optional[str] = None         # e.g. "CCAL_REPL_OWNER.TXN"
    base_alias: Optional[str] = None         # e.g. "T"
    joins: List[DrdAdJoin] = field(default_factory=list)
    # ``Look up using A.X=B.Y`` shorthand: list of (left, right) predicate pairs
    lookup_pairs: List[DrdAdJoinPredicate] = field(default_factory=list)
    # Free-form WHERE / filter fragments (best-effort).
    filters: List[str] = field(default_factory=list)
    # Notes about anything we recognised but couldn't fully structure.
    notes: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.joins or self.lookup_pairs or self.base_table or self.filters)


# ---------------------------------------------------------------------------
# Regex inventory (compiled once, content-agnostic)
# ---------------------------------------------------------------------------

# DRD authors freely mix line-breaks, capitalisation and qualifier styles.
# Identifier := optional schema.optional alias-or-table with $ # _ allowed.
_IDENT = r"[A-Za-z][A-Za-z0-9_\$#]*"
# Tolerate up to 3 dot-segments (some real DRDs duplicate the schema by typo:
# ``ssds_dal_owner.SSDS_DAL_OWNER.PERSON_RV``).  Post-processing keeps only the
# last two segments as the canonical fq table.
_FQ_TABLE = rf"{_IDENT}(?:\.{_IDENT}){{0,2}}"


def _normalize_fq_table(raw: str) -> str:
    """Return the canonical ``SCHEMA.TABLE`` (last two segments, upper-cased)."""
    parts = [p for p in raw.split(".") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper()
    return f"{parts[-2]}.{parts[-1]}".upper()

# A first ``schema.table [alias]`` line that introduces the FROM target.
# Matches at the start of the string OR after a newline; tolerates leading
# whitespace.  Allows the line to STOP at a newline or at the keyword
# `left/inner/full/right/cross join`.
_BASE_FROM_RE = re.compile(
    rf"(?:^|\n)\s*({_IDENT})\.({_IDENT})\s+({_IDENT})?\s*(?:\n|(?=\b(?:LEFT|INNER|FULL|RIGHT|CROSS)\s+JOIN\b)|$)",
    re.IGNORECASE,
)

# Each JOIN clause: type + fq table + optional alias + ON <cond ...>.
# `cond` runs until the next JOIN keyword (any variant -- LEFT/INNER/.. JOIN or
# bare JOIN), the literal `WHERE`, ; or end-of-string.
_JOIN_RE = re.compile(
    rf"\b(LEFT\s+JOIN|INNER\s+JOIN|RIGHT\s+JOIN|FULL\s+JOIN|JOIN)\b"
    rf"\s+({_FQ_TABLE})"
    rf"(?:\s+({_IDENT}))?"
    rf"\s+ON\s+(.+?)"
    rf"(?="
    rf"\b(?:LEFT|INNER|RIGHT|FULL|CROSS)\s+JOIN\b"
    rf"|\bJOIN\b"
    rf"|\bWHERE\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

# One equality predicate inside ON: `A.B = C.D` (allow `(+)` outer-join markers).
_EQ_PRED_RE = re.compile(
    rf"({_IDENT}(?:\.{_IDENT})?)\s*(?:\(\+\))?\s*=\s*"
    rf"({_IDENT}(?:\.{_IDENT})?)\s*(?:\(\+\))?",
    re.IGNORECASE,
)

# Implicit-lookup shorthand:  ``Look up using A.X = B.Y`` (and friends).
# Captures the equality pair; the prelude text is ignored.
_LOOKUP_USING_RE = re.compile(
    rf"\bLook(?:up|\s+up)\s+(?:using|via|with|on)?\s*"
    rf"({_IDENT}(?:\.{_IDENT})?)\s*=\s*"
    rf"({_IDENT}(?:\.{_IDENT})?)",
    re.IGNORECASE,
)

# Trivial placeholder cells that mean "no rule documented".
_TRIVIAL_AD = {"", "none", "n/a", "na", "-", "--", "tbd", "?"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_ad_text(text: str) -> str:
    """Strip BOM / surrounding quotes; collapse runs of internal whitespace
    while preserving newlines so JOIN-clause boundaries stay detectable."""
    if not text:
        return ""
    # Strip control chars except newline and tab
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text))
    # Trim outer whitespace
    cleaned = cleaned.strip()
    # Collapse runs of spaces / tabs (but keep newlines)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    return cleaned


def _split_predicates(on_text: str) -> List[DrdAdJoinPredicate]:
    """Split an ON clause into individual equality predicates."""
    out: List[DrdAdJoinPredicate] = []
    for m in _EQ_PRED_RE.finditer(on_text or ""):
        left, right = m.group(1), m.group(2)
        out.append(DrdAdJoinPredicate(left=left, right=right, raw=m.group(0).strip()))
    return out


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------

def parse_drd_ad(text: str) -> DrdAdRule:
    """Parse one DRD col-AD cell into a structured ``DrdAdRule``.

    Generic: works for any schema / table / column.  Never raises.
    """
    raw = text or ""
    if not raw or raw.strip().lower() in _TRIVIAL_AD:
        return DrdAdRule(raw=raw)

    norm = _normalize_ad_text(raw)
    rule = DrdAdRule(raw=raw)

    # 1) Detect the FROM base table + alias (best-effort)
    base_match = _BASE_FROM_RE.search(norm)
    if base_match:
        schema, table, alias = base_match.group(1), base_match.group(2), base_match.group(3)
        rule.base_table = f"{schema}.{table}".upper()
        rule.base_alias = (alias or table).upper()

    # 2) Detect every JOIN ... ON ...
    for jm in _JOIN_RE.finditer(norm):
        join_kw = re.sub(r"\s+", " ", jm.group(1).upper())
        fq_table = _normalize_fq_table(jm.group(2))
        alias = (jm.group(3) or fq_table.split(".")[-1]).upper()
        on_text = jm.group(4).strip()
        preds = _split_predicates(on_text)
        rule.joins.append(
            DrdAdJoin(
                join_type=join_kw,
                fq_table=fq_table,
                alias=alias,
                predicates=preds,
                raw=jm.group(0).strip(),
            )
        )

    # 3) Detect "Look up using A.X = B.Y" shorthand
    for lm in _LOOKUP_USING_RE.finditer(norm):
        rule.lookup_pairs.append(
            DrdAdJoinPredicate(left=lm.group(1), right=lm.group(2), raw=lm.group(0).strip())
        )

    return rule


# ---------------------------------------------------------------------------
# JOIN_DRIFT comparison
# ---------------------------------------------------------------------------

def _bare_col(ident: str) -> str:
    """Return the bare column name (strip any ``<alias>.`` prefix), upper-cased."""
    if not ident:
        return ""
    parts = ident.split(".")
    return parts[-1].strip().upper()


def predicate_matches(
    drd_pred: DrdAdJoinPredicate,
    odi_on_sql: str,
) -> bool:
    """Return True if ``drd_pred`` (e.g. ``t.src_actn_code = lk.action_code``)
    is present in ``odi_on_sql`` after case normalisation.

    Matching is alias-insensitive (we only care about the bare column pair)
    and order-insensitive (``A=B`` matches ``B=A``).  This treats ``t.X=lk.Y``
    and ``X=Y`` and ``MY_ALIAS.X=OTHER.Y`` as the same join semantic.
    """
    if not odi_on_sql:
        return False
    drd_left = _bare_col(drd_pred.left)
    drd_right = _bare_col(drd_pred.right)
    if not drd_left or not drd_right:
        return False
    drd_pair = frozenset({drd_left, drd_right})

    # Extract every equality predicate from the ODI ON clause and reduce each
    # side to its bare column name; compare as a set so order doesn't matter.
    for m in _EQ_PRED_RE.finditer(odi_on_sql):
        odi_left = _bare_col(m.group(1))
        odi_right = _bare_col(m.group(2))
        if not odi_left or not odi_right:
            continue
        if frozenset({odi_left, odi_right}) == drd_pair:
            return True
    return False


def compare_drd_ad_joins(
    drd_ad: DrdAdRule,
    odi_join_on_sqls: List[str],
) -> dict:
    """Compare DRD-required JOIN predicates against actual ODI ON clauses.

    Returns a dict suitable for ComparisonResult.explanation enrichment::

        {
            "drd_required": [<DrdAdJoinPredicate>...],
            "satisfied": [<DrdAdJoinPredicate>...],
            "unsatisfied": [<DrdAdJoinPredicate>...],
            "all_satisfied": bool,
        }

    A "lookup_pair" (shorthand `Look up using A=B`) and a "join predicate"
    are treated identically here: both describe a required equality.
    """
    required: List[DrdAdJoinPredicate] = []
    seen_raw: set = set()
    for j in drd_ad.joins:
        for p in j.predicates:
            key = (p.norm_left, p.norm_right)
            if key in seen_raw:
                continue
            seen_raw.add(key)
            required.append(p)
    for p in drd_ad.lookup_pairs:
        key = (p.norm_left, p.norm_right)
        if key in seen_raw:
            continue
        seen_raw.add(key)
        required.append(p)

    satisfied: List[DrdAdJoinPredicate] = []
    unsatisfied: List[DrdAdJoinPredicate] = []
    for p in required:
        if any(predicate_matches(p, on) for on in odi_join_on_sqls):
            satisfied.append(p)
        else:
            unsatisfied.append(p)

    return {
        "drd_required": required,
        "satisfied": satisfied,
        "unsatisfied": unsatisfied,
        "all_satisfied": not unsatisfied and bool(required),
        "any_required": bool(required),
    }
