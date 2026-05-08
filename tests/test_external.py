"""register_external: retroactive registration of pre-existing JSON outputs."""
from __future__ import annotations

import json

import pytest

import qprov
from qprov import register_external
from qprov.serialize import hash_value
from qprov.store import get_store


def test_basic_registration_creates_visible_row():
    cid = register_external(
        function_name="kernel_search_part2_v1",
        inputs={"alpha": "cbrt2", "d_X": 3, "d_q": 12, "N": 400},
        outputs={"kernel_dim": 0, "kernel_basis": []},
        code_path="scripts/search.sage",
        code_sha="abc123",
        runtime_seconds=42.5,
    )
    comps = qprov.find()
    assert len(comps) == 1
    c = comps[0]
    assert c.id == cid
    assert c.function_name == "kernel_search_part2_v1"
    assert c.status == "ok"
    assert c.runtime_seconds == 42.5
    assert c.code_sha == "abc123"


def test_input_and_output_hashes_match_canonical_form():
    inputs = {"alpha": "cbrt2", "d_X": 3, "d_q": 12}
    outputs = {"kernel_dim": 1, "kernel_basis": ["x^3 - 2"]}
    cid = register_external(
        function_name="f",
        inputs=inputs,
        outputs=outputs,
        code_sha="sha",
    )
    c = qprov.get(cid)
    assert c.input_hash == hash_value(inputs)
    assert c.output_hash == hash_value(outputs)


def test_outputs_none_leaves_output_hash_none():
    cid = register_external(
        function_name="f",
        inputs={"x": 1},
        outputs=None,
        code_sha="sha",
    )
    c = qprov.get(cid)
    assert c.output_hash is None


def test_repeated_registration_is_idempotent():
    """Same function + inputs + code_sha must collapse to one row."""
    kwargs = dict(
        function_name="kernel_search_part1_v1",
        inputs={"alpha": "phi", "d_X": 2, "d_q": 2},
        outputs={"kernel_dim": 1},
        code_sha="deadbeef",
    )
    id1 = register_external(**kwargs)
    id2 = register_external(**kwargs)
    id3 = register_external(**kwargs)
    assert id1 == id2 == id3
    assert len(qprov.find()) == 1


def test_different_inputs_make_different_rows():
    base = dict(function_name="f", outputs={"k": 0}, code_sha="sha")
    register_external(inputs={"d_X": 3, "d_q": 12}, **base)
    register_external(inputs={"d_X": 3, "d_q": 16}, **base)
    register_external(inputs={"d_X": 4, "d_q": 16}, **base)
    assert len(qprov.find()) == 3


def test_different_code_sha_makes_different_rows():
    base = dict(function_name="f", inputs={"x": 1}, outputs={"k": 0})
    register_external(code_sha="sha1", **base)
    register_external(code_sha="sha2", **base)
    assert len(qprov.find()) == 2


def test_tags_are_persisted_and_filterable():
    register_external(
        function_name="kernel_search_part2_v1",
        inputs={"alpha": "cbrt2"},
        outputs={"kernel_dim": 0},
        code_sha="sha",
        tags={"paper": "cubic-negative", "phase": "part-2", "retroactive": True},
    )
    register_external(
        function_name="kernel_search_part1_v1",
        inputs={"alpha": "phi"},
        outputs={"kernel_dim": 1},
        code_sha="sha",
        tags={"paper": "cubic-negative", "phase": "part-1", "retroactive": True},
    )
    p2 = qprov.find(tags={"phase": "part-2"})
    assert len(p2) == 1
    assert p2[0].tags["phase"] == "part-2"
    assert p2[0].tags["retroactive"] == "True"


def test_payload_round_trips_inputs_outputs_and_metadata():
    inputs = {"alpha": "cbrt2", "d_X": 3, "d_q": 12, "N": 400}
    outputs = {"kernel_dim": 0, "kernel_basis": []}
    cid = register_external(
        function_name="kernel_search_part2_v1",
        inputs=inputs,
        outputs=outputs,
        code_path="scripts/search.sage",
        code_sha="sha",
        source_file="data/search/cbrt2/cell_3_12.json",
        notes="retroactive ingest, runtime not recorded in source JSON",
    )
    payload = get_store().read_payload(cid)
    assert payload["inputs"] == inputs
    assert payload["outputs"] == outputs
    assert payload["external"] is True
    assert payload["source_file"].endswith("cell_3_12.json")
    assert payload["code_path"] == "scripts/search.sage"
    assert payload["notes"] == "retroactive ingest, runtime not recorded in source JSON"


def test_register_from_real_json_file(tmp_path):
    """End-to-end: load a search-output JSON, hand it to register_external."""
    cell_json = {
        "alpha": "cbrt2",
        "d_X": 3,
        "d_q": 12,
        "d_q_minus": 0,
        "d_q_plus": 12,
        "B": 12,
        "N": 400,
        "kernel_ring": "QQ",
        "kernel_dim": 0,
        "kernel_basis": [],
        "runtime_seconds": 17.3,
    }
    src = tmp_path / "cell_3_12.json"
    src.write_text(json.dumps(cell_json), encoding="utf-8")

    data = json.loads(src.read_text(encoding="utf-8"))
    input_keys = ("alpha", "d_X", "d_q", "d_q_minus", "d_q_plus", "B", "N", "kernel_ring")
    cid = register_external(
        function_name="kernel_search_part2_v1",
        inputs={k: data[k] for k in input_keys},
        outputs={"kernel_dim": data["kernel_dim"], "kernel_basis": data["kernel_basis"]},
        code_path="scripts/search.sage",
        code_sha="HEAD",
        runtime_seconds=data.get("runtime_seconds", 0.0),
        tags={"paper": "cubic-negative", "phase": "part-2", "alpha": data["alpha"], "retroactive": True},
        source_file=str(src),
    )
    c = qprov.get(cid)
    assert c is not None
    assert c.function_name == "kernel_search_part2_v1"
    assert c.tags["alpha"] == "cbrt2"
    assert c.runtime_seconds == 17.3
    payload = get_store().read_payload(cid)
    assert payload["inputs"]["d_X"] == 3
    assert payload["outputs"]["kernel_dim"] == 0


def test_rejects_non_dict_inputs():
    with pytest.raises(ValueError):
        register_external(
            function_name="f",
            inputs=[1, 2, 3],  # type: ignore[arg-type]
            code_sha="sha",
        )


def test_rejects_empty_function_name():
    with pytest.raises(ValueError):
        register_external(
            function_name="",
            inputs={},
            code_sha="sha",
        )


def test_claim_can_link_to_external_computation():
    """The retroactive-claim flow described in Experiment 2."""
    cid = register_external(
        function_name="kernel_search_part2_v1",
        inputs={"alpha": "cbrt2", "d_X": 3, "d_q": 12, "N": 400},
        outputs={"kernel_dim": 0},
        code_sha="sha",
        tags={"paper": "cubic-negative"},
    )
    claim_id = qprov.claim(
        "no P(X, q) of bidegree at most (3, 12) annihilates [cbrt2]_q modulo q^400",
        computation_id=cid,
    )
    linked = get_store().list_claims(computation_id=cid)
    assert len(linked) == 1
    assert linked[0].id == claim_id
