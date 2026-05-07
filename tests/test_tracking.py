"""@tracked decorator: records correct row, hashes, payload, captures errors."""
from __future__ import annotations

import pytest

import qprov
from qprov import tracked
from qprov.serialize import hash_value
from qprov.store import get_store


def test_decorator_returns_value_unchanged():
    @tracked
    def add(x, y):
        return x + y

    assert add(2, 3) == 5
    assert add(10, 20) == 30


def test_one_call_writes_one_row_with_correct_hashes():
    @tracked
    def square(n):
        return n * n

    out = square(7)
    assert out == 49

    comps = qprov.find()
    assert len(comps) == 1
    c = comps[0]
    assert c.function_name == "square"
    assert c.status == "ok"
    assert c.input_hash == hash_value({"args": [7], "kwargs": {}})
    assert c.output_hash == hash_value(49)
    assert c.runtime_seconds is not None and c.runtime_seconds >= 0


def test_payload_contains_args_kwargs_and_result():
    @tracked
    def shout(word, suffix="!"):
        return word.upper() + suffix

    shout("hi", suffix="?!")

    c = qprov.find()[0]
    payload = get_store().read_payload(c.id)
    assert payload["args"] == ["hi"]
    assert payload["kwargs"] == {"suffix": "?!"}
    assert payload["result"] == "HI?!"
    assert "function_source" in payload


def test_id_is_idempotent_on_repeat():
    """Same function + same inputs + same code should collapse to same id."""
    @tracked
    def double(x):
        return 2 * x

    double(11)
    double(11)
    double(11)
    comps = qprov.find()
    assert len(comps) == 1, "repeats should collapse to one row"


def test_different_inputs_create_different_rows():
    @tracked
    def double(x):
        return 2 * x

    double(1)
    double(2)
    double(3)
    comps = qprov.find()
    assert len(comps) == 3
    assert len({c.id for c in comps}) == 3


def test_tags_recorded():
    @tracked(tags={"experiment": "G1.2", "constant": "pi"})
    def f(N):
        return N * N

    f(10)
    c = qprov.find(tags={"constant": "pi"})
    assert len(c) == 1
    assert c[0].tags == {"experiment": "G1.2", "constant": "pi"}


def test_exception_is_recorded_and_reraised():
    @tracked
    def bad():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        bad()

    comps = qprov.find()
    assert len(comps) == 1
    c = comps[0]
    assert c.status == "error"
    assert c.error_type == "ValueError"
    assert c.error_message == "nope"
    payload = get_store().read_payload(c.id)
    assert payload["error"]["type"] == "ValueError"
    assert "Traceback" in payload["error"]["traceback"]


def test_stdout_is_captured_in_payload():
    @tracked
    def noisy():
        print("hello world")
        return 1

    noisy()
    c = qprov.find()[0]
    payload = get_store().read_payload(c.id)
    assert "hello world" in payload["stdout"]


def test_unavailable_source_does_not_crash():
    """A function defined via exec / lambda has no inspect.getsource."""
    src = "from qprov import tracked\n@tracked\ndef anon(x): return x + 1\n"
    ns: dict = {}
    exec(compile(src, "<dynamic>", "exec"), ns)
    anon = ns["anon"]
    assert anon(5) == 6
    c = qprov.find()[0]
    payload = get_store().read_payload(c.id)
    assert payload["function_source"] == "<unavailable>"


def test_hardware_fields_populated():
    @tracked
    def f():
        return 1
    f()
    c = qprov.find()[0]
    assert c.hostname  # always populated
    assert c.python_version  # always populated
    # cpu_model and ram_gb may be None on locked-down systems, that's OK
