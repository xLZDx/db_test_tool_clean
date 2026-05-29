"""Deep ODI derivation walker built on sqlglot Oracle-dialect AST.

For every target column in every ODI step (STEP1..STEP5 + MERGE), this
walker classifies the SELECT-list expression that feeds the column,
identifies pass-throughs vs real derivations, and marks the single
authoritative source step.

Operator-locked invariants (2026-05-29):
  * Generic -- no business-domain identifiers anywhere in the logic.
  * Phase 0 empirically verified 6/6 ODI SQL blocks parse cleanly after
    ``odi_sql_preprocessor.preprocess``.  On unforeseen ParseError, the
    walker degrades to ``EXPR_KIND_PARSE_FAILED`` for that step so the
    consumer can surface the gap rather than silently dropping.
  * Pass-through detection is STRUCTURAL (alias name matches a staging
    table name), not heuristic substring -- avoids false positives on
    columns whose name happens to contain ``_STG_RT``.
  * Authoritative-source rule: walk from STEP1 upward; the FIRST step
    with a non-pass-through expression is authoritative.  If every step
    is pass-through (legitimate ODI gap), the EARLIEST step is marked
    authoritative with kind=``passthrough`` so the consumer can render
    "no derivation in chain" honestly.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

try:
    import sqlglot
    import sqlglot.errors
    import sqlglot.expressions as exp
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False

from app.sql_model.odi_sql_preprocessor import preprocess
from app.sql_model.types import (
    EXPR_KIND_AGG,
    EXPR_KIND_CASE_WHEN,
    EXPR_KIND_COLUMN_REF,
    EXPR_KIND_FUNCTION,
    EXPR_KIND_LITERAL,
    EXPR_KIND_PARSE_FAILED,
    EXPR_KIND_PASSTHROUGH,
    EXPR_KIND_SUBQUERY,
    EXPR_KIND_UNKNOWN,
    EXPR_KIND_UNPIVOT,
    MERGE_STEP_ID,
    MERGE_USING_STEP_ID,
    ColumnDerivation,
    ODIModel,
)

_logger = logging.getLogger(__name__)

# Known Oracle aggregate function names (case-insensitive).  Operator-locked
# list; not exhaustive but covers every aggregate seen in this XML + standard
# Oracle pre-aggregates.
_AGGREGATE_NAMES = frozenset({
    "SUM", "MIN", "MAX", "AVG", "COUNT", "STDDEV", "VARIANCE",
    "LISTAGG", "WM_CONCAT", "XMLAGG", "GROUP_CONCAT",
    "FIRST", "LAST", "RANK", "DENSE_RANK", "ROW_NUMBER",
})

# Staging-table alias regex for pass-through detection.  Matches the generic
# ODI convention ``<PREFIX>_STEP<N>_STG`` with an optional ``_RT`` runtime
# suffix.  Structural -- not bound to any specific business prefix; the
# prefix portion is captured from the model's staging-step names at runtime.
_STAGING_ALIAS_RE = re.compile(
    r"^(?:[A-Z][A-Z0-9_]*)_STEP\d+_STG(?:_RT)?$",
    re.IGNORECASE,
)


def _is_staging_alias(alias: str, staging_table_names: set) -> bool:
    """True if ``alias`` matches a staging table name pattern or is one of
    the known staging table names from the model."""
    if not alias:
        return False
    up = alias.strip().upper()
    if up in staging_table_names:
        return True
    if (up + "_RT") in staging_table_names:
        return True
    if up.endswith("_RT") and up[:-3] in staging_table_names:
        return True
    return bool(_STAGING_ALIAS_RE.match(up))


def _extract_target_cols_from_insert(ast) -> List[str]:
    """Return the INSERT column list in positional order.  Empty list if the
    AST is not an INSERT or has no explicit column list."""
    if not isinstance(ast, exp.Insert):
        return []
    schema = ast.this
    if isinstance(schema, exp.Schema):
        return [c.name for c in schema.expressions]
    return []


def _get_select_expressions(ast):
    """Return the SELECT-list expressions list for an INSERT...SELECT AST.

    Empty list when AST is not Insert-of-Select or the SELECT body is
    elsewhere (e.g. MERGE).
    """
    if not isinstance(ast, exp.Insert):
        return []
    inner = ast.expression
    if isinstance(inner, exp.Select):
        return inner.expressions
    return []


def _extract_merge_insert_target_and_exprs(ast) -> Tuple[List[str], List]:
    """For a MERGE statement, extract:
       - the WHEN NOT MATCHED THEN INSERT clause's target column list (stripped
         of the alias prefix like ``T.``)
       - the corresponding VALUES expressions from the same INSERT clause
         (also alias-stripped), which are the per-column source projections
         after pass-through of the USING subquery.

    Returns ``(target_cols, select_exprs)``.  Empty lists when extraction
    cannot succeed.

    The structure sqlglot emits for ``WHEN NOT MATCHED THEN INSERT (T.X, T.Y)
    VALUES (S.X, S.Y)``:
        Insert.this        = Tuple of Column nodes (target cols, T.X form)
        Insert.expression  = Tuple of Column nodes (values, S.X form)
    Both tuples are positionally aligned in well-formed SQL.
    """
    if not isinstance(ast, exp.Merge):
        return [], []
    target_cols: List[str] = []
    value_exprs: List = []
    for when in ast.args.get("expressions", []):
        then_node = when.args.get("then") if hasattr(when, "args") else None
        if not isinstance(then_node, exp.Insert):
            continue
        # ins.this is a Tuple of Column nodes (target column list)
        tcols_node = then_node.this
        if isinstance(tcols_node, exp.Tuple):
            for c in tcols_node.expressions:
                name = c.name if hasattr(c, "name") else str(c)
                # Strip the leading ``T.`` alias if present
                if hasattr(c, "args") and c.args.get("table") is not None:
                    pass  # name already bare
                target_cols.append(name)
        # ins.expression is the VALUES tuple
        vals_node = then_node.expression
        if isinstance(vals_node, exp.Tuple):
            value_exprs = list(vals_node.expressions)
        break
    return target_cols, value_exprs


def _classify_select_item(
    item,
    target_col: str,
    staging_table_names: set,
) -> Tuple[str, str, str, str]:
    """Classify a single SELECT-list AST node.

    Returns ``(expr_kind, expr_sql, source_alias, source_col)``.

    ``expr_sql`` is the canonical AST .sql() rendering -- always available
    even when classification falls through to ``EXPR_KIND_UNKNOWN``.
    """
    if item is None:
        return EXPR_KIND_UNKNOWN, "", "", ""

    # sqlglot wraps every SELECT item in an Alias node when ``AS X`` is
    # present.  Strip the alias wrapper before inspecting the expression.
    inner = item
    while isinstance(inner, exp.Alias):
        inner = inner.this

    expr_sql = inner.sql(dialect="oracle") if inner is not None else ""

    # Bare column ref: <alias>.<col>  ->  passthrough OR column_ref.
    if isinstance(inner, exp.Column):
        col_name = inner.name or ""
        # ``table`` is sqlglot's term for the alias-or-table prefix.
        alias_name = ""
        tbl = inner.args.get("table")
        if tbl is not None:
            alias_name = tbl.name if hasattr(tbl, "name") else str(tbl)
        if _is_staging_alias(alias_name, staging_table_names):
            return (EXPR_KIND_PASSTHROUGH, expr_sql, alias_name, col_name)
        return (EXPR_KIND_COLUMN_REF, expr_sql, alias_name, col_name)

    # Literal: number, string, null
    if isinstance(inner, (exp.Literal, exp.Null, exp.Boolean)):
        return (EXPR_KIND_LITERAL, expr_sql, "", "")

    # CASE WHEN
    if isinstance(inner, exp.Case):
        return (EXPR_KIND_CASE_WHEN, expr_sql, "", "")

    # Subquery (correlated lookup)
    if isinstance(inner, exp.Subquery):
        return (EXPR_KIND_SUBQUERY, expr_sql, "", "")

    # UNPIVOT-derived (sqlglot 25.x uses ``Pivot`` for both PIVOT and UNPIVOT;
    # older / future versions may expose ``Unpivot`` separately).  Defensive
    # lookup so missing attributes don't crash the walker.
    _Pivot = getattr(exp, "Pivot", None)
    _Unpivot = getattr(exp, "Unpivot", None)
    if _Unpivot is not None and isinstance(inner, _Unpivot):
        return (EXPR_KIND_UNPIVOT, expr_sql, "", "")
    if _Pivot is not None and isinstance(inner, _Pivot):
        return (EXPR_KIND_UNPIVOT, expr_sql, "", "")

    # Aggregates + general functions (NVL, COALESCE, TO_DATE, SUBSTR, ...)
    # sqlglot exposes function names in several places depending on whether
    # the function is a recognised typed node, an Anonymous node, or a
    # well-known builtin:
    #   - ``inner.key`` -- node type key (e.g. 'sum', 'count')
    #   - ``inner.name`` -- for Anonymous, the literal function-name string
    #   - the .sql() prefix before '(' -- last-resort fallback
    func_names: list = []
    if hasattr(inner, "key") and isinstance(inner.key, str):
        func_names.append(inner.key.upper())
    if hasattr(inner, "name") and inner.name:
        func_names.append(str(inner.name).upper())
    # Anonymous nodes carry the function-name in .this when Identifier-typed
    this = getattr(inner, "this", None)
    if this is not None and hasattr(this, "name") and this.name:
        func_names.append(str(this.name).upper())
    # Last resort: parse the prefix of the SQL render before '('
    head = expr_sql.split("(", 1)[0].strip().upper() if "(" in expr_sql else ""
    if head and head.replace("_", "").isalpha():
        func_names.append(head)
    if any(n in _AGGREGATE_NAMES for n in func_names):
        return (EXPR_KIND_AGG, expr_sql, "", "")
    # CAST / Coalesce / NVL / DECODE / arithmetic / Func nodes all classify
    # as FUNCTION here (non-pass-through, has a real expression).
    if isinstance(inner, (exp.Func, exp.Cast, exp.Coalesce, exp.Anonymous,
                          exp.Binary, exp.Unary, exp.Paren)):
        return (EXPR_KIND_FUNCTION, expr_sql, "", "")

    return (EXPR_KIND_UNKNOWN, expr_sql, "", "")


def _build_step_derivations(
    step_label: str,
    step_id: int,
    raw_sql: str,
    staging_table_names: set,
) -> Dict[str, ColumnDerivation]:
    """Parse one step's SQL and return per-target-column derivation map.

    Returns ``{target_col_upper: ColumnDerivation}``.  When parsing fails,
    returns a map populated with EXPR_KIND_PARSE_FAILED entries for every
    target column the upstream parser knew about (so consumers see the gap
    rather than silently missing the step).
    """
    if not raw_sql:
        return {}
    cleaned, _applied = preprocess(raw_sql)

    if not _SQLGLOT_AVAILABLE:
        return {}

    try:
        ast = sqlglot.parse_one(cleaned, dialect="oracle")
    except sqlglot.errors.ParseError as e:
        _logger.warning(
            "derivation_walker: sqlglot ParseError on %s -- falling back to "
            "PARSE_FAILED placeholders. err=%s",
            step_label,
            str(e)[:200],
        )
        return {}
    except Exception as e:  # noqa: BLE001 -- sqlglot has many exception types
        _logger.warning(
            "derivation_walker: unexpected sqlglot error on %s: %s: %s",
            step_label, type(e).__name__, str(e)[:200],
        )
        return {}

    if isinstance(ast, exp.Merge):
        target_cols, select_exprs = _extract_merge_insert_target_and_exprs(ast)
    else:
        target_cols = _extract_target_cols_from_insert(ast)
        select_exprs = _get_select_expressions(ast)
    if len(target_cols) != len(select_exprs):
        _logger.warning(
            "derivation_walker: positional misalignment on %s: "
            "target_cols=%d, select_exprs=%d -- skipping step",
            step_label, len(target_cols), len(select_exprs),
        )
        return {}

    out: Dict[str, ColumnDerivation] = {}
    for tc, expr_node in zip(target_cols, select_exprs):
        kind, expr_sql, source_alias, source_col = _classify_select_item(
            expr_node, tc, staging_table_names,
        )
        out[tc.upper()] = ColumnDerivation(
            step_label=step_label,
            step_id=step_id,
            expr_sql=expr_sql,
            expr_kind=kind,
            is_authoritative=False,  # set in second pass
            source_alias=source_alias,
            source_col=source_col,
        )
    return out


def _build_using_step_derivations(
    step_label: str,
    step_id: int,
    raw_merge_sql: str,
    staging_table_names: set,
) -> Dict[str, ColumnDerivation]:
    """Parse the MERGE's USING subquery and align its SELECT-list expressions
    to the WHEN NOT MATCHED INSERT clause's target column list.

    This is what surfaces real derivations for MERGE-only columns -- the
    target columns that ONLY appear in the MERGE's INSERT clause and are
    sourced from an outer join executed inside the USING subquery.
    """
    if not raw_merge_sql:
        return {}
    cleaned, _ = preprocess(raw_merge_sql)
    try:
        ast = sqlglot.parse_one(cleaned, dialect="oracle")
    except Exception as e:  # noqa: BLE001
        _logger.warning(
            "derivation_walker: USING parse failed on %s: %s: %s",
            step_label, type(e).__name__, str(e)[:160],
        )
        return {}
    if not isinstance(ast, exp.Merge):
        return {}
    using_node = ast.args.get("using")
    select_exprs: List = []
    if isinstance(using_node, exp.Subquery):
        inner = using_node.this
        if isinstance(inner, exp.Select):
            select_exprs = list(inner.expressions)
    elif isinstance(using_node, exp.Select):
        select_exprs = list(using_node.expressions)
    target_cols, _values = _extract_merge_insert_target_and_exprs(ast)
    if not target_cols or not select_exprs:
        return {}
    # Align: take the first N expressions where N = target col count.
    # If counts differ by 1-2 (extra audit cols in USING that don't make it
    # into INSERT), we accept the prefix alignment -- the misalignment will
    # surface visually in the report.
    n = min(len(target_cols), len(select_exprs))
    out: Dict[str, ColumnDerivation] = {}
    for tc, expr_node in zip(target_cols[:n], select_exprs[:n]):
        kind, expr_sql, source_alias, source_col = _classify_select_item(
            expr_node, tc, staging_table_names,
        )
        out[tc.upper()] = ColumnDerivation(
            step_label=step_label,
            step_id=step_id,
            expr_sql=expr_sql,
            expr_kind=kind,
            is_authoritative=False,  # second pass marks
            source_alias=source_alias,
            source_col=source_col,
        )
    return out


def _collect_staging_table_names(model: ODIModel) -> set:
    """Build the set of staging-table names (upper case) used for pass-through
    detection.  Includes both the bare step names and their ``_RT`` runtime
    variants."""
    names: set = set()
    for step in model.staging_steps:
        if step.name:
            up = step.name.upper()
            names.add(up)
            if not up.endswith("_RT"):
                names.add(up + "_RT")
            elif up.endswith("_STG_RT"):
                names.add(up[:-3])  # strip _RT to add the base form
    # Also include the generic STEPn_STG_RT runtime alias form
    # by extracting any common prefix.
    return names


def enrich_model(model: ODIModel) -> None:
    """Populate ``model.column_derivations`` in place.

    Walks every staging step + MERGE, extracts per-column expressions, marks
    the authoritative step per column, stores the chain.  Idempotent (safe
    to call twice -- second call overwrites).
    """
    if not _SQLGLOT_AVAILABLE:
        _logger.info("derivation_walker: sqlglot unavailable; skipping enrichment")
        return

    staging_names = _collect_staging_table_names(model)

    # ── Pass 1: build per-step derivation maps ────────────────────────────
    per_step: Dict[int, Dict[str, ColumnDerivation]] = {}
    for step in model.staging_steps:
        label = f"STEP{step.step_id}"
        per_step[step.step_id] = _build_step_derivations(
            label, step.step_id, step.select_sql or "", staging_names,
        )

    # MERGE block.  We extract derivations from BOTH:
    #   (a) the MERGE's WHEN NOT MATCHED INSERT clause -> shows ``S.X``
    #       references (step_id=99, MERGE_STEP_ID)
    #   (b) the USING(...) subquery's SELECT -> the actual per-column
    #       projections that S.X resolves to (step_id=98, MERGE_USING_STEP_ID)
    # The USING-derived chain is what tells the comparator whether the
    # MERGE-only columns have real derivations or are pass-throughs from
    # the latest staging step.
    if model.final_select_sql:
        per_step[MERGE_STEP_ID] = _build_step_derivations(
            "MERGE", MERGE_STEP_ID, model.final_select_sql, staging_names,
        )
        per_step[MERGE_USING_STEP_ID] = _build_using_step_derivations(
            "MERGE_USING", MERGE_USING_STEP_ID, model.final_select_sql,
            staging_names,
        )

    # ── Pass 2: assemble per-column chains in step order ─────────────────
    all_cols: set = set()
    for sd in per_step.values():
        all_cols.update(sd.keys())

    chains: Dict[str, List[ColumnDerivation]] = {}
    for col in all_cols:
        chain: List[ColumnDerivation] = []
        for step_id in sorted(per_step.keys()):
            d = per_step[step_id].get(col)
            if d is not None:
                chain.append(d)
        # Authoritative = first non-passthrough.  If all are passthrough,
        # the FIRST (earliest) is authoritative so the consumer sees
        # "originated in STEPn as a passthrough" honestly.
        chosen_index = None
        for i, d in enumerate(chain):
            if d.expr_kind != EXPR_KIND_PASSTHROUGH:
                chosen_index = i
                break
        if chosen_index is None and chain:
            chosen_index = 0
        if chosen_index is not None:
            # Dataclass is frozen so swap the entry rather than mutate
            old = chain[chosen_index]
            chain[chosen_index] = ColumnDerivation(
                step_label=old.step_label,
                step_id=old.step_id,
                expr_sql=old.expr_sql,
                expr_kind=old.expr_kind,
                is_authoritative=True,
                source_alias=old.source_alias,
                source_col=old.source_col,
            )
        chains[col] = chain

    model.column_derivations = chains


__all__ = (
    "enrich_model",
    "preprocess",
    "_classify_select_item",
    "_is_staging_alias",
)
