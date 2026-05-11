"""File-content fingerprints for tracked inputs.

A path string is not data. Two machines can hold a CSV named
`qreal_phi_5000.csv` where one has 5000 rows and the other has 500; the
filename collides but the contents do not. Before this module existed,
`@tracked` hashed only the path string, so the two machines produced the
same computation id and silently overwrote each other in any shared store.

The fix is to record a content fingerprint alongside (or instead of) the
filename. Two entry points:

  * `hash_file(path)` returns the raw blake2b digest of the file's bytes.
    Use it when you only need the hash itself.

  * `canonical_file(path)` returns a small dict suitable for embedding in
    a tracked function's arguments. The dict carries the absolute path,
    the size, the mtime, and the content hash; the content hash is what
    actually contributes to the input hash, so two machines with the
    same file contents under different paths still collapse to the same
    computation id (the correct behavior).

The decorator wires this up automatically via the `data_files` parameter
on `@tracked`; manual callers can call `canonical_file()` directly.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import warnings as _warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_CHUNK = 1 << 20  # 1 MiB

CANONICAL_FILE_TAG = "__qprov_canonical_file__"

# Visitor traversal cap. 16 is plenty for legitimately nested
# scientific input structures (a dict of lists of dataclasses of dicts
# is ~5 deep); anything past that is almost certainly a cycle or a
# pathological structure that we should refuse to fingerprint silently.
_VISITOR_MAX_DEPTH = 16


def hash_file(path: str | os.PathLike) -> str:
    """blake2b hex digest of the file's bytes (streamed, no full load)."""
    p = Path(path)
    h = hashlib.blake2b(digest_size=16)
    with open(p, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def canonical_file(path: str | os.PathLike) -> dict[str, Any]:
    """Return a content-pinned descriptor of a file, suitable as a tracked input.

    The returned dict is intentionally JSON-serializable and stable: callers
    can pass it as an argument to a `@tracked` function, and the decorator's
    canonical-JSON hashing picks up the content hash. Same content -> same
    descriptor -> same computation id, regardless of which machine the file
    sits on.

    Example:

        from qprov import tracked, canonical_file

        @tracked
        def scan_csv(csv):
            data = read_csv(csv["path"])
            return summarize(data)

        scan_csv(canonical_file("./data/qreal_phi_5000.csv"))
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"canonical_file: no file at {p}")
    digest = hash_file(p)
    stat = p.stat()
    return {
        CANONICAL_FILE_TAG: True,
        "path": str(p.resolve()),
        "name": p.name,
        "size": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha": digest,
    }


def is_canonical_file_arg(value: Any) -> bool:
    return isinstance(value, dict) and bool(value.get(CANONICAL_FILE_TAG))


def path_of(value: Any) -> str:
    """Return the on-disk path for either a canonical_file descriptor or a plain path.

    Use inside @tracked function bodies that take a `data_files`-declared
    parameter: the decorator may have replaced the original path string with
    a canonical_file dict, so the function can no longer pass it straight
    to `open()`. `path_of(arg)` papers over the difference.
    """
    if isinstance(value, dict) and value.get(CANONICAL_FILE_TAG):
        return value["path"]
    return os.fspath(value)


def auto_canonicalize(value: Any, strict: bool = False) -> Any:
    """If ``value`` looks like a path to an existing file, return ``canonical_file(value)``.

    Used by ``@tracked(data_files=[...])`` so callers can keep passing
    plain strings. Pass-through for everything that is not a path or
    is already a canonical_file descriptor.

    When ``strict=False`` (the default), a value that *looks* like a
    path but does not resolve to an existing file emits a
    ``QprovHashWarning`` and falls back to path-string hashing - the
    pre-v0.2 behaviour, preserved for backwards compatibility. When
    ``strict=True``, the same situation raises
    ``QprovFileMissingError`` instead. ``strict=True`` is wired up
    from ``@tracked(..., require_data_files=True)``.
    """
    # Local imports break the otherwise-circular tracking <-> inputs
    # cycle. ``tracking`` already imports from ``inputs``, so the
    # exception classes have to be resolved lazily here.
    from .tracking import QprovFileMissingError, QprovHashWarning

    if isinstance(value, dict) and value.get(CANONICAL_FILE_TAG):
        return value
    if isinstance(value, (str, os.PathLike)):
        try:
            p = Path(value)
        except (TypeError, ValueError):
            return value
        if p.is_file():
            return canonical_file(p)
        if strict:
            raise QprovFileMissingError(
                f"auto_canonicalize: path {value!r} does not resolve "
                f"to an existing file at hash time. This is the v0.2 "
                f"'soft fallback' failure mode. Either fix the path "
                f"or remove the data_files declaration."
            )
        _warnings.warn(
            f"auto_canonicalize: path {value!r} did not resolve; "
            f"falling back to path-string hashing.",
            category=QprovHashWarning,
            stacklevel=3,
        )
    return value


def collect_data_hashes(
    payload: Any,
    out: dict[str, str] | None = None,
    *,
    key: str | None = None,
    max_depth: int = _VISITOR_MAX_DEPTH,
) -> dict[str, str] | None:
    """Extract canonical_file descriptors hidden in ``payload``.

    Two call shapes, distinguished by whether the caller supplies
    ``out``:

    * Legacy: ``collect_data_hashes(payload)`` returns a fresh dict
      keyed by the descriptor's ``name`` (basename), with a ``#N``
      suffix on collisions so duplicates remain visible. This is the
      earlier shape used by the decorator to build
      ``computations.canonical_data_hash``.

    * Path-keyed: ``collect_data_hashes(value, out, key="root")``
      mutates ``out`` in place, using dotted path keys
      (``root.<field>.<field>``) so callers can trace where a file
      lived in their input graph. Returns ``None``.

    The visitor traverses dict, list, tuple, set, frozenset,
    namedtuple, dataclass instances, numpy structured arrays
    (optional, ``numpy`` import gated), and any object with a
    populated ``__dict__``. Strings, bytes, and numeric primitives
    are atomic.

    Two safety nets:

    * A ``set`` of ``id()`` values guards against reference cycles.
      The id is removed when we leave a node, so legitimate DAGs
      reached via multiple paths still get fingerprinted at each
      reachable occurrence.
    * A depth limit (``max_depth``, default 16) raises
      ``QprovTraversalError`` on overflow. Deeper-than-16 nesting in
      a research payload is almost certainly a cycle or a
      pathological structure that we should refuse to fingerprint
      silently - the audit cares about *which* files contributed to
      a row, and a silently-truncated walk would hide gaps.
    """
    from .tracking import QprovTraversalError

    path_keyed = out is not None
    result: dict[str, str] = out if path_keyed else {}
    seen_names: dict[str, int] = {}
    seen_ids: set[int] = set()

    def _emit(descriptor: dict, key_path: str) -> None:
        if path_keyed:
            result[key_path] = descriptor.get("sha", "")
            return
        name = descriptor.get("name") or "<unnamed>"
        seen_names[name] = seen_names.get(name, 0) + 1
        out_key = name if seen_names[name] == 1 else f"{name}#{seen_names[name]}"
        result[out_key] = descriptor.get("sha", "")

    def _join(prefix: str, child: str) -> str:
        if not prefix:
            return child
        return f"{prefix}.{child}"

    def visit(v: Any, depth: int, key_path: str) -> None:
        if depth > max_depth:
            raise QprovTraversalError(
                f"collect_data_hashes: input structure exceeds depth "
                f"limit {max_depth}. Either a cycle in the inputs or "
                f"a pathologically nested object - refusing to "
                f"silently truncate."
            )
        # Cheap primitives short-circuit. Strings/bytes are
        # explicitly NOT recursed into (they are atomic from the
        # visitor's perspective).
        if v is None or isinstance(v, (bool, int, float, complex, str, bytes, bytearray)):
            return

        # Cycle guard on the heavyweights only - primitives can have
        # shared ids and we do not want to refuse a legitimate
        # ``[1, 1, 1]`` payload.
        oid = id(v)
        if oid in seen_ids:
            return
        seen_ids.add(oid)

        try:
            if isinstance(v, dict):
                if v.get(CANONICAL_FILE_TAG):
                    _emit(v, key_path)
                    return
                for k, val in v.items():
                    visit(val, depth + 1, _join(key_path, str(k)))
                return

            # namedtuple detection must precede plain-tuple traversal
            # because every namedtuple is a tuple. ``_fields`` is the
            # canonical marker.
            if isinstance(v, tuple) and hasattr(v, "_fields"):
                for fname in v._fields:
                    visit(getattr(v, fname), depth + 1, _join(key_path, fname))
                return

            if isinstance(v, (list, tuple)):
                for i, item in enumerate(v):
                    visit(item, depth + 1, _join(key_path, str(i)))
                return

            if isinstance(v, (set, frozenset)):
                # Sets have no positional or named order; emit with
                # a stable sentinel so path-keyed walks remain
                # deterministic.
                for i, item in enumerate(v):
                    visit(item, depth + 1, _join(key_path, f"{{{i}}}"))
                return

            if dataclasses.is_dataclass(v) and not isinstance(v, type):
                for f in dataclasses.fields(v):
                    visit(getattr(v, f.name), depth + 1, _join(key_path, f.name))
                return

            # numpy structured-array support, gated by a soft import
            # so pure-Python installs do not pay an import cost.
            try:
                import numpy as _np
            except ImportError:
                _np = None
            if _np is not None and isinstance(v, _np.ndarray) and v.dtype.names:
                for field_name in v.dtype.names:
                    visit(v[field_name].tolist(), depth + 1, _join(key_path, field_name))
                return

            # Generic object with a __dict__ (covers most user
            # classes). Skip if it has slots only or no introspectable
            # state.
            d = getattr(v, "__dict__", None)
            if isinstance(d, dict) and d:
                for k, val in d.items():
                    visit(val, depth + 1, _join(key_path, str(k)))
                return
        finally:
            # Allow re-visiting the same id from a different branch.
            # The cycle guard is per-walk, not per-input-graph.
            seen_ids.discard(oid)

    if path_keyed:
        visit(payload, 0, key or "")
        return None

    if isinstance(payload, dict) and (
        "args" in payload or "kwargs" in payload
    ):
        # Legacy payload-shape entry point used by the decorator.
        for slot in ("args", "kwargs"):
            visit(payload.get(slot, ()), 0, slot)
    else:
        # Direct traversal for external callers without ``out``.
        visit(payload, 0, "")
    return result
