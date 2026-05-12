"""Audit a paper's ``\\provid{...}`` references against the qprov store.

This guards against paper-vs-record drift that plain proofreading misses:
a claim's prose can state a stronger result than the linked computation
actually verified, or a number in the text can drift by a digit from the
value in the recorded payload. Both are visible to a regex-level check that
walks each ``\\provid{...}`` reference and compares the surrounding
paragraph's numeric assertions to the linked computation's outputs.

``audit_paper`` is that check. It walks a LaTeX source, extracts each
``\\provid{...}`` reference and the paragraph it sits in, looks up the
matching claim or computation in the qprov store, and emits an
:class:`AuditEntry` with one of four statuses:

- ``MATCH``: every number in the paragraph appears (within tolerance)
  somewhere in the linked computation's payload.
- ``DRIFT``: at least one paragraph number has no match in the payload.
  The user has to decide whether the paper or the database is wrong.
- ``MISSING``: the provid does not resolve to any claim or computation.
- ``ORPHAN``: the provid resolves to a claim with ``computation_id IS
  NULL`` (a paper-tagged unbacked claim, or a stale row from before the
  paper-tag gate landed).

The matcher is intentionally conservative: regex + structured payload
walk, no natural-language understanding. A paragraph may legitimately
contain numbers unrelated to the cited claim (citation keys, equation
labels, the year a paper was published), so a stray Mismatch is a
warning, not an error. A user inspecting the report decides whether each
DRIFT is a real divergence or a benign side-note.
"""
from __future__ import annotations

import dataclasses
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from .store import Claim, Computation, Store


PROVID_RE = re.compile(r"\\provid\{([^}]+)\}")

# Number regexes. Float runs first; the int regex finds plenty of false
# positives that lie inside a float span, which `_overlaps` strips. Avoid
# matching identifier-internal digits (e.g. the `5` in `q^5`); a leading
# alphanumeric character disqualifies. End-of-sentence periods are
# allowed after an integer (so "is 0." extracts 0), but `\.\d` after an
# int is suppressed to keep `0.531` from also extracting "531" before
# the float pass.
_FRACTION_RE = re.compile(r"(?<!\w)(\d+)\s*/\s*(\d+)(?!\w)")
_FLOAT_RE = re.compile(r"(?<!\w)-?\d+\.\d+(?!\w)")
_INT_RE = re.compile(r"(?<!\w)-?\d+(?!\w)")

# Citation- and label-like commands whose contents are NEVER counted as
# extracted numbers (a `\ref{eq:5}` or `\cite{MGO-reals}` carries digits
# that are not factual claims).
_SKIP_COMMAND_RE = re.compile(
    r"\\(?:ref|cref|Cref|eqref|pageref|cite[a-z]*|label|provid|footnote"
    r"|footnotemark|protect|texttt|emph|textbf|textit)\s*\{[^}]*\}"
)

# Section-style numbers like "Section 5.2" or "Chapter 3" - the digits
# refer to document structure, not claim content. We strip these too.
_SECTION_REF_RE = re.compile(
    r"(?:Section|section|Sec\.|sec\.|Chapter|chapter|Chap\.|chap\.|Appendix"
    r"|appendix|App\.|app\.|Figure|figure|Fig\.|fig\.|Table|table|Tab\.|tab\."
    r"|Theorem|theorem|Thm\.|thm\.|Lemma|lemma|Lem\.|lem\.|Proposition"
    r"|proposition|Prop\.|prop\.|Definition|definition|Def\.|def\.|Corollary"
    r"|corollary|Cor\.|cor\.|Remark|remark|Rem\.|rem\.|Equation|equation"
    r"|Eq\.|eq\.)\s*(?:~|\\ref\{[^}]*\}|\d+(?:\.\d+)*)"
)

DEFAULT_FLOAT_TOLERANCE = 1e-6


@dataclasses.dataclass
class ExtractedNumber:
    """A number lifted out of a paragraph of LaTeX prose.

    ``raw_text`` is the literal substring so the report can echo what
    the paragraph said. ``value`` is the numeric reading we used for the
    payload search.
    """

    value: float
    raw_text: str
    paragraph_index: int  # character offset in paragraph
    kind: Literal["int", "float", "fraction"] = "int"


@dataclasses.dataclass
class Mismatch:
    """A number from the paragraph that did not show up in the linked
    computation's payload.

    ``field`` is best-effort: the audit walks the payload as nested
    structure and emits the deepest dotted key it could associate the
    miss with. For free-floating numbers (no field name in scope) the
    field is ``"paragraph"``.
    """

    field: str
    paper_value: float
    db_value: float | None
    tolerance: float
    severity: Literal["error", "warning"] = "warning"


@dataclasses.dataclass
class AuditEntry:
    provid: str
    line: int
    paragraph: str
    status: Literal["MATCH", "DRIFT", "MISSING", "ORPHAN"]
    detail: str
    claim_text: Optional[str] = None
    computation_outputs: Optional[dict] = None
    extracted_numbers: list[ExtractedNumber] = dataclasses.field(default_factory=list)
    mismatches: list[Mismatch] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class AuditReport:
    tex_path: Path
    entries: list[AuditEntry]
    summary: dict[str, int]

    @classmethod
    def from_entries(cls, tex_path: Path, entries: list[AuditEntry]) -> "AuditReport":
        summary = {"MATCH": 0, "DRIFT": 0, "MISSING": 0, "ORPHAN": 0}
        for e in entries:
            summary[e.status] = summary.get(e.status, 0) + 1
        return cls(tex_path=tex_path, entries=entries, summary=summary)


def _unescape_provid(raw: str) -> str:
    """LaTeX often writes `polynomial\\_emptiness\\_cbrt2` for an
    underscore-bearing identifier. Strip the backslash before the underscore
    so the lookup matches the stored id. Also tolerate the rarer `\\#`,
    `\\&`, `\\%` and the no-op `\\\\`.
    """
    out = raw
    for esc, plain in (("\\_", "_"), ("\\#", "#"), ("\\&", "&"), ("\\%", "%"), ("\\\\", "\\")):
        out = out.replace(esc, plain)
    return out.strip()


def extract_provid_references(tex_text: str) -> list[tuple[str, int, str]]:
    """Return ``(provid_id, line_number, paragraph)`` triples in document order.

    A paragraph is the chunk of text bounded by blank lines (one or more
    consecutive newlines surrounded by whitespace) around the provid
    reference. Two ``\\provid{...}`` references in the same paragraph each
    produce their own entry, both carrying the full paragraph text.

    The ``provid_id`` is unescaped (``\\_`` -> ``_``) so the result can be
    passed straight to ``Store.get_claim`` / ``Store.get_computation``.
    """
    paragraphs = _split_paragraphs(tex_text)
    refs: list[tuple[str, int, str]] = []
    for para_text, start_line in paragraphs:
        for match in PROVID_RE.finditer(para_text):
            raw = match.group(1)
            pid = _unescape_provid(raw)
            # Skip the macro definition itself (e.g. `\\provid{#1}`).
            if pid.startswith("#"):
                continue
            # Skip the documented "no-provenance" placeholder.
            if pid == "no-provenance":
                continue
            # Locate the line by counting newlines from the paragraph start
            # to the match position.
            line = start_line + para_text.count("\n", 0, match.start())
            refs.append((pid, line, para_text.strip()))
    return refs


def _split_paragraphs(tex_text: str) -> list[tuple[str, int]]:
    """Split into ``(text, start_line)`` paragraphs. Lines are 1-based."""
    lines = tex_text.splitlines(keepends=True)
    paragraphs: list[tuple[str, int]] = []
    buf: list[str] = []
    start_line = 1
    current_line = 1
    for ln in lines:
        if ln.strip() == "":
            if buf:
                paragraphs.append(("".join(buf), start_line))
                buf = []
            current_line += 1
            start_line = current_line
            continue
        if not buf:
            start_line = current_line
        buf.append(ln)
        current_line += 1
    if buf:
        paragraphs.append(("".join(buf), start_line))
    return paragraphs


def _scrub_paragraph_for_numbers(paragraph: str) -> str:
    """Remove citation and label commands so their digit-bearing keys do
    not become spurious ExtractedNumbers. Replace with spaces (not empty)
    so adjacent words don't fuse.
    """
    stripped = _SKIP_COMMAND_RE.sub(lambda m: " " * len(m.group(0)), paragraph)
    stripped = _SECTION_REF_RE.sub(lambda m: " " * len(m.group(0)), stripped)
    return stripped


def extract_numbers_from_paragraph(paragraph: str) -> list[ExtractedNumber]:
    """Pull every numeric literal that looks like a factual assertion.

    Conservative: prefers false negatives over false positives. Numbers
    inside ``\\ref{...}``, ``\\cite{...}``, ``\\label{...}``, section/figure
    references, and the ``\\provid{...}`` macro itself are skipped.
    Floats and fractions take precedence over the integers that compose
    them.
    """
    scrubbed = _scrub_paragraph_for_numbers(paragraph)
    spans: list[tuple[int, int, ExtractedNumber]] = []

    for m in _FRACTION_RE.finditer(scrubbed):
        num = int(m.group(1))
        den = int(m.group(2))
        if den == 0:
            continue
        spans.append((m.start(), m.end(), ExtractedNumber(
            value=num / den,
            raw_text=m.group(0),
            paragraph_index=m.start(),
            kind="fraction",
        )))

    for m in _FLOAT_RE.finditer(scrubbed):
        if _overlaps(spans, m.start(), m.end()):
            continue
        spans.append((m.start(), m.end(), ExtractedNumber(
            value=float(m.group(0)),
            raw_text=m.group(0),
            paragraph_index=m.start(),
            kind="float",
        )))

    for m in _INT_RE.finditer(scrubbed):
        if _overlaps(spans, m.start(), m.end()):
            continue
        spans.append((m.start(), m.end(), ExtractedNumber(
            value=float(int(m.group(0))),
            raw_text=m.group(0),
            paragraph_index=m.start(),
            kind="int",
        )))

    spans.sort(key=lambda t: t[0])
    return [s[2] for s in spans]


def _overlaps(spans: list[tuple[int, int, Any]], start: int, end: int) -> bool:
    for s, e, _ in spans:
        if not (end <= s or start >= e):
            return True
    return False


def _payload_numbers(payload: Any, prefix: str = "") -> list[tuple[str, float]]:
    """Walk a payload and collect every numeric leaf as (dotted_key, value).

    Only int/float leaves count. Strings that happen to parse as numbers
    are not included (a CSV name like ``qreal_pi_4999.csv`` carries the
    integer 4999 but as a path component, not a recorded result).
    """
    found: list[tuple[str, float]] = []
    _walk_payload(payload, prefix, found, depth=0)
    return found


def _walk_payload(node: Any, prefix: str, out: list[tuple[str, float]], depth: int) -> None:
    if depth > 32:
        return
    if isinstance(node, bool):
        # bool is a subclass of int in Python. Skip booleans - "True" / "False"
        # are not numeric assertions even though they cast to 1 / 0.
        return
    if isinstance(node, (int, float)):
        if isinstance(node, float) and (math.isnan(node) or math.isinf(node)):
            return
        out.append((prefix or "root", float(node)))
        return
    if isinstance(node, dict):
        for k, v in node.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            _walk_payload(v, key, out, depth + 1)
        return
    if isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            key = f"{prefix}[{i}]" if prefix else f"[{i}]"
            _walk_payload(v, key, out, depth + 1)
        return
    # strings, None, etc are not numeric


def _claim_text_numbers(text: str) -> list[float]:
    """Pull numbers from the claim's stored text so a paragraph number
    that came from the claim itself (e.g. the bidegree it asserts) is
    treated as a known match. The claim text is part of the truth
    surface.
    """
    nums = extract_numbers_from_paragraph(text)
    return [n.value for n in nums]


def _match_one(
    needle: ExtractedNumber, payload_nums: list[tuple[str, float]],
    claim_nums: list[float], tolerance_float: float,
) -> Optional[tuple[str, float]]:
    """Find a payload value matching ``needle``. Integers and fractions
    match exactly; floats match within ``tolerance_float``. Returns the
    ``(field, value)`` pair on success, else None.
    """
    if needle.kind == "int" or needle.kind == "fraction":
        target = needle.value
        for field, val in payload_nums:
            if isinstance(val, float) and val.is_integer() and needle.kind == "int":
                if int(val) == int(target):
                    return field, val
            elif val == target:
                return field, val
        for cn in claim_nums:
            if cn == target:
                return "claim_text", cn
        return None
    # float: tolerant compare
    for field, val in payload_nums:
        if abs(val - needle.value) <= tolerance_float:
            return field, val
    for cn in claim_nums:
        if abs(cn - needle.value) <= tolerance_float:
            return "claim_text", cn
    return None


def audit_paper(
    tex_path: Path,
    db: Store,
    *,
    float_tolerance: float = DEFAULT_FLOAT_TOLERANCE,
) -> AuditReport:
    """Walk every ``\\provid{...}`` reference in ``tex_path`` and audit it
    against ``db``.

    See the module docstring for the four possible statuses.
    """
    text = Path(tex_path).read_text(encoding="utf-8")
    refs = extract_provid_references(text)
    entries: list[AuditEntry] = []
    for provid, line, paragraph in refs:
        entry = _audit_one(provid, line, paragraph, db, float_tolerance)
        entries.append(entry)
    return AuditReport.from_entries(Path(tex_path), entries)


def _audit_one(
    provid: str,
    line: int,
    paragraph: str,
    db: Store,
    float_tolerance: float,
) -> AuditEntry:
    claim, comp = _resolve_provid(provid, db)
    if claim is None and comp is None:
        return AuditEntry(
            provid=provid,
            line=line,
            paragraph=paragraph,
            status="MISSING",
            detail=(
                f"provid {provid!r} did not match any claim id or "
                f"computation id in the store"
            ),
        )
    if claim is not None and comp is None:
        # Claim found but its computation_id is NULL (paper-tag-gate
        # `unbacked` row) or links to a missing computation.
        if claim.computation_id is None:
            return AuditEntry(
                provid=provid,
                line=line,
                paragraph=paragraph,
                status="ORPHAN",
                detail=(
                    f"claim {provid!r} has no computation_id "
                    f"(unbacked={claim.unbacked}); the paragraph's numeric "
                    f"assertions cannot be checked against a payload"
                ),
                claim_text=claim.text,
            )
        return AuditEntry(
            provid=provid,
            line=line,
            paragraph=paragraph,
            status="ORPHAN",
            detail=(
                f"claim {provid!r} links to computation "
                f"{claim.computation_id!r} which was not found in the store"
            ),
            claim_text=claim.text,
        )
    # comp is present (with or without an attached claim).
    payload: dict
    try:
        payload = db.read_payload(comp.id)
    except FileNotFoundError as exc:
        return AuditEntry(
            provid=provid,
            line=line,
            paragraph=paragraph,
            status="ORPHAN",
            detail=(
                f"computation {comp.id[:12]!r} resolved but payload file "
                f"is missing on disk: {exc}"
            ),
            claim_text=claim.text if claim is not None else None,
        )
    except Exception as exc:  # PayloadTamperedError and friends
        return AuditEntry(
            provid=provid,
            line=line,
            paragraph=paragraph,
            status="ORPHAN",
            detail=f"computation {comp.id[:12]!r} payload could not be read: {exc}",
            claim_text=claim.text if claim is not None else None,
        )
    return _diff_paragraph_against_payload(
        provid=provid,
        line=line,
        paragraph=paragraph,
        claim=claim,
        comp=comp,
        payload=payload,
        float_tolerance=float_tolerance,
    )


def _resolve_provid(
    provid: str, db: Store
) -> tuple[Optional[Claim], Optional[Computation]]:
    """Try to resolve ``provid`` as a claim id first, then as a computation
    id. The claim/computation pair returned is (None if not found).

    The auto-generated `claims.tex` from `qprov export-latex` writes
    *computation* ids inside ``\\provid{...}``, while paper authors who
    hand-cite a named claim write the *claim* id. Both forms are valid;
    the audit accepts either.
    """
    claim = db.get_claim(provid)
    comp: Optional[Computation] = None
    if claim is not None and claim.computation_id is not None:
        comp = db.get_computation(claim.computation_id)
    if claim is None:
        comp = db.get_computation(provid)
    return claim, comp


def _diff_paragraph_against_payload(
    *,
    provid: str,
    line: int,
    paragraph: str,
    claim: Optional[Claim],
    comp: Computation,
    payload: dict,
    float_tolerance: float,
) -> AuditEntry:
    extracted = extract_numbers_from_paragraph(paragraph)
    # Pull both inputs/outputs branches and the result branch. Computations
    # registered via `register_external` use `inputs`/`outputs`; tracked
    # functions use `args`/`kwargs`/`result`. Walk all that are present.
    payload_subset = {
        k: payload[k]
        for k in ("inputs", "outputs", "args", "kwargs", "result")
        if k in payload
    }
    payload_nums = _payload_numbers(payload_subset)
    claim_nums = _claim_text_numbers(claim.text) if claim is not None else []

    mismatches: list[Mismatch] = []
    for n in extracted:
        match = _match_one(n, payload_nums, claim_nums, float_tolerance)
        if match is None:
            mismatches.append(Mismatch(
                field="paragraph",
                paper_value=n.value,
                db_value=None,
                tolerance=float_tolerance if n.kind == "float" else 0.0,
                severity="warning",
            ))

    status: Literal["MATCH", "DRIFT"] = "MATCH" if not mismatches else "DRIFT"
    detail = (
        f"{len(extracted)} number(s) extracted; "
        f"{len(extracted) - len(mismatches)} matched against payload"
    )
    if status == "DRIFT":
        unmatched = ", ".join(
            f"{m.paper_value:g}" for m in mismatches[:8]
        )
        if len(mismatches) > 8:
            unmatched += f", ... ({len(mismatches)} total)"
        detail += f"; unmatched: {unmatched}"

    return AuditEntry(
        provid=provid,
        line=line,
        paragraph=paragraph,
        status=status,
        detail=detail,
        claim_text=claim.text if claim is not None else None,
        computation_outputs=payload_subset,
        extracted_numbers=extracted,
        mismatches=mismatches,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(report: AuditReport, output_format: str = "text") -> str:
    if output_format == "text":
        return _render_text(report)
    if output_format == "json":
        return _render_json(report)
    if output_format == "markdown":
        return _render_markdown(report)
    raise ValueError(f"unknown output_format {output_format!r}")


def _render_text(report: AuditReport) -> str:
    lines = [
        f"qprov audit-paper {report.tex_path}",
        f"  {report.summary}",
        "",
    ]
    for e in report.entries:
        lines.append(f"[{e.status}] {e.provid}  (line {e.line})")
        lines.append(f"  {e.detail}")
        if e.mismatches:
            for m in e.mismatches:
                lines.append(
                    f"    unmatched paper value {m.paper_value:g} "
                    f"({m.field}, tol={m.tolerance})"
                )
        lines.append("")
    return "\n".join(lines)


def _render_json(report: AuditReport) -> str:
    def _entry_to_dict(e: AuditEntry) -> dict:
        return {
            "provid": e.provid,
            "line": e.line,
            "status": e.status,
            "detail": e.detail,
            "paragraph": e.paragraph,
            "claim_text": e.claim_text,
            "computation_outputs": e.computation_outputs,
            "extracted_numbers": [
                {"value": n.value, "raw_text": n.raw_text, "kind": n.kind}
                for n in e.extracted_numbers
            ],
            "mismatches": [dataclasses.asdict(m) for m in e.mismatches],
        }
    payload = {
        "tex_path": str(report.tex_path),
        "summary": report.summary,
        "entries": [_entry_to_dict(e) for e in report.entries],
    }
    return json.dumps(payload, indent=2, default=str)


def _render_markdown(report: AuditReport) -> str:
    lines = [
        f"# qprov audit-paper report",
        "",
        f"- **Source**: `{report.tex_path}`",
        f"- **Summary**: "
        f"MATCH={report.summary.get('MATCH', 0)}, "
        f"DRIFT={report.summary.get('DRIFT', 0)}, "
        f"MISSING={report.summary.get('MISSING', 0)}, "
        f"ORPHAN={report.summary.get('ORPHAN', 0)}, "
        f"total={sum(report.summary.values())}",
        "",
    ]
    if report.summary.get("DRIFT", 0) == 0 and report.summary.get("MISSING", 0) == 0 \
            and report.summary.get("ORPHAN", 0) == 0:
        lines.append("All references resolved cleanly.")
        lines.append("")
    grouped: dict[str, list[AuditEntry]] = {
        "DRIFT": [], "MISSING": [], "ORPHAN": [], "MATCH": [],
    }
    for e in report.entries:
        grouped.setdefault(e.status, []).append(e)
    for status in ("DRIFT", "MISSING", "ORPHAN", "MATCH"):
        bucket = grouped.get(status, [])
        if not bucket:
            continue
        lines.append(f"## {status} ({len(bucket)})")
        lines.append("")
        for e in bucket:
            lines.append(f"### `\\provid{{{e.provid}}}` (line {e.line})")
            lines.append("")
            lines.append(f"- **Detail**: {e.detail}")
            if e.claim_text:
                lines.append(f"- **Claim text**: {e.claim_text}")
            if e.mismatches:
                lines.append("- **Unmatched paper numbers**:")
                for m in e.mismatches:
                    lines.append(
                        f"    - `{m.paper_value:g}` (field={m.field}, "
                        f"tolerance={m.tolerance})"
                    )
            if e.extracted_numbers and status != "MATCH":
                preview = ", ".join(
                    f"`{n.raw_text}`" for n in e.extracted_numbers[:12]
                )
                lines.append(f"- **Paragraph numbers**: {preview}")
            lines.append("")
            lines.append("```")
            lines.append(_truncate(e.paragraph, 1200))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 3] + "..."


__all__ = [
    "audit_paper",
    "AuditEntry",
    "AuditReport",
    "ExtractedNumber",
    "Mismatch",
    "extract_provid_references",
    "extract_numbers_from_paragraph",
    "render_report",
    "DEFAULT_FLOAT_TOLERANCE",
]
