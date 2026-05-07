"""@tracked decorator. Records every call into the active store.

Flow:
  1. hash inputs *before* the call (canonical-JSON hash of args/kwargs)
  2. capture stdout/stderr/warnings during the call
  3. on success: hash the return value, write payload, write SQLite row
  4. on exception: record exception type+message+traceback, re-raise
  5. always return the original result (the decorator is transparent)

Identity convention: id = blake2b(function_name | input_hash | code_sha).
This means the same code on the same inputs collapses to the same row, which
makes verify trivially correct.
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import io
import json
import os
import time
import traceback
import warnings as _warnings
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from . import gitinfo, hardware
from .inputs import auto_canonicalize, collect_data_hashes
from .properties import Property, PropertyResult, QprovPropertyError
from .serialize import canonical_dumps, hash_text, hash_value
from .store import Computation, get_store, utc_now_iso

F = TypeVar("F", bound=Callable[..., Any])


class QprovHashWarning(UserWarning):
    """Warning category emitted when @tracked detects an input-hashing gap.

    Two situations trigger it:

    1. The decorated function has a file-like parameter (name ending in
       ``_path``/``_file``/``_csv``/``_json``/``_pkl``, or annotated as
       ``Path``/``PathLike``/``str`` with ``"path"`` in the name) but
       ``data_files=[...]`` was not declared. The input hash will be
       computed from the path string rather than the file content, which
       is the failure mode the ``data_files`` parameter exists to close.
    2. ``auto_canonicalize`` was asked to canonicalize a value that does
       not resolve to an existing file on disk. Same effect.

    Pass ``data_files=[...]`` to silence (1). Pass
    ``require_data_files=True`` to escalate either case into a
    ``QprovHashError`` or ``QprovFileMissingError`` instead of a soft
    warning.
    """


class QprovHashError(RuntimeError):
    """Raised at decoration time when ``require_data_files=True`` and
    @tracked detects a file-like parameter not declared in
    ``data_files=[...]``. The recommended default for paper-tagged
    work; failing at decoration time is preferable to silently
    hashing only the path string.
    """


class QprovFileMissingError(FileNotFoundError):
    """Raised at call time when ``require_data_files=True`` and the
    declared data file does not resolve to an existing file on disk.
    Replaces the pre-v0.2 silent fallback to path-string hashing.
    """


class QprovTraversalError(RuntimeError):
    """Raised by ``collect_data_hashes`` when the visitor exceeds its
    depth limit (default 16). Indicates a cyclic or pathologically
    deep input structure that cannot be safely fingerprinted.
    """


class QprovPropertyWarning(UserWarning):
    """Warning category emitted when a warning-severity
    :class:`Property` attached to a ``@tracked`` function fails. The
    qprov row is still written; the warning surfaces the partial
    failure to test harnesses and interactive callers.
    """


# Parameter-name suffixes that strongly suggest a file path argument.
# Used by ``_undeclared_file_like_params`` to detect functions that
# would silently fall back to path-string hashing.
_FILE_LIKE_SUFFIXES = ("_path", "_file", "_csv", "_json", "_pkl")


def _annotation_is_pathlike(annotation: Any) -> bool:
    """Best-effort check for ``Path``/``PathLike``/``str`` annotations.

    Handles both real types and string ``from __future__ import
    annotations`` forms (which are common in this project).
    """
    if annotation is inspect.Parameter.empty:
        return False
    if annotation is str or annotation is Path or annotation is os.PathLike:
        return True
    if isinstance(annotation, type) and issubclass(annotation, (str, Path)):
        return True
    if isinstance(annotation, str):
        s = annotation.split("[", 1)[0].strip()
        return s in {
            "str", "Path", "pathlib.Path", "PathLike", "os.PathLike",
            "Path | None", "str | None",
        } or s.endswith(".Path") or s.endswith(".PathLike")
    return False


def _undeclared_file_like_params(
    sig: inspect.Signature, declared: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the names of parameters that look file-like but were not
    listed in ``data_files=[...]``.

    Heuristic, intentionally conservative: only suffixes in
    ``_FILE_LIKE_SUFFIXES`` plus the annotation-AND-name rule. A bare
    ``csv`` parameter is NOT flagged because false positives on common
    short names would create noise.
    """
    declared_set = set(declared)
    flagged: list[str] = []
    for name, param in sig.parameters.items():
        if name in declared_set:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
            continue
        if any(name.endswith(suffix) for suffix in _FILE_LIKE_SUFFIXES):
            flagged.append(name)
            continue
        if "path" in name.lower() and _annotation_is_pathlike(param.annotation):
            flagged.append(name)
    return tuple(flagged)


def _capture_source(func: Callable[..., Any]) -> str:
    try:
        return inspect.getsource(func)
    except (OSError, TypeError):
        return "<unavailable>"


def _safe_module(func: Callable[..., Any]) -> str | None:
    return getattr(func, "__module__", None)


def _make_id(function_name: str, input_hash: str, code_sha: str | None) -> str:
    return hash_text(f"{function_name}|{input_hash}|{code_sha or ''}")


class _Capture:
    """Context manager that tees stdout/stderr into a buffer.

    Sage's stdout is block-buffered to file; we redirect via contextlib so
    `print()` calls inside the wrapped function are still surfaced to the
    real stdout AND captured into the buffer.
    """
    def __init__(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self._warn_records: list[_warnings.WarningMessage] = []

    def __enter__(self):
        import sys
        self._real_out, self._real_err = sys.stdout, sys.stderr
        self._tee_out = _Tee(self._real_out, self.out)
        self._tee_err = _Tee(self._real_err, self.err)
        sys.stdout, sys.stderr = self._tee_out, self._tee_err
        self._warn_ctx = _warnings.catch_warnings(record=True)
        self._warn_records = self._warn_ctx.__enter__()
        _warnings.simplefilter("always")
        return self

    def __exit__(self, exc_type, exc, tb):
        import sys
        sys.stdout, sys.stderr = self._real_out, self._real_err
        self._warn_ctx.__exit__(exc_type, exc, tb)
        return False

    def warnings_as_dicts(self) -> list[dict]:
        return [
            {
                "category": w.category.__name__,
                "message": str(w.message),
                "filename": w.filename,
                "lineno": w.lineno,
            }
            for w in self._warn_records
        ]


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._streams[0], name)


def tracked(
    _func: Callable[..., Any] | None = None,
    *,
    tags: dict[str, Any] | None = None,
    name: str | None = None,
    data_files: Iterable[str] | None = None,
    require_data_files: bool = False,
    properties: Iterable[Property] | None = None,
):
    """Decorator that records a provenance row for every call.

    Usage:
        @tracked
        def add(x, y): return x + y

        @tracked(tags={"experiment": "G1.2", "constant": "pi"})
        def compute_qreal(N): ...

        @tracked(data_files=["csv_path"])
        def scan(csv_path): ...
        # When called with a path string that resolves to an existing file,
        # the decorator replaces the argument with a canonical_file()
        # descriptor before hashing. Two machines with the same content
        # under the same parameter name collapse to the same id; differing
        # content produces different ids (the correct behavior).

        @tracked(data_files=["csv_path"], require_data_files=True)
        def scan(csv_path): ...
        # Recommended default for paper-tagged work. The decorator raises
        # QprovHashError at decoration time if the function has a
        # file-like parameter not in data_files, and the underlying
        # auto_canonicalize raises QprovFileMissingError at call time if
        # the declared path does not resolve. Replaces the earlier soft
        # path-only fallback.

    The ``require_data_files`` argument has three effects:

    1. At decoration time, parameters whose names end in ``_path``,
       ``_file``, ``_csv``, ``_json``, or ``_pkl`` (or are annotated as
       ``Path``/``PathLike``/``str`` and contain ``"path"`` in the
       name) and are not listed in ``data_files=[...]`` raise
       ``QprovHashError`` instead of emitting a ``QprovHashWarning``.
    2. At call time, listed paths that do not resolve raise
       ``QprovFileMissingError`` instead of falling back to
       path-string hashing.
    3. Whether or not ``require_data_files`` is True, a
       ``QprovHashWarning`` is always emitted at decoration time when
       a file-like parameter is undeclared, so test suites and
       interactive callers see the gap even on the default soft path.

    The ``properties`` argument attaches a list of metamorphic
    invariants. Each
    :class:`qprov.properties.Property` carries a name, a check function
    ``(inputs, outputs) -> PropertyResult``, a description, and a
    severity. After the wrapped function completes and BEFORE the qprov
    row is written, every property is run; results are stored in the
    new ``computations.property_results`` column (JSON). A failed
    error-severity property raises
    :class:`qprov.properties.QprovPropertyError` and BLOCKS the write,
    so the offending computation never lands in the store. A failed
    warning-severity property logs and writes. The check functions are
    free to use Hypothesis internally for property-based random-input
    testing; see :mod:`qprov.properties_qnumbers` for the project-
    specific property declarations.
    """
    data_file_names = tuple(data_files or ())
    property_decls: tuple[Property, ...] = tuple(properties or ())
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(func)
        undeclared = _undeclared_file_like_params(sig, data_file_names)
        warn_message: str | None = None
        if undeclared:
            decl_hint = ", ".join(repr(n) for n in undeclared)
            warn_message = (
                f"qprov @tracked: function {func.__qualname__!r} has "
                f"file-like parameter(s) {list(undeclared)!r} but they "
                f"were not declared in data_files=[...]. Hashes will "
                f"not include file content; this is the pre-v0.2 "
                f"failure mode. Pass data_files=[{decl_hint}] to "
                f"silence (or require_data_files=True to hard-fail)."
            )
            if require_data_files:
                raise QprovHashError(warn_message)
            _warnings.warn(
                warn_message,
                category=QprovHashWarning,
                stacklevel=2,
            )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if warn_message is not None:
                # Re-emit at call time so warning-capture contexts
                # (e.g. pytest's catch_warnings) see the gap even when
                # the decoration happened earlier. Python's default
                # filter dedupes by (message, category, module), so
                # the user-visible noise is at most one line.
                _warnings.warn(
                    warn_message,
                    category=QprovHashWarning,
                    stacklevel=2,
                )
            store = get_store()
            function_name = name or func.__name__
            module = _safe_module(func)
            source = _capture_source(func)

            if data_file_names:
                bound = sig.bind_partial(*args, **kwargs)
                for fname in data_file_names:
                    if fname in bound.arguments:
                        bound.arguments[fname] = auto_canonicalize(
                            bound.arguments[fname],
                            strict=require_data_files,
                        )
                args = tuple(bound.args)
                kwargs = dict(bound.kwargs)

            input_payload = {"args": list(args), "kwargs": dict(kwargs)}
            input_hash = hash_value(input_payload)
            data_hashes = collect_data_hashes(input_payload)
            canonical_data_hash = (
                json.dumps(data_hashes, sort_keys=True, separators=(",", ":"))
                if data_hashes
                else None
            )
            git_info = gitinfo.collect()
            hw = hardware.collect()
            comp_id = _make_id(function_name, input_hash, git_info.sha)
            started = utc_now_iso()

            cap = _Capture()
            t0 = time.perf_counter()
            error_type: str | None = None
            error_message: str | None = None
            error_tb: str | None = None
            result: Any = None
            output_hash: str | None = None
            status = "ok"

            try:
                with cap:
                    result = func(*args, **kwargs)
                output_hash = hash_value(result)
            except BaseException as exc:
                status = "error"
                error_type = type(exc).__name__
                error_message = str(exc)
                error_tb = traceback.format_exc()
                runtime = time.perf_counter() - t0
                ended = utc_now_iso()
                payload = _build_payload(
                    comp_id, function_name, module, source,
                    args, kwargs, None,
                    cap.out.getvalue(), cap.err.getvalue(),
                    cap.warnings_as_dicts(),
                    {"type": error_type, "message": error_message, "traceback": error_tb},
                )
                payload_path, payload_hash = store.write_payload(comp_id, payload)
                comp = _make_computation(
                    comp_id, function_name, module, input_hash, None,
                    git_info, hw, runtime, started, ended,
                    status, error_type, error_message, payload_path,
                    tags or {}, canonical_data_hash,
                    payload_hash=payload_hash,
                )
                store.insert_computation(comp)
                raise

            runtime = time.perf_counter() - t0
            ended = utc_now_iso()

            # Run declared property checks BEFORE writing to the store.
            # An error-severity failure raises QprovPropertyError and
            # blocks the write, surfacing the bug at the moment of the
            # offending computation. Warning-severity failures log and
            # write.
            property_results: dict[str, dict[str, Any]] = {}
            if property_decls:
                bound_inputs = _bind_inputs_dict(sig, args, kwargs)
                outputs_dict = _coerce_to_outputs_dict(result)
                for prop in property_decls:
                    try:
                        pr = prop.check(bound_inputs, outputs_dict)
                    except BaseException as exc:
                        pr = PropertyResult(
                            passed=False,
                            detail=(
                                f"property check raised "
                                f"{type(exc).__name__}: {exc}"
                            ),
                            measured={"exception_type": type(exc).__name__},
                        )
                    property_results[prop.name] = {
                        "passed": bool(pr.passed),
                        "detail": str(pr.detail),
                        "measured": pr.measured,
                        "hypothesis_examples_tried": int(pr.hypothesis_examples_tried),
                        "severity": prop.severity,
                        "description": prop.description,
                    }
                    if not pr.passed and prop.severity == "error":
                        raise QprovPropertyError(
                            property_name=prop.name,
                            description=prop.description,
                            result=pr,
                            function_name=function_name,
                        )
                    if not pr.passed and prop.severity == "warning":
                        _warnings.warn(
                            f"qprov property {prop.name!r} failed "
                            f"(warning-severity, computation will still "
                            f"be written): {pr.detail}",
                            category=QprovPropertyWarning,
                            stacklevel=2,
                        )

            payload = _build_payload(
                comp_id, function_name, module, source,
                args, kwargs, result,
                cap.out.getvalue(), cap.err.getvalue(),
                cap.warnings_as_dicts(),
                None,
                property_results=property_results or None,
            )
            payload_path, payload_hash = store.write_payload(comp_id, payload)
            comp = _make_computation(
                comp_id, function_name, module, input_hash, output_hash,
                git_info, hw, runtime, started, ended,
                status, None, None, payload_path,
                tags or {}, canonical_data_hash,
                payload_hash=payload_hash,
                property_results=property_results or None,
            )
            store.insert_computation(comp)
            return result

        wrapper.__qprov_source__ = source_lazy(func)  # type: ignore[attr-defined]
        wrapper.__qprov_tags__ = dict(tags or {})  # type: ignore[attr-defined]
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return wrapper

    if _func is not None and callable(_func):
        return decorator(_func)
    return decorator


def source_lazy(func: Callable[..., Any]) -> Callable[[], str]:
    def _lazy() -> str:
        return _capture_source(func)
    return _lazy


def _build_payload(
    comp_id: str,
    function_name: str,
    module: str | None,
    source: str,
    args: tuple,
    kwargs: dict,
    result: Any,
    stdout: str,
    stderr: str,
    warnings: list[dict],
    error: dict | None,
    property_results: dict[str, dict[str, Any]] | None = None,
) -> dict:
    payload = {
        "id": comp_id,
        "function_name": function_name,
        "function_module": module,
        "function_source": source,
        "args": list(args),
        "kwargs": dict(kwargs),
        "result": result,
        "stdout": stdout,
        "stderr": stderr,
        "warnings": warnings,
        "error": error,
    }
    if property_results is not None:
        payload["property_results"] = property_results
    return payload


def _make_computation(
    comp_id: str,
    function_name: str,
    module: str | None,
    input_hash: str,
    output_hash: str | None,
    git_info,
    hw,
    runtime: float,
    started: str,
    ended: str,
    status: str,
    error_type: str | None,
    error_message: str | None,
    payload_path,
    tags: dict[str, Any],
    canonical_data_hash: str | None = None,
    *,
    payload_hash: str | None = None,
    property_results: dict[str, dict[str, Any]] | None = None,
) -> Computation:
    from .store import PAYLOAD_HASH_ALGORITHM
    return Computation(
        id=comp_id,
        function_name=function_name,
        function_module=module,
        input_hash=input_hash,
        output_hash=output_hash,
        code_sha=git_info.sha,
        code_dirty=git_info.dirty,
        hostname=hw.hostname,
        cpu_model=hw.cpu_model,
        ram_gb=hw.ram_gb,
        gpu_model=hw.gpu_model,
        python_version=hw.python_version,
        sage_version=hw.sage_version,
        os_info=hw.os_info,
        runtime_seconds=runtime,
        started_at=started,
        ended_at=ended,
        status=status,
        error_type=error_type,
        error_message=error_message,
        payload_path=str(payload_path),
        tags={k: str(v) for k, v in tags.items()},
        canonical_data_hash=canonical_data_hash,
        payload_hash=payload_hash,
        output_hash_algorithm=PAYLOAD_HASH_ALGORITHM if payload_hash else None,
        property_results=property_results,
    )


def _bind_inputs_dict(sig: inspect.Signature, args: tuple, kwargs: dict) -> dict[str, Any]:
    """Bind args/kwargs into a name-keyed dict via the captured signature.

    Falls back to a flat ``{"args": list, "kwargs": dict}`` shape if the
    binding fails (e.g. *args/**kwargs functions where positional
    indices are the only meaningful key).
    """
    try:
        bound = sig.bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except TypeError:
        return {"args": list(args), "kwargs": dict(kwargs)}


def _coerce_to_outputs_dict(result: Any) -> dict[str, Any]:
    """Wrap a non-dict return value in a single-entry dict so property
    checks can access it under the ``"_result"`` key.

    Most q-numbers tracked functions return dicts already; the wrap is
    here so the property surface is uniform.
    """
    if isinstance(result, dict):
        return result
    return {"_result": result}
