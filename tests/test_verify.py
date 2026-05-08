"""Verify command: bit-identical re-run check.

Verify needs the original function to be importable. The test fixture
defines the target in this very module, decorates it, runs it, then asks
verify to resolve qprov.tests.test_verify.<name> and re-invoke.
"""
from __future__ import annotations

import qprov
from qprov import tracked
from qprov.verify import verify


@tracked
def deterministic_add(x, y):
    return x + y


@tracked
def builds_a_list(n):
    return [k * k for k in range(n)]


def test_verify_returns_ok_for_deterministic():
    deterministic_add(7, 9)
    comp = qprov.find(function="deterministic_add")[0]
    result = verify(comp.id)
    assert result.ok, result.message
    assert result.expected_hash == result.actual_hash


def test_verify_handles_list_output():
    builds_a_list(20)
    comp = qprov.find(function="builds_a_list")[0]
    result = verify(comp.id)
    assert result.ok, result.message


def test_verify_unknown_id_fails_clearly():
    result = verify("does-not-exist")
    assert not result.ok
    assert "no computation" in result.message


def test_verify_detects_modified_record(monkeypatch):
    deterministic_add(1, 2)
    comp = qprov.find(function="deterministic_add")[0]
    # tamper with the stored output_hash to force a mismatch
    store = qprov.get_store()
    with store._connect() as conn:
        conn.execute(
            "UPDATE computations SET output_hash = ? WHERE id = ?",
            ("0" * 32, comp.id),
        )
    result = verify(comp.id)
    assert not result.ok
    assert result.actual_hash and result.expected_hash == "0" * 32
