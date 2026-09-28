---
name: spec
description: Use for MemHub spec-driven development — bootstrap specs locally or in MemHub cloud, find requirements before coding, check a working diff, resolve drift, create or revise specs, or diagnose the repository's spec setup. Supports Git-authored specs and explicitly selected governing Brain documents.
argument-hint: <work|init|revise|bootstrap|check|resolve|status|setup> [file|topic] [--local|--cloud]
---

# Repository specs

Keep `/memhub:spec` as the common entry point. Route to the appropriate bundled
skill below and follow it; do not merely suggest another command to the user.
Relative links resolve from this file, including in an installed plugin.

| Intent / command | Read and follow |
|---|---|
| `work`, implement a feature against specs, find governing requirements | [Work with specs](../spec-work/SKILL.md) |
| `check`, review a diff against specs, investigate an audit finding | [Check specs](../spec-check/SKILL.md) |
| `init`, `revise`, `bootstrap`, `resolve`, `status`, `setup` | [Maintain specs](../spec-maintain/SKILL.md) |

Preserve the user's arguments and existing decisions. `check` means a fresh
comparison with code, not just a search for old audit reports. `bootstrap --local`
uses the coding agent; `bootstrap --cloud` is run by the user from Studio's
Repository → Specs, which also owns the repository's spec settings. See the
maintenance workflow for execution choice and domain confirmation.

With no subcommand, infer the requested task. If there is no task to infer, show
these options briefly rather than scanning or generating an entire repository.
