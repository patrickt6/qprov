# qprov integration guide for paper authors

This guide shows how to attach qprov provenance to a LaTeX manuscript, so that
every numerical claim in the paper points to a recorded computation in the
qprov store and a reader can run `qprov verify <id>` to reproduce it.

## The qprov store

By default the store is the nearest ancestor `.qprov/` directory, falling back
to `./.qprov`. Override it with the `QPROV_HOME` environment variable or
`qprov.set_store_root(path)`.

Store contents:

- `.qprov/qprov.sqlite` - SQLite metadata (computations, tags, claims)
- `.qprov/payloads/{id[:2]}/{id}.json.gz` - one gzipped JSON per computation,
  holding inputs, outputs, source code, and captured stdout/stderr

A single store can hold the computations behind several papers. Filter a
paper's claims with the `paper=` tag.

## Two ways a computation enters the store

### (a) Live: the `@tracked` decorator

For computations you are writing now:

```python
import qprov

@qprov.tracked(tags={"paper": "your-paper", "experiment": "X.Y"})
def compute_thing(N):
    ...
    return result
```

Every call records a row keyed on
`blake2b(function_name | input_hash | code_sha)`. The same inputs and the same
code collapse to the same id, so repeats overwrite in place.

**If the function reads a file by path, declare it.** The `data_files`
parameter makes the file's *contents* part of the input hash, not just the path
string. Without it, two machines holding files under the same name with
different content silently collapse to one row:

```python
@qprov.tracked(
    tags={"paper": "your-paper"},
    data_files=["csv_path"],
)
def scan_csv(csv_path, N):
    ...
```

The decorator replaces `csv_path` with a `canonical_file(...)` descriptor
before hashing. The descriptor carries the file's blake2b digest, size, and
mtime; the digest is what feeds the input hash. The resulting row has a
non-NULL `canonical_data_hash` column, so `qprov lint` will not flag it as
NOHASH.

### (b) Retroactive: `register_external(...)`

For pre-existing computations whose output is already on disk:

```python
import qprov

qprov.register_external(
    function_name="my_search_v1",            # logical name; part of the id
    inputs={"target": "x", "N": 2000},       # dict; hashed for the id
    outputs={"result": 0},                   # any JSON-serializable value
    code_path="path/to/script.py",           # recorded in the payload
    code_sha="3c0d5d7f...",                  # part of the id
    runtime_seconds=12.5,                    # optional
    tags={"paper": "your-paper", "retroactive": True},
    source_file="path/to/original/output.json",
    notes="anything useful",
)
```

The id is again `blake2b(function_name | input_hash | code_sha)`. Re-running the
registration script is idempotent by design, so you can re-run it whenever you
add or change a record and the database stays clean.

## Claims

A claim is a one-line factual statement, optionally linked to a computation.
Claims are what end up in the paper as `\fact{...}` macros.

```python
qprov.claim(
    "No polynomial of bidegree at most (6, 50) annihilates [x]_q modulo q^2000.",
    computation_id="<some_comp_id>",
    claim_id="emptiness_x",            # stable id; re-runs overwrite
    tags={"paper": "your-paper"},      # paper tag gates this claim
)
```

### The paper-tag gate

When a claim carries `tags={"paper": "..."}`, qprov requires a non-NULL
`computation_id`. Calls without one raise `UnbackedPaperClaimError`, so a
paper-bound statement can never silently render as `\provid{None}` in the
exported LaTeX.

If you genuinely need to stage a claim before its computation lands (for
example, writing a draft from notes while a long scan is still running), pass
`allow_unbacked=True` and back-attach later:

```python
# Stage now:
qprov.claim(
    "A statement to be backed once the scan finishes.",
    tags={"paper": "your-paper"},
    allow_unbacked=True,
    claim_id="staged_claim",
)

# Back-attach after the computation registers:
qprov.claim(
    "A statement to be backed once the scan finishes.",
    tags={"paper": "your-paper"},
    computation_id="<the_real_comp_id>",
    claim_id="staged_claim",           # same id; overwrites in place
)
```

`qprov lint` flags every unbacked paper claim, so a pre-flight catches anything
left staged.

### Three claim-id strategies

| Need | Use | Effect |
|---|---|---|
| One-off interactive claim | default | random hex id every call |
| Idempotent batch script | `deterministic_id=True` | id derived from `(text, comp_id, value_numeric)` |
| Stable claim that updates with new data | `claim_id="<your_key>"` | you control the id; overwrites in place |

Use `claim_id=...` for any claim whose text will evolve as you add
computations (for example a bound widening from `(6,50)` to `(7,50)`).
Re-running the registration script then updates the same row instead of leaving
stale claims.

## Tags

Every computation and most claims carry tags. Tags are how the paper-level
filter works (`qprov export-latex --tag paper=your-paper`). Useful conventions:

- `paper`: short slug for the paper a record contributes to.
- `phase`: lifecycle stage (`part-1`, `validation`, ...).
- `retroactive`: `True` for records added via `register_external`.
- `type`: cross-cutting kinds (`verification`, `negative_control`, ...).

Find by tag:

```python
qprov.find(tags={"paper": "your-paper", "phase": "part-1"}, limit=500)
```

## Generating `claims.tex`

The CLI exports every claim in the store as a `\fact{...}` macro with a
`\footnote{Provenance: \provid{<id>}}` attached. Filter by tag:

```bash
python -m qprov.cli export-latex \
    --tag paper=your-paper \
    --output claims.tex
```

The exporter runs a `latexify` pass over every `$...$` math span, turning
polynomial strings like `X*q^10` into clean LaTeX (`X q^{10}`). It is automatic
and idempotent.

## Inlining into `main.tex`

Add to the preamble:

```latex
% Provenance macros required by claims.tex
\newcommand{\provid}[1]{\texttt{\small #1}}
\newcommand{\fact}[1]{#1}    % or wrap in a box, theorem env, etc.

\input{claims.tex}
```

Then footnote each numerical statement with its claim id:

```latex
\begin{theorem}
... \footnote{Claim \provid{emptiness\_x}; reproducible via
\texttt{qprov verify <id>}.}
\end{theorem}
```

Stable claim ids (`emptiness_x`) read better in the source than the 32-char
blake2b digests, which is one reason to use them.

## Cross-checking with a second tool

For a result that rests on a single computer algebra system, run a second,
independent implementation and record both. If the two disagree, you want to
know before the number reaches the paper, not after. Record each side as its
own computation and tag them so a reviewer can see that both were checked.

## Verifying a recorded computation

```bash
qprov verify <comp_id>
```

This re-imports the original function from its recorded module, re-invokes it
with the recorded args and kwargs, and compares the output hash byte for byte.
Determinism is required: any uncontrolled randomness makes verify fail. The
recommended pattern is to seed inside the decorated function so the seed shows
up in the input hash.

`verify` is only meaningful for `@tracked` computations. Records added via
`register_external` are immutable records of work done elsewhere and cannot be
re-run by qprov itself.

## Common pitfalls

- **Non-stable inputs.** If the `inputs` dict varies between runs due to
  insertion order or floating-point quirks, ids will differ. Use
  deterministic, JSON-serializable types.
- **Claims pile up.** Without `claim_id` or `deterministic_id`, every
  `qprov.claim(...)` call mints a new row, so re-running a script duplicates.
  Pin claim ids in batch scripts.
- **Filename-only file inputs.** A tracked function that takes `csv_path` as a
  plain string and reads it inside the body hashes only the path string. Two
  files with the same name but different contents then collapse to one id.
  Declare `data_files=["csv_path"]`, or wrap the call site in
  `canonical_file(...)`. `qprov lint` flags these as NOHASH advisories.
- **Paper-tagged claim with no computation.** Calling `qprov.claim(...,
  tags={"paper": "..."}, computation_id=None)` raises
  `UnbackedPaperClaimError`. Stage with `allow_unbacked=True` and back-attach
  before exporting LaTeX.

## Asking for help

If something in qprov is not doing what this guide claims, prefer reading the
source over guessing: the package is small. The public API surface in
`src/qprov/__init__.py` is the source of truth for what exists.
