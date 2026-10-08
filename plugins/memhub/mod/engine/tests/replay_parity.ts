// The real-session parity replay's TS side (scripts/replay_parity.py,
// scripts/replay-parity.sh). Not a test file itself: the generator writes one
// `replay/shard_NNN.test.ts` per shard, each calling `replayShard` on its
// generated parts. Those exist only in a scratch copy of the plugin under the
// gitignored output dir: they quote the user's real transcripts.
//
// Each session's steps go through `createEngine` with a fake EngineIO that
// answers from what the Python hook was answered (lane_io.ts holds what the
// lane vectors share): its git argv table, a snapshot of every path it
// touched, the repo names it resolved, the clock, and the prompts so far.
// Where the engine decides differently from the recorded Python step, one
// `REPLAY-PARITY {json}` line goes to stdout, naming the kinds of difference;
// each shard ends with a `REPLAY-PARITY-SUMMARY {json}` line. The wrapper turns
// those into report.md. The test fails only when the harness itself broke
// (a shard with no steps), never on a divergence: this is a report.

import { test, expect } from 'claude-code/testing'

import { postResultText } from '../../lanes'
import { createEngine, ForeignRepoError, UnportableRuleError } from '../engine'
import type { EngineIO, HookRule, TurnMessage, Verdict } from '../index'
import {
  applyStateOp, eventAsWritten, fireAsWritten, isEvent, modelText, sessionDoc, sortedArgv, type Dict,
} from './lane_io'

const BASE = '/replay-parity-base'
const CLIP = 1500

/** One snapshotted path. `t` is the text of a file the Python read or counted
 * (up to the generator's cap); `n`/`nl` its line count when only that was kept
 * (over the cap); `x` why the text is missing although the Python read it for
 * its text (over the cap, unreadable). */
type FsEntry = { k: 'f' | 'd' | 'o'; s: number; m: number; r?: string; t?: string; n?: number; nl?: boolean; x?: string } | null
type PyOut = {
  stdout: { hookSpecificOutput?: Dict; systemMessage?: string; [k: string]: unknown } | null
  error: string | null
  fires: Dict[]
  events: Dict[]
  git: string[]
  judge_sent: number
  wt_state: Record<string, unknown>
  armings: Dict
}
type Step = {
  lane: 'pre' | 'post' | 'prompt' | 'session'
  at: number
  cwd: string
  nmsg: number
  tool?: string
  input?: Dict
  tool_use_id?: string
  /** `record`: the string fields of the tool's record (toolUseResult) that postResultText reads. */
  result?: { text: string; isError: boolean; record?: Dict }
  prompt?: string
  py: PyOut
}
type Session = {
  /** The replay key: session id + transcript file. A resumed or forked session
   * can share its id with another transcript; the Python ran each file in its
   * own base, so each is its own session here too. */
  session: string
  /** The real session id, what both sides were handed as `session_id`. */
  sid: string
  repo: string
  home: string
  names: Record<string, string>
  messages: TurnMessage[]
  fs: Map<string, FsEntry>
  git: Map<string, [number | null, string]>
  /** `rulebook_mod_state.py specs` answers, keyed JSON [root, spec_dir, paths]. */
  specs: Map<string, Dict>
  steps: Step[]
}
type Piece =
  | { book: string; rules: HookRule[] }
  | { session: string; sid: string; repo: string; names: Record<string, string>; home: string }
  | { session: string; msg: TurnMessage }
  | { session: string; fs: Record<string, FsEntry> }
  | { session: string; git: Record<string, [number | null, string]> }
  | { session: string; specs: Record<string, Dict> }
  | { session: string; step: Step }

/** A key Python spelled with `json.dumps`, re-spelled as JSON.stringify spells it. */
const reKey = (pyKey: string) => JSON.stringify(JSON.parse(pyKey) as string[])

/** A stable spelling for comparing JSON values (keys sorted). */
function canon(v: unknown): string {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']'
  if (v && typeof v === 'object') {
    return '{' + Object.keys(v as Dict).sort().map((k) => JSON.stringify(k) + ':' + canon((v as Dict)[k])).join(',') + '}'
  }
  return JSON.stringify(v === undefined ? null : v)
}

/** `v` with every long string cut, and every list past 40 items: a report line, not a dump. */
function clip(v: unknown): unknown {
  if (typeof v === 'string') return v.length <= CLIP ? v : v.slice(0, CLIP) + `… (+${v.length - CLIP} chars)`
  if (Array.isArray(v)) return [...v.slice(0, 40).map(clip), ...(v.length > 40 ? [`… (+${v.length - 40} items)`] : [])]
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v as Dict).map(([k, x]) => [k, clip(x)]))
  return v
}

function assemble(parts: readonly unknown[]): { books: Map<string, HookRule[]>; sessions: Session[] } {
  const books = new Map<string, HookRule[]>()
  const byId = new Map<string, Session>()
  const order: Session[] = []
  const get = (id: string): Session => {
    let s = byId.get(id)
    if (!s) {
      s = { session: id, sid: id, repo: '', home: '', names: {}, messages: [], fs: new Map(), git: new Map(), specs: new Map(), steps: [] }
      byId.set(id, s)
      order.push(s)
    }
    return s
  }
  for (const part of parts) {
    for (const p of part as Piece[]) {
      if ('book' in p) {
        books.set(p.book, p.rules)
        continue
      }
      const s = get(p.session)
      if ('repo' in p) {
        s.sid = p.sid
        s.repo = p.repo
        s.names = p.names
        s.home = p.home
      } else if ('msg' in p) s.messages.push(p.msg)
      else if ('fs' in p) for (const [k, v] of Object.entries(p.fs)) s.fs.set(k, v)
      else if ('git' in p) for (const [k, v] of Object.entries(p.git)) s.git.set(reKey(k), v)
      else if ('specs' in p) for (const [k, v] of Object.entries(p.specs)) s.specs.set(reKey(k), v)
      else if ('step' in p) s.steps.push(p.step)
    }
  }
  return { books, sessions: order }
}

/** The fake world one session replays in. */
function world(s: Session) {
  const mem = new Map<string, { text: string; mtimeMs: number }>()
  const st = {
    now: 0, step: undefined as Step | undefined, asked: [] as string[][], judge: 0, misses: [] as string[],
    /** Misses by what was missing, over the whole session (every one, not only the first 20 per step). */
    byWhat: {} as Record<string, number>,
  }
  const miss = (what: string, kind = what.split(' ')[0]) => {
    st.byWhat[kind] = (st.byWhat[kind] ?? 0) + 1
    if (st.misses.length < 20) st.misses.push(what)
  }
  const inBase = (p: string) => p === BASE || p.startsWith(BASE + '/')
  const readJson = (p: string): unknown => {
    const f = mem.get(p)
    if (!f) return undefined
    try {
      return JSON.parse(f.text)
    } catch {
      return undefined
    }
  }
  const files = { base: BASE, readJson, put: (p: string, t: string) => void mem.set(p, { text: t, mtimeMs: st.now }) }
  const entry = (p: string): FsEntry | undefined => {
    if (s.fs.has(p)) return s.fs.get(p)!
    const pre = p.replace(/\/+$/, '') + '/'
    for (const k of s.fs.keys()) if (k.startsWith(pre)) return { k: 'd', s: 0, m: 0, r: p }
    miss(`fs ${p}`)
    return undefined
  }
  /** The text the engine asked for. When the snapshot has only a line count,
   * the answer is made up (blank lines, the same count), and that is ALWAYS
   * logged as a miss: a rule reading the real content would see blanks here,
   * so a divergence on that step is the harness's, not the port's. */
  const textOf = (p: string, e: NonNullable<FsEntry>): string | undefined => {
    if (e.k !== 'f') return undefined
    if (e.t !== undefined) return e.t
    if (e.n !== undefined) {
      if (e.x) miss(`text ${p}: made up from its line count; the Python read it but it was ${e.x}`, 'text (over cap)')
      else miss(`text ${p}: made up from its line count; the Python only counted its lines, and it is over the snapshot cap`, 'text (counted, over cap)')
      return '\n'.repeat(e.n) + (e.nl ? '' : 'x')
    }
    return undefined
  }
  const io: EngineIO = {
    git: async (argv) => {
      st.asked.push([...argv])
      const a = s.git.get(JSON.stringify(argv))
      if (!a) {
        miss(`git ${JSON.stringify(argv)}`)
        return { code: 128, stdout: '' }
      }
      if (a[0] === null) throw new Error('git timed out (recorded)')
      return { code: a[0], stdout: a[1] }
    },
    readText: async (p) => {
      if (inBase(p)) return mem.get(p)?.text
      const e = entry(p)
      if (!e) return undefined
      const t = textOf(p, e)
      if (t === undefined && e.k === 'f') {
        if (e.x) miss(`text ${p}: the Python read it but it was ${e.x}`, 'text (unreadable)')
        else miss(`text ${p}: the Python never read it`, 'text (python never read)')
      }
      return t
    },
    writeText: async (p, t) => {
      if (inBase(p)) mem.set(p, { text: t, mtimeMs: st.now })
    },
    stat: async (p) => {
      if (inBase(p)) {
        const f = mem.get(p)
        return f ? { kind: 'file', size: f.text.length, mtimeMs: f.mtimeMs, realPath: p } : undefined
      }
      const e = entry(p)
      if (!e) return undefined
      return { kind: e.k === 'f' ? 'file' : e.k === 'd' ? 'dir' : 'other', size: e.s, mtimeMs: e.m, realPath: e.r ?? p }
    },
    countLines: async (p) => {
      const e = entry(p)
      if (!e || e.k !== 'f') return undefined
      if (e.n !== undefined) return e.n + (e.nl ? 0 : 1)
      const t = e.t
      if (t === undefined) return undefined
      return t.split('\n').length - 1 + (t && !t.endsWith('\n') ? 1 : 0)
    },
    now: () => st.now,
    sleep: async () => {
      for (let i = 0; i < 100; i++) await Promise.resolve()
    },
    tzOffsetMinutes: () => 0,
    home: s.home,
    env: async () => ({ judge: '0' }),
    paths: async (dir) => {
      const name = s.names[dir]
      if (name === undefined) {
        miss(`paths ${dir}`)
        return undefined
      }
      return { repo: name, root: dir, base: BASE }
    },
    state: async (ops) => ops.map((op) => {
      if (op.op !== 'specs') return applyStateOp(files, op)
      const got = s.specs.get(JSON.stringify([op.root, op.spec_dir, op.paths]))
      if (got) return got
      miss(`specs ${op.root} ${op.spec_dir} (${op.paths.length} paths)`)
      return { error: 'not recorded' }
    }),
    judge: async () => {
      st.judge += 1
      return undefined
    },
    turn: async () => ({ id: '', messages: s.messages.slice(0, st.step?.nmsg ?? 0) }),
  }
  const wtState = () => {
    const wt: Record<string, unknown> = {}
    for (const k of [...mem.keys()].sort()) {
      const name = k.slice(`${BASE}/state/`.length)
      if (k.startsWith(`${BASE}/state/wt-`) && name.endsWith('.json')) wt[name] = readJson(k)
    }
    return wt
  }
  return { io, st, files, wtState }
}

function callOf(step: Step): string {
  const i = step.input ?? {}
  const v = step.lane === 'prompt' ? step.prompt : (i.command ?? i.file_path ?? i.notebook_path ?? '')
  return String(v ?? '').slice(0, 300)
}

type Rec = {
  kinds: string[]
  detail: Record<string, { py: unknown; ts: unknown }>
  /** The ledger rows only one side wrote, as `<rule id>:<mode>` (`<kind>:<kind>` for an event). */
  rules?: { py_only: string[]; ts_only: string[] }
}

/** Compares one engine answer with the recorded Python step. */
function compare(step: Step, got: Verdict, w: ReturnType<typeof world>, session: string): Rec {
  const r: Rec = { kinds: [], detail: {} }
  const add = (k: string, py: unknown, ts: unknown) => {
    r.kinds.push(k)
    r.detail[k] = { py: clip(py), ts: clip(ts) }
  }
  const hso = (step.py.stdout?.hookSpecificOutput ?? {}) as Dict
  const pyDeny = hso.permissionDecision === 'deny' ? (hso.permissionDecisionReason as string | undefined) ?? '' : undefined
  if ((pyDeny !== undefined) !== (got.deny !== undefined)) add('deny', pyDeny ?? null, got.deny ?? null)
  const pyCtx = hso.additionalContext as string | undefined
  const tsCtx = modelText(got)
  if ((pyCtx ?? null) !== (tsCtx ?? null)) add('context', pyCtx ?? null, tsCtx ?? null)
  else if (pyDeny !== undefined && got.deny !== undefined && pyDeny !== got.deny) add('context', { deny: pyDeny }, { deny: got.deny })
  const sys = step.py.stdout?.systemMessage ?? ''
  const lines = got.fires.map((f) => f.line)
  const sysCount = sys.split('Rule fired: ').length - 1
  if (lines.some((l) => !sys.includes(l)) || sysCount !== lines.length) add('fires', sys || null, lines)
  const fires = got.ledger.filter((x) => !isEvent(x)).map((x) => fireAsWritten(x, session))
  const events = got.ledger.filter(isEvent).map((x) => eventAsWritten(x, session))
  if (canon(fires) !== canon(step.py.fires) || canon(events) !== canon(step.py.events)) {
    add('ledger', { fires: step.py.fires, events: step.py.events }, { fires, events })
    const key = (x: Dict) => `${String(x.rule_id ?? x.kind)}:${String(x.mode ?? x.kind)}`
    const py = new Set([...step.py.fires, ...step.py.events].map(key))
    const ts = new Set([...fires, ...events].map(key))
    r.rules = { py_only: [...py].filter((k) => !ts.has(k)), ts_only: [...ts].filter((k) => !py.has(k)) }
  }
  const pyGit = step.py.git.map(reKey).sort()
  const tsGit = sortedArgv(w.st.asked)
  if (canon(pyGit) !== canon(tsGit)) {
    add('git', pyGit.filter((g) => !tsGit.includes(g)), tsGit.filter((g) => !pyGit.includes(g)))
  }
  const doc = sessionDoc(w.files, session)
  const armings = { armed: doc.armed, armed_version: doc.armed_version, armed_once: doc.armed_once }
  if (canon(w.wtState()) !== canon(step.py.wt_state) || canon(armings) !== canon(step.py.armings)) {
    add('state', { wt: step.py.wt_state, armings: step.py.armings }, { wt: w.wtState(), armings })
  }
  if (step.py.judge_sent || w.st.judge) add('judge', step.py.judge_sent, w.st.judge)
  return r
}

async function runStep(engine: ReturnType<typeof createEngine>, s: Session, step: Step, i: number): Promise<Verdict> {
  if (step.lane === 'pre' || step.lane === 'post') {
    return engine[step.lane]({
      phase: step.lane,
      tool: step.tool!,
      input: step.input ?? {},
      sessionId: s.sid,
      cwd: step.cwd,
      toolUseId: step.tool_use_id || `tu-${i}`,
      // As the mod's post lane builds it (lanes.ts): a Bash result's text from its record.
      ...(step.lane === 'post'
        ? { result: { text: postResultText(step.tool!, { text: step.result?.text ?? '', result: step.result?.record }) ?? '', isError: step.result?.isError } }
        : {}),
    })
  }
  if (step.lane === 'prompt') return engine.prompt(step.prompt ?? '', s.sid, step.cwd)
  const r = await engine.session(s.sid, step.cwd)
  return { context: r.context, fires: [], ledger: r.ledger ?? [] }
}

export function replayShard(name: string, parts: readonly unknown[]): void {
  test(`replay parity: ${name}`, { timeoutMs: 1_800_000 }, async () => {
    const { books, sessions } = assemble(parts)
    const counts: Record<string, number> = {}
    let steps = 0
    let divergent = 0
    // What the Python decided, so a report of zero divergences says over how much.
    const python = { context: 0, deny: 0, fire_rows: 0, event_rows: 0, git_steps: 0 }
    const misses: Record<string, number> = {}
    for (const s of sessions) {
      const w = world(s)
      const engine = createEngine(w.io)
      const book = books.get(s.repo)
      // The generator refuses an empty book; a missing one here is a broken shard, not an empty book.
      expect(book?.length ?? 0).toBeGreaterThan(0)
      engine.setBook(book ?? [], { repo: s.repo, fetchedAt: 0 })
      for (const [i, step] of s.steps.entries()) {
        steps += 1
        const hso = step.py.stdout?.hookSpecificOutput
        if (hso?.additionalContext) python.context += 1
        if (hso?.permissionDecision === 'deny') python.deny += 1
        python.fire_rows += step.py.fires.length
        python.event_rows += step.py.events.length
        if (step.py.git.length) python.git_steps += 1
        w.st.now = step.at
        w.st.step = step
        w.st.asked = []
        w.st.judge = 0
        w.st.misses = []
        let rec: Rec
        try {
          const got = await runStep(engine, s, step, i)
          rec = compare(step, got, w, s.sid)
        } catch (err) {
          const handoff = err instanceof ForeignRepoError || err instanceof UnportableRuleError
          rec = { kinds: [handoff ? 'handoff' : 'throw'], detail: { [handoff ? 'handoff' : 'throw']: { py: clip(step.py.stdout), ts: String(err).slice(0, 500) } } }
        }
        if (step.py.error) {
          rec.kinds.push('pyerror')
          rec.detail.pyerror = { py: step.py.error, ts: null }
        }
        if (w.st.misses.length) {
          rec.kinds.push('miss')
          rec.detail.miss = { py: null, ts: w.st.misses }
        }
        if (!rec.kinds.length) continue
        divergent += 1
        for (const k of rec.kinds) counts[k] = (counts[k] ?? 0) + 1
        console.log('REPLAY-PARITY ' + JSON.stringify({
          shard: name, session: s.session, sid: s.sid, repo: s.repo, step: i, lane: step.lane, tool: step.tool ?? null,
          call: callOf(step), kinds: rec.kinds, rules: rec.rules ?? null, detail: rec.detail,
        }))
      }
      for (const [k, n] of Object.entries(w.st.byWhat)) misses[k] = (misses[k] ?? 0) + n
    }
    console.log('REPLAY-PARITY-SUMMARY ' + JSON.stringify({ shard: name, sessions: sessions.length, steps, divergent, counts, python, misses }))
    expect(steps).toBeGreaterThan(0)
  })
}
