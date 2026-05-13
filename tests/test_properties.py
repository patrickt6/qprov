"""Property-based tracking via Hypothesis.

Covers the contract:

- An error-severity property that fails BLOCKS the qprov write.
- A warning-severity property that fails LOGS but writes.
- An all-pass property run records ``property_results`` on the row
  and in the payload.
- The ``properties_qnumbers`` checks behave correctly on real
  outputs (gap theorem, MGO equation 17 recovery, bidegree
  conformity).
- Hypothesis surfaces a small counterexample when a metamorphic
  invariant is broken.
"""
from __future__ import annotations

import json
import warnings as _warnings

import pytest

import qprov
from qprov import (
    Property,
    PropertyResult,
    QprovPropertyError,
    QprovPropertyWarning,
    tracked,
)
from qprov.properties_qnumbers import (
    check_bidegree_conformity,
    check_gap_theorem,
    check_kernel_empty_for_cube_root,
    check_q_to_1_classical_limit,
    check_recovers_mgo_eq_17_at_sqrt2,
    check_recovers_mgo_eq_14_at_phi,
    check_roots_of_unity_specialization,
)


# ---------------------------------------------------------------------------
# Decorator contract: passes / fails / warnings
# ---------------------------------------------------------------------------


def _always_pass(_inputs, _outputs):
    return PropertyResult(passed=True, detail="trivially true")


def _always_fail(_inputs, _outputs):
    return PropertyResult(passed=False, detail="trivially false",
                          measured={"reason": "constructed for the test"})


def test_property_passes_writes_record():
    """All-pass error-severity property: row is written, results stored."""
    @tracked(properties=[
        Property(name="trivial_pass", check=_always_pass,
                 description="trivially true", severity="error"),
    ])
    def add(x, y):
        return {"sum": x + y}

    add(2, 3)
    comps = qprov.find()
    assert len(comps) == 1
    c = comps[0]
    assert c.property_results is not None
    assert "trivial_pass" in c.property_results
    assert c.property_results["trivial_pass"]["passed"] is True


def test_property_error_blocks_write():
    """Failed error-severity property raises and BLOCKS the qprov write."""
    @tracked(properties=[
        Property(name="trivial_fail", check=_always_fail,
                 description="trivially false", severity="error"),
    ])
    def add(x, y):
        return {"sum": x + y}

    with pytest.raises(QprovPropertyError) as excinfo:
        add(2, 3)
    assert "trivial_fail" in str(excinfo.value)
    # No row landed in the store.
    assert qprov.find() == []


def test_property_warning_logs_but_writes():
    """Failed warning-severity property: row is still written, warning emitted."""
    @tracked(properties=[
        Property(name="trivial_warn", check=_always_fail,
                 description="trivially false (warning)", severity="warning"),
    ])
    def add(x, y):
        return {"sum": x + y}

    with _warnings.catch_warnings(record=True) as wlist:
        _warnings.simplefilter("always")
        add(2, 3)

    qpwarns = [w for w in wlist if issubclass(w.category, QprovPropertyWarning)]
    assert len(qpwarns) >= 1
    assert "trivial_warn" in str(qpwarns[0].message)

    comps = qprov.find()
    assert len(comps) == 1
    c = comps[0]
    assert c.property_results is not None
    assert c.property_results["trivial_warn"]["passed"] is False
    assert c.property_results["trivial_warn"]["severity"] == "warning"


def test_property_results_persist_in_payload():
    """The full property_results dict lands in the gzipped payload."""
    @tracked(properties=[
        Property(name="trivial_pass", check=_always_pass,
                 description="trivially true", severity="error"),
    ])
    def stub():
        return {"value": 42}

    stub()
    c = qprov.find()[0]
    payload = qprov.get_store().read_payload(c.id)
    assert "property_results" in payload
    assert payload["property_results"]["trivial_pass"]["passed"] is True


def test_property_check_exception_treated_as_failure():
    """A property check that raises is treated as a failure (not a crash
    that bypasses the gate)."""
    def _explodes(_inputs, _outputs):
        raise ValueError("boom")

    @tracked(properties=[
        Property(name="explodes", check=_explodes,
                 description="raises", severity="error"),
    ])
    def stub():
        return {"value": 1}

    with pytest.raises(QprovPropertyError) as excinfo:
        stub()
    assert "explodes" in str(excinfo.value)
    assert "ValueError" in str(excinfo.value)
    assert qprov.find() == []


def test_no_properties_legacy_path_still_works():
    """No properties argument: behaves exactly as v0.3."""
    @tracked
    def add(x, y):
        return x + y

    assert add(1, 2) == 3
    c = qprov.find()[0]
    assert c.property_results is None


# ---------------------------------------------------------------------------
# Hypothesis surfaces small counterexamples
# ---------------------------------------------------------------------------


def test_hypothesis_finds_counterexample():
    """A Hypothesis-driven property must find a small counterexample
    when the invariant is broken on small inputs."""
    from hypothesis import given, settings, strategies as st

    failures: list[int] = []
    examples: list[int] = []

    def _check_n_squared_geq_n(_inputs, _outputs):
        @given(n=st.integers(min_value=-5, max_value=5))
        @settings(max_examples=50, deadline=None)
        def _inner(n):
            examples.append(n)
            # Deliberately wrong: this fails for n < 0.
            if not (n * n >= 2 * n):
                failures.append(n)
        _inner()
        return PropertyResult(
            passed=(len(failures) == 0),
            detail=f"checked {len(examples)} integers; {len(failures)} broke n*n >= 2n",
            measured={"failures": sorted(set(failures))[:5]},
            hypothesis_examples_tried=len(examples),
        )

    @tracked(properties=[
        Property(name="n_squared_geq_2n",
                 check=_check_n_squared_geq_n,
                 description="n^2 >= 2n on [-5, 5]",
                 severity="warning"),
    ])
    def stub():
        return {"value": 0}

    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", category=QprovPropertyWarning)
        stub()

    c = qprov.find()[0]
    pr = c.property_results["n_squared_geq_2n"]
    assert pr["passed"] is False
    # The counterexample is one of {-5, -4, -3, -2, -1, 1}; Hypothesis is
    # encouraged to find the smallest |n|.
    assert pr["measured"]["failures"]
    assert pr["hypothesis_examples_tried"] >= 1


# ---------------------------------------------------------------------------
# Project-specific property checks (no Sage required)
# ---------------------------------------------------------------------------


def test_gap_theorem_passes_on_phi_csv_shape():
    """phi has floor=1, so the first 1 coefficient is 1 and c_1 is 0."""
    inputs = {"alpha": "phi"}
    outputs = {"q_expansion_coeffs": [1, 0, 1, -1, 0, 2, -3]}
    pr = check_gap_theorem(inputs, outputs)
    assert pr.passed
    assert pr.measured["leading_ones_ok"]
    assert pr.measured["gap_zero_ok"]


def test_gap_theorem_fails_on_corrupted_phi():
    """Tampered c_1 = 5 instead of 0 trips the gap-theorem check."""
    inputs = {"alpha": "phi"}
    outputs = {"q_expansion_coeffs": [1, 5, 1, -1, 0]}
    pr = check_gap_theorem(inputs, outputs)
    assert not pr.passed
    assert pr.measured["gap_zero_ok"] is False


def test_gap_theorem_passes_on_pi_csv_shape():
    """pi has floor=3: kappa_0=kappa_1=kappa_2=1, kappa_3=0."""
    inputs = {"alpha": "pi"}
    outputs = {"q_expansion_coeffs": [1, 1, 1, 0, 1, -1, -1, 0]}
    pr = check_gap_theorem(inputs, outputs)
    assert pr.passed


def test_q_to_1_classical_limit_passes_on_c0_eq_1():
    inputs = {"alpha": "sqrt2"}
    outputs = {"q_expansion_coeffs": [1, 0, 0, 1, -1, 0]}
    pr = check_q_to_1_classical_limit(inputs, outputs)
    assert pr.passed


def test_q_to_1_classical_limit_fails_on_c0_eq_0():
    inputs = {"alpha": "sqrt2"}
    outputs = {"q_expansion_coeffs": [0, 0, 0, 1]}
    pr = check_q_to_1_classical_limit(inputs, outputs)
    assert not pr.passed


def test_recovers_mgo_eq_17_passes_on_canonical_basis():
    """Use the literal MGO eq (17) basis vector."""
    inputs = {"alpha": "sqrt2", "d_X": 2, "d_q_minus": 0, "d_q_plus": 3}
    outputs = {
        "kernel_dim": 1,
        "kernel_basis": [{
            "terms": [
                {"i": 0, "j": 0, "c": 1},
                {"i": 0, "j": 2, "c": 1},
                {"i": 1, "j": 0, "c": -1},
                {"i": 1, "j": 3, "c": 1},
                {"i": 2, "j": 2, "c": -1},
            ],
        }],
    }
    pr = check_recovers_mgo_eq_17_at_sqrt2(inputs, outputs)
    assert pr.passed, pr.detail


def test_recovers_mgo_eq_17_fails_on_perturbed_basis():
    """Flip one coefficient; the polynomial is no longer proportional."""
    inputs = {"alpha": "sqrt2", "d_X": 2, "d_q_minus": 0, "d_q_plus": 3}
    outputs = {
        "kernel_dim": 1,
        "kernel_basis": [{
            "terms": [
                {"i": 0, "j": 0, "c": 1},
                {"i": 0, "j": 2, "c": 1},
                {"i": 1, "j": 0, "c": -1},
                {"i": 1, "j": 3, "c": 1},
                {"i": 2, "j": 2, "c": 7},  # was -1
            ],
        }],
    }
    pr = check_recovers_mgo_eq_17_at_sqrt2(inputs, outputs)
    assert not pr.passed


def test_recovers_mgo_eq_17_skips_when_not_regression_case():
    """Different alpha / bidegree -> N/A pass."""
    inputs = {"alpha": "phi", "d_X": 2, "d_q_minus": 0, "d_q_plus": 2}
    outputs = {"kernel_dim": 0, "kernel_basis": []}
    pr = check_recovers_mgo_eq_17_at_sqrt2(inputs, outputs)
    assert pr.passed
    assert "N/A" in pr.detail


def test_recovers_mgo_eq_17_na_on_cross_cas_payload_shape():
    """Cross-CAS verification rows record only dim agreement (sympy_kernel_dim
    + sage_kernel_dim) and never expose kernel_basis. The MGO recovery check
    cannot validate the basis polynomial without the basis itself, so it
    must return N/A rather than failing the row.
    """
    inputs = {"alpha": "sqrt2", "d_X": 2, "d_q_minus": 0, "d_q_plus": 3}
    outputs = {"sympy_kernel_dim": 1, "sage_kernel_dim": 1, "sage_match": True}
    pr = check_recovers_mgo_eq_17_at_sqrt2(inputs, outputs)
    assert pr.passed
    assert "N/A" in pr.detail
    assert "cross-CAS" in pr.detail


def test_kernel_empty_for_cube_root_passes_on_cross_cas_zero():
    """Cross-CAS row at the production window (cbrt2, d_X=6, d_q_plus=50,
    N=2000) records sympy_kernel_dim=0 and sage_kernel_dim=0; the property
    should PASS by reading both fields, not the absent kernel_dim.
    """
    inputs = {"alpha": "cbrt2", "d_X": 6, "d_q_plus": 50, "d_q_minus": 0,
              "N": 2000}
    outputs = {"sympy_kernel_dim": 0, "sage_kernel_dim": 0, "sage_match": True}
    pr = check_kernel_empty_for_cube_root(inputs, outputs)
    assert pr.passed
    assert "cross-CAS" in pr.detail


def test_kernel_empty_for_cube_root_fails_on_cross_cas_nonzero():
    """If either CAS reports a nonzero kernel at the production window,
    the property must FAIL.
    """
    inputs = {"alpha": "cbrt2", "d_X": 6, "d_q_plus": 50, "d_q_minus": 0,
              "N": 2000}
    outputs = {"sympy_kernel_dim": 0, "sage_kernel_dim": 1, "sage_match": False}
    pr = check_kernel_empty_for_cube_root(inputs, outputs)
    assert not pr.passed


def test_bidegree_conformity_passes_on_in_box_basis():
    inputs = {"d_X": 2, "d_q_minus": 0, "d_q_plus": 3}
    outputs = {"kernel_basis": [{"terms": [
        {"i": 0, "j": 0, "c": 1},
        {"i": 1, "j": 3, "c": 1},
        {"i": 2, "j": 2, "c": -1},
    ]}]}
    pr = check_bidegree_conformity(inputs, outputs)
    assert pr.passed


def test_bidegree_conformity_fails_on_out_of_box_basis():
    inputs = {"d_X": 2, "d_q_minus": 0, "d_q_plus": 3}
    outputs = {"kernel_basis": [{"terms": [
        {"i": 3, "j": 0, "c": 1},  # i > d_X
        {"i": 0, "j": 5, "c": 1},  # j > d_q_plus
    ]}]}
    pr = check_bidegree_conformity(inputs, outputs)
    assert not pr.passed
    assert len(pr.measured["violations"]) == 2


def test_roots_of_unity_n2_na_on_power_series_only():
    """Without an R/S polynomial pair, the BRY-style sweep is N/A and
    the property correctly reports passed=True with an N/A note (the
    n>=3 sweep is FLAGGED in the spec)."""
    inputs = {"alpha": "phi"}
    outputs = {"q_expansion_coeffs": [1, 0, 1, -1, 0, 2, -3]}
    pr = check_roots_of_unity_specialization(inputs, outputs)
    assert pr.passed
    assert "N/A" in pr.detail


def test_roots_of_unity_n2_passes_on_canonical_19_over_7_polys():
    """[19/7]_q from MGO-rationals Definition 1.1: R, S polynomials
    satisfy R(-1), S(-1) in {-1, 0, 1} (Prop 1.8). Use a hand-built
    example consistent with the proposition.

    Pick R(q) = 1 + q + q^2 + q^3 (palindromic-ish), S(q) = 1 + q + q^2.
    R(-1) = 0; S(-1) = 1; both in {-1, 0, 1}.
    """
    inputs = {"p": 7, "s": 5}
    outputs = {
        "r_polynomial": [1, 1, 1, 1],
        "s_polynomial": [1, 1, 1],
    }
    pr = check_roots_of_unity_specialization(inputs, outputs)
    assert pr.passed
    assert pr.measured["R_at_minus_1"] == 0
    assert pr.measured["S_at_minus_1"] == 1


def test_roots_of_unity_n2_fails_when_r_at_minus_one_too_large():
    """R(-1) = 2 would violate Prop 1.8."""
    inputs = {"p": 7, "s": 5}
    outputs = {
        "r_polynomial": [1, 0, 1],  # R(-1) = 2
        "s_polynomial": [1, 1, 1],  # S(-1) = 1
    }
    pr = check_roots_of_unity_specialization(inputs, outputs)
    assert not pr.passed
    assert pr.measured["R_at_minus_1"] == 2


def test_recovers_mgo_eq_14_passes_on_canonical_phi_basis():
    """MGO eq (14) for phi: 1 + (q^2 + q - 1) X - q X^2 = 0."""
    inputs = {"alpha": "phi", "d_X": 2, "d_q_minus": 0, "d_q_plus": 2}
    outputs = {
        "kernel_dim": 1,
        "kernel_basis": [{
            "terms": [
                {"i": 0, "j": 0, "c": 1},
                {"i": 1, "j": 0, "c": -1},
                {"i": 1, "j": 1, "c": 1},
                {"i": 1, "j": 2, "c": 1},
                {"i": 2, "j": 1, "c": -1},
            ],
        }],
    }
    pr = check_recovers_mgo_eq_14_at_phi(inputs, outputs)
    assert pr.passed, pr.detail


# ---------------------------------------------------------------------------
# CLI / store: rerun properties against the stored payload.
# ---------------------------------------------------------------------------


def test_properties_check_cli_reruns_against_stored_payload(tmp_path, monkeypatch):
    """`qprov properties --check --comp-id <id>` re-runs declared
    properties using the stored payload's args/kwargs/result. The
    re-run respects whatever Property declarations are wired through
    a name resolution table (here: stub via the in-memory registry)."""
    from click.testing import CliRunner
    from qprov.cli import main

    # Run a tracked function with a passing property, get its id.
    @tracked(properties=[
        Property(name="trivial_pass", check=_always_pass,
                 description="trivially true", severity="error"),
    ])
    def stub():
        return {"value": 1}

    stub()
    c = qprov.find()[0]

    runner = CliRunner()
    result = runner.invoke(main, ["properties", "--check", "--comp-id", c.id])
    assert result.exit_code == 0, result.output
    assert "trivial_pass" in result.output
    assert "PASS" in result.output


def test_properties_list_cli_lists_all_property_keys(tmp_path):
    """`qprov properties --list` enumerates the property names recorded
    across the store."""
    from click.testing import CliRunner
    from qprov.cli import main

    @tracked(properties=[
        Property(name="prop_alpha", check=_always_pass,
                 description="A", severity="error"),
        Property(name="prop_beta", check=_always_pass,
                 description="B", severity="warning"),
    ])
    def stub():
        return {"value": 7}

    stub()

    runner = CliRunner()
    result = runner.invoke(main, ["properties", "--list"])
    assert result.exit_code == 0, result.output
    assert "prop_alpha" in result.output
    assert "prop_beta" in result.output
