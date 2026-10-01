---
description: Use when the user wants the MemHub companion — the small animal above the prompt that shows what the plugin is doing — turned on, turned off, swapped for another animal, or explained (e.g. "turn on the companion", "activate the goose", "enable my companion", "what is that goose above my prompt", "show me the hippo instead", "turn the companion off", "/memhub:companion"). Enables the function-hook runtime the companion needs, persists it, and says what each of its states means.
argument-hint: [on | off | status]
allowed-tools: Bash, Read, Edit
---

The companion is a pixel animal drawn in the band above the prompt — Gus the
Goose, Hugo the Hippo, Penelope the Penguin or Popo the Shiba, one per session, picked from the session
id unless the person pins one. It shows, without costing a token, what MemHub
is doing in the session:

| It does this | Because |
| --- | --- |
| Sleeps | nothing is happening |
| Wakes and looks around | Claude is answering, or you are typing |
| Rises and says a rule | a MemHub rule fired on a tool call |
| Says it crossly | a rule **blocked** that call |
| Hops up under a gold `!` and asks, in a gold bubble — every animal in its own way | MemHub proposed a new rule; it is **not active** yet. The bubble names the rule, and its **rule↗** is clickable — click it to open the rule in MemHub Studio, the page the Stop notice links. Buttons inside the bubble, where others say Got it, answer it — **1 Activate**, **2 Reject**, **3 Later** (press the digit from an empty prompt, or click). Activating takes a rulebook or org admin; for anyone else the animal says so and the rule waits in MemHub Studio (the Stop notice carries the link) |
| Shows a heart | you clicked the small ♥ just left of its ground: that pets it (asleep, it wakes first; saying a rule, the click is Got it and the bubble goes; a proposal is never dismissed by a click) |
| Opens your browser | you clicked a proposal's **rule↗** (or the rule's name on the "new rule waiting:" row): that rule's page in MemHub Studio. Where the machine has no `open` / `xdg-open` (SSH, a container) the click does nothing |

It ships inside this plugin. It needs one thing the plugin cannot turn on for
itself: Claude Code's function-hook runtime, which is early access and off
unless `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` is set for the session.

**Claude Code only.** Codex and Cursor have no equivalent band; say so and stop
if the user is on either.

## `status` (also what to run first for `on`)

```bash
echo "runtime: ${CLAUDE_CODE_ENABLE_FUNCTION_HOOKS:-unset}"
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".claude" / "settings.json"
env = (json.loads(p.read_text()) if p.exists() else {}).get("env", {})
print("settings.json:", env.get("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", "unset"))
PY
```

Both unset means it has never been turned on. `runtime: 1` means it is on in
**this** session already — the band should be showing it.

## `on`

1. Run the `status` block.
2. If settings.json does not have it, add it — reading, merging and writing the
   whole file, never clobbering the other keys:

```bash
python3 - <<'PY'
import json, pathlib, shutil
p = pathlib.Path.home() / ".claude" / "settings.json"
p.parent.mkdir(parents=True, exist_ok=True)
doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
if p.exists():
    shutil.copy2(p, p.with_suffix(".json.bak"))
doc.setdefault("env", {})["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] = "1"
p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
print("set in", p)
PY
```

3. Tell the user to **restart Claude Code** — the runtime is read at startup,
   so it does not appear in the session that turned it on.
4. Then tell them what they will see, in one line each: it sleeps while idle,
   watches while Claude works, and stands up to announce a rule — or hops up with a newly
   proposed one and asks: press 1 to activate it, 2 to reject it, 3 for later.
   Clicking the ♥ beside it pets it, and a proposal's **rule↗** opens that
   rule in MemHub Studio. Each
   session gets its own animal, picked from the session id; each animal is
   its own command — `/goose` for Gus, `/hippo` for Hugo, `/penguin` for
   Penelope, `/shiba` for Popo — and running one switches **this session** to it. `/hippo always`
   pins Hugo for every new session (`/hippo random` undoes it), and
   `/penguin never` / `/penguin include` leave Penelope out of, or put her
   back in, the random pick. `/goose off` hides it, `/goose` brings it back,
   `/goose demo` plays the whole loop — a rule firing, a rule blocking, and a
   new rule proposed with its Activate / Reject / Later buttons —
   `/goose propose` loops just the proposal, `/goose pet` the pet (every
   animal has both), and `/goose scale 1-4` sets how chunky it is. The demo's
   buttons only say what they would do; nothing reaches MemHub until a real
   proposal arrives.

## `off`

Two different things — ask which, or read it from how they asked:

- **Hide the animal, keep the runtime:** `/goose off` in the session — any
  animal's command hides whichever is showing. It is remembered, and running
  that command again brings it back. Nothing else changes.
- **Turn the runtime off entirely:** remove
  `env.CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` from `~/.claude/settings.json` with
  the same read-merge-write shape as above, and say that a restart is needed.
  Mention that this also turns off any other mod, not just the companion.

## Which animal

`plugins/memhub-staging/companion/animals/` holds them, one folder each, and
every one is its own command: Gus is `/goose`, Hugo is `/hippo`,
Penelope is `/penguin`, Popo is `/shiba`. Which one a session shows, first match wins:

1. the animal run in **this session** (`/hippo`) — remembered under the
   session id, so a resume shows it again;
2. the **pin**, `/<animal> always` — for every new session; `/<animal> random`
   drops it;
3. otherwise the **session id** picks, from every animal except those left
   out with `/<animal> never` (`/<animal> include` puts one back; the last one
   cannot be left out).

A `/clear` or `/resume` moves to another session id, so it may bring a different animal. A new animal is written to
the contract in `companion/animal.ts`; in the development repo
`docs/companion/ANIMALS.md` is the guide and `docs/companion/DESIGN-BRIEF.md`
is what to hand a designer.
