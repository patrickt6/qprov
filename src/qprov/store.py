"""SQLite store + gzipped JSON payload directory.

Layout under store_root (default `./.qprov`):

    .qprov/
      qprov.sqlite        metadata for computations, tags, claims
      payloads/
        ab/
          ab1234...json.gz   payload keyed by computation id

The store is single-user. We open a fresh sqlite3 connection per write to
avoid threading subtleties, and keep the schema minimal.

Schema v4 adds the ``property_results`` column to ``computations`` for the
property-based tracking layer. Strictly additive on top of v3:
existing rows get a NULL ``property_results``; new writes populate it
when ``@tracked`` is invoked with ``properties=[...]``. The lint
``PROPMISSING`` advisory flags paper-tagged rows whose
``property_results`` is NULL; ``PROPFAIL`` is a hard failure when a
stored property's ``passed`` flag is False.

Schema v3 adds three protections over v2:

- ``payload_hash`` records the blake2b hash of the uncompressed canonical
  JSON payload bytes on write. ``read_payload`` re-hashes the payload bytes
  on every read and raises :class:`PayloadTamperedError` on mismatch.
  Closes the "hand-edit the gzipped payload" failure mode.
- ``output_hash_algorithm`` records which hash function produced the value
  in ``payload_hash`` (always ``'blake2b'`` for now; kept explicit so a
  future migration can rotate the algorithm without re-using the column).
- The ``claims`` table is rebuilt with
  ``computation_id`` ``REFERENCES computations(id) ON DELETE RESTRICT``
  (was ``ON DELETE SET NULL``) plus a CHECK constraint enforcing the
  paper-tagged-claims-must-be-backed invariant at the database level.

The pre-existing ``output_hash`` column (semantics: blake2b of the
function *return value's* canonical JSON, used by ``verify``) is left
alone. The spec literal asked us to "add output_hash" and store the
payload-bytes hash there, but ``output_hash`` already existed with a
different and load-bearing meaning. ``payload_hash`` is the spec's
intent under a clearer name; ``output_hash`` continues to support
``verify`` (which re-runs the function and compares result hashes).

INSERT semantics changed in v3 as well. ``computations`` and ``claims``
no longer use ``INSERT OR REPLACE``: a duplicate id with differing
content raises :class:`QprovCollisionError` instead of silently
clobbering. Duplicate ids with byte-identical content are still
accepted as a no-op (this preserves register_external's documented
idempotency). Tag tables continue to use ON CONFLICT UPDATE because
the intentional semantic is "set the tag value".
"""
from __future__ import annotations

import dataclasses
import gzip
import hashlib
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .serialize import canonical_dumps, canonical_loads


CURRENT_SCHEMA_VERSION = "4"
PAYLOAD_HASH_ALGORITHM = "blake2b"
PAYLOAD_HASH_DIGEST_SIZE = 16  # bytes -> 32 hex chars, matches hash_value/hash_text

_log = logging.getLogger("qprov.store")


class QprovCollisionError(RuntimeError):
    """Raised when a write attempts to insert a row whose id already
    exists in the store AND whose content differs from the existing
    row. v3's content-aware id makes this a near-impossible event in
    practice; if it ever fires, treat it as either a hash collision or
    a coding bug at the call site, never as "old row was stale".

    The pre-v3 store used ``INSERT OR REPLACE`` and silently clobbered
    the older row in this case; v3's strict write path makes a silent
    clobber loud instead.

    Pass ``force=True`` to :meth:`Store.insert_computation` /
    :meth:`Store.insert_claim` to escalate back to the legacy REPLACE
    behavior. Document the reason; the older row is lost.
    """


class PayloadTamperedError(RuntimeError):
    """Raised when a payload file's recomputed blake2b hash does not
    match the value recorded in ``computations.payload_hash`` at write
    time. Indicates that either the .json.gz file on disk was edited
    out-of-band, or the store is corrupted, or the algorithm in
    ``output_hash_algorithm`` is no longer understood.

    Use ``qprov show <id> --no-verify`` to bypass the check for
    forensics on an already-corrupted store.
    """


SCHEMA = """
CREATE TABLE IF NOT EXISTS computations (
    id                     TEXT PRIMARY KEY,
    function_name          TEXT NOT NULL,
    function_module        TEXT,
    input_hash             TEXT NOT NULL,
    output_hash            TEXT,
    output_hash_algorithm  TEXT DEFAULT 'blake2b',
    payload_hash           TEXT,
    code_sha               TEXT,
    code_dirty             INTEGER,
    hostname               TEXT,
    cpu_model              TEXT,
    ram_gb                 REAL,
    gpu_model              TEXT,
    python_version         TEXT,
    sage_version           TEXT,
    os_info                TEXT,
    runtime_seconds        REAL,
    started_at             TEXT NOT NULL,
    ended_at               TEXT,
    status                 TEXT NOT NULL DEFAULT 'ok',
    error_type             TEXT,
    error_message          TEXT,
    payload_path           TEXT NOT NULL,
    canonical_data_hash    TEXT,
    property_results       TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    computation_id  TEXT NOT NULL,
    key             TEXT NOT NULL,
    value           TEXT,
    PRIMARY KEY (computation_id, key),
    FOREIGN KEY (computation_id) REFERENCES computations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tags_kv ON tags(key, value);

CREATE TABLE IF NOT EXISTS claims (
    id              TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    value_numeric   REAL,
    computation_id  TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT,
    paper_tag       TEXT,
    unbacked        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (computation_id) REFERENCES computations(id) ON DELETE RESTRICT,
    CHECK (paper_tag IS NULL OR computation_id IS NOT NULL OR unbacked = 1)
);

CREATE INDEX IF NOT EXISTS idx_claims_created ON claims(created_at);
CREATE INDEX IF NOT EXISTS idx_claims_comp ON claims(computation_id);

CREATE TABLE IF NOT EXISTS claim_tags (
    claim_id  TEXT NOT NULL,
    key       TEXT NOT NULL,
    value     TEXT,
    PRIMARY KEY (claim_id, key),
    FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_claim_tags_kv ON claim_tags(key, value);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclasses.dataclass
class Computation:
    id: str
    function_name: str
    function_module: str | None
    input_hash: str
    output_hash: str | None
    code_sha: str | None
    code_dirty: bool | None
    hostname: str | None
    cpu_model: str | None
    ram_gb: float | None
    gpu_model: str | None
    python_version: str | None
    sage_version: str | None
    os_info: str | None
    runtime_seconds: float | None
    started_at: str
    ended_at: str | None
    status: str
    error_type: str | None
    error_message: str | None
    payload_path: str
    tags: dict[str, str] = dataclasses.field(default_factory=dict)
    canonical_data_hash: str | None = None
    payload_hash: str | None = None
    output_hash_algorithm: str | None = None
    property_results: dict[str, dict[str, Any]] | None = None

    @property
    def result(self) -> Any:
        """Lazily read the payload's `result` field from disk."""
        store = get_store()
        return store.read_payload(self.id).get("result")

    def payload(self) -> dict:
        return get_store().read_payload(self.id)


@dataclasses.dataclass
class Claim:
    id: str
    text: str
    value_numeric: float | None
    computation_id: str | None
    created_at: str
    notes: str | None
    tags: dict[str, str] = dataclasses.field(default_factory=dict)
    paper_tag: str | None = None
    unbacked: bool = False


_DEFAULT_STORE_DIRNAME = ".qprov"
_store_singleton: "Store | None" = None
_store_root_override: Path | None = None
_store_lock = threading.Lock()


def set_store_root(path: str | os.PathLike) -> None:
    """Override the default store location. Resets the cached singleton."""
    global _store_singleton, _store_root_override
    _store_root_override = Path(path).resolve()
    _store_singleton = None


def get_store() -> "Store":
    """Return the shared Store, creating it on first call.

    Resolution order:
      1. explicit override via set_store_root()
      2. QPROV_HOME env var
      3. nearest ancestor `.qprov/` directory
      4. cwd / .qprov  (auto-created)
    """
    global _store_singleton
    with _store_lock:
        if _store_singleton is not None:
            return _store_singleton
        root = _resolve_store_root()
        _store_singleton = Store(root)
        return _store_singleton


def _resolve_store_root() -> Path:
    if _store_root_override is not None:
        return _store_root_override
    env = os.environ.get("QPROV_HOME")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / _DEFAULT_STORE_DIRNAME
        if candidate.is_dir():
            return candidate
    return cwd / _DEFAULT_STORE_DIRNAME


class Store:
    """SQLite + payload directory.

    Initializing a Store auto-creates the directory and schema if missing.
    """

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()
        self.db_path = self.root / "qprov.sqlite"
        self.payloads_dir = self.root / "payloads"
        self._ensure()

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.payloads_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # During a v2->v3 rebuild of the claims table the FK pragma
            # has to be off so the legacy ON DELETE SET NULL constraint
            # does not nullify rows mid-rebuild. _ensure is the only
            # place that creates and migrates the schema, so the toggle
            # is local; live writes through other methods see the
            # default `PRAGMA foreign_keys = ON` set by _connect.
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.executescript(SCHEMA)
                self._migrate(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
                    (CURRENT_SCHEMA_VERSION,),
                )
            finally:
                conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply schema upgrades to a pre-existing store.

        Migrations are guarded by per-step idempotency checks
        (column-presence checks via ``PRAGMA table_info``, table-shape
        checks via ``sqlite_master``) so a re-run on an already-migrated
        store is a silent no-op. CREATE TABLE IF NOT EXISTS in the
        SCHEMA literal handles new tables; this method handles ALTER
        TABLE and rebuild-via-rename for column changes and constraint
        changes.

        The chain is v1 -> v2 -> v3:

        - v1 -> v2: add ``canonical_data_hash`` to computations.
        - v2 -> v3: add ``output_hash_algorithm`` and ``payload_hash``
          columns, backfill ``payload_hash`` from existing payloads, and
          rebuild the ``claims`` table to use
          ``ON DELETE RESTRICT`` plus a CHECK that paper-tagged
          claims have a backing computation.
        """
        # v1 -> v2: canonical_data_hash column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        if "canonical_data_hash" not in cols:
            conn.execute("ALTER TABLE computations ADD COLUMN canonical_data_hash TEXT")

        # v2 -> v3: payload_hash + output_hash_algorithm columns.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        if "output_hash_algorithm" not in cols:
            conn.execute(
                "ALTER TABLE computations ADD COLUMN output_hash_algorithm TEXT DEFAULT 'blake2b'"
            )
        if "payload_hash" not in cols:
            conn.execute("ALTER TABLE computations ADD COLUMN payload_hash TEXT")

        # v2 -> v3: backfill payload_hash for existing rows by reading
        # each payload off disk and re-hashing. Missing payloads are
        # logged but not fatal (a data-integrity problem, not a
        # migration problem). The backfill only updates rows that don't
        # already have a payload_hash, so re-running is a no-op.
        self._backfill_payload_hashes(conn)

        # v2 -> v3: tighten claims FK + add CHECK constraint. Detected
        # via sqlite_master.sql; idempotent.
        self._rebuild_claims_table_if_needed(conn)

        # v3 -> v4: add property_results column for the property-based
        # tracking layer. Strictly additive: existing
        # rows get a NULL property_results (legacy v3 records have no
        # property declarations to evaluate), new writes populate it
        # when @tracked is invoked with properties=[...]. The lint
        # PROPMISSING advisory surfaces paper-tagged rows whose
        # property_results is NULL.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        if "property_results" not in cols:
            conn.execute("ALTER TABLE computations ADD COLUMN property_results TEXT")

    def _backfill_payload_hashes(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id FROM computations WHERE payload_hash IS NULL"
        ).fetchall()
        backfilled = 0
        missing: list[str] = []
        for row in rows:
            comp_id = row["id"]
            path = self.payload_path_for(comp_id)
            if not path.is_file():
                missing.append(comp_id)
                continue
            try:
                with gzip.open(path, "rb") as f:
                    raw = f.read()
            except OSError as exc:
                missing.append(comp_id)
                _log.warning("payload unreadable for %s: %s", comp_id, exc)
                continue
            ph = _hash_payload_bytes(raw)
            conn.execute(
                "UPDATE computations SET payload_hash = ?, "
                "output_hash_algorithm = COALESCE(output_hash_algorithm, ?) "
                "WHERE id = ?",
                (ph, PAYLOAD_HASH_ALGORITHM, comp_id),
            )
            backfilled += 1
        if backfilled or missing:
            _log.info(
                "qprov v2->v3 payload_hash backfill: %d hashed, %d payloads missing",
                backfilled, len(missing),
            )

    def _claims_needs_rebuild(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'claims'"
        ).fetchone()
        if row is None or row["sql"] is None:
            return False
        sql = row["sql"]
        return "ON DELETE RESTRICT" not in sql or "CHECK" not in sql

    def _rebuild_claims_table_if_needed(self, conn: sqlite3.Connection) -> None:
        if not self._claims_needs_rebuild(conn):
            return

        # Detect rows that would violate the new CHECK so the migration
        # fails loudly instead of dropping them on the floor. A paper-
        # tagged claim with no computation_id and no unbacked=true tag
        # is the ORPHAN that v3 prohibits at the DB level; v2 enforced
        # it at the Python layer, so old v2 stores should already be
        # clean, but defensive detection beats silent loss.
        offenders = conn.execute(
            """
            SELECT c.id
            FROM claims c
            JOIN claim_tags pt ON pt.claim_id = c.id AND pt.key = 'paper'
            WHERE c.computation_id IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM claim_tags ut
                WHERE ut.claim_id = c.id AND ut.key = 'unbacked' AND ut.value = 'true'
              )
            """
        ).fetchall()
        if offenders:
            ids = ", ".join(repr(r["id"]) for r in offenders)
            raise RuntimeError(
                f"qprov v2->v3 migration: {len(offenders)} paper-tagged "
                f"claim(s) have no backing computation and no unbacked tag, "
                f"which violates the v3 CHECK constraint: {ids}. Resolve "
                f"(link a computation, mark unbacked, or delete) and "
                f"re-open the store."
            )

        conn.executescript(
            """
            ALTER TABLE claims RENAME TO claims_v2;
            CREATE TABLE claims (
                id              TEXT PRIMARY KEY,
                text            TEXT NOT NULL,
                value_numeric   REAL,
                computation_id  TEXT,
                created_at      TEXT NOT NULL,
                notes           TEXT,
                paper_tag       TEXT,
                unbacked        INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (computation_id) REFERENCES computations(id) ON DELETE RESTRICT,
                CHECK (paper_tag IS NULL OR computation_id IS NOT NULL OR unbacked = 1)
            );
            INSERT INTO claims (id, text, value_numeric, computation_id, created_at, notes, paper_tag, unbacked)
            SELECT
                c.id, c.text, c.value_numeric, c.computation_id, c.created_at, c.notes,
                (SELECT value FROM claim_tags WHERE claim_id = c.id AND key = 'paper'),
                CASE WHEN EXISTS (
                    SELECT 1 FROM claim_tags
                    WHERE claim_id = c.id AND key = 'unbacked' AND value = 'true'
                ) THEN 1 ELSE 0 END
            FROM claims_v2 c;
            DROP TABLE claims_v2;
            CREATE INDEX IF NOT EXISTS idx_claims_created ON claims(created_at);
            CREATE INDEX IF NOT EXISTS idx_claims_comp ON claims(computation_id);
            """
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def payload_path_for(self, comp_id: str) -> Path:
        return self.payloads_dir / comp_id[:2] / f"{comp_id}.json.gz"

    def write_payload(self, comp_id: str, payload: dict) -> tuple[Path, str]:
        """Write a canonical-JSON gzipped payload, returning (path, payload_hash).

        The hash is computed over the *uncompressed* canonical JSON
        bytes (not the gzipped on-disk bytes) because gzip headers
        carry an mtime that changes the byte stream without changing
        the data. Uncompressed bytes are content-only and stable across
        machines.
        """
        path = self.payload_path_for(comp_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = canonical_dumps(payload)
        raw = text.encode("utf-8")
        payload_hash = _hash_payload_bytes(raw)
        # mtime=0 strips the gzip member timestamp so two machines
        # writing identical payload bytes produce byte-identical .gz
        # files. The hash is computed over the uncompressed bytes
        # regardless, but stable .gz files help downstream diff tools.
        with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as f:
            f.write(raw)
        return path, payload_hash

    def read_payload(self, comp_id: str, *, verify_hash: bool = True) -> dict:
        """Read a payload off disk and (by default) check its hash against
        the value recorded at write time.

        Set ``verify_hash=False`` to skip the integrity check. The skip
        path is for forensics on a known-corrupted store; in normal
        operation the check is cheap and adds the only guard against
        hand-edited payloads landing in ``qprov show`` and lint.
        """
        path = self.payload_path_for(comp_id)
        if not path.is_file():
            raise FileNotFoundError(f"no payload at {path}")
        with gzip.open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
        if verify_hash:
            recorded = self._recorded_payload_hash(comp_id)
            if recorded is not None:
                actual = _hash_payload_bytes(raw)
                if actual != recorded:
                    raise PayloadTamperedError(
                        f"payload for computation {comp_id!r} has changed on disk.\n"
                        f"  recorded hash: {recorded}\n"
                        f"  current hash:  {actual}\n"
                        f"Either the payload file was edited or the store is "
                        f"corrupted. Use `qprov show {comp_id[:12]} --no-verify` "
                        f"to inspect."
                    )
        return canonical_loads(text)

    def _recorded_payload_hash(self, comp_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_hash FROM computations WHERE id = ?", (comp_id,)
            ).fetchone()
        if row is None:
            return None
        return row["payload_hash"]

    # Columns whose equality means "same logical row." Differences in
    # any of these between an existing row and an incoming row trigger
    # QprovCollisionError; differences in hostname/started_at/timings
    # are treated as re-runs of the same logical computation and the
    # existing row is kept. The new row's payload file (which is
    # content-determined and itself fingerprinted) is overwritten
    # with byte-identical content, so the on-disk store stays stable.
    _COMP_IDENTITY_COLUMNS = (
        "function_name",
        "function_module",
        "input_hash",
        "output_hash",
        "code_sha",
        "canonical_data_hash",
        "payload_hash",
    )

    def insert_computation(self, comp: Computation, *, force: bool = False) -> None:
        """Insert a computation row.

        Default behavior (``force=False``): try ``INSERT`` first; on a
        primary-key conflict, compare the existing row against the
        incoming row on the identity columns. Identical -> silent
        no-op (preserves register_external's documented idempotency).
        Different -> :class:`QprovCollisionError`.

        ``force=True``: escalate to REPLACE. This is the pre-v3
        behavior and loses the audit history of the older row.
        Document a reason in the surrounding code or commit message
        when you reach for it.
        """
        property_results_json = (
            None if comp.property_results is None
            else canonical_dumps(comp.property_results)
        )
        params = (
            comp.id, comp.function_name, comp.function_module,
            comp.input_hash, comp.output_hash,
            comp.output_hash_algorithm or PAYLOAD_HASH_ALGORITHM,
            comp.payload_hash,
            comp.code_sha,
            int(comp.code_dirty) if comp.code_dirty is not None else None,
            comp.hostname, comp.cpu_model, comp.ram_gb, comp.gpu_model,
            comp.python_version, comp.sage_version, comp.os_info,
            comp.runtime_seconds, comp.started_at, comp.ended_at,
            comp.status, comp.error_type, comp.error_message,
            comp.payload_path, comp.canonical_data_hash,
            property_results_json,
        )
        cols_sql = (
            "id, function_name, function_module, "
            "input_hash, output_hash, output_hash_algorithm, payload_hash, "
            "code_sha, code_dirty, "
            "hostname, cpu_model, ram_gb, gpu_model, "
            "python_version, sage_version, os_info, "
            "runtime_seconds, started_at, ended_at, "
            "status, error_type, error_message, payload_path, "
            "canonical_data_hash, property_results"
        )
        with self._connect() as conn:
            if force:
                conn.execute(
                    f"INSERT OR REPLACE INTO computations ({cols_sql}) "
                    f"VALUES ({','.join('?' * len(params))})",
                    params,
                )
            else:
                try:
                    conn.execute(
                        f"INSERT INTO computations ({cols_sql}) "
                        f"VALUES ({','.join('?' * len(params))})",
                        params,
                    )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "SELECT * FROM computations WHERE id = ?", (comp.id,)
                    ).fetchone()
                    if existing is None:
                        raise
                    if _row_matches_computation(existing, comp, self._COMP_IDENTITY_COLUMNS):
                        # Same id, same content. v2 INSERT OR REPLACE
                        # silently overwrote metadata fields (hostname,
                        # started_at); v3 keeps the older row's
                        # provenance metadata and treats the re-write
                        # as a no-op. The payload file on disk has
                        # already been overwritten with byte-identical
                        # content by the caller, so the store stays
                        # internally consistent.
                        return
                    raise QprovCollisionError(
                        f"computation id {comp.id!r} already exists with "
                        f"different content.\n"
                        f"  existing: {_dict_for_collision_msg(existing, self._COMP_IDENTITY_COLUMNS)}\n"
                        f"  incoming: {_dict_for_collision_msg(comp, self._COMP_IDENTITY_COLUMNS)}\n"
                        f"v3's content-aware id should have prevented this. "
                        f"Treat as either a hash collision (rare) or a coding "
                        f"bug at the call site. Pass force=True to override "
                        f"(the older row's audit history will be lost)."
                    )
            if comp.tags:
                conn.executemany(
                    "INSERT INTO tags (computation_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(computation_id, key) DO UPDATE SET value = excluded.value",
                    [(comp.id, k, str(v)) for k, v in comp.tags.items()],
                )

    def get_computation(self, comp_id: str) -> Computation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM computations WHERE id = ?", (comp_id,)
            ).fetchone()
            if row is None:
                # Try prefix match for convenience
                rows = conn.execute(
                    "SELECT * FROM computations WHERE id LIKE ?", (comp_id + "%",)
                ).fetchall()
                if len(rows) != 1:
                    return None
                row = rows[0]
            tag_rows = conn.execute(
                "SELECT key, value FROM tags WHERE computation_id = ?", (row["id"],)
            ).fetchall()
        tags = {r["key"]: r["value"] for r in tag_rows}
        return _row_to_computation(row, tags)

    def list_computations(
        self,
        limit: int = 50,
        function_name: str | None = None,
        tag_filters: dict[str, str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[Computation]:
        sql = "SELECT c.* FROM computations c"
        params: list[Any] = []
        wheres: list[str] = []
        if tag_filters:
            for i, (k, v) in enumerate(tag_filters.items()):
                alias = f"t{i}"
                sql += f" JOIN tags {alias} ON {alias}.computation_id = c.id"
                if v is None:
                    wheres.append(f"{alias}.key = ?")
                    params.append(k)
                else:
                    wheres.append(f"{alias}.key = ? AND {alias}.value = ?")
                    params.extend([k, str(v)])
        if function_name:
            wheres.append("c.function_name = ?")
            params.append(function_name)
        if since:
            wheres.append("c.started_at >= ?")
            params.append(since)
        if until:
            wheres.append("c.started_at <= ?")
            params.append(until)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY c.started_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            comps = []
            for row in rows:
                tag_rows = conn.execute(
                    "SELECT key, value FROM tags WHERE computation_id = ?", (row["id"],)
                ).fetchall()
                tags = {r["key"]: r["value"] for r in tag_rows}
                comps.append(_row_to_computation(row, tags))
            return comps

    # Identity columns for claim deduplication; see _COMP_IDENTITY_COLUMNS.
    _CLAIM_IDENTITY_COLUMNS = (
        "text",
        "value_numeric",
        "computation_id",
        "paper_tag",
        "unbacked",
    )

    def insert_claim(self, claim: Claim, *, force: bool = False) -> None:
        """Insert a claim row.

        Same collision semantics as :meth:`insert_computation`: a
        duplicate id whose content matches is a silent no-op; a
        duplicate id with different content raises
        :class:`QprovCollisionError` unless ``force=True``.
        """
        paper_tag = claim.paper_tag if claim.paper_tag is not None else claim.tags.get("paper")
        unbacked_flag = 1 if (
            claim.unbacked or claim.tags.get("unbacked") == "true"
        ) else 0
        params = (
            claim.id, claim.text, claim.value_numeric,
            claim.computation_id, claim.created_at, claim.notes,
            paper_tag, unbacked_flag,
        )
        cols_sql = (
            "id, text, value_numeric, computation_id, created_at, notes, "
            "paper_tag, unbacked"
        )
        with self._connect() as conn:
            if force:
                conn.execute(
                    f"INSERT OR REPLACE INTO claims ({cols_sql}) "
                    f"VALUES ({','.join('?' * len(params))})",
                    params,
                )
            else:
                try:
                    conn.execute(
                        f"INSERT INTO claims ({cols_sql}) "
                        f"VALUES ({','.join('?' * len(params))})",
                        params,
                    )
                except sqlite3.IntegrityError as exc:
                    existing = conn.execute(
                        "SELECT * FROM claims WHERE id = ?", (claim.id,)
                    ).fetchone()
                    if existing is None:
                        # Not a PK conflict - likely the v3 CHECK constraint
                        # firing on a paper-tagged claim without a backing
                        # computation. Surface it as-is so the caller sees
                        # the database's reason.
                        raise
                    incoming_dict = {
                        "text": claim.text,
                        "value_numeric": claim.value_numeric,
                        "computation_id": claim.computation_id,
                        "paper_tag": paper_tag,
                        "unbacked": unbacked_flag,
                    }
                    if _row_matches_dict(existing, incoming_dict):
                        return
                    raise QprovCollisionError(
                        f"claim id {claim.id!r} already exists with different content.\n"
                        f"  existing: {_dict_for_collision_msg(existing, self._CLAIM_IDENTITY_COLUMNS)}\n"
                        f"  incoming: {incoming_dict}\n"
                        f"Pass force=True to override (older row's history is lost)."
                    )
            if claim.tags:
                conn.execute("DELETE FROM claim_tags WHERE claim_id = ?", (claim.id,))
                conn.executemany(
                    "INSERT INTO claim_tags (claim_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(claim_id, key) DO UPDATE SET value = excluded.value",
                    [(claim.id, k, str(v)) for k, v in claim.tags.items()],
                )

    def get_claim_tags(self, claim_id: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM claim_tags WHERE claim_id = ?", (claim_id,)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def list_claims_by_tag(self, key: str, value: str | None = None) -> list[Claim]:
        with self._connect() as conn:
            if value is None:
                sql = (
                    "SELECT c.* FROM claims c "
                    "JOIN claim_tags t ON t.claim_id = c.id "
                    "WHERE t.key = ? ORDER BY c.created_at ASC"
                )
                rows = conn.execute(sql, (key,)).fetchall()
            else:
                sql = (
                    "SELECT c.* FROM claims c "
                    "JOIN claim_tags t ON t.claim_id = c.id "
                    "WHERE t.key = ? AND t.value = ? ORDER BY c.created_at ASC"
                )
                rows = conn.execute(sql, (key, value)).fetchall()
        return [self._row_to_claim_with_tags(r) for r in rows]

    def _row_to_claim_with_tags(self, row: sqlite3.Row) -> Claim:
        c = _row_to_claim(row)
        c.tags = self.get_claim_tags(c.id)
        return c

    def list_claims(
        self,
        since: str | None = None,
        until: str | None = None,
        computation_id: str | None = None,
        limit: int = 1000,
    ) -> list[Claim]:
        sql = "SELECT * FROM claims"
        wheres: list[str] = []
        params: list[Any] = []
        if since:
            wheres.append("created_at >= ?")
            params.append(since)
        if until:
            wheres.append("created_at <= ?")
            params.append(until)
        if computation_id:
            wheres.append("computation_id = ?")
            params.append(computation_id)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            claims = [_row_to_claim(r) for r in rows]
            for c in claims:
                tag_rows = conn.execute(
                    "SELECT key, value FROM claim_tags WHERE claim_id = ?", (c.id,)
                ).fetchall()
                c.tags = {r["key"]: r["value"] for r in tag_rows}
        return claims

    def get_claim(self, claim_id: str) -> Claim | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM claims WHERE id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                return None
            c = _row_to_claim(row)
            tag_rows = conn.execute(
                "SELECT key, value FROM claim_tags WHERE claim_id = ?", (c.id,)
            ).fetchall()
            c.tags = {r["key"]: r["value"] for r in tag_rows}
        return c

    def referenced_computation_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT computation_id FROM claims WHERE computation_id IS NOT NULL"
            ).fetchall()
        return {r["computation_id"] for r in rows}

    def delete_computation(self, comp_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM computations WHERE id = ?", (comp_id,))
            removed = cur.rowcount > 0
        path = self.payload_path_for(comp_id)
        if path.is_file():
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
        return removed

    def iter_all_computations(self) -> Iterator[Computation]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM computations").fetchall()
            for row in rows:
                tag_rows = conn.execute(
                    "SELECT key, value FROM tags WHERE computation_id = ?", (row["id"],)
                ).fetchall()
                yield _row_to_computation(row, {r["key"]: r["value"] for r in tag_rows})


def _row_to_computation(row: sqlite3.Row, tags: dict[str, str]) -> Computation:
    keys = row.keys()
    cdh = row["canonical_data_hash"] if "canonical_data_hash" in keys else None
    payload_hash = row["payload_hash"] if "payload_hash" in keys else None
    output_hash_algorithm = (
        row["output_hash_algorithm"] if "output_hash_algorithm" in keys else None
    )
    property_results_raw = (
        row["property_results"] if "property_results" in keys else None
    )
    property_results = (
        canonical_loads(property_results_raw) if property_results_raw else None
    )
    return Computation(
        id=row["id"],
        function_name=row["function_name"],
        function_module=row["function_module"],
        input_hash=row["input_hash"],
        output_hash=row["output_hash"],
        code_sha=row["code_sha"],
        code_dirty=bool(row["code_dirty"]) if row["code_dirty"] is not None else None,
        hostname=row["hostname"],
        cpu_model=row["cpu_model"],
        ram_gb=row["ram_gb"],
        gpu_model=row["gpu_model"],
        python_version=row["python_version"],
        sage_version=row["sage_version"],
        os_info=row["os_info"],
        runtime_seconds=row["runtime_seconds"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        status=row["status"],
        error_type=row["error_type"],
        error_message=row["error_message"],
        payload_path=row["payload_path"],
        tags=tags,
        canonical_data_hash=cdh,
        payload_hash=payload_hash,
        output_hash_algorithm=output_hash_algorithm,
        property_results=property_results,
    )


def _row_to_claim(row: sqlite3.Row) -> Claim:
    keys = row.keys()
    paper_tag = row["paper_tag"] if "paper_tag" in keys else None
    unbacked_val = row["unbacked"] if "unbacked" in keys else 0
    return Claim(
        id=row["id"],
        text=row["text"],
        value_numeric=row["value_numeric"],
        computation_id=row["computation_id"],
        created_at=row["created_at"],
        notes=row["notes"],
        paper_tag=paper_tag,
        unbacked=bool(unbacked_val),
    )


def _hash_payload_bytes(raw: bytes) -> str:
    """blake2b hex digest over uncompressed payload bytes.

    Matches :func:`qprov.serialize.hash_text` digest_size so payload
    hashes are comparable with the other hashes in the store.
    """
    h = hashlib.blake2b(digest_size=PAYLOAD_HASH_DIGEST_SIZE)
    h.update(raw)
    return h.hexdigest()


def _row_matches_computation(
    row: sqlite3.Row, comp: Computation, identity_columns: tuple[str, ...]
) -> bool:
    for col in identity_columns:
        if row[col] != getattr(comp, col):
            return False
    return True


def _row_matches_dict(row: sqlite3.Row, incoming: dict[str, Any]) -> bool:
    for col, val in incoming.items():
        if row[col] != val:
            return False
    return True


def _dict_for_collision_msg(
    obj: sqlite3.Row | Computation | Claim,
    identity_columns: tuple[str, ...],
) -> dict[str, Any]:
    if isinstance(obj, sqlite3.Row):
        return {col: obj[col] for col in identity_columns if col in obj.keys()}
    return {col: getattr(obj, col, None) for col in identity_columns}


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp, second precision, suitable for ordering."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
