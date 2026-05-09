"""Pure-Python q-real implementation, mirroring MGO Prop 1.1.

The qnumbers project's primary implementation lives in
`computations/sage/q_continued_fraction.sage` and uses Sage's Laurent series
ring. This module re-implements the same construction in pure Python with
truncated polynomial arithmetic, so qprov can be demonstrated without Sage.

Math is identical: same MGO formula, same even-length CF normalisation, same
Prop 1.1 stopping criterion (continue the CF until the partial-quotient sum
S_n exceeds N, then S_n - 1 stable coefficients are guaranteed).

Representation: every q-series we manipulate is a tuple `(v, c)` where `v`
is the integer valuation and `c` is the dense list of coefficients
[c_v, c_{v+1}, ...] mod q^{v + N + slack}. Coefficients are exact ints.
Division uses Newton's identity for power-series inversion.

References:
  - Morier-Genoud and Ovsienko, "q-deformed rationals and q-continued
    fractions", Forum Math. Sigma 8 (2020), e13.  Definition 1.1, Prop 1.1.
  - computations/sage/q_continued_fraction.sage  (Sage version)
"""
from __future__ import annotations

from fractions import Fraction
from typing import Iterable

import sympy as sp


# ---------- continued fractions ----------

def _cf_partials(x_repr: str, max_sum: int, max_depth: int = 500) -> list[int]:
    """Return enough partial quotients of x = sympify(x_repr) so that their
    sum is >= max_sum + 1 (MGO Prop 1.1 with N := max_sum)."""
    x = sp.sympify(x_repr)
    out: list[int] = []
    S = 0
    for k, ai in enumerate(sp.continued_fraction_iterator(x)):
        out.append(int(ai))
        S += int(ai)
        if S >= max_sum + 1:
            return out
        if k >= max_depth:
            break
    return out


def _make_even_length(a: list[int]) -> list[int]:
    a = list(a)
    if len(a) % 2 == 0:
        return a
    if a[-1] >= 2:
        a[-1] -= 1
        a.append(1)
        return a
    if len(a) >= 2 and a[-1] == 1:
        a.pop()
        a[-1] += 1
        return a
    raise ValueError(f"cannot make even-length CF from {a!r}")


# ---------- truncated Laurent series in q ----------
# Internal representation: (valuation, coefficients) where coefficients is a
# list of ints starting at q^valuation and going up to length-1 powers higher.

Series = tuple[int, list[int]]


def _trim(s: Series, prec: int) -> Series:
    v, c = s
    end = v + len(c)
    if end > prec:
        c = c[: max(prec - v, 0)]
    return v, c


def _normalise(s: Series) -> Series:
    """Remove trailing zeros and bump valuation past leading zeros."""
    v, c = s
    while c and c[-1] == 0:
        c.pop()
    if not c:
        return 0, []
    while c and c[0] == 0:
        c.pop(0)
        v += 1
    return v, c


def _add(a: Series, b: Series, prec: int) -> Series:
    av, ac = a
    bv, bc = b
    v = min(av, bv)
    end = max(av + len(ac), bv + len(bc))
    end = min(end, prec)
    out = [0] * (end - v)
    for i, x in enumerate(ac):
        if av + i < end:
            out[av + i - v] += x
    for i, x in enumerate(bc):
        if bv + i < end:
            out[bv + i - v] += x
    return _normalise((v, out))


def _mul(a: Series, b: Series, prec: int) -> Series:
    av, ac = a
    bv, bc = b
    v = av + bv
    if not ac or not bc:
        return 0, []
    max_len = max(prec - v, 0)
    out = [0] * max_len
    for i, ax in enumerate(ac):
        if ax == 0:
            continue
        # j-th coefficient ends up at i+j, occupying position i+j in `out`
        max_j = max_len - i
        if max_j <= 0:
            break
        for j in range(min(len(bc), max_j)):
            out[i + j] += ax * bc[j]
    return _normalise((v, out))


def _scalar_mul(a: Series, s: int, prec: int) -> Series:
    v, c = a
    if s == 0:
        return 0, []
    return _normalise((v, [x * s for x in c]))


def _add_int(a: Series, n: int, prec: int) -> Series:
    return _add(a, (0, [n] if n != 0 else []), prec)


def _q_pow(k: int, prec: int) -> Series:
    """The series q^k as (k, [1]), trimmed to prec."""
    if k >= prec:
        return 0, []
    return k, [1]


def _invert(a: Series, prec: int) -> Series:
    """Inverse of a Laurent series with leading coefficient +-1.

    For a = q^v (1 + r) where r has positive valuation, 1/a = q^{-v} (1 - r +
    r^2 - ...). We compute via Newton: y_{k+1} = y_k * (2 - a * y_k).
    Requires the leading coefficient to be 1 or -1 (which is always the case
    in MGO's recursion - the innermost term is `[a_{2m}]_{q^{-1}}` which has
    leading 1).
    """
    v, c = a
    if not c:
        raise ZeroDivisionError("invert of zero series")
    leading = c[0]
    if leading not in (1, -1):
        # fall back to fraction arithmetic for general leading coefficient
        return _invert_general(a, prec)
    sign = leading
    # work with monic series u = sign * a / q^v  =  1 + (sign*c[1])q + ...
    monic_coeffs = [sign * x for x in c]
    # Newton iteration on monic: y_0 = 1, y_{k+1} = y_k * (2 - u * y_k)
    target_len = max(prec - (-v), 1)  # we'll multiply by q^{-v} at the end
    y = (0, [1])
    cur_prec = 1
    monic = (0, monic_coeffs)
    while cur_prec < target_len:
        cur_prec = min(cur_prec * 2, target_len)
        # 2 - u * y
        uy = _mul(monic, y, cur_prec)
        two_minus = _add_int(_scalar_mul(uy, -1, cur_prec), 2, cur_prec)
        y = _mul(y, two_minus, cur_prec)
    # y is inverse of (sign * a / q^v); inverse of a is sign * q^{-v} * y
    yv, yc = y
    out = (yv - v, [sign * x for x in yc])
    return _trim(_normalise(out), prec)


def _invert_general(a: Series, prec: int) -> Series:
    """Generic inverse via Fraction coefficients then cast back to int when
    possible. Slow path; only used for non-unit leading coefficient."""
    v, c = a
    if not c:
        raise ZeroDivisionError
    target_len = max(prec - (-v), 1)
    # work in Fraction
    fc = [Fraction(x) for x in c]
    leading = fc[0]
    monic = [x / leading for x in fc]
    y = [Fraction(1)]
    cur = 1
    while cur < target_len:
        cur = min(cur * 2, target_len)
        # u * y
        uy = [Fraction(0)] * cur
        for i, mi in enumerate(monic):
            if i >= cur:
                break
            for j, yj in enumerate(y):
                if i + j >= cur:
                    break
                uy[i + j] += mi * yj
        two_minus = [(-x) for x in uy]
        two_minus[0] += 2
        new_y = [Fraction(0)] * cur
        for i, yi in enumerate(y):
            if i >= cur:
                break
            for j, tm in enumerate(two_minus):
                if i + j >= cur:
                    break
                new_y[i + j] += yi * tm
        y = new_y
    out_coeffs_frac = [yi / leading for yi in y]
    out_coeffs: list[int] = []
    for fr in out_coeffs_frac:
        if fr.denominator != 1:
            raise ValueError("non-integer coefficient in series inverse")
        out_coeffs.append(int(fr.numerator))
    return _trim(_normalise((-v, out_coeffs)), prec)


# ---------- q-numbers ----------

def q_int_series(n: int, prec: int) -> Series:
    """[n]_q as a series at q=0, truncated to q^prec."""
    n = int(n)
    if n == 0:
        return 0, []
    if n > 0:
        coeffs = [1] * min(n, prec)
        return _normalise((0, coeffs))
    # n < 0: -[-n]_q / q^{-n}  =>  valuation -(-n) = n,  coefficients -[1, 1, ..., 1]
    m = -n
    # the result is q^{-m} * (-1) * (1 + q + ... + q^{m-1}) = -q^{-m} - q^{-m+1} - ... - q^{-1}
    coeffs = [-1] * m
    return _trim(_normalise((-m, coeffs)), prec)


def q_int_qinv_series(n: int, prec: int) -> Series:
    """[n]_{q^{-1}} = q^{-(n-1)} [n]_q for n > 0."""
    n = int(n)
    if n == 0:
        return 0, []
    if n > 0:
        coeffs = [1] * min(n, prec - (-(n - 1)))
        return _trim(_normalise((-(n - 1), coeffs)), prec)
    m = -n
    return _scalar_mul(_mul(q_int_qinv_series(m, prec), _q_pow(m, prec), prec), -1, prec)


def _mgo_build_series(a: list[int], prec: int) -> Series:
    n = len(a)
    if n == 0:
        return 0, []

    def term(i: int, ai: int) -> Series:
        if (i + 1) % 2 == 1:
            return q_int_series(ai, prec)
        return q_int_qinv_series(ai, prec)

    def num_above(i: int, ai: int) -> Series:
        return _q_pow(ai if (i + 1) % 2 == 1 else -ai, prec)

    result = term(n - 1, a[n - 1])
    for i in range(n - 2, -1, -1):
        inv = _invert(result, prec)
        result = _add(term(i, a[i]), _mul(num_above(i, a[i]), inv, prec), prec)
    return result


def q_real_truncated(x_repr: str, N: int) -> list[int]:
    """First N stable Taylor coefficients of [x]_q.

    Args:
        x_repr: sympy-parseable string, e.g. "pi", "sqrt(2)", "(1+sqrt(5))/2", "E".
        N: number of stable coefficients required (per MGO Prop 1.1).

    Returns:
        List of N integers [c_0, c_1, ..., c_{N-1}].
    """
    a = _cf_partials(x_repr, N)
    a = _make_even_length(a)
    prec = N + 5
    series = _mgo_build_series(a, prec)
    v, coeffs = series
    out = [0] * N
    for k in range(N):
        idx = k - v
        if 0 <= idx < len(coeffs):
            out[k] = int(coeffs[idx])
    return out


# ---------- helpers used by claim generators ----------

def first_nonzero_coefficient_index(coeffs: Iterable[int]) -> int:
    for i, c in enumerate(coeffs):
        if c != 0:
            return i
    return -1


def first_negative_coefficient_index(coeffs: Iterable[int]) -> int:
    for i, c in enumerate(coeffs):
        if c < 0:
            return i
    return -1


def coefficient_max_abs(coeffs: Iterable[int]) -> int:
    return max((abs(c) for c in coeffs), default=0)


def number_of_zeros(coeffs: Iterable[int]) -> int:
    return sum(1 for c in coeffs if c == 0)
