"""Re-run a recorded computation and assert bit-identical output.

Strategy:
  1. Look up the row in SQLite.
  2. Read the payload.
  3. Import payload['function_module'] and resolve `function_name`.
  4. Re-invoke with the recorded args/kwargs.
  5. Hash the new output and compare to the stored output_hash.

Caveats (matched to the PRD):
  - If the function isn't importable (lambda, function moved, function only
    defined inside a Sage `.sage` file), we report a clear error and abort -
    we do not silently mark the computation 'unverifiable'.
  - If the canonical-JSON hashes match exactly, verify reports OK.
  - Any divergence: verify reports both hashes and the first differing byte
    of the canonical JSON.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from .serialize import canonical_dumps, hash_value
from .store import get_store


@dataclass
class VerifyResult:
    ok: bool
    computation_id: str
    expected_hash: str | None
    actual_hash: str | None
    message: str
    diff_index: int | None = None


def _resolve_callable(module_name: str | None, function_name: str) -> Any:
    if not module_name:
        raise LookupError(
            f"computation has no recorded module; cannot resolve {function_name!r}"
        )
    mod = importlib.import_module(module_name)
    func = getattr(mod, function_name, None)
    if func is None:
        raise LookupError(
            f"function {function_name!r} not found in module {module_name!r}"
        )
    if hasattr(func, "__wrapped__"):
        # Unwrap @tracked so verify doesn't double-record. The store's
        # idempotent id collapses repeats anyway, but the unwrap keeps the
        # verify side-effect-free.
        return func.__wrapped__
    return func


def _first_diff_index(a: str, b: str) -> int | None:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return n
    return None


def verify(comp_id: str) -> VerifyResult:
    store = get_store()
    comp = store.get_computation(comp_id)
    if comp is None:
        return VerifyResult(False, comp_id, None, None, f"no computation with id {comp_id!r}")
    if comp.status != "ok":
        return VerifyResult(
            False, comp.id, comp.output_hash, None,
            f"original run did not complete successfully (status={comp.status!r}, "
            f"error={comp.error_type!r}: {comp.error_message!r})",
        )
    payload = store.read_payload(comp.id)
    args = payload.get("args", [])
    kwargs = payload.get("kwargs", {})
    try:
        func = _resolve_callable(payload.get("function_module"), payload["function_name"])
    except Exception as exc:
        return VerifyResult(
            False, comp.id, comp.output_hash, None,
            f"could not resolve original function: {type(exc).__name__}: {exc}",
        )
    try:
        new_result = func(*args, **kwargs)
    except Exception as exc:
        return VerifyResult(
            False, comp.id, comp.output_hash, None,
            f"re-run raised {type(exc).__name__}: {exc}",
        )
    new_hash = hash_value(new_result)
    if new_hash == comp.output_hash:
        return VerifyResult(True, comp.id, comp.output_hash, new_hash, "bit-identical output")
    expected_text = canonical_dumps(payload.get("result"))
    actual_text = canonical_dumps(new_result)
    diff = _first_diff_index(expected_text, actual_text)
    return VerifyResult(
        False, comp.id, comp.output_hash, new_hash,
        "output hash differs - re-run produced a different value than the original. "
        "If the function is non-deterministic, seed any RNG before decorating.",
        diff_index=diff,
    )
