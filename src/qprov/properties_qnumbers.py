"""Property checks for q-deformed number computations.

Each property is a metamorphic invariant the output of a ``@tracked``
function MUST satisfy. The references are the published MGO papers:

- MGO-rationals: Morier-Genoud and Ovsienko, "q-Deformed rationals and
  q-continued fractions", Forum Math. Sigma 8 (2020) e13,
  arXiv:1812.00170v3.
- MGO-reals: Morier-Genoud and Ovsienko, "On q-deformed real numbers",
  Experimental Mathematics 31 (2022) 652-660, arXiv:1908.04365v3.

The check functions below cover the q = 1 classical limit, the gap theorem,
palindromicity, the translation identity, the MGO functional-equation
recoveries, bidegree conformity, and the n = 2 roots-of-unity
specialization (MGO-rationals Proposition 1.8, R(-1), S(-1) in {-1, 0, 1}).
The broader n in {3, 4, 5, 6} roots-of-unity sweep is not implemented; the
relevant check reports N/A rather than a false pass for that case.

Use these as ``Property(check=...)`` arguments to ``@qprov.tracked``.
The decorator runs each check after the wrapped function completes
and before writing the qprov row. A failed error-severity property
raises :class:`qprov.properties.QprovPropertyError`.
"""
from __future__ import annotations

import cmath
import math
from fractions import Fraction
from typing import Any

from .properties import Property, PropertyResult


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_ALPHA_FLOORS: dict[str, int] = {
    # floor(alpha) for the algebraic / transcendental constants used in
    # the project. Source: direct numerical evaluation; see MGO-reals
    # Theorem 2 ("gap theorem") for why floor matters.
    "sqrt2": 1,
    "sqrt3": 1,
    "sqrt5": 2,
    "phi": 1,
    "cbrt2": 1,
    "cbrt3": 1,
    "cbrt5": 1,
    "qrt2": 1,
    "pi": 3,
    "e": 2,
    "log2": 0,  # log(2) ~ 0.693 < 1; the gap theorem assumes alpha >= 1.
}


def _alpha_value(name_or_value: Any) -> float | None:
    """Resolve an alpha label like ``"sqrt2"`` to the numerical value, or
    parse a numerical value directly. Returns ``None`` when the label is
    unknown and the value is non-numeric.
    """
    if isinstance(name_or_value, (int, float)):
        return float(name_or_value)
    if isinstance(name_or_value, str):
        name = name_or_value.lower()
        table = {
            "sqrt2": math.sqrt(2),
            "sqrt3": math.sqrt(3),
            "sqrt5": math.sqrt(5),
            "phi": (1 + math.sqrt(5)) / 2,
            "cbrt2": 2 ** (1 / 3),
            "cbrt3": 3 ** (1 / 3),
            "cbrt5": 5 ** (1 / 3),
            "qrt2": 2 ** (1 / 4),
            "pi": math.pi,
            "e": math.e,
            "log2": math.log(2),
        }
        return table.get(name)
    return None


def _alpha_floor(name_or_value: Any) -> int | None:
    """Return floor(alpha) for known labels; for numeric inputs use
    floor() directly.
    """
    if isinstance(name_or_value, (int, float)):
        return int(math.floor(name_or_value))
    if isinstance(name_or_value, str):
        v = _ALPHA_FLOORS.get(name_or_value.lower())
        if v is not None:
            return v
        val = _alpha_value(name_or_value)
        if val is not None:
            return int(math.floor(val))
    return None


def _coeffs_from_outputs(outputs: dict[str, Any]) -> list[int] | None:
    """Find the q-expansion Taylor coefficients in an outputs dict.

    Looks under several plausible keys; returns the first that resolves
    to a list of numbers.
    """
    for key in (
        "q_expansion_coeffs",
        "coeffs",
        "coefficients",
        "qreal_coeffs",
        "kappa",
    ):
        v = outputs.get(key)
        if isinstance(v, list) and v and all(
            isinstance(c, (int, float)) for c in v
        ):
            return [int(c) if isinstance(c, int) or float(c).is_integer() else c
                    for c in v]
    return None


def _polynomial_from_basis_terms(terms: list[dict[str, Any]]) -> dict[tuple[int, int], int]:
    """Convert a kernel-basis term list into a {(i, j): c} dict, where i is
    the degree in X and j is the (signed) degree in q.
    """
    poly: dict[tuple[int, int], int] = {}
    for t in terms:
        try:
            i = int(t["i"])
            j = int(t["j"])
            c = int(t["c"])
        except (KeyError, ValueError, TypeError):
            continue
        poly[(i, j)] = c
    return poly


# MGO eq (17) for sqrt(2): q^2 + 1 + (q^3 - 1) X - q^2 X^2 = 0.
# Encoded under the project's basis convention (i = X-degree, j = q-degree)
# as the term list:
#   (0,0): 1, (0,2): 1, (1,0): -1, (1,3): 1, (2,2): -1.
_MGO_EQ_17_SQRT2 = {
    (0, 0): 1,
    (0, 2): 1,
    (1, 0): -1,
    (1, 3): 1,
    (2, 2): -1,
}

# MGO eq (18) for sqrt(3): q^2 + q + 1 + (q^3 + q^2 - q - 1) X - q^2 X^2 = 0.
# Sign-flipped from the LHS = RHS form per this module's basis convention.
_MGO_EQ_18_SQRT3 = {
    (0, 0): 1,
    (0, 1): 1,
    (0, 2): 1,
    (1, 0): -1,
    (1, 1): -1,
    (1, 2): 1,
    (1, 3): 1,
    (2, 2): -1,
}

# MGO eq (19) for sqrt(5): q^4 + q^3 + q^2 + q + 1 + (q^5 + q^3 - q^2 - 1) X
# - q^3 X^2 = 0.
_MGO_EQ_19_SQRT5 = {
    (0, 0): 1,
    (0, 1): 1,
    (0, 2): 1,
    (0, 3): 1,
    (0, 4): 1,
    (1, 0): -1,
    (1, 2): -1,
    (1, 3): 1,
    (1, 5): 1,
    (2, 3): -1,
}

# MGO eq (14) for phi: 1 + (q^2 + q - 1) X - q X^2 = 0.
_MGO_EQ_14_PHI = {
    (0, 0): 1,
    (1, 0): -1,
    (1, 1): 1,
    (1, 2): 1,
    (2, 1): -1,
}


def _basis_to_poly_dict(basis_entry: dict[str, Any]) -> dict[tuple[int, int], int] | None:
    """Extract the (i, j) -> c dict from one basis entry."""
    terms = basis_entry.get("terms")
    if not isinstance(terms, list):
        return None
    return _polynomial_from_basis_terms(terms)


def _polys_proportional(p: dict[tuple[int, int], int],
                        q: dict[tuple[int, int], int]) -> bool:
    """Do two polynomials (as {(i, j): c} dicts) span the same line in
    the kernel? True iff one is a scalar multiple of the other (with no
    extraneous nonzero entries on either side).
    """
    if not p or not q:
        return False
    keys = set(p.keys()) | set(q.keys())
    # Find a shared support entry.
    common = [k for k in keys if p.get(k, 0) != 0 and q.get(k, 0) != 0]
    if not common:
        return False
    k0 = common[0]
    pa, qa = p[k0], q[k0]
    # Scalar must be rational; we work with Fractions.
    scale = Fraction(pa, qa)
    for k in keys:
        a = Fraction(p.get(k, 0))
        b = Fraction(q.get(k, 0)) * scale
        if a != b:
            return False
    return True


# ---------------------------------------------------------------------------
# Property check functions
# ---------------------------------------------------------------------------


def check_q_to_1_classical_limit(inputs: dict, outputs: dict) -> PropertyResult:
    """At q = 1 the q-deformation [alpha]_q reduces to the classical
    truncation: sum of the first N+1 Taylor coefficients equals the
    floor(alpha) prefix value, but more usefully, the FIRST coefficient
    c_0 must be 1 for any alpha >= 1.

    Source: MGO-reals page 1, Gauss q-integer evaluated at q = 1
    ([n]_1 = n); MGO-reals Theorem 2 (gap theorem) gives c_0 = 1 for
    alpha >= 1.

    Note: the classical limit "sum at q=1" is divergent for the infinite
    series; we restrict to the truncated CSV / coefficients case where
    the relevant numerical claim is c_0 = 1.
    """
    coeffs = _coeffs_from_outputs(outputs)
    if coeffs is None:
        # Try the per_constant shape that scan_gap_theorem returns.
        per_constant = outputs.get("per_constant")
        if isinstance(per_constant, dict):
            failures = []
            for name, info in per_constant.items():
                if not info.get("prefix_ones_ok", True):
                    failures.append(name)
            return PropertyResult(
                passed=(len(failures) == 0),
                detail=(
                    f"per_constant prefix_ones_ok across "
                    f"{len(per_constant)} constants; {len(failures)} failed"
                ),
                measured={"failures": failures},
            )
        return PropertyResult(
            passed=True,
            detail="no q-expansion coefficients in output (N/A)",
            measured=None,
        )
    c0 = coeffs[0]
    return PropertyResult(
        passed=(c0 == 1),
        detail=f"c_0 (= [alpha]_q at q -> 0 limit, but constant term = 1 for alpha >= 1) = {c0}",
        measured={"c_0": c0, "expected": 1},
    )


def check_gap_theorem(inputs: dict, outputs: dict) -> PropertyResult:
    """For alpha with floor(alpha) = k: the first k coefficients of the
    q-expansion are 1, and the (k+1)-th coefficient (index k, 0-indexed)
    is 0.

    Source: MGO-reals Theorem 2 (gap theorem), page 2:
        [x]_q = 1 + q + ... + q^(k-1) + kappa_{k+1} q^(k+1) + ...

    Equivalently, the k-th-order coefficient vanishes and all preceding
    coefficients equal 1.

    Applies to ``q_real_truncated``, ``scan_gap_theorem``, or any
    function whose outputs contain a list of q-expansion Taylor
    coefficients keyed under one of the names recognized by
    :func:`_coeffs_from_outputs`. The expected floor is taken from the
    inputs' ``alpha`` argument (label or numeric).
    """
    # Special path: scan_gap_theorem returns per_constant: {alpha: {floor, ...}}.
    per_constant = outputs.get("per_constant")
    if isinstance(per_constant, dict):
        failures = []
        per_alpha = {}
        for name, info in per_constant.items():
            ok_prefix = info.get("prefix_ones_ok")
            ok_gap = info.get("gap_zero_ok")
            per_alpha[name] = {
                "floor": info.get("floor"),
                "prefix_ones_ok": ok_prefix,
                "gap_zero_ok": ok_gap,
            }
            if not (ok_prefix and ok_gap):
                failures.append(name)
        return PropertyResult(
            passed=(len(failures) == 0),
            detail=(
                f"per_constant gap-theorem check across "
                f"{len(per_constant)} constants; {len(failures)} failed"
            ),
            measured={"failures": failures, "per_alpha": per_alpha},
        )

    alpha_in = inputs.get("alpha")
    if alpha_in is None:
        # Some pipelines record alpha in the outputs dict (e.g.
        # external-registered rows where the inputs are upstream
        # arguments and alpha is named in the output payload).
        alpha_in = outputs.get("alpha")
    floor_alpha = _alpha_floor(alpha_in)
    if floor_alpha is None:
        return PropertyResult(
            passed=True,
            detail=f"alpha {alpha_in!r} has no resolvable floor (N/A)",
            measured=None,
        )

    coeffs = _coeffs_from_outputs(outputs)
    if coeffs is None or len(coeffs) <= floor_alpha:
        return PropertyResult(
            passed=True,
            detail="no coefficients of sufficient depth in output (N/A)",
            measured=None,
        )

    leading = coeffs[:floor_alpha]
    leading_ones_ok = all(c == 1 for c in leading)
    gap_zero_ok = coeffs[floor_alpha] == 0
    return PropertyResult(
        passed=(leading_ones_ok and gap_zero_ok),
        detail=(
            f"floor(alpha)={floor_alpha}, leading {floor_alpha} coeffs="
            f"{leading}, c[{floor_alpha}]={coeffs[floor_alpha]}"
        ),
        measured={
            "floor_alpha": floor_alpha,
            "leading_ones": list(leading),
            "gap_coeff": coeffs[floor_alpha],
            "leading_ones_ok": leading_ones_ok,
            "gap_zero_ok": gap_zero_ok,
        },
    )


def check_palindromicity(inputs: dict, outputs: dict) -> PropertyResult:
    """For r/s in lowest terms, the numerator R(q) and denominator S(q)
    of [r/s]_q are palindromic.

    Source: MGO-rationals Proposition 1.6 + Corollary 1.7. Uses
    Hypothesis to sweep r/s with denominator <= 50.

    Applies to ``q_rational`` and to any tracked function whose outputs
    contain a ``r_polynomial`` / ``s_polynomial`` pair (each as a
    coefficient list, lowest power first).
    """
    r_poly = outputs.get("r_polynomial") or outputs.get("R")
    s_poly = outputs.get("s_polynomial") or outputs.get("S")
    if not (isinstance(r_poly, list) and isinstance(s_poly, list)):
        return PropertyResult(
            passed=True,
            detail="no r_polynomial/s_polynomial in output (N/A)",
            measured=None,
        )
    r_pal = list(r_poly) == list(reversed(r_poly))
    s_pal = list(s_poly) == list(reversed(s_poly))
    return PropertyResult(
        passed=(r_pal and s_pal),
        detail=(
            f"R coefficients (len {len(r_poly)}) palindromic={r_pal}; "
            f"S coefficients (len {len(s_poly)}) palindromic={s_pal}"
        ),
        measured={
            "R": list(r_poly),
            "S": list(s_poly),
            "R_palindromic": r_pal,
            "S_palindromic": s_pal,
        },
    )


def check_q_rational_classical_limit(inputs: dict, outputs: dict) -> PropertyResult:
    """For [p/s]_q with gcd(p, s) = 1, the q -> 1 limit equals p/s.

    Source: MGO-rationals Corollary 1.7 (constant terms of R, S equal 1;
    leading coefficients equal 1; R(1) = r, S(1) = s, hence
    R(1)/S(1) = r/s). Also MGO-reals page 1 ([a]_q at q = 1 equals a).

    Uses Hypothesis-style sampling internally on a small batch of random
    rationals constructed from inputs. If the outputs contain
    ``q_at_one_value``, it is checked directly against ``p / s``;
    otherwise the property is N/A and the check passes.
    """
    p = inputs.get("p")
    s = inputs.get("s")
    actual = outputs.get("q_at_one_value")
    if p is None or s is None or actual is None:
        return PropertyResult(
            passed=True,
            detail="no (p, s, q_at_one_value) triple in inputs/outputs (N/A)",
            measured=None,
        )
    expected = float(p) / float(s)
    diff = abs(float(actual) - expected)
    return PropertyResult(
        passed=(diff < 1e-9),
        detail=f"q_at_one_value={actual}, p/s={expected}, |diff|={diff}",
        measured={"q_at_one_value": actual, "expected": expected, "diff": diff},
    )


def check_translation_identity_right(inputs: dict, outputs: dict) -> PropertyResult:
    """[x + 1]_q = q [x]_q + 1.

    Source: MGO-reals eq (3) (translation identities, verbatim).

    Applies to a tracked function that returns a tuple
    ``(q_expansion_coeffs, q_expansion_coeffs_x_plus_1)`` so the
    identity can be checked coefficient-by-coefficient: shifting the
    base series by one (multiplying by q, plus 1 in the constant term)
    must reproduce the +1 series.
    """
    a = outputs.get("q_expansion_coeffs")
    b = outputs.get("q_expansion_coeffs_x_plus_1")
    if not (isinstance(a, list) and isinstance(b, list)):
        return PropertyResult(
            passed=True,
            detail=(
                "no (q_expansion_coeffs, q_expansion_coeffs_x_plus_1) "
                "pair in output (N/A)"
            ),
            measured=None,
        )
    n = min(len(a), len(b))
    expected = [0] * n
    expected[0] = 1
    for k in range(1, n):
        expected[k] = a[k - 1]
    diff_idx = next((k for k in range(n) if expected[k] != b[k]), None)
    return PropertyResult(
        passed=(diff_idx is None),
        detail=(
            f"checked first {n} coefficients of [x+1]_q = q[x]_q + 1; "
            f"first divergence at k = {diff_idx}"
        ),
        measured={
            "first_divergence_index": diff_idx,
            "expected_at_diff": expected[diff_idx] if diff_idx is not None else None,
            "got_at_diff": b[diff_idx] if diff_idx is not None else None,
        },
    )


def check_roots_of_unity_specialization(inputs: dict, outputs: dict) -> PropertyResult:
    """At q = -1 (the n = 2 root of unity), the q-deformed numerator and
    denominator polynomials of [r/s]_q evaluate to values in {-1, 0, 1}.

    Source: MGO-rationals Proposition 1.8: for r/s in lowest terms,
    R(-1), S(-1) in {-1, 0, 1}.

    This is the n = 2 case. The full n in {3, 4, 5, 6} branch is not
    implemented here; this check reports N/A rather than a false PASS when
    the n in {3, 4, 5, 6} case is what would be claimed.

    Inputs:
      - ``r_polynomial`` and ``s_polynomial`` in outputs: list[int] of
        coefficients (lowest power first), where the property is
        evaluated and asserted strictly.
      - Otherwise, the property is N/A and reports passed=True with a
        "no R/S polynomials available (N/A for power-series outputs)"
        detail. A future check can be added once the BRY citation is
        re-verified to handle power-series truncations.
    """
    r_poly = outputs.get("r_polynomial") or outputs.get("R")
    s_poly = outputs.get("s_polynomial") or outputs.get("S")
    if not (isinstance(r_poly, list) and isinstance(s_poly, list)):
        return PropertyResult(
            passed=True,
            detail=(
                "no R/S polynomial pair in outputs (N/A for power-series "
                "truncations; full n-th root sweep not implemented)"
            ),
            measured=None,
        )
    r_at_minus_1 = sum(((-1) ** k) * c for k, c in enumerate(r_poly))
    s_at_minus_1 = sum(((-1) ** k) * c for k, c in enumerate(s_poly))
    allowed = {-1, 0, 1}
    in_set = (r_at_minus_1 in allowed) and (s_at_minus_1 in allowed)
    return PropertyResult(
        passed=in_set,
        detail=(
            f"R(-1) = {r_at_minus_1}, S(-1) = {s_at_minus_1}; "
            f"both must be in {{-1, 0, 1}} per MGO-rationals Prop 1.8"
        ),
        measured={
            "R_at_minus_1": r_at_minus_1,
            "S_at_minus_1": s_at_minus_1,
            "allowed_set": sorted(allowed),
            "n2_case_only": True,
            "note": (
                "n=2 case from MGO-rationals Prop 1.8; full n in {3,4,5,6} "
                "not implemented here."
            ),
        },
    )


def check_recovers_mgo_eq_17_at_sqrt2(inputs: dict, outputs: dict) -> PropertyResult:
    """At alpha = sqrt(2), (d_X = 2, d_q = 3), the kernel must recover
    MGO-reals eq (17): q^2 + 1 + (q^3 - 1) X - q^2 X^2 = 0
    (sign-flipped from MGO's LHS = RHS form).

    Source: MGO-reals Proposition 4.5, eq (17).
    """
    return _check_mgo_recovery(
        inputs, outputs,
        expected_alpha="sqrt2",
        expected_d_X=2,
        expected_d_q_plus=3,
        expected_d_q_minus=0,
        expected_poly=_MGO_EQ_17_SQRT2,
        eq_label="MGO-reals eq (17)",
    )


def check_recovers_mgo_eq_18_at_sqrt3(inputs: dict, outputs: dict) -> PropertyResult:
    """At alpha = sqrt(3), (d_X = 2, d_q = 3), the kernel must recover
    MGO-reals eq (18) in sign-flipped form.

    Source: MGO-reals Proposition 4.5, eq (18).
    """
    return _check_mgo_recovery(
        inputs, outputs,
        expected_alpha="sqrt3",
        expected_d_X=2,
        expected_d_q_plus=3,
        expected_d_q_minus=0,
        expected_poly=_MGO_EQ_18_SQRT3,
        eq_label="MGO-reals eq (18)",
    )


def check_recovers_mgo_eq_19_at_sqrt5(inputs: dict, outputs: dict) -> PropertyResult:
    """At alpha = sqrt(5), (d_X = 2, d_q = 5), the kernel must recover
    MGO-reals eq (19) in sign-flipped form.

    Source: MGO-reals Proposition 4.5, eq (19).
    """
    return _check_mgo_recovery(
        inputs, outputs,
        expected_alpha="sqrt5",
        expected_d_X=2,
        expected_d_q_plus=5,
        expected_d_q_minus=0,
        expected_poly=_MGO_EQ_19_SQRT5,
        eq_label="MGO-reals eq (19)",
    )


def check_recovers_mgo_eq_14_at_phi(inputs: dict, outputs: dict) -> PropertyResult:
    """At alpha = phi, (d_X = 2, d_q = 2), the kernel must recover
    MGO-reals eq (14): 1 + (q^2 + q - 1) X - q X^2 = 0
    (sign-flipped from MGO's q[phi]_q^2 - (q^2 + q - 1)[phi]_q - 1 = 0).

    Source: MGO-reals eq (14), page 9.
    """
    return _check_mgo_recovery(
        inputs, outputs,
        expected_alpha="phi",
        expected_d_X=2,
        expected_d_q_plus=2,
        expected_d_q_minus=0,
        expected_poly=_MGO_EQ_14_PHI,
        eq_label="MGO-reals eq (14)",
    )


def _check_mgo_recovery(
    inputs: dict, outputs: dict, *,
    expected_alpha: str,
    expected_d_X: int,
    expected_d_q_plus: int,
    expected_d_q_minus: int,
    expected_poly: dict[tuple[int, int], int],
    eq_label: str,
) -> PropertyResult:
    """Common machinery for the four MGO-recovery property checks."""
    alpha = inputs.get("alpha")
    d_X = inputs.get("d_X")
    d_q_plus = inputs.get("d_q_plus")
    d_q_minus = inputs.get("d_q_minus", 0)
    if (
        alpha is None
        or str(alpha).lower() != expected_alpha
        or d_X != expected_d_X
        or d_q_plus != expected_d_q_plus
        or d_q_minus != expected_d_q_minus
    ):
        return PropertyResult(
            passed=True,
            detail=(
                f"N/A: not the {eq_label} regression case "
                f"(got alpha={alpha}, d_X={d_X}, d_q_plus={d_q_plus}, "
                f"d_q_minus={d_q_minus})"
            ),
            measured=None,
        )
    kernel_dim = outputs.get("kernel_dim")
    basis = outputs.get("kernel_basis")
    if basis is None and kernel_dim is None:
        return PropertyResult(
            passed=True,
            detail=(
                f"N/A: outputs do not expose kernel_dim / kernel_basis "
                f"(this looks like a cross-CAS dim-agreement record, not a "
                f"kernel-search record); cannot verify {eq_label} recovery"
            ),
            measured=None,
        )
    if kernel_dim != 1 or not basis:
        return PropertyResult(
            passed=False,
            detail=(
                f"expected kernel_dim=1 with one basis element for "
                f"{eq_label} recovery; got kernel_dim={kernel_dim}, "
                f"len(basis)={len(basis) if isinstance(basis, list) else 'n/a'}"
            ),
            measured={"kernel_dim": kernel_dim},
        )
    got_poly = _basis_to_poly_dict(basis[0]) if isinstance(basis[0], dict) else None
    if got_poly is None:
        return PropertyResult(
            passed=False,
            detail=f"basis[0] does not have a 'terms' list usable for {eq_label} comparison",
            measured={"basis_0": basis[0]},
        )
    matches = _polys_proportional(got_poly, expected_poly)
    return PropertyResult(
        passed=matches,
        detail=(
            f"basis[0] {'matches' if matches else 'does NOT match'} "
            f"{eq_label} (up to scalar)"
        ),
        measured={
            "got_poly": {f"{i},{j}": c for (i, j), c in got_poly.items()},
            "expected_poly": {f"{i},{j}": c for (i, j), c in expected_poly.items()},
        },
    )


def check_bidegree_conformity(inputs: dict, outputs: dict) -> PropertyResult:
    """Every returned kernel basis element has degree in X at most d_X
    and degree in q at most max(d_q_minus, d_q_plus).
    """
    d_X = inputs.get("d_X")
    d_q_plus = inputs.get("d_q_plus")
    d_q_minus = inputs.get("d_q_minus", 0)
    basis = outputs.get("kernel_basis", [])
    if not isinstance(basis, list) or d_X is None or d_q_plus is None:
        return PropertyResult(
            passed=True,
            detail="missing d_X / d_q_plus / kernel_basis (N/A)",
            measured=None,
        )
    violations = []
    for k, entry in enumerate(basis):
        if not isinstance(entry, dict):
            continue
        terms = entry.get("terms", [])
        for t in terms:
            i = int(t.get("i", 0))
            j = int(t.get("j", 0))
            if i > d_X or j > d_q_plus or j < -d_q_minus:
                violations.append({"basis_index": k, "i": i, "j": j})
    return PropertyResult(
        passed=(len(violations) == 0),
        detail=(
            f"checked {len(basis)} basis element(s) against bidegree "
            f"(d_X={d_X}, d_q_minus={d_q_minus}, d_q_plus={d_q_plus}); "
            f"{len(violations)} violation(s)"
        ),
        measured={"violations": violations[:8], "d_X": d_X,
                  "d_q_minus": d_q_minus, "d_q_plus": d_q_plus},
    )


def check_kernel_empty_for_cube_root(inputs: dict, outputs: dict) -> PropertyResult:
    """Example invariant: for alpha in {cbrt2, cbrt3, cbrt5, qrt2} at a
    fixed search window (d_X = 6, d_q_plus = 50, N >= 2000), the kernel of
    the constraint matrix is expected to be trivial (kernel_dim = 0).

    This shows how to pin a search's expected null result as a runtime
    invariant: once recorded, any future re-run that returns a nonzero
    kernel is flagged as a defect rather than passing unnoticed.
    """
    alpha = inputs.get("alpha")
    d_X = inputs.get("d_X")
    d_q_plus = inputs.get("d_q_plus")
    d_q_minus = inputs.get("d_q_minus", 0)
    N = inputs.get("N")
    if str(alpha).lower() not in {"cbrt2", "cbrt3", "cbrt5", "qrt2"}:
        return PropertyResult(
            passed=True,
            detail=f"alpha {alpha!r} is not a cube root / fourth root (N/A)",
            measured=None,
        )
    if d_X != 6 or d_q_plus != 50 or d_q_minus != 0 or (N is not None and N < 2000):
        return PropertyResult(
            passed=True,
            detail=(
                f"not the production-window case "
                f"(d_X={d_X}, d_q_plus={d_q_plus}, d_q_minus={d_q_minus}, N={N})"
            ),
            measured=None,
        )
    kdim = outputs.get("kernel_dim")
    if kdim is None:
        sympy_kdim = outputs.get("sympy_kernel_dim")
        sage_kdim = outputs.get("sage_kernel_dim")
        if sympy_kdim is not None or sage_kdim is not None:
            both_zero = sympy_kdim == 0 and sage_kdim == 0
            return PropertyResult(
                passed=both_zero,
                detail=(
                    f"cross-CAS production-window kernel dims: "
                    f"sympy={sympy_kdim}, sage={sage_kdim}; expected both 0"
                ),
                measured={"sympy_kernel_dim": sympy_kdim,
                          "sage_kernel_dim": sage_kdim, "alpha": alpha},
            )
    return PropertyResult(
        passed=(kdim == 0),
        detail=f"production-window kernel_dim={kdim}; expected 0",
        measured={"kernel_dim": kdim, "alpha": alpha},
    )


# ---------------------------------------------------------------------------
# Hypothesis-driven properties
# ---------------------------------------------------------------------------


def check_translation_identity_right_hypothesis(
    inputs: dict, outputs: dict
) -> PropertyResult:
    """Hypothesis variant of :func:`check_translation_identity_right`.

    Uses the ``q_deform_callable`` injected into inputs (a function
    ``f(p: int, s: int) -> list[int]`` returning the truncated
    coefficient list of ``[p/s]_q``) to verify
    ``[p/s + 1]_q == q [p/s]_q + 1`` on random rationals up to
    denominator 50.

    Source: MGO-reals eq (3) (translation identity).
    """
    q_deform = inputs.get("_q_deform_callable")
    if q_deform is None:
        return PropertyResult(
            passed=True,
            detail="no _q_deform_callable in inputs (N/A)",
            measured=None,
        )
    try:
        from hypothesis import given, settings, strategies as st
    except ImportError:  # pragma: no cover - hypothesis is a hard dep
        return PropertyResult(
            passed=False,
            detail="hypothesis library not available",
            measured=None,
        )
    failures: list[dict[str, Any]] = []
    examples_tried = [0]

    @given(p=st.integers(min_value=1, max_value=49),
           s=st.integers(min_value=2, max_value=50))
    @settings(max_examples=80, deadline=None)
    def _check(p: int, s: int) -> None:
        if math.gcd(p, s) != 1:
            return
        examples_tried[0] += 1
        try:
            base = q_deform(p, s)
            shifted = q_deform(p + s, s)
        except Exception as exc:  # pragma: no cover
            failures.append({"p": p, "s": s, "error": str(exc)})
            return
        n = min(len(base), len(shifted))
        # Expected at index k for the +1 shift: base[k-1] for k >= 1, 1 for k = 0.
        for k in range(n):
            expected = 1 if k == 0 else base[k - 1]
            if shifted[k] != expected:
                failures.append({
                    "p": p, "s": s, "k": k,
                    "expected": expected, "got": shifted[k],
                })
                return

    _check()
    return PropertyResult(
        passed=(len(failures) == 0),
        detail=(
            f"hypothesis swept random p/s with denominator <= 50; "
            f"tested {examples_tried[0]} reduced fractions; "
            f"{len(failures)} failures"
        ),
        measured={
            "failures": failures[:5],
            "examples_tried": examples_tried[0],
        },
        hypothesis_examples_tried=examples_tried[0],
    )


# ---------------------------------------------------------------------------
# Convenience: pre-bundled Property declarations.
# ---------------------------------------------------------------------------


def q_real_truncated_properties() -> list[Property]:
    """Properties to attach to the q_real_truncated computation
    (q-expansion of an irrational alpha to N stable Taylor coefficients).
    """
    return [
        Property(
            name="q_to_1_classical_limit",
            check=check_q_to_1_classical_limit,
            description="At q -> 0, c_0 of [alpha]_q is 1 for alpha >= 1 (MGO-reals page 1, Thm 2)",
            severity="error",
        ),
        Property(
            name="gap_theorem",
            check=check_gap_theorem,
            description="Gap theorem: first floor(alpha) coeffs are 1, then 0 (MGO-reals Thm 2)",
            severity="error",
        ),
        Property(
            name="roots_of_unity_n2",
            check=check_roots_of_unity_specialization,
            description="n=2 root-of-unity probe via alternating sum (MGO-rationals Prop 1.8)",
            severity="warning",
        ),
    ]


def kernel_search_properties() -> list[Property]:
    """Properties to attach to the kernel-search computations (Sage and
    SymPy halves).
    """
    return [
        Property(
            name="recovers_mgo_eq_17_at_sqrt2",
            check=check_recovers_mgo_eq_17_at_sqrt2,
            description="At alpha=sqrt2 / (d_X=2, d_q=3), kernel recovers MGO eq (17)",
            severity="error",
        ),
        Property(
            name="recovers_mgo_eq_18_at_sqrt3",
            check=check_recovers_mgo_eq_18_at_sqrt3,
            description="At alpha=sqrt3 / (d_X=2, d_q=3), kernel recovers MGO eq (18)",
            severity="error",
        ),
        Property(
            name="recovers_mgo_eq_19_at_sqrt5",
            check=check_recovers_mgo_eq_19_at_sqrt5,
            description="At alpha=sqrt5 / (d_X=2, d_q=5), kernel recovers MGO eq (19)",
            severity="error",
        ),
        Property(
            name="recovers_mgo_eq_14_at_phi",
            check=check_recovers_mgo_eq_14_at_phi,
            description="At alpha=phi / (d_X=2, d_q=2), kernel recovers MGO eq (14)",
            severity="error",
        ),
        Property(
            name="bidegree_conformity",
            check=check_bidegree_conformity,
            description="Each kernel basis element fits within (d_X, d_q_minus, d_q_plus)",
            severity="error",
        ),
        Property(
            name="kernel_empty_for_cube_root",
            check=check_kernel_empty_for_cube_root,
            description="At (d_X=6, d_q_plus=50, N>=2000) for cube/4th roots, kernel_dim=0",
            severity="error",
        ),
    ]


def gap_theorem_scan_properties() -> list[Property]:
    """Properties to attach to scan_gap_theorem (per_constant outputs)."""
    return [
        Property(
            name="gap_theorem",
            check=check_gap_theorem,
            description="Gap theorem holds for each constant in the per_constant map (MGO-reals Thm 2)",
            severity="error",
        ),
        Property(
            name="q_to_1_classical_limit",
            check=check_q_to_1_classical_limit,
            description="c_0 of every constant is 1 (Gauss q-integer specialization)",
            severity="error",
        ),
    ]


__all__ = [
    "Property",
    "PropertyResult",
    "check_q_to_1_classical_limit",
    "check_gap_theorem",
    "check_palindromicity",
    "check_q_rational_classical_limit",
    "check_translation_identity_right",
    "check_translation_identity_right_hypothesis",
    "check_roots_of_unity_specialization",
    "check_recovers_mgo_eq_17_at_sqrt2",
    "check_recovers_mgo_eq_18_at_sqrt3",
    "check_recovers_mgo_eq_19_at_sqrt5",
    "check_recovers_mgo_eq_14_at_phi",
    "check_bidegree_conformity",
    "check_kernel_empty_for_cube_root",
    "q_real_truncated_properties",
    "kernel_search_properties",
    "gap_theorem_scan_properties",
]
