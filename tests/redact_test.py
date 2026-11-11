"""Self-test for credential redaction.

The guarantee is narrow and absolute: a MemHub key never leaves the machine
inside a captured transcript. So these assert both halves — that secrets are
removed wherever they hide in a record, and that ordinary content is left
alone, because a redactor that eats real text is one people turn off.

Run: python3 redact_test.py  (stdlib only).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
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
        ("a full git sha, in text that is not a transcript record",
         "merged at 6285d40179074934f43deba53713a7d286cb3d5f"),
        ("an e-mail address, in text that is not a transcript record", "ask dana@example.com"),
        ("a lockfile digest", "integrity sha512-Zk9xQ2pL7vN3mR8tY1wB5cD0eF6gH4iJZk9xQ2pL7vN3mR8tY1wB5cD0eF6gH4iJ"),
        ("a long identifier made of words", "MemHubBackendCaptureRedactionModule2026"),
        ("code that names a token", "token = self.token"),
    ]:
        check(label, r.redact_text(text), text)


# The server's rules, ported (MemHub-Backend app/core/redaction.py). Values
# are made up; each has the shape of the real thing.
VENDOR = [
    ("an Anthropic key", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "sk-ant-<redacted>"),
    ("a GitHub token", "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "ghp_<redacted>"),
    ("an AWS access key id", "AKIAIOSFODNN7EXAMPLE", "AKIA<redacted>"),
    ("a Slack token", "xoxb-123456789012-abcdefghijkl", "xoxb-<redacted>"),
]


def test_the_servers_rules_run_on_this_machine():
    print("\nthe server's rules run here, before upload")
    for label, secret, crumb in VENDOR:
        out = r.redact_text(f"export KEY={secret} done")
        check(f"{label}: value gone", secret in out, False)
    out = r.redact_text("use sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 here")
    check("a vendor key keeps its prefix as a breadcrumb", "sk-ant-<redacted>" in out, True)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    check("a JWT", r.redact_text(f"Authorization: Bearer {jwt}"), "Authorization: Bearer <redacted-jwt>")
    check("a DSN password",
          r.redact_text("postgres://app:hunter2secret@db.internal:5432/prod"),
          "postgres://app:<redacted>@db.internal:5432/prod")
    check("an ENV-style secret assignment",
          r.redact_text("AUTH_JWT_SECRET=correct-horse-battery"), "AUTH_JWT_SECRET=<redacted>")
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAx7Zk9xQ2pL7vN3mR8tY1wB5c\n-----END RSA PRIVATE KEY-----"
    out = r.redact_text(pem)
    check("a PEM private key body", "MIIEowIBAAKCAQEA" in out, False)
    check("…its BEGIN and END lines stay", out.startswith("-----BEGIN RSA PRIVATE KEY-----<redacted>")
          and out.endswith("-----END RSA PRIVATE KEY-----"), True)
    bare = "Zk9xQ2pL7vN3mR8tY1wB5cD0eF6gH4iJ"       # no prefix, no name: entropy alone
    check("a bare high-entropy token (Shannon entropy)", r.redact_text(f"the key is {bare}"),
          "the key is <redacted>")
    check("entropy is what decides", r.shannon_entropy(bare) >= 4.0
          and r.shannon_entropy("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == 0.0, True)
    for text in ["AUTH_JWT_SECRET=correct-horse-battery", f"k {bare}", pem,
                 "postgres://app:hunter2secret@db.internal:5432/prod", f"Bearer {jwt}"]:
        once = r.redact_text(text)
        check(f"idempotent: {text[:24]!r}", r.redact_text(once), once)


def test_transcript_records_get_capture_strength():
    print("\ntranscript records: capture strength, identifiers kept")
    dd = "0123456789abcdef0123456789abcdef"            # a Datadog key is 32 hex
    rec = {"type": "user", "uuid": "6285d40179074934f43deba53713a7d2",
           "sessionId": "0123456789abcdef0123456789abcdef", "cwd": "/Users/dana/repo",
           "gitBranch": "fm/topic", "timestamp": "2026-11-10T00:00:00Z",
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "toolu_01AbCdEfGhIjKlMnOpQrStUv",
                "content": f"DD_API_KEY={dd}\nbare {dd}\nmail dana@example.com\ncard 4111 1111 1111 1111"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"}}]}}
    out = r.redact_records([rec])[0]
    text = out["message"]["content"][0]["content"]
    check("a named hex key", f"DD_API_KEY={dd}" in text, False)
    check("a bare 32-hex token", dd in text, False)
    check("an e-mail address", "dana@example.com" in text, False)
    check("a card number", "4111 1111 1111 1111" in text, False)
    check("the record's uuid is untouched", out["uuid"], rec["uuid"])
    check("a 32-hex session id is untouched", out["sessionId"], rec["sessionId"])
    check("cwd and branch are untouched", (out["cwd"], out["gitBranch"]), (rec["cwd"], rec["gitBranch"]))
    check("the tool-call id is untouched", out["message"]["content"][0]["tool_use_id"],
          rec["message"]["content"][0]["tool_use_id"])
    check("image bytes are untouched", out["message"]["content"][1]["source"]["data"],
          rec["message"]["content"][1]["source"]["data"])
    check("idempotent, so the server's pass changes nothing", r.redact_records([out])[0], out)
    clean = {"type": "assistant", "uuid": "u1", "message": {"role": "assistant", "content": "ran the tests, 12 passed"}}
    check("a clean record comes back equal", r.redact_records([clean])[0], clean)
    check("the input is not mutated", rec["message"]["content"][0]["content"].startswith("DD_API_KEY=0123"), True)


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
    # Deliberately hostile: an object json can't serialize, a non-string dict key.
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


def test_identities_for_a_model_bound_window():
    """`redact_identities` is for the harness window, NOT for captured
    records: it over-redacts on purpose, and the archive must not."""
    print("\nidentities (window only)")
    check("home dir becomes ~",
          r.redact_identities("cd /Users/colleague/dev/x && ls /home/dana/y"),
          "cd ~/dev/x && ls ~/y")
    check("windows home too", r.redact_identities(r"C:\Users\dana\dev"), "~\\dev")
    check("email becomes <email>",
          r.redact_identities("owner dana.k+x@example.co.uk here"), "owner <email> here")
    check("a URL path segment is not a home dir",
          r.redact_identities("https://h.io/Users/list"), "https://h.io/Users/list")
    check("already ~ is untouched", r.redact_identities("~/xtrace/ok"), "~/xtrace/ok")
    check("root's home too",
          r.redact_identities("cat /root/private-project/file"), "cat ~/private-project/file")
    check("a path that merely contains root is not a home",
          r.redact_identities("/rooted/x /srv/root/x"), "/rooted/x /srv/root/x")
    saved = os.environ.get("HOME")
    os.environ["HOME"] = "/github/home"
    try:
        check("this machine's own home, wherever it lives",
              r.redact_identities("ls /github/home/work /github/homework"),
              "ls ~/work /github/homework")
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
    check("empty in, empty out", r.redact_identities(""), "")
    rec = {"t": "/Users/colleague/dev/x dana@example.com"}
    check("capture redaction leaves identities alone", r.redact(rec), rec)


if __name__ == "__main__":
    for test in (test_secrets_are_removed, test_ordinary_content_survives,
                 test_the_servers_rules_run_on_this_machine,
                 test_transcript_records_get_capture_strength,
                 test_structure_is_preserved, test_never_raises, test_batch,
                 test_identities_for_a_model_bound_window):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall redact checks passed")
