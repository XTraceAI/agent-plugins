#!/usr/bin/env python3
"""Run one synthetic production session and one local rejection session per host.

Only fresh disposable homes are used. Never import/replay a developer transcript.
Raw agent output, transcripts and credential files are not release artifacts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import UUID, uuid4

from release_check_lib import NotVerified, Report, compat, host_version, isolated_env, json_lines, read_json, require, run

for _name, _filename in (("install", "check-host-install.py"), ("policy", "check-upgrade-contract.py")):
    _spec = importlib.util.spec_from_file_location(_name, Path(__file__).with_name(_filename))
    globals()[_name] = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(globals()[_name])

AUTH = {"codex": "CODEX_API_KEY", "claude": "ANTHROPIC_API_KEY", "cursor": "CURSOR_API_KEY"}
LIVE_CHECKS = ("agent_completed", "advice_delivered", "gate_enforced", "allowed_operation", "capture_acknowledged")
REPO = "memhub-production-release-e2e"
BLOCKED_FILE = ".memhub-release-blocked"
# The id each host's capture sends as ``conversation_id`` — what the backend's
# ``DELETE /v1/team/conversations?session_id=eq.<id>`` resolves (ENG-1074).
# Codex and Cursor prefix the native id (codex_flush / cursor_flush); Claude
# sends it bare (flush_turn / flush_session).
HARNESS_SESSION_ID = {"codex": "codex-{}", "claude": "{}", "cursor": "cursor-{}"}
MANIFEST_SCHEMA = 1
AGENT_TIME_BUDGET_S = 360


def fixture_config(raw):
    try:
        fixture = json.loads(raw)
        require(fixture["schema_version"] == 1 and fixture["repo"] == REPO, "invalid agent fixture schema or repo")
        for field in ("org_id", "advice_rule_id", "gate_rule_id"):
            UUID(fixture[field])
        require(fixture["advice_rule_id"] != fixture["gate_rule_id"], "fixture rule IDs must differ")
        require(re.fullmatch(r"MEMHUB_RELEASE_ADVICE_[A-Z0-9]{8,40}", fixture["advice_marker"]),
                "fixture advice marker is invalid")
        return fixture
    except (ValueError, KeyError, TypeError):
        raise NotVerified("MEMHUB_PROD_E2E_FIXTURE_JSON is not provisioned") from None


def validate_live_fixture(package, fixture, token):
    require(token.startswith("mhk_") and not any(c.isspace() for c in token), "production E2E key is malformed")
    http = compat.load_module("release_http", package / "scripts/mcp_http.py")
    result = http.request(compat.PRODUCTION + "/mcp-server/mcp", token, "tools/call",
                          {"name": "list_orgs", "arguments": {}}, timeout=20)
    require(not result.get("isError"), "could not check the test key's organization scope")
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        data = next((json.loads(c["text"]) for c in result.get("content", []) if c.get("type") == "text"), {})
    orgs = data.get("orgs", [])
    company = [o for o in orgs if o.get("org_type") == "company"]
    require(len(company) == 1 and company[0].get("org_id") == fixture["org_id"]
            and company[0].get("is_default") and str(company[0].get("role", "")).lower() == "member",
            "E2E key must default to the single test company organization as a member")
    version = compat.package_version(package)
    reply = http.rest(compat.PRODUCTION + compat.RULES_PATH + f"?view=hook&repo={REPO}&hook_version={version}",
                      token, headers={"X-Org-Id": fixture["org_id"]}, timeout=20)
    rules = {r.get("rule_id", r.get("id")): r for r in reply.data.get("rules", [])}
    require(set(rules) == {fixture["advice_rule_id"], fixture["gate_rule_id"]},
            "active agent fixture rules differ from the provisioned fixture")
    advice, gate = rules[fixture["advice_rule_id"]], rules[fixture["gate_rule_id"]]
    require(advice.get("mode") == "advise" and advice.get("delivery") == "agent_hook"
            and fixture["advice_marker"] in advice.get("statement", ""), "advice fixture does not match")
    require(gate.get("mode") == "gate" and gate.get("delivery") == "agent_hook", "gate fixture does not match")
    for rule, command in ((advice, "echo memhub-release-advice"), (gate, "touch " + BLOCKED_FILE)):
        matcher = rule.get("matcher", {})
        require(rule.get("scope_repos") == [REPO] and not rule.get("scope_paths")
                and not rule.get("scope_exclude_paths") and not matcher.get("given")
                and not matcher.get("command_not_rx") and matcher.get("event") == "bash",
                "agent fixture must be a simple repo-scoped shell rule")
        require(re.search(matcher.get("command_rx") or r"(?!)", command), "fixture matcher misses its command")


def prepare_workspace(root, env, name):
    ws = root / name
    ws.mkdir()
    run(["git", "init", "-q", str(ws)], env=env, cwd=root)
    run(["git", "-C", str(ws), "remote", "add", "origin", f"https://github.com/XTraceAI/{name}.git"], env=env, cwd=root)
    return ws


def prepare_host(root, env, package, host, executable):
    if host == "cursor":
        # This is a real CLI load of unchanged package bytes, not a marketplace install.
        installed = root / "plugin"
        shutil.copytree(package, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        installed = install.native_install(root, env, package, host, executable)
    if host == "codex":
        run([sys.executable, str(installed / "scripts/setup_codex_hooks.py"), "install"], env=env, cwd=root)
    require(compat.package_digest(installed) == compat.package_digest(package), "host package bytes changed")
    return installed


UPGRADE_ANSWER = {"type": "object", "additionalProperties": False,
                  "required": ["error_code", "minimum_version", "remediation"],
                  "properties": {"error_code": {"type": "string"}, "minimum_version": {"type": "string"},
                                 "remediation": {"type": "string"}}}
LIVE_ANSWER = {"type": "object", "additionalProperties": False,
               "required": ["advice", "blocked_command_denied", "allowed_file_written"],
               "properties": {"advice": {"type": "string"}, "blocked_command_denied": {"type": "boolean"},
                              "allowed_file_written": {"type": "boolean"}}}


def command(host, executable, installed, model, schema=None, schema_path=None):
    if host == "codex":
        # Only the reviewed MemHub hooks exist in this disposable HOME.
        return [executable, "exec", "--json", "--sandbox", "workspace-write",
                "--dangerously-bypass-hook-trust", "-c", "approval_policy=\"never\"",
                "-c", "sandbox_workspace_write.network_access=true", "--model", model,
                *(["--output-schema", str(schema_path)] if schema_path else []), "-"]
    if host == "claude":
        return [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--include-hook-events", "--max-turns", "8", "--max-budget-usd", "2",
                "--model", model, "--allowedTools", "Bash(echo:*),Bash(touch:*),Write,Read",
                *(["--json-schema", json.dumps(schema)] if schema else [])]
    return [executable, "-p", "--output-format", "stream-json", "--trust", "--force",
            "--sandbox", "enabled", "--model", model, "--plugin-dir", str(installed)]


def answer_schema_path(root, schema):
    """Codex takes its schema from a file; keep it outside the workspace the agent is told to use."""
    path = Path(root) / "answer-schema.json"
    path.write_text(json.dumps(schema))
    return path


def drive(host, executable, installed, model, prompt, env, workspace, on_event=None, schema=None, schema_path=None):
    """Run the agent and return its JSON event stream.

    Every host STREAMS: each event is parsed as it arrives and handed to
    ``on_event`` while the host is still running. That is what lets the caller
    persist the session identity before the outcome is known — a host that
    then times out or exits nonzero has already let the capture hooks create
    the production session, and only an identity recorded mid-run can name it
    for cleanup. Buffering the output (``communicate``) would discard exactly
    that on the failures that need it. Raw output is never retained beyond the
    parsed events, and is never streamed into CI logs.
    """
    cmd = command(host, executable, installed, model, schema=schema, schema_path=schema_path)
    events = []

    def read(stream):
        for line in stream:
            for event in json_lines(line):
                events.append(event)
                if on_event is not None:
                    on_event(event)

    # Cursor takes the prompt as an argument; Codex reads it from stdin; Claude
    # speaks stream-json on stdin, held open for its Stop hooks (a plain -p
    # process can exit before asynchronous capture hooks finish).
    with subprocess.Popen(cmd + ([prompt] if host == "cursor" else []), cwd=workspace, env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, start_new_session=True) as proc:
        reader = threading.Thread(target=read, args=(proc.stdout,), daemon=True)
        reader.start()
        if host == "claude":
            proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            proc.stdin.flush()
        else:
            if host == "codex":
                proc.stdin.write(prompt)
            proc.stdin.close()
        deadline = time.monotonic() + AGENT_TIME_BUDGET_S
        if host != "claude":
            try:
                proc.wait(timeout=AGENT_TIME_BUDGET_S)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise compat.GateError("command exceeded its time budget") from None
            reader.join(timeout=5)
            require(proc.returncode == 0, "host command failed; raw output withheld")
            return events
        while time.monotonic() < deadline and proc.poll() is None:
            if any(e.get("type") == "result" for e in list(events)):
                break
            time.sleep(.25)
        grace = time.monotonic() + 120
        while time.monotonic() < grace and proc.poll() is None:
            stops = [e for e in list(events) if e.get("hook_event") == "Stop"]
            started = sum(e.get("subtype") == "hook_started" for e in stops)
            ended = sum(e.get("subtype") == "hook_response" for e in stops)
            if started and ended >= started:
                break
            time.sleep(.25)
        proc.stdin.close()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise compat.GateError("Claude session exceeded its shutdown budget") from None
        reader.join(timeout=5)
        require(proc.returncode == 0, "Claude session exited unsuccessfully")
    return events


def final_text(events, host):
    if host == "codex":
        require(any(e.get("type") == "turn.completed" for e in events), "Codex did not complete its turn")
        require(not any(e.get("type") in ("error", "turn.failed") for e in events), "Codex reported a failed turn")
        return "\n".join(e["item"].get("text", "") for e in events if e.get("type") == "item.completed"
                         and e.get("item", {}).get("type") == "agent_message")
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    require(result is not None and not result.get("is_error"), "agent did not return a successful result")
    return result.get("result", "")


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def answer_fields(text):
    """The last JSON object in the agent's final text, or {}.

    The prompts ask for one object and nothing else, and the hosts are given
    the schema, but the evidence must survive a model that wraps it in a fence
    or a sentence: scan fenced blocks first, then every balanced ``{...}`` from
    the end, and keep the first that parses as an object."""
    if not isinstance(text, str):
        return {}
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
    for raw in reversed(candidates):
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def final_answer(events, host):
    """(final text, structured fields). Claude's result event carries
    ``structured_output`` when it was run with a schema; that is preferred
    over re-parsing the text, and everything else falls back to the text."""
    text = final_text(events, host)
    if host != "codex":
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        structured = (result or {}).get("structured_output")
        if isinstance(structured, dict):
            return text, structured
    return text, answer_fields(text)


def claude_hook_health(events):
    require(not any(e.get("subtype") == "init" and e.get("plugin_errors") for e in events),
            "Claude reported plugin load errors")
    responses = [e for e in events if e.get("subtype") == "hook_response"]
    require(responses and all(
        (e.get("outcome") == "success" and e.get("exit_code") in (0, None)) or
        (e.get("outcome") == "blocked" and e.get("exit_code") in (0, 2, None))
        for e in responses), "Claude hooks failed or were not observed")


def event_diagnostics(events):
    """Project events onto fixed categories; never emit arbitrary host strings."""
    def enum(value, allowed):
        return value if isinstance(value, str) and value in allowed else "other"
    def exit_code(value):
        return value if type(value) is int and -255 <= value <= 255 else None
    def signals(value):
        raw = json.dumps(value).lower()
        return [label for label, pattern in (
            ("sandbox_error", r"sandbox.*(?:fail|error)|bwrap:|landlock"),
            ("permission_denied", r"permission denied|operation not permitted"),
            ("missing_executable", r"command not found|no such file or directory"),
            ("authentication_error", r"invalid.api.key|unauthorized|authentication.*fail"),
            ("uv_missing", r"uv:.*not found"),
        ) if re.search(pattern, raw)]
    hooks, commands = [], []
    for event in events:
        if event.get("subtype") in {"hook_started", "hook_response"}:
            hooks.append({
                "phase": event["subtype"],
                "event": enum(event.get("hook_event"), {"SessionStart", "PreToolUse", "PostToolUse", "Stop", "SessionEnd", "UserPromptSubmit"}),
                "outcome": enum(event.get("outcome"), {"success", "blocked", "error", "cancelled"}),
                "exit_code": exit_code(event.get("exit_code")),
                "signals": signals(event),
            })
        item = event.get("item", {})
        if event.get("type") == "item.completed" and item.get("type") == "command_execution":
            commands.append({
                "status": enum(item.get("status"), {"completed", "failed"}),
                "exit_code": exit_code(item.get("exit_code")),
                "signals": signals(item),
            })
    return {"event_count": len(events), "hooks": hooks, "commands": commands,
            "signals": signals(events)}


def ledger(root):
    path = root / "home/.config/memhub-plugin/rulebook/ledger/fires.jsonl"
    return json_lines(path.read_text()) if path.is_file() else []


def identity_of(event, host):
    """The native session identity this one event announces, else ``None``.
    Unvalidated on purpose: ``session_id`` and ``record_session`` each apply
    the shape rule, so a hostile value is refused wherever it is used."""
    if host == "codex":
        return event.get("thread_id") if event.get("type") == "thread.started" else None
    if event.get("type") == "system" and event.get("subtype") == "init":
        return event.get("session_id")
    return None


def session_id(events, host):
    value = next((v for v in (identity_of(e, host) for e in events) if v is not None), None)
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value),
            "host did not expose a usable native session identity")
    return value


def harness_session_id(host, sid):
    return HARNESS_SESSION_ID[host].format(sid)


def record_session(path, *, host, sid, org_id):
    """Upsert this run's production session into the run-owned manifest.

    The manifest lives OUTSIDE the disposable agent home (the caller passes a
    path under the job's report directory), and is written the moment the host
    announces the native session identity — while the host is still running —
    so a crash, a timeout or a cancelled workflow still leaves the ``always()``
    cleanup step an exact list of what this attempt created. Exact ids only:
    cleanup deletes what is listed here and nothing else, so it can never reach
    the fixtures, the rules, or a session another run or a developer captured
    into the same test account.

    Deliberately no "capture finished" mark. Codex and Cursor flush from
    DETACHED processes (``codex_hook_bridge._detach_flush``,
    ``cursor_capture.spawn_cursor_flush``) and write the acknowledgement on
    every flush, so a fresh ``last_ok_at`` proves an earlier flush landed, not
    that the final one has; nothing this script can observe says a flush is
    not still in flight. Cleanup therefore always allows for one.
    """
    path = Path(path)
    require(re.fullmatch(r"[A-Za-z0-9_-]{8,128}", sid), "refusing to record a malformed session identity")
    UUID(org_id)
    manifest = read_json(path) if path.is_file() else None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA:
        manifest = {"schema_version": MANIFEST_SCHEMA, "sessions": []}
    entry = {"host": host, "org_id": org_id, "repo": REPO,
             "native_session_id": sid, "harness_session_id": harness_session_id(host, sid),
             "recorded_at": int(time.time())}
    sessions = [row for row in manifest.get("sessions", []) if isinstance(row, dict)]
    existing = next((row for row in sessions if row.get("harness_session_id") == entry["harness_session_id"]
                     and row.get("org_id") == org_id), None)
    if existing is None:
        sessions.append(entry)
    else:
        entry = existing
    manifest["sessions"] = sessions
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, path)
    return entry


def capture_ok(root, host, since, sid):
    folder = {"codex": "codexflush", "claude": "turnflush", "cursor": "cursorflush"}[host]
    paths = [root / "home/.config/memhub-plugin" / folder / f"{sid}.json"]
    if host == "claude":
        paths.append(paths[0].with_name(f"{sid}.sessionflush.json"))
    rows = [read_json(p) for p in paths]
    return all(isinstance(r, dict) and not r.get("last_error") and not r.get("unsupported")
               and not r.get("pending_pr_urls") and isinstance(r.get("last_ok_at"), (int, float))
               and r["last_ok_at"] >= since for r in rows)


def run_live(args, report, model):
    token = os.environ.get("MEMHUB_PROD_E2E_TOKEN", "")
    if not token:
        report.blocked(LIVE_CHECKS, "MEMHUB_PROD_E2E_TOKEN is not provisioned")
        return
    fixture = report.check("fixture_configuration", lambda: fixture_config(os.environ.get("MEMHUB_PROD_E2E_FIXTURE_JSON", "")))
    if fixture is None:
        report.blocked(LIVE_CHECKS, "production advice and gate fixtures are not provisioned")
        return
    ready = report.check("production_fixture", lambda: (validate_live_fixture(args.plugin_root, fixture, token), True)[1])
    if not ready:
        report.blocked(LIVE_CHECKS, "production fixture verification failed")
        return
    # ignore_cleanup_errors: the host can still be writing into its disposable
    # home after the checks are done (Codex's plugin clone under
    # ~/.codex/.tmp, a detached capture flush). A cleanup race must not turn a
    # fully passed run into a crash with no report (#235, run 35023881455).
    with tempfile.TemporaryDirectory(prefix="memhub-agent-prod-", ignore_cleanup_errors=True) as raw:
        root = Path(raw)
        env = isolated_env(root)
        ws = prepare_workspace(root, env, REPO)
        installed = report.check("native_package_load", lambda: prepare_host(root, env, args.plugin_root, args.host, args.executable))
        if installed is None:
            report.blocked(LIVE_CHECKS, "native plugin setup failed")
            return
        # Provision exactly the dedicated test key, not any host's saved OAuth credentials.
        state = root / "home/.config/memhub-plugin"
        state.mkdir(parents=True, exist_ok=True)
        key = state / "pak-api.memhub.xtrace.ai.json"
        key.write_text(json.dumps({"secret": token}))
        key.chmod(0o600)
        env[AUTH[args.host]] = os.environ[AUTH[args.host]]
        marker = "release-run-" + uuid4().hex
        # The allowed write comes BEFORE the command a hook will deny, and the
        # denial is named as expected: a model that treats a denial as the end
        # of the task otherwise never writes the file or reports the advice,
        # and the run reads as a plugin failure.
        prompt = ("This is a synthetic plugin release test. Do not read credentials, environment variables, or plugin state. "
                  "Execute each requested shell command in a separate tool call; do not combine commands or wrap them in a script. "
                  "Use only this workspace. Step 1: execute exactly `echo memhub-release-advice`. "
                  f"Step 2: write allowed.txt containing exactly {marker}. "
                  f"Step 3: attempt exactly `touch {BLOCKED_FILE}` once. A hook is expected to deny it: respect the denial, "
                  "do not retry or override it, and continue. "
                  "Step 4: finish with a single JSON object and nothing else, with exactly these keys: "
                  "\"advice\" (the advice text the plugin hooks delivered during this session, verbatim, or \"\" if none), "
                  "\"blocked_command_denied\" (true if the touch was denied), \"allowed_file_written\" (true if allowed.txt was written).")
        started = time.time()
        recorded = {}

        def note(event):
            # Persist the identity the moment the host announces it, while the
            # host is still running. A host that then times out or exits
            # nonzero has already let the capture hooks create the production
            # session; a cleanup that never hears of it is a leak reported as a
            # clean run. Errors are kept, not raised: this runs on the reader
            # thread, and ``session_recorded`` below is where they surface.
            if recorded or not args.session_manifest:
                return
            sid = identity_of(event, args.host)
            if not isinstance(sid, str):
                return
            try:
                record_session(args.session_manifest, host=args.host, sid=sid, org_id=fixture["org_id"])
                recorded["sid"] = sid
            except Exception as exc:
                recorded["error"] = exc

        events = report.check("agent_execution", lambda: drive(
            args.host, args.executable, installed, model, prompt, env, ws, on_event=note,
            schema=LIVE_ANSWER, schema_path=answer_schema_path(root, LIVE_ANSWER)))
        if args.session_manifest:
            def session_recorded():
                if "error" in recorded:
                    raise compat.GateError("the session manifest could not be written")
                if "sid" not in recorded:
                    raise NotVerified("the host announced no session identity; nothing to record")
            report.check("session_recorded", session_recorded)
        if events is None:
            report.blocked(LIVE_CHECKS, "agent process failed")
            return
        report.data["live_diagnostics"] = event_diagnostics(events)
        answer = report.check("agent_completed", lambda: final_answer(events, args.host))
        final, fields = answer if answer else ("", {})
        report.data["live_answer"] = {"structured": bool(fields),
                                      "blocked_command_denied": fields.get("blocked_command_denied"),
                                      "allowed_file_written": fields.get("allowed_file_written")}
        if args.host == "claude":
            report.check("hook_health", lambda: claude_hook_health(events))
        sid = report.check("native_session_identity", lambda: session_id(events, args.host))
        fires = ledger(root)
        # The marker exists only in the hook's advice text, never in the
        # prompt: its presence in the reported advice (or anywhere in the
        # final text) proves the model read a delivered fire.
        report.check("advice_delivered", lambda: require(
            (fixture["advice_marker"] in str(fields.get("advice", "")) or fixture["advice_marker"] in final) and any(
                r.get("rule_id") == fixture["advice_rule_id"] and r.get("mode") == "advise"
                and sid is not None and r.get("session_id") == sid for r in fires),
            "agent did not echo the hidden advice marker from a recorded hook fire"))
        report.check("gate_enforced", lambda: require(not (ws / BLOCKED_FILE).exists() and any(
            r.get("rule_id") == fixture["gate_rule_id"] and r.get("mode") == "gate"
            and sid is not None and r.get("session_id") == sid
            and not r.get("override_reason") for r in fires), "no enforced gate fire, or forbidden marker file was created"))
        report.check("allowed_operation", lambda: require(
            (ws / "allowed.txt").is_file() and (ws / "allowed.txt").read_text().strip() == marker,
            "unrelated allowed operation did not complete"))
        deadline = time.monotonic() + (240 if sid else 0)
        while time.monotonic() < deadline and not capture_ok(root, args.host, started, sid):
            time.sleep(1)
        report.check("capture_acknowledged", lambda: require(sid and capture_ok(root, args.host, started, sid),
            "native capture never recorded a fresh successful production acknowledgement"))


def upgrade_nonce():
    """A minimum version that exists nowhere but this run's 426 body. The hook
    accepts up to six digits per component (rulebook_hook.upgrade_status)."""
    return f"999.{secrets.randbelow(999999) + 1}.{secrets.randbelow(999999) + 1}"


def upgrade_notice_evidence(text, fields, nonce, server_contacted):
    """Record fixed booleans only; never publish agent output or credentials.

    Each fact is taken from the structured answer first and the final text
    second: the ask is explicit and the hosts are given the schema, so a
    structured answer is the expected shape, but a model that answers in a
    sentence with the right facts has still read the notice."""
    text = text if isinstance(text, str) else ""
    fields = fields if isinstance(fields, dict) else {}
    remediation = str(fields.get("remediation", ""))
    return {
        "server_contacted": bool(server_contacted),
        "structured_answer": bool(fields),
        "error_code_reported": fields.get("error_code") == "PLUGIN_UPGRADE_REQUIRED" or "PLUGIN_UPGRADE_REQUIRED" in text,
        "minimum_version_reported": fields.get("minimum_version") == nonce or nonce in text,
        "restart_reported": "restart" in remediation.lower() or "restart" in text.lower(),
    }


def upgrade_response_diagnostics(events, text, fields):
    """Distinguish answer extraction from model omission without retaining text."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), {})
    assistant_text = "\n".join(
        block.get("text", "") for event in events if event.get("type") == "assistant"
        for block in event.get("message", {}).get("content", [])
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
    hook_text = "\n".join(
        str(event.get(key, "")) for event in events if event.get("subtype") == "hook_response"
        for key in ("stdout", "output"))
    subtype = result.get("subtype")
    return {
        "result_kind": subtype if subtype in ("success", "error_max_turns", "error_max_budget_usd",
                                               "error_during_execution", "error_max_structured_output_retries") else "other",
        "result_has_structured_output": isinstance(result.get("structured_output"), dict),
        "final_text_has_restart": "restart" in str(text).lower(),
        "assistant_text_has_restart": "restart" in assistant_text.lower(),
        "hook_output_has_restart": "restart" in hook_text.lower(),
        "final_text_has_relaunch": "relaunch" in str(text).lower(),
        "final_text_has_new_session": "new session" in str(text).lower(),
        "remediation_has_relaunch": "relaunch" in str(fields.get("remediation", "")).lower(),
        "remediation_has_new_session": "new session" in str(fields.get("remediation", "")).lower(),
    }


UPGRADE_REQUIRED_EVIDENCE = ("server_contacted", "error_code_reported", "minimum_version_reported", "restart_reported")


def run_rejection(args, report, model):
    with tempfile.TemporaryDirectory(prefix="memhub-agent-rejection-", ignore_cleanup_errors=True) as raw:
        root = Path(raw)
        env = isolated_env(root)
        ws = prepare_workspace(root, env, "memhub-release-upgrade")
        installed = report.check("rejection_package_load", lambda: prepare_host(root, env, args.plugin_root, args.host, args.executable))
        if installed is None:
            report.blocked(["agent_upgrade_notice"], "native plugin setup failed")
            return
        env[AUTH[args.host]] = os.environ[AUTH[args.host]]
        env["MEMHUB_TOKEN"] = "synthetic-release-test-token"
        nonce = upgrade_nonce()
        server = policy.PolicyServer(minimum_version=nonce)
        server.reject = True
        env["MEMHUB_MCP_BASE_URL"] = server.url + "/mcp-server/mcp"
        try:
            prompt = ("Run `echo release-connectivity-check`. The plugin hooks will report a plugin compatibility problem; "
                      "do not repair anything. Do not read plugin files, credentials or environment variables. "
                      "Use only the information delivered by the plugin hooks. "
                      "Finish with a single JSON object and nothing else, with exactly these keys: "
                      "\"error_code\" (the exact error code the hooks reported), "
                      "\"minimum_version\" (the exact minimum required plugin version they reported), "
                      "\"remediation\" (the remediation they gave, verbatim).")
            def check():
                events = drive(args.host, args.executable, installed, model, prompt, env, ws,
                               schema=UPGRADE_ANSWER, schema_path=answer_schema_path(root, UPGRADE_ANSWER))
                report.data["rejection_diagnostics"] = event_diagnostics(events)
                if args.host == "claude":
                    report.check("rejection_hook_health", lambda: claude_hook_health(events))
                text, fields = final_answer(events, args.host)
                report.data["upgrade_response_diagnostics"] = upgrade_response_diagnostics(events, text, fields)
                evidence = upgrade_notice_evidence(text, fields, nonce, server.requests)
                report.data["upgrade_notice_evidence"] = evidence
                missing = [name for name in UPGRADE_REQUIRED_EVIDENCE if not evidence[name]]
                require(not missing, "upgrade requirement evidence missing: " + ", ".join(missing))
            report.check("agent_upgrade_notice", check)
        finally:
            server.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", choices=tuple(AUTH), required=True)
    ap.add_argument("--plugin-root", type=Path, required=True)
    ap.add_argument("--source-sha", required=True)
    ap.add_argument("--executable", required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--session-manifest", type=Path, default=None,
                    help="run-owned list of the production sessions this run captured, for the "
                         "always() cleanup step; keep it outside the disposable agent home")
    args = ap.parse_args()
    args.plugin_root = args.plugin_root.resolve()
    report = Report(args.host, args.plugin_root, args.source_sha)
    model = os.environ.get("MEMHUB_E2E_MODEL", "")
    if not os.environ.get(AUTH[args.host]) or not model:
        report.blocked((*LIVE_CHECKS, "agent_upgrade_notice"), "host CI credential or explicit model is not provisioned")
    else:
        require(re.fullmatch(r"[A-Za-z0-9./:_-]{1,100}", model), "invalid host model configuration")
        report.data["model"] = model
        with tempfile.TemporaryDirectory(prefix="memhub-host-version-", ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            report.data["host_version"] = report.check("host_cli", lambda: host_version(
                args.executable, isolated_env(root), root))
        run_live(args, report, model)
        run_rejection(args, report, model)
    return report.finish(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
