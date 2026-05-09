"""Claim recording and LaTeX export."""
from __future__ import annotations

import pytest

import qprov
from qprov import tracked
from qprov.claims import claim, export_latex
from qprov.store import get_store


@pytest.fixture
def computation_id():
    @tracked(tags={"constant": "pi"})
    def f(N):
        return [k**2 for k in range(N)]
    f(10)
    return qprov.find()[0].id


def test_claim_with_link(computation_id):
    cid = claim(
        "The first nonzero coefficient of [pi]_q after q^45 is at q^46",
        computation_id=computation_id,
        value_numeric=46,
    )
    assert cid
    rec = get_store().get_claim(cid)
    assert rec.text.startswith("The first nonzero")
    assert rec.computation_id == computation_id
    assert rec.value_numeric == 46.0


def test_claim_unlinked():
    cid = claim("a qualitative observation")
    rec = get_store().get_claim(cid)
    assert rec.computation_id is None
    assert rec.value_numeric is None


def test_claim_rejects_empty_text():
    with pytest.raises(ValueError):
        claim("")


def test_export_latex_renders_fact_macros(computation_id):
    claim("R([sqrt2]_q) = 0.531213", computation_id=computation_id, value_numeric=0.531213)
    claim("c_45([pi]_q) = 0", computation_id=computation_id, value_numeric=0)
    out = export_latex()
    assert r"\fact{R([sqrt2]_q) = 0.531213}" in out
    assert r"\fact{c_45([pi]_q) = 0}" in out
    assert r"\provid{" + computation_id + "}" in out


def test_export_latex_writes_file(tmp_path, computation_id):
    claim("anything", computation_id=computation_id)
    target = tmp_path / "claims.tex"
    text = export_latex(output=str(target))
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == text


def test_export_latex_filters_by_computation(computation_id):
    @tracked
    def g(N):
        return N * 3
    g(5)
    other = qprov.find(function="g")[0].id

    claim("about pi", computation_id=computation_id)
    claim("about g", computation_id=other)

    out = export_latex(computation_id=computation_id)
    assert "about pi" in out
    assert "about g" not in out


def test_latexify_brackets_multidigit_exponents():
    from qprov.claims import latexify
    assert latexify("$q^10$") == "$q^{10}$"
    assert latexify("$X^15 + q^123$") == "$X^{15} + q^{123}$"


def test_latexify_leaves_single_digit_exponents():
    from qprov.claims import latexify
    assert latexify("$q^2$") == "$q^2$"
    assert latexify("$X^3 + q^9$") == "$X^3 + q^9$"


def test_latexify_strips_sage_multiplication():
    from qprov.claims import latexify
    assert latexify("$1 - X + X*q + X*q^2 - X^2*q$") == "$1 - X + X q + X q^2 - X^2 q$"


def test_latexify_combined_real_validation_polynomial():
    """The shape of a typical MGO validation claim after latexify."""
    from qprov.claims import latexify
    raw = "$P(X,q) = 1 + q^2 - X + X*q^3 - X^2*q^2$"
    expected = "$P(X,q) = 1 + q^2 - X + X q^3 - X^2 q^2$"
    assert latexify(raw) == expected


def test_latexify_preserves_text_outside_math():
    from qprov.claims import latexify
    assert latexify("at bidegree $(d_X, d_q) = (6,50)$ no annihilator exists") == \
        "at bidegree $(d_X, d_q) = (6,50)$ no annihilator exists"


def test_latexify_is_idempotent():
    from qprov.claims import latexify
    s = "$1 - X + X*q^2 - X^2*q^15$"
    assert latexify(latexify(s)) == latexify(s)


def test_export_latex_applies_latexify():
    cid = claim(
        "$P(X,q) = 1 - X + X*q^2 - X^2*q^15$ is the recurrence",
        deterministic_id=True,
    )
    out = export_latex()
    # Sage-style asterisks gone
    assert "X*q" not in out
    # Multi-digit exponent bracketed
    assert "q^{15}" in out
    # Single-digit exponent untouched
    assert "q^2" in out


def test_latex_escape_passes_math_through():
    cid = claim("R([\\sqrt{2}]_q) = 0.531213", value_numeric=0.531213)
    out = export_latex()
    # math content must survive verbatim
    assert "\\sqrt{2}" in out
    # but lone % gets escaped
    cid2 = claim("growth rate is 50% per N", value_numeric=50)
    out2 = export_latex()
    assert "50\\%" in out2
