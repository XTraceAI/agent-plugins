# ENG-1106: Codex Rulebook context and host routing

Some desktop builds normalize `exec_command` into a Bash hook payload containing
only `command` and the session `cwd`. An explicit `workdir` used by the shell is
absent. In a projectless task, Rulebook therefore cannot identify the checkout.
Directive recall can still run; it is not evidence of formal rule evaluation.

The plugin now resolves a leading literal `cd <checkout> && ...` when the session
has no repository. It does not infer a checkout from siblings, shell expansions,
mid-command changes, or a `cd ...;` that can continue after failure. A Codex
SessionStart notice explains the workaround. Existing repository-rooted scope
semantics are unchanged. Starting in the checkout is required for session-start
rules; the shell workaround does not retroactively deliver those rules.

The staging build now has a real Codex manifest selecting the Codex hook bridge.
Claude compatibility handlers skip Codex invocations, while genuine Claude
transcripts take precedence over an inherited CODEX_THREAD_ID. Bundled Codex
handlers defer per event to an installed matching user bridge. That bridge still
requires host-controlled trust; setup does not approve handlers.

The remaining upstream requirement is for Codex to preserve the effective shell
working directory in both PreToolUse and PostToolUse. The plugin cannot recover
an omitted value reliably, and does not parse arbitrary JavaScript or replay
transcripts to guess it. A complete upstream fix should also cover file edits
and calls executed outside the task's initial directory.

Verification includes an isolated invocation of the actual Rulebook engine:
a normalized projectless payload is silent, an explicit checkout prefix fires
one fixture rule, and the ledger contains one row with host `codex`. The new
behavioral case moves a disposable fixture's Git metadata into a child checkout
and expects the explicit `cd` command to be gated. No real user's book is edited.
