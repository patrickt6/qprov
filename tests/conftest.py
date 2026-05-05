"""Pytest fixtures: every test gets a fresh, isolated qprov store."""
from __future__ import annotations

import importlib

import pytest

import qprov
from qprov import store as store_mod


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point qprov at a per-test temp directory, then reset module state."""
    store_root = tmp_path / ".qprov"
    monkeypatch.setenv("QPROV_HOME", str(store_root))
    # Clear any cached singleton from prior tests
    store_mod._store_singleton = None
    store_mod._store_root_override = None
    importlib.reload(qprov)  # ensures the package re-resolves the store
    yield store_mod.get_store()
    store_mod._store_singleton = None
    store_mod._store_root_override = None
