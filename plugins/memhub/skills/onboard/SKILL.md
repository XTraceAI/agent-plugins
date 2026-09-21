---
description: Use when a new user wants to set up MemHub for their repo, or asks to "onboard", "get started", "set up my brain", or "connect this repo to MemHub". Logs the plugin in, creates or reuses the repo's agent brain, scans the repo for its own markdown documents wherever it keeps them (specs, designs, ADRs, runbooks, guides — no directory layout assumed), saves the ones that look important to the brain as artifacts without asking (folders or files given as arguments override the choice), shows the brain's Index, and ends with an optional hint that `/memhub:start-rulebook` creates the team's rulebook.
argument-hint: [folder-or-file ...]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub_memhub__get_brain_overview, mcp__plugin_memhub_memhub__search_memory, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__get_brain_overview, mcp__plugin_memhub-staging_memhub__search_memory, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Onboard a new user onto MemHub for the repo they're in. Three things, in this
order, and then stop:

1. **Connect** — the plugin's own login, and the repo's brain (its "room").
2. **Stock the brain with what the repo already knows** — its design docs,
   saved as artifacts. A brain that holds the team's specs is useful to the
   next agent today; an empty one is not.
3. **Point at the Rulebook** — `/memhub:start-rulebook` is where the team's
   rules come from. This skill names it and ends.

What this skill does **not** do: import a session. Sessions are captured
automatically from the next turn on (§4), and the server does not mine rules
or facts out of them — a session yields its transcript (the Sessions view) and
task episodes, nothing else. Rules are authored through the Rulebook. So there
is no "seed session" to pick and no recall to prove; do not add either back.

Arguments: `$ARGUMENTS` — optional folders or files to add. Given → still run
the scan in §2 (it builds the manifest the upload needs) and upload exactly
those with `--only-folder` / `--path`.

Do exactly this:

## 0. Authenticate the plugin itself (before anything else)

```bash
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/login.py" --status
```

`uv: command not found` → the plugin's scripts and hooks all need `uv`; send
the user to https://docs.astral.sh/uv/ to install it, then start over. Not
logged in → run `/memhub:login` (no `--status`) and let it finish before
continuing. Everything below needs it: the uploads in §2 send as this user,
and the capture hooks cannot run without it.

This is **not** the `/mcp` connector's login. They share an Auth0 client but
store tokens in different places, so "connected in `/mcp`" and "my sessions are
being captured" are independent facts — never treat the first as evidence of the
second. What the hooks actually use is a **personal access key** (`mhk_…`) that
`/memhub:login` mints and stores at `~/.config/memhub-plugin/pak-<host>.json`: a
static bearer, because a hook is a cold background process that can never open a
browser to refresh an expiring token. See `/memhub:login` for the full story.

## 1. Resolve the repo room (the durable boundary)
- Derive the room name from the repo: `Repo: <org>/<name>` from
  `git remote get-url origin` (host + `.git` stripped).
- `list_agent_brains` → **exact-name match**. Reuse the existing id if found (a
  teammate may have created it). **Only** `create_agent_brain` when there is no
  exact match — do NOT mint a second room for a repo that already has one, and
  give it a real one-line description plus `category: "repo"`, which is what
  declares this brain a code repository's room rather than leaving it
  uncategorised.
- Edge cases (SSH remotes, no remote, worktrees, **not a git repo at all**) and
  the full create-time rules are in
  `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md` — read it if the common path
  above doesn't apply cleanly.
- Record the `agent_brain_id`; call it `ROOM`. Also note the org the brain
  lives in: the `org_id` you passed to `list_agent_brains` / `create_agent_brain`,
  or — if you passed none — the default org's `org_id` from `list_orgs` (the
  response's `scope` only carries `org_name`, not the id). Accounts in a single
  org can skip this.
- **Cache it — so capture routes to this room from the very next turn:**

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" set --brain-id "<ROOM>" --org-id "<ORG_ID>"
  ```

  The capture paths — the per-turn `Stop` flush, the commit/PR flush, and the
  `SessionEnd` backstop — also resolve the room themselves on a cache miss
  (exact-name lookup, then cached), so they only fall back to personal memory
  when no brain of this exact name exists yet. Caching here is still what
  makes the FIRST capture after onboarding route without a lookup, and
  `--org-id` is what lets captures reach a room outside the caller's default
  org (a brain lives in exactly one org; without it the write fails with
  "Agent brain not found" in multi-org accounts, and the entry gets re-probed).
  It writes to `~/.config/memhub-plugin/rooms.json` — the user's own config,
  never the repo — and covers every worktree of this repo. Teammates run
  `/memhub:onboard` once themselves.

## 2. Stock the brain — the repo's own documents, as artifacts

Every repo keeps its knowledge somewhere different — `docs/`, `design/`,
`rfcs/`, a `handbook/`, READMEs beside each service — so **assume no layout**.
A script finds the documents and scores them; the important ones go in without a question.

**Look first.** `get_brain_overview(ROOM)` and read `index_markdown`. A brain a
teammate already onboarded lists its artifacts there — say what it holds, and
below offer only what is missing. Re-uploading is harmless (the same name
versions the artifact, and identical content is deduplicated server-side) but
it is noise; don't.

**Scan** (stdlib, no network, reads nothing outside the repo):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard_docs.py" scan --out "${TMPDIR:-/tmp}/memhub-onboard-docs.json"
```

It lists every tracked markdown document (`.md` / `.mdx` / `.markdown`; every
one on disk when the directory is not a git repo), **grouped by folder**, with
a score per document from its own name and content — spec / design / ADR / RFC
/ runbook wording, a link from the root README, real structure, real length.
It prints what it left out and why: vendored and generated trees, licences and
changelogs, stubs, and **agent instruction files** (`CLAUDE.md`, `AGENTS.md`,
`.claude/` …) — those are excluded on purpose, because
`/memhub:start-rulebook` turns them into rules that fire at the moment they
matter, where an artifact copy would only be read once, like the file is.
The full list is in the `--out` manifest; read it when the printed top rows of
a folder don't tell you what the folder is.

**Upload them — do not ask first.** The user ran onboarding to get their
repo's knowledge into its brain, and the brain needs the specs there for
anything downstream that checks code against them (spec drift, PR review). A
question here is a step a new user cannot answer well — they do not yet know
what the brain is for. So decide from the scan and go:

- **In:** every document the scan scored as important (score ≥ 3) — specs,
  designs, ADRs, RFCs, runbooks, guides, the root README — from every folder.
  There is no cap: a repo with sixty real specs gets sixty artifacts.
- **Out, automatically:** shelved folders (`archive/`, `retired/`,
  `deprecated/` … — the scan already scores them below the bar), and
  everything the scan skipped.
- **Out, by one check you make:** a folder the scan marked `[spec dir: may
  already be mirrored]` **when the Index you read above already lists those
  specs** — a repo on the Git spec workflow (`/memhub:spec`) has that directory
  mirrored into the brain by the backend, and a hand upload would be a second,
  competing copy. Pass it as `--exclude-folder`. If the Index lists none of
  them, the folder goes in like any other — those specs are exactly what the
  brain is missing.
- **Out, by reading:** a document that plainly carries credentials (an `.env`
  block, a token). Files are uploaded as they are, nothing is redacted, and
  this brain is shared with the team — `--exclude` it and say so in the report.

One command; it saves each document through `save_artifact.py`, continues past
a failure, and exits non-zero naming every file that failed:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/onboard_docs.py" upload \
  --manifest "${TMPDIR:-/tmp}/memhub-onboard-docs.json" --min-score 3 \
  [--exclude-folder "<mirrored spec dir>"] [--exclude "<file with credentials>"]
```

The user can still steer it, before or after:

- folders or files given as arguments to this skill → upload exactly those:
  `--only-folder "<folder>"` / `--path "<file>"` instead of `--min-score 3` (a
  named path is uploaded whatever it scores; `--only-folder .` is the repo
  root's own documents);
- "everything" → drop `--min-score`;
- `--dry-run` prints the exact list and sends nothing — use it when they ask
  what would go in;
- a document the scan did not list (another format — `.rst`, `.txt`, a PDF)
  is not in the manifest: save it with `/memhub:save-artifact` instead.

Do NOT pass a brain id. Each save routes to the room cached in §1 — that cache
entry is also what carries the room's org, which a bare brain id would lack
("Agent brain not found" in a multi-org account). Names and types are taken
from the manifest, which derives them with the same rule automatic capture
uses (§4), so a later edit of an uploaded file versions that artifact instead
of starting a second one. Tags default to the document's type plus its folder;
if the org requires tags from its own vocabulary the save is refused with that
vocabulary in the error — re-run the failed files with `--path … --tags
"<words it offered>"`.

The scripts target the plugin's default endpoint — **production** when
installed as `memhub`, staging when installed as `memhub-staging`. Do NOT pass
`--url` to cross between them: the OAuth client and tenant still come from the
*installed* plugin's `.mcp.json`, so a prod install pointed at staging fails to
authenticate. (Staging is XTrace-internal; see CONTRIBUTING.md.)

Report the result as the script printed it: `saved N of M`, and each failed
path with its error line. Never round a partial upload up to "done".

**Nothing worth adding** (a young repo, or nothing but a stub README) → say so
and move on. Do not pad the brain to have something to show; it fills from real
work (§4).

## 3. Show what the brain holds now
`get_brain_overview(ROOM)` again and show `index_markdown` — it is rendered
from the rows themselves, so the artifacts you just saved appear at once:
*"Here's what your repo's brain holds."* The `overview` prose summary is
compiled asynchronously and may still be `null`; that is normal right after a
first upload — say it will appear on its own, and never report this step as
empty when `index_markdown` rendered.

Then prove it is reachable the way an agent will reach it: one
`search_memory(query="<a topic from one uploaded doc>", memory_type="artifacts",
agent_brain_id=ROOM)` and show the hit. No hit on a doc you just saved usually
means indexing has not caught up — say that, don't retry in a loop.

## 4. Say what happens from here, and end on the Rulebook
Report plainly, with real values: the room (created or reused), the docs saved
(count, and any that failed), and that the Index rendered.

**What is now automatic** — nothing for the user to run:
- every session in this repo is captured turn by turn into MemHub's Sessions
  view (transcript, tools used, the PRs it opened), and its task episodes land
  in this brain;
- a substantial `.md` the agent writes or edits (a report, a design doc — past
  ~6 KB) is saved to this brain as a draft artifact when the turn ends, under
  the same name rule as §2, so an edit to a doc saved above versions it. The
  exception is the folder the scan marked as the spec dir, if there was one:
  automatic capture never touches it (it belongs to the Git spec workflow), so
  a spec uploaded from there stays as it is until someone re-saves it with
  `/memhub:save-artifact` or sets up `/memhub:spec`. Only say this when the
  user actually uploaded from that folder.

**Tell them to restart Claude Code**, and what they will see when they do: every
session in this repo now opens with a line naming the brain, and the agent
receives the brain's map as context before the first prompt. That is also the
fastest way to confirm §1 took, since the brief only appears once a room
resolves. From then on `/memhub:search-memory` searches this brain alongside
personal memory.

**End with a hint about the Rulebook — optional, one or two lines, and do not
start it.** Onboarding is finished at this point; the Rulebook is something to
try next *if they want it*, not a remaining step. The brain is what the agent
can *look up*; the Rulebook is what it is *told at the moment it matters*
("you're about to force-push"). Say something like: *"You're set up. If you'd
like to try rules next, `/memhub:start-rulebook` creates a rulebook for your
team and proposes its first rules — the starter set takes about a minute, and
nothing it files turns on until you say so."* A hint, not a step: the rulebook
skill opens with questions of its own, and it is run once per team rather than
once per person — so say it and stop, even if they seem keen. This skill cannot see whether a rulebook already exists (it
has no rulebook tools on purpose), which is why the line says "first rules" and
lets `/memhub:start-rulebook` notice an existing book itself.

**And name the other half, in one line — do not start it.** The brain is what
the agent *remembers*; the Rulebook is what it is *told at the moment it
matters* ("you're about to force-push"). This skill sets up only the first, and
a new user has no way to know the second exists. So end with: *"Next, when you
have a minute: `/memhub:start-rulebook` gives your team its first rules — the
starter set takes about a minute, and nothing it files turns on until you say
so."* A pointer, not a step: onboarding is judged on time-to-first-recall, the
rulebook skill opens with questions, and it is run once per team rather than
once per person — so say it and stop, even if they seem keen. This skill
cannot see whether a rulebook already exists (it has no rulebook tools on
purpose), which is why the line says "first rules" and lets
`/memhub:start-rulebook` notice an existing book itself.

Plain-English output throughout. If a step fails on authentication, send the
user to `/memhub:login`, not to `/mcp` — the hooks and the scripts here use the
plugin's own credential, and a connected `/mcp` says nothing about whether they
have one.
