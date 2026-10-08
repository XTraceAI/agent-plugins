// The lane orchestration replayed against the REAL Python lanes
// (vectors/lanes.ts, generated for the run by scripts/test-mod.sh from
// scripts/rule_vector_cases/lanes.py). Each case is one fixture book, one
// checkout and a sequence of hook calls; every call goes through
// `createEngine` with a fake EngineIO answering what the Python was answered
// (the same git argv table, files, clock, judge replies and conversation),
// and the engine must say what the Python said:
//
//   * the model's text: `Verdict.context` plus the disclosure instruction
//     act.ts appends (= Python's additionalContext), and the deny reason;
//   * every ledger row, projected as rulebook_mod_cli.py `log` writes it;
//   * the git argv asked, the judge bodies sent;
//   * the shared state after the call: the per-checkout ordering file and the
//     session's armings, which the fake `state` op writes as
//     scripts/rulebook_mod_state.py would.
//
// The user's line (systemMessage) is the shell's: here each fire's line must
// appear in it, nothing more.

import { test, expect } from 'claude-code/testing'

import { postResultText } from '../../lanes'
import { createEngine } from '../engine'
import type { EngineIO, HookRule, StateOp, TurnMessage, Verdict } from '../index'
import {
  applyStateOp, eventAsWritten, fireAsWritten, isEvent, modelText, sessionDoc as laneSessionDoc, sortedArgv,
} from './lane_io'
import { VECTORS } from './vectors/lanes'
import { belowRatchet } from './vector_checks'

type Dict = Record<string, unknown>
type Step = {
  lane: 'pre' | 'post' | 'prompt' | 'session'
  tool?: string
  input?: Dict
  at?: number
  writes?: Record<string, string>
  messages?: TurnMessage[]
  agent_id?: string
  tool_use_id?: string
  /** A Bash result's stdout (the record's), or another tool's result. */
  result?: string
  /** A Bash result's stderr (the record's). */
  stderr?: string
  /** What the mod's `text` reads when it is not `result` (a persisted output's preview). */
  preview?: string
  is_error?: boolean
  /** The exit status a reporting host sends Python; the mod never sees one. */
  exit_code?: number
  prompt?: string
  session?: string
  cwd?: string
}
type Case = {
  name: string
  rules: Dict[]
  files?: Record<string, string>
  branch?: string
  git?: [string[], number, string][]
  judge?: (Dict | 'timeout')[]
  env?: Record<string, string>
  steps: Step[]
}
type StepOut = {
  stdout: { hookSpecificOutput?: Dict; systemMessage?: string } | null
  fires: Dict[]
  events: Dict[]
  git: string[][]
  judge_sent: Dict[]
  turn_id: string | null
  wt_state: Record<string, unknown>
  armings: Dict
}
type CaseOut = { root: string; rules: HookRule[]; git_table: [string[], number, string][]; steps: StepOut[] }

const T0 = 1_790_000_000_000
const HOME = '/home/vector'
const REPO = 'acme/app'
const SESSION = 'sess-lanes-1'
const BASE = '/base'

const utf8Len = (s: string) => {
  let n = 0
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    n += c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4
  }
  return n
}

function deepSub<T>(v: T, root: string): T {
  if (typeof v === 'string') return v.split('{ROOT}').join(root) as T
  if (Array.isArray(v)) return v.map((x) => deepSub(x, root)) as T
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, deepSub(x, root)])) as T
  return v
}

/** The fake world one case runs in. */
function world(c: Case, o: CaseOut) {
  const files = new Map<string, { text: string; mtimeMs: number }>()
  const put = (p: string, text: string, mtimeMs: number) => files.set(p, { text, mtimeMs })
  put(`${o.root}/.git/HEAD`, `ref: refs/heads/${c.branch ?? 'main'}\n`, T0 - 10_000)
  for (const [rel, text] of Object.entries(c.files ?? {})) put(`${o.root}/${rel}`, text, T0 - 10_000)
  const table = new Map(o.git_table.map(([argv, rc, out]) => [JSON.stringify(argv), [rc, out] as const]))
  const replies = [...(c.judge ?? [])]
  const s = { now: T0, asked: [] as string[][], sent: [] as Dict[], step: undefined as Step | undefined, out: undefined as StepOut | undefined }
  const readJson = (p: string): unknown => {
    const f = files.get(p)
    if (!f) return undefined
    try {
      return JSON.parse(f.text)
    } catch {
      return undefined
    }
  }
  const stateFiles = { base: BASE, readJson, put: (p: string, t: string) => put(p, t, s.now) }
  const sessionDoc = (session: string) => laneSessionDoc(stateFiles, session)
  const applyOp = (op: StateOp) => applyStateOp(stateFiles, op)

  const io: EngineIO = {
    git: async (argv) => {
      s.asked.push([...argv])
      const a = table.get(JSON.stringify(argv))
      return { code: a ? a[0] : 128, stdout: a ? a[1] : '' }
    },
    readText: async (p) => files.get(p)?.text,
    writeText: async (p, t) => {
      put(p, t, s.now)
    },
    stat: async (p) => {
      const f = files.get(p)
      if (f) return { kind: 'file', size: utf8Len(f.text), mtimeMs: f.mtimeMs, realPath: p }
      const pre = p.replace(/\/+$/, '') + '/'
      if ([...files.keys()].some((k) => k.startsWith(pre))) return { kind: 'dir', size: 0, mtimeMs: 0, realPath: p }
      return undefined
    },
    countLines: async (p) => {
      const t = files.get(p)?.text
      if (t === undefined) return undefined
      return t.split('\n').length - 1 + (t && !t.endsWith('\n') ? 1 : 0)
    },
    now: () => s.now,
    sleep: async () => {
      for (let i = 0; i < 100; i++) await Promise.resolve()
    },
    tzOffsetMinutes: () => 0,
    home: HOME,
    env: async () => ({
      recall: c.env?.MEMHUB_RULEBOOK_RECALL,
      judge: c.env?.MEMHUB_RULEBOOK_JUDGE,
      timeoutS: c.env?.MEMHUB_RULEBOOK_TIMEOUT_S,
      baseBranch: c.env?.MEMHUB_RULEBOOK_BASE_BRANCH,
      briefBudget: c.env?.MEMHUB_BRIEF_TOKEN_BUDGET,
    }),
    paths: async (dir) => ({ repo: REPO, root: dir, base: BASE }),
    state: async (ops) => ops.map(applyOp),
    judge: async (body) => {
      s.sent.push(JSON.parse(JSON.stringify(body)) as Dict)
      const r = replies.shift()
      if (r === undefined) return undefined
      if (r === 'timeout') return new Promise(() => undefined)
      if (r.status === 404) return { status: 404 }
      return { status: (r.status as number | undefined) ?? 200, data: r.data }
    },
    turn: async () => (s.step?.messages ? { id: s.out?.turn_id ?? '', messages: s.step.messages } : undefined),
  }
  const state = () => {
    const wt: Record<string, unknown> = {}
    for (const k of [...files.keys()].sort()) {
      const name = k.slice(`${BASE}/state/`.length)
      if (k.startsWith(`${BASE}/state/wt-`) && name.endsWith('.json')) wt[name] = readJson(k)
    }
    return wt
  }
  return { io, s, put, state, sessionDoc }
}

const noTurnId = (b: Dict) => ({ ...b, turn_id: '<turn>' })

/**
 * Cases where the mod deliberately answers differently from the Python
 * (engine.ts, "Port notes"). Each is left out of the replay below and has a
 * test of its own that asserts BOTH sides: what the Python vector recorded, and
 * what the mod does instead.
 */
const MOD_DIVERGES = new Set(['ordering-gate-receipt-without-exit-code'])

/** A post step as `tool.call` resolves it: Bash's record is `{ stdout, stderr }`. */
const toolResult = (step: Step) => ({
  text: step.preview ?? step.result ?? '',
  result: step.tool === 'Bash' ? { stdout: step.result ?? '', stderr: step.stderr ?? '' } : step.result ?? '',
})

/** Runs one recorded step through `engine`, as the hook call it was. */
async function runStep(
  engine: ReturnType<typeof createEngine>,
  w: ReturnType<typeof world>,
  o: CaseOut,
  raw: Step,
  i: number,
): Promise<{ step: Step; got: Verdict; sessionId: string }> {
  const step = deepSub(raw, o.root)
  w.s.now = T0 + (step.at ?? 0)
  for (const [rel, text] of Object.entries(step.writes ?? {})) w.put(`${o.root}/${rel}`, text, w.s.now)
  w.s.asked = []
  w.s.sent = []
  w.s.step = step
  w.s.out = o.steps[i]!
  const sessionId = step.session ?? SESSION
  const cwd = step.cwd ?? o.root
  let got: Verdict
  if (step.lane === 'pre' || step.lane === 'post') {
    got = await engine[step.lane]({
      phase: step.lane,
      tool: step.tool!,
      input: step.input ?? {},
      sessionId,
      cwd,
      ...(step.agent_id ? { agentId: step.agent_id } : {}),
      toolUseId: step.tool_use_id ?? `tu-${i}`,
      // No exit code: the mod's tool result carries `text`, the tool's record
      // and `isError`, and the post lane reads them through postResultText.
      ...(step.lane === 'post' ? { result: { text: postResultText(step.tool!, toolResult(step)), isError: step.is_error } } : {}),
    })
  } else if (step.lane === 'prompt') {
    got = await engine.prompt(step.prompt!, sessionId, cwd)
  } else {
    const r = await engine.session(sessionId, cwd)
    got = { context: r.context, fires: [], ledger: r.ledger ?? [] }
  }
  return { step, got, sessionId }
}

for (const v of VECTORS) {
  const c = v.args[0] as Case
  const o = v.out as CaseOut
  if (MOD_DIVERGES.has(c.name)) continue
  test(`lanes: ${c.name}`, async () => {
    expect(v.raises).toBeUndefined()
    const w = world(c, o)
    const engine = createEngine(w.io)
    engine.setBook(o.rules, { repo: REPO, fetchedAt: 0 })
    for (const [i, raw] of c.steps.entries()) {
      const want = o.steps[i]!
      const { step, got, sessionId } = await runStep(engine, w, o, raw, i)
      const where = `${c.name} step ${i} (${step.lane}${step.tool ? ' ' + step.tool : ''})`
      const hso = (want.stdout?.hookSpecificOutput ?? {}) as Dict
      expect({ where, context: modelText(got) }).toEqual({ where, context: hso.additionalContext as string | undefined })
      expect({ where, deny: got.deny }).toEqual({ where, deny: hso.permissionDecisionReason as string | undefined })
      const sys = want.stdout?.systemMessage ?? ''
      for (const f of got.fires) expect({ where, line: sys.includes(f.line) }).toEqual({ where, line: true })
      const fires = got.ledger.filter((r) => !isEvent(r)).map((r) => fireAsWritten(r, sessionId))
      const events = got.ledger.filter(isEvent).map((r) => eventAsWritten(r, sessionId))
      expect({ where, fires }).toEqual({ where, fires: want.fires })
      expect({ where, events }).toEqual({ where, events: want.events })
      expect({ where, git: sortedArgv(w.s.asked) }).toEqual({ where, git: sortedArgv(want.git) })
      expect({ where, judge: w.s.sent.map(noTurnId) }).toEqual({ where, judge: want.judge_sent.map(noTurnId) })
      expect({ where, judgeTurn: w.s.sent.map((b) => b.turn_id) }).toEqual({ where, judgeTurn: want.judge_sent.map((b) => b.turn_id) })
      expect({ where, wt: w.state() }).toEqual({ where, wt: want.wt_state })
      const doc = w.sessionDoc(sessionId)
      const armings = { armed: doc.armed, armed_version: doc.armed_version, armed_once: doc.armed_once }
      expect({ where, armings }).toEqual({ where, armings: want.armings })
    }
  })
}

test('DIVERGES from Python: a gate-mode receipt with no exit code discharges on the mod', async () => {
  const v = VECTORS.find((x) => (x.args[0] as Case).name === 'ordering-gate-receipt-without-exit-code')!
  const c = v.args[0] as Case
  const o = v.out as CaseOut
  const decision = (s: StepOut) => (s.stdout?.hookSpecificOutput as Dict | undefined)?.permissionDecision
  // The Python, given no exit_code (Claude Code's PostToolUse): no receipt, and the push stays blocked.
  expect(c.steps.some((s) => s.exit_code !== undefined)).toBe(false)
  expect(o.steps.map(decision)).toEqual([undefined, 'deny', undefined, 'deny'])
  expect(o.steps[2]!.events).toEqual([])
  // The mod: the green, non-error Bash result is the receipt, and the push is let through.
  const w = world(c, o)
  const engine = createEngine(w.io)
  engine.setBook(o.rules, { repo: REPO, fetchedAt: 0 })
  const got: Verdict[] = []
  for (const [i, raw] of c.steps.entries()) got.push((await runStep(engine, w, o, raw, i)).got)
  expect(got.map((g) => g.deny === undefined ? undefined : 'deny')).toEqual([undefined, 'deny', undefined, undefined])
  expect(got[2]!.ledger.filter(isEvent).map((r) => [r.kind, r.rule_id])).toEqual([['receipt', 'tests-before-push']])
  // …which is what the Python answers once the host does send exit_code 0.
  const twin = VECTORS.find((x) => (x.args[0] as Case).name === 'ordering-gate-receipt-exit-0')!
  expect((twin.out as CaseOut).steps.map(decision)).toEqual([undefined, 'deny', undefined, undefined])
})

test('a gate-mode receipt is refused on an error result, whatever its text', async () => {
  const v = VECTORS.find((x) => (x.args[0] as Case).name === 'ordering-gate-receipt-without-exit-code')!
  const c = v.args[0] as Case
  const o = v.out as CaseOut
  const steps = c.steps.map((s, i) => (i === 2 ? { ...s, is_error: true } : s))
  const w = world(c, o)
  const engine = createEngine(w.io)
  engine.setBook(o.rules, { repo: REPO, fetchedAt: 0 })
  const got: Verdict[] = []
  for (const [i, raw] of steps.entries()) got.push((await runStep(engine, w, o, raw, i)).got)
  expect(got[3]!.deny).toBeDefined()
  expect(got[2]!.ledger.filter(isEvent)).toEqual([])
})

test('a Bash output persisted to a file fires on its stdout, past the preview the model reads', async () => {
  const v = VECTORS.find((x) => (x.args[0] as Case).name === 'result-beyond-preview')!
  const c = v.args[0] as Case
  const o = v.out as CaseOut
  const step = c.steps[0]!
  // The match is past the preview and inside the record's stdout; the Python fired on it.
  expect(step.preview!.includes('SILENT FAIL')).toBe(false)
  expect(step.result!.includes('SILENT FAIL')).toBe(true)
  expect(o.steps[0]!.fires.map((f) => f.rule_id)).toEqual(['silent-fail'])
  // The mod, through the post lane's composition, fires with it (the replay above checks the whole answer)…
  const w = world(c, o)
  const engine = createEngine(w.io)
  engine.setBook(o.rules, { repo: REPO, fetchedAt: 0 })
  expect((await runStep(engine, w, o, step, 0)).got.fires.map((f) => f.ruleId)).toEqual(['silent-fail'])
  // …where the preview alone (the lane before ENG-1206) does not.
  const w2 = world(c, o)
  const e2 = createEngine(w2.io)
  e2.setBook(o.rules, { repo: REPO, fetchedAt: 0 })
  const previewOnly: Step = { ...step, result: step.preview, preview: undefined }
  expect((await runStep(e2, w2, o, previewOnly, 0)).got.fires).toEqual([])
})

test('lane vectors cover every lane, a gate, a judge hold and an ordering receipt', () => {
  // Recorded count (vector_checks.ts): raise it when cases are added.
  expect(belowRatchet('lanes', VECTORS.length, 25)).toBeNull()
  const steps = VECTORS.flatMap((v) => (v.out as CaseOut).steps)
  const lanes = new Set(VECTORS.flatMap((v) => (v.args[0] as Case).steps.map((s) => s.lane)))
  expect([...lanes].sort()).toEqual(['post', 'pre', 'prompt', 'session'])
  expect(steps.some((s) => (s.stdout?.hookSpecificOutput as Dict | undefined)?.permissionDecision === 'deny')).toBe(true)
  expect(steps.some((s) => s.fires.some((f) => f.mode === 'suppressed' && f.judge_verdict))).toBe(true)
  expect(steps.some((s) => s.events.some((e) => e.kind === 'receipt'))).toBe(true)
})

// ── what the engine refuses to judge: it throws, and the shell hands the lane to Python ──

function bareWorld(root: string) {
  const c: Case = { name: 'bare', rules: [], steps: [] }
  return world(c, { root, rules: [], git_table: [], steps: [] }).io
}

test('a rule pyre cannot run is never skipped: every tool and prompt lane throws, the session lane does not', async () => {
  const root = '/tmp/memhub-lane-vectors/unportable'
  const engine = createEngine(bareWorld(root))
  engine.setBook([{ id: 'odd', on: 'bash', mode: 'gate', rx: '(?<=a+)b', text: 'x', _unportable: ['rx'] }], { repo: REPO, fetchedAt: 0 })
  const call = { phase: 'pre' as const, tool: 'Bash', input: { command: 'ls' }, sessionId: SESSION, cwd: root }
  await expect(engine.pre(call)).rejects.toThrow('not portable')
  await expect(engine.post({ ...call, phase: 'post' })).rejects.toThrow('not portable')
  await expect(engine.prompt('hi', SESSION, root)).rejects.toThrow('not portable')
  await expect(engine.session(SESSION, root)).resolves.toBeDefined()
})

test('a call in a checkout of another repo is not judged against this book', async () => {
  const root = '/tmp/memhub-lane-vectors/foreign'
  const io = bareWorld(root)
  const engine = createEngine({ ...io, paths: async (dir) => ({ repo: 'someone/else', root: dir, base: BASE }) })
  engine.setBook([{ id: 'g', on: 'bash', mode: 'gate', rx: 'push', text: 'x' }], { repo: REPO, fetchedAt: 0 })
  await expect(engine.pre({ phase: 'pre', tool: 'Bash', input: { command: 'git push' }, sessionId: SESSION, cwd: root }))
    .rejects.toThrow('the loaded book is acme/app')
})

// The real engine on each tool's d.ts input shape: it reads the path and the
// body from the keys rulebook_hook.py reads from `tool_input`.
test('the engine reads Edit/Write/Read paths and bodies from the tool arguments, as rulebook_hook.py does', async () => {
  const root = '/tmp/memhub-lane-vectors/inputs'
  const engine = createEngine(bareWorld(root))
  engine.setBook([
    { id: 'use-logging', on: 'edit', mode: 'advise', text: 'Use the logger.', path_rx: String.raw`\.py$`, content_rx: String.raw`\bprint\(` },
    { id: 'no-lock-reads', on: 'read', mode: 'advise', text: 'Do not read lockfiles.', path_rx: String.raw`\.lock$` },
  ], { repo: REPO, fetchedAt: 0 })
  const pre = (tool: string, input: Record<string, unknown>, n: number) =>
    engine.pre({ phase: 'pre', tool, input, sessionId: `${SESSION}-${n}`, cwd: root, toolUseId: `tu-${n}` })
  const fired = (v: Verdict) => v.fires.map((f) => f.ruleId)
  expect(fired(await pre('Edit', { file_path: `${root}/a.py`, old_string: 'x', new_string: 'print(x)' }, 1))).toEqual(['use-logging'])
  expect(fired(await pre('Edit', { file_path: `${root}/a.md`, old_string: 'x', new_string: 'print(x)' }, 2))).toEqual([])
  expect(fired(await pre('Write', { file_path: `${root}/b.py`, content: 'print(1)\n' }, 3))).toEqual(['use-logging'])
  expect(fired(await pre('Read', { file_path: `${root}/c.lock`, limit: 20 }, 4))).toEqual(['no-lock-reads'])
  // NotebookEdit: rulebook_hook.py's edit lane reads `file_path` and
  // `new_string`/`content`, neither of which NotebookEdit has (its keys are
  // `notebook_path` and `new_source`), so the Python matches no content rule
  // on it; the port does the same rather than diverge.
  expect(fired(await pre('NotebookEdit', { notebook_path: `${root}/n.ipynb`, new_source: 'print(2)', cell_id: 'c' }, 5))).toEqual([])
})
