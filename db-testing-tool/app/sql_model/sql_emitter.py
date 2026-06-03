"""SQL emitter: ODIModel -> Oracle INSERT SQL.

Design rules (consensus 2026-05-28):
- ONLY ResolvedColumn may be emitted as SQL.
- UnresolvedExpr raises EmitError immediately (never silently produces SQL).
- Output format: WITH cte1 AS (...), cte2 AS (...) ... INSERT INTO target (...) SELECT ...
- Each staging step becomes a CTE whose body is the template-resolved SELECT.
- The final INSERT column list comes from the MERGE block (ODIModel.final_insert_columns).

The emitter does NOT call any Oracle DB.  It is purely a string transformation
over the already-resolved ODIModel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from app.sql_model.types import (
    ColumnMapping,
    ODIModel,
    ResolvedColumn,
    StagingStep,
    UnresolvedExpr,
)


class EmitError(RuntimeError):
    """Raised when an UnresolvedExpr would be emitted as SQL.

    Per consensus: the emitter must refuse to emit rather than produce
    NULL /* PDM_MISS */ or ON 1=0 garbage.
    """


@dataclass
class EmitResult:
    """Output of emit_insert()."""
    sql: str                                    # ready-to-run Oracle INSERT SQL
    unresolved: list[dict] = field(default_factory=list)   # columns that needed substitution
    warnings: list[str] = field(default_factory=list)


def _select_body_from_step_sql(step_sql: str) -> str:
    """Extract the SELECT ... portion from a template-resolved INSERT...SELECT SQL.

    Strips the INSERT INTO table (...) header; returns everything from SELECT onward.
    """
    upper = step_sql.upper()

    # Find the end of the column list: the first '(' after 'INTO'
    into_idx = upper.find("INTO")
    if into_idx < 0:
        sel_idx = upper.find("SELECT")
        return step_sql[sel_idx:].strip() if sel_idx >= 0 else step_sql.strip()

    depth = 0
    col_list_end = -1
    for i in range(step_sql.find("(", into_idx), len(step_sql)):
        if step_sql[i] == "(":
            depth += 1
        elif step_sql[i] == ")":
            depth -= 1
            if depth == 0:
                col_list_end = i
                break

    search_from = col_list_end + 1 if col_list_end >= 0 else into_idx + 4

    # Find SELECT at depth 0 from search_from
    depth = 0
    i = search_from
    while i < len(upper) - 5:
        ch = upper[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and upper[i:i + 6] == "SELECT":
            before_ok = i == 0 or not (upper[i - 1].isalpha() or upper[i - 1] == "_")
            after_ok = i + 6 >= len(upper) or not (upper[i + 6].isalpha() or upper[i + 6] == "_")
            if before_ok and after_ok:
                return step_sql[i:].strip()
        i += 1

    return step_sql[search_from:].strip()


def _indent(text: str, spaces: int = 2) -> str:
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else line for line in text.splitlines())


def _emit_header(
    model: ODIModel, ikm: str, n_cols: int, u_count: int,
    caveats: list[str] | None = None,
) -> str:
    status = "PARTIAL -- see unresolved list" if u_count else "OK"
    lines = [
        f"-- Generated Oracle INSERT for {model.target.fq}",
        "-- Source: ODI XML semantic parser (db-testing-tool v2)",
        f"-- IKM style: {ikm}",
        f"-- Final columns: {n_cols}",
        f"-- Unresolved expressions: {u_count}",
        f"-- Status: {status}",
    ]
    for c in (caveats or []):
        lines.append(f"-- CAVEAT: {c}")
    return "\n".join(lines) + "\n"


def _emit_simple_insert(
    model: ODIModel, *, strict: bool, add_header_comment: bool
) -> EmitResult:
    """Faithful INSERT for a Simple-Insert IKM (single promoted step, no MERGE).

    The step's own SQL already IS ``INSERT INTO <target> (cols) SELECT <exprs>
    FROM <joins>`` -- reproduce it directly (no pointless CTE wrapper, no
    hard-coded ``SSDS_AVY_FACT_STEP*`` staging name).

    PRECONDITION (holds for the parser today, odi_parser.py ~604-607): the INSERT
    column list (``final_insert_columns``) and the step SELECT body come from the
    SAME promoted INSERT statement, so they are positionally aligned.  We assert
    the arity (col-count == mapping-count) defensively and warn on any drift so a
    future caller that diverges the two sources cannot silently misalign columns.
    """
    step = model.staging_steps[0]
    unresolved_report: list[dict] = []
    warnings: list[str] = []
    for cm in step.column_mappings:
        if isinstance(cm.source, UnresolvedExpr):
            if strict:
                raise EmitError(
                    f"{cm.target_col}: {cm.source.reason} -- {cm.source.detail}"
                )
            unresolved_report.append({
                "step": step.step_id, "target_col": cm.target_col,
                "reason": cm.source.reason, "detail": cm.source.detail,
                "original_expr": cm.source.original_expr,
            })

    cols = model.final_insert_columns or [cm.target_col for cm in step.column_mappings]
    if not cols:
        raise EmitError("Simple-Insert has no target columns -- cannot emit INSERT")
    # Defensive arity guard against positional misalignment (see PRECONDITION).
    n_map = len(step.column_mappings)
    if n_map and len(cols) != n_map:
        warnings.append(
            f"ARITY: INSERT column list ({len(cols)}) != step SELECT expressions "
            f"({n_map}) -- positional alignment NOT guaranteed; verify before running"
        )
    select_body = _select_body_from_step_sql(step.select_sql)
    if not select_body:
        raise EmitError("Simple-Insert step has no SELECT body -- cannot emit INSERT")

    col_list = ",\n  ".join(cols)
    insert_block = (
        f"INSERT INTO {model.target.fq}\n"
        f"(\n  {col_list}\n)\n"
        f"{select_body};"
    )
    header = _emit_header(
        model, "Simple-Insert (faithful)", len(cols), len(unresolved_report), warnings
    ) if add_header_comment else ""
    full_sql = f"{header}\n{insert_block}" if header else insert_block
    return EmitResult(sql=full_sql, unresolved=unresolved_report, warnings=warnings)


def _emit_from_merge(
    model: ODIModel, *, strict: bool, add_header_comment: bool
) -> EmitResult:
    """Faithful INSERT for a MERGE-only IKM (0 staging steps).

    The MERGE ``using ( select <pass-through> from ( select <real bindings>
    from <joins> ) )`` carries the full per-column projection.  Take the USING
    inner-SELECT as the INSERT body:
        ``INSERT INTO <target> (insert_cols) SELECT <inner expr per col> FROM <inner joins>``.
    Generic -- column order = the MERGE's WHEN NOT MATCHED INSERT column list.
    """
    # Helpers live in comparator (verified); lazy import avoids a load cycle.
    from app.sql_model.comparator import (
        _merge_inner_projection_map,
        _merge_inner_from_clause,
    )
    fs = model.final_select_sql or ""
    proj = _merge_inner_projection_map(fs)
    from_clause = _merge_inner_from_clause(fs)
    cols = model.final_insert_columns
    if not cols or not from_clause:
        raise EmitError(
            "MERGE USING inner-SELECT could not be parsed -- cannot emit INSERT"
        )
    if not proj:
        raise EmitError(
            "MERGE USING inner-SELECT parsed but found zero column bindings "
            "-- cannot emit INSERT"
        )

    unresolved_report: list[dict] = []
    warnings: list[str] = []
    nulled: list[str] = []
    select_items: list[str] = []
    for col in cols:
        expr = proj.get(col.upper())
        if not expr:
            if strict:
                raise EmitError(
                    f"{col}: column in MERGE INSERT list but not in USING projection"
                )
            unresolved_report.append({
                "target_col": col, "reason": "NOT_IN_USING_PROJECTION",
                "detail": "column absent from MERGE inner-SELECT", "original_expr": "",
            })
            nulled.append(col)
            select_items.append(f"NULL /* {col}: not in USING projection */")
        else:
            select_items.append(f"{expr}  /* {col} */")

    if nulled:
        warnings.append(
            "NULL-SUBSTITUTED columns absent from the USING projection: "
            + ", ".join(nulled)
        )

    # Operator-locked caveat: an INSERT built from the USING source SELECT
    # reproduces ALL source rows.  A MERGE with a WHEN MATCHED branch UPDATEs
    # matched rows and only INSERTs the unmatched ones -- so this faithful
    # column-mapping INSERT is NOT row-equivalent to the MERGE.  Surface it
    # loudly so the later data-validation step (control table vs target) does
    # not treat it as an insert-only replay.
    caveats: list[str] = []
    if "WHEN MATCHED" in fs.upper():
        caveats.append(
            "Source is a MERGE (WHEN MATCHED UPDATE + WHEN NOT MATCHED INSERT). "
            "This INSERT reproduces the full USING source (ALL rows); the MERGE "
            "only INSERTs unmatched rows. Add a NOT EXISTS/MINUS filter for "
            "insert-only semantics before using this for data validation."
        )
        warnings.append(caveats[-1])

    col_list = ",\n  ".join(cols)
    sel_list = ",\n  ".join(select_items)
    insert_block = (
        f"INSERT INTO {model.target.fq}\n"
        f"(\n  {col_list}\n)\n"
        f"SELECT\n  {sel_list}\n"
        f"{from_clause};"
    )
    header = _emit_header(
        model, "MERGE (faithful column-mapping, from USING)",
        len(cols), len(unresolved_report), caveats,
    ) if add_header_comment else ""
    full_sql = f"{header}\n{insert_block}" if header else insert_block
    return EmitResult(sql=full_sql, unresolved=unresolved_report, warnings=warnings)


def emit_insert(
    model: ODIModel,
    *,
    strict: bool = True,
    add_header_comment: bool = True,
) -> EmitResult:
    """Emit an Oracle INSERT SQL from the ODIModel.

    Three IKM shapes are supported:
      * AVY multi-step (>=1 staging step + final MERGE) -> WITH-CTE per step.
      * Simple-Insert  (1 step, no MERGE)               -> faithful single INSERT.
      * MERGE-only     (0 steps, MERGE USING)           -> faithful INSERT from USING.

    Args:
        model:               The fully-parsed ODIModel.
        strict:              If True, raise EmitError on any UnresolvedExpr.
                             If False, substitute NULL and record the issue.
        add_header_comment:  Prepend a generation comment block.

    Returns:
        EmitResult with .sql, .unresolved list, .warnings list.
    """
    # MERGE-only IKM: no staging steps, but the USING clause carries the mapping.
    # Match a real MERGE `USING (` clause (word-boundary), not any stray "USING"
    # token (hint / column name / comment) that could misroute the dispatch.
    if not model.staging_steps:
        if re.search(r"\bUSING\s*\(", model.final_select_sql or "", re.IGNORECASE):
            return _emit_from_merge(
                model, strict=strict, add_header_comment=add_header_comment
            )
        raise EmitError("ODIModel has no staging steps -- cannot emit INSERT")

    # Simple-Insert IKM: one promoted step, no MERGE integration block.  Emit
    # the original INSERT...SELECT directly (no CTE wrap, no AVY staging name).
    if len(model.staging_steps) == 1 and not (model.final_select_sql or "").strip():
        return _emit_simple_insert(
            model, strict=strict, add_header_comment=add_header_comment
        )

    unresolved_report: list[dict] = []
    warnings: list[str] = []

    # -- Validate: check for UnresolvedExpr in all steps --------------------
    for step in model.staging_steps:
        for cm in step.column_mappings:
            if isinstance(cm.source, UnresolvedExpr):
                entry = {
                    "step": step.step_id,
                    "step_name": step.name,
                    "target_col": cm.target_col,
                    "reason": cm.source.reason,
                    "detail": cm.source.detail,
                    "original_expr": cm.source.original_expr,
                }
                if strict:
                    raise EmitError(
                        f"STEP{step.step_id}.{cm.target_col}: "
                        f"{cm.source.reason} -- {cm.source.detail}"
                    )
                unresolved_report.append(entry)

    # -- Build CTEs (one per staging step) ----------------------------------
    cte_parts: list[str] = []
    for step in model.staging_steps:
        select_body = _select_body_from_step_sql(step.select_sql)
        if not select_body:
            warnings.append(f"STEP{step.step_id}: empty SELECT body -- using placeholder")
            select_body = "SELECT NULL FROM DUAL"
        cte_parts.append(f"{step.name} AS (\n{_indent(select_body, 2)}\n)")

    with_clause = "WITH " + ",\n".join(cte_parts)

    # -- Build INSERT column list --------------------------------------------
    final_cols = model.final_insert_columns
    if not final_cols:
        # Fall back to the last step's target columns
        last_step = model.staging_steps[-1]
        final_cols = [cm.target_col for cm in last_step.column_mappings]
        warnings.append("No MERGE INSERT columns found -- using last step column list")

    col_list = ",\n  ".join(final_cols)
    last_step_name = model.staging_steps[-1].name

    # -- Build SELECT list for the final INSERT ------------------------------
    # Select every final column by name from the last staging CTE.
    # Columns in the last step that are unresolved get NULL substitution
    # (if strict=False) or were already blocked above.
    select_items: list[str] = []
    last_step_col_map = {
        cm.target_col: cm for cm in model.staging_steps[-1].column_mappings
    }
    for col in final_cols:
        cm = last_step_col_map.get(col)
        if cm is None:
            # Column not in last step -- may come from an earlier step via pass-through
            select_items.append(col)
        elif isinstance(cm.source, UnresolvedExpr):
            select_items.append(f"NULL /* {cm.source.reason}: {col} */")
        else:
            select_items.append(col)

    sel_list = ",\n  ".join(select_items)

    # -- Compose final SQL ---------------------------------------------------
    header = ""
    if add_header_comment:
        u_count = len(unresolved_report)
        status = "PARTIAL -- see unresolved list" if u_count else "OK"
        header = (
            f"-- Generated Oracle INSERT for {model.target.fq}\n"
            f"-- Source: ODI XML semantic parser (db-testing-tool v2)\n"
            f"-- Staging steps: {len(model.staging_steps)}\n"
            f"-- Final columns: {len(final_cols)}\n"
            f"-- Unresolved expressions: {u_count}\n"
            f"-- Status: {status}\n"
        )

    insert_block = (
        f"INSERT INTO {model.target.fq}\n"
        f"(\n"
        f"  {col_list}\n"
        f")\n"
        f"SELECT\n"
        f"  {sel_list}\n"
        f"FROM {last_step_name};"
    )

    full_sql = f"{header}\n{with_clause}\n{insert_block}" if header else f"{with_clause}\n{insert_block}"

    return EmitResult(sql=full_sql, unresolved=unresolved_report, warnings=warnings)


def emit_insert_strict(model: ODIModel) -> str:
    """Convenience wrapper: raise EmitError on any unresolved, return SQL string."""
    return emit_insert(model, strict=True).sql


def emit_insert_permissive(model: ODIModel) -> EmitResult:
    """Convenience wrapper: substitute NULL for unresolved, return full result."""
    return emit_insert(model, strict=False)
