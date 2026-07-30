---
description: Use when a PR should be babysat to green — poll its review bots (Cursor bugbot, OpenAI Codex) and CI, fix the real findings, push, and when clean save the whole fixing process to the repo's MemHub room (e.g. "babysit this PR", "watch PR 14 and fix the bot findings", or auto-armed by the memhub hook right after `gh pr create`). Designed as the body of a self-paced /loop — one poll→fix→push pass per invocation; the final pass writes the memory and ends the loop.
argument-hint: [pr-number-or-url]
allowed-tools: mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub_memhub__import_conversation, mcp__plugin_memhub-staging_memhub__import_conversation, Bash, Read, Edit, Write, Glob, Grep
---

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
   Name `Repo: <org>/<name>` from `git remote get-url origin` (host and
   `.git` stripped). Match it EXACTLY in `list_agent_brains` — a teammate may
   have created it; use theirs. No match → `create_agent_brain` (omit
   `workspace_id`). Edge cases (SSH remotes, no remote, worktrees, not a git
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
     so self-pace around 4–5 minutes (stay under the 5-minute cache window).
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

Ship the fixing process into the repo's room by importing THIS SESSION'S
transcript through the **agentic** ingestion path. The session gist — the
structured episode folding goals, decisions, errors, and outcome — is
composed ONLY on the agentic path (Claude Code `.jsonl` transcripts);
hand-built plain-chat `messages` route to the regular bulk importer, where
gist composition is off, and produce loose per-batch episodes instead. The
transcript already contains every finding read, every accept/reject
judgment, and every fix commit — do NOT hand-write `{role, content}` pairs
on top of it.

1. **Resolve this session's transcript**: the most recently modified
   `.jsonl` sitting DIRECTLY inside the `~/.claude/projects/` directory
   matching the current working directory (top level only — `.jsonl` files
   in subdirectories are subagent/workflow transcripts, not sessions).

   **Mixed-session check**: if this session visibly spanned more than one
   repo (you changed working directories into another checkout, committed
   to another repo, or babysat alongside unrelated work elsewhere), the
   import still lands the WHOLE transcript in THIS repo's room — the other
   repos' work included, while their rooms get none of it. Do NOT try to
   slice the transcript; import as-is and say exactly that in the report
   (step 4) so nobody mistakes the room for repo-pure memory.
2. **Import via the helper script** — never call `import_conversation`
   with transcript content yourself; the script handles any size and
   auto-chunks huge sessions:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
     --session "<transcript-path>" \
     --conversation-id "pr-babysit-<owner>-<repo>-<n>" \
     --title "PR babysit — <owner>/<repo>#<n>" \
     --agent-brain-id "<repo-room-id-from-step-2>"
   ```

   The deterministic `--conversation-id` keeps re-runs incremental: a later
   babysit of the same PR folds the gist forward instead of duplicating.

   The script exits non-zero with the server's error on stderr when an
   import is rejected — that is a FAILED save, never report it as queued.
   If the error says the agent brain can't be found or accessed, the
   cached room id went stale mid-loop: re-resolve the room ONCE (re-run
   "Every pass" step 2 — exact-name match in `list_agent_brains`, create
   if missing) and retry the import with the fresh id. Fails again →
   report the error in step 4 instead of retrying further.
3. **Verify the path**: the script's output should report `path:
   "agentic"`. If it reports `"regular"`, the transcript resolution picked
   a wrong file — stop and re-resolve rather than accepting a gist-less
   import.
4. Add one short top-level outcome note IN THE REPORT to the user (not as
   extra imported messages): PR url and title, branch, findings per bot
   with accepted/rejected counts, and any repo-specific gotcha or bot
   false-positive tendency observed.

Fallback: if no transcript file can be found (e.g. a headless runner with
no `~/.claude/projects/` dir), fall back to ONE `import_conversation` call
with plain-chat pairs per finding (user = bot name + finding verbatim +
`file:line` + PR/commit refs; assistant = the fix + commit SHA, or the
rejection rationale), same `conversation_id`/`title`/`agent_brain_id` —
and tell the user this path produces episodes and facts but NO session
gist.

Then report to the user (PR state, what was fixed, where the memory went)
and END the loop — do not schedule another wake-up.

Plain-English output throughout. If the memhub MCP is not
connected, do the fixing anyway and tell the user the memory save needs
`/mcp` authentication — don't fail the babysit over it.
