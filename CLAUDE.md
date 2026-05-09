# CLAUDE.md - qprov

## What this repo is

`qprov` is a local-first provenance tracker for math research computations.
It records every call of a decorated function (input hash, output hash, git
commit, hardware, runtime, captured stdout/stderr, source code) into a
SQLite + gzipped-JSON store, and lets paper authors link `\fact{...}` macros
in a manuscript back to the exact computation that produced the number.

It is built for research on q-deformed real numbers, where numerical claims in
a manuscript need to stay tied to the exact code and data that produced them.
The design target is that a quiet drift between code, data, and a written claim
becomes a loud, immediate failure.

## Scope

- In scope: `@tracked`, SQLite + payload store, claim recording, LaTeX
  export, `verify`, `lint`, `audit-paper`, property-based tracking.
- Out of scope: web UI, multi-user, cloud sync, anything that needs a
  server. Local-first only.

## Working in this repo

- Conventional commits, lowercase, present tense.
- One logical change per commit. No `git add -A`; stage specific files.
- The library targets Python 3.11+. Keep dependencies minimal: `click`,
  `psutil`, `gitpython`, `hypothesis`. `pynvml` is optional under `[gpu]`.
- Tests run via `python -m pytest tests/ -v`. The Sage integration test
  auto-skips when Sage is not on the path.
- Keep prose plain and technical. No em-dashes (U+2014) or en-dashes
  (U+2013) in any tracked file; use hyphens, commas, or rephrase.

## Public API surface

The source of truth for what exists is `src/qprov/__init__.py`. Anything
not re-exported there is private and may change without notice.

## Layout

```
qprov/
  src/qprov/      package source
  tests/          pytest suite
  example/        runnable end-to-end demo
  README.md       user-facing docs
  INTEGRATION.md  paper-author integration guide
  CLAUDE.md       this file (context for AI coding agents)
```
