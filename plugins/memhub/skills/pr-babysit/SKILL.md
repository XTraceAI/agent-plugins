---
description: Use when a PR should be babysat to green — poll its review bots (Cursor bugbot, OpenAI Codex) and CI, fix the real findings, push, and when clean save a PR review record to the repo's MemHub room (e.g. "babysit this PR", "watch PR 14 and fix the bot findings", or auto-armed by the memhub hook right after `gh pr create`). Designed as the body of a self-paced /loop — one poll→fix→push pass per invocation; the final pass writes the memory and ends the loop.
argument-hint: [pr-number-or-url]
allowed-tools: mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__save_artifact, mcp__plugin_memhub-staging_memhub__save_artifact, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs, Bash, Read, Edit, Write, Glob, Grep
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Babysit a pull request until its review bots are satisfied, then bank what
was learned into team memory. Each invocation is ONE pass; state between
passes (handled comment ids, the room id, pass counters) lives in the
loop's conversation context — re-derive nothing that an earlier pass
already resolved.

## Every pass

1. **Resolve the PR.** From `$ARGUMENTS` (number or URL) or, absent that,
   `gh pr view --json number,url,state,headRefName` on the current branch.
   PR merged or closed → report that and END the loop (no further passes).
2. **Resolve the repo's room** (first pass only — reuse the id afterwards).
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show` prints it when the
   repo is already cached — take that id and skip the lookup. Otherwise: name
   `Repo: <org>/<name>` from `git remote get-url origin` (host and `.git`
   stripped), match it EXACTLY in `list_agent_brains` — a teammate may have
   created it and shared it with you; use theirs. No match → `create_agent_brain`
   (omit `workspace_id`) — but note that a miss here does not prove the room is
   absent, only that none is shared with YOU, and that what you create is
   private to you until shared. Say so in one line when you create one, and
   point at `/memhub:onboard`, which does the sharing with a user present to
   approve it. Do not share from inside a babysit pass. Either way, persist what
   you resolved with `room_map.py set
   --brain-id <id> --org-id <org-id>` (the org id is the one you passed to
   `list_agent_brains`, or the default org's from `list_orgs` — the response's
   `scope` carries only `org_name`) so later passes and the capture hooks
   route without repeating this lookup. Edge cases (SSH remotes, no remote, worktrees, not a git
   repo) and the create-time rules — resolve before create, required
   description, report where it landed — are in
   `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md`.
3. **Collect findings** (`{owner}/{repo}` and `{n}` from step 1):
   - `gh pr view <n> --json state,mergeable,statusCheckRollup`
   - `gh api repos/{owner}/{repo}/pulls/{n}/comments --paginate` (inline
     review comments), `.../pulls/{n}/reviews --paginate` (review bodies),
     `.../issues/{n}/comments --paginate` (top-level comments).
   - A finding is: a comment/review from a bot reviewer — login containing
     `cursor` or `bugbot` (Cursor BugBot) or `codex`/`chatgpt` (OpenAI
     Codex), typically with a `[bot]` suffix — or a FAILING required check
     in `statusCheckRollup`. Skip comment ids already handled in a previous
     pass.
4. **Triage and fix.** For each new finding, read the code it points at and
   judge it — bots are wrong often enough that "a bot said so" is not a
   reason to change code.
   - Real → fix it on the PR's head branch (check it out if HEAD moved;
     `git pull` first; NEVER force-push). One commit per finding or one per
     coherent batch, message naming what the bot caught.
   - False positive → record the rejection rationale for step 6, and
     best-effort reply to the comment thread with one line of why
     (`gh api repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies -f body=...`;
     if the reply fails, move on — it's cosmetic).
   - Push once at the end of the pass, after all of the pass's commits.
5. **Decide: another pass, or done?**
   - Pushed fixes this pass → NOT clean; the bots need time to re-review.
     End the turn so the loop re-wakes; bots typically take a few minutes,
     so self-pace around 4–5 minutes.
   - Clean = a pass that pushed nothing AND found no new findings AND no
     required check is failing or pending on the head commit AND the bots
     have had their review window: at least one bot review/comment exists
     for the current head commit, OR ~20 minutes have passed since that
     commit was pushed (its `committedDate` from
     `gh pr view --json commits` vs now — review bots that are going to
     comment usually do within ~20 minutes). Right after `gh pr create`
     neither holds, so an immediate first pass can never end the loop.
     First clean pass after any push → proceed to step 6.
   - Safety valve: findings still arriving after ~10 passes, or the same
     finding reopening repeatedly → stop looping, summarize the impasse to
     the user, and still do step 6 with what happened so far.

## Final pass — save the process to MemHub, then end the loop

Save a **PR-scoped artifact** — the review record — into the repo's room.
Do NOT import the session transcript. The Stop-hook capture already ships
this session to this same room continuously, routed by the same room cache the
capture hooks use,
so an import would write a SECOND copy of the same conversation under a
different id. Two transcripts of one session in one room produce competing
facts and episodes that BOTH surface in retrieval — the exact failure
artifact versioning exists to prevent — and it costs megabytes to do it.

What capture does not record is the judgment: which findings were real,
which were rejected and why, and which commit answered each. That is this
step's whole value, and it is a page of text.

1. **Compose the review record.** Write it yourself from the pass history in
   this loop's context — you read every finding and made every call, so
   nothing needs re-deriving. Keep it to what a future reader needs:
   - **PR** — url, title, head branch, final state.
   - **Findings** — one entry each: the bot, the finding in one line, the
     verdict (fixed / rejected), and the fix commit SHA or the rejection
     rationale. Rejections matter MORE than fixes here; they are the part
     no diff records.
   - **Patterns worth carrying** — a bot's false-positive tendency, a
     finding created by an earlier fix, a repo-specific trap. Skip this
     section rather than padding it.
2. **Save it** into the repo's room with `save_artifact`:
   `name: "PR review record — <owner>/<repo>#<n>"`,
   `artifact_type: "document"`, `tags: ["pr-review", "<repo>"]`,
   `agent_brain_id: <repo-room-id-from-step-2>`.

   The stable `name` is load-bearing: saving it again VERSIONS the record,
   so a later babysit of the same PR supersedes the earlier one instead of
   competing with it in retrieval.

   `save_artifact` failing with the brain not found usually means the
   WRONG-ORG lookup, not a stale id — CLI/MCP calls resolve the caller's
   default org, which follows the org last selected in the MemHub app, and
   a repo room in another org is invisible from it. Re-resolve the room
   ONCE (re-run "Every pass" step 2); still failing → report the error in
   step 4 rather than retrying.
3. **Never import the transcript.** Per-turn capture already ships this
   session, as it happens, into the room its `cwd` resolves to. Importing it
   again would re-upload megabytes for the watermark to discard, and into a
   different room it would extract the same session's facts and episodes a
   second time.

   The one gap: capture routes by the session's `cwd`, this babysit routes by
   the PR's repo, so if you babysat a PR in repo B from a checkout of repo A,
   B's room gets the artifact but not the reasoning trail. Do not paper over
   it with an import — say so in the report (step 4), naming the room the
   session DID land in, and let the user run `/memhub:import-session` if they
   want it in B as well. That skill is the only one that imports, and it
   imports under the session's own id.
4. Add one short top-level outcome note IN THE REPORT to the user: PR url
   and title, branch, findings per bot with accepted/rejected counts, and
   any repo-specific gotcha or bot false-positive tendency observed.

Then report to the user (PR state, what was fixed, where the memory went)
and END the loop: call `ScheduleWakeup` with `stop: true` so no further
wake-up is scheduled.

Plain-English output throughout. If the memory save fails on authentication, do
the fixing anyway and send the user to `/memhub:login` — **not** `/mcp`. The
save runs through the plugin's own credential (the personal access key
`/memhub:login` mints), which is a separate store from the `/mcp` connector's
token, so a connected `/mcp` does not mean the save can authenticate. Never fail
the babysit over it.
