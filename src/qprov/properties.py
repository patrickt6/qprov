"""Property-based tracking primitives.

Defines :class:`Property` and :class:`PropertyResult` dataclasses, plus the
:class:`QprovPropertyError` exception that ``@tracked`` raises when an
``error``-severity property fails.

A property is a metamorphic invariant the output of a ``@tracked`` function
MUST satisfy. The check function is supplied by the user and receives the
function's bound-input dict and the function's output dict; it returns a
:class:`PropertyResult`. Properties are run after the wrapped function
completes and BEFORE the qprov store row is written: a failed
error-severity property blocks the write, surfacing the bug at the same
moment the offending computation would otherwise have been recorded.

Hypothesis (MacIver and Hatfield-Dodds, JOSS 4:1891, 2019) is the canonical
property-based-testing library for Python. Property check functions are free
to use Hypothesis internally for metamorphic relations across random inputs
(via ``hypothesis.given`` and ``hypothesis.strategies``); this module does
not require it. The property-tests module
(:mod:`qprov.properties_qnumbers`) does use Hypothesis for the MGO recursion
and palindromicity checks.

This module is a primitive layer; the project-specific properties live in
:mod:`qprov.properties_qnumbers`.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable, Literal, Optional


Severity = Literal["error", "warning"]


@dataclasses.dataclass
class PropertyResult:
    """Outcome of running a single property check on a tracked call.

    Attributes:
        passed: ``True`` if the property held on the inputs/outputs given.
        detail: One-line human-readable summary, e.g. "tested 200 random
            rationals; 0 failed".
        measured: Structured payload that the property reports for future
            audit (e.g. the failing examples Hypothesis surfaced, or the
            measured value at q = 1). Stored as JSON in
            ``computations.property_results``.
        hypothesis_examples_tried: How many concrete examples a
            Hypothesis-driven property check ran. Zero for properties
            that are not Hypothesis-driven (deterministic invariants).
    """

    passed: bool
    detail: str
    measured: Any = None
    hypothesis_examples_tried: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "detail": str(self.detail),
            "measured": self.measured,
            "hypothesis_examples_tried": int(self.hypothesis_examples_tried),
        }


@dataclasses.dataclass
class Property:
    """Declaration of a property check attached to a ``@tracked`` function.

    Attributes:
        name: Short identifier, e.g. ``"q_to_1_classical_limit"``. Used as
            the key under which the property's :class:`PropertyResult` is
            stored in ``computations.property_results``.
        check: Callable ``(inputs: dict, outputs: dict) -> PropertyResult``.
            ``inputs`` is the bound argument mapping (``args`` / ``kwargs``
            already merged into a single name-keyed dict by the decorator);
            ``outputs`` is the return value of the wrapped function when it
            is a dict, otherwise ``{"_result": <return value>}``. The check
            should be self-describing (cite the source equation it
            enforces in its docstring).
        description: One-line description for the lint output, e.g. "At
            q=1 the q-deformation reduces to alpha".
        severity: ``"error"`` causes :class:`QprovPropertyError` to be
            raised and the qprov write to be blocked; ``"warning"`` logs
            but writes the row.
        hypothesis_strategy: Optional Hypothesis ``SearchStrategy`` the
            check uses. Stored on the dataclass for documentation and
            future tooling; the check function is responsible for actually
            invoking ``hypothesis.given`` if it wants property-based
            random-input testing.
    """

    name: str
    check: Callable[[dict, dict], PropertyResult]
    description: str
    severity: Severity = "error"
    hypothesis_strategy: Optional[Any] = None


class QprovPropertyError(RuntimeError):
    """Raised when an ``error``-severity property attached to a
    ``@tracked`` function fails.

    The wrapped function ran to completion, but its output violated a
    declared metamorphic invariant. The qprov store row is NOT written;
    the offending computation is surfaced immediately rather than
    archived. Catch this exception only in test code; in production the
    intent is to halt and inspect.

    The exception message includes the property name, description,
    detail, and (when the check used Hypothesis) a minimal failing
    example.
    """

    def __init__(
        self,
        property_name: str,
        description: str,
        result: PropertyResult,
        function_name: str | None = None,
    ):
        self.property_name = property_name
        self.description = description
        self.result = result
        self.function_name = function_name
        msg_lines = [
            f"qprov property {property_name!r} failed",
        ]
        if function_name:
            msg_lines.append(f"  on call to {function_name!r}")
        msg_lines.append(f"  property: {description}")
        msg_lines.append(f"  detail:   {result.detail}")
        if result.hypothesis_examples_tried:
            msg_lines.append(
                f"  hypothesis examples tried: {result.hypothesis_examples_tried}"
            )
        if result.measured is not None:
            measured_summary = str(result.measured)
            if len(measured_summary) > 400:
                measured_summary = measured_summary[:397] + "..."
            msg_lines.append(f"  measured: {measured_summary}")
        super().__init__("\n".join(msg_lines))


__all__ = [
    "Property",
    "PropertyResult",
    "QprovPropertyError",
    "Severity",
]
