"""Decorated entry points used by run_example.py.

Living in a real module (not __main__) means qprov.verify can resolve and
re-invoke these functions in a fresh Python process.
"""
from __future__ import annotations

from fractions import Fraction

import sympy as sp

import q_real_python as M
from qprov import tracked


@tracked(tags={"experiment": "qprov-demo", "module": "q_real_python"})
def q_real_truncated(x_repr: str, N: int) -> list[int]:
    """Mirror of `q_real_truncated` from the project's Sage module."""
    return M.q_real_truncated(x_repr, N)


@tracked(tags={"experiment": "qprov-demo", "module": "q_real_python"})
def q_rational_series(p: int, s: int, N: int) -> list[int]:
    """First N coefficients of [p/s]_q via the same MGO machinery."""
    a = M._make_even_length(list(sp.continued_fraction(Fraction(p, s))))
    series = M._mgo_build_series(a, N + 5)
    v, coeffs = series
    out = [0] * N
    for k in range(N):
        idx = k - v
        if 0 <= idx < len(coeffs):
            out[k] = int(coeffs[idx])
    return out
