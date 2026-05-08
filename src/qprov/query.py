"""Programmatic query API. Thin wrapper over Store with friendlier kwargs."""
from __future__ import annotations

from typing import Any

from .store import Computation, get_store


def get(comp_id: str) -> Computation | None:
    """Look up a single computation by id (full or unique prefix)."""
    return get_store().get_computation(comp_id)


def find(
    *,
    tags: dict[str, Any] | None = None,
    function: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[Computation]:
    """List recent computations matching all given filters."""
    tag_filters: dict[str, str] = {k: str(v) for k, v in (tags or {}).items()}
    return get_store().list_computations(
        limit=limit,
        function_name=function,
        tag_filters=tag_filters or None,
        since=since,
        until=until,
    )
