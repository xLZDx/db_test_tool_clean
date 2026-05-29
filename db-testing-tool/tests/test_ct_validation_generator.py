"""Tests for scripts/generate_ct_vs_target_validation.py.

Operator-locked security gate (2026-05-29 Phase 7.2): the validation
SQL generator must reject malicious identifiers at generation time so
that the .sql file the operator runs on Oracle never contains injected
queries.  These tests pin the _quote() validator.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

from generate_ct_vs_target_validation import (  # noqa: E402
    IdentifierError,
    _quote,
)


def test_quote_accepts_simple_uppercase():
    assert _quote("IKOROSTELEV") == "IKOROSTELEV"


def test_quote_uppercases_lowercase_input():
    assert _quote("avy_fact_side") == "AVY_FACT_SIDE"


def test_quote_accepts_underscore_digits_dollar_hash():
    assert _quote("T1_$#X") == "T1_$#X"


def test_quote_rejects_space_in_identifier():
    with pytest.raises(IdentifierError):
        _quote("BAD NAME")


def test_quote_rejects_semicolon():
    with pytest.raises(IdentifierError):
        _quote("IKOROSTELEV;DROP")


def test_quote_rejects_double_quote():
    with pytest.raises(IdentifierError):
        _quote('FOO"BAR')


def test_quote_rejects_parenthesis():
    with pytest.raises(IdentifierError):
        _quote("FOO(1)")


def test_quote_rejects_dot_in_identifier():
    """Dot is intentionally rejected -- callers must pass the schema
    and table separately so _quote can validate each piece."""
    with pytest.raises(IdentifierError):
        _quote("SCHEMA.TABLE")


def test_quote_rejects_leading_digit():
    with pytest.raises(IdentifierError):
        _quote("1FOO")


def test_quote_rejects_none():
    with pytest.raises(IdentifierError):
        _quote(None)  # type: ignore[arg-type]


def test_quote_rejects_empty_string():
    with pytest.raises(IdentifierError):
        _quote("")


def test_quote_rejects_injection_payload_with_union_select():
    """The canonical SQL injection vector through a CLI arg."""
    with pytest.raises(IdentifierError):
        _quote("FOO UNION SELECT password FROM SYS.USER$--")
