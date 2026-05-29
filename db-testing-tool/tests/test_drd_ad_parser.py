"""Tests for app.sql_model.drd_ad_parser -- generic DRD col-AD parser.

Verifies:
  * basic JOIN extraction (LEFT/INNER/JOIN variants)
  * multi-predicate ON clauses
  * "Look up using A.X = B.Y" shorthand
  * trivial / empty cells produce empty rules
  * predicate matching is order- and alias-insensitive
  * works for arbitrary identifiers (no table-name hard-coding)
"""
from __future__ import annotations

import pytest

from app.sql_model.drd_ad_parser import (
    DrdAdRule,
    parse_drd_ad,
    predicate_matches,
    compare_drd_ad_joins,
)


def test_empty_text_returns_empty_rule():
    assert parse_drd_ad("").is_empty
    assert parse_drd_ad(None).is_empty
    assert parse_drd_ad("None").is_empty
    assert parse_drd_ad("-").is_empty
    assert parse_drd_ad("n/a").is_empty


def test_lookup_using_shorthand():
    rule = parse_drd_ad("Look up using TXN.SRC_TXN_TP=SHDW_TXN_TP.SRC_TXN_TP")
    assert len(rule.lookup_pairs) == 1
    p = rule.lookup_pairs[0]
    assert p.norm_left == "TXN.SRC_TXN_TP"
    assert p.norm_right == "SHDW_TXN_TP.SRC_TXN_TP"


def test_left_join_with_alias_and_single_predicate():
    text = (
        "ccal_repl_owner.txn t\n"
        "left join ccal_repl_owner.impct_action_lku lk\n"
        "  ON t.src_actn_code = impct_action_lku.action_code"
    )
    rule = parse_drd_ad(text)
    assert rule.base_table == "CCAL_REPL_OWNER.TXN"
    assert rule.base_alias == "T"
    assert len(rule.joins) == 1
    j = rule.joins[0]
    assert j.join_type == "LEFT JOIN"
    assert j.fq_table == "CCAL_REPL_OWNER.IMPCT_ACTION_LKU"
    assert j.alias == "LK"
    assert len(j.predicates) == 1
    assert j.predicates[0].norm_left == "T.SRC_ACTN_CODE"
    assert j.predicates[0].norm_right == "IMPCT_ACTION_LKU.ACTION_CODE"


def test_multi_predicate_on_clause():
    text = (
        "ccal_repl_owner.txn t\n"
        "left join ccal_repl_owner.cl_val cv ON cv.cl_val_id = t.SRC_CNCL_RSN_ID\n"
        "  and cs.cl_scm_id = '102'"
    )
    rule = parse_drd_ad(text)
    assert len(rule.joins) == 1
    preds = rule.joins[0].predicates
    # First predicate is the column join; second is the literal-constant filter
    assert any(
        p.norm_left == "CV.CL_VAL_ID" and p.norm_right == "T.SRC_CNCL_RSN_ID"
        for p in preds
    )


def test_two_joins_in_one_cell():
    text = (
        "select fa.FA_NUM from\n"
        "ccal_repl_owner.txn t\n"
        "LEFT JOIN ccsi_owner.AR_GRP_SUBDIM fa ON t.AR_ID = fa.AR_ID\n"
        "  and t.td >= fa.EFF_DT\n"
        "  and t.td < fa.END_DT;\n"
        "LEFT JOIN reference_repl_owner.cl_val v ON v.cl_val_id = fa.STATUS_ID"
    )
    rule = parse_drd_ad(text)
    assert len(rule.joins) == 2
    aliases = {j.alias for j in rule.joins}
    assert aliases == {"FA", "V"}


def test_predicate_matches_order_insensitive():
    rule = parse_drd_ad("Look up using A.X=B.Y")
    p = rule.lookup_pairs[0]
    assert predicate_matches(p, "B.Y = A.X")
    assert predicate_matches(p, "A.X = B.Y")
    assert not predicate_matches(p, "A.X = B.Z")


def test_predicate_matches_alias_insensitive():
    rule = parse_drd_ad("Look up using t.src_actn_code = impct_action_lku.action_code")
    p = rule.lookup_pairs[0]
    # ODI may use a different alias for IMPCT_ACTION_LKU; bare names must still match
    assert predicate_matches(p, "T.SRC_ACTN_CODE = LK.ACTION_CODE")
    # ...and even if both sides are aliased
    assert predicate_matches(p, "ALIAS_X.SRC_ACTN_CODE = ALIAS_Y.ACTION_CODE")


def test_compare_drd_ad_joins_all_satisfied():
    rule = parse_drd_ad(
        "ccal_repl_owner.txn t\n"
        "left join ccal_repl_owner.cl_val cv ON cv.cl_val_id = t.SRC_CNCL_RSN_ID"
    )
    cmp = compare_drd_ad_joins(rule, ["CV.CL_VAL_ID = T.SRC_CNCL_RSN_ID"])
    assert cmp["all_satisfied"]
    assert len(cmp["satisfied"]) == 1
    assert not cmp["unsatisfied"]


def test_compare_drd_ad_joins_unsatisfied():
    rule = parse_drd_ad(
        "ccal_repl_owner.txn t\n"
        "left join ccal_repl_owner.cl_val cv ON cv.cl_val_id = t.SRC_CNCL_RSN_ID"
    )
    # ODI joined on a different column entirely
    cmp = compare_drd_ad_joins(rule, ["CV.CL_VAL_ID = T.WRONG_FIELD"])
    assert not cmp["all_satisfied"]
    assert len(cmp["unsatisfied"]) == 1


def test_generic_no_hardcoded_names_round_trip():
    """Same code must work for arbitrary tables / columns / schemas."""
    rule = parse_drd_ad(
        "MYSCHEMA.MY_TABLE my_alias\n"
        "INNER JOIN OTHER_SCHEMA.LOOKUP_TBL lk ON my_alias.foo_id = lk.foo_id\n"
        "  and lk.active_flag = 'Y'"
    )
    assert rule.base_table == "MYSCHEMA.MY_TABLE"
    assert rule.base_alias == "MY_ALIAS"
    assert len(rule.joins) == 1
    j = rule.joins[0]
    assert j.fq_table == "OTHER_SCHEMA.LOOKUP_TBL"
    assert j.alias == "LK"
    assert any(
        p.norm_left == "MY_ALIAS.FOO_ID" and p.norm_right == "LK.FOO_ID"
        for p in j.predicates
    )
