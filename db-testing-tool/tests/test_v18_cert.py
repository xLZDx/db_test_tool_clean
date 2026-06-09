"""Gate V2 tests for the v18 EXPLAIN-PLAN certification logic
(tools/validate_v18_inserts.py) -- pure logic, no DB.

Proves the anti-false-green classifier behaves: a privilege-only failure on a
fully-resolved statement is PASS_RESOLVED; a missing object that exists in the
production KB (but not in the mirror) is a KNOWN_MISMATCH, not a defect; a
missing object absent from the KB, or any other ORA, is FAIL_SQL.
"""
import pytest

from tools.validate_v18_inserts import select_only, _missing_object, classify


# --- pure string helpers -----------------------------------------------------

def test_select_only_strips_insert_prefix():
    sql = "INSERT INTO TRANSACTIONS_OWNER.AVY_FACT (A, B)\nSELECT 1 A, 2 B FROM DUAL"
    assert select_only(sql) == "SELECT 1 A, 2 B FROM DUAL"


def test_select_only_handles_nested_parens_in_collist():
    sql = "INSERT INTO O.T (A, B)\nWITH X AS (SELECT 1 N FROM DUAL) SELECT N A, N B FROM X"
    assert select_only(sql).startswith("WITH X AS")


def test_select_only_none_when_no_insert():
    assert select_only("SELECT 1 FROM DUAL") is None


def test_missing_object_extracts_last_quoted():
    err = 'ORA-00942: table or view "SSDS_DAL_OWNER"."ENTERPRISE_ENTITY_RISK_DIMENSION_V" does not exist'
    assert _missing_object(err) == "ENTERPRISE_ENTITY_RISK_DIMENSION_V"


def test_missing_object_none_when_unquoted():
    assert _missing_object("ORA-00904: invalid identifier") is None


# --- classify() with a fake cursor -------------------------------------------

class _FakeRaw:
    def rollback(self):
        pass


class _FakeCur:
    """execute() consults a responder(stmt)->None|raises to simulate Oracle."""
    def __init__(self, responder):
        self._responder = responder

    def execute(self, stmt):
        self._responder(stmt)


class _FakeKB:
    def __init__(self, tables):
        self._table_index = tables


_INSERT_SQL = "INSERT INTO O.T (A, B)\nSELECT 1 A, 2 B FROM DUAL"


def test_classify_pass_when_clean():
    cur = _FakeCur(lambda s: None)
    assert classify(cur, _FakeRaw(), _INSERT_SQL, _FakeKB({})) == ("PASS", "")


def test_classify_pass_resolved_on_privilege_with_clean_select():
    def responder(stmt):
        if "INSERT INTO" in stmt:
            raise Exception('ORA-41900: missing INSERT privilege on "O"."T"')
        # SELECT-only explains clean
        return None
    verdict, detail = classify(_FakeCur(responder), _FakeRaw(), _INSERT_SQL, _FakeKB({}))
    assert verdict == "PASS_RESOLVED"
    assert "SELECT explains clean" in detail


def test_classify_known_mismatch_when_missing_object_in_kb():
    def responder(stmt):
        raise Exception('ORA-00942: table or view "SSDS_DAL_OWNER"."FOO_V" does not exist')
    kb = _FakeKB({"FOO_V": "SSDS_DAL_OWNER.FOO_V"})
    verdict, detail = classify(_FakeCur(responder), _FakeRaw(), _INSERT_SQL, kb)
    assert verdict == "KNOWN_MISMATCH"
    assert "FOO_V" in detail


def test_classify_fail_sql_when_missing_object_not_in_kb():
    def responder(stmt):
        raise Exception('ORA-00942: table or view "BOGUS"."NOPE" does not exist')
    verdict, _ = classify(_FakeCur(responder), _FakeRaw(), _INSERT_SQL, _FakeKB({}))
    assert verdict == "FAIL_SQL"


def test_classify_fail_sql_on_other_ora():
    def responder(stmt):
        raise Exception('ORA-00904: "BAR": invalid identifier')
    verdict, _ = classify(_FakeCur(responder), _FakeRaw(), _INSERT_SQL, _FakeKB({}))
    assert verdict == "FAIL_SQL"
