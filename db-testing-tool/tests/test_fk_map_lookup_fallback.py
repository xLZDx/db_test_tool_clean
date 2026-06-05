"""R5 step 4: _fk_map_lookup_fallback -- READ-ONLY FK-map resolution at the lookup
give-up site. Functional: builds synthetic FK maps via fk_map_service and asserts
the helper resolves only when exactly one non-conflicted edge matches + the source
column validates in the PDM. None/empty map -> "" (give-up preserved).
"""
from app.services.control_table_service import _fk_map_lookup_fallback
from app.services import fk_map_service as fk


def _map_with(*edges):
    """edges: (base_schema, base_table, fk_col, ref_schema, ref_table, ref_col[, source])"""
    m = fk.new_fk_map(3)
    for e in edges:
        src = e[6] if len(e) > 6 else "pdm"
        fk.upsert_join(m, e[0], e[1], e[2], e[3], e[4], e[5], source=src)
    return m


def test_none_or_empty_map_returns_empty():
    assert _fk_map_lookup_fallback(None, "S.CL_VAL", "CL_VAL_ID", "SRC_T", None) == ""
    assert _fk_map_lookup_fallback(fk.new_fk_map(3), "S.CL_VAL", "CL_VAL_ID", "SRC_T", None) == ""


def test_unique_matching_edge_resolves_fk_col():
    m = _map_with(("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "CL_VAL_ID"))
    got = _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", None)
    assert got == "STM_ID"


def test_base_table_must_match_source():
    m = _map_with(("STG", "OTHER_T", "STM_ID", "REF", "CL_VAL", "CL_VAL_ID"))
    # source table is SRC_T, edge base is OTHER_T -> no match
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", None) == ""


def test_ref_col_must_match_join_col_when_known():
    m = _map_with(("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "OTHER_ID"))
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", None) == ""
    # when join_col is unknown (blank), ref_col is not constrained -> resolves
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "", "STG.SRC_T", None) == "STM_ID"


def test_ambiguous_multiple_matches_returns_empty():
    m = _map_with(
        ("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "CL_VAL_ID"),
        ("STG", "SRC_T", "OTHER_FK", "REF", "CL_VAL", "CL_VAL_ID"),
    )
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", None) == ""


def test_conflicted_edge_skipped():
    # two equal-priority edges with different ref on the SAME (base, fk_col) -> conflict
    m = _map_with(
        ("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "CL_VAL_ID", "drd"),
        ("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "OTHER_ID", "drd"),
    )
    assert m["joins"]["STG.SRC_T"]["STM_ID"].get("conflict") is True
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", None) == ""


def test_m2_source_column_must_exist_in_pdm():
    m = _map_with(("STG", "SRC_T", "STM_ID", "REF", "CL_VAL", "CL_VAL_ID"))
    # source_entry with columns NOT containing STM_ID -> rejected (stale map guard)
    bad_entry = {"columns": {"OTHER_COL": {}}}
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", bad_entry) == ""
    # source_entry that DOES contain STM_ID -> resolves
    good_entry = {"columns": {"STM_ID": {}, "OTHER_COL": {}}}
    assert _fk_map_lookup_fallback(m, "REF.CL_VAL", "CL_VAL_ID", "STG.SRC_T", good_entry) == "STM_ID"
