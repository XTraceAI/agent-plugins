---
description: Use when handing a coding agent a self-contained piece of work to run autonomously — "here's the brief", "kick this off", "implement spec items X–Y and open a PR" — or when a request is big enough that the agent would otherwise guess at sources of truth, environment, or definition of done. Pins those four things before any work starts.
argument-hint: [the brief text, or a path to a brief file]
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
---


# Kickoff brief

A brief is the highest-yield way to hand work to an agent: across 172 local
coding sessions, every session that opened with an explicit brief finished
with zero mid-task redirections; sessions that opened with an open question
averaged 2–3. The difference is that a brief pins the four things an agent
otherwise guesses at.

Works in any agent host (Claude Code, Codex, Cursor): it is a document
contract, not a tool. Nothing here depends on host-specific commands.

## When you are the agent RECEIVING a brief

1. Read the brief in full. Restate the GOAL in one sentence of your own.
2. List exactly which files, branches, endpoints, and brains you will read
   as sources of truth — paths, not descriptions. If the brief names
   `origin/<branch>`, run `git fetch origin` before reading it.
3. Name the ENVIRONMENT you will hit (prod / staging / local) for every live
   read or write. If it is ambiguous, ask — do not guess.
4. Restate the DEFINITION OF DONE as checks you will actually run (the full
   test command, not a piped/truncated one; the smoke-import; the PR state).
5. Confirm OUT OF SCOPE. The default out-of-scope for this team: merging a PR,
   force-pushing, editing shared settings/marketplace registrations, writing
   to prod data.
6. Stop and wait for "go" unless the brief says to proceed without one.
7. While working: when you hit a decision the brief does not cover, finish
   everything that does not depend on it, then ask — do not silently pick.
8. Finish with a report shaped like the brief: goal → what was done → the
   real DoD outputs pasted (test summary line, PR URL, exit codes) → what
   was left out and why.

## When you are WRITING a brief for an agent

Use this template verbatim; every section is load-bearing.

```
KICKOFF BRIEF
GOAL: <one sentence>
SOURCES OF TRUTH (read these, not summaries):
  - <repo path / origin/<branch> / PR # / external repo to clone / brain name>
ENVIRONMENT: prod | staging | local — <which, and for what>
DEFINITION OF DONE:
  - <full test suite command> green — paste the summary line
  - <smoke-import / live probe> — paste the output
  - PR opened against origin/<base>, self-audit clean
OUT OF SCOPE: merging, force-push, shared settings, prod data, <extras>
Before you start: restate the goal, list the exact files/branches you will
read, name the environment. Wait for my "go".
```

## Why each section exists (from real friction)

- SOURCES OF TRUTH — 5 sessions reasoned from a stale local checkout; 4 from a
  README instead of source. Naming the artifact prevents both.
- ENVIRONMENT — 4 sessions did correct work against the wrong environment.
- DEFINITION OF DONE — a `pytest | tail` masked a failure that got pushed;
  "done" must name the unpiped command.
- OUT OF SCOPE — one PR was merged autonomously after a "babysit to green" ask.
