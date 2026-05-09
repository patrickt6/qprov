# qprov

Provenance tracking for math research computations. Local-first, no servers,
no accounts, no config.

## What is provenance, and why would a math project need it?

When a paper says "we computed that the first nonzero coefficient appears at
`q^46`", that sentence is the end of a long chain. Some code ran, with some
inputs, on some machine, at some version, and produced a number. Months
later the chain is usually gone: the script was edited, the data file was
renamed, nobody remembers which version of the code was used, and the only
surviving record of the number is the sentence in the paper.

Provenance is the record of that chain. For every number you care about,
provenance answers:

- which function produced it, and the exact source code of that function at
  the time it ran
- what the inputs were, including the contents of any data files the
  function read
- which git commit, which machine, which Python and Sage version
- when it ran, and how long it took
- and the part that matters most: enough of all of the above that the
  computation can be run again and checked against the original result, bit
  for bit

`qprov` is a small Python library and command-line tool that captures this
automatically. You add one line above a function. Every time that function
runs, qprov writes a record. Later, `qprov verify <id>` re-runs the
computation and confirms the output is identical. If it is not, you find out
loudly.

Think of it as a lab notebook that fills itself in, plus a receipt you can
hand to a reader so they can reproduce any number in the paper.

It is designed around a common failure mode in research computing: when two
computations of the same quantity disagree, the cause is often two machines
reading different data files that happen to share a name. Most of qprov's
design is a response to that class of mistake; the goal is that a quiet drift
between code, data, and the written claim becomes a loud, immediate failure.

## Who is this for

qprov is built so that it is usable whether or not you think of yourself as
a programmer.

**If you write Python.** Add `@qprov.tracked` above a function. That is the
whole integration. The function still returns exactly what it returned
before; the recording happens as a side effect.

**If you mostly write papers, not code.** You do not have to touch the
library at all. The `qprov` command-line tool lets you list what has been
recorded, inspect any record, re-run and verify it, and check a LaTeX
manuscript against the store with `qprov audit-paper`. A collaborator wires
in the decorator once; from then on you work through the CLI.

**If you work with an AI coding assistant.** The repo ships a `CLAUDE.md`
with the project context an agent needs, and an `INTEGRATION.md` written as
a step-by-step guide for an agent attaching qprov to a manuscript. Point
your assistant at those two files and it can do the wiring for you. See
[For AI coding agents](#for-ai-coding-agents) below.

There is nothing to set up beyond `pip install`. The store is a SQLite
database and a folder of compressed JSON files that live in your project
directory and travel with it in git. No server, no account, no config file.

## Install

Requires Python 3.11 or newer.

```bash
pip install -e .
```

For contributors who also want the test and lint tools:

```bash
pip install -e .[dev]
```

Either way, the `qprov` command becomes available on your `PATH`.

## Quickstart

The 30-second version: decorate a function, run it, record a claim about the
result, export the claims to LaTeX.

```python
import qprov

@qprov.tracked(tags={"experiment": "G1.2", "constant": "pi"})
def compute_qreal_pi(N):
    return _heavy_computation(N)

result = compute_qreal_pi(N=5000)
# A row is now in .qprov/qprov.sqlite, with a gzipped payload on disk.

qprov.claim(
    "The first nonzero coefficient of [pi]_q after q^45 is at q^46",
    computation_id=qprov.find(tags={"constant": "pi"})[0].id,
    value_numeric=46,
    tags={"paper": "my-paper"},   # gated: must point to a computation
)

qprov.export_latex(output="claims.tex")
```

The decorator is transparent: the wrapped function returns its original
result. Recording happens as a side effect.

## Core ideas

There are three things to know.

- A **computation** is one recorded call of a `@tracked` function. It is
  keyed by a hash of the function name, its inputs, and its source code, so
  the same inputs and the same code always produce the same id.
- A **claim** is a one-line factual statement, optionally linked to a
  computation. Claims are what end up in the paper as `\fact{...}` macros.
  A claim tagged with a `paper` is required to point at a computation, so a
  paper-bound statement can never silently lose its backing.
- **Verify** re-runs a recorded computation and compares the output hash to
  the original. It either matches or it fails. There is no "soft" mode.

### Tracking computations that read files

If your function reads a CSV (or any file) by path, declare the path
parameter so the file's contents contribute to the id:

```python
import qprov

@qprov.tracked(data_files=["csv_path"])
def scan_csv_for_modular_pattern(csv_path, modulus):
    with open(csv_path) as f:
        ...

scan_csv_for_modular_pattern("./data/qreal_phi_5000.csv", modulus=5)
```

When called, the decorator replaces the `csv_path` argument with a
`canonical_file(...)` descriptor that carries the file's blake2b digest,
size, and mtime. Same content produces the same id, regardless of which path
the caller supplied. Different content under the same filename produces a
different id, which is exactly the silent-collision bug that motivated this
parameter.

Manual callers can build the descriptor directly:

```python
from qprov import canonical_file
scan(canonical_file("./data/qreal_phi_5000.csv"))
```

## The command-line tool

```text
qprov init                            create .qprov/ in cwd
qprov list                            recent computations as a table
qprov show <id>                       full record (use --payload to dump the payload too)
qprov find --tag k=v                  search by tag, function, time
qprov claim "..." --link <id>         register a claim
   [--value N] [--notes ...]
   [--tag paper=<slug>]               paper-tagged claims require --link
   [--allow-unbacked]                 escape valve for staged claims
qprov export-latex --since DATE       render claims to LaTeX
qprov verify <id>                     re-run computation, assert hash match
qprov lint                            flag orphan / dangling paper claims
qprov gc [--dry-run]                  delete computations not referenced by any claim
qprov audit-paper <tex>               diff a .tex source against the store
qprov properties --list | --check     inspect / re-run property-based checks
```

`<id>` is a 32-char blake2b digest, but any unique prefix (for example 12
chars) is accepted.

`--store PATH` overrides the default store location. The default is the
nearest ancestor `.qprov/` directory, falling back to `cwd/.qprov`. The
environment variable `QPROV_HOME` works as another override.

## Try the demo

```bash
cd example
python run_example.py
```

This runs five q-real computations (`[pi]_q`, `[sqrt(2)]_q`, `[phi]_q`,
`[e]_q`, `[3/2]_q`), records seven claims about their Taylor coefficients,
and writes `example/claims.tex`. Sample line:

```latex
\fact{For $[\pi]_q$, the first nonzero Taylor coefficient at $q^k$ for $k > 2$
appears at $k = 10$, with value $c_{10}([\pi]_q) = 1$.}\footnote{Provenance:
\provid{bd9390f53345afa6eecf38e9428ed5c9}, recorded 2024-01-15T...}
```

To make the LaTeX compile, define `\fact` and `\provid` in your preamble:

```latex
\newcommand{\provid}[1]{\texttt{#1}}
\newcommand{\fact}[1]{\textbf{Claim:} #1}
```

Then verify any of the recorded computations:

```bash
qprov --store .qprov verify bd9390f53345
# OK  bd9390f53345afa6eecf38e9428ed5c9  hash=658168982bc24a4835e5c2ff31b1e410
```

## What gets captured per call

For every successful call:

- `id` - blake2b(function_name | input_hash | code_sha)
- `function_name`, `function_module`, `function_source` (or `<unavailable>`)
- `input_hash`, `output_hash` - blake2b of canonical JSON
- `canonical_data_hash` - JSON map of `{filename: sha}` for every
  `canonical_file(...)` descriptor passed in. `NULL` if the function took no
  file inputs.
- `code_sha`, `code_dirty` - via gitpython
- `hostname`, `cpu_model`, `ram_gb`, `gpu_model`, `python_version`,
  `sage_version`, `os_info` - via psutil + platform + optional pynvml
- `started_at`, `ended_at`, `runtime_seconds`
- payload at `.qprov/payloads/{id[:2]}/{id}.json.gz` containing args, kwargs,
  result, stdout, stderr, warnings

For exceptions: the same row, with `status='error'`, plus `error_type`,
`error_message`, and the full traceback in the payload. The exception is
re-raised, so qprov never changes program behavior.

## For AI coding agents

Two files in this repo are written for an AI coding assistant working on
your project:

- `CLAUDE.md` is the project context an agent should read first: what qprov
  is, what is in and out of scope, the public API surface, and the repo
  conventions to follow when changing code.
- `INTEGRATION.md` is a step-by-step guide for attaching qprov provenance to
  a LaTeX manuscript: where the store lives, the two ways a computation
  enters it, how claims and tags work, how to generate `claims.tex`, and the
  common pitfalls.

If you use Claude Code or a similar agent, the workflow is: point it at
those two files, describe the computations and the paper you want backed,
and let it write the registration script and wire the `\fact{...}` macros
into the manuscript. A human then reviews the diff and runs
`qprov audit-paper main.tex` to confirm every numerical claim lines up with
the store.

## Sage interop

qprov detects Sage at runtime via a soft `import sage.rings.integer`. When
Sage is not installed, the package still works on plain Python values; the
Sage-only types are simply never seen.

The serializer uses canonical JSON with explicit type tags so that
`Integer(10**60)`, Sage rationals, and Sage Laurent series round-trip
through gzipped JSON without losing precision. The single most common
failure mode in math tooling is `json.dump` on a Sage `Integer` raising
`TypeError`; qprov's `canonical_dumps` handles this cleanly.

The Sage-backed test (`tests/test_sage_integration.py`) decorates a
`q_real_truncated` function and verifies bit-identical output. It is skipped
when Sage is not on the path; on a machine with Sage 10.8 it should pass.

## Verify and reproducibility

`qprov verify <id>` imports the original function from its recorded module,
re-invokes it with the recorded args and kwargs, and compares the output
hash byte for byte. The computation must be deterministic: any uncontrolled
randomness will cause `verify` to fail. The recommended pattern is to seed
inside the decorated function so the seed shows up in the input hash:

```python
@qprov.tracked
def sample(n, seed):
    random.seed(seed)
    return [random.random() for _ in range(n)]
```

`verify` deliberately fails loudly when re-run output does not match. There
is no "soft verify" mode.

## Tests

```bash
python -m pytest tests/ -v
```

Tests cover tracking, the store, claims, serialize, verify, external
registration, the canonical-file path, the paper-tag gate, the audit-paper
command, and property-based tracking. `test_sage_integration.py` is skipped
automatically when Sage is not on the path.

## Status

The library covers five capability lines:

- File-content hashing via `canonical_file(...)` and
  `@tracked(data_files=[...])`, so a record's identity tracks the contents of
  the data it read, not just the path.
- A paper-tag gate on `claim()`: a paper-bound claim must point at a
  computation.
- Payload-tamper detection (`payload_hash`), strict collision semantics on the
  store, and FK + CHECK constraints on claims.
- `qprov audit-paper <tex>` walks a LaTeX source and reports
  MATCH / DRIFT / MISSING / ORPHAN per `\provid{...}`.
- Property-based tracking: `@tracked(properties=[...])` runs Hypothesis-driven
  metamorphic checks before the row is written.

## Project layout

```
qprov/
├── pyproject.toml
├── README.md
├── CLAUDE.md              context for AI coding agents
├── INTEGRATION.md         how paper authors wire qprov into a manuscript
├── src/qprov/
│   ├── __init__.py        public API
│   ├── tracking.py        @tracked decorator + payload assembly
│   ├── inputs.py          canonical_file(), hash_file(), data_files plumbing
│   ├── store.py           SQLite + gzipped JSON payload directory + migrations
│   ├── serialize.py       canonical JSON with Sage / NumPy type tags
│   ├── hardware.py        CPU / RAM / GPU / OS / Python / Sage version capture
│   ├── gitinfo.py         git SHA + dirty flag via gitpython
│   ├── claims.py          claim recording + LaTeX export + paper-tag gate
│   ├── query.py           find() / get() programmatic API
│   ├── verify.py          re-run + hash compare
│   ├── external.py        retroactive registration for pre-existing outputs
│   ├── audit_paper.py     walk a .tex file, reconcile against the store
│   ├── properties.py      property-based tracking via Hypothesis
│   ├── properties_qnumbers.py  q-number specific metamorphic property checks
│   └── cli.py             click CLI entry point
├── tests/                 pytest suite (Sage-only test is auto-skipped)
└── example/
    ├── q_real_python.py   pure-Python q-real reference implementation
    ├── q_real_demo.py     decorated entry points (importable)
    ├── run_example.py     end-to-end demo
    └── claims.tex         sample \fact macros from a real run
```

## Limitations and open work

- The serializer represents Sage Laurent / power series as
  `(valuation, [coefficients], precision)` rather than reconstructing the
  exact ring object. Verify still compares bit for bit; semantic
  reconstruction is not needed for the current scope.
- `verify` requires the function to be importable from its recorded module.
  Functions defined in `__main__`, lambdas, or REPL-only definitions cannot
  be verified. The error message is explicit.
- `gc` deletes any computation not referenced by a claim, with confirmation.
  A time-based retention heuristic is on the wish list.
- No claim numeric-value index. Sorting claims by `value_numeric` works via
  SQL `ORDER BY` but is not exposed in the CLI.
- Structured claim assertions are still free-form prose. A claim that
  asserts "biconditional, 4998 indices" is opaque to qprov; only the
  computation's output_hash is machine-checked. A future column
  `claim_assertions` (JSON) could capture direction / N / counts so a re-run
  can fail loudly when prose drifts from data.

## License

MIT. See [LICENSE](./LICENSE).
