"""Self-test for credential redaction.

The guarantee is narrow and absolute: a MemHub key never leaves the machine
inside a captured transcript. So these assert both halves — that secrets are
removed wherever they hide in a record, and that ordinary content is left
alone, because a redactor that eats real text is one people turn off.

Run: python3 redact_test.py  (stdlib only).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact as r  # noqa: E402

# Shaped like the real thing: mhk_ + 43 chars, as observed from the mint API.
SECRET = "mhk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0Uv2"
XTK = "xtk_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2H1g0F1e2"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def test_secrets_are_removed():
    print("\nsecrets are removed")
    for label, text in [
        ("bare", SECRET),
        ("export line", f"export MEMHUB_TOKEN={SECRET}"),
        ("shell with quotes", f'export MEMHUB_API_KEY="{SECRET}"'),
        ("inside a curl", f"curl -H 'Authorization: Bearer {SECRET}' https://x"),
        ("in prose", f"here is my key {SECRET} please don't share"),
        ("data-plane key", f"XTRACE_KEY={XTK}"),
        ("two on one line", f"{SECRET} and {XTK}"),
    ]:
        check(label, r.contains_secret(r.redact_text(text)), False)

    # The prefix survives, so an accidental paste stays VISIBLE and fixable
    # rather than vanishing into an indistinguishable blank.
    check("prefix is kept as a breadcrumb",
          "mhk_<redacted>" in r.redact_text(f"key={SECRET}"), True)


def test_ordinary_content_survives():
    print("\nordinary content survives")
    for label, text in [
        ("plain prose", "we decided to ship the capture fix on Friday"),
        ("a uuid", "session 32ec4261-fe83-4621-a8d1-dcd6bf3ebb1c"),
        ("a git sha", "commit 613e007 fixed it"),
        ("prefix mentioned in docs", "keys look like mhk_… in the guide"),
        ("prefix with a short tail", "mhk_short"),
        ("a path", "/Users/x/.config/memhub-plugin/pak-api.host.json"),
        ("base64ish but unprefixed", "ZXlKaGJHY2lPaUpTVXpJMU5pSXNJblI1Y0NJNiJ9"),
    ]:
        check(label, r.redact_text(text), text)


def test_structure_is_preserved():
    print("\nstructure is preserved")
    record = {
        "type": "user",
        "uuid": "abc-123",
        "message": {"role": "user",
                    "content": [{"type": "text", "text": f"run: export K={SECRET}"}]},
        "meta": {"nested": [{"deep": SECRET}], "count": 7, "ok": True, "nil": None},
    }
    out = r.redact(record)
    check("no secret anywhere", r.contains_secret(out), False)
    check("shape is identical", type(out), dict)
    check("scalars untouched", out["meta"]["count"], 7)
    check("booleans untouched", out["meta"]["ok"], True)
    check("nulls untouched", out["meta"]["nil"], None)
    check("uuid untouched", out["uuid"], "abc-123")
    check("role untouched", out["message"]["role"], "user")
    check("surrounding text kept",
          out["message"]["content"][0]["text"].startswith("run: export K=mhk_"), True)

    # A key is just as capturable in a key position as in a value.
    keyed = r.redact({SECRET: "value"})
    check("dict keys are redacted too", r.contains_secret(keyed), False)


def test_never_raises():
    print("\nnever raises")
    # Deliberately hostile: an object json can't handle, and a self-reference.
    class Odd:
        pass
    weird = [Odd(), {1: "x"}, (SECRET,), None]
    out = r.redact_records(weird)
    check("odd input does not raise", isinstance(out, list), True)
    check("tuple secret still redacted", r.contains_secret(out[2]), False)

    # The contract on failure is "return the records", never "drop them".
    check("batch is never emptied", len(r.redact_records([{"a": 1}])), 1)


def test_batch():
    print("\nbatch")
    records = [{"text": f"k={SECRET}"}, {"text": "clean"}]
    out = r.redact_records(records)
    check("count preserved", len(out), 2)
    check("secret gone", r.contains_secret(out), False)
    check("clean record identical", out[1]["text"], "clean")


if __name__ == "__main__":
    for test in (test_secrets_are_removed, test_ordinary_content_survives,
                 test_structure_is_preserved, test_never_raises, test_batch):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall redact checks passed")
