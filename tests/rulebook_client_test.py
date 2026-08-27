"""Rulebook hook ↔ server: book fetch + fire flush against a FAKE server.

The server is a stdlib ``http.server`` that records every request and serves
canned replies (200+ETag, 304, 500, slow, or nothing at all — connection
refused). What is asserted is the client contract from the spec (§4.1, §4.3,
§5.3, §5.4):

* fetch sends ``status=active&repo=<repo>&view=hook`` with the bearer, caches
  ``{etag, fetched_at, rules}``, and revalidates with ``If-None-Match``;
* server rules merge with the local book, local wins on id collision, and the
  merge audit names each rule's source;
* offline / 500 / slow keep the LAST book; no book at all → local only;
* the session lane and the tool-call lanes never wait on the network — the
  pre lane makes zero requests and its latency with the server down is bounded;
* flush POSTs the v2 rows minus ``excerpt``, advances a watermark only on a
  2xx, retries a failed batch with the SAME ``fire_id``, re-sends a converted
  fire as an update, logs ``rejected`` rows locally and never retries them,
  and honours the every-N / every-M throttle unless ``final``;
* ``mode: gate`` from a cache older than 24 h degrades to advise, said once.

Run: python3 tests/rulebook_client_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK = os.path.join(os.path.dirname(__file__), "..",
                    "plugins", "memhub", "scripts", "rulebook_hook.py")
FAILURES: list[str] = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class Fake:
    """Canned server. ``mode`` ∈ ok | 304 | 500 | slow; ``requests`` records all."""

    def __init__(self):
        self.mode = "ok"
        self.etag = '"v1"'
        self.rules = []
        self.post_reply = {"accepted": None, "rejected": 0}   # None → filled with the batch size
        self.recalls = []
        self.recall_reply = []
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8") if n else ""
                fake.requests.append({"method": self.command, "path": self.path,
                                      "headers": dict(self.headers), "body": body})
                return body

            def _send(self, status, obj=None, etag=None):
                self.send_response(status)
                if etag:
                    self.send_header("ETag", etag)
                data = json.dumps(obj).encode() if obj is not None else b""
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._record()
                if fake.mode == "slow":
                    time.sleep(3)
                if fake.mode == "500":
                    return self._send(500, {"code": 1, "msg": "boom"})
                if fake.mode == "304" or self.headers.get("If-None-Match") == fake.etag:
                    return self._send(304, None, fake.etag)
                # REST envelope on GET; bare on POST — both must decode
                self._send(200, {"code": 0, "msg": "ok",
                                 "data": {"rules": fake.rules}}, fake.etag)

            def do_POST(self):
                body = self._record()
                if fake.mode == "slow":
                    time.sleep(3)
                if fake.mode == "500":
                    return self._send(500, {"detail": "boom"})
                if self.path.endswith("/recall"):
                    req = json.loads(body or "{}")
                    fake.recalls.append(req)
                    kept = [r for r in fake.recall_reply if r["rule_id"] not in (req.get("already_fired") or [])]
                    return self._send(200, {"code": 0, "msg": "ok", "data": {"rules": kept, "judge": "gate"}})
                n = len(json.loads(body or "{}").get("fires", []))
                rep = dict(fake.post_reply)
                if "accepted" in rep and rep["accepted"] is None:
                    rep["accepted"] = n
                elif rep.get("accepted") == "rest":       # everything the reply did not reject
                    rj = rep.get("rejected")
                    rep["accepted"] = n - (len(rj) if isinstance(rj, list) else int(rj or 0))
                self._send(202, rep)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def posts(self):
        return [json.loads(r["body"]) for r in self.requests if r["method"] == "POST"]


def run(mode, payload, env, extra_args=()):
    p = subprocess.run([sys.executable, HOOK, mode, *extra_args], input=json.dumps(payload),
                       capture_output=True, text=True, env=dict(os.environ, **env), timeout=60)
    if p.stderr.strip():
        print("STDERR:", p.stderr[-800:])
    return p.returncode, p.stdout


def ctx(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def jl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []



def _anchor_lane_checks(check, run, ctx):
    """§4.7 — anchor rules go through the server judge; everything fails open.
    Own fake server + temp dir: this lane must not inherit the flush/fetch
    state the earlier checks leave behind."""
    fake = Fake()
    fake.mode = "ok"
    fake.etag = '"v-anchor"'
    fake.rules = [
        {"rule_id": "a-bus", "title": "bus", "statement": "Mind the context bus.", "delivery": "anchor_recall",
         "status": "active", "mode": "advise", "version": 1, "scope_repos": [], "anchors": ["context_bus.py"]},
        {"rule_id": "s-post", "title": "posture", "statement": "POSTURE LINE", "delivery": "session_context",
         "status": "active", "mode": "advise", "version": 1, "scope_repos": []},
    ]
    fake.recall_reply = [{"rule_id": "a-bus", "title": "bus", "statement": "Mind the context bus.",
                          "version": 1, "anchors": ["context_bus.py"]}]
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "xmem")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/b\n")
        book = os.path.join(td, "rulebook.json")
        with open(book, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rules": []}, f)
        env = {"MEMHUB_RULEBOOK": book, "MEMHUB_TOKEN": "tok-123",
               "MEMHUB_MCP_BASE_URL": f"http://127.0.0.1:{fake.port}",
               "MEMHUB_RULEBOOK_FETCH": "0", "MEMHUB_RULEBOOK_TIMEOUT_S": "1"}
        run("fetch", {"cwd": repo}, env)
        rc, out = run("session", {"cwd": repo, "session_id": "an1"}, env)
        check("anchor: session_context rule from the server book is served at SessionStart", "POSTURE LINE" in ctx(out))
        ev = {"cwd": repo, "session_id": "an1", "tool_name": "Edit",
              "tool_input": {"file_path": repo + "/xmem/context_bus.py", "new_string": "SECRET BODY"}}
        rc, out = run("pre", ev, env)
        last = fake.recalls[-1] if fake.recalls else {}
        check("anchor: kept rule injected; POST /recall carried the file handle only (no body)",
              "[a-bus]" in ctx(out) and last.get("args") == {"file_path": repo + "/xmem/context_bus.py"}
              and "SECRET BODY" not in json.dumps(last), str(last))
        n = len(fake.recalls)
        rc, out = run("pre", ev, env)
        check("anchor: fired once per session — second call makes no recall call and injects nothing",
              out.strip() == "" and len(fake.recalls) == n)
        rc, out = run("pre", {"cwd": repo, "session_id": "an2", "tool_name": "Read", "tool_input": {"file_path": "x"}}, env)
        check("anchor: a tool with no handle → no recall call", len(fake.recalls) == n)
        rc, out = run("pre", dict(ev, session_id="an2b"), env)
        check("anchor: a fresh session re-asks and sends its own already_fired (empty)",
              "[a-bus]" in ctx(out) and fake.recalls[-1]["already_fired"] == [])
        fake.mode = "500"
        rc, out = run("pre", dict(ev, session_id="an3"), env)
        check("anchor: server 500 → silent, exit 0", rc == 0 and out.strip() == "")
        fake.mode = "slow"
        import time as _t; t0 = _t.time()
        rc, out = run("pre", dict(ev, session_id="an4"), env)
        check("anchor: slow judge → fail open within the hook budget", rc == 0 and out.strip() == "" and _t.time() - t0 < 4.5)
        fake.mode = "ok"
        rows = jl(os.path.join(td, "ledger", "fires.jsonl"))
        check("anchor: the kept rule is logged like any other fire",
              any(r["rule_id"] == "a-bus" and r["hook_phase"] == "pre" for r in rows))
    fake.srv.shutdown()

def main():
    fake = Fake()
    fake.rules = [
        {"rule_id": "srv-bash", "title": "Server bash rule", "statement": "SERVER BASH TEXT",
         "mode": "advise", "version": 3, "status": "active", "scope_repos": [],
         "matcher": {"event": "bash", "command_rx": r"server-only-cmd", "warn_once_per": "session"}},
        {"rule_id": "shared-id", "statement": "SERVER WINS?", "mode": "advise", "version": 1,
         "scope_repos": ["xmem"], "matcher": {"event": "bash", "command_rx": r"shared-cmd"}},
        {"rule_id": "srv-gate", "statement": "GATE TEXT", "mode": "gate", "version": 2,
         "scope_repos": [], "matcher": {"event": "bash", "command_rx": r"gated-cmd"}},
    ]
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "xmem")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/b\n")
        book = os.path.join(td, "rulebook.json")
        with open(book, "w", encoding="utf-8") as f:
            json.dump({"version": "pilot-t", "rules": [
                {"id": "local-rule", "on": "bash", "rx": r"local-cmd", "fire_scope": "session",
                 "repo_scope": "any", "text": "LOCAL TEXT", "why": "w", "converted_rx": r"do-it"},
                {"id": "shared-id", "on": "bash", "rx": r"shared-cmd", "fire_scope": "session",
                 "repo_scope": "any", "text": "LOCAL WINS", "why": "w"},
            ]}, f)
        env = {"MEMHUB_RULEBOOK": book, "MEMHUB_TOKEN": "tok-123",
               "MEMHUB_MCP_BASE_URL": f"http://127.0.0.1:{fake.port}",
               "MEMHUB_RULEBOOK_FETCH": "0", "MEMHUB_RULEBOOK_TIMEOUT_S": "1"}
        import hashlib
        cache = os.path.join(td, "book", f"xmem-{hashlib.sha1(b'xmem').hexdigest()[:8]}.json")
        base = {"cwd": repo, "session_id": "s1", "tool_name": "Bash"}

        # ── fetch ───────────────────────────────────────────────────────
        rc, out = run("fetch", {"cwd": repo}, env)
        check("fetch: silent exit-0", rc == 0 and out == "")
        req = fake.requests[-1]
        check("fetch: GET /v1/team/rulebook/rules?status=active&repo=xmem&view=hook",
              req["method"] == "GET" and req["path"].startswith("/v1/team/rulebook/rules?")
              and all(k in req["path"] for k in ("status=active", "repo=xmem", "view=hook")), req["path"])
        check("fetch: bearer from the plugin's credential path",
              req["headers"].get("Authorization") == "Bearer tok-123")
        b = json.load(open(cache, encoding="utf-8"))
        check("fetch: cache {etag, fetched_at, rules}",
              b["etag"] == '"v1"' and len(b["rules"]) == 3 and b["fetched_at"])
        first_at = b["fetched_at"]
        time.sleep(1.1)
        run("fetch", {"cwd": repo}, env)
        req = fake.requests[-1]
        b2 = json.load(open(cache, encoding="utf-8"))
        check("fetch: revalidates with If-None-Match; 304 keeps rules, touches fetched_at",
              req["headers"].get("If-None-Match") == '"v1"' and b2["rules"] == b["rules"]
              and b2["fetched_at"] > first_at, str(req["headers"].get("If-None-Match")))

        # ── merge ───────────────────────────────────────────────────────
        rc, out = run("pre", dict(base, tool_input={"command": "server-only-cmd"}), env)
        check("merge: a server rule fires from the cache", "SERVER BASH TEXT" in ctx(out), out)
        rc, out = run("pre", dict(base, tool_input={"command": "shared-cmd"}), env)
        check("merge: local wins on id collision", "LOCAL WINS" in ctx(out) and "SERVER WINS" not in ctx(out))
        run("session", {"cwd": repo, "session_id": "s1"}, env)
        src = json.load(open(cache + ".sources", encoding="utf-8"))["sources"]
        check("merge: audit names each rule's source",
              src == {"local-rule": "local", "shared-id": "local", "srv-bash": "server", "srv-gate": "server"}, str(src))
        rows = jl(os.path.join(td, "ledger", "fires.jsonl"))
        srv = next(r for r in rows if r["rule_id"] == "srv-bash")
        check("ledger: a server rule's fire carries the server's rule version", srv["rule_version"] == 3)

        # ── failure semantics: keep the last book ───────────────────────
        before = open(cache, encoding="utf-8").read()
        for mode in ("500", "slow"):
            fake.mode = mode
            rc, out = run("fetch", {"cwd": repo}, env)
            check(f"fetch: {mode} → silent, cache untouched",
                  rc == 0 and out == "" and open(cache, encoding="utf-8").read() == before)
        fake.mode = "ok"
        down = dict(env, MEMHUB_MCP_BASE_URL="http://127.0.0.1:1")     # nothing listens
        rc, out = run("fetch", {"cwd": repo}, down)
        check("fetch: connection refused → silent, cache untouched",
              rc == 0 and out == "" and open(cache, encoding="utf-8").read() == before)
        rc, out = run("pre", dict(base, session_id="off", tool_input={"command": "server-only-cmd"}), down)
        check("offline session keeps the last book (server rule still fires)", "SERVER BASH TEXT" in ctx(out))
        nocache = dict(env, MEMHUB_RULEBOOK=os.path.join(td, "nocache", "rulebook.json"))
        os.makedirs(os.path.join(td, "nocache"))
        with open(nocache["MEMHUB_RULEBOOK"], "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rules": [{"id": "l", "on": "bash", "rx": "zzz", "text": "ONLY LOCAL", "why": "w"}]}, f)
        rc, out = run("pre", dict(base, tool_input={"command": "zzz server-only-cmd"}), nocache)
        check("no cache → local book only", "ONLY LOCAL" in ctx(out) and "SERVER" not in ctx(out))
        os.remove(nocache["MEMHUB_RULEBOOK"])
        rc, out = run("pre", dict(base, tool_input={"command": "zzz"}), nocache)
        check("no cache and no local book → silent", rc == 0 and out == "")

        # ── latency: tool-call lanes never touch the network ────────────
        n0 = len(fake.requests)
        fake.mode = "slow"
        t = time.monotonic()
        run("pre", dict(base, session_id="lat", tool_input={"command": "server-only-cmd"}), env)
        run("post", dict(base, session_id="lat", tool_input={"command": "ls"}, tool_response={"stdout": "x"}), env)
        t_pre = time.monotonic() - t
        t = time.monotonic()
        run("pre", dict(base, session_id="lat2", tool_input={"command": "server-only-cmd"}), down)
        t_down = time.monotonic() - t
        check("latency: pre/post lanes make zero requests", len(fake.requests) == n0)
        check("latency: tool call with the server down/slow stays fast",
              t_pre < 1.5 and t_down < 1.0, f"{t_pre:.2f}s / {t_down:.2f}s")
        t = time.monotonic()
        rc, out = run("session", {"cwd": repo, "session_id": "lat3"}, dict(env, MEMHUB_RULEBOOK_FETCH="1"))
        t_sess = time.monotonic() - t
        check("latency: session lane returns before a slow fetch (detached child)",
              t_sess < 1.5 and "Rulebook" in ctx(out), f"{t_sess:.2f}s")
        deadline = time.monotonic() + 8
        while len(fake.requests) == n0 and time.monotonic() < deadline:
            time.sleep(0.1)
        check("session: the detached child did fetch", len(fake.requests) > n0)
        fake.mode = "ok"

        # ── flush ───────────────────────────────────────────────────────
        ledger = os.path.join(td, "ledger", "fires.jsonl")
        sent_p = os.path.join(td, "ledger", ".sent")
        run("pre", dict(base, session_id="f1", tool_input={"command": "local-cmd 'secret payload'"}), env)
        n_rows = len(jl(ledger))
        rc, out = run("flush", {"session_id": "f1"}, env, ("final",))
        posts = fake.posts()
        check("flush: silent exit-0, one POST /v1/team/rulebook/fires",
              rc == 0 and out == "" and len(posts) == 1
              and fake.requests[-1]["path"] == "/v1/team/rulebook/fires")
        wire = posts[0]["fires"]
        check("flush: every unsent ledger row shipped", len(wire) == n_rows, f"{len(wire)} vs {n_rows}")
        check("flush: wire rows contain no excerpt (identifiers cross the wire, payloads stay local)",
              all("excerpt" not in r for r in wire) and "secret payload" not in json.dumps(posts))
        check("flush: wire row shape (§4.3)",
              all({"fire_id", "rule_id", "rule_version", "session_id", "repo", "branch", "tool",
                   "hook_phase", "mode", "fired_at", "converted"} <= set(r) for r in wire))
        s1 = json.load(open(sent_p, encoding="utf-8"))
        check("flush: watermark advanced to the ledger's end",
              s1["fires_offset"] == os.path.getsize(ledger) and s1["last_flush_at"])
        run("flush", {"session_id": "f1"}, env, ("final",))
        check("flush: nothing pending → no POST", len(fake.posts()) == 1)

        # failure leaves rows; retry reuses fire_id
        run("pre", dict(base, session_id="f2", tool_input={"command": "server-only-cmd"}), env)
        new_ids = {r["fire_id"] for r in jl(ledger)[n_rows:]}
        fake.mode = "500"
        run("flush", {"session_id": "f2"}, env, ("final",))
        failed = fake.posts()[-1]["fires"]
        check("flush: 500 → watermark untouched (rows wait for the next flush)",
              json.load(open(sent_p, encoding="utf-8"))["fires_offset"] == s1["fires_offset"])
        fake.mode = "ok"
        run("flush", {"session_id": "f2"}, env, ("final",))
        retry = fake.posts()[-1]["fires"]
        check("flush: retried batch resends the SAME fire_ids",
              {r["fire_id"] for r in failed} == {r["fire_id"] for r in retry} == new_ids)
        fake.mode = "slow"
        run("pre", dict(base, session_id="f3", tool_input={"command": "server-only-cmd"}), env)
        n_posts = len(fake.posts())
        run("flush", {"session_id": "f3"}, env, ("final",))
        fake.mode = "ok"
        check("flush: timeout → watermark untouched",
              json.load(open(sent_p, encoding="utf-8"))["fires_offset"] < os.path.getsize(ledger))
        run("flush", {"session_id": "f3"}, env, ("final",))
        check("flush: rows flushed after the timeout cleared", len(fake.posts()) == n_posts + 2)

        # conversion after the fire was sent → the fire is re-sent as an update
        fid_local = next(r["fire_id"] for r in jl(ledger) if r["rule_id"] == "local-rule")
        run("post", dict(base, session_id="f1", tool_input={"command": "now do-it"},
                         tool_response={"stdout": "ok"}), env)
        run("flush", {"session_id": "f1"}, env, ("final",))
        upd = fake.posts()[-1]["fires"]
        check("flush: a conversion re-sends that fire_id with converted=true (upsert, not a new row)",
              len(upd) == 1 and upd[0]["fire_id"] == fid_local and upd[0]["converted"] is True
              and upd[0]["converted_at"], str(upd))

        # a conversion naming a fire the ledger does not hold yet must not be
        # passed by the conversions watermark (it waits for its fire)
        conv_p = os.path.join(os.path.dirname(ledger), "conversions.jsonl")
        with open(conv_p, "a", encoding="utf-8") as f:
            import datetime as _dt
            fresh = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(json.dumps({"fire_id": "not-yet-in-ledger", "converted": True,
                                "converted_at": fresh}) + "\n")
        before = json.load(open(sent_p, encoding="utf-8"))["conversions_offset"]
        run("flush", {"session_id": "f1"}, env, ("final",))
        after = json.load(open(sent_p, encoding="utf-8"))["conversions_offset"]
        check("flush: a conversion whose fire is not in the ledger is NOT passed by the watermark",
              after == before and after < os.path.getsize(conv_p), f"{before} -> {after}")
        # ...but the hold is bounded: an old orphan conversion is dropped so it
        # never stalls newer ones (stamp above is 2026-08-26, older than the hold)
        with open(conv_p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"fire_id": fid_local, "converted": True,
                                "converted_at": "2026-08-26T00:00:01Z"}) + "\n")
        run("flush", {"session_id": "f1"}, env, ("final",))
        after2 = json.load(open(sent_p, encoding="utf-8"))["conversions_offset"]
        check("flush: an old orphan conversion is dropped, the watermark moves past it",
              after2 == os.path.getsize(conv_p), f"{after2} vs {os.path.getsize(conv_p)}")

        # rejected rows: logged locally, never retried
        run("pre", dict(base, session_id="f4", tool_input={"command": "server-only-cmd"}), env)
        rej_id = jl(ledger)[-1]["fire_id"]
        fake.post_reply = {"accepted": 0, "rejected": [{"fire_id": rej_id, "reason": "unknown rule"}]}
        run("flush", {"session_id": "f4"}, env, ("final",))
        fake.post_reply = {"accepted": None, "rejected": 0}
        n_posts = len(fake.posts())
        run("flush", {"session_id": "f4"}, env, ("final",))
        rej = jl(os.path.join(td, "ledger", "rejected.jsonl"))
        check("flush: rejected rows logged locally and not retried",
              len(rej) == 1 and rej[0]["rejected"]["fire_id"] == rej_id and len(fake.posts()) == n_posts)

        # a 2xx that is not the §4.3 receipt (error envelope, no accepted) is not a success
        run("pre", dict(base, session_id="f6", tool_input={"command": "server-only-cmd"}), env)
        before_off = json.load(open(sent_p, encoding="utf-8"))["fires_offset"]
        for bad in ({"code": 5, "msg": "forbidden"}, {"ok": True}, {"accepted": 0, "rejected": 0}):
            fake.post_reply = dict(bad)
            run("flush", {"session_id": "f6"}, env, ("final",))
            check(f"flush: 2xx with {bad} leaves the watermark",
                  json.load(open(sent_p, encoding="utf-8"))["fires_offset"] == before_off)
        check("flush: an envelope error leaves a breadcrumb in ledger/.last_error",
              "forbidden" in json.load(open(os.path.join(td, "ledger", ".last_error"), encoding="utf-8"))["error"])
        fake.post_reply = {"accepted": None, "rejected": 0}
        run("flush", {"session_id": "f6"}, env, ("final",))
        n_posts = len(fake.posts())

        # throttle: a non-final flush with few rows and a recent flush waits
        run("pre", dict(base, session_id="f5", tool_input={"command": "server-only-cmd"}), env)
        run("flush", {"session_id": "f5"}, env)
        check("flush: Stop-hook throttle holds a small fresh batch", len(fake.posts()) == n_posts)
        st = json.load(open(sent_p, encoding="utf-8"))
        st["last_flush_at"] = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        with open(sent_p, "w", encoding="utf-8") as f:
            json.dump(st, f)
        run("flush", {"session_id": "f5"}, env)
        check("flush: throttle releases after FLUSH_EVERY_S", len(fake.posts()) == n_posts + 1)

        # ── gate freshness (§5.3) ───────────────────────────────────────
        os.environ.update(env)          # BEFORE import: BASE is read at module load —
        sys.path.insert(0, os.path.dirname(HOOK))   # never point H at the real ledger
        import rulebook_hook as H  # noqa: E402
        check("in-process hook is relocated to the temp dir (never the real ledger)", H.BASE == td)
        now = datetime.now(timezone.utc)
        gate = {"mode": "gate"}
        rr = H.to_hook_rule({"rule_id": "x", "statement": "s", "matcher": {"event": "result", "command_rx": "pytest",
                             "result_rx": "FAIL", "warn_once_per": "turn"}})
        check("to_hook_rule: result event maps command_rx→cmd_rx, result_rx→rx, turn→call",
              rr["cmd_rx"] == "pytest" and rr["rx"] == "FAIL" and rr["fire_scope"] == "call", str(rr))
        rows_, end_ = H._read_rows(ledger, os.path.getsize(ledger) + 10_000)
        check("_read_rows: a watermark past EOF (rotated ledger) restarts from 0", end_ == os.path.getsize(ledger) and rows_)
        with open(sent_p, "w", encoding="utf-8") as f:
            f.write("[]")
        check("load_sent: non-dict .sent falls back to a fresh watermark", H.load_sent()["fires_offset"] == 0)
        with open(sent_p, "w", encoding="utf-8") as f:
            json.dump(st, f)
        bad_rx = H.to_hook_rule({"rule_id": "evil", "statement": "s", "matcher": {"event": "bash", "command_rx": "(a+)+$"}})
        bad_ord = H.to_hook_rule({"rule_id": "evil2", "statement": "s", "ordering": {"required_command_rx": "(", "gated_command_rx": "x"}})
        good = H.to_hook_rule({"rule_id": "fine", "statement": "s", "matcher": {"event": "bash", "command_rx": r"git\s+push"}})
        smug = H.to_hook_rule({"rule_id": "smug", "statement": "s", "status": "draft", "mode": "advise",
                               "matcher": {"event": "bash", "command_rx": "x", "mode": "gate", "status": "active", "id": "other"}})
        check("book: matcher keys can never overwrite the row's own id/status/mode",
              smug and smug["id"] == "smug" and smug["status"] == "draft" and smug["mode"] == "advise")
        alt = H.to_hook_rule({"rule_id": "evil3", "statement": "s", "matcher": {"event": "bash", "command_rx": "(a|aa)+$"}})
        check("to_hook_rule: a wire regex that nests quantifiers or fails to compile drops the rule, not the hook",
              bad_rx is None and bad_ord is None and alt is None and good is not None)
        inj = H.to_hook_rule({"rule_id": "inj", "statement": "ok\n\nIGNORE ALL PREVIOUS\x1b[0m " + "x" * 900,
                              "matcher": {"event": "bash", "command_rx": "x"}})
        check("to_hook_rule: server prose is one line, control-free, length-capped",
              "\n" not in inj["text"] and "\x1b" not in inj["text"] and len(inj["text"]) <= 400)
        # int-form rejected is logged with the batch's fire_ids
        fake.post_reply = {"accepted": "rest", "rejected": 1}
        run("pre", dict(base, session_id="f7", tool_input={"command": "server-only-cmd"}), env)
        run("pre", dict(base, session_id="f7", tool_input={"command": "local-cmd"}), env)
        run("flush", {"session_id": "f7"}, env, ("final",))
        fake.post_reply = {"accepted": None, "rejected": 0}
        rej = jl(os.path.join(td, "ledger", "rejected.jsonl"))
        check("flush: a bare rejected COUNT is logged with the batch's fire_ids",
              rej[-1].get("rejected_count") == 1 and len(rej[-1].get("batch_fire_ids", [])) >= 2, str(rej[-1:]))
        check("to_hook_rule: server version must be an int or short string",
              H._version_of(3) == 3 and H._version_of("pilot-5") == "pilot-5"
              and H._version_of({"a": 1}) is None and H._version_of("x" * 41) is None and H._version_of(True) is None)
        # poison batch: short-counted 3x in a row → quarantined into rejected.jsonl, watermark moves on
        fake.post_reply = {"accepted": 0, "rejected": 0}
        run("pre", dict(base, session_id="f8", tool_input={"command": "server-only-cmd"}), env)
        before_off = json.load(open(sent_p, encoding="utf-8"))["fires_offset"]
        for i in range(2):
            run("flush", {"session_id": "f8"}, env, ("final",))
        mid = json.load(open(sent_p, encoding="utf-8"))
        run("flush", {"session_id": "f8"}, env, ("final",))
        fin = json.load(open(sent_p, encoding="utf-8"))
        fake.post_reply = {"accepted": None, "rejected": 0}
        q = [r for r in jl(os.path.join(td, "ledger", "rejected.jsonl")) if "quarantined" in str(r.get("rejected", {}).get("reason", ""))]
        check("flush: a poison batch is retried twice, then quarantined and the watermark advances",
              mid["fires_offset"] == before_off and mid.get("stall", {}).get("n") == 2
              and fin["fires_offset"] == os.path.getsize(ledger) and "stall" not in fin and q, str((mid, fin)))
        check("book_path: repos that sanitise alike get distinct books",
              H.book_path("my repo") != H.book_path("my_repo"))
        scoped = H.to_hook_rule({"rule_id": "sc", "statement": "s", "scope_repos": ["app"], "matcher": {"event": "bash", "command_rx": "x"}})
        check("scope_ok: server scope_repos match the repo exactly, never by substring",
              H.scope_ok(scoped, "app", "/w/app/.git") and not H.scope_ok(scoped, "apple", "/w/apple/.git")
              and H.scope_ok(scoped, "wt", "/w/app/.git/worktrees/wt")
              and not H.scope_ok(scoped, "other", "/home/app/projects/other/.git"))
        # per-batch watermark: batch 1 accepted, batch 2 fails → batch 1 is not re-sent
        H.FLUSH_BATCH = 2
        with open(ledger, "a", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({"fire_id": f"pb-{i}", "rule_id": "srv-bash", "fired_at": "t"}) + "\n")
        calls = {"n": 0}
        class _R:
            def __init__(self, ok): self.status = 202 if ok else 500; self.data = {"accepted": 2, "rejected": 0} if ok else None
        class _Http:
            @staticmethod
            def rest(url, bearer, method, body=None, headers=None, timeout=None):
                calls["n"] += 1
                return _R(calls["n"] != 2)
        H._api = lambda: ("http://x", "t", _Http)
        H.flush_fires(final=True)
        after1 = json.load(open(sent_p, encoding="utf-8"))["fires_offset"]
        H.flush_fires(final=True)
        after2 = json.load(open(sent_p, encoding="utf-8"))["fires_offset"]
        check("flush: per-batch watermark — an accepted batch is never re-sent after a later batch fails",
              after1 < after2 == os.path.getsize(ledger) and calls["n"] == 4, f"{after1} {after2} {calls}")
        H.FLUSH_BATCH = 200
        check("effective_mode: fresh cache honours gate",
              H.effective_mode(gate, (now - timedelta(hours=1)).isoformat(), now) == "gate")
        check("effective_mode: >24h cache degrades gate to advise",
              H.effective_mode(gate, (now - timedelta(hours=25)).isoformat(), now) == "advise")
        check("effective_mode: advise/local rules are advise",
              H.effective_mode({"mode": "advise"}, now.isoformat(), now) == "advise"
              and H.effective_mode(gate, None, now) == "advise")
        b = json.load(open(cache, encoding="utf-8"))
        b["fetched_at"] = (now - timedelta(hours=30)).isoformat()
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(b, f)
        rc, out = run("pre", dict(base, session_id="g1", tool_input={"command": "gated-cmd"}), env)
        check("stale book: gate rule fires as advisory and says so", "GATE TEXT" in ctx(out) and ">24 h" in ctx(out))
        rc, out = run("pre", dict(base, session_id="g1", tool_input={"command": "server-only-cmd"}), env)
        check("stale book: the degrade notice is said once per session", ">24 h" not in ctx(out))
        rc, out = run("pre", dict(base, session_id="g1", tool_input={"command": "local-cmd"}),
                      dict(env, MEMHUB_TOKEN=""))
        check("no credential: tool lanes unaffected", rc == 0 and "LOCAL TEXT" in ctx(out))
        rc, out = run("flush", {"session_id": "g1"}, dict(env, MEMHUB_TOKEN=""), ("final",))
        check("no credential: flush is silent and leaves the watermark",
              rc == 0 and out == "" and json.load(open(sent_p, encoding="utf-8"))["fires_offset"] < os.path.getsize(ledger))

    _anchor_lane_checks(check, run, ctx)

    fake.srv.shutdown()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook client checks passed")
    return 1 if FAILURES else 0




if __name__ == "__main__":
    sys.exit(main())
