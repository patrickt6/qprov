"""Canonical JSON serialization with explicit type tags.

The hashing and verification story depends on producing identical bytes for
identical Python values. `json.dumps(sort_keys=True, separators=(',', ':'))`
gets close but cannot encode the math types this project deals with (Sage
Integer, Sage Rational, Sage Laurent series, NumPy ndarray, complex,
fractions.Fraction). For each of those we emit a small `{"__type__": ...}`
envelope and recover it on read.

Sage interop is detected lazily: at import time we try a `sage.rings.integer`
import and fall back to `None`. Tests that don't have Sage installed still
exercise the encode/decode loop on plain ints and floats; the Sage branch is
only taken when a Sage object is actually present in the payload.

Hashing convention: blake2b over the UTF-8 bytes of canonical_dumps(value).
"""
from __future__ import annotations

import base64
import fractions
import hashlib
import json
from typing import Any

try:
    import sage.rings.integer as _sage_int_mod  # type: ignore
    import sage.rings.rational as _sage_rat_mod  # type: ignore
    _SageInteger = _sage_int_mod.Integer
    _SageRational = _sage_rat_mod.Rational
except Exception:
    _SageInteger = None
    _SageRational = None

try:
    import numpy as _np  # type: ignore
except Exception:
    _np = None


_TAG = "__qprov_type__"


def _is_sage_int(x: Any) -> bool:
    return _SageInteger is not None and isinstance(x, _SageInteger)


def _is_sage_rational(x: Any) -> bool:
    return _SageRational is not None and isinstance(x, _SageRational)


def _has_attr_chain(x: Any, *names: str) -> bool:
    return all(hasattr(x, n) for n in names)


def _encode_sage_laurent(x: Any) -> dict | None:
    """Encode Sage Laurent / power series by valuation + dense coefficient list.

    A Laurent series f = sum_{k>=v} c_k q^k is fully represented by
    (valuation, [c_v, c_{v+1}, ...]) up to its known precision. We round-trip
    only the *known* finite slice; precision tags are recorded but not
    reconstructed (the result is a list of integers + a valuation, which is
    enough to verify hashes and re-render claims).
    """
    if not _has_attr_chain(x, "valuation", "list"):
        return None
    cls_name = type(x).__name__
    if "LaurentSeries" not in cls_name and "PowerSeries" not in cls_name:
        return None
    try:
        v = int(x.valuation())
        coeffs = [_to_jsonable(c) for c in x.list()]
        prec = None
        if hasattr(x, "prec"):
            try:
                p = x.prec()
                prec = None if p == float("inf") else int(p)
            except Exception:
                prec = None
        return {
            _TAG: "sage_laurent",
            "valuation": v,
            "coeffs": coeffs,
            "prec": prec,
            "ring": str(x.parent()) if hasattr(x, "parent") else None,
        }
    except Exception:
        return None


def _encode_sage_polynomial(x: Any) -> dict | None:
    """Encode Sage univariate polynomial / rational function via str()."""
    cls_name = type(x).__name__
    if "Polynomial" not in cls_name and "FractionFieldElement" not in cls_name:
        return None
    try:
        return {
            _TAG: "sage_expr",
            "repr": str(x),
            "ring": str(x.parent()) if hasattr(x, "parent") else None,
        }
    except Exception:
        return None


def _to_jsonable(x: Any) -> Any:
    """Convert x into a json-safe value with type tags for non-native types."""
    if x is None or isinstance(x, (bool, int, str)):
        return x
    if isinstance(x, float):
        if x != x or x in (float("inf"), float("-inf")):
            return {_TAG: "float", "repr": repr(x)}
        return x
    if isinstance(x, bytes):
        return {_TAG: "bytes", "b64": base64.b64encode(x).decode("ascii")}
    if isinstance(x, complex):
        return {_TAG: "complex", "real": _to_jsonable(x.real), "imag": _to_jsonable(x.imag)}
    if isinstance(x, fractions.Fraction):
        return {_TAG: "fraction", "num": str(x.numerator), "den": str(x.denominator)}
    if _is_sage_int(x):
        return {_TAG: "sage_int", "value": str(int(x))}
    if _is_sage_rational(x):
        return {_TAG: "sage_rat", "num": str(x.numerator()), "den": str(x.denominator())}
    if _np is not None and isinstance(x, _np.ndarray):
        return {
            _TAG: "ndarray",
            "dtype": str(x.dtype),
            "shape": list(x.shape),
            "data": [_to_jsonable(v) for v in x.flatten().tolist()],
        }
    if _np is not None and isinstance(x, _np.generic):
        return _to_jsonable(x.item())
    if isinstance(x, (list, tuple)):
        out = [_to_jsonable(v) for v in x]
        if isinstance(x, tuple):
            return {_TAG: "tuple", "items": out}
        return out
    if isinstance(x, set):
        return {_TAG: "set", "items": sorted([_to_jsonable(v) for v in x], key=_canonical_key)}
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if not isinstance(k, str):
                return {
                    _TAG: "dict_nonstr_keys",
                    "items": [[_to_jsonable(k), _to_jsonable(v)] for k, v in x.items()],
                }
            out[k] = _to_jsonable(v)
        return out

    laurent = _encode_sage_laurent(x)
    if laurent is not None:
        return laurent
    poly = _encode_sage_polynomial(x)
    if poly is not None:
        return poly

    return {_TAG: "repr", "type": type(x).__module__ + "." + type(x).__name__, "repr": repr(x)}


def _canonical_key(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _from_jsonable(x: Any) -> Any:
    """Reverse of _to_jsonable for the types that round-trip exactly.

    Note: types stored as `{__qprov_type__: 'repr', ...}` or `'sage_expr'` are
    *not* reconstructed - they retain the dict form. Verification compares
    canonical-JSON bytes, which is faithful to the original serialization.
    """
    if isinstance(x, list):
        return [_from_jsonable(v) for v in x]
    if isinstance(x, dict):
        tag = x.get(_TAG)
        if tag is None:
            return {k: _from_jsonable(v) for k, v in x.items()}
        if tag == "float":
            return float(x["repr"])
        if tag == "bytes":
            return base64.b64decode(x["b64"])
        if tag == "complex":
            return complex(_from_jsonable(x["real"]), _from_jsonable(x["imag"]))
        if tag == "fraction":
            return fractions.Fraction(int(x["num"]), int(x["den"]))
        if tag == "sage_int":
            if _SageInteger is not None:
                return _SageInteger(x["value"])
            return int(x["value"])
        if tag == "sage_rat":
            if _SageRational is not None:
                return _SageRational((int(x["num"]), int(x["den"])))
            return fractions.Fraction(int(x["num"]), int(x["den"]))
        if tag == "tuple":
            return tuple(_from_jsonable(v) for v in x["items"])
        if tag == "set":
            return set(_from_jsonable(v) for v in x["items"])
        if tag == "dict_nonstr_keys":
            return {_from_jsonable(k): _from_jsonable(v) for k, v in x["items"]}
        if tag == "ndarray":
            if _np is None:
                return [_from_jsonable(v) for v in x["data"]]
            arr = _np.array([_from_jsonable(v) for v in x["data"]], dtype=x["dtype"])
            return arr.reshape(x["shape"])
        return x
    return x


def canonical_dumps(value: Any) -> str:
    """Stable, sorted-key JSON string for hashing and storage."""
    return json.dumps(
        _to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_loads(text: str) -> Any:
    return _from_jsonable(json.loads(text))


def hash_value(value: Any) -> str:
    """blake2b(16-byte digest) hex of canonical JSON bytes of value."""
    h = hashlib.blake2b(digest_size=16)
    h.update(canonical_dumps(value).encode("utf-8"))
    return h.hexdigest()


def hash_text(text: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(text.encode("utf-8"))
    return h.hexdigest()
