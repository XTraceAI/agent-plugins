---
description: Use when asked for an audit, critique, comparison, root-cause, or "is X actually true / working / deployed" — especially "honest audit", "grounded critique", "can you really read their code", "actually test it live", "what's on origin/staging". Forces every claim onto primary evidence (fetched remote, cloned source, live run) and labels what could not be verified.
argument-hint: [the audit / comparison / claim to verify]
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
---


# Grounded audit

The single largest friction class in this team's sessions (13 of 172) is an
analysis delivered from secondary evidence — a README, a paper, a blog post,
a stale local checkout, a prior audit's numbers — that the user then had to
push back on ("can you really read their code", "the memory u did is all
stale", "actually test it live"). This skill makes the evidence base explicit
BEFORE the analysis, so a weak basis is caught in one line instead of two
rounds.

Host-agnostic: only needs a shell and git. Works in Claude Code, Codex, Cursor.

## Procedure

### 1. Evidence block first — before any conclusion

Print this block and fill it honestly. If it would be empty or all
"inferred", stop and gather evidence instead of answering.

```
EVIDENCE BASE
- Remote state: git fetch origin run? <yes/no>  branches read: origin/<...>
- Source read: <repo/path cloned or opened — file list>  (README/abstract/blog = NOT evidence)
- Live probes run: <command → exit code / endpoint → status>
- Memory searched: <brain names>  (repo brain alone is not "all")
- Prior context I am reusing WITHOUT re-checking: <list or NONE>
```

### 2. Rules of evidence

- **Remote, not local.** Any claim about "what's on <branch>" comes from
  `git fetch origin` + `git log/diff/show origin/<branch>`, never from the
  working tree. Say the fetch time.
- **Source, not prose.** Comparing an external system: clone it and cite
  `file:line`. A README or paper describes intent; the code is the fact.
- **Run, don't reason.** "Works", "deployed", "fixed", "blocks auth" — each
  needs a live command or request with its real output pasted. Deploy lag is
  not proof; a green unit test is not a behavioral check.
- **Every brain.** A "not in memory" claim names every brain searched.
- **Stale numbers die.** Do not quote a prior audit's statistic; re-measure
  or mark it `[STALE — unverified]`.

### 3. Claims table

End with one table; no claim outside it:

| # | Claim | Verdict | Evidence (file:line / command + output / URL) |
|---|-------|---------|-----------------------------------------------|
| 1 | …     | CONFIRMED / REFUTED / UNVERIFIABLE | … |

Anything UNVERIFIABLE is stated as an unknown, never smoothed into prose.
Where two sources disagree, show both and say which is stronger and why.

### 4. Adversarial pass (when the host supports subagents)

For a high-stakes audit, hand the claims table to a fresh agent with NO
access to your reasoning and the instruction "assume each claim is wrong;
disprove it from primary sources". Report its dissent verbatim.

## Shape of a good report

Short and honest beats long and confident: evidence block → findings with
citations → claims table → "what I could not verify".
