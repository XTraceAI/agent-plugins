// A fake Io and a fake Engine for the shell's tests: everything the shell
// does goes through these, recorded, so a test asserts on effects without an
// engine beneath it.

import type { FireNote, LaneName } from '../../types'
import type { Ctx, Env, HttpReply, Io, RunResult, StateShape } from '../ctx'
import type { CallEvent, Engine, HookRule, Verdict } from '../engine'

export const ROOT = '/plugin'
export const BOOK = '/home/u/.config/memhub-plugin/rulebook/book/repo-abc.json'
export const REPO = 'XTraceAI/agent-plugins-internal'
export const SESSION = 'sess-1'

export type Run = { argv: readonly string[]; stdin?: string }
export type Fetch = { url: string; method?: string; headers: Record<string, string>; body?: string }

export type FakeIo = Io & {
  envVars: Partial<Record<Env, string>>
  envWrites: { env: Env; value: string | undefined }[]
  state: Partial<StateShape>
  stateVersions: Record<string, number>
  statuses: (string | undefined)[]
  logs: string[]
  debugs: string[]
  appends: { type: 'user' | 'system'; text: string }[]
  runs: Run[]
  fetches: Fetch[]
  files: Map<string, string>
  writes: { path: string; text: string }[]
  timers: { ms: number; fn: () => void; cancelled: boolean }[]
  session: string
  /** Answers `process.run`; default: api-info signed in, paths for REPO, log ok. */
  onRun: (argv: readonly string[], stdin?: string) => RunResult | Promise<RunResult>
  onFetch: (f: Fetch) => HttpReply | Promise<HttpReply>
  /** Runs every live timer once, then lets the work it started settle. */
  tick(): Promise<void>
}

export const ok = (stdout: string): RunResult => ({ exitCode: 0, stdout, stderr: '' })
export const reply = (status: number, text = '', headers: Record<string, string> = {}): HttpReply => ({
  status,
  ok: status >= 200 && status < 300,
  headers,
  text,
})

/** Lets every pending promise chain run (a few macrotask-free turns of the microtask queue). */
export async function settle(): Promise<void> {
  for (let i = 0; i < 50; i++) await Promise.resolve()
}

export function defaultRun(argv: readonly string[]): RunResult {
  const sub = argv[2]
  if (sub === 'api-info') return ok(JSON.stringify({ base: 'https://api.example.test', bearer: 'tok' }))
  if (sub === 'paths') return ok(JSON.stringify({ book: BOOK, repo: REPO, ledger: '/l' }))
  if (sub === 'log') return ok('{"ok":true}')
  return ok('')
}

export function fakeIo(over: Partial<Pick<FakeIo, 'onRun' | 'onFetch' | 'pluginName'>> = {}): FakeIo {
  const io: FakeIo = {
    pluginName: over.pluginName ?? 'memhub-staging',
    root: ROOT,
    envVars: {},
    envWrites: [],
    state: {},
    stateVersions: {},
    statuses: [],
    logs: [],
    debugs: [],
    appends: [],
    runs: [],
    fetches: [],
    files: new Map([[`${ROOT}/.claude-plugin/plugin.json`, '{"version":"0.111.0"}']]),
    writes: [],
    timers: [],
    session: SESSION,
    onRun: over.onRun ?? defaultRun,
    // The compatibility endpoint answers as an older backend does (no
    // `mod_lanes`: the mod may serve); every other GET (the book) 304.
    onFetch: over.onFetch ?? (f => (f.url.endsWith('/v1/plugin/compatibility') ? reply(200, '{"code":0,"data":{}}') : reply(304))),
    now: async () => 1_700_000_000_000,
    sessionId: async () => io.session,
    cwd: async () => '/work',
    run: async (argv, init) => {
      io.runs.push({ argv, stdin: init?.stdin })
      return io.onRun(argv, init?.stdin)
    },
    fetch: async (url, init) => {
      const f = { url, method: init?.method, headers: init?.headers ?? {}, body: init?.body }
      io.fetches.push(f)
      return io.onFetch(f)
    },
    readFile: async path => {
      const t = io.files.get(path)
      if (t === undefined) throw new Error(`ENOENT ${path}`)
      return t
    },
    writeFile: async (path, text) => {
      io.files.set(path, text)
      io.writes.push({ path, text })
    },
    getLanesVar: async env => io.envVars[env],
    setLanesVar: async (env, value) => {
      io.envWrites.push({ env, value })
      if (value === undefined) delete io.envVars[env]
      else io.envVars[env] = value
    },
    getState: async key => ({ value: io.state[key] as never, version: io.stateVersions[key] ?? 0 }),
    setState: async (key, value) => {
      ;(io.state as Record<string, unknown>)[key] = JSON.parse(JSON.stringify(value))
      io.stateVersions[key] = (io.stateVersions[key] ?? 0) + 1
    },
    status: text => {
      io.statuses.push(text)
    },
    log: text => {
      io.logs.push(text)
    },
    debug: text => {
      io.debugs.push(text)
    },
    append: async (type, text) => {
      io.appends.push({ type, text })
    },
    every: (ms, fn) => {
      const t = { ms, fn, cancelled: false }
      io.timers.push(t)
      return {
        cancel() {
          t.cancelled = true
        },
      }
    },
    // Never wakes unless a test replaces it: no timeout fires on its own.
    sleep: () => new Promise<void>(() => undefined),
    async tick() {
      for (const t of io.timers) if (!t.cancelled) t.fn()
      await settle()
    },
  }
  return io
}

export function fakeCtx(io: Io, env: Env = 'staging'): Ctx {
  return {
    env,
    root: io.root,
    api: async () => ({ base: 'https://api.example.test', bearer: 'tok' }),
    forgetApi() {},
  }
}

export const EMPTY: Verdict = { context: [], fires: [], ledger: [] }

export type FakeEngine = Engine & {
  calls: { pre: CallEvent[]; post: CallEvent[]; prompt: string[]; session: string[] }
  books: HookRule[][]
  verdicts: { pre?: Verdict | Error; post?: Verdict | Error; prompt?: Verdict | Error }
  sessionContext: string[] | Error
}

export function fakeEngine(): FakeEngine {
  const answer = (v: Verdict | Error | undefined) => (v instanceof Error ? Promise.reject(v) : Promise.resolve(v ?? EMPTY))
  const eng: FakeEngine = {
    calls: { pre: [], post: [], prompt: [], session: [] },
    books: [],
    verdicts: {},
    sessionContext: ['## posture: keep diffs small'],
    setBook(rules) {
      eng.books.push([...rules])
    },
    pre: async e => {
      eng.calls.pre.push(e)
      return answer(eng.verdicts.pre)
    },
    post: async e => {
      eng.calls.post.push(e)
      return answer(eng.verdicts.post)
    },
    prompt: async text => {
      eng.calls.prompt.push(text)
      return answer(eng.verdicts.prompt)
    },
    session: async sessionId => {
      eng.calls.session.push(sessionId)
      if (eng.sessionContext instanceof Error) throw eng.sessionContext
      return { context: eng.sessionContext }
    },
  }
  return eng
}

/** A served row → the flat hook rule, for tests (the real one is engine/rules.ts). */
export const toHook = (row: Record<string, unknown>): HookRule | null =>
  typeof row.id === 'string' ? { id: row.id, on: String(row.on ?? 'bash'), mode: String(row.mode ?? 'advise') } : null

export function fire(ruleId: string, blocked = false) {
  const desc = `rule ${ruleId} in twenty words or fewer`
  return {
    ruleId,
    label: ruleId,
    line: `${blocked ? '⛔️' : '📏'} Rule fired: ${desc}`,
    mode: (blocked ? 'gate' : 'advise') as 'gate' | 'advise',
    text: desc,
  }
}

export const lanesOf = (io: FakeIo): LaneName[] | undefined => io.state.lanes
export const firesOf = (io: FakeIo): FireNote[] => io.state.fires ?? []
