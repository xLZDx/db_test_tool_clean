"""Golden fixture test: OdiXmlParser on the real ODI XML.

Asserts that at least one column in STEP1 resolves to a canonical
source_table.column through the real join graph, using NO regex on the
resolved result — only the IR types are inspected.

The fixture file is the actual scenario export for AVY_FACT_SIDE.
If it is absent (CI without the XML), the test is skipped.
"""
from __future__ import annotations

import pathlib
import pytest

# ── Fixture path (relative to project root) ───────────────────────────────────
_XML_PATH = pathlib.Path(__file__).parent.parent / (
    "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
)


def _load_xml_bytes() -> bytes:
    return _XML_PATH.read_bytes()


pytestmark = pytest.mark.skipif(
    not _XML_PATH.exists(),
    reason="ODI XML fixture not present; skipped in CI without the file",
)


# ── Import the parser under test ──────────────────────────────────────────────
from app.sql_model.odi_parser import OdiXmlParser
from app.sql_model.types import (
    ODIModel,
    Provenance,
    ResolvedColumn,
    StagingStep,
    UnresolvedExpr,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_model() -> ODIModel:
    parser = OdiXmlParser(
        target_schema="IKOROSTELEV",
        target_table="AVY_FACT_SIDE",
    )
    return parser.parse_bytes(_load_xml_bytes())


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_model_has_five_staging_steps():
    """The real ODI XML must produce exactly 5 staging steps (STEP1..STEP5)."""
    model = _parse_model()
    assert len(model.staging_steps) == 5, (
        f"Expected 5 staging steps, got {len(model.staging_steps)}: "
        f"{[s.name for s in model.staging_steps]}"
    )


def test_staging_step_ids_are_sequential():
    """Step IDs must be 1, 2, 3, 4, 5 in that order."""
    model = _parse_model()
    ids = [s.step_id for s in model.staging_steps]
    assert ids == [1, 2, 3, 4, 5], f"Step ID sequence wrong: {ids}"


def test_step1_has_column_mappings():
    """STEP1 must contain at least 10 column mappings (it has ~100+ in the real file)."""
    model = _parse_model()
    step1 = model.staging_steps[0]
    assert len(step1.column_mappings) >= 10, (
        f"STEP1 has only {len(step1.column_mappings)} column mappings"
    )


def test_step1_has_resolved_columns_via_join_graph():
    """At least one STEP1 column must resolve to a ResolvedColumn with provenance=ODI
    and a non-None TableRef — meaning the alias was found in the FROM clause.

    This is the CORE correctness assertion: the parser traced an alias -> table
    binding through the real join graph without any regex on the output.
    """
    model = _parse_model()
    step1 = model.staging_steps[0]

    resolved_with_table: list[str] = []
    for cm in step1.column_mappings:
        if (
            isinstance(cm.source, ResolvedColumn)
            and cm.source.provenance == Provenance.ODI
            and cm.source.ref is not None
            and cm.source.ref.table != ""
        ):
            resolved_with_table.append(cm.target_col)

    assert resolved_with_table, (
        "No STEP1 column resolved to a non-None TableRef via the join graph. "
        "This means _parse_from_clause or _classify_source_expr is broken.\n"
        "Sample column_mappings:\n"
        + "\n".join(
            f"  {cm.target_col}: {type(cm.source).__name__} "
            f"reason={getattr(cm.source, 'reason', 'n/a')}"
            for cm in step1.column_mappings[:10]
        )
    )


def test_step1_source_bindings_include_ccal_repl_owner():
    """STEP1 FROM clause must contain at least one table from CCAL_REPL_OWNER schema
    (the primary source schema for AVY_FACT_SIDE staging).
    """
    model = _parse_model()
    step1 = model.staging_steps[0]

    schemas_found = {b.ref.schema.upper() for b in step1.source_bindings if b.ref.schema}
    assert "CCAL_REPL_OWNER" in schemas_found, (
        f"CCAL_REPL_OWNER not found in STEP1 source bindings. "
        f"Schemas found: {sorted(schemas_found)}"
    )


def test_step1_source_bindings_include_j_avy_fact():
    """STEP1 FROM clause must contain the driving table J_AVY_FACT
    (alias for CCAL_REPL_OWNER.J$AVY_FACT / J_AVY_FACT).
    """
    model = _parse_model()
    step1 = model.staging_steps[0]

    aliases = {b.alias.upper() for b in step1.source_bindings}
    # After template resolution the alias appears as J_AVY_FACT
    assert "J_AVY_FACT" in aliases, (
        f"Alias J_AVY_FACT not found in STEP1 FROM clause. "
        f"Aliases: {sorted(aliases)}"
    )


def test_step1_has_join_edges():
    """STEP1 WHERE clause must yield at least 5 join edges
    (it has many equality predicates between dimension tables).
    """
    model = _parse_model()
    step1 = model.staging_steps[0]
    assert len(step1.join_graph) >= 5, (
        f"STEP1 join_graph has only {len(step1.join_graph)} edges"
    )


def test_merge_block_produces_final_insert_columns():
    """The MERGE block must yield at least 50 final INSERT columns
    (AVY_FACT_SIDE has 369 attributes in total).
    """
    model = _parse_model()
    assert len(model.final_insert_columns) >= 50, (
        f"final_insert_columns has only {len(model.final_insert_columns)} entries"
    )


def test_no_unresolved_expr_for_simple_alias_dot_col():
    """Any expression of the form ALIAS.COL where ALIAS is in the FROM clause
    must NOT produce UnresolvedExpr with reason=ALIAS_NOT_IN_JOIN_GRAPH.

    This catches the case where _parse_from_clause failed silently.
    """
    model = _parse_model()
    step1 = model.staging_steps[0]
    alias_set = {b.alias.upper() for b in step1.source_bindings}

    bad: list[str] = []
    for cm in step1.column_mappings:
        if (
            isinstance(cm.source, UnresolvedExpr)
            and cm.source.reason == "ALIAS_NOT_IN_JOIN_GRAPH"
        ):
            # Check if the alias actually IS in the FROM clause (would be a parser bug)
            expr = cm.source.original_expr.strip()
            if "." in expr:
                alias_part = expr.split(".")[0].strip("( ")
                if alias_part.upper() in alias_set:
                    bad.append(
                        f"{cm.target_col}: expr={expr!r} alias={alias_part!r} "
                        f"IS in FROM but was ALIAS_NOT_IN_JOIN_GRAPH"
                    )

    assert not bad, (
        "Parser produced ALIAS_NOT_IN_JOIN_GRAPH for aliases that ARE in the FROM clause:\n"
        + "\n".join(bad)
    )


def test_resolved_column_canonical_repr():
    """For the first STEP1 column that resolves to ResolvedColumn with ODI provenance
    and a non-None ref, verify the canonical fields are all properly set:
    - ref.schema is non-empty
    - ref.table is non-empty
    - column is non-empty
    - expr_sql is non-empty
    """
    model = _parse_model()
    step1 = model.staging_steps[0]

    candidate = None
    for cm in step1.column_mappings:
        src = cm.source
        if (
            isinstance(src, ResolvedColumn)
            and src.provenance == Provenance.ODI
            and src.ref is not None
            and src.ref.table
        ):
            candidate = (cm.target_col, src)
            break

    assert candidate is not None, "No suitable candidate found — see test_step1_has_resolved_columns_via_join_graph"

    col_name, src = candidate
    assert src.ref.schema, f"{col_name}: ref.schema is empty"
    assert src.ref.table, f"{col_name}: ref.table is empty"
    assert src.column, f"{col_name}: column is empty"
    assert src.expr_sql, f"{col_name}: expr_sql is empty"


def test_step2_reads_step1_stg():
    """STEP2 must have at least one source binding whose table is SSDS_AVY_FACT_STEP1_STG
    (STEP2 reads from STEP1's staging table for the LISTAGG aggregation).
    """
    model = _parse_model()
    assert len(model.staging_steps) >= 2
    step2 = model.staging_steps[1]

    tables = {b.ref.table.upper() for b in step2.source_bindings}
    assert "SSDS_AVY_FACT_STEP1_STG" in tables, (
        f"STEP2 source bindings do not include SSDS_AVY_FACT_STEP1_STG. "
        f"Tables found: {sorted(tables)}"
    )


def test_final_insert_columns_are_uppercase_identifiers():
    """Every entry in final_insert_columns must be a valid Oracle identifier
    (uppercase alphanumeric + _ + $ + #, starting with a letter).
    """
    import re
    model = _parse_model()
    bad = [c for c in model.final_insert_columns if not re.match(r"^[A-Z][A-Z0-9_#$]*$", c)]
    assert not bad, f"Non-identifier entries in final_insert_columns: {bad[:20]}"
