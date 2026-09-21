---
name: spec-work
description: Use when implementing a feature against repository specs, finding requirements before coding, or turning a Git or Brain spec into an implementation plan and acceptance checks. Also handles /memhub:spec work. Do not use merely because an unrelated document is called a spec.
argument-hint: <feature|task|files>
---

# Work with specs

Read [shared context](../../references/spec-workflow.md). Resolve the configured
source and requested scope before planning changes.

1. Find governing requirements. For Git, read active ownership specs for the
   proposed files and relevant feature specs for new files. For Brain, retrieve
   relevant documents within the selected governing brain and read the sections
   being relied on. Include cross-cutting requirements such as authorization
   where they apply; don't load unrelated documents merely to fill the context.
2. Inspect the implementation and tests. Separate existing behavior, required
   behavior, and proposals. A statement in code is evidence of implementation,
   not proof the team intended it.
3. Give a concise work brief: source/document and revision, requirements that
   constrain this task, files likely to change, acceptance checks, and unresolved
   decisions. Cite the source section and relevant code. An uncovered new file
   is a proposed ownership addition, not silently owned by a nearby spec.
4. If the task contradicts a governing requirement, identify the exact conflict.
   Reuse an explicit user decision to change intended behavior; otherwise ask
   which behavior should govern before implementing the conflicting part.
   Continue independent work. Don't ask again about decisions already made.
5. Carry out the user's requested scope. A planning request produces a plan;
   an implementation request proceeds with code and meaningful acceptance
   checks. Keep necessary Git spec edits in the same change. In Brain mode,
   prepare a revision proposal if requirements change; don't mark it published.
6. Follow [Check specs](../spec-check/SKILL.md) on the actual resulting diff.
   Summarize implemented requirements, validation, proposed spec changes, and
   remaining gaps. Do not set verification markers merely because tests pass.

If no relevant requirement is found, say what was searched and which code is
uncovered. Offer or perform `spec init` when it is within the user's requested
scope; never manufacture a governing requirement from an old audit summary.
