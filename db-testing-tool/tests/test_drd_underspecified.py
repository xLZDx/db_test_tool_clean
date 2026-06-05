"""R5 step 3: scan_underspecified_joins surfaces neutralized (ON 1=0) joins as a
distinct DRD_UNDERSPECIFIED verdict. Functional: feeds synthetic emitted SQL with
the three real give-up marker comments and asserts the structured records.
"""
from app.services.control_table_service import (
    DRD_UNDERSPECIFIED,
    scan_underspecified_joins,
)

CLEAN = """INSERT INTO T (A, B)
SELECT O.A AS A, O.B AS B
FROM odi_final_source O
LEFT JOIN DIM D ON O.D_ID = D.ID;"""

UNDEF_ALIAS = (
    "LEFT JOIN CL_VAL CV ON 1 = 0 /* neutralized: undefined alias(es) CV,ACG */"
)
# real give-up comments carry an em-dash; the scanner matches on the ASCII substring
LOOKUP = "LEFT JOIN X ON 1 = 0 /* WARNING: lookup key unresolved — joined cols will be NULL */"
SELFJOIN = "LEFT JOIN Y ON 1 = 0 /* WARNING: self-join key unresolved — joined cols will be NULL */"


def test_clean_sql_has_no_underspecified():
    assert scan_underspecified_joins(CLEAN) == []


def test_empty_and_none():
    assert scan_underspecified_joins("") == []
    assert scan_underspecified_joins(None) == []


def test_undefined_alias_extracts_aliases():
    rows = scan_underspecified_joins("SELECT 1 FROM DUAL " + UNDEF_ALIAS)
    assert len(rows) == 1
    r = rows[0]
    assert r["verdict"] == DRD_UNDERSPECIFIED
    assert r["kind"] == "undefined_alias"
    assert set(r["aliases"]) == {"CV", "ACG"}


def test_lookup_key_unresolved_kind():
    rows = scan_underspecified_joins(LOOKUP)
    assert len(rows) == 1 and rows[0]["kind"] == "lookup_key_unresolved"
    assert rows[0]["aliases"] == []


def test_self_join_key_unresolved_kind():
    rows = scan_underspecified_joins(SELFJOIN)
    assert len(rows) == 1 and rows[0]["kind"] == "self_join_key_unresolved"


def test_multiple_markers_counted():
    sql = "\n".join(["SELECT 1 FROM DUAL", UNDEF_ALIAS, LOOKUP, SELFJOIN])
    rows = scan_underspecified_joins(sql)
    assert len(rows) == 3
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["lookup_key_unresolved", "self_join_key_unresolved", "undefined_alias"]
    assert all(r["verdict"] == DRD_UNDERSPECIFIED for r in rows)


def test_generic_no_hardcoded_names():
    # works for any alias/table, not just the taxlot/avy ones
    sql = "JOIN FOO_BAR FB ON 1 = 0 /* neutralized: undefined alias(es) ZZZ */"
    rows = scan_underspecified_joins(sql)
    assert rows and rows[0]["aliases"] == ["ZZZ"]
