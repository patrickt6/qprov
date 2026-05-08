"""CLI smoke tests via Click's CliRunner."""
from __future__ import annotations

import gzip
import json

import pytest
from click.testing import CliRunner

import qprov
from qprov import register_external, tracked
from qprov.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def seeded_computation():
    @tracked(tags={"constant": "pi"})
    def add(x, y):
        return x + y
    add(2, 3)
    return qprov.find()[0]


def test_cli_help(runner):
    res = runner.invoke(main, ["--help"])
    assert res.exit_code == 0
    assert "init" in res.output and "verify" in res.output


def test_cli_init_in_tmp(runner, tmp_path):
    res = runner.invoke(main, ["init", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert (tmp_path / ".qprov" / "qprov.sqlite").is_file()


def test_cli_list(runner, seeded_computation):
    res = runner.invoke(main, ["list"])
    assert res.exit_code == 0
    assert seeded_computation.id[:12] in res.output
    assert "add" in res.output


def test_cli_show(runner, seeded_computation):
    res = runner.invoke(main, ["show", seeded_computation.id])
    assert res.exit_code == 0
    data = json.loads(res.output.split("--- payload ---")[0])
    assert data["function_name"] == "add"
    assert data["status"] == "ok"


def test_cli_show_with_payload(runner, seeded_computation):
    res = runner.invoke(main, ["show", seeded_computation.id, "--payload"])
    assert res.exit_code == 0
    assert "--- payload ---" in res.output
    payload = json.loads(res.output.split("--- payload ---")[1])
    assert payload["result"] == 5


def test_cli_show_unique_prefix(runner, seeded_computation):
    res = runner.invoke(main, ["show", seeded_computation.id[:8]])
    assert res.exit_code == 0


def test_cli_find_by_tag(runner, seeded_computation):
    res = runner.invoke(main, ["find", "--tag", "constant=pi"])
    assert res.exit_code == 0
    assert seeded_computation.id[:12] in res.output


def test_cli_claim_and_export(runner, seeded_computation):
    res = runner.invoke(main, [
        "claim", "test claim", "--link", seeded_computation.id, "--value", "46",
    ])
    assert res.exit_code == 0
    assert "claim " in res.output
    res2 = runner.invoke(main, ["export-latex"])
    assert res2.exit_code == 0
    assert r"\fact{test claim}" in res2.output


def test_cli_export_latex_to_file(runner, seeded_computation, tmp_path):
    runner.invoke(main, ["claim", "linked", "--link", seeded_computation.id])
    target = tmp_path / "out.tex"
    res = runner.invoke(main, ["export-latex", "--output", str(target)])
    assert res.exit_code == 0
    assert target.is_file()
    text = target.read_text(encoding="utf-8")
    assert r"\fact{linked}" in text


def test_cli_gc_dry_run(runner, seeded_computation):
    res = runner.invoke(main, ["gc", "--dry-run"])
    assert res.exit_code == 0
    assert "1 computations are not referenced" in res.output


def test_cli_gc_skips_referenced(runner, seeded_computation):
    runner.invoke(main, ["claim", "linked", "--link", seeded_computation.id])
    res = runner.invoke(main, ["gc", "--dry-run"])
    assert res.exit_code == 0
    assert "nothing to gc" in res.output


def test_cli_show_missing_id(runner):
    res = runner.invoke(main, ["show", "deadbeef"])
    assert res.exit_code != 0
    assert "no computation" in (res.output + (res.stderr or ""))


def test_cli_show_no_verify_returns_tampered_payload(runner, seeded_computation):
    """`qprov show <id> --payload --no-verify` must read a tampered
    payload without raising. The v3 default check still raises
    PayloadTamperedError on `--payload` alone."""
    store = qprov.get_store()
    path = store.payload_path_for(seeded_computation.id)
    with gzip.open(path, "rb") as f:
        body = f.read().decode("utf-8")
    tampered = body.replace('"result":5', '"result":999')
    assert tampered != body
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as f:
        f.write(tampered.encode("utf-8"))

    # Without --no-verify: integrity check fires.
    res = runner.invoke(main, ["show", seeded_computation.id, "--payload"])
    assert res.exit_code != 0
    err_text = res.output + (res.stderr or "")
    assert "tamper" in err_text.lower() or "changed on disk" in err_text

    # With --no-verify: the tampered payload comes through.
    res2 = runner.invoke(
        main, ["show", seeded_computation.id, "--payload", "--no-verify"]
    )
    assert res2.exit_code == 0
    payload = json.loads(res2.output.split("--- payload ---")[1])
    assert payload["result"] == 999


def test_cli_lint_flags_tampered(runner):
    """A paper-backing computation whose payload was edited on disk
    must surface as TAMPERED in lint with exit code 1."""
    cid = register_external(
        function_name="kernel_search",
        inputs={"alpha": "phi"},
        outputs={"kernel_dim": 1},
        code_sha="sha",
    )
    qprov.claim(
        "first nonzero coefficient",
        computation_id=cid,
        tags={"paper": "my-paper"},
    )

    store = qprov.get_store()
    path = store.payload_path_for(cid)
    with gzip.open(path, "rb") as f:
        body = f.read().decode("utf-8")
    tampered = body.replace('"kernel_dim":1', '"kernel_dim":2')
    assert tampered != body
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as f:
        f.write(tampered.encode("utf-8"))

    res = runner.invoke(main, ["lint"])
    assert res.exit_code == 1
    combined = res.output + (res.stderr or "")
    assert "TAMPERED" in combined


def test_cli_lint_flags_id_drift(runner):
    """If the column input_hash is directly UPDATEd to a value that no
    longer matches the payload's args/kwargs, lint must report
    ID_DRIFT with exit code 1."""
    cid = register_external(
        function_name="kernel_search",
        inputs={"alpha": "phi"},
        outputs={"kernel_dim": 1},
        code_sha="sha",
    )
    qprov.claim(
        "claim text",
        computation_id=cid,
        tags={"paper": "my-paper"},
    )

    store = qprov.get_store()
    with store._connect() as conn:
        conn.execute(
            "UPDATE computations SET input_hash = ? WHERE id = ?",
            ("0" * 32, cid),
        )

    res = runner.invoke(main, ["lint"])
    assert res.exit_code == 1
    combined = res.output + (res.stderr or "")
    assert "ID_DRIFT" in combined


def test_cli_lint_clean_passes_with_real_data(runner):
    """A clean store with one backed paper claim and a non-tampered
    payload must exit 0 (no failures). Pre-existing NOHASH advisories
    are reported but do not flip the exit code."""
    cid = register_external(
        function_name="kernel_search",
        inputs={"alpha": "phi"},
        outputs={"kernel_dim": 1},
        code_sha="sha",
    )
    qprov.claim(
        "a clean claim",
        computation_id=cid,
        tags={"paper": "my-paper"},
    )
    res = runner.invoke(main, ["lint"])
    assert res.exit_code == 0
    combined = res.output + (res.stderr or "")
    assert "TAMPERED" not in combined
    assert "ID_DRIFT" not in combined
