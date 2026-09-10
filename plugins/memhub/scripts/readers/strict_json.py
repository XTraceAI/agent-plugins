"""Opt-in JSON decoding for native export without changing tolerant capture."""
import json
import math


def _constant(_value):
    raise ValueError("non-standard JSON constant")


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number exceeds finite range")
    return parsed


def loads(value, *, strict=False):
    if strict:
        return json.loads(value, parse_constant=_constant, parse_float=_finite_float)
    return json.loads(value)
