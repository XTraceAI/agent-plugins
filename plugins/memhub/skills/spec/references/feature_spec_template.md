---
spec: <feature>_spec
owns:
  - app/services/<feature>_service.py
  - app/models/<feature>.py
  - app/api/v1/endpoints/<feature>.py
last_verified_at: null
---
# Feature Spec: <Feature Name>

> **How to use this template**
> Fill out every section before any code is written. A section is complete when an
> engineer who has never seen this feature can implement it correctly from this
> document alone — no Slack threads, no follow-up questions. Leave nothing as
> "TBD" at implementation time. Delete this callout block when the spec is ready
> for review.
>
> Section numbering follows the convention used across docs/ (Overview, Background,
> Data Model, API, Services, Lifecycle, Pydantic schemas, Configuration, Security,
> Open Questions). Drop sections that genuinely don't apply rather than leaving
> empty placeholders.

## Ready-to-implement checklist

All boxes checked before implementation begins:

- [ ] No open questions remain in §10
- [ ] Every new/changed table has exact SQL (column types, constraints, indexes)
- [ ] Every new/changed API endpoint has a full request + response example
- [ ] Every owned file in the frontmatter is listed in §4 with what it does
- [ ] Tenant scoping is named for every read and every write
- [ ] Failure semantics are spelled out (status codes, retries, idempotency)
- [ ] Rollout strategy is defined (feature flag / phased / hard cut)
- [ ] A second engineer has reviewed this doc

---

## 0. Overview

**Problem.** Two-to-four sentences. What is broken or missing today? Who is affected?

**Solution.** Two-to-four sentences. What gets built? Name the major primitives (tables, services, endpoints). The reader should be able to predict §2 + §3 from this.

**This doc owns:** which tables, endpoints, services, workers. Match the frontmatter `owns:` list.

**This doc does not own:** the surfaces that consume this feature, related infra owned by other specs. Cross-reference them.

---

## 1. Background

### 1.1 Why now

The forcing function. Customer request, blocker, deadline, scaling concern. One paragraph.

### 1.2 Prior art

What already exists in the codebase that this builds on or replaces. Reference exact files with `app/...` links so the reader can compare. Mark anything being deleted explicitly.

### 1.3 Constraints

Non-negotiables. Multi-tenancy, backward compatibility, schema-migration shape, latency / cost budgets, external API limits. Anything that pre-shapes the solution.

---

## 2. Data Model

For each new or changed table:

### 2.x `<table_name>`

What it stores, why it's its own table, and the cardinality.

```sql
CREATE TABLE <table_name> (
    <id_col>     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id       UUID NOT NULL REFERENCES orgs(org_id) ON DELETE CASCADE,
    -- ...
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_<table>_<dim> ON <table_name> (<col>);
```

Notes on non-obvious choices (NULL semantics, CHECK constraints, partial indexes, foreign-key actions). Spell out the *why*, not just the *what*.

---

## 3. API

### 3.1 Common conventions

If this spec inherits a common envelope / auth model from another spec, link there. Otherwise document: response envelope, HTTP status codes, pagination, auth header, idempotency.

### 3.2 `<METHOD> /v1/<path>`

One section per endpoint. For each:

- **Auth:** which role / membership.
- **Path / query parameters:** typed table.
- **Request body:** full JSON example + field table.
- **Response:** full JSON example for the success path + field table.
- **Errors:** table of (HTTP status, internal `code`, when it fires).

---

## 4. Services

For each new file under `app/services/` or `app/workers/`, name it and describe what it does. One file per logical responsibility.

| File | Responsibility |
|---|---|
| `app/services/<feature>_service.py` | Enforce / orchestrate / dispatch. Describe in one line. |
| `app/services/<feature>_validator.py` | Schema validation at create/update. |
| `app/workers/handlers/<feature>_handler.py` | If a worker handler is owned here. |

For non-trivial control flow (transactions, multi-step writes, retry semantics), include pseudocode or a numbered sequence.

---

## 5. Lifecycle flows

End-to-end happy-path walks. One subsection per important flow. Each step names the file/function that handles it.

### 5.1 `<flow name>` — happy path

```
1. Client → POST /v1/<path>             [app/api/v1/endpoints/<feature>.py]
2. Service validates input                [app/services/<feature>_validator.py]
3. CRUD writes row                        [app/crud/<feature>.py]
4. Returns response                       [...]
```

Edge cases that exercise a different path get their own subsections (e.g., `5.2 Concurrent writes — idempotent skip`).

---

## 6. Pydantic schemas

List the request/response models this feature owns. The exact field shapes belong here so frontend can implement against the spec without reading the code.

| Schema | Used by | Fields |
|---|---|---|
| `<Feature>Create` | POST request body | `name: str`, `config: dict`, ... |
| `<Feature>Response` | GET/list response | `<id>: UUID`, `created_at: datetime`, ... |

---

## 7. Configuration

Environment variables this feature adds or reads.

| Env var | Default | Purpose |
|---|---|---|
| `<FEATURE>_<KNOB>` | `<default>` | What this controls. |

If a knob is hardcoded (not env-configurable), say so explicitly — drift checks have caught this kind of mismatch.

---

## 8. Security

### 8.1 Authentication

How callers prove identity (JWT / HMAC / API key / no auth).

### 8.2 Authorization

Who can read, who can write, where the row-level check happens. Defense-in-depth: which SQL `WHERE` clauses reinforce the API-layer check.

### 8.3 Tenant scoping

Every read and every write must scope by `org_id` (or `workspace_id`). Name the column.

### 8.4 Input validation

Cron, UUIDs, enum values, length caps, JSON shape. Where validation lives.

### 8.5 Sensitive data

Encryption at rest, redaction in responses, never-logged fields. Reference the encryption key chain by name.

---

## 9. Failure semantics

Comprehensive failure table: what happens when each layer fails. Network errors, validation errors, conflicts, missing resources, downstream outages.

| Failure | Surface | HTTP | Recovery |
|---|---|---|---|
| Invalid input | API | 400 | Caller retries with valid input |
| Conflict | API | 409 | Caller resolves and retries |
| Backend down | API | 502 | Caller retries with backoff |

If background workers are involved: spell out SQS redelivery behavior, terminal states, idempotency keys.

---

## 10. Open questions

Numbered list of unresolved decisions. Each gets:

1. **Question.** Why it matters.
   - Option A: ...
   - Option B: ...
   - **Tentative pick:** ... (revisit at <date or trigger>).

All questions resolved before implementation begins.

---

## 11. Rollout plan

- **Migration order:** which Alembic revision lands first, dependencies on other workstreams.
- **Feature flag (if any):** env var name and default. When the flag flips on/off.
- **Backfill:** if existing rows need transformation.
- **Rollback:** what reverting looks like; what's irreversible.

---

## 12. Tests

Bullet list of test files + what each covers. Reviewer should be able to read this and predict the test suite's shape.

- `tests/<area>/test_<feature>_<concern>.py` — covers <X, Y, Z>.

---

> Before opening the PR: bump nothing. `last_verified_at` stays `null` on a new spec; it's set to the merge SHA once the feature ships and a maintainer has read the spec end-to-end against the merged code.
