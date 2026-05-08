"""Retroactive registration of computations produced outside @tracked.

Use case: a large body of pre-existing per-cell JSON files (for example
under a `data/` directory) representing finished computations that
never ran through the @tracked decorator. `register_external` folds those
results into the qprov store so they show up in `qprov list`, can be
tagged, and can have claims attached.

The id convention mirrors @tracked exactly:

    id = blake2b(function_name | input_hash | code_sha)

so re-registering the same logical computation collapses to the same row.
This makes the retroactive-registration script safely re-runnable.

Hardware fields are left None: we don't know what machine produced the
original JSON. status is always "ok" - failures should not be retroactively
registered (rerun them if you care about preserving the failure).
"""
from __future__ import annotations

import json
import os
from typing import Any

from .inputs import collect_data_hashes
from .serialize import hash_value
from .store import Computation, get_store, utc_now_iso
from .tracking import _make_id


def register_external(
    *,
    function_name: str,
    inputs: dict[str, Any],
    outputs: Any = None,
    code_path: str | None = None,
    code_sha: str | None = None,
    runtime_seconds: float | None = 0.0,
    started_at: str | None = None,
    ended_at: str | None = None,
    tags: dict[str, Any] | None = None,
    source_file: str | os.PathLike | None = None,
    notes: str | None = None,
) -> str:
    """Register a computation produced outside the @tracked decorator.

    All metadata is supplied by the caller; the function does no
    introspection - it cannot reach back into the original Python process.

    Args:
        function_name: logical name of the function that produced the JSON
            (e.g. "kernel_search_part2_v1"). With `inputs` and `code_sha`,
            determines the row id.
        inputs: input dict for the original computation. Hashed to derive
            input_hash. Pass {} if the original had no inputs.
        outputs: the output value, or None if the JSON carries inputs only.
            Hashed to derive output_hash when non-None.
        code_path: path (relative to project root) to the .sage / .py file
            that produced the JSON. Stored in the payload, not in the id.
        code_sha: git SHA of the code when the JSON was produced. Used in
            the id calculation, so accuracy matters - different SHAs make
            different rows.
        runtime_seconds: wall-clock runtime of the original computation,
            or 0 if not recorded.
        started_at, ended_at: ISO-8601 timestamps. Default to "now" for
            sort ordering; pass real values if the JSON has them.
        tags: arbitrary key/value pairs. Retroactive scripts should always
            include {"retroactive": True} so these rows are filterable.
        source_file: on-disk JSON file this row was built from. Recorded
            in the payload for traceability.
        notes: optional free-text annotation, stored in the payload.

    Returns:
        The computation id (32-char blake2b hex, same format as @tracked).
    """
    if not function_name or not isinstance(function_name, str):
        raise ValueError("function_name must be a non-empty string")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be a dict (use {} for no inputs)")

    input_hash = hash_value(inputs)
    output_hash = hash_value(outputs) if outputs is not None else None
    comp_id = _make_id(function_name, input_hash, code_sha)

    data_hashes = collect_data_hashes({"args": (), "kwargs": inputs})
    canonical_data_hash = (
        json.dumps(data_hashes, sort_keys=True, separators=(",", ":"))
        if data_hashes
        else None
    )

    now = utc_now_iso()
    started = started_at or now
    ended = ended_at or started

    payload = {
        "id": comp_id,
        "function_name": function_name,
        "function_module": None,
        "function_source": "<external>",
        "inputs": inputs,
        "outputs": outputs,
        "external": True,
        "source_file": str(source_file) if source_file is not None else None,
        "code_path": code_path,
        "notes": notes,
    }

    store = get_store()
    payload_path, payload_hash = store.write_payload(comp_id, payload)

    from .store import PAYLOAD_HASH_ALGORITHM
    rt = float(runtime_seconds) if runtime_seconds is not None else 0.0
    comp = Computation(
        id=comp_id,
        function_name=function_name,
        function_module=None,
        input_hash=input_hash,
        output_hash=output_hash,
        code_sha=code_sha,
        code_dirty=None,
        hostname=None,
        cpu_model=None,
        ram_gb=None,
        gpu_model=None,
        python_version=None,
        sage_version=None,
        os_info=None,
        runtime_seconds=rt,
        started_at=started,
        ended_at=ended,
        status="ok",
        error_type=None,
        error_message=None,
        payload_path=str(payload_path),
        tags={k: str(v) for k, v in (tags or {}).items()},
        canonical_data_hash=canonical_data_hash,
        payload_hash=payload_hash,
        output_hash_algorithm=PAYLOAD_HASH_ALGORITHM,
    )
    store.insert_computation(comp)
    return comp_id
