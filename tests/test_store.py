"""Store-layer regressions for v3 (collision + tamper + FK restrict).

Each test exercises one of the four protections introduced by the
v2->v3 schema rework: strict INSERT with collision detection on
computations/claims, payload integrity check on read, ON DELETE
RESTRICT plus CHECK on claims, and idempotency of the v2->v3
migration itself.

The fixture in conftest.py provides an isolated store per test.
"""
from __future__ import annotations

import gzip
import sqlite3

import pytest

import qprov
from qprov import (
    QprovCollisionError,
    PayloadTamperedError,
    register_external,
    tracked,
)
from qprov.store import (
    PAYLOAD_HASH_ALGORITHM,
    Claim,
    Computation,
    Store,
    get_store,
    utc_now_iso,
)


# ---------------------------------------------------------------------------
# Helpers


def _fresh_computation(comp_id: str = "abc123", *, output_hash: str = "out") -> Computation:
    """Construct a minimal Computation row for direct insert tests."""
    return Computation(
        id=comp_id,
        function_name="f",
        function_module=None,
        input_hash="ih",
        output_hash=output_hash,
        code_sha="sha",
        code_dirty=False,
        hostname=None,
        cpu_model=None,
        ram_gb=None,
        gpu_model=None,
        python_version=None,
        sage_version=None,
        os_info=None,
        runtime_seconds=0.0,
        started_at=utc_now_iso(),
        ended_at=utc_now_iso(),
        status="ok",
        error_type=None,
        error_message=None,
        payload_path="<test>",
        canonical_data_hash=None,
        payload_hash="ph",
        output_hash_algorithm=PAYLOAD_HASH_ALGORITHM,
    )


# ---------------------------------------------------------------------------
# Collision detection on computations


def test_collision_raises_on_distinct_content():
    """Two writes that share an id but differ on identity columns must
    raise QprovCollisionError. The pre-v3 INSERT OR REPLACE silently
    clobbered the older row; v3 makes the conflict loud."""
    store = get_store()
    store.insert_computation(_fresh_computation(output_hash="out_A"))
    with pytest.raises(QprovCollisionError) as excinfo:
        store.insert_computation(_fresh_computation(output_hash="out_B"))
    assert "abc123" in str(excinfo.value)
    assert "out_A" in str(excinfo.value)
    assert "out_B" in str(excinfo.value)


def test_collision_same_content_is_noop():
    """A duplicate write with byte-identical identity columns is a
    silent no-op; this preserves register_external's documented
    idempotency contract from v0.2."""
    store = get_store()
    comp = _fresh_computation()
    store.insert_computation(comp)
    store.insert_computation(comp)
    store.insert_computation(comp)
    fetched = store.get_computation("abc123")
    assert fetched is not None
    assert fetched.output_hash == "out"


def test_force_overrides_collision():
    """force=True restores the legacy REPLACE semantics. Reserved for
    explicit overrides where the older row's audit history can be
    discarded."""
    store = get_store()
    store.insert_computation(_fresh_computation(output_hash="out_A"))
    store.insert_computation(
        _fresh_computation(output_hash="out_B"), force=True
    )
    fetched = store.get_computation("abc123")
    assert fetched.output_hash == "out_B"


def test_collision_via_tracked_idempotent_runs():
    """Calling the same @tracked function with the same inputs must
    collapse to a single row without raising QprovCollisionError. This
    is the regression guard for users who rerun their scripts."""
    @tracked
    def add(x, y):
        return x + y

    add(2, 3)
    add(2, 3)
    add(2, 3)

    comps = qprov.find(function="add")
    assert len(comps) == 1
    assert comps[0].output_hash is not None


# ---------------------------------------------------------------------------
# Payload tamper detection


def test_payload_tampering_detected():
    """Hand-editing the gzipped payload between write and read must
    raise PayloadTamperedError. Pre-v3 silently returned the edited
    contents; v3 closes that hole."""
    @tracked
    def make_payload(x):
        return {"value": x}

    make_payload(1)
    comp = qprov.find(function="make_payload")[0]

    store = get_store()
    payload_path = store.payload_path_for(comp.id)
    # Round-trip through gzip with edited contents (mtime=0 so the
    # gzip header bytes do not drift with the wall clock).
    with gzip.open(payload_path, "rb") as f:
        body = f.read().decode("utf-8")
    tampered = body.replace('"value":1', '"value":2')
    assert tampered != body, "test setup failed - replacement did not match"
    with gzip.GzipFile(filename=str(payload_path), mode="wb", mtime=0) as f:
        f.write(tampered.encode("utf-8"))

    with pytest.raises(PayloadTamperedError) as excinfo:
        store.read_payload(comp.id)
    assert comp.id in str(excinfo.value)
    assert "recorded hash" in str(excinfo.value)
    assert "current hash" in str(excinfo.value)


def test_no_verify_skips_integrity_check():
    """The verify_hash=False escape valve must return the (possibly
    tampered) contents without raising. Reserved for forensics."""
    @tracked
    def make_payload(x):
        return {"value": x}

    make_payload(1)
    comp = qprov.find(function="make_payload")[0]
    store = get_store()
    payload_path = store.payload_path_for(comp.id)
    with gzip.open(payload_path, "rb") as f:
        body = f.read().decode("utf-8")
    tampered = body.replace('"value":1', '"value":2')
    with gzip.GzipFile(filename=str(payload_path), mode="wb", mtime=0) as f:
        f.write(tampered.encode("utf-8"))

    # Without --no-verify, read_payload raises.
    with pytest.raises(PayloadTamperedError):
        store.read_payload(comp.id, verify_hash=True)

    # With verify_hash=False, the tampered contents come back as-is.
    body = store.read_payload(comp.id, verify_hash=False)
    assert body["result"] == {"value": 2}


def test_payload_hash_populated_on_write():
    """Every @tracked write must populate payload_hash and
    output_hash_algorithm; the integrity check has nothing to compare
    against otherwise."""
    @tracked
    def f():
        return 42

    f()
    comp = qprov.find(function="f")[0]
    assert comp.payload_hash is not None
    assert len(comp.payload_hash) == 32  # blake2b 16-byte digest -> hex
    assert comp.output_hash_algorithm == PAYLOAD_HASH_ALGORITHM


# ---------------------------------------------------------------------------
# Foreign key + CHECK constraints on claims


def test_fk_restrict_blocks_delete_of_backing_computation():
    """The v3 FK uses ON DELETE RESTRICT (was SET NULL). Deleting a
    computation that backs a paper claim must fail with IntegrityError."""
    cid = register_external(
        function_name="kernel_search_part2_v1",
        inputs={"alpha": "phi"},
        outputs={"kernel_dim": 1},
        code_sha="sha",
    )
    qprov.claim(
        "first nonzero coefficient",
        computation_id=cid,
        tags={"paper": "my-paper"},
    )
    store = get_store()
    with pytest.raises(sqlite3.IntegrityError):
        with store._connect() as conn:
            conn.execute("DELETE FROM computations WHERE id = ?", (cid,))


def test_check_constraint_rejects_orphan_paper_claim():
    """The v3 CHECK constraint enforces paper_tag IS NULL OR
    computation_id IS NOT NULL OR unbacked = 1 at the DB level.
    Inserting a claim that violates it must fail. The Python layer
    in claim() already prevents this at the public API, so this test
    goes through the lower-level Store.insert_claim."""
    store = get_store()
    bad = Claim(
        id="bad_orphan",
        text="paper-bound statement with no backing",
        value_numeric=None,
        computation_id=None,
        created_at=utc_now_iso(),
        notes=None,
        paper_tag="third-paper",
        unbacked=False,
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_claim(bad)


def test_check_constraint_allows_unbacked_paper_claim():
    """A paper-tagged claim with unbacked=1 must satisfy the CHECK."""
    store = get_store()
    ok = Claim(
        id="ok_unbacked",
        text="staged",
        value_numeric=None,
        computation_id=None,
        created_at=utc_now_iso(),
        notes=None,
        paper_tag="third-paper",
        unbacked=True,
    )
    store.insert_claim(ok)
    fetched = store.get_claim("ok_unbacked")
    assert fetched is not None
    assert fetched.unbacked is True


# ---------------------------------------------------------------------------
# Claim collision detection


def test_claim_collision_raises_on_distinct_content():
    cid = register_external(
        function_name="f",
        inputs={"x": 1},
        outputs={"y": 2},
        code_sha="sha",
    )
    qprov.claim(
        "first",
        claim_id="cl_a",
        computation_id=cid,
    )
    store = get_store()
    duplicate = Claim(
        id="cl_a",
        text="DIFFERENT TEXT",
        value_numeric=None,
        computation_id=cid,
        created_at=utc_now_iso(),
        notes=None,
    )
    with pytest.raises(QprovCollisionError):
        store.insert_claim(duplicate)


def test_claim_collision_same_content_is_noop():
    cid = register_external(
        function_name="f",
        inputs={"x": 1},
        outputs={"y": 2},
        code_sha="sha",
    )
    qprov.claim("same", claim_id="cl_b", computation_id=cid)
    qprov.claim("same", claim_id="cl_b", computation_id=cid)
    qprov.claim("same", claim_id="cl_b", computation_id=cid)
    all_claims = get_store().list_claims()
    assert len([c for c in all_claims if c.id == "cl_b"]) == 1


# ---------------------------------------------------------------------------
# Tag tables use ON CONFLICT UPDATE


def test_tag_upsert_via_executemany_works():
    """tags use INSERT ... ON CONFLICT DO UPDATE; re-running the
    executemany on a fresh second computation with the same tag key
    must not raise. (Direct test of the SQL pattern; the no-op
    branch of insert_computation deliberately preserves tag rows
    along with the row metadata so this test exercises the parent
    SQL only.)"""
    store = get_store()
    with store._connect() as conn:
        # Need a parent row to satisfy the FK.
        conn.execute(
            "INSERT INTO computations (id, function_name, input_hash, started_at, payload_path) "
            "VALUES ('tag_test', 'f', 'ih', '2024-01-01T00:00:00Z', '<test>')"
        )
        conn.executemany(
            "INSERT INTO tags (computation_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(computation_id, key) DO UPDATE SET value = excluded.value",
            [("tag_test", "phase", "1")],
        )
        conn.executemany(
            "INSERT INTO tags (computation_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(computation_id, key) DO UPDATE SET value = excluded.value",
            [("tag_test", "phase", "2")],
        )
        row = conn.execute(
            "SELECT value FROM tags WHERE computation_id = 'tag_test' AND key = 'phase'"
        ).fetchone()
        assert row["value"] == "2"


# ---------------------------------------------------------------------------
# Migration v2 -> v3


def test_v2_to_v3_migration_idempotent(tmp_path):
    """Open the same store twice. The second open must be a no-op:
    no new columns added, no rebuild attempted, version stays at 3."""
    root = tmp_path / "iso"
    s1 = Store(root)
    # Snapshot v3 state.
    with s1._connect() as conn:
        cols_1 = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        claims_sql_1 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='claims'"
        ).fetchone()["sql"]
        version_1 = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()["value"]

    # Re-open; migration runs idempotently.
    s2 = Store(root)
    with s2._connect() as conn:
        cols_2 = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        claims_sql_2 = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='claims'"
        ).fetchone()["sql"]
        version_2 = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()["value"]

    assert cols_1 == cols_2
    assert claims_sql_1 == claims_sql_2
    assert version_1 == version_2 == "4"


def test_migration_from_simulated_v2_store(tmp_path):
    """Build a v2-shaped store by hand, then point Store() at it and
    confirm the v2->v3 migration:
      * adds payload_hash and output_hash_algorithm
      * backfills payload_hash from existing payloads
      * rebuilds claims with the RESTRICT FK and CHECK constraint
      * bumps schema_meta.version to 3
    """
    root = tmp_path / "legacy"
    root.mkdir()
    payloads = root / "payloads" / "ab"
    payloads.mkdir(parents=True)

    db_path = root / "qprov.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE computations (
            id TEXT PRIMARY KEY,
            function_name TEXT NOT NULL,
            function_module TEXT,
            input_hash TEXT NOT NULL,
            output_hash TEXT,
            code_sha TEXT,
            code_dirty INTEGER,
            hostname TEXT, cpu_model TEXT, ram_gb REAL, gpu_model TEXT,
            python_version TEXT, sage_version TEXT, os_info TEXT,
            runtime_seconds REAL,
            started_at TEXT NOT NULL, ended_at TEXT,
            status TEXT NOT NULL DEFAULT 'ok',
            error_type TEXT, error_message TEXT,
            payload_path TEXT NOT NULL,
            canonical_data_hash TEXT
        );
        CREATE TABLE tags (
            computation_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
            PRIMARY KEY (computation_id, key),
            FOREIGN KEY (computation_id) REFERENCES computations(id) ON DELETE CASCADE
        );
        CREATE TABLE claims (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            value_numeric REAL,
            computation_id TEXT,
            created_at TEXT NOT NULL,
            notes TEXT,
            FOREIGN KEY (computation_id) REFERENCES computations(id) ON DELETE SET NULL
        );
        CREATE TABLE claim_tags (
            claim_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
            PRIMARY KEY (claim_id, key),
            FOREIGN KEY (claim_id) REFERENCES claims(id) ON DELETE CASCADE
        );
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta (key, value) VALUES ('version', '2');
        """
    )
    payload_path = payloads / "abcdef.json.gz"
    payload_bytes = b'{"id":"abcdef","function_name":"f","args":[],"kwargs":{},"result":1}'
    with gzip.GzipFile(filename=str(payload_path), mode="wb", mtime=0) as f:
        f.write(payload_bytes)
    conn.execute(
        "INSERT INTO computations (id, function_name, input_hash, started_at, payload_path) "
        "VALUES (?, 'f', 'ih', '2024-01-01T00:00:00Z', ?)",
        ("abcdef", str(payload_path)),
    )
    conn.execute(
        "INSERT INTO claims (id, text, created_at) VALUES ('cl_legacy', 'legacy', '2024-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # Open via Store; migration runs (chains v2 -> v3 -> v4).
    s = Store(root)
    with s._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(computations)").fetchall()}
        assert "payload_hash" in cols
        assert "output_hash_algorithm" in cols
        assert "property_results" in cols
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'"
        ).fetchone()["value"]
        assert version == "4"
        claims_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='claims'"
        ).fetchone()["sql"]
        assert "ON DELETE RESTRICT" in claims_sql
        assert "CHECK" in claims_sql
        row = conn.execute(
            "SELECT payload_hash, output_hash_algorithm FROM computations WHERE id = 'abcdef'"
        ).fetchone()
        assert row["payload_hash"] is not None
        assert row["output_hash_algorithm"] == "blake2b"
