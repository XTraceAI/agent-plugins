// The Rulebook engine (mods spec §4.1–4.8): a port of rulebook_hook.py
// `main()`'s lane orchestration — the pre, post, prompt and session lanes —
// over the layers beside it (rules.ts, shell.ts, ordering.ts, pyre.ts) and
// EngineIO. scripts/rule_vector_cases/lanes.py runs the REAL Python lanes on
// recorded hook sequences; ./tests/lanes.test.ts replays them through this
// engine and fails on any difference. Read main() beside this file: the
// order of every step below is its order.
//
// What the engine keeps, and where:
//   * the session's dedup state (`fired`, `counts`, `raw`, `spec_pending`,
//     the judge's per-turn cache, the Bash pre-call stamps) in memory, one
//     object per session id, seeded ONCE from the Python's
//     `state/<session>.json` (lockless: Python replaces it atomically) so
//     whatever Python served before the claim stays deduped;
//   * the session's ARMINGS (`armed`, `armed_version`, `armed_once`) in that
//     same file, shared with the Python lanes: re-read at each tool call that
//     could need them, written only through scripts/rulebook_mod_state.py,
//     which takes the Python's lock and merges by delta;
//   * the per-checkout ordering obligations in `state/wt-<worktree_key>.json`:
//     a gate reads it lockless; an arm (an edit) and a discharge (a green
//     receipt) go through the same script, under the same lock.
//
// Port notes (each a known, accepted difference from the Python):
//   * `$.session.messages({ as: "api" })` stands in for the transcript file
//     (given.user, the judge's turn): `isMeta` and compaction-summary rows are
//     not visible in that form, and `source_message_id` is always null;
//   * the post lane's result text: for a Bash call it is built from the
//     tool's own record (`stdout`, `stderr`) exactly as `result_text` builds
//     it from `tool_response` (lanes.ts `postResultText`), so a large output
//     Claude Code persisted to a file is matched on the same ~30,000-character
//     `stdout` Python gets, not the ~2 KB preview the model reads. Still
//     different: every non-Bash tool, a Bash error result, and a Bash output
//     with no stdout or stderr read `text` (Python matches the raw
//     `tool_response` dict, or its `json.dumps`, there);
//   * no `show_upgrade` (a book suspended by an upgrade notice is never loaded:
//     book.ts), no `maybe_refresh` / `refresh_if_stale` (book.ts refreshes on a
//     timer), no `.sources` audit file, no breadcrumbs, no legacy conversions;
//   * a call whose checkout belongs to ANOTHER repo than the loaded book
//     throws ForeignRepoError: the lane hands that call to Python (which loads
//     that repo's book) rather than judge it against the wrong rules;
//   * a rule pyre cannot express (`_unportable`) is never skipped: while one
//     is in the book every tool and prompt lane throws UnportableRuleError,
//     the shell hands the call to Python and, on the second, releases the
//     lane for the session (claims.ts) — Python then serves it whole.
//   * a GATE-mode ordering receipt discharges on a Bash result that is not
//     an error. Python's `bash_ok(strict=True)` needs an explicit `exit_code`,
//     and neither Claude Code's PostToolUse payload nor the mod's tool result
//     carries one, so on Claude Code the Python lane never discharges a gate
//     (harness/cases/README.md, "A gate-mode receipt never discharges under
//     Claude Code"). The mod knows `isError` for the call it ran, and Claude
//     Code sets it for a Bash call that exited non-zero, so the mod reads a
//     non-error result as exit 0: a deliberate divergence. The lane vectors
//     pin it: `ordering-gate-receipt-without-exit-code` stays blocked in the
//     Python and is allowed here (tests/lanes.test.ts, MOD_DIVERGES);
//   * any other exception is Python's `except BaseException: rc = 0`: the call
//     gets nothing (no gate, no advice), and this call's state changes are
//     dropped, as an unsaved Python state file drops them.

import type { CallEvent, Engine, EngineEnv, EngineIO, Fire, HookRule, TurnMessage, Verdict } from './index'
import { judgeFires, judgeTimeoutMs, unfire, type JudgeState, type Marks } from './judge'
import { OrderingEngine, armsOn, sessionScoped } from './ordering'
import {
  BRAND, dismissalLines, findEditOverride, findOverride, labelOf, namedRules, resolveDismissals,
  splitNamedOverride, stripOverride,
} from './override'
import { givenOk, Probes, pyCompare, type SpecHit } from './probes'
import { isoMicros, pyExec, pyMatch } from './py'
import { pySearch } from './pyre'
import { Repos, sessionFileName, worktreeKey, type RepoInfo } from './repo'
import {
  bookRank, cpLen, cpSlice, disclosureLine, EDIT_TOOLS, editAddedText, evaluate, isDict, oneLine, pyStr, pyStrip, READ_TOOLS, truthy,
} from './rules'
import {
  anchorHits, bashReads, join, pathInScope, redactSecrets, relpath, shellOnly, stripComments,
} from './shell'

type Dict = Record<string, unknown>

export const MAX_ADVISE = 2
export const MAX_POSTURE = 15
const BASH_EDIT_MAX_FILES = 40
const BASH_EDIT_MAX_BYTES = 512 * 1024
const BASH_EDIT_MAX_STATUS = 4000
const BASH_EDIT_MARKS_KEPT = 8
const FS_READ_MAX = 4 * 1024 * 1024
const MAX_BOOKS_NAMED = 6
const ROSTER_MAX_CHARS = 1000
const SPEC_LIST_MAX = 3
const SPEC_OWNED_MAX = 5

const TREE_REWRITE_RX = String.raw`(?:^|[;&|(]\s*)git\s+(?:-C\s+\S+\s+)?(?:checkout|switch|stash|merge|rebase|pull|reset` +
  String.raw`|cherry-pick|revert|apply|am|restore|worktree)\b`
const HARNESS_PROMPT_RX = String.raw`\s*(?:<(?:command-name|command-message|command-args|local-command-stdout` +
  String.raw`|local-command-stderr|local-command-caveat|system-reminder|task-notification)>` +
  String.raw`|This session is being continued|Caveat: The messages below|Base directory for this skill:` +
  String.raw`|Another Claude session sent a message:)`
const AGENT_MESSAGE_RX = String.raw`\s*Another Claude session sent a message:`
const BASH_RED_RX = String.raw`(^|\n)(FAILED|ERROR)\b|\b\d+ (failed|errors?)\b|\nTraceback \(most recent call last\)` +
  String.raw`|(^|\n)npm ERR!|(^|\n)error(\[E\d+\])?:`

export const ADVISE_FEEDBACK_HINT =
  "_If you go on without following one of these, say why on your next shell command — " +
  "`RULEBOOK_OVERRIDE='[<label>] <why>' <command>` — so the reason is recorded against " +
  'that rule instead of silence._'

export const SESSION_PREAMBLE =
  "These are your team's engineering rules — standing instructions from your teammates, " +
  "carrying the same weight as this repo's CLAUDE.md. Follow them as you would CLAUDE.md: " +
  'they are how this team works, not suggestions to weigh. When one fires, you MUST disclose ' +
  'it to the user on its own line, exactly `📏 Rule fired: <the rule, in 20 words or fewer>`, ' +
  'before anything else in that reply.'

/** A call in a checkout of another repo than the book's: Python serves it. */
export class ForeignRepoError extends Error {
  constructor(repo: string, book: string) {
    super(`call is in ${repo}, the loaded book is ${book}`)
    this.name = 'ForeignRepoError'
  }
}

/** The book holds a rule pyre cannot run: the lane must be Python's. */
export class UnportableRuleError extends Error {
  constructor(ids: readonly string[]) {
    super(`rules not portable to the mod: ${ids.slice(0, 3).join(', ')}`)
    this.name = 'UnportableRuleError'
  }
}

// ── small Python-isms ───────────────────────────────────────────────────────

const has = (o: object, k: string) => Object.prototype.hasOwnProperty.call(o, k)
/** `r.get("status", "active") == "active"` */
const isActive = (r: HookRule) => (has(r, 'status') ? r.status : 'active') === 'active'
/** `d.get(k, dflt)` */
const getOr = (d: Dict, k: string, dflt: unknown): unknown => (has(d, k) ? d[k] ?? null : dflt)
/** `str(v)` of a payload value read with `.get(k, "")`. */
const strOf = (d: Dict, k: string): string => pyStr(getOr(d, k, ''))
const casefold = (s: string) => s.toLowerCase()

/** `int(x)` as read_facts and the counter scope accept it, else null (Python's except). */
function pyIntLoose(v: unknown): number | null {
  if (typeof v === 'boolean') return Number(v)
  if (typeof v === 'number' && Number.isFinite(v)) return Math.trunc(v)
  if (typeof v === 'string' && /^\s*[+-]?\d+(_\d+)*\s*$/.test(v)) return Number(v.replace(/_/g, ''))
  return null
}

/** `json.dumps(v)` for the string lists the state keys are made of. */
function pyDumps(v: unknown): string {
  if (Array.isArray(v)) return '[' + v.map(pyDumps).join(', ') + ']'
  if (typeof v === 'string') {
    return JSON.stringify(v).replace(/[\u0080-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
  }
  return JSON.stringify(v ?? null)
}

/** `scope_ok(rule, repo, gitdir)` */
export function scopeOk(rule: HookRule, repo: string, gitdir: string): boolean {
  const scope = has(rule, 'repo_scope') ? rule.repo_scope : 'any'
  const repos = rule._scope_repos
  if (truthy(repos)) {
    const parts = gitdir ? gitdir.split('/') : []
    const i = parts.indexOf('.git')
    const main = i > 0 ? parts[i - 1]! : ''
    const here = new Set([casefold(repo), casefold(main)].filter(Boolean))
    return (repos as unknown[]).some((s) => here.has(casefold(pyStr(s))))
  }
  if (scope === 'any') return true
  if (typeof scope !== 'string') throw new TypeError("'in <string>' requires string as left operand")
  return repo.includes(scope) || (!!gitdir && gitdir.includes(`/${scope}/`))
}

/** A rule as the shell layer's scope/anchor helpers read it. */
const sc = (r: HookRule) => r as unknown as { _scope_paths?: unknown; _scope_exclude_paths?: unknown; anchors?: unknown }

/** `harness_prompt(text)` */
export const harnessPrompt = (text: string): boolean => pyMatch(HARNESS_PROMPT_RX, text || '') !== null

/** `agent_message(text)`: another agent's message — it fires no `prompt` rule. */
export const agentMessage = (text: string): boolean => pyMatch(AGENT_MESSAGE_RX, text || '') !== null

/** `_why(r)` */
const why = (r: HookRule) => (truthy(r.why) ? `  _(why: ${pyStr(r.why)})_` : '')

/** `_spec_untouched_text(text, hits)` */
function specUntouchedText(text: string, hits: readonly SpecHit[]): [string, string] {
  const shown = hits.slice(0, SPEC_LIST_MAX)
  const more = hits.length - shown.length
  let names = shown.map(([p]) => oneLine(p)).join(', ')
  if (more > 0) names += ` (+${more} more)`
  if (!names) return [text, '']
  const owned = shown.map(([p, paths]) => {
    let head = paths.slice(0, SPEC_OWNED_MAX).map(oneLine).join(', ')
    if (paths.length > SPEC_OWNED_MAX) head += ` (+${paths.length - SPEC_OWNED_MAX} more)`
    return `${oneLine(p)} owns ${head}`
  })
  const detail = owned.length ? `  _(changed owned paths: ${owned.join('; ')})_` : ''
  return [`${text.replace(/[\s]+$/u, '')} Specs not updated: ${names}.`, detail]
}

/**
 * `bash_ok(resp, strict)` over what the mod sees of a Bash result, which never
 * carries an exit code. Python's strict (gate-mode receipt) answer without one
 * is always False; here `isError` stands in for it: Claude Code marks a Bash
 * call that exited non-zero as an error, so a strict receipt is a Bash result
 * that is not one (the port note "a gate-mode receipt discharges" above).
 */
function bashOk(resp: { text: string; isError?: boolean } | null, strict: boolean): boolean {
  if (resp === null) return false
  if (resp.isError) return false
  if (strict) return true
  return !pySearch(BASH_RED_RX, resp.text)
}

const cmpTuple = (a: readonly (number | string)[], b: readonly (number | string)[]): number => {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const x = a[i]!
    const y = b[i]!
    const d = typeof x === 'number' && typeof y === 'number' ? x - y : pyCompare(String(x), String(y))
    if (d) return d
  }
  return a.length - b.length
}

// ── session state ───────────────────────────────────────────────────────────

type SessionState = JudgeState & {
  fired: string[]
  counts: Record<string, number>
  raw: Record<string, number>
  armed: Record<string, unknown>
  armed_once: string[]
  armed_version: Record<string, unknown>
  spec_pending: Record<string, unknown>
  bash_t0: Map<string, number>
}

function freshState(): SessionState {
  return { fired: [], counts: {}, raw: {}, armed: {}, armed_once: [], armed_version: {}, spec_pending: {}, bash_t0: new Map() }
}

/** `load_state`'s defaulting, over a parsed file. */
function stateFrom(doc: unknown): SessionState {
  const st = freshState()
  if (!isDict(doc)) return st
  const list = (v: unknown) => (Array.isArray(v) ? v.map((x) => x as string) : [])
  const dict = (v: unknown) => (isDict(v) ? { ...v } : {})
  st.fired = list(doc.fired)
  st.counts = dict(doc.counts) as Record<string, number>
  st.raw = dict(doc.raw) as Record<string, number>
  st.armed = dict(doc.armed)
  st.armed_once = list(doc.armed_once)
  st.armed_version = dict(doc.armed_version)
  st.spec_pending = dict(doc.spec_pending)
  if (isDict(doc.judge)) st.judge = doc.judge as SessionState['judge']
  if (isDict(doc.bash_t0)) {
    for (const [k, v] of Object.entries(doc.bash_t0)) if (typeof v === 'number') st.bash_t0.set(k, v)
  }
  return st
}

const cloneState = (st: SessionState): SessionState => {
  const c = stateFrom(JSON.parse(JSON.stringify({ ...st, bash_t0: Object.fromEntries(st.bash_t0) })))
  if (!st.judge) delete c.judge
  return c
}

/** An event of one call (main()'s `events` entries). */
type Ev = {
  tool: string
  phase: string
  order_phase: string
  cmd: string
  fp: string
  body: string
  /** What the call ADDED, for edit rules' content_rx; null = read `body`. */
  added?: string | null
  rtext: string
  resp: { text: string; isError: boolean } | null
  via: 'bash' | 'bash-read' | null
  read: Dict | null
}

type Ctx = {
  session: string
  agent_id: string | null
  repo: string
  branch: string
  tool: string
  worktree: string | null
}

// ── the engine ──────────────────────────────────────────────────────────────

export function createEngine(io: EngineIO): Engine {
  return new RulebookEngine(io)
}

class RulebookEngine implements Engine {
  private rules: HookRule[] = []
  private repo = ''
  private loaded = false
  private unportable: string[] = []
  private sessions = new Map<string, SessionState>()
  private repos: Repos

  constructor(private io: EngineIO) {
    this.repos = new Repos(io)
  }

  setBook(rules: readonly HookRule[], meta: { repo: string; fetchedAt: number }): void {
    this.rules = rules.map((r) => ({ ...r }))
    this.repo = meta.repo
    this.loaded = true
    this.unportable = rules.filter((r) => truthy(r._unportable) && r.on !== 'session').map((r) => r.id)
  }

  // ── shared plumbing ──────────────────────────────────────────────────────

  private assertPortable(): void {
    if (this.unportable.length) throw new UnportableRuleError(this.unportable)
  }

  /** A per-call copy: Python loads the book fresh per process, and the fire pass writes on rules. */
  private book(): HookRule[] {
    return this.rules.map((r) => ({ ...r }))
  }

  private async where(cwd: string, tool: string, inp: Dict): Promise<RepoInfo | undefined> {
    const info = await this.repos.ofCall(cwd, tool, inp)
    if (!info.repo) return undefined
    if (info.repo !== this.repo) throw new ForeignRepoError(info.repo, this.repo)
    return info
  }

  private async baseOf(root: string): Promise<string> {
    const p = await this.io.paths(root).catch(() => undefined)
    if (!p?.base) throw new Error('no rulebook base')
    return p.base
  }

  private nowUs(): number {
    return Math.round(this.io.now() * 1000)
  }

  private iso(us: number): string {
    return isoMicros(us, this.io.tzOffsetMinutes(Math.floor(us / 1000)))
  }

  /** The session's state: memory, seeded once from Python's file. */
  private async state(session: string, base: string): Promise<SessionState> {
    let st = this.sessions.get(session)
    if (!st) {
      const text = await this.io.readText(join(base, 'state', sessionFileName(session)))
      let doc: unknown
      try {
        doc = text === undefined ? undefined : JSON.parse(text)
      } catch {
        doc = undefined
      }
      st = stateFrom(doc)
      this.sessions.set(session, st)
    }
    return st
  }

  /** The armings, re-read from the shared file (only when a rule could read them). */
  private async refreshArmings(st: SessionState, session: string, base: string, rules: readonly HookRule[]) {
    if (!rules.some((r) => r.on === 'ordering' && sessionScoped(r))) return
    const text = await this.io.readText(join(base, 'state', sessionFileName(session)))
    let doc: unknown
    try {
      doc = text === undefined ? undefined : JSON.parse(text)
    } catch {
      doc = undefined
    }
    const f = stateFrom(doc)
    st.armed = f.armed
    st.armed_version = f.armed_version
    st.armed_once = f.armed_once
  }

  private turns(): () => Promise<readonly TurnMessage[] | undefined> {
    let got: Promise<readonly TurnMessage[] | undefined> | undefined
    return () => (got ??= this.io.turn().then((t) => t?.messages, () => undefined))
  }

  /** `log_fires` row for rulebook_mod_cli.py log. */
  private fireRow(
    ctx: Ctx, r: HookRule, phase: string, mode: string, excerpt: string,
    o: { raw?: Record<string, unknown>; dedup?: Record<string, string>; override?: Record<string, string>; at?: string;
      judge?: Map<string, { verdict: string; p_fit: number | null }> },
  ): Record<string, unknown> {
    const v = o.judge?.get(r.id)
    return {
      rule_id: r.id,
      rulebook_id: r._rulebook_id ?? null,
      rule_version: r._version ?? null,
      agent_id: ctx.agent_id,
      worktree: ctx.worktree,
      source_message_id: null,
      repo: ctx.repo,
      branch: ctx.branch,
      tool: ctx.tool,
      hook_phase: phase,
      mode,
      dedup_key: o.dedup?.[r.id] ?? null,
      raw_matches_before_fire: o.raw && has(o.raw, r.id) ? o.raw[r.id] ?? null : null,
      fired_at: o.at ?? this.iso(this.nowUs()),
      override_reason: o.override?.[r.id] ?? null,
      excerpt: cpSlice(excerpt, 0, 160),
      ...(v ? { judge_score: v.p_fit, judge_verdict: v.verdict } : {}),
    }
  }

  /** `log_event` row. `worktree` undefined = the call's checkout; null = none (a session-scoped receipt). */
  private eventRow(ctx: Ctx, kind: string, o: { rule_id?: string; reason?: string | null; worktree?: string | null; at?: string }) {
    return {
      kind,
      rule_id: o.rule_id ?? null,
      agent_id: ctx.agent_id,
      worktree: o.worktree !== undefined ? o.worktree : ctx.worktree,
      repo: ctx.repo,
      branch: ctx.branch,
      reason: o.reason ?? null,
      at: o.at ?? this.iso(this.nowUs()),
    }
  }

  // ── ordering ─────────────────────────────────────────────────────────────

  /**
   * OrderingEngine.feed: decided on a lockless read of the worktree file; an
   * outcome that WRITES (an edit arming, a green receipt) is decided again by
   * the Python under its lock (rulebook_mod_state.py), whose answer stands.
   */
  private async feed(
    rule: HookRule, ev: Ev, armed: unknown, root: string, base: string, cwd: string, cache: Map<string, unknown>,
  ): Promise<string | null> {
    const path = join(base, 'state', `wt-${worktreeKey(root) ?? 'None'}.json`)
    if (!cache.has(path)) {
      const text = await this.io.readText(path)
      let doc: unknown = {}
      try {
        doc = text === undefined ? {} : JSON.parse(text)
      } catch {
        doc = {}
      }
      cache.set(path, doc)
    }
    let wrote = false
    const snapshot = JSON.parse(JSON.stringify(cache.get(path) ?? {})) as unknown
    const engine = new OrderingEngine({
      lock: () => true,
      unlock: () => {},
      read: () => snapshot,
      write: () => {
        wrote = true
      },
    })
    const ok = ev.resp !== null ? bashOk(ev.resp, rule.mode === 'gate') : null
    const armedArg = typeof armed === 'string' ? armed : armed === undefined ? null : (armed as string | null)
    const local = engine.feed(rule, { hookPhase: ev.order_phase, tool: ev.tool, cmd: ev.cmd, filePath: ev.fp, ok, armed: armedArg })
    if (!wrote) return local
    delete rule._gate_msg
    delete rule._legacy_fires
    const r = await this.io.state([{
      op: 'feed', root, rule: this.rules.find((x) => x.id === rule.id) ?? rule, hook_phase: ev.order_phase, tool: ev.tool,
      cmd: ev.cmd, file_path: ev.fp, ok, armed: armedArg,
    }], cwd)
    cache.delete(path)
    const got = r?.[0]
    if (!isDict(got) || 'error' in got) return null
    if (typeof got.gate_msg === 'string') rule._gate_msg = got.gate_msg
    if (Array.isArray(got.legacy_fires)) rule._legacy_fires = got.legacy_fires
    return typeof got.outcome === 'string' ? got.outcome : null
  }

  // ── file facts ───────────────────────────────────────────────────────────

  private async realpath(p: string): Promise<string> {
    return (await this.io.stat(p, true))?.realPath ?? p
  }

  /** `_worktrees(root)` */
  private async worktrees(root: string): Promise<string[]> {
    let r: { code: number; stdout: string }
    try {
      r = await this.io.git(['-C', root, 'worktree', 'list', '--porcelain'], root, 3000)
    } catch {
      return [root]
    }
    if (r.code !== 0) return [root]
    const seen = [root]
    const real = new Set([await this.realpath(root)])
    for (const line of r.stdout.split(/\r\n|\r|\n/)) {
      if (!line.startsWith('worktree ')) continue
      const p = line.slice(9)
      const rp = await this.realpath(p)
      if (!real.has(rp)) {
        seen.push(p)
        real.add(rp)
      }
    }
    return seen
  }

  /** `_names_of(path)` */
  private async namesOf(path: string): Promise<string[]> {
    const names = new Set([path, await this.realpath(path)])
    for (const n of [...names]) if (n.startsWith('/private/')) names.add(n.slice('/private'.length))
    return [...names]
  }

  /** `bash_written_files(root, cmd, since)` */
  private async bashWrittenFiles(root: string, cmd: string, since: number): Promise<[string, boolean][]> {
    if (!root || pySearch(TREE_REWRITE_RX, shellOnly(cmd || ''), 'm')) return []
    const roots: string[] = []
    for (const w of await this.worktrees(root)) {
      if (w === root || (await this.namesOf(w)).some((n) => (cmd || '').includes(n))) roots.push(w)
    }
    const out: [string, boolean][] = []
    for (const wt of roots) {
      let r: { code: number; stdout: string }
      try {
        r = await this.io.git(['-C', wt, 'status', '--porcelain=v1', '-z', '--untracked-files=all'], wt, 5000)
      } catch {
        continue
      }
      if (r.code !== 0) continue
      const entries = r.stdout.split('\0')
      if (entries.length > BASH_EDIT_MAX_STATUS) continue
      let skipNext = false
      for (const e of entries) {
        if (skipNext) {
          skipNext = false
          continue
        }
        if (e.length < 4) continue
        const code = e.slice(0, 2)
        const rel = e.slice(3)
        skipNext = code[0] === 'R' || code[0] === 'C'
        if (code.includes('D')) continue
        const isNew = code === '??' || code[0] === 'A'
        const path = join(wt, rel)
        const st = await this.io.stat(path)
        if (!st || st.kind !== 'file' || st.mtimeMs / 1000 < since) continue
        out.push([path, isNew])
        if (out.length > BASH_EDIT_MAX_FILES) return []
      }
    }
    return out
  }

  /** `read_edit_body(path, is_new)` */
  private async readEditBody(path: string, isNew: boolean): Promise<string | null> {
    const st = await this.io.stat(path)
    if (!st || st.size > BASH_EDIT_MAX_BYTES) return null
    const raw = await this.io.readText(path)
    if (raw === undefined || raw.includes('\0')) return null
    if (isNew) return raw
    let r: { code: number; stdout: string }
    try {
      const dir = path.slice(0, Math.max(path.lastIndexOf('/'), 0)) || (path.startsWith('/') ? '/' : '')
      r = await this.io.git(['-C', dir, 'diff', 'HEAD', '--no-color', '--no-ext-diff', '-U0', '--', path], dir || '.', 5000)
    } catch {
      return null
    }
    if (r.code !== 0) return null
    return r.stdout.split('\n').filter((l) => l.startsWith('+') && !l.startsWith('+++')).map((l) => l.slice(1)).join('\n')
  }

  /** `read_facts(path, pulled, offset, limit)` */
  private async readFacts(path: string, o: { pulled?: number | null; offset?: unknown; limit?: unknown }): Promise<Dict | null> {
    const st = await this.io.stat(path)
    if (!st || st.kind !== 'file') return null
    const size = st.size
    let total: number | undefined
    if (size < FS_READ_MAX) {
      const text = await this.io.readText(path)
      if (text !== undefined) {
        total = 0
        for (let i = text.indexOf('\n'); i >= 0; i = text.indexOf('\n', i + 1)) total++
        if (text.length && !text.endsWith('\n')) total++
      }
    }
    if (total === undefined) total = await this.io.countLines(path)
    if (total === undefined) return null
    let n = total
    if (o.offset !== undefined && o.offset !== null) {
      const off = pyIntLoose(o.offset)
      if (off !== null) n = Math.max(total - Math.max(off, 1) + 1, 0)
    }
    for (const cap of [o.limit, o.pulled]) {
      if (cap === undefined || cap === null) continue
      const c = pyIntLoose(cap)
      if (c !== null) n = Math.min(n, Math.max(c, 0))
    }
    const bytes = n >= total ? size : total ? Math.trunc((size * n) / total) : 0
    return { lines: n, bytes }
  }

  // ── pre / post ───────────────────────────────────────────────────────────

  async pre(e: CallEvent): Promise<Verdict> {
    return this.call('pre', e)
  }

  async post(e: CallEvent): Promise<Verdict> {
    return this.call('post', e)
  }

  private async call(mode: 'pre' | 'post', e: CallEvent): Promise<Verdict> {
    this.assertPortable()
    const out: Verdict = { context: [], fires: [], ledger: [] }
    if (!this.loaded) return out
    const inp = (e.input ?? {}) as Dict
    const info = await this.where(e.cwd, e.tool, inp)
    if (!info) return out
    const base = await this.baseOf(info.root)
    const st = await this.state(e.sessionId, base)
    const saved = cloneState(st)
    try {
      return await this.callInner(mode, e, inp, info, base, st, out)
    } catch (err) {
      if (err instanceof ForeignRepoError || err instanceof UnportableRuleError) throw err
      // Python's main(): any exception is a silent exit 0, its state unsaved.
      this.sessions.set(e.sessionId, saved)
      return { context: [], fires: [], ledger: [] }
    }
  }

  private async callInner(
    mode: 'pre' | 'post', e: CallEvent, inp: Dict, info: RepoInfo, base: string, st: SessionState, out: Verdict,
  ): Promise<Verdict> {
    const io = this.io
    const { repo, root, gitdir, branch } = info
    const rules = this.book()
    const env: EngineEnv = await io.env().catch(() => ({}))
    const tool = e.tool
    const session = e.sessionId
    const cwd = e.cwd
    const agentId = e.agentId && pyStrip(e.agentId) ? cpSlice(pyStrip(e.agentId), 0, 64) : null
    const ctx: Ctx = { session, agent_id: agentId, repo, branch, tool, worktree: worktreeKey(root) }
    const ledger = out.ledger
    const cmdText = tool === 'Bash' ? pyStr(inp.command || '') : ''
    let probeRoot = root
    let probeBranch = branch
    const elsewhere = await this.repos.commandRoot(cwd, cmdText)
    if (elsewhere && elsewhere !== root) {
      probeRoot = elsewhere
      probeBranch = (await this.repos.repoInfo(elsewhere, false)).branch
    }
    const turns = this.turns()
    const probes = new Probes(io, probeRoot, probeBranch, cmdText, agentId, turns, cwd, env.baseBranch ?? '')

    await this.refreshArmings(st, session, base, rules)
    const droppedArmings: string[] = []
    const dropArming = (rid: string) => {
      if (rid in st.armed || rid in st.armed_version) droppedArmings.push(rid)
      delete st.armed[rid]
      delete st.armed_version[rid]
    }
    let firedNow: HookRule[] = []

    let cmd = tool === 'Bash' ? strOf(inp, 'command') : ''
    let overrideReason: string | null = null
    if (cmd) {
      const found = findOverride(cmd)
      if (found) {
        overrideReason = cpSlice(redactSecrets(found[0]), 0, 2000)
        cmd = stripOverride(cmd, found)
      }
    }
    let overrideLabel: string | null = null
    if (overrideReason !== null) {
      const rawReason = overrideReason
      ;[overrideLabel, overrideReason] = splitNamedOverride(overrideReason)
      if (overrideLabel !== null && !overrideReason) {
        overrideLabel = null
        overrideReason = null
      } else if (overrideLabel !== null &&
          !rules.some((r) => overrideLabel === pyStr(r.id).toLowerCase() || overrideLabel === labelOf(r))) {
        overrideLabel = null
        overrideReason = rawReason
      }
    }
    const fp = strOf(inp, 'file_path')
    const edits = truthy(inp.edits) && Array.isArray(inp.edits) ? (inp.edits as unknown[]) : []
    const body = strOf(inp, 'new_string') + strOf(inp, 'content') +
      edits.filter(isDict).map((x) => strOf(x, 'new_string')).join('\n')
    const added = editAddedText(inp)
    const editMarkers = new Map<string, string>()
    if (mode === 'pre' && EDIT_TOOLS.includes(tool)) {
      for (const [k, v] of findEditOverride(body)) editMarkers.set(k, cpSlice(redactSecrets(v), 0, 2000))
    }
    const rtext = mode === 'post' ? e.result?.text ?? '' : ''
    const resp = mode === 'post' && tool === 'Bash' ? { text: e.result?.text ?? '', isError: !!e.result?.isError } : null
    const dedupKeys: Record<string, string> = {}

    // The events this call is (main(): synthetic edits FIRST, then the call, then synthetic reads).
    const real: Ev = { tool, phase: mode, order_phase: mode, cmd, fp, body, added, rtext, resp, via: null, read: null }
    const events: Ev[] = []
    if (tool === 'Bash') {
      const marks = st.bash_t0
      const callKey = e.toolUseId ? pyStr(e.toolUseId) : 'last'
      if (mode === 'pre') {
        marks.set(callKey, io.now() / 1000)
        const keys = [...marks.keys()]
        for (const k of keys.slice(0, Math.max(keys.length - BASH_EDIT_MARKS_KEPT, 0))) marks.delete(k)
      } else {
        let t0 = marks.get(callKey)
        marks.delete(callKey)
        if (t0 === undefined && callKey !== 'last') {
          t0 = marks.get('last')
          marks.delete('last')
        }
        const wantsEdits = rules.some((r) => (r.on === 'edit' || r.on === 'ordering') && isActive(r))
        if (t0 !== undefined && wantsEdits) {
          for (const [path, isNew] of await this.bashWrittenFiles(root, cmd, t0)) {
            const text = await this.readEditBody(path, isNew)
            if (text === null) continue
            events.push({ tool: 'Write', phase: 'pre', order_phase: 'post', cmd: '', fp: path, body: text, rtext: '', resp: null, via: 'bash', read: null })
          }
        }
      }
    }
    events.push(real)
    const wantsReads = mode === 'pre' && rules.some((r) => r.on === 'read' && isActive(r))
    if (wantsReads && READ_TOOLS.includes(tool) && fp) {
      real.read = await this.readFacts(fp, { offset: inp.offset ?? null, limit: inp.limit ?? null })
    } else if (wantsReads && tool === 'Bash' && cmd) {
      for (const [path, pulled] of bashReads(cwd, cmd, io.home)) {
        events.push({
          tool: 'Read', phase: 'pre', order_phase: 'pre', cmd, fp: path, body: '', rtext: '', resp: null,
          via: 'bash-read', read: await this.readFacts(path, { pulled }),
        })
      }
    }

    // Conversions: collected now, posted after the fire pass.
    const convertedHits: string[] = []
    if (mode === 'post' && Object.values(st.spec_pending).some((p) => isDict(p) && p.root === probeRoot && p.branch === probeBranch)) {
      const changed = await probes.diffPaths()
      if (changed !== null) {
        for (const rule of rules) {
          const pendingKey = pyDumps([rule.id, probeRoot, probeBranch])
          const pending = st.spec_pending[pendingKey]
          if (!(isDict(pending) && pending.root === probeRoot && pending.branch === probeBranch && truthy(pending.paths))) continue
          const specDir = pyStr(getOr(pending, 'spec_dir', 'docs/specs'))
          let all = true
          for (const path of pending.paths as string[]) {
            if (!changed.includes(path) || !((await probes.activeSpecPaths(specDir)) ?? new Set()).has(path)) {
              all = false
              break
            }
          }
          if (all && scopeOk(rule, repo, gitdir)) {
            convertedHits.push(rule.id)
            delete st.spec_pending[pendingKey]
          }
        }
      }
    }
    if (mode === 'post' && tool === 'Bash' && cmd) {
      const stripped = stripComments(shellOnly(cmd))
      for (const r of rules) {
        const crx = r.converted_rx
        if (truthy(crx) && isActive(r) && scopeOk(r, repo, gitdir)) {
          let hit = false
          try {
            hit = pySearch(pyStr(crx), stripped, 'im')
          } catch {
            hit = false
          }
          if (hit) convertedHits.push(r.id)
        }
      }
    }

    const dismissals = new Map<string, string>()
    if (mode === 'pre' && overrideLabel !== null) dismissals.set(overrideLabel, overrideReason!)

    const marks: Marks = { fired: [...st.fired], counts: { ...st.counts }, spec_pending: { ...st.spec_pending } }

    // Anchor rules (§4.7), matched locally.
    let handle = ''
    let apath = ''
    if (tool === 'Bash' && cmd) handle = cpSlice(redactSecrets(shellOnly(cmd)), 0, 400)
    else if (EDIT_TOOLS.includes(tool) && fp) handle = apath = fp
    if (mode === 'pre' && handle && (env.recall ?? '1') !== '0') {
      let n = 0
      for (const r of rules) {
        if (n >= MAX_ADVISE) break
        if (r.on !== 'anchor' || !isActive(r) || st.fired.includes(r.id) || !scopeOk(r, repo, gitdir) ||
            !pathInScope(sc(r), apath, root) || !anchorHits(sc(r), handle).length) continue
        st.fired.push(r.id)
        dedupKeys[r.id] = r.id
        firedNow.push(r)
        n += 1
      }
    }

    const firedOn = new Map<string, Ev>()
    const wtCache = new Map<string, unknown>()
    for (const ev of events) {
      const { tool: etool, phase: ephase, cmd: ecmd, fp: efp, body: ebody } = ev
      for (let r of rules) {
        if (r.on === 'session' || r.on === 'anchor' || !scopeOk(r, repo, gitdir) || !isActive(r)) continue
        const rid = r.id
        if (firedOn.has(rid)) continue

        if (r.on === 'ordering') {
          if (EDIT_TOOLS.includes(etool) && !pathInScope(sc(r), efp, root)) continue
          if (rid in st.armed && rid in st.armed_version && (st.armed_version[rid] ?? null) !== (r._version ?? null)) dropArming(rid)
          let outcome: string | null
          try {
            outcome = await this.feed(r, ev, st.armed[rid], root, base, cwd, wtCache)
          } catch {
            outcome = null
          }
          if (outcome === 'discharged') {
            dropArming(rid)
            ledger.push(this.eventRow(ctx, 'receipt', { rule_id: rid, worktree: sessionScoped(r) ? null : ctx.worktree }))
            delete r._legacy_fires
          } else if (outcome === 'fired') {
            dedupKeys[rid] = `${rid}@${root}:${branch}`
            firedNow.push(r)
            firedOn.set(rid, ev)
          }
          continue
        }

        if (!pathInScope(sc(r), EDIT_TOOLS.includes(etool) || READ_TOOLS.includes(etool) ? efp : '', root)) continue
        let scope = has(r, 'fire_scope') ? r.fire_scope : 'session'
        if (ephase === 'pre' && r.mode === 'gate' && (ev.via === null || ev.via === 'bash-read') &&
            ((etool === 'Bash' && r.on === 'bash') || (EDIT_TOOLS.includes(etool) && r.on === 'edit') ||
              (READ_TOOLS.includes(etool) && r.on === 'read'))) {
          scope = 'call'
        }
        if (typeof scope !== 'string') throw new TypeError("'NoneType' object has no attribute 'startswith'")
        const key = !scope.startsWith('branch') ? rid : `${rid}:${branch}`
        const matched = evaluate(r, { hookPhase: ephase, tool: etool, cmd: ecmd, filePath: efp, body: ebody, resultText: ev.rtext, added: ev.added ?? null }) &&
          (await givenOk(r, probes, ev.read))
        if (scope !== 'call' && !scope.startsWith('counter') && st.fired.includes(key)) {
          if (matched) st.raw[rid] = (st.raw[rid] ?? 0) + 1
          continue
        }
        if (!matched) continue
        st.raw[rid] = (st.raw[rid] ?? 0) + 1
        if (scope.startsWith('counter')) {
          const i = scope.indexOf(':')
          const threshold = (i >= 0 ? pyIntLoose(scope.slice(i + 1)) : null) ?? 1
          st.counts[rid] = (st.counts[rid] ?? 0) + 1
          if (st.counts[rid] !== threshold) continue
        }
        st.fired.push(key)
        dedupKeys[rid] = key
        const given = truthy(r.given) && isDict(r.given) ? r.given : {}
        const specGiven = truthy(given.repo) && isDict(given.repo) ? given.repo : {}
        if (truthy(specGiven.spec_untouched)) {
          const specDir = pyStr(getOr(specGiven, 'spec_dir', 'docs/specs'))
          const hits = (await probes.untouchedSpecs(specDir)) ?? []
          st.spec_pending[pyDumps([rid, probeRoot, probeBranch])] = {
            root: probeRoot, branch: probeBranch, spec_dir: specDir, paths: hits.map(([p]) => p),
          }
          r = { ...r }
          const [text, detail] = specUntouchedText(pyStr(r.text), hits)
          r.text = text
          r._spec_detail = detail
        }
        firedNow.push(r)
        firedOn.set(rid, ev)
      }
    }

    // One instant for everything this call records.
    const firedUs = this.nowUs()
    const firedAt = this.iso(firedUs)
    const firedIds = new Set(firedNow.map((r) => r.id))
    for (const rid of convertedHits) {
      ledger.push(this.eventRow(ctx, 'converted', { rule_id: rid, at: firedIds.has(rid) ? this.iso(firedUs - 1) : firedAt }))
    }

    const excerptOf = (r: HookRule): string => {
      const ev = firedOn.get(r.id)
      if (ev?.via === 'bash') return `bash-edit ${ev.fp}`
      if (ev?.via === 'bash-read') return `bash-read ${ev.fp}`
      return cmd || fp || ''
    }

    // The judge.
    const [judged, held, fresh] = await judgeFires(io, st, {
      repo, session, tool, cmd, fp, root, firedNow, firedOn,
      enabled: (env.judge ?? '1') !== '0', timeoutMs: judgeTimeoutMs(env.timeoutS), base,
    })
    for (const r of firedNow.filter((r) => held.has(r.id))) {
      if (fresh.has(r.id)) {
        ledger.push(this.fireRow(ctx, r, mode, 'suppressed', excerptOf(r), {
          raw: { [r.id]: st.raw[r.id] ?? null }, dedup: dedupKeys, at: firedAt, judge: judged,
        }))
      }
      unfire(st, r, dedupKeys[r.id], marks, fresh.has(r.id))
    }
    firedNow = firedNow.filter((r) => !held.has(r.id))

    const dismiss = (d: ReadonlyMap<string, string>): [[string, string][], [string, number][]] => {
      const [resolved, ambiguous] = d.size ? resolveDismissals(rules, d) : [[], []]
      const recorded: [string, string][] = []
      for (const [r, w] of resolved) {
        ledger.push(this.eventRow(ctx, 'dismissed', { rule_id: r.id, reason: w }))
        recorded.push([pyStr(r._label || r.id), w])
      }
      return [recorded, ambiguous]
    }

    const finish = async () => {
      if (droppedArmings.length) {
        await io.state([{ op: 'drop', session, rule_ids: [...new Set(droppedArmings)] }], cwd).catch(() => undefined)
      }
    }

    if (!firedNow.length) {
      const [setAside, ambiguous] = dismiss(dismissals)
      await finish()
      if (setAside.length || ambiguous.length) {
        const [agentLines] = dismissalLines(setAside, ambiguous)
        out.context.push(agentLines.join('\n'))
      }
      return out
    }

    const fireOf = (r: HookRule) => firedOn.get(r.id)
    const gateable = (r: HookRule): boolean => {
      if (mode !== 'pre' || r.mode !== 'gate') return false
      const via = fireOf(r)?.via ?? null
      if (via === 'bash') return false
      if (tool === 'Bash') return r.on === 'bash' || r.on === 'ordering' || (r.on === 'read' && via === 'bash-read')
      if (READ_TOOLS.includes(tool)) return r.on === 'read'
      return EDIT_TOOLS.includes(tool) && r.on === 'edit'
    }
    const gateIds = new Set(firedNow.filter(gateable).map((r) => r.id))

    let overridden: Record<string, string> = {}
    const gatesHere = firedNow.filter((r) => gateIds.has(r.id))
    const labelCount = new Map<string, number>()
    for (const r of gatesHere) labelCount.set(labelOf(r), (labelCount.get(labelOf(r)) ?? 0) + 1)
    let ambiguousGate: [string, number] | null = null
    const namedGates = (label: string) => namedRules(label, rules, gatesHere)
    if (overrideReason !== null && overrideLabel === null) {
      overridden = Object.fromEntries(gatesHere.map((r) => [r.id, overrideReason!]))
    } else if (overrideReason !== null) {
      const named = namedGates(overrideLabel!)
      if (named.length === 1) {
        overridden[named[0]!.id] = overrideReason
        dismissals.delete(overrideLabel!)
      } else if (named.length) {
        ambiguousGate = [overrideLabel!, named.length]
        dismissals.delete(overrideLabel!)
      }
    } else if (editMarkers.size) {
      for (const [label, w] of editMarkers) {
        const named = label ? namedGates(label) : []
        if (named.length === 1) overridden[named[0]!.id] = w
        else if (named.length) ambiguousGate = [label, named.length]
      }
    }
    const gates = firedNow.filter((r) => gateIds.has(r.id))
    const advisories = firedNow
      .filter((r) => !gateIds.has(r.id))
      .map((r, i) => ({ r, i, k: [r.on === 'anchor' ? 0 : 1, ...bookRank(r)] as number[] }))
      .sort((a, b) => cmpTuple(a.k, b.k) || a.i - b.i)
      .map((x) => x.r)
    const shown = [...gates, ...advisories.slice(0, MAX_ADVISE)]
    const cut = advisories.slice(MAX_ADVISE)
    if (overrideLabel !== null && namedRules(overrideLabel, rules, firedNow).length) dismissals.delete(overrideLabel)
    const [setAside, ambiguous] = dismiss(dismissals)
    const sameCall: Record<string, string> = {}
    const gateTookIt = overrideLabel !== null ? namedRules(overrideLabel, rules, gates).some((r) => r.id in overridden) : false
    if (overrideLabel !== null && !gateTookIt) {
      const here = namedRules(overrideLabel, rules, shown.filter((r) => !gateIds.has(r.id)))
      if (here.length === 1) {
        sameCall[here[0]!.id] = overrideReason!
        const ack: [string, string] = [pyStr(here[0]!._label || here[0]!.id), overrideReason!]
        if (!setAside.some(([a, b]) => a === ack[0] && b === ack[1])) setAside.push(ack)
      } else if (here.length) {
        ambiguous.push([overrideLabel, here.length])
      }
    }
    const blocked = gates.some((r) => !(r.id in overridden))
    const lines = [blocked ? `## ${BRAND} Rulebook — BLOCKED` : `## ${BRAND} Rulebook (team rules — advisory, not blocking)`]
    const denyLines: string[] = []
    if (setAside.length || ambiguous.length) lines.push(...dismissalLines(setAside, ambiguous)[0])

    const whereOf = (r: HookRule): string => {
      const ev = firedOn.get(r.id)
      if (!ev || (ev.via !== 'bash' && ev.via !== 'bash-read')) return ''
      let path = ev.fp
      if (root && path.startsWith(root.replace(/\/+$/, '') + '/')) path = relpath(path, root)
      if (ev.via === 'bash-read') {
        const n = (ev.read ?? {}).lines
        return n !== undefined && n !== null ? ` _(\`${path}\`, ${pyStr(n)} lines, read by that command)_` : ` _(\`${path}\`, read by that command)_`
      }
      return ` _(in \`${path}\`, written by that command)_`
    }

    for (const r of shown) {
      const label = pyStr(r._label || r.id)
      const detail = truthy(r._gate_msg) ? ` — ${pyStr(r._gate_msg)}` : ''
      const staleKey = `_degraded:${r.id}`
      let note = ''
      if (truthy(r._degraded) && !st.fired.includes(staleKey)) {
        st.fired.push(staleKey)
        note = `  _(advice only — ${pyStr(r._degraded)}. Update the ${BRAND} plugin to let this rule gate.)_`
      }
      const blockedHere = gateIds.has(r.id) && !(r.id in overridden)
      const specDetail = pyStr(r._spec_detail ?? '')
      const text = pyStr(r.text)
      if (!gateIds.has(r.id)) {
        lines.push(`- **[${label}]** ${text}${detail}${whereOf(r)}${why(r)}${specDetail}`)
      } else if (r.id in overridden) {
        lines.push(`- **[${label}]** ${text}${detail}${whereOf(r)}${why(r)}${specDetail} _(gate overridden: ${overridden[r.id]})_`)
      } else {
        lines.push(`- **BLOCKED [${label}]** ${text}${detail}${whereOf(r)}${why(r)}${specDetail}`)
        const ident = (labelCount.get(labelOf(r)) ?? 0) > 1 ? ` (rule id ${r.id})` : ''
        denyLines.push(`[${label}]${ident} ${text}${detail}${whereOf(r)}`)
      }
      if (note) lines.push(note)
      const line = disclosureLine(r, blockedHere)
      const f: Fire = { ruleId: r.id, label, line, mode: gateIds.has(r.id) ? 'gate' : 'advise', text }
      out.fires.push(f)
    }
    if (shown.some((r) => !gateIds.has(r.id))) lines.push(ADVISE_FEEDBACK_HINT)
    if (blocked) {
      const still = gates.filter((r) => !(r.id in overridden))
      let how: string
      if (tool === 'Bash') {
        how = "re-run the same command prefixed RULEBOOK_OVERRIDE='<why>' — that allows exactly that call and records why"
      } else if (READ_TOOLS.includes(tool)) {
        how = 'read only the part you need (`offset`/`limit`), or hand the question to a ' +
          'subagent so its answer, not the file, enters this context; if the whole ' +
          "file must be read here, run RULEBOOK_OVERRIDE='<why>' cat <path> in Bash — " +
          'that allows exactly that read and records why'
      } else {
        const named = still.map((r) => `\`rulebook-override[${pyStr(r._label || r.id)}]: <why>\``).join(', ')
        how = `add a comment naming the rule you are excusing (${named}) — each allows ` +
          'that one rule, records why, and stays in the diff for the next reader'
        if (editMarkers.has('')) {
          how += '. A `rulebook-override:` with no rule in brackets excuses nothing — ' +
            'it would mean something different as soon as a second edit gate ' +
            'covers this line'
        }
      }
      if (ambiguousGate) {
        how = `\`[${ambiguousGate[0]}]\` fits ${ambiguousGate[1]} of this call's gates, so it ` +
          'excused none — name the one you mean by its rule id, ' +
          `\`[<rule id>] <why>\`, in the same override form; ${how}`
      }
      out.deny = `Blocked by the ${BRAND} team rulebook:\n` + denyLines.map((l) => `- ${l}`).join('\n') +
        `\nIf this is a legitimate exception, ${how}.`
      lines.push(`_This call was blocked. If it is a legitimate exception, ${how}._`)
    }
    out.context.push(lines.join('\n'))

    const raw: Record<string, unknown> = Object.fromEntries(firedNow.map((r) => [r.id, st.raw[r.id] ?? null]))
    for (const r of shown.filter((r) => !gateIds.has(r.id))) {
      ledger.push(this.fireRow(ctx, r, mode, 'advise', excerptOf(r), { raw, dedup: dedupKeys, at: firedAt, judge: judged }))
    }
    for (const r of gates) {
      ledger.push(this.fireRow(ctx, r, mode, 'gate', cmd || fp || '', {
        raw, dedup: dedupKeys, override: overridden, at: firedAt, judge: judged,
      }))
    }
    for (const r of cut) {
      ledger.push(this.fireRow(ctx, r, mode, 'suppressed', excerptOf(r), { raw, dedup: dedupKeys, at: firedAt, judge: judged }))
    }
    for (const r of shown) {
      st.raw[r.id] = 0
      if (r.id in sameCall) ledger.push(this.eventRow(ctx, 'dismissed', { rule_id: r.id, reason: sameCall[r.id]!, at: firedAt }))
    }
    await finish()
    return out
  }

  // ── arming (prompt / session) ────────────────────────────────────────────

  /** `arm_obligations(rules, repo, gitdir, session, event, prompt)`, written through the Python's lock. */
  private async arm(
    rules: readonly HookRule[], info: RepoInfo, session: string, event: 'prompt' | 'session', prompt: string, cwd: string, st: SessionState,
  ): Promise<string[]> {
    const arming = rules.filter((r) => isActive(r) && scopeOk(r, info.repo, info.gitdir) && armsOn(r, event, prompt))
    if (!arming.length) return []
    const r = await this.io.state([{
      op: 'arm', session, event, prompt, rules: arming.map((x) => this.rules.find((y) => y.id === x.id) ?? x),
      repo: info.repo, gitdir: info.gitdir,
    }], cwd)
    const got = r?.[0]
    if (!isDict(got) || !Array.isArray(got.armed)) return []
    const armed = got.armed as string[]
    for (const rid of armed) {
      if (event === 'session' && !st.armed_once.includes(`session:${rid}`)) st.armed_once.push(`session:${rid}`)
      if (!(rid in st.armed)) st.armed[rid] = event
      st.armed_version[rid] = arming.find((x) => x.id === rid)?._version ?? null
    }
    return armed
  }

  // ── prompt ───────────────────────────────────────────────────────────────

  async prompt(text: string, sessionId: string, cwd: string): Promise<Verdict> {
    this.assertPortable()
    const out: Verdict = { context: [], fires: [], ledger: [] }
    if (!this.loaded || !text) return out
    const info = await this.where(cwd, '', {})
    if (!info) return out
    const base = await this.baseOf(info.root)
    const st = await this.state(sessionId, base)
    const saved = cloneState(st)
    try {
      const rules = this.book()
      if (!harnessPrompt(text)) await this.arm(rules, info, sessionId, 'prompt', text, cwd, st)
      if (!agentMessage(text)) await this.promptLane(rules, info, sessionId, cwd, text, st, out)
      return out
    } catch {
      this.sessions.set(sessionId, saved)
      return { context: [], fires: [], ledger: [] }
    }
  }

  /** `prompt_lane(...)`: the `prompt` matcher rules, advise only. */
  private async promptLane(rules: HookRule[], info: RepoInfo, session: string, cwd: string, text: string, st: SessionState, out: Verdict) {
    const { repo, root, gitdir, branch } = info
    const live = rules.filter((r) => r.on === 'prompt' && isActive(r) && scopeOk(r, repo, gitdir) && pathInScope(sc(r), '', root))
    if (!live.length) return
    const env: EngineEnv = await this.io.env().catch(() => ({}))
    const ctx: Ctx = { session, agent_id: null, repo, branch, tool: 'UserPromptSubmit', worktree: worktreeKey(root) }
    const probes = new Probes(this.io, root, branch, '', null, this.turns(), cwd, env.baseBranch ?? '')
    const fired: HookRule[] = []
    const dedupKeys: Record<string, string> = {}
    for (const r of live) {
      const rid = r.id
      const scope = has(r, 'fire_scope') ? r.fire_scope : 'session'
      if (typeof scope !== 'string') throw new TypeError("'NoneType' object has no attribute 'startswith'")
      const key = !scope.startsWith('branch') ? rid : `${rid}:${branch}`
      const matched = evaluate(r, { hookPhase: 'prompt', tool: 'UserPromptSubmit', prompt: text }) && (await givenOk(r, probes))
      if (scope !== 'call' && st.fired.includes(key)) {
        if (matched) st.raw[rid] = (st.raw[rid] ?? 0) + 1
        continue
      }
      if (!matched) continue
      st.raw[rid] = (st.raw[rid] ?? 0) + 1
      if (scope !== 'call') st.fired.push(key)
      dedupKeys[rid] = key
      fired.push(r)
    }
    if (!fired.length) return
    const sorted = fired.map((r, i) => ({ r, i })).sort((a, b) => cmpTuple(bookRank(a.r), bookRank(b.r)) || a.i - b.i).map((x) => x.r)
    const shown = sorted.slice(0, MAX_ADVISE)
    const cut = sorted.slice(MAX_ADVISE)
    const lines = [`## ${BRAND} Rulebook (team rules — advisory, not blocking)`]
    for (const r of shown) {
      const label = pyStr(r._label || r.id)
      lines.push(`- **[${label}]** ${pyStr(r.text)}${why(r)}`)
      const staleKey = `_degraded:${r.id}`
      if (truthy(r._degraded) && !st.fired.includes(staleKey)) {
        st.fired.push(staleKey)
        lines.push(`  _(advice only — ${pyStr(r._degraded)}. Update the ${BRAND} plugin to let this rule gate.)_`)
      }
      out.fires.push({ ruleId: r.id, label, line: disclosureLine(r), mode: 'advise', text: pyStr(r.text) })
    }
    lines.push(ADVISE_FEEDBACK_HINT)
    out.context.push(lines.join('\n'))
    const firedAt = this.iso(this.nowUs())
    const raw: Record<string, unknown> = Object.fromEntries(sorted.map((r) => [r.id, st.raw[r.id] ?? null]))
    for (const r of shown) {
      const m = pyExec(pyStr(r.rx), text, 'im')
      out.ledger.push(this.fireRow(ctx, r, 'prompt', 'advise', m ? m[0] : '', { raw, dedup: dedupKeys, at: firedAt }))
    }
    for (const r of cut) out.ledger.push(this.fireRow(ctx, r, 'prompt', 'suppressed', '', { raw, dedup: dedupKeys, at: firedAt }))
    for (const r of shown) st.raw[r.id] = 0
  }

  // ── session ──────────────────────────────────────────────────────────────

  async session(sessionId: string, cwd: string, opts: { servedAlready?: boolean } = {}): Promise<{ context: string[]; ledger?: Record<string, unknown>[] }> {
    if (!this.loaded) return { context: [] }
    const info = await this.where(cwd, '', {})
    if (!info) return { context: [] }
    const base = await this.baseOf(info.root)
    const st = await this.state(sessionId, base)
    const rules = this.book()
    const ledger: Record<string, unknown>[] = []
    const context: string[] = []
    if (!opts.servedAlready) {
      const env: EngineEnv = await this.io.env().catch(() => ({}))
      const text = this.digest(rules, info, sessionId, env, ledger)
      if (text) context.push(text)
    }
    await this.arm(rules, info, sessionId, 'session', '', cwd, st)
    return { context, ledger }
  }

  /** `session_digest(rules, repo, gitdir, ctx)`: the posture preamble, and its fire rows. */
  private digest(rules: HookRule[], info: RepoInfo, session: string, env: EngineEnv, ledger: Record<string, unknown>[]): string {
    const { repo, gitdir, branch, root } = info
    const inScope = rules.filter((r) => scopeOk(r, repo, gitdir) && isActive(r))
    if (!inScope.length) return ''
    const ctx: Ctx = { session, agent_id: null, repo, branch, tool: '', worktree: worktreeKey(root) }
    const budget = postureBudgetChars(env.briefBudget)
    const postureAll = postureOrder(inScope.filter((r) => r.on === 'session'))
    const posture: HookRule[] = []
    const cut: HookRule[] = []
    let used = 0
    for (const r of postureAll) {
      const cost = cpLen(pyStr(r.text || '')) + cpLen(pyStr(r.why || ''))
      if (posture.length < MAX_POSTURE && used + cost <= budget) {
        posture.push(r)
        used += cost
      } else cut.push(r)
    }
    const active = inScope.filter((r) => r.on !== 'session')
    const stale = inScope.filter((r) => truthy(r._degraded))
    const lines = ['## 📏 Rulebook (team rules — advisory)', SESSION_PREAMBLE]
    for (const r of posture) lines.push(`- ${pyStr(r.text)}${why(r)}`)
    if (active.length) {
      lines.push(`- ${active.length} rule${active.length !== 1 ? 's' : ''} armed for ` +
        'this repo — they fire inline as you work (proactive on tool ' +
        'calls, reactive on errors). Treat a fire as a teammate\'s note, ' +
        'not boilerplate.')
    }
    if (stale.length) {
      const n = stale.length
      const names = stale.map((r) => pyStr(r._label || r.id)).sort(pyCompare).slice(0, 5).join(', ')
      lines.push(`- ${n} rule${n !== 1 ? 's' : ''} in your book ` +
        `need${n !== 1 ? '' : 's'} a newer ${BRAND} plugin than ` +
        `this one (${names}) — ${n !== 1 ? 'they run' : 'it runs'} ` +
        'as advice and cannot gate. Update the plugin to get ' +
        `${n !== 1 ? 'them' : 'it'} back.`)
    }
    const roster = booksLine([...posture, ...active])
    if (roster) lines.push(roster)
    for (const r of posture) ledger.push(this.fireRow(ctx, r, 'session', 'advise', '', {}))
    for (const r of cut) {
      ledger.push(this.fireRow(ctx, r, 'session', 'suppressed', '', {
        dedup: Object.fromEntries(cut.map((x) => [x.id, `${x.id}@session`])),
        raw: Object.fromEntries(cut.map((x) => [x.id, 0])),
      }))
    }
    return lines.join('\n')
  }
}

/** brief_budget.rulebook_chars() */
export function postureBudgetChars(raw: string | undefined): number {
  const t = pyStrip(raw ?? '')
  let n = 2500
  if (t) {
    const v = pyIntLoose(t)
    n = v === null ? 2500 : v
  }
  const total = Math.max(200, n) * 4
  return total - Math.floor((total * 2) / 5)
}

/** `posture_order(rules)`: the books take turns — each book's first session
 *  rule, then each book's second — wider book first in every round
 *  (`book_rank`), title then id inside a book. */
export function postureOrder(rules: readonly HookRule[]): HookRule[] {
  const books = new Map<string, HookRule[]>()
  for (const r of rules) {
    const k = truthy(r._rulebook_id) ? pyStr(r._rulebook_id) : ''
    if (!books.has(k)) books.set(k, [])
    books.get(k)!.push(r)
  }
  const keyOf = (r: HookRule): string[] => [casefold(pyStr(r._label || r.title || r.id)), pyStr(r.id)]
  for (const rs of books.values()) {
    const keyed = rs.map((r, i) => ({ r, i, k: keyOf(r) }))
    keyed.sort((a, b) => cmpTuple(a.k, b.k) || a.i - b.i)
    rs.splice(0, rs.length, ...keyed.map((x) => x.r))
  }
  const order = [...books.entries()].sort((a, b) =>
    cmpTuple([...bookRank(a[1][0])], [...bookRank(b[1][0])]) || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
  const out: HookRule[] = []
  const rounds = Math.max(0, ...order.map(([, rs]) => rs.length))
  for (let i = 0; i < rounds; i++) for (const [, rs] of order) if (i < rs.length) out.push(rs[i])
  return out
}

/** `books_line(carried)` */
export function booksLine(carried: readonly HookRule[]): string | null {
  type B = { name: unknown; n: number; rank: [number, number]; kind: unknown; scope: unknown; members: unknown }
  const books = new Map<string, B>()
  for (const r of carried) {
    const rid = r._rulebook_id
    if (!truthy(rid)) continue
    const k = pyStr(rid)
    if (!books.has(k)) {
      books.set(k, {
        name: truthy(r._book_label) ? r._book_label : (r._book_name ?? null), n: 0, rank: bookRank(r),
        kind: r._book_kind ?? null, scope: r._book_scope ?? null, members: r._book_members ?? null,
      })
    }
    books.get(k)!.n += 1
  }
  if (books.size < 2) return null
  const order = [...books.keys()]
    .map((id, i) => ({ id, i, b: books.get(id)! }))
    .sort((a, b) => cmpTuple([...a.b.rank, casefold(pyStr(a.b.name || ''))], [...b.b.rank, casefold(pyStr(b.b.name || ''))]) || a.i - b.i)
  const parts = order.map(({ b }) => {
    let who: string | null
    // a scope book's label already says who it reaches; see books_line
    if (b.kind === 'org' || b.kind === 'personal') who = null
    else if (b.scope === 'all_org') who = 'org-wide'
    else if (typeof b.members === 'number' && Number.isInteger(b.members)) who = `${b.members} member${b.members !== 1 ? 's' : ''}`
    else who = null
    const bits = [who, `${b.n} rule${b.n !== 1 ? 's' : ''}`].filter((x): x is string => !!x).join(', ')
    return `${truthy(b.name) ? pyStr(b.name) : 'unnamed rulebook'} (${bits})`
  })
  const extra = parts.length - MAX_BOOKS_NAMED
  const shown = parts.slice(0, MAX_BOOKS_NAMED)
  if (extra > 0) shown.push(`and ${extra} more`)
  return cpSlice('- _From ' + shown.join(' · '), 0, ROSTER_MAX_CHARS) + '._'
}
