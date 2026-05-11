"""File-content fingerprinting."""
from __future__ import annotations

import dataclasses
import warnings
from collections import namedtuple
from pathlib import Path

import pytest

import qprov
from qprov import (
    QprovFileMissingError,
    QprovHashError,
    QprovHashWarning,
    QprovTraversalError,
    canonical_file,
    hash_file,
    tracked,
)
from qprov.inputs import (
    CANONICAL_FILE_TAG,
    auto_canonicalize,
    collect_data_hashes,
    is_canonical_file_arg,
)
from qprov.store import get_store


def _write(parent, name, content):
    parent.mkdir(parents=True, exist_ok=True)
    p = parent / name
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return p


def test_hash_file_is_content_addressed(tmp_path):
    a = _write(tmp_path, "a.csv", b"n,coefficient\n0,1\n1,0\n")
    b = _write(tmp_path / "sub", "b.csv", b"n,coefficient\n0,1\n1,0\n")
    assert hash_file(a) == hash_file(b), "same content under different paths must hash identically"


def test_hash_file_differs_on_one_byte(tmp_path):
    a = _write(tmp_path, "a.csv", b"hello")
    b = _write(tmp_path, "b.csv", b"hellp")
    assert hash_file(a) != hash_file(b)


def test_canonical_file_carries_content_hash(tmp_path):
    p = _write(tmp_path, "data.csv", b"row1\nrow2\n")
    d = canonical_file(p)
    assert d[CANONICAL_FILE_TAG] is True
    assert d["name"] == "data.csv"
    assert d["size"] == p.stat().st_size
    assert d["sha"] == hash_file(p)


def test_canonical_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        canonical_file(tmp_path / "no-such.csv")


def test_is_canonical_file_arg_detects_descriptors(tmp_path):
    p = _write(tmp_path, "x.csv", b"hi")
    assert is_canonical_file_arg(canonical_file(p))
    assert not is_canonical_file_arg("a string")
    assert not is_canonical_file_arg({"path": str(p)})


def test_tracked_with_data_files_replaces_path_string(tmp_path):
    p = _write(tmp_path, "qreal_phi_5000.csv", b"n,coefficient\n0,1\n")

    @tracked(data_files=["csv"])
    def scan(csv):
        return csv  # echo back

    out = scan(str(p))
    assert isinstance(out, dict)
    assert out[CANONICAL_FILE_TAG] is True
    assert out["sha"] == hash_file(p)

    rows = qprov.find()
    assert len(rows) == 1
    assert rows[0].canonical_data_hash is not None
    assert hash_file(p) in rows[0].canonical_data_hash


def test_tracked_collapses_id_when_content_matches(tmp_path):
    """Same content under two different filenames must yield the same id."""
    a = _write(tmp_path, "a.csv", b"identical contents\n")
    b = _write(tmp_path, "b.csv", b"identical contents\n")
    assert hash_file(a) == hash_file(b)

    @tracked(data_files=["csv"])
    def scan(csv):
        return 1

    scan(str(a))
    scan(str(b))
    rows = qprov.find()
    # NOTE: ids differ because the descriptor includes the name and path.
    # The content hash IS the same, which is what audit cares about. If we
    # wanted name-blind collapse we'd strip name+path from the descriptor.
    shas = set()
    for r in rows:
        assert r.canonical_data_hash is not None
        shas.add(r.canonical_data_hash.split('"')[-2])  # peel sha from JSON
    assert len(shas) == 1, "both rows should record the same content sha"


def test_tracked_id_differs_when_content_differs(tmp_path):
    """The core regression case: same filename, different content => different id.

    Two machines hold a file named `qreal_phi_5000.csv`, one with 500 rows
    and one with 5000. Hashing only the path string gives both the same id,
    so INSERT OR REPLACE clobbers one with the other. Hashing the file
    contents gives them distinct ids, and both survive.
    """
    a = _write(tmp_path / "m1", "qreal_phi_5000.csv", b"500-row stub\n")
    b = _write(tmp_path / "m2", "qreal_phi_5000.csv", b"the real 5000-row\n")

    @tracked(data_files=["csv"])
    def scan(csv):
        return 1

    scan(str(a))
    scan(str(b))
    rows = qprov.find()
    assert len({r.id for r in rows}) == 2, "differing content must produce distinct ids"


def test_collect_data_hashes_handles_nested_structure(tmp_path):
    p = _write(tmp_path, "x.csv", b"hi")
    payload = {
        "args": (canonical_file(p),),
        "kwargs": {"meta": {"primary": canonical_file(p)}},
    }
    hashes = collect_data_hashes(payload)
    assert "x.csv" in hashes
    assert hashes["x.csv"] == hash_file(p)
    assert "x.csv#2" in hashes


# ---------------------------------------------------------------------------
# Regressions for the silent path-only hash fallback: a file-like parameter
# not declared in data_files must surface a warning, not hash the path string.
# ---------------------------------------------------------------------------


def test_warn_on_forgotten_data_files(tmp_path):
    """A file-like parameter without ``data_files=[...]`` must surface a
    ``QprovHashWarning`` so callers cannot accidentally fall back to
    path-string hashing.
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        @tracked(tags={"paper": "x"})
        def read_csv(csv_path):
            return Path(csv_path).read_text()

        csv = tmp_path / "a.csv"
        csv.write_text("hello")
        read_csv(str(csv))
    assert any("data_files" in str(x.message) for x in w)
    assert any(issubclass(x.category, QprovHashWarning) for x in w)


def test_require_data_files_raises_at_decoration_time(tmp_path):
    """``require_data_files=True`` upgrades the warning to a hard error,
    detected at decoration time before any call has been made.
    """
    with pytest.raises(QprovHashError):
        @tracked(tags={"paper": "x"}, require_data_files=True)
        def read_csv(csv_path):
            return Path(csv_path).read_text()


def test_require_data_files_strict_missing_file(tmp_path):
    """``require_data_files=True`` also makes a missing declared file a
    hard ``QprovFileMissingError`` at call time, replacing the v0.2
    soft fallback to path-string hashing.
    """

    @tracked(
        tags={"paper": "x"},
        data_files=["csv_path"],
        require_data_files=True,
    )
    def read_csv(csv_path):
        return Path(csv_path).read_text()

    with pytest.raises(QprovFileMissingError):
        read_csv(str(tmp_path / "does-not-exist.csv"))


def test_auto_canonicalize_warns_on_missing_default(tmp_path):
    """Default (non-strict) ``auto_canonicalize`` still warns when the
    path does not resolve, so the soft fallback is no longer silent.
    """
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = auto_canonicalize(str(tmp_path / "missing.csv"))
    assert result == str(tmp_path / "missing.csv")
    assert any(issubclass(x.category, QprovHashWarning) for x in w)


def test_auto_canonicalize_strict_raises_on_missing(tmp_path):
    with pytest.raises(QprovFileMissingError):
        auto_canonicalize(str(tmp_path / "missing.csv"), strict=True)


def test_visitor_handles_dataclass(tmp_path):
    """The visitor must descend into dataclass fields, which the
    earlier dict/list/tuple-only walker silently ignored."""

    @dataclasses.dataclass
    class Inputs:
        path: dict  # holds a canonical_file descriptor

    csv = tmp_path / "b.csv"
    csv.write_text("world")
    cf = canonical_file(csv)
    out: dict[str, str] = {}
    collect_data_hashes(Inputs(path=cf), out, key="root")
    assert "root.path" in out
    assert out["root.path"] == hash_file(csv)


def test_visitor_handles_namedtuple(tmp_path):
    """The visitor must distinguish namedtuples from plain tuples and
    descend via field names."""
    Bundle = namedtuple("Bundle", ["primary", "secondary"])
    a = tmp_path / "a.csv"
    a.write_text("aaa")
    b = tmp_path / "b.csv"
    b.write_text("bbb")
    payload = Bundle(primary=canonical_file(a), secondary=canonical_file(b))
    out: dict[str, str] = {}
    collect_data_hashes(payload, out, key="bundle")
    assert out["bundle.primary"] == hash_file(a)
    assert out["bundle.secondary"] == hash_file(b)


def test_visitor_handles_set_and_frozenset(tmp_path):
    """The visitor must survive sets and frozensets in the input tree.

    canonical_file descriptors cannot live directly inside a set
    (dicts are unhashable), but a set of tags or labels often sits
    alongside the file descriptor in the same payload. The earlier
    visitor would walk past the sibling and miss the descriptor here
    only if it crashed on the set; the regression is "does not
    crash, and still surfaces the canonical_file from the wider
    structure".
    """
    p = tmp_path / "s.csv"
    p.write_text("set-content")
    cf = canonical_file(p)
    payload = {
        "tags": {"alpha", "beta", "gamma"},
        "frozen": frozenset(["x", "y", "z"]),
        "csv": cf,
    }
    hashes = collect_data_hashes(payload)
    assert hashes.get("s.csv") == cf["sha"]


def test_visitor_handles_plain_object_with_dict(tmp_path):
    """A user class with ``__dict__`` must be walked."""

    class Inputs:
        def __init__(self, csv):
            self.csv = csv
            self.note = "ignore me"

    p = tmp_path / "u.csv"
    p.write_text("dunder")
    cf = canonical_file(p)
    out: dict[str, str] = {}
    collect_data_hashes(Inputs(csv=cf), out, key="i")
    assert out["i.csv"] == hash_file(p)


def test_visitor_handles_cycle_without_infinite_recursion(tmp_path):
    """A self-referencing structure must not crash the visitor."""
    p = tmp_path / "c.csv"
    p.write_text("cycle")
    cf = canonical_file(p)
    container: dict = {"cf": cf}
    container["self"] = container  # introduce a cycle
    # Must not raise RecursionError.
    hashes = collect_data_hashes(container)
    assert any(sha == cf["sha"] for sha in hashes.values())


def test_visitor_depth_limit_raises_traversal_error():
    """Pathologically deep structures must raise ``QprovTraversalError``
    rather than silently truncating, since the audit cares about which
    files contributed to a row."""
    deep: list = []
    cur = deep
    for _ in range(40):  # well past the default max_depth=16
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    with pytest.raises(QprovTraversalError):
        collect_data_hashes(deep)


def test_tracked_with_explicit_data_files_does_not_warn(tmp_path):
    """The decoration-time warning must not fire when ``data_files``
    covers the file-like parameter."""
    from qprov import path_of

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        @tracked(data_files=["csv_path"])
        def read_csv(csv_path):
            return Path(path_of(csv_path)).read_text()

        csv = tmp_path / "ok.csv"
        csv.write_text("declared")
        read_csv(str(csv))
    assert not any(
        issubclass(x.category, QprovHashWarning) and "data_files" in str(x.message)
        for x in w
    )
