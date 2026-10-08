// The plugin's one hooks module (spec §3.1, D1): a composition root that
// registers each part in a fixed order. Hooks of one module run in
// registration order, so:
//
//   shell (session.start: claims → book → remote switch → session lane) → rulebook lanes
//   (tool.call pre+post, prompt.submit) → turn.complete (ledger) → companion
//
// The companion registers LAST, so its tool.call hook is innermost and sees
// the rulebook's answer.
//
// The mod never builds on `classic.*`, `prompt.section`, `prompt.context`,
// `prompt.compose`, `skill.prompt` or `attribution.text` (spec §1.4, D3): the
// built-in guard skips user mods on those for most Team/Enterprise sign-ins.

import type { EngineInterface, On } from 'claude-code'

import type { FireNote, LaneName } from '../types'

// The companion registers last (innermost) with the mod's ctx: it reads fires
// from `$.state` and polls proposals over `$.http.fetch` (`register(on, ctx)`).
import { register as registerCompanion } from '../companion/register'
import { Act } from './act'
import { Book, RemoteSwitch } from './book'
import { Claims, StatusLine } from './claims'
import { type Ctx, type Env, envOf, type Io, lastJson, makeCtx, MOD_CLI, rootOf } from './ctx'
import type { Engine, EngineEnv, EngineIO, FileStat, HookRule, PathsInfo, TurnMessage } from './engine'
import { createEngine } from './engine/engine'
import { Lanes, TOOL_RX } from './lanes'

/**
 * The Ctx every part receives at register time. Its fields are the real
 * ones from session.start on (the plugin's name — and so the environment —
 * is only known through `$`); before that `env` reads 'staging', `root` ''
 * and `api()` undefined.
 */
function lateCtx(): Ctx & { bind(real: Ctx): void } {
  let real: Ctx | undefined
  return {
    get env(): Env {
      return real?.env ?? 'staging'
    },
    get root() {
      return real?.root ?? ''
    },
    api: () => (real ? real.api() : Promise.resolve(undefined)),
    forgetApi: () => real?.forgetApi(),
    bind(r) {
      real = r
    },
  }
}

// ── the real port, over `$` ─────────────────────────────────────────────────
//
// `$.env` and `$.state` take literals only (claude plugin validate reads them
// off the source), so each name is spelled out. promote_export.py renames the
// plugin to `memhub` for prod and must rewrite the `'memhub-staging'` state
// refs below (and types/index.d.ts) with it: a plugin may write only its own
// state.

const FIRES = { plugin: 'memhub', key: 'fires' } as const
const LANES = { plugin: 'memhub', key: 'lanes' } as const
const HEALTH = { plugin: 'memhub', key: 'health' } as const

export function ioOf($: EngineInterface): Io {
  return {
    pluginName: $.plugin.name,
    root: rootOf($.plugin.root),
    now: () => $.clock.now(),
    sessionId: () => $.session.id(),
    cwd: () => $.session.cwd(),
    run: (argv, init) => $.process.run(argv, init),
    fetch: (url, init) => $.http.fetch(url, init),
    readFile: async path => {
      const text: unknown = await $.fs.read(path)
      if (typeof text !== 'string') throw new Error(`not text: ${path}`)
      return text
    },
    writeFile: (path, text) => $.fs.write(path, text),
    getLanesVar: env => (env === 'prod' ? $.env.get('MEMHUB_MOD_LANES_PROD') : $.env.get('MEMHUB_MOD_LANES_STAGING')),
    setLanesVar: (env, value) =>
      env === 'prod' ? $.env.set('MEMHUB_MOD_LANES_PROD', value) : $.env.set('MEMHUB_MOD_LANES_STAGING', value),
    getState: async key => {
      const read =
        key === 'fires' ? await $.state.get(FIRES) : key === 'lanes' ? await $.state.get(LANES) : await $.state.get(HEALTH)
      return read as { value: never; version: number }
    },
    setState: async (key, value) => {
      if (key === 'fires') await $.state.set(FIRES, value as FireNote[])
      else if (key === 'lanes') await $.state.set(LANES, value as LaneName[])
      else await $.state.set(HEALTH, value as string)
    },
    status: text => $.ui.status(text),
    log: text => $.ui.log(text),
    debug: text => $.ui.log(text, { to: 'debug' }),
    append: async (type, text) => {
      const r: unknown = await $.session.append({ message: { type, content: [{ type: 'text', text }] } })
      if (r && typeof r === 'object' && 'deny' in r && (r as { deny?: unknown }).deny) {
        throw new Error(`session.append refused: ${String((r as { deny: unknown }).deny)}`)
      }
    },
    every: (ms, fn) => $.clock.every(ms, fn),
    sleep: ms => $.clock.sleep(ms),
  }
}

/**
 * What the engine needs from `$` beyond the shell's Io: built by `extrasOf`
 * (the one place `$` is read for it); the tests leave it out.
 */
export type EngineExtras = {
  stat(path: string, resolve?: boolean): Promise<FileStat | undefined>
  /** The main conversation as ApiMessage (`$.session.messages({ as: "api" })`). */
  messages(): Promise<readonly TurnMessage[] | undefined>
  /** `$.session.turns()`: user prompts so far, the judge's per-turn id. */
  turns(): Promise<number>
  env(): Promise<EngineEnv>
  home(): Promise<string>
  sleep(ms: number): Promise<void>
}

export function extrasOf($: EngineInterface): EngineExtras {
  return {
    stat: async (path, resolve) => {
      try {
        const st = await $.fs.stat(path, resolve ? { resolve: true } : undefined)
        return { kind: st.kind, size: st.size, mtimeMs: st.mtimeMs, ...(st.realPath ? { realPath: st.realPath } : {}) }
      } catch {
        return undefined
      }
    },
    messages: async () => {
      try {
        return (await $.session.messages({ as: 'api' })) as unknown as TurnMessage[]
      } catch {
        return undefined
      }
    },
    turns: () => $.session.turns(),
    env: async () => ({
      recall: await $.env.get('MEMHUB_RULEBOOK_RECALL'),
      judge: await $.env.get('MEMHUB_RULEBOOK_JUDGE'),
      timeoutS: await $.env.get('MEMHUB_RULEBOOK_TIMEOUT_S'),
      baseBranch: await $.env.get('MEMHUB_RULEBOOK_BASE_BRANCH'),
      briefBudget: await $.env.get('MEMHUB_BRIEF_TOKEN_BUDGET'),
    }),
    home: async () => (await $.env.get('HOME')) ?? '',
    sleep: ms => $.clock.sleep(ms),
  }
}

/** `read_facts`' newline count for a file past the `$.fs` read cap, by the Python's own loop. */
const COUNT_LINES_PY =
  'import sys\n' +
  'total, seen, last = 0, 0, b"\\n"\n' +
  'with open(sys.argv[1], "rb") as f:\n' +
  '    while seen < (64 << 20):\n' +
  '        chunk = f.read(1 << 20)\n' +
  '        if not chunk: break\n' +
  '        total += chunk.count(b"\\n"); seen += len(chunk); last = chunk[-1:]\n' +
  'if seen and last != b"\\n": total += 1\n' +
  'print(total)\n'

/** The engine's own I/O, over the shell's port (and `$`, through `extras`). */
function engineIoOf(io: Io, ctx: Ctx, extras: EngineExtras | undefined, home: string): EngineIO {
  const pathsCache = new Map<string, Promise<PathsInfo | undefined>>()
  return {
    git: async (argv, cwd, timeoutMs = 10_000) => {
      const r = await io.run(['git', ...argv], { cwd, timeoutMs })
      return { code: r.exitCode, stdout: r.stdout }
    },
    readText: path => io.readFile(path).catch(() => undefined),
    writeText: (path, text) => io.writeFile(path, text).catch(() => undefined),
    stat: (path, resolve) => (extras ? extras.stat(path, resolve) : Promise.resolve(undefined)),
    countLines: async path => {
      try {
        const r = await io.run(['python3', '-c', COUNT_LINES_PY, path], { timeoutMs: 10_000 })
        const n = Number(r.stdout.trim())
        return r.exitCode === 0 && Number.isInteger(n) ? n : undefined
      } catch {
        return undefined
      }
    },
    now: () => Date.now(),
    sleep: ms => (extras ? extras.sleep(ms) : new Promise<void>(() => undefined)),
    tzOffsetMinutes: ms => -new Date(ms).getTimezoneOffset(),
    home,
    env: () => (extras ? extras.env() : Promise.resolve({})),
    paths: dir => {
      let p = pathsCache.get(dir)
      if (!p) {
        p = (async () => {
          const r = await io.run(['python3', `${ctx.root}/${MOD_CLI}`, 'paths', '--env', ctx.env, '--cwd', dir], { timeoutMs: 10_000 })
          const got = lastJson(r.stdout) as Record<string, unknown> | undefined
          if (r.exitCode !== 0 || !got || typeof got.base !== 'string') return undefined
          return { repo: typeof got.repo === 'string' ? got.repo : '', root: typeof got.root === 'string' ? got.root : null, base: got.base }
        })().catch(() => undefined)
        pathsCache.set(dir, p)
        // A failed answer is not kept: the next call asks again.
        void p.then(v => {
          if (!v) pathsCache.delete(dir)
        })
      }
      return p
    },
    state: async (ops, cwd) => {
      try {
        const r = await io.run(['python3', `${ctx.root}/${MOD_STATE}`], { stdin: JSON.stringify({ cwd, ops }), timeoutMs: 10_000 })
        const got = lastJson(r.stdout) as { results?: unknown } | undefined
        return r.exitCode === 0 && got && Array.isArray(got.results) ? (got.results as Record<string, unknown>[]) : undefined
      } catch {
        return undefined
      }
    },
    judge: async body => {
      const api = await ctx.api()
      if (!api) return undefined
      try {
        const r = await io.fetch(`${api.base}/v1/team/rulebook/judge`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${api.bearer}`, 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(body),
        })
        if (r.status === 401) ctx.forgetApi()
        if (!r.ok) return { status: r.status }
        if (!r.text.trim()) return { status: r.status, data: null }
        const p = JSON.parse(r.text) as unknown
        // mcp_http.rest: a non-zero envelope code is a failure the transport reported as 2xx.
        if (p && typeof p === 'object' && 'code' in p) {
          const env = p as { code?: unknown; data?: unknown }
          if (env.code !== 0) return undefined
          return { status: r.status, data: 'data' in env ? env.data : p }
        }
        return { status: r.status, data: p }
      } catch {
        return undefined
      }
    },
    turn: async () => {
      if (!extras) return undefined
      const [messages, n] = await Promise.all([extras.messages(), extras.turns().catch(() => -1)])
      return messages ? { id: n >= 0 ? `mod-turn-${n}` : '', messages } : undefined
    },
  }
}

/** This plugin's manifest version: the `hook_version` the mod reports (spec §4.1). */
async function versionOf(io: Io): Promise<string | undefined> {
  try {
    const v = (JSON.parse(await io.readFile(`${io.root}/.claude-plugin/plugin.json`)) as { version?: unknown }).version
    return typeof v === 'string' && /^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(v) ? v : undefined
  } catch {
    return undefined
  }
}

/**
 * A matcher every event of its kind matches. One module may hook an event
 * once without a matcher (a second is refused at load), and the companion
 * already hooks `session.start` and `turn.complete` bare; a matcher on the
 * shell's own makes them distinct registrations without narrowing them.
 */
/** The shared-state writer (scripts/rulebook_mod_state.py): the Python's own lock. */
const MOD_STATE = 'scripts/rulebook_mod_state.py'

const EVERY_SESSION = { cwd: /^/ } as const
const EVERY_TURN = { turnId: /^/ } as const

/** Everything the shell holds for the session, built at session.start. */
export type Shell = {
  io: Io
  ctx: Ctx
  claims: Claims
  act: Act
  lanes: Lanes
  book: Book | undefined
  /** The remote `mod_lanes` switch; undefined where there is no engine. */
  remote?: RemoteSwitch
  /** Settles once the book is held and the lanes are claimed, or the load gave up. */
  ready: Promise<void>
  /** The first prompt of the process has come: startup is past (see `boot`). */
  firstPrompt(): Promise<void>
  /**
   * At every prompt, before the lanes: a session id that changed in-process
   * (/clear, resume) gets the claim re-stamped (claims.ts `restamp`). Python
   * served that new session until now, its SessionStart preamble included,
   * so the session lane counts it as delivered.
   */
  sessionChanged(): Promise<void>
}

/**
 * The shell's session.start, without `$` (so a test drives it through a fake
 * Io): claims → book → remote switch → session lane.
 *
 * `pastStartup` is false in a fresh process: the startup session's posture
 * preamble is then Python's (its SessionStart hook runs before anything here
 * could claim), and `session` is claimed only once the first prompt has come
 * AND the book is held, whichever is later. A hot reload is long past startup.
 */
export async function boot(
  io: Io,
  ctx: Ctx,
  makeEngine: ((io: EngineIO) => Engine) | undefined,
  opts: {
    cwd: string
    pastStartup: boolean
    toHook?: (row: Record<string, unknown>) => HookRule | null
    /** What the engine reads off `$` (extrasOf); absent in the shell's tests. */
    extras?: EngineExtras
  },
): Promise<Shell> {
  const status = new StatusLine(io)
  const claims = new Claims(io, ctx.env, status)
  const act = new Act(io, ctx, status)
  // Whatever the variable lists now is not this module's to serve yet: an
  // inherited value (a `claude` started from Bash) or the module before a
  // reload. Python serves until the claim below.
  await claims.forgetInherited()
  let pastStartup = opts.pastStartup
  let isLoaded = false
  if (!makeEngine) {
    await claims.failLoad()
    const lanes = new Lanes(io, NO_ENGINE, claims, act)
    return { io, ctx, claims, act, lanes, book: undefined, ready: Promise.resolve(), firstPrompt: async () => {}, sessionChanged: async () => {} }
  }
  const home = opts.extras ? await opts.extras.home().catch(() => '') : ''
  const engine = makeEngine(engineIoOf(io, ctx, opts.extras, home))
  const hookVersion = await versionOf(io)
  const book = new Book(io, ctx, engine, claims, status, { hookVersion, toHook: opts.toHook })
  // The remote switch (book.ts): a false gives every lane to Python for the
  // session, and stops the book's refresh, which then serves nothing.
  const remote = new RemoteSwitch(io, ctx, claims, status, { hookVersion, onOff: () => book.stop() })
  const lanes = new Lanes(io, engine, claims, act)
  // Claim late, in the background: the first prompt never waits on the book,
  // and until it is held Python serves every lane.
  const ready = (async () => {
    try {
      if (!(await book.load(opts.cwd))) return
      // Before any claim: switched off, nothing is claimed (and `firstPrompt`
      // claims nothing either: every lane is released for the session).
      if (!(await remote.allowsClaim())) return
      isLoaded = true
      await claims.claim(pastStartup ? ['pre', 'post', 'prompt', 'session'] : ['pre', 'post', 'prompt'])
      await lanes.sessionStart(true)
      book.start()
      remote.start()
    } catch (err) {
      act.unhealthy('load', err)
      await claims.failLoad().catch(() => undefined)
    }
  })()
  return {
    io,
    ctx,
    claims,
    act,
    lanes,
    book,
    remote,
    ready,
    async firstPrompt() {
      if (pastStartup) return
      pastStartup = true
      if (isLoaded) await claims.claim(['session'])
    },
    async sessionChanged() {
      if (await claims.restamp()) await lanes.sessionStart(true)
    },
  }
}

/** Stands in where there is no engine: every lane is released, so nothing calls it. */
const NO_ENGINE: Engine = {
  setBook() {},
  pre: () => Promise.reject(new Error('no engine')),
  post: () => Promise.reject(new Error('no engine')),
  prompt: () => Promise.reject(new Error('no engine')),
  session: () => Promise.reject(new Error('no engine')),
}

export function register(on: On) {
  const ctx = lateCtx()
  let shell: Shell | undefined

  // ── shell: claims → book → remote switch → session lane ───────────────────
  on('session.start', EVERY_SESSION, async ($: EngineInterface, e, next) => {
    // The shell failing to start must never cost the companion its start:
    // caught here, and the shell stays off (no claim, so Python serves).
    shell = undefined
    try {
      const io = ioOf($)
      const env = envOf(io.pluginName)
      if (env) {
        const real = makeCtx(io, env)
        ctx.bind(real)
        // A fresh process has no `lanes` in $.state; a hot reload keeps it.
        const pastStartup = (await io.getState('lanes')).version > 0
        shell = await boot(io, real, createEngine, { cwd: e.cwd, pastStartup, extras: extrasOf($) })
      }
    } catch {
      shell = undefined
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // ── rulebook lanes ────────────────────────────────────────────────────────
  on('tool.call', { tool: TOOL_RX }, async ($, e, next) => {
    if (!shell) return next(e)
    return shell.lanes.toolCall(e, next as never) as never
  }).catch(async ($, e, next) => {
    // Overran or threw outside the lanes' own handling: hand the call to Python.
    // `next` here is replay-safe (claude-code.d.ts `Caught`): when the hook had
    // called it (`next.called`), `next(e)` resolves to what that call settled
    // to and nothing beneath runs again, so the tool never runs twice; the
    // d.ts's own pattern for a tool.call guard is
    // `next.called ? next(e) : { deny }`. Not called: the call has not run,
    // and Python's hook beneath serves it once `pre` is out of the claim.
    if (!next.called && shell?.claims.has('pre')) {
      await shell.claims.fail('pre').catch(() => undefined)
      try {
        return await next(e)
      } finally {
        await shell.claims.recover('pre').catch(() => undefined)
      }
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (!shell) return next(e)
    // Re-stamp the claim first: until it carries this session's id, Python serves.
    await shell.sessionChanged().catch(() => undefined)
    await shell.firstPrompt()
    return shell.lanes.promptSubmit(e, next as never) as never
  }).catch(async ($, e, next) => {
    // Replay-safe `next`, as for tool.call above.
    if (!next.called && shell?.claims.has('prompt')) {
      await shell.claims.fail('prompt').catch(() => undefined)
      try {
        return await next(e)
      } finally {
        await shell.claims.recover('prompt').catch(() => undefined)
      }
    }
    return next(e)
  })

  // A compaction summarises the preamble away; the next prompt brings it back,
  // and the next fire the no-echo note (act.ts NO_ECHO_NOTE).
  on('session.compact', async ($, e, next) => {
    const r = await next(e)
    if (shell && e.trigger !== 'precompute' && e.agentId === undefined && !('skip' in r)) {
      const sessionId = await shell.io.sessionId()
      shell.lanes.compacted(sessionId)
      shell.act.compacted(sessionId)
    }
    return r
  }).catch(($, e, next) => next(e))

  // The turn's batched ledger events (spec §4.7).
  on('turn.complete', EVERY_TURN, async ($, e, next) => {
    if (shell) await shell.act.flush(await shell.io.sessionId())
    return next(e)
  }).catch(($, e, next) => next(e))

  // ── companion: last, so innermost ─────────────────────────────────────────
  registerCompanion(on, ctx)
}
