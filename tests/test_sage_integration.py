"""Sage-backed integration test.

Skipped gracefully when Sage isn't installed in the active env. When Sage is
available, this:
  1. constructs Sage Integers, Rationals, and Laurent polynomials
  2. feeds them through canonical_dumps -> canonical_loads -> hash_value
  3. wraps `q_real_truncated` from the project's `q_continued_fraction.sage`
     (if findable) and verifies bit-identical re-run via qprov.verify
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sage_all = pytest.importorskip("sage.all")


def test_sage_integer_serializes():
    from sage.all import Integer
    from qprov.serialize import canonical_dumps, canonical_loads, hash_value

    big = Integer(10) ** 60
    text = canonical_dumps(big)
    assert "sage_int" in text
    back = canonical_loads(text)
    assert int(back) == int(big)
    assert hash_value(big) == hash_value(canonical_loads(text))


def test_sage_rational_serializes():
    from sage.all import QQ
    from qprov.serialize import canonical_dumps, canonical_loads

    r = QQ((22, 7))
    text = canonical_dumps(r)
    assert "sage_rat" in text
    back = canonical_loads(text)
    assert int(back.numerator()) == 22
    assert int(back.denominator()) == 7


def test_sage_laurent_serializes_and_hashes_stably():
    from sage.all import LaurentSeriesRing, ZZ, Integer
    from qprov.serialize import canonical_dumps, hash_value

    L = LaurentSeriesRing(ZZ, "q", default_prec=10)
    q = L.gen()
    f = 1 + q + Integer(2) * q**2 + Integer(3) * q**3

    a = canonical_dumps(f)
    assert "sage_laurent" in a
    # Re-hash a separately constructed but equal series
    g = 1 + q + Integer(2) * q**2 + Integer(3) * q**3
    assert hash_value(f) == hash_value(g)


def _find_q_continued_fraction_sage() -> str | None:
    """Look up `computations/sage/q_continued_fraction.sage` by walking up
    parent directories from this test module, returning its path if a
    sibling research tree provides one.
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / "computations" / "sage" / "q_continued_fraction.sage"
        if candidate.is_file():
            return str(candidate)
    return None


def _load_q_real_truncated():
    """Preparse the .sage file via sage.repl.load.load_wrap and exec into a ns."""
    sage_path = _find_q_continued_fraction_sage()
    if sage_path is None:
        pytest.skip("q_continued_fraction.sage not found in repo")
    from sage.misc.sage_eval import sage_eval  # noqa: F401
    from sage.repl.preparse import preparse

    with open(sage_path, "r", encoding="utf-8") as f:
        src = f.read()
    code = preparse(src)
    ns: dict = {"__name__": "qprov_sage_target"}
    exec(compile(code, sage_path, "exec"), ns)
    return ns["q_real_truncated"]


def test_q_real_truncated_roundtrips_through_qprov():
    """End-to-end: decorate q_real_truncated, run it, verify hashes are stable."""
    q_real_truncated = _load_q_real_truncated()
    from sage.all import pi as sage_pi
    from qprov import tracked, find
    from qprov.serialize import hash_value

    decorated = tracked(tags={"constant": "pi", "test": "sage_integration"})(
        q_real_truncated
    )
    out1 = decorated(sage_pi, 50)
    out2 = decorated(sage_pi, 50)  # idempotent
    assert hash_value(out1) == hash_value(out2)

    comps = find(tags={"test": "sage_integration"})
    assert len(comps) == 1, "decorator should collapse identical calls"
    assert comps[0].output_hash == hash_value(out1)
