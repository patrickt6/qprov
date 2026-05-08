"""Click CLI: qprov init | list | show | find | claim | export-latex | verify | gc | properties."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import click

from . import __version__
from .audit_paper import audit_paper as run_audit_paper, render_report
from .claims import claim as record_claim, export_latex
from .properties import Property, PropertyResult
from .serialize import hash_value
from .store import (
    PayloadTamperedError,
    Store,
    get_store,
    set_store_root,
    utc_now_iso,
)
from .tracking import _make_id
from .verify import verify as run_verify


def _shorten(s: str | None, n: int) -> str:
    if s is None:
        return ""
    return s if len(s) <= n else s[: n - 1] + "."


def _store_for_cwd() -> Store:
    return get_store()


@click.group()
@click.version_option(__version__, prog_name="qprov")
@click.option(
    "--store",
    type=click.Path(),
    default=None,
    help="Override store root (default: nearest .qprov ancestor or ./.qprov).",
)
def main(store: str | None) -> None:
    """Provenance tracker for math research computations."""
    if store:
        set_store_root(store)


@main.command()
@click.option("--path", type=click.Path(), default=".", help="Where to create .qprov (default: cwd).")
def init(path: str) -> None:
    """Create a fresh .qprov/ store in the given directory."""
    target = Path(path).resolve() / ".qprov"
    if target.exists():
        click.echo(f"already exists: {target}")
        return
    set_store_root(str(target))
    Store(target)  # triggers schema creation
    click.echo(f"initialized {target}")


@main.command(name="list")
@click.option("--limit", type=int, default=20)
@click.option("--function", default=None)
@click.option("--since", default=None, help="ISO date/time, e.g. 2024-01-01")
def list_cmd(limit: int, function: str | None, since: str | None) -> None:
    """List recent computations."""
    store = _store_for_cwd()
    comps = store.list_computations(limit=limit, function_name=function, since=since)
    if not comps:
        click.echo("no computations recorded")
        return
    click.echo(f"{'ID':<14}  {'WHEN':<22}  {'FUNCTION':<28}  {'STATUS':<6}  RUNTIME")
    click.echo("-" * 90)
    for c in comps:
        rt = f"{c.runtime_seconds:.3f}s" if c.runtime_seconds is not None else "-"
        click.echo(
            f"{c.id[:12]:<14}  {c.started_at[:19]:<22}  "
            f"{_shorten(c.function_name, 28):<28}  "
            f"{c.status:<6}  {rt}"
        )


@main.command()
@click.argument("comp_id")
@click.option("--payload", is_flag=True, help="Also dump the payload JSON.")
@click.option(
    "--no-verify",
    is_flag=True,
    help="Skip the on-read payload integrity check (forensics on a corrupted store).",
)
def show(comp_id: str, payload: bool, no_verify: bool) -> None:
    """Show a single computation by id or unique prefix."""
    store = _store_for_cwd()
    comp = store.get_computation(comp_id)
    if comp is None:
        click.echo(f"no computation matched {comp_id!r}", err=True)
        sys.exit(1)
    rec = {
        "id": comp.id,
        "function_name": comp.function_name,
        "function_module": comp.function_module,
        "input_hash": comp.input_hash,
        "output_hash": comp.output_hash,
        "output_hash_algorithm": comp.output_hash_algorithm,
        "payload_hash": comp.payload_hash,
        "code_sha": comp.code_sha,
        "code_dirty": comp.code_dirty,
        "hostname": comp.hostname,
        "cpu_model": comp.cpu_model,
        "ram_gb": comp.ram_gb,
        "gpu_model": comp.gpu_model,
        "python_version": comp.python_version,
        "sage_version": comp.sage_version,
        "os_info": comp.os_info,
        "runtime_seconds": comp.runtime_seconds,
        "started_at": comp.started_at,
        "ended_at": comp.ended_at,
        "status": comp.status,
        "error_type": comp.error_type,
        "error_message": comp.error_message,
        "payload_path": comp.payload_path,
        "tags": comp.tags,
        "canonical_data_hash": comp.canonical_data_hash,
    }
    click.echo(json.dumps(rec, indent=2, default=str))
    if payload:
        click.echo("--- payload ---")
        try:
            body = store.read_payload(comp.id, verify_hash=not no_verify)
        except PayloadTamperedError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        click.echo(json.dumps(body, indent=2, default=str))


@main.command()
@click.option("--tag", "tag_filters", multiple=True, help="key=value tag filter (repeatable)")
@click.option("--function", default=None)
@click.option("--since", default=None)
@click.option("--until", default=None)
@click.option("--limit", type=int, default=50)
def find(tag_filters: tuple[str, ...], function: str | None, since: str | None, until: str | None, limit: int) -> None:
    """Search computations by tag/function/time."""
    parsed: dict[str, str] = {}
    for tf in tag_filters:
        if "=" not in tf:
            raise click.BadParameter(f"--tag expects key=value, got {tf!r}")
        k, v = tf.split("=", 1)
        parsed[k] = v
    store = _store_for_cwd()
    comps = store.list_computations(
        limit=limit, function_name=function, tag_filters=parsed or None,
        since=since, until=until,
    )
    if not comps:
        click.echo("no matches")
        return
    for c in comps:
        tag_str = " ".join(f"{k}={v}" for k, v in c.tags.items())
        click.echo(f"{c.id[:12]}  {c.function_name:<28}  {tag_str}")


@main.command(name="claim")
@click.argument("text")
@click.option("--link", "computation_id", default=None, help="Link to computation id.")
@click.option("--value", "value_numeric", type=float, default=None)
@click.option("--notes", default=None)
@click.option("--tag", "tag_specs", multiple=True, help="key=value claim tag (repeatable). Tag `paper=...` enforces a backing computation.")
@click.option("--allow-unbacked", is_flag=True, help="Permit a paper-tagged claim without --link (stages for later back-attach).")
def claim_cmd(
    text: str,
    computation_id: str | None,
    value_numeric: float | None,
    notes: str | None,
    tag_specs: tuple[str, ...],
    allow_unbacked: bool,
) -> None:
    """Register a claim, optionally linked to a computation."""
    store = _store_for_cwd()
    if computation_id:
        comp = store.get_computation(computation_id)
        if comp is None:
            raise click.ClickException(f"no computation matched {computation_id!r}")
        computation_id = comp.id
    parsed_tags: dict[str, str] = {}
    for ts in tag_specs:
        if "=" not in ts:
            raise click.BadParameter(f"--tag expects key=value, got {ts!r}")
        k, v = ts.split("=", 1)
        parsed_tags[k] = v
    try:
        cid = record_claim(
            text,
            computation_id=computation_id,
            value_numeric=value_numeric,
            notes=notes,
            tags=parsed_tags or None,
            allow_unbacked=allow_unbacked,
        )
    except Exception as exc:
        raise click.ClickException(str(exc))
    click.echo(f"claim {cid} recorded")


@main.command(name="export-latex")
@click.option("--since", default=None, help="claims_since (ISO date)")
@click.option("--until", default=None)
@click.option("--computation", default=None, help="restrict to claims linked to this computation id")
@click.option("--tag", "tag_filters", multiple=True, help="restrict to claims whose linked computation has key=value (repeatable)")
@click.option("--drop-unlinked", is_flag=True, help="when --tag is set, also drop claims with no linked computation")
@click.option("--output", type=click.Path(), default=None, help="write to file (default: stdout)")
@click.option("--no-preamble", is_flag=True)
def export_latex_cmd(since: str | None, until: str | None, computation: str | None,
                     tag_filters: tuple[str, ...], drop_unlinked: bool,
                     output: str | None, no_preamble: bool) -> None:
    """Render claims to LaTeX with `\\fact{}` macros."""
    parsed: dict[str, str] = {}
    for tf in tag_filters:
        if "=" not in tf:
            raise click.BadParameter(f"--tag expects key=value, got {tf!r}")
        k, v = tf.split("=", 1)
        parsed[k] = v
    text = export_latex(
        claims_since=since, claims_until=until, computation_id=computation,
        tag_filters=parsed or None,
        include_unlinked=not drop_unlinked,
        output=output, include_preamble=not no_preamble,
    )
    if output is None:
        click.echo(text)
    else:
        click.echo(f"wrote {output}")


@main.command()
@click.argument("comp_id")
def verify(comp_id: str) -> None:
    """Re-run a recorded computation and check the output hash."""
    result = run_verify(comp_id)
    if result.ok:
        click.echo(f"OK  {result.computation_id}  hash={result.actual_hash}")
        return
    click.echo(f"FAIL  {result.computation_id}", err=True)
    click.echo(f"  expected: {result.expected_hash}", err=True)
    click.echo(f"  actual:   {result.actual_hash}", err=True)
    if result.diff_index is not None:
        click.echo(f"  first divergence at canonical-JSON byte {result.diff_index}", err=True)
    click.echo(f"  {result.message}", err=True)
    sys.exit(2)


@main.command()
@click.option(
    "--rerun-properties",
    is_flag=True,
    help=(
        "Re-run all declared properties against stored payloads via the "
        "qnumbers properties registry. Without this flag, lint checks "
        "the stored property_results JSON only."
    ),
)
def lint(rerun_properties: bool) -> None:
    """Find claims and computations that violate v0.2/v0.3/v0.4 integrity rules.

    Checks (failing, exit 1):
      * ORPHAN: paper-tagged claim with NULL computation_id (would render
        as \\provid{None} in LaTeX export).
      * DANGLING: paper-tagged claim whose linked computation no longer
        exists.
      * TAMPERED: payload file on disk does not match the
        payload_hash recorded at write time. Either a hand-edit or a
        corrupted store.
      * ID_DRIFT: the computation's recomputed input_hash (rehash from
        the payload's args/kwargs or inputs) does not match the stored
        input_hash, OR the recomputed id does not match the row's id.
        Catches a directly-edited input column or a payload swap.
      * PROPFAIL: a stored property result has ``passed: false`` AND
        severity ``error``. Indicates a metamorphic invariant violation
        on a backing computation; surface immediately.

    Advisory (not failing, exit 0):
      * NOHASH: paper-backed computation with no canonical_data_hash.
        Legacy path-only hashing; re-run through @tracked(data_files=...)
        to upgrade.
      * PROPMISSING: paper-backed computation has no property_results
        (legacy v0.3 record predating the property-based tracking
        layer). Re-run through @tracked(properties=[...]) to upgrade.
      * PROPWARN: a stored property result has ``passed: false`` AND
        severity ``warning``. Logged but not failing.

    Database-level errors (exit 2):
      * Connection failure or schema_version mismatch.
    """
    try:
        store = _store_for_cwd()
    except sqlite3.DatabaseError as exc:
        click.echo(f"DB ERROR  cannot open store: {exc}", err=True)
        sys.exit(2)

    issues = 0
    advisories = 0
    paper_claims = store.list_claims_by_tag("paper")

    # Track which computations are linked from paper claims so we don't
    # rehash unrelated payloads. Payload integrity is only enforced for
    # the paper-backing rows; lint a wider net via `qprov lint --all`
    # in a future iteration if needed.
    backing_ids: set[str] = set()

    for c in paper_claims:
        paper = c.tags.get("paper", "?")
        if c.computation_id is None:
            if c.tags.get("unbacked") == "true":
                click.echo(
                    f"unbacked  claim {c.id[:12]}  paper={paper}  intentionally has no computation_id (allow_unbacked=True)",
                )
                continue
            click.echo(
                f"ORPHAN    claim {c.id[:12]}  paper={paper}  has no computation_id",
                err=True,
            )
            issues += 1
            continue
        comp = store.get_computation(c.computation_id)
        if comp is None:
            click.echo(
                f"DANGLING  claim {c.id[:12]}  paper={paper}  links to missing computation {c.computation_id[:12]}",
                err=True,
            )
            issues += 1
            continue
        backing_ids.add(comp.id)
        if comp.canonical_data_hash is None:
            click.echo(
                f"NOHASH    claim {c.id[:12]}  paper={paper}  computation {comp.id[:12]} has no canonical_data_hash (advisory)",
                err=True,
            )
            advisories += 1

    for comp_id in sorted(backing_ids):
        comp = store.get_computation(comp_id)
        if comp is None:
            continue
        tamper = _check_payload_tamper(store, comp)
        if tamper is not None:
            click.echo(
                f"TAMPERED  computation {comp.id[:12]}  {tamper}",
                err=True,
            )
            issues += 1
            # Skip ID_DRIFT for tampered payloads - reading the payload
            # to recompute is unsafe; the tampered hash already tells us
            # the bytes are not what they were.
            continue
        drift = _check_id_drift(store, comp)
        if drift is not None:
            click.echo(
                f"ID_DRIFT  computation {comp.id[:12]}  {drift}",
                err=True,
            )
            issues += 1
        # Property-results check: PROPMISSING (advisory) and PROPFAIL
        # (failing). Re-runs are off by default; --rerun-properties
        # walks the qnumbers properties registry and re-evaluates each
        # declared property against the stored payload's outputs.
        if comp.property_results is None:
            click.echo(
                f"PROPMISSING  computation {comp.id[:12]}  {comp.function_name}  "
                f"no property_results (legacy v0.3 record; advisory)",
                err=True,
            )
            advisories += 1
        else:
            for prop_name, pr in comp.property_results.items():
                if pr.get("passed"):
                    continue
                severity = pr.get("severity", "error")
                if severity == "warning":
                    click.echo(
                        f"PROPWARN  computation {comp.id[:12]}  "
                        f"{prop_name}: {pr.get('detail', '')} (advisory)",
                        err=True,
                    )
                    advisories += 1
                else:
                    click.echo(
                        f"PROPFAIL  computation {comp.id[:12]}  "
                        f"{prop_name}: {pr.get('detail', '')}",
                        err=True,
                    )
                    issues += 1
        if rerun_properties:
            rerun_outcomes = _rerun_properties_against_payload(store, comp)
            for prop_name, pr_dict in rerun_outcomes.items():
                if pr_dict.get("passed"):
                    continue
                if pr_dict.get("severity", "error") == "warning":
                    click.echo(
                        f"PROPWARN  computation {comp.id[:12]}  rerun "
                        f"{prop_name}: {pr_dict.get('detail', '')} (advisory)",
                        err=True,
                    )
                    advisories += 1
                else:
                    click.echo(
                        f"PROPFAIL  computation {comp.id[:12]}  rerun "
                        f"{prop_name}: {pr_dict.get('detail', '')}",
                        err=True,
                    )
                    issues += 1

    if issues == 0 and advisories == 0:
        click.echo("clean")
        return
    if issues == 0:
        click.echo(f"{advisories} advisory issue(s); no failures")
        return
    click.echo(
        f"{issues} failing issue(s), {advisories} "
        f"advisor{'y' if advisories == 1 else 'ies'}",
        err=True,
    )
    sys.exit(1)


def _check_payload_tamper(store: Store, comp) -> str | None:
    if comp.payload_hash is None:
        # No recorded hash - cannot verify. Treat as advisory-handled
        # via NOHASH path; do not double-count as TAMPERED.
        return None
    try:
        store.read_payload(comp.id, verify_hash=True)
    except FileNotFoundError:
        return f"payload file missing at {comp.payload_path}"
    except PayloadTamperedError as exc:
        # Extract the two hashes for a one-line summary; full message
        # is in the exception body and emitted only by `qprov show`.
        return str(exc).splitlines()[0]
    return None


_QNUMBERS_REGISTRY: dict[str, list[Property]] | None = None


def _qnumbers_property_registry() -> dict[str, list[Property]]:
    """Lazy import of the project-specific property bundles. Returns
    ``{function_name: [Property, ...]}``.

    Imported lazily so qprov stays usable in environments without the
    q-numbers helper imports loaded yet.
    """
    global _QNUMBERS_REGISTRY
    if _QNUMBERS_REGISTRY is not None:
        return _QNUMBERS_REGISTRY
    try:
        from .properties_qnumbers import (
            kernel_search_properties,
            q_real_truncated_properties,
            gap_theorem_scan_properties,
        )
    except ImportError:  # pragma: no cover
        _QNUMBERS_REGISTRY = {}
        return _QNUMBERS_REGISTRY
    _QNUMBERS_REGISTRY = {
        # The keys here align with both the live ``@tracked`` function
        # names and the legacy external-registration function names so
        # retroactive rerun works on backfilled rows.
        "q_real_truncated": q_real_truncated_properties(),
        "kernel_search": kernel_search_properties(),
        "kernel_search_part1_v1": kernel_search_properties(),
        "kernel_search_part2_v1": kernel_search_properties(),
        "kernel_search_sympy_v1": kernel_search_properties(),
        "scan_gap_theorem": gap_theorem_scan_properties(),
    }
    return _QNUMBERS_REGISTRY


def _rerun_properties_against_payload(
    store: Store, comp,
) -> dict[str, dict[str, object]]:
    """Re-run the project-specific property declarations for ``comp``
    against the stored payload's outputs. Returns ``{name: <dict>}``.

    Uses the qnumbers property registry to find which properties to
    run by ``comp.function_name``. Functions absent from the registry
    return an empty dict.
    """
    registry = _qnumbers_property_registry()
    props = registry.get(comp.function_name, [])
    if not props:
        return {}
    try:
        payload = store.read_payload(comp.id, verify_hash=False)
    except FileNotFoundError:
        return {}
    # External / registered-after-the-fact rows store inputs/outputs;
    # tracked rows store args/kwargs/result.
    if payload.get("external"):
        inputs = dict(payload.get("inputs") or {})
        outputs_raw = payload.get("outputs")
    else:
        inputs = dict(payload.get("kwargs") or {})
        outputs_raw = payload.get("result")
    outputs = outputs_raw if isinstance(outputs_raw, dict) else {"_result": outputs_raw}
    out: dict[str, dict[str, object]] = {}
    for prop in props:
        try:
            pr = prop.check(inputs, outputs)
        except BaseException as exc:
            pr = PropertyResult(
                passed=False,
                detail=f"property check raised {type(exc).__name__}: {exc}",
                measured={"exception_type": type(exc).__name__},
            )
        out[prop.name] = {
            "passed": bool(pr.passed),
            "detail": str(pr.detail),
            "measured": pr.measured,
            "hypothesis_examples_tried": int(pr.hypothesis_examples_tried),
            "severity": prop.severity,
            "description": prop.description,
        }
    return out


def _check_id_drift(store: Store, comp) -> str | None:
    if comp.code_sha is None and comp.function_name is None:
        return None
    try:
        payload = store.read_payload(comp.id, verify_hash=False)
    except FileNotFoundError:
        return None
    if payload.get("external") is True:
        recomputed_input_hash = hash_value(payload.get("inputs", {}))
    elif "args" in payload or "kwargs" in payload:
        recomputed_input_hash = hash_value({
            "args": payload.get("args", []),
            "kwargs": payload.get("kwargs", {}),
        })
    else:
        # Payload shape unknown; cannot rehash safely.
        return None
    if recomputed_input_hash != comp.input_hash:
        return (
            f"recomputed input_hash {recomputed_input_hash[:12]} != "
            f"stored {comp.input_hash[:12]}"
        )
    recomputed_id = _make_id(comp.function_name, recomputed_input_hash, comp.code_sha)
    if recomputed_id != comp.id:
        return (
            f"recomputed id {recomputed_id[:12]} != stored {comp.id[:12]}"
        )
    return None


@main.command(name="audit-paper")
@click.argument("tex", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--output-format",
    type=click.Choice(["text", "json", "markdown"]),
    default="text",
    help="Report shape. `markdown` is suitable for dropping into reports/.",
)
@click.option(
    "--fail-on",
    "fail_on",
    multiple=True,
    default=("DRIFT", "MISSING", "ORPHAN"),
    type=click.Choice(["MATCH", "DRIFT", "MISSING", "ORPHAN"]),
    help="Statuses that cause a non-zero exit. Repeatable. Default: DRIFT, MISSING, ORPHAN.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(),
    default=None,
    help="Path to qprov.sqlite (or its containing .qprov dir). Defaults to the resolved store.",
)
def audit_paper_cmd(
    tex: Path,
    output_format: str,
    fail_on: tuple[str, ...],
    db_path: str | None,
) -> None:
    """Audit a paper's `\\provid{...}` references against the qprov DB.

    For each `\\provid{...}` reference in the .tex source, fetches the
    linked claim and computation from the qprov store, extracts the
    surrounding paragraph's numeric assertions, and reports MATCH /
    DRIFT / MISSING / ORPHAN. Catches the class of paper-vs-record drift
    where a manuscript number no longer matches the recorded computation.
    """
    if db_path is not None:
        # Accept either the .qprov dir or the sqlite file path; the
        # store root is the *directory* in both cases.
        p = Path(db_path)
        root = p.parent if p.is_file() else p
        set_store_root(str(root))
    store = _store_for_cwd()
    report = run_audit_paper(tex, store)
    click.echo(render_report(report, output_format=output_format))
    fail_set = set(fail_on)
    if any(e.status in fail_set for e in report.entries):
        sys.exit(1)


@main.command(name="properties")
@click.option("--list", "list_mode", is_flag=True, help="List property names recorded in the store.")
@click.option("--check", "check_mode", is_flag=True, help="Re-run properties against stored payloads.")
@click.option("--paper", "paper_slug", default=None, help="Restrict --check to claims tagged with this paper slug.")
@click.option("--comp-id", "comp_id", default=None, help="Re-run only the specified computation id (or unique prefix).")
@click.option("--hypothesis-iterations", "hypothesis_iterations", type=int, default=None,
              help="Suggested upper bound on Hypothesis examples per check; passed through to checks that read it from os.environ.")
def properties_cmd(
    list_mode: bool,
    check_mode: bool,
    paper_slug: str | None,
    comp_id: str | None,
    hypothesis_iterations: int | None,
) -> None:
    """Inspect or re-run property-based tracking results.

    --list             enumerate every property name recorded in the store.
    --check            re-run declared properties against stored payloads.
    --paper <slug>     restrict --check to backing computations of paper-tagged claims for <slug>.
    --comp-id <id>     re-run only the specified computation id (or unique prefix).
    --hypothesis-iterations N
                       suggested upper bound on Hypothesis examples per
                       Hypothesis-driven check; exported as
                       ``QPROV_HYPOTHESIS_ITERATIONS`` so check functions
                       can read it from the environment.
    """
    if not (list_mode or check_mode):
        raise click.UsageError("pass --list or --check (or both).")

    store = _store_for_cwd()

    if hypothesis_iterations is not None:
        import os as _os
        _os.environ["QPROV_HYPOTHESIS_ITERATIONS"] = str(hypothesis_iterations)

    if list_mode:
        all_names: dict[str, int] = {}
        for c in store.iter_all_computations():
            if c.property_results:
                for name in c.property_results:
                    all_names[name] = all_names.get(name, 0) + 1
        if not all_names:
            click.echo("(no property_results recorded yet)")
        else:
            click.echo(f"{'PROPERTY':<40}  COUNT")
            click.echo("-" * 50)
            for name, count in sorted(all_names.items()):
                click.echo(f"{name:<40}  {count}")
        if not check_mode:
            return

    targets: list[object] = []
    if comp_id:
        comp = store.get_computation(comp_id)
        if comp is None:
            raise click.ClickException(f"no computation matched {comp_id!r}")
        targets = [comp]
    elif paper_slug:
        backing_ids = set()
        for cl in store.list_claims_by_tag("paper", paper_slug):
            if cl.computation_id:
                backing_ids.add(cl.computation_id)
        for cid in sorted(backing_ids):
            comp = store.get_computation(cid)
            if comp is not None:
                targets.append(comp)
    else:
        # --check without scoping: walk every paper-tagged computation
        # in the store. This is the broadest sweep; for narrower work
        # use --paper or --comp-id.
        backing_ids = set()
        for cl in store.list_claims_by_tag("paper"):
            if cl.computation_id:
                backing_ids.add(cl.computation_id)
        for cid in sorted(backing_ids):
            comp = store.get_computation(cid)
            if comp is not None:
                targets.append(comp)

    if not targets:
        click.echo("no target computations found")
        return

    failures = 0
    for comp in targets:
        outcomes = _rerun_properties_against_payload(store, comp)
        if not outcomes:
            # Fall back to the stored property_results from the original
            # tracked call. This is the right behavior when the function
            # is not registered in the qnumbers property registry: we
            # still surface what was recorded at write time, marked as
            # ``stored`` instead of ``rerun``.
            stored = comp.property_results or {}
            if not stored:
                click.echo(
                    f"{comp.id[:12]}  {comp.function_name:<28}  "
                    f"(no registered properties for this function and no "
                    f"stored property_results)"
                )
                continue
            for name, pr in stored.items():
                tag = "PASS" if pr.get("passed") else (
                    "WARN" if pr.get("severity") == "warning" else "FAIL"
                )
                line = (
                    f"{comp.id[:12]}  {comp.function_name:<28}  "
                    f"{tag:<4}  {name}  (stored)  -  {pr.get('detail', '')}"
                )
                click.echo(line)
                if not pr.get("passed") and pr.get("severity") != "warning":
                    failures += 1
            continue
        for name, pr in outcomes.items():
            tag = "PASS" if pr["passed"] else (
                "WARN" if pr.get("severity") == "warning" else "FAIL"
            )
            line = (
                f"{comp.id[:12]}  {comp.function_name:<28}  "
                f"{tag:<4}  {name}  (rerun)  -  {pr['detail']}"
            )
            click.echo(line)
            if not pr["passed"] and pr.get("severity") != "warning":
                failures += 1

    if failures:
        click.echo(f"\n{failures} property failure(s)", err=True)
        sys.exit(1)


@main.command()
@click.option("--yes", is_flag=True, help="Skip confirmation.")
@click.option("--dry-run", is_flag=True)
def gc(yes: bool, dry_run: bool) -> None:
    """Delete computations not referenced by any claim."""
    store = _store_for_cwd()
    referenced = store.referenced_computation_ids()
    candidates = [c for c in store.iter_all_computations() if c.id not in referenced]
    if not candidates:
        click.echo("nothing to gc")
        return
    click.echo(f"{len(candidates)} computations are not referenced by any claim:")
    for c in candidates[:50]:
        click.echo(f"  {c.id[:12]}  {c.function_name}  {c.started_at[:19]}")
    if len(candidates) > 50:
        click.echo(f"  ... and {len(candidates) - 50} more")
    if dry_run:
        click.echo("(dry-run; nothing deleted)")
        return
    if not yes:
        if not click.confirm("delete all of the above?"):
            click.echo("aborted")
            return
    deleted = 0
    for c in candidates:
        if store.delete_computation(c.id):
            deleted += 1
    click.echo(f"deleted {deleted} computations")


if __name__ == "__main__":
    main()
