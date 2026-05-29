"""Typed semantic model for the SQL test-generation pipeline.

This is the IR (intermediate representation) the whole rebuild binds to.
Design rule (from the 2026-05-28 consensus): illegal states must be
unrepresentable. An alias resolves to exactly one table; an unresolved
column is a distinct type that cannot be silently emitted as SQL; a
self-join with no source key is rejected at construction.

No SQL string-mangling lives here -- only data + invariants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


def norm(s: object) -> str:
    """Uppercase + strip a SQL identifier fragment. '' for None (never None)."""
    return str(s or "").strip().upper()


@dataclass(frozen=True)
class TableRef:
    """A physical table. schema='' means schema-unqualified (never None)."""
    schema: str
    table: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", norm(self.schema))
        object.__setattr__(self, "table", norm(self.table))
        if not self.table:
            raise ValueError("TableRef.table must be non-empty")

    @property
    def fq(self) -> str:
        return f"{self.schema}.{self.table}" if self.schema else self.table

    def __str__(self) -> str:
        return self.fq


@dataclass(frozen=True)
class AliasBinding:
    """An alias token bound to exactly one physical table."""
    alias: str
    ref: TableRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias", norm(self.alias))
        if not self.alias:
            raise ValueError("AliasBinding.alias must be non-empty")


class AliasConflictError(ValueError):
    """Raised when the same alias token is bound to two different tables."""


class Provenance(Enum):
    DRD = "DRD"
    ODI = "ODI"
    KB = "KB"
    LITERAL = "LITERAL"        # SYSDATE, constants, NULL-by-design
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class ResolvedColumn:
    """An expression resolved to a concrete source. Safe to emit as SQL."""
    expr_sql: str                       # the actual SQL expression text (emittable)
    provenance: Provenance
    ref: Optional[TableRef] = None      # populated when the expr is a single column ref
    column: str = ""                    # physical column name when ref is set
    original_expr: str = ""             # raw text before resolution

    def __post_init__(self) -> None:
        if self.provenance == Provenance.UNRESOLVED:
            raise ValueError("Use UnresolvedExpr for unresolved expressions")


@dataclass(frozen=True)
class UnresolvedExpr:
    """An expression that could NOT be resolved. MUST NOT be emitted as SQL.

    reason is a machine code surfaced to the UI grid as a hard error so the
    operator knows exactly where to look (PDM_MISS, ALIAS_NOT_IN_JOIN_GRAPH,
    UNCLEAR_RULE, COLUMN_NOT_IN_KB, ...).
    """
    original_expr: str
    reason: str
    detail: str = ""


# A column mapping's source is EITHER resolved (emittable) OR unresolved (error).
SourceExpr = "ResolvedColumn | UnresolvedExpr"


@dataclass
class ColumnMapping:
    target_col: str
    source: object                       # ResolvedColumn | UnresolvedExpr
    is_nullable: bool = True
    is_pk: bool = False

    def __post_init__(self) -> None:
        self.target_col = norm(self.target_col)

    @property
    def is_resolved(self) -> bool:
        return isinstance(self.source, ResolvedColumn)


@dataclass(frozen=True)
class JoinCondition:
    left_ref: TableRef
    left_col: str
    right_ref: TableRef
    right_col: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "left_col", norm(self.left_col))
        object.__setattr__(self, "right_col", norm(self.right_col))
        if self.left_ref == self.right_ref and self.left_col == self.right_col:
            raise ValueError(
                f"Degenerate self-join: {self.left_ref}.{self.left_col} = "
                f"{self.right_ref}.{self.right_col}"
            )


class JoinType(Enum):
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"
    CROSS = "CROSS"


@dataclass
class JoinEdge:
    join_type: JoinType
    driving: AliasBinding
    joined: AliasBinding
    on_sql: str = ""                     # raw ON predicate (emittable), for fidelity

    def __post_init__(self) -> None:
        if self.join_type != JoinType.CROSS and not self.on_sql.strip():
            raise ValueError(
                f"JoinEdge {self.driving.alias}->{self.joined.alias} needs an ON predicate"
            )


@dataclass
class StagingStep:
    """One STEPn intermediate table in the ODI multi-step staging chain."""
    step_id: int
    name: str                            # CTE / staging table name (e.g. AVY_FACT_STEP1_STG)
    select_sql: str = ""                 # the full SELECT body that fills this step
    column_mappings: list = field(default_factory=list)   # list[ColumnMapping]
    source_bindings: list = field(default_factory=list)   # list[AliasBinding]
    join_graph: list = field(default_factory=list)        # list[JoinEdge]


@dataclass(frozen=True)
class ColumnDerivation:
    """One ODI step's SELECT-list contribution to a target column.

    Operator-locked invariants (2026-05-29):
      * Generic -- no business-domain identifiers in the field names.
      * ``expr_kind`` classifies what KIND of expression produces the value;
        consumers use this to distinguish pass-throughs from real derivations.
      * ``is_authoritative=True`` marks the SINGLE step in a column's chain
        that is the "source of truth" -- the deepest non-pass-through step,
        or the deepest pass-through if no derivation exists anywhere.
    """
    step_label: str                  # "STEP3" or "MERGE"
    step_id: int                     # numeric (MERGE = 99)
    expr_sql: str                    # raw SQL expression text (cleaned, post-preprocessor)
    expr_kind: str                   # passthrough | column_ref | case_when | agg |
                                     # function | literal | subquery | unpivot |
                                     # unknown | parse_failed
    is_authoritative: bool = False
    source_alias: str = ""           # populated for column_ref / passthrough
    source_col: str = ""             # populated for column_ref / passthrough


# Expression kinds (constants -- callers may compare to these for stability).
EXPR_KIND_PASSTHROUGH = "passthrough"
EXPR_KIND_COLUMN_REF = "column_ref"
EXPR_KIND_CASE_WHEN = "case_when"
EXPR_KIND_AGG = "agg"
EXPR_KIND_FUNCTION = "function"
EXPR_KIND_LITERAL = "literal"
EXPR_KIND_SUBQUERY = "subquery"
EXPR_KIND_UNPIVOT = "unpivot"
EXPR_KIND_UNKNOWN = "unknown"
EXPR_KIND_PARSE_FAILED = "parse_failed"

# MERGE step uses synthetic step_id so it sorts deterministically AFTER STEP5.
# The MERGE_USING step is the SELECT inside the USING(...) subquery -- it
# contains the actual per-column derivations that the WHEN NOT MATCHED INSERT
# clause copies via ``S.X`` references.
MERGE_USING_STEP_ID = 98
MERGE_STEP_ID = 99


@dataclass
class ODIModel:
    """The whole ODI mapping resolved to a structured, typed model."""
    target: TableRef
    staging_steps: list = field(default_factory=list)     # list[StagingStep], ordered
    final_insert_columns: list = field(default_factory=list)   # target col order from MERGE/INSERT
    final_select_sql: str = ""           # the MERGE USING / final SELECT body
    notes: list = field(default_factory=list)             # parse notes / stripped blocks
    # ── Deep derivation map (added 2026-05-29) ──
    # ``column_derivations[target_col_upper]`` -> ordered list of every step
    # where the column has a SELECT-list expression.  Default empty dict so
    # construction sites that don't populate it stay backward-compatible.
    column_derivations: dict = field(default_factory=dict)  # dict[str, list[ColumnDerivation]]

    def step(self, step_id: int):
        for s in self.staging_steps:
            if s.step_id == step_id:
                return s
        return None

    def authoritative_derivation(self, target_col: str):
        """Return the single ColumnDerivation marked authoritative for this
        column, or ``None`` if the column is absent from the derivation map.
        """
        chain = self.column_derivations.get((target_col or "").upper())
        if not chain:
            return None
        for d in chain:
            if d.is_authoritative:
                return d
        return chain[-1] if chain else None


def build_alias_map(bindings) -> dict:
    """Build {alias: AliasBinding}; raise AliasConflictError on collision."""
    out: dict = {}
    for b in bindings:
        existing = out.get(b.alias)
        if existing is not None and existing.ref != b.ref:
            raise AliasConflictError(
                f"alias {b.alias!r} bound to both {existing.ref} and {b.ref}"
            )
        out[b.alias] = b
    return out


class ComparisonVerdict(Enum):
    MATCHED = "MATCHED"                   # semantically identical
    ALIAS_DRIFT_ONLY = "ALIAS_DRIFT_ONLY"  # same physical column, different surface alias
    REAL_MISMATCH = "REAL_MISMATCH"      # genuinely different column/logic
    UNRESOLVABLE = "UNRESOLVABLE"        # one+ side failed to resolve (unclear rule)
    SOURCE_MISSING = "SOURCE_MISSING"    # one of the compared sources produced nothing


class MismatchKind(Enum):
    """Sub-classification of a REAL_MISMATCH.

    Used to surface side-by-side DRD-vs-ODI evidence in the comparison grid so
    the operator can see *why* something disagrees, not just *that* it does.
    Empty / NONE means no sub-classification (legacy MATCHED / ALIAS_DRIFT_ONLY
    / generic mismatch).
    """
    NONE = ""
    COLUMN_MISMATCH = "COLUMN_MISMATCH"             # different source column on same table
    TABLE_MISMATCH = "TABLE_MISMATCH"               # different source table entirely
    TRANSFORMATION_DRIFT = "TRANSFORMATION_DRIFT"   # DRD says CASE/parse/lookup, ODI does pass-through (or vice versa)
    JOIN_DRIFT = "JOIN_DRIFT"                       # DRD requires join A=B; ODI implements A=C
    APPLICABLE_FILTER_DRIFT = "APPLICABLE_FILTER_DRIFT"  # DRD says "Applicable only for X" (CASE-WHEN expected); ODI projects unfiltered
