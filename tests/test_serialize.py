"""Round-trip and hashing tests for serialize.py."""
from __future__ import annotations

import fractions

import pytest

from qprov.serialize import canonical_dumps, canonical_loads, hash_value


def _roundtrip(value):
    text = canonical_dumps(value)
    back = canonical_loads(text)
    return text, back


def test_native_types_roundtrip():
    cases = [
        None, True, False, 0, 1, -42, 10**40,  # ints incl. bignums
        "hello", "", "non-ascii: π√α",
        [1, 2, 3],
        {"a": 1, "b": [1, 2]},
        3.14,
    ]
    for v in cases:
        text, back = _roundtrip(v)
        assert back == v, f"roundtrip failed for {v!r}: got {back!r}"


def test_canonical_is_sorted_and_compact():
    a = canonical_dumps({"b": 1, "a": 2})
    b = canonical_dumps({"a": 2, "b": 1})
    assert a == b
    assert " " not in a  # compact separators


def test_tuple_distinguished_from_list():
    a = canonical_dumps([1, 2, 3])
    b = canonical_dumps((1, 2, 3))
    assert a != b
    assert canonical_loads(b) == (1, 2, 3)
    assert canonical_loads(a) == [1, 2, 3]


def test_fraction_roundtrip():
    f = fractions.Fraction(3, 7)
    text, back = _roundtrip(f)
    assert back == f


def test_complex_roundtrip():
    z = complex(1.5, -2.25)
    text, back = _roundtrip(z)
    assert back == z


def test_bytes_roundtrip():
    b = b"\x00\x01\x02hello"
    text, back = _roundtrip(b)
    assert back == b


def test_special_floats():
    inf = float("inf")
    neg = float("-inf")
    text_inf, back_inf = _roundtrip(inf)
    text_neg, back_neg = _roundtrip(neg)
    assert back_inf == inf
    assert back_neg == neg


def test_hash_is_stable_across_dict_order():
    h1 = hash_value({"x": 1, "y": 2})
    h2 = hash_value({"y": 2, "x": 1})
    assert h1 == h2


def test_hash_changes_with_value():
    assert hash_value(1) != hash_value(2)
    assert hash_value("a") != hash_value("b")


def test_unknown_type_falls_back_to_repr():
    class Custom:
        def __repr__(self): return "Custom()"
    text = canonical_dumps(Custom())
    assert "__qprov_type__" in text
    assert "Custom()" in text


def test_set_is_canonicalised():
    a = canonical_dumps({1, 2, 3})
    b = canonical_dumps({3, 2, 1})
    assert a == b
