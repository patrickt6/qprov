"""Audit-paper end-to-end and unit regressions.

Each test exercises one of the four paths the audit can take when it
encounters a `\\provid{...}` reference: clean match, numeric drift,
missing id, or orphan claim (claim row but no backing computation).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import qprov
from qprov import (
    audit_paper,
    register_external,
    tracked,
    Claim,
)
from qprov.audit_paper import (
    extract_numbers_from_paragraph,
    extract_provid_references,
    render_report,
)
from qprov.store import get_store, utc_now_iso


# ---------------------------------------------------------------------------
# Helpers


def _write_tex(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "synthetic.tex"
    p.write_text(body, encoding="utf-8")
    return p


def _seed_claim_with_payload(
    claim_id: str,
    *,
    text: str,
    payload: dict,
    paper_tag: str = "test-paper",
) -> str:
    """Register a computation via register_external and a claim that
    links to it. Returns the claim id.
    """
    cid = register_external(
        function_name=f"f_{claim_id}",
        inputs=payload.get("inputs", {}),
        outputs=payload.get("outputs", {}),
        notes=None,
        tags={"paper": paper_tag},
    )
    qprov.claim(
        text,
        computation_id=cid,
        claim_id=claim_id,
        tags={"paper": paper_tag},
    )
    return claim_id


# ---------------------------------------------------------------------------
# Helper-level regressions


def test_extract_provid_handles_escaped_underscores(tmp_path):
    body = r"""

\provid{polynomial\_emptiness\_cbrt2} appears here.

Different paragraph with \provid{plain_name}.
"""
    refs = extract_provid_references(body)
    ids = [pid for pid, _, _ in refs]
    assert "polynomial_emptiness_cbrt2" in ids
    assert "plain_name" in ids


def test_extract_provid_skips_macro_definition():
    """A `\\provid{#1}` inside `\\newcommand{\\provid}[1]{...}` should not
    be treated as a real reference (the `#1` token is a macro
    parameter, not an id)."""
    body = r"\newcommand{\provid}[1]{\texttt{#1}}"
    refs = extract_provid_references(body)
    assert refs == []


def test_extract_provid_skips_no_provenance_placeholder():
    body = r"""

A claim with \provid{no-provenance}.

"""
    refs = extract_provid_references(body)
    assert refs == []


def test_extract_numbers_skips_citation_keys():
    paragraph = (
        r"See Section~\ref{sec:5}, equation~\ref{eq:42}, and "
        r"\cite{Smith-2024-paper}. The kernel dimension is 0."
    )
    nums = extract_numbers_from_paragraph(paragraph)
    values = [n.value for n in nums]
    # The "5", "42", and "2024" inside cite/ref must be filtered out.
    assert 5 not in values
    assert 42 not in values
    assert 2024 not in values
    assert 0 in values


def test_extract_numbers_recognizes_floats_and_fractions():
    paragraph = "The bound is 0.531, or about 1/2."
    nums = extract_numbers_from_paragraph(paragraph)
    kinds = {(n.kind, n.value) for n in nums}
    assert ("float", 0.531) in kinds
    assert ("fraction", 0.5) in kinds


# ---------------------------------------------------------------------------
# Audit-level regressions


def test_match(tmp_path):
    """Paragraph asserts a number that matches the linked payload exactly.
    Expect MATCH, no mismatches."""
    _seed_claim_with_payload(
        "test_match_claim",
        text="The kernel has dimension 0.",
        payload={"inputs": {"d_X": 6, "d_q": 50}, "outputs": {"kernel_dim": 0}},
    )
    tex = _write_tex(
        tmp_path,
        r"""

The kernel-emptiness claim for cbrt2 at bidegree $(6, 50)$ has
$\dim \ker M = 0$ and is recorded under
\provid{test_match_claim}.

"""
    )
    report = audit_paper(tex, get_store())
    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.status == "MATCH", entry.detail
    assert entry.mismatches == []
    assert entry.claim_text is not None
    assert report.summary["MATCH"] == 1


def test_drift_on_numeric(tmp_path):
    """Paragraph asserts a tail length 4972 that does not appear in the
    payload (which records 4973). Expect DRIFT with a Mismatch on the
    drifted number."""
    _seed_claim_with_payload(
        "test_drift_claim",
        text="Unique zero in the tail.",
        payload={
            "inputs": {"N_min": 27, "N_max": 4999},
            "outputs": {"tail_length": 4973, "zero_index": 45},
        },
    )
    tex = _write_tex(
        tmp_path,
        r"""

The unique zero at $n = 45$ across the tail of length 4972 is recorded
under \provid{test_drift_claim}.

"""
    )
    report = audit_paper(tex, get_store())
    entry = report.entries[0]
    assert entry.status == "DRIFT", entry.detail
    drift_values = [m.paper_value for m in entry.mismatches]
    assert 4972 in drift_values
    # 45 is in the payload (zero_index=45), so it should NOT be a drift.
    assert 45 not in drift_values
    assert report.summary["DRIFT"] == 1


def test_missing_provid(tmp_path):
    """A \\provid{...} that does not resolve to any claim or computation
    must surface as MISSING."""
    tex = _write_tex(
        tmp_path,
        r"""

The Laurent-extended bound is recorded as \provid{nonexistent_claim_id}.

"""
    )
    report = audit_paper(tex, get_store())
    entry = report.entries[0]
    assert entry.status == "MISSING", entry.detail
    assert "nonexistent_claim_id" in entry.detail
    assert report.summary["MISSING"] == 1


def test_orphan_claim(tmp_path):
    """A claim row with NULL computation_id (allow_unbacked=True) must
    surface as ORPHAN; the paragraph cannot be checked against a
    payload that does not exist."""
    qprov.claim(
        "Unbacked staging claim.",
        claim_id="test_orphan_claim",
        tags={"paper": "test-paper"},
        allow_unbacked=True,
    )
    tex = _write_tex(
        tmp_path,
        r"""

A staged but unbacked claim sits under
\provid{test_orphan_claim} pending back-attachment.

"""
    )
    report = audit_paper(tex, get_store())
    entry = report.entries[0]
    assert entry.status == "ORPHAN", entry.detail
    assert "test_orphan_claim" in entry.detail
    assert report.summary["ORPHAN"] == 1


def test_provid_can_be_computation_id_directly(tmp_path):
    """The auto-generated claims.tex writes the *computation* id inside
    `\\provid{...}` (per claims.render_latex). The audit must resolve
    that case too, not just named claim ids."""
    cid = register_external(
        function_name="f_direct",
        inputs={"x": 3},
        outputs={"y": 7},
        tags={"paper": "test-paper"},
    )
    tex = _write_tex(
        tmp_path,
        f"""

The output is 7, recorded under \\provid{{{cid}}}.

"""
    )
    report = audit_paper(tex, get_store())
    entry = report.entries[0]
    assert entry.status == "MATCH", entry.detail


def test_render_markdown_contains_provid_and_status(tmp_path):
    _seed_claim_with_payload(
        "test_md_claim",
        text="Match-format test.",
        payload={"outputs": {"value": 1}},
    )
    tex = _write_tex(
        tmp_path,
        r"""

The value is 1, recorded as \provid{test_md_claim}.

"""
    )
    report = audit_paper(tex, get_store())
    md = render_report(report, output_format="markdown")
    assert "test_md_claim" in md
    assert "MATCH" in md


def test_render_json_round_trips(tmp_path):
    import json
    _seed_claim_with_payload(
        "test_json_claim",
        text="JSON-format test.",
        payload={"outputs": {"value": 42}},
    )
    tex = _write_tex(
        tmp_path,
        r"""

The value is 42, recorded as \provid{test_json_claim}.

"""
    )
    report = audit_paper(tex, get_store())
    blob = render_report(report, output_format="json")
    data = json.loads(blob)
    assert data["summary"]["MATCH"] == 1
    assert data["entries"][0]["provid"] == "test_json_claim"
