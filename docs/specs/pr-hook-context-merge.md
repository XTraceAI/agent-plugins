---
title: "Spec: one PostToolUse context per GitHub call"
type: spec
---

# Plugin Spec: one PostToolUse context per GitHub call

**Repo:** `XTraceAI/agent-plugins` (client-only — no MemHub server change).
**Subsystem:** the PR lane — `plugins/memhub/hooks/claude-hooks.json`,
`plugins/memhub/scripts/pr_link_trigger.py`,
`plugins/memhub/scripts/pr_babysit_trigger.py`.
**Companion spec:** `docs/specs/pr-linking-plugin-spec.md`, which quotes the
shipped registration this spec moves and is revised alongside it.
**Status:** proposed → implement on `fix/pr-hook-context-merge`, cut from
`origin/main` at v0.53.1.

---

## 1. The defect

On a Claude Code `PostToolUse` event, **two hook groups that each return
`hookSpecificOutput.additionalContext` do not both reach the model.** The
earlier-registered group's context survives; the later one is dropped, with no
error on either side.

`gh pr create` is the one command where both PR-lane hooks fire:

| group | matcher | script | fires on `gh pr create` |
|---|---|---|---|
| `PostToolUse[3]` | `Bash` | `pr_babysit_trigger.py` | yes |
| `PostToolUse[6]` | `^(Bash\|mcp__.*[Gg]it[Hh]ub.*__.*)$` | `pr_link_trigger.py` | yes |

So on the *only* call that is unconditionally self-linking — the narrow lane
the linking design calls its strongest signal — the link instruction is
exactly the one that loses, and the babysit instruction that survives sends
the model into a `/loop` where it never revisits the question.

### Evidence

1. On `gh pr create`, both hooks matched; only the babysit `additionalContext`
   reached the model.
2. `gh pr view <n> --json url -q .url` matches `[6]`'s guard but not `[3]`'s
   (`*gh*pr*create*`). The link context surfaced immediately and in full.
3. Both scripts, fed the same payload by hand, emit well-formed
   `hookSpecificOutput.additionalContext` and exit 0. Neither is broken.
4. The surviving one is the earlier registration.

**The plugin's link logic is fine.** What fails is the assumption that two
`PostToolUse` groups can each return context for one tool call.

### The other host already fixed this

`codex_hook_bridge.py::_dispatch_post` runs directive-recall, artifact-sync
and `pr_link_trigger` as jobs and folds them into **one** document with
`"\n\n".join(contexts)`. Its comment: *"The old layout ran these as separate
handlers. Preserve that latency profile while folding their output into one
valid JSON document."* Codex is already immune. Claude's manifest never got
the same treatment. This spec gives the Claude PR lane the same property, by
the same means: **one registration, one context.**

---

## 2. Scope

**In scope.** The Bash + GitHub-MCP lane only: `PostToolUse[3]` and
`PostToolUse[6]` collapse into a single registration behind one entry point
that emits at most one `additionalContext`.

**Explicitly out of scope — and it leaves a residual hole, named here rather
than glossed.** The merge stops the two PR instructions displacing *each
other*. It does not make a `PostToolUse` call deliver more than one context,
and the PR group is not the only synchronous handler matching `Bash` that can
return one:

| idx | handler | matches `Bash` | emits context |
|---|---|---|---|
| `[0]` | `pr_post_context.py` (this change) | yes | yes |
| `[1]` | `reactive_prefilter → directive_recall` | yes | yes, when the tool output matches `_ERROR_RE` |
| `[2]` | `flush_session` | yes | no — and `async`, whose context is not delivered anyway |
| `[3]` | `artifact_sync_reminder` | no (Edit family) | yes |
| `[4]` | `md_capture` | yes | no |
| `[5]` | `rulebook_hook post` | yes | yes |

So the PR group is registered **first**. Without that, a
`git push && gh pr create` or any create whose output carries `error:`,
`fatal:` or `npm ERR!` loses BOTH PR instructions to reactive recall — strictly
worse than the pre-fix state, where babysit at least arrived. Registering first
is a real trade, accepted deliberately: on exactly those calls the directive
recall loses instead. It costs nothing on ordinary Bash calls, because this
group is silent unless a pull request was really created.

The eventual fix is one Claude-side dispatcher folding every `Bash` handler
together, mirroring `codex_hook_bridge.py`. It is deliberately **not** this
change, and this spec must not make it harder: each lane stays a pure function
a dispatcher can call.

**Also out of scope.** Arming babysit on a PR created through a GitHub MCP
tool. The merged matcher covers MCP tools, which the babysit hook has never
seen; the babysit lane keeps its exact current trigger surface (see §3.4).

**No server change.** Every gate — `pr_link.check` included — already exists
and is unchanged.

---

## 3. Design

### 3.1 New entry point — `plugins/memhub/scripts/pr_post_context.py`

Stdlib-only, no network of its own, always exits 0, prints nothing when both
lanes are silent.

```python
payload = json.loads(sys.stdin.read() or "{}")
parts = []
if (text := lane(lambda: pr_link_trigger.context_for(payload, host=host))):
    parts.append(text)
if _BASH.search(tool_name) and (
        text := lane(lambda: pr_babysit_trigger.context_for(payload))):
    parts.append(text)
if parts:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "\n\n".join(parts)}}))
```

**Lanes run in-process**, imported rather than shelled out: one interpreter
start instead of three, and the two lanes are already import-safe
(`pr_link_trigger` imports `pr_link` the same way). `lane()` wraps each call
in `except BaseException: return None`, so a raising or unreachable link lane
still lets the babysit lane speak — a hook never fails the call it follows.
`pr_link.check` carries its own `CHECK_TIMEOUT_S = 4.0`, which is what bounds
the network.

**Order is load-bearing: link first, babysit second.** The link instruction
must be read before the babysit instruction sends the model into a loop.

**Separator is `"\n\n"`** — byte-identical to the Codex bridge, so the two
hosts compose context the same way.

### 3.2 Lane extraction — the two triggers keep their `main()`

Each trigger grows a pure function and keeps its stdin/stdout entry point
unchanged, because `codex_hook_bridge.py` invokes `pr_link_trigger.py` as a
**subprocess** and `tests/pr_link_trigger_test.py` drives it end-to-end the
same way. Neither may regress.

- `pr_link_trigger.context_for(payload, *, host="claude") -> str | None` —
  the body of today's `main()` between parsing stdin and printing.
- `pr_babysit_trigger.context_for(payload) -> str | None` — likewise; today's
  `main()` becomes `print` around it.
- `pr_link_trigger._host` is promoted to `host_from_argv` (the private name
  kept as an alias) so the entry point can resolve `--host` without reaching
  into a private.

### 3.3 Manifest — `plugins/memhub/hooks/claude-hooks.json`

`PostToolUse[6]` is **deleted**. `PostToolUse[3]` is **replaced in place** by
the merged group, taking `[6]`'s matcher and case guard (which is a strict
superset of the babysit guard: every payload matching `*gh*pr*create*` also
matches `*gh*pr*`).

```json
{
  "matcher": "^(Bash|mcp__.*[Gg]it[Hh]ub.*__.*)$",
  "hooks": [{
    "type": "command",
    "timeout": 15,
    "statusMessage": "MemHub: checking PR context",
    "command": "IN=$(cat); case \"$IN\" in *gh*pr*|*[Gg]it[Hh]ub*|*api/v3*|*repos/*pulls*) if [ -n \"${CLAUDE_PLUGIN_ROOT:-}\" ] && printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/claude_hook_guard.py\" ignore PostToolUse; then printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/pr_post_context.py\"; fi ;; esac"
  }]
}
```

**Position is deliberate: index 0.** While the harness keeps the earliest
context, every later Bash emitter — reactive directive recall and the rulebook
post handler — can displace the PR context from any other index. The index is a
mitigation, not the fix; the fix is that our own two lanes can no longer
displace each other.

**Timeout is 15s**, the tighter of the two budgets replaced. The only I/O is
`pr_link.check` (4s); the babysit lane is regex over the payload. The merge
therefore cannot make a `PostToolUse` call slower than the link hook alone is
permitted to be today.

`claude_hook_guard.py ignore PostToolUse` stays first, as in every other
handler.

### 3.4 Behaviour table — nothing changes except that both instructions arrive

| call | link lane | babysit lane |
|---|---|---|
| `gh pr create` (URL in stdout) | B1 self-link | arms | 
| `gh pr create` that failed / no URL | B2 judge (create downgraded) | silent |
| `gh pr view 761 --json url` | B2 judge | silent |
| `mcp__github__create_pull_request` | B1 self-link | **silent** (unchanged) |
| `curl -X POST …/pulls` | B2 judge | silent |
| org without GitHub connected | advisory | silent |
| server unreachable / no credential | silent + breadcrumb | unaffected |

The babysit lane is gated on the tool name matching `re.compile("Bash")` —
**unanchored, replicating the harness matcher `"Bash"` it is replacing**, so
its trigger surface (including `BashOutput`, which carries no command and is
silent anyway) is byte-for-byte what it was.

### 3.5 Degradation

Unchanged from both lanes today: backend down, unauthenticated, org without
the feature, malformed payload, non-dict payload → **silence and exit 0**.
Silence is the normal outcome. `/memhub:link-pr` and `/memhub:pr-babysit`
remain the manual paths.

### 3.6 Host parity

Codex and Cursor are **unchanged** — the bridge already merges, and
`gen_codex_hooks.py` builds Codex's manifest from a fixed dispatch rather than
from these groups. Two invariants must be re-pinned:

- `gen_codex_hooks.CLAUDE_ONLY_CAPTURE` gains `"pr_post_context.py"`, so the
  merged entry point (which can arm babysit) can never mount on Codex.
  `tests/codex_hooks_parity_test.py` asserts the same tuple.
- `generate()`'s required-script check still passes: it names
  `directive_recall.py` and `artifact_sync_reminder.py`, neither of which this
  change touches.

Prod and staging behave identically — `plugins/memhub-staging` is the same
tree by symlink.

### 3.7 Compatibility with installed caches

None needed. Hook manifests are cached per version
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), so an install
runs either the old two-group manifest with the old scripts, or the new
one-group manifest with the new scripts. There is no mixed state.

---

## 4. Tests

New: `tests/pr_post_context_test.py`, house style (stdlib only, no pytest,
`main()` printing `PASS …`, isolated by tmpdir + `MEMHUB_*` env overrides, a
stub `checker` so nothing reaches a live backend). It must cover:

1. `gh pr create` with a PR URL → **one** document, `additionalContext`
   containing both instructions, **link text first**.
2. `gh pr view` → one document, link text only.
3. A GitHub MCP create → link text only, babysit absent.
4. A raising link lane → babysit context still emitted, exit 0.
5. A raising babysit lane → link context still emitted, exit 0.
5b. A lane that cannot be IMPORTED (module-scope failure, outside `_lane`) →
   the other lane still emitted, exit 0, no traceback.
5c. The two lanes naming DIFFERENT pull requests → the babysit half dropped,
   the link instruction emitted alone (the hardened extractor wins; arming a
   loop on a pull request the session never opened is worse than arming none).
   Compared on IDENTITY — host, owner, repo, number, case-insensitively — not
   on the URL text: the backend answers with the owner lowercased
   (`XTraceAI` → `xtraceai`), and a byte comparison read gh's own URL as a
   different pull request, suppressing babysit on every repo whose owner has a
   capital in it. A differing NUMBER is still a disagreement.
5d. The merged group is registered ahead of every other synchronous handler
   that matches `Bash` and can emit context.
6. Both lanes silent → **no stdout at all**, exit 0.
7. Malformed / non-dict stdin → no traceback, no stdout, exit 0.
8. The output parses as one JSON object with exactly one
   `hookSpecificOutput`.

Amended:

- `tests/claude_hook_guard_test.py` — `len(commands)` 19 → 18, comment updated
  to say why (two PR-lane handlers became one).
- `tests/documentation_test.py` — the loop that finds the `pr_link_trigger`
  registration now looks for the merged entry point, **and asserts it was
  found before asserting the quote**. Today a rename makes the assertions go
  vacuous rather than fail; that trap is closed as part of this change.
- `tests/codex_hooks_parity_test.py` — passes unchanged once
  `CLAUDE_ONLY_CAPTURE` is updated; it reads the constant.

Green required from **both**:

```
python3 tests/run_all.py
uv run --with 'mcp<2' python tests/run_all.py
```

Plus a hook smoke: realistic event JSON piped into `pr_post_context.py` must
exit 0, print nothing on the unhappy path, and finish well inside 15s.
Any manual run goes to staging with `MEMHUB_MCP_BASE_URL` set explicitly, and
is cleaned up with `scripts/purge_today.py`.

---

## 5. Docs and release

- `README.md` — the file tree gains `pr_post_context.py`; the **PR
  babysitting** and **Session ↔ PR linking** sections say that one hook now
  carries both instructions and why (a `PostToolUse` call yields one context).
- `docs/specs/pr-linking-plugin-spec.md` — revised so the matcher and case
  guard it quotes are the shipped merged ones.
- `plugin.json` / `marketplace.json` descriptions — updated where they
  describe the two hooks as separate, since those descriptions are
  documentation.
- Version **0.53.3** (a fix; 0.53.2 was taken by #187 mid-flight), bumped in **all five** manifests together:
  `plugins/memhub/plugin.json`, `plugins/memhub/.claude-plugin/plugin.json`,
  `plugins/memhub/.codex-plugin/plugin.json`,
  `plugins/memhub/.cursor-plugin/plugin.json`, and
  `plugins/memhub-staging/.claude-plugin/plugin.json`
  (`tests/version_parity_test.py`).
- Nothing dev-only may land under `plugins/`.

---

## 6. Done means

`gh pr create` yields **one** `additionalContext` carrying the link
instruction followed by the babysit instruction; every other GitHub call
behaves exactly as it does today; both test runs green; the two memhub
manifests in version parity; Codex output unchanged.
