---
description: Use when a PR should be babysat to green — poll its review bots (any bot reviewer — Cursor BugBot, OpenAI Codex, the MemHub reviewer, others) and CI, fix the real findings, push, and when clean propose a team rule for any finding that taught something the next PR should not repeat (e.g. "babysit this PR", "watch PR 14 and fix the bot findings", or, on Claude Code only, auto-armed by the memhub hook right after `gh pr create` — Codex and Cursor do not run that hook, so there it is started by hand). Designed as the body of a self-paced /loop — one poll→fix→push pass per invocation; the final pass proposes rules (usually none) and ends the loop.
argument-hint: "[pr-number-or-url]"
allowed-tools: 'Bash(git remote get-url origin), Bash(gh pr view *), Bash(gh api repos/*/comments --paginate), Bash(gh api repos/*/reviews --paginate), Bash(gh api repos/*/replies -f body=*), Bash(git pull), Bash(git add *), Bash(git commit *), Bash(git push), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_verify.py" *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook_conflicts.py" *), Write(//tmp/*.json), Edit(//tmp/*.json), Edit(./**), mcp__plugin_memhub_memhub__list_rules, mcp__plugin_memhub-staging_memhub__list_rules, mcp__plugin_memhub_memhub__list_rulebooks, mcp__plugin_memhub-staging_memhub__list_rulebooks, mcp__plugin_memhub_memhub__create_rule, mcp__plugin_memhub-staging_memhub__create_rule, Read, Glob, Grep'
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code
exports it; Codex exports `PLUGIN_ROOT` instead. If it is unset, set it first —
from `$PLUGIN_ROOT` when that is set, otherwise (e.g. on Cursor) to this
plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Babysit a pull request until its review bots are satisfied, then turn what
the review proved into proposed team rules. Each invocation is ONE pass;
state between passes (handled comment ids, each finding's verdict and fix
commit, pass counters) lives in the loop's conversation context — re-derive
nothing that an earlier pass already resolved.

## Every pass

1. **Resolve the PR.** From `$ARGUMENTS` (number or URL) or, absent that,
   `gh pr view --json number,url,state,headRefName` on the current branch.
   PR merged or closed → report that and END the loop (no further passes).
2. **Collect findings** (`{owner}/{repo}` and `{n}` from step 1):
   - `gh pr view <n> --json state,mergeable,mergeStateStatus,statusCheckRollup,commits`
   - `gh api repos/{owner}/{repo}/pulls/{n}/comments --paginate` (inline
     review comments), `.../pulls/{n}/reviews --paginate` (review bodies),
     `.../issues/{n}/comments --paginate` (top-level comments).
   - A finding is an inline review comment, or a review with a non-empty
     body, whose author is a bot — `user.type == "Bot"` — or a FAILING
     required check in `statusCheckRollup`. Match on the account type, never
     on a list of bot names: a repo's own reviewer app is a bot too, and a
     name list is how one went unread for two passes. Top-level comments from
     a bot are findings only when they review the code; a status post (a
     ticket link, a deploy notice, a summary of the PR) is not.
   - Skip: comment ids already handled in a previous pass; reviews with an
     empty body (replying to a thread creates one, on the new head, a minute
     after the push — they are your own replies, not a re-review); a bot's
     own reply withdrawing an earlier finding.
   - A comment from a **person** is not triaged here: the person decides
     what they meant. Note each unresolved human thread for the final
     report.
3. **Triage and fix.** For each new finding, read the code it points at and
   judge it — bots are wrong often enough that "a bot said so" is not a
   reason to change code.
   - Real → fix it on the PR's head branch (check it out if HEAD moved;
     `git pull` first; NEVER force-push). One commit per finding or one per
     coherent batch, message naming what the bot caught. Note the verdict
     and the fix commit; the final pass reads them.
   - False positive → best-effort reply to the comment thread with one line
     of why (`gh api repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies -f body=...`;
     if the reply fails, move on — it's cosmetic).
   - Push once at the end of the pass, after all of the pass's commits.
4. **Decide: another pass, or done?**
   - Pushed fixes this pass → NOT clean; the bots need time to re-review.
     End the turn so the loop re-wakes; bots typically take a few minutes,
     so self-pace around 4–5 minutes.
   - Clean = a pass that pushed nothing AND found no new findings AND no
     required check is failing or pending on the head commit AND the PR
     still merges (`mergeable` is not `CONFLICTING`) AND the bots have had
     their review window: at least one bot review/comment exists for the
     current head commit, OR ~20 minutes have passed since that commit was
     pushed (its `committedDate` from `gh pr view --json commits` vs now —
     review bots that are going to comment usually do within ~20 minutes).
     Right after `gh pr create` neither holds, so an immediate first pass
     can never end the loop. First clean pass after any push → the final
     pass.
   - `CONFLICTING` → the base moved under the PR. Say so and end the loop
     without the final pass: resolving a conflict is the person's call, not
     a finding to fix.
   - Safety valve: findings still arriving after ~10 passes, or the same
     finding reopening repeatedly → stop looping, summarize the impasse to
     the user, and still run the final pass with what happened so far.

## Final pass — propose rules, then end the loop

The review just proved something no other moment proves as well: a finding
was real, and a commit fixed it. When the mistake behind it would recur on
another PR, that is a rule. **None is the usual answer** — most findings are
one-off defects in one diff. Do not import the session transcript and do not
save a review record: capture already ships the session to the user's
personal memory, and a rule is the only thing from this review the team
needs to see again.

1. **Pick the lessons.** Take each finding you judged real and fixed, and
   keep it only if all four hold:
   - **The mistake will recur.** It is a way of writing code, testing it or
     changing it — not a typo, a missing case in this one function, or a
     fact about this PR.
   - **A rule would have prevented it**: something an agent can be told
     before or while it writes the code, in one sentence.
   - **It is general enough to state without this diff.** Name the file,
     symbol or table only when the lesson lives there (then the rule is
     scoped to this repo).
   - **No rule already says it.** Step 2 of create-rule checks this.

   Two findings with one cause are one lesson. A finding caused by your own
   earlier fix in this loop counts only if the cause is the general one
   (e.g. "the fix was not tested against the unfixed code"), never the
   specific bug.

2. **File each lesson** by Reading
   `${CLAUDE_PLUGIN_ROOT}/skills/create-rule/SKILL.md` and following it, on
   the **unattended path** in
   `${CLAUDE_PLUGIN_ROOT}/skills/create-rule/references/harness.md`: nobody
   is asked anything, step 0 files it for the user alone (`scope:
   "personal"`) unless anyone working in the repo needs it, as that file
   says, step
   4b (the live test) does not run, and a conflict it says not to file is
   not filed. What differs from a harness fork:
   - `source: "nomination"` with `author: "xtrace"` — the review, not a
     person, wrote it — and **no** `state` stamp (that is a harness fork's
     alone).
   - `source_ref: "<the PR url>#<the finding's comment id>"`, exactly; a
     re-file under the same ref is answered `unchanged`.
   - `why` names the finding and the fix commit: what went wrong on this PR.
   - A **dismissed** rule that says the same thing is a reviewer's "no"
     (create-rule step 2 lists retired rules for exactly this): file
     nothing for that lesson. The server refuses a dismissed twin only for
     harness drafts, so here the check is yours.

   It lands `proposed`; a reviewer activates it. Never pass `activate`.

3. **Report** to the user, plain English:
   - PR url and title, final state, findings per reviewer with
     fixed / rejected counts.
   - Each rule proposed (its title, who it applies to — the reply's
     `label` — and who activates it: the user for a personal rule, an org
     admin for an org one), or "no rule from this
     review" — and for each real finding you did not turn into a rule, a
     few words on why (one-off, already a rule).
   - Each unresolved human thread, by its first line.

Then END the loop: call `ScheduleWakeup` with `stop: true` so no further
wake-up is scheduled.

If filing fails on authentication, the fixing still stands: report the
lessons you would have filed and send the user to `/memhub:login` — **not**
`/mcp`. The plugin's tools run on the personal access key `/memhub:login`
mints, a separate store from the `/mcp` connector's token, so a connected
`/mcp` does not mean the filing can authenticate. Never fail the babysit over
it.
