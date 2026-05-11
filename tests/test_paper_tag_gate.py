"""Paper-tagged claims require a backing computation."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import qprov
from qprov import UnbackedPaperClaimError, claim, tracked
from qprov.cli import main as cli_main
from qprov.store import get_store


def _make_backed_computation():
    @tracked
    def make_data():
        return {"n": 42}
    make_data()
    return qprov.find()[0]


def test_unbacked_paper_tag_raises():
    """A paper-tagged claim with no computation_id once silently inserted a
    row that would render as `\\provid{None}` in LaTeX. Now it raises."""
    with pytest.raises(UnbackedPaperClaimError):
        claim(
            "The first nonzero coefficient of $[\\pi]_q$ after $q^{45}$ is at $q^{46}$.",
            tags={"paper": "my-paper"},
        )


def test_backed_paper_claim_succeeds():
    comp = _make_backed_computation()
    cid = claim(
        "A paper-ready statement.",
        computation_id=comp.id,
        tags={"paper": "my-paper"},
    )
    assert cid
    rec = get_store().get_claim(cid)
    assert rec.tags == {"paper": "my-paper"}
    assert rec.computation_id == comp.id


def test_allow_unbacked_escape_valve():
    """For staged claims awaiting back-attach, the escape valve persists the
    row anyway and auto-tags it `unbacked=true` so `qprov lint` can
    distinguish opted-in unbacked from forgot-to-attach."""
    cid = claim(
        "Staged claim awaiting data.",
        tags={"paper": "other-paper"},
        allow_unbacked=True,
    )
    rec = get_store().get_claim(cid)
    assert rec is not None
    assert rec.computation_id is None
    assert rec.tags == {"paper": "other-paper", "unbacked": "true"}


def test_non_paper_tag_does_not_gate():
    cid = claim("freeform", tags={"category": "modular"})
    rec = get_store().get_claim(cid)
    assert rec.tags == {"category": "modular"}


def test_list_claims_by_tag_finds_paper_claims():
    comp = _make_backed_computation()
    claim("one", computation_id=comp.id, tags={"paper": "my-paper"})
    claim("two", computation_id=comp.id, tags={"paper": "my-paper"})
    claim("three", computation_id=comp.id, tags={"paper": "other-paper"})
    p1 = get_store().list_claims_by_tag("paper", "my-paper")
    assert len(p1) == 2
    p4 = get_store().list_claims_by_tag("paper", "other-paper")
    assert len(p4) == 1
    every = get_store().list_claims_by_tag("paper")
    assert len(every) == 3


def test_cli_claim_paper_tag_without_link_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("QPROV_HOME", str(tmp_path / ".qprov"))
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["claim", "stmt", "--tag", "paper=my-paper"],
    )
    assert result.exit_code != 0
    assert "paper" in result.output.lower() or "unbacked" in result.output.lower()


def test_cli_lint_flags_orphan(tmp_path, monkeypatch):
    """A claim that is paper-tagged but NOT marked unbacked is an ORPHAN.

    The v3 CHECK constraint rejects such a row on any INSERT or
    UPDATE, so this test simulates the legacy corruption via
    ``PRAGMA ignore_check_constraints = 1`` (SQLite's only legal
    bypass). Point of the test is defense-in-depth: lint must still
    catch the row even if the CHECK were ever disabled by an older
    toolchain.
    """
    monkeypatch.setenv("QPROV_HOME", str(tmp_path / ".qprov"))
    from qprov import store as store_mod
    from qprov.store import utc_now_iso
    store_mod._store_singleton = None
    store = store_mod.get_store()

    with store._connect() as conn:
        conn.execute("PRAGMA ignore_check_constraints = 1")
        conn.execute(
            "INSERT INTO claims "
            "(id, text, value_numeric, computation_id, created_at, notes, paper_tag, unbacked) "
            "VALUES (?, ?, NULL, NULL, ?, ?, ?, 0)",
            (
                "legacy_orphan_001",
                "A paper-bound statement with no computation backing.",
                utc_now_iso(),
                "simulated v0.1 carry-over",
                "third-paper",
            ),
        )
        conn.execute(
            "INSERT INTO claim_tags (claim_id, key, value) VALUES (?, 'paper', 'third-paper')",
            ("legacy_orphan_001",),
        )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["lint"])
    assert result.exit_code == 1
    assert "ORPHAN" in result.output or "ORPHAN" in (result.stderr or "")


def test_allow_unbacked_does_not_trigger_lint():
    """A claim staged via allow_unbacked=True must NOT show up as ORPHAN
    in lint - the whole point of the escape valve is opt-in immunity."""
    runner = CliRunner()
    claim("staged statement", tags={"paper": "third-paper"}, allow_unbacked=True)
    result = runner.invoke(cli_main, ["lint"])
    assert result.exit_code == 0
    assert "ORPHAN" not in result.output
    assert "unbacked" in result.output.lower()


def test_cli_lint_passes_when_all_backed():
    """Backed paper claims must pass the lint (exit code 0).

    A bare backed computation registered via @tracked has no
    canonical_data_hash because the function took no data_files. That
    surfaces as a NOHASH advisory, which is intentionally non-failing.
    """
    runner = CliRunner()
    comp = _make_backed_computation()
    claim("a backed claim", computation_id=comp.id, tags={"paper": "my-paper"})
    result = runner.invoke(cli_main, ["lint"])
    assert result.exit_code == 0
    assert "ORPHAN" not in result.output
    assert "DANGLING" not in result.output
