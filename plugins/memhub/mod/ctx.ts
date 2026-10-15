// What every part of the mod shares, resolved once per session at
// `session.start` by mod/register.ts.
//
// Two things live here:
//   * `Ctx`: the environment (staging | prod), the plugin root and the REST
//     credential, cached in module memory.
//   * `Io`: the narrow port every part of the shell (claims, book, act,
//     lanes) does its I/O through. register.ts builds it from `$` (`ioOf`,
//     which must live there: the engine follows `$` only into functions of
//     the file that holds it); the tests build a fake one. Nothing but
//     register.ts touches `$`, so each part is testable without the engine
//     beneath it.

import type { FireNote, LaneName } from '../types'

export type Env = 'staging' | 'prod'

export type Api = {
  base: string
  bearer: string
  /**
   * The MemHub Studio web app paired with `base` (api-info's `studio`:
   * plugin_onboarding._ORIGINS), or absent for an API with none. The mod
   * links a rule's Studio page from it and keeps no host table of its own.
   */
  studio?: string
}

/**
 * What the last credential lookup said: 'in' with a credential, 'out' when
 * api-info answered signed out, 'unknown' when it could not answer at all
 * (no python3, a broken install): never taken for a signed-out person.
 */
export type SignIn = 'in' | 'out' | 'unknown'

export type Ctx = {
  /** 'staging' for memhub-staging, 'prod' for memhub (promote_export.py changes the name). */
  env: Env
  /** The plugin's install directory (`$.plugin.root`, without a trailing `/.claude-plugin`). */
  root: string
  /**
   * The REST base, bearer and Studio origin, from `python3 scripts/rulebook_mod_cli.py
   * api-info --env <env>`; cached in module memory only (never $.state /
   * $.store: it is a secret). undefined when signed out.
   */
  api(): Promise<Api | undefined>
  /** Drop the cached credential (after a 401) so the next api() re-resolves. */
  forgetApi(): void
  /**
   * Whether MemHub is active in the session's repository: false when the
   * machine's repository allowlist (`~/.config/memhub-plugin/repos.json`,
   * scripts/repo_allow.py) leaves it out, which `rulebook_mod_cli.py paths`
   * answers as `enabled`. The shell settles it at boot (register.ts) once it
   * has asked; true where nothing could ask. The companion stays hidden in
   * a repository MemHub is inactive in, as the Python lanes stay silent there.
   * Resolves to the LATEST answer: the strip's Yes (ENG-1253) lists the
   * repository mid-session and sets it again.
   */
  active(): Promise<Active>
  /**
   * The shell's answer (register.ts `boot`), and later the strip's: the
   * first call settles anyone waiting, each call is what `active()` says next.
   */
  setActive(value: Active): void
  /** The book the shell holds for the session's repository, once loaded (the companion's status strip reads it). */
  book(): BookInfo | undefined
  setBook(info: BookInfo): void
  /** What the last api() lookup said of the sign-in (see SignIn); 'unknown' before any. */
  signIn(): SignIn
}

/** What the status strip says of the book: the repository and how many rules the mod holds for it. */
export type BookInfo = { repo: string; rules: number }

/**
 * The allowlist's answer for the session: `enabled` false where MemHub is
 * dormant, and `repo` the session's repository name — '' outside any checkout,
 * which with a repos.json is dormant too (there is no repository to list).
 * The rest is what the strip's enable ask (ENG-1253) needs, all from `paths`
 * so nothing re-reads repos.json in TypeScript: `restricted`, a repos.json
 * exists; `declined`, the person answered No to this repository before;
 * `identity`, the checkout's name as the list carries it (`org/name`).
 * Each is optional: a shell that could not ask leaves them out, and the ask
 * then never shows.
 */
export type Active = { enabled: boolean; repo: string; restricted?: boolean; declined?: boolean; identity?: string }
export const ACTIVE: Active = { enabled: true, repo: '' }

/** The keys of this plugin's `$.state` contract (types/index.d.ts) the shell writes. */
export type StateShape = {
  fires: FireNote[]
  lanes: LaneName[]
  health: string
}

export type RunResult = { exitCode: number; stdout: string; stderr: string }
export type HttpReply = { status: number; ok: boolean; headers: Record<string, string>; text: string }

/** Every effect the shell has, as one port. */
export interface Io {
  pluginName: string
  root: string
  now(): Promise<number>
  sessionId(): Promise<string>
  cwd(): Promise<string>
  run(argv: readonly string[], init?: { stdin?: string; timeoutMs?: number; cwd?: string }): Promise<RunResult>
  fetch(url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }): Promise<HttpReply>
  /** Rejects when the file is missing or unreadable. */
  readFile(path: string): Promise<string>
  writeFile(path: string, text: string): Promise<void>
  /** `MEMHUB_MOD_LANES_<ENV>`: written only by claims.ts. */
  getLanesVar(env: Env): Promise<string | undefined>
  setLanesVar(env: Env, value: string | undefined): Promise<void>
  getState<K extends keyof StateShape>(key: K): Promise<{ value: StateShape[K] | undefined; version: number }>
  setState<K extends keyof StateShape>(key: K, value: StateShape[K]): Promise<void>
  status(text: string | undefined): void
  /** A dim transcript line for the person (the model never reads it). */
  log(text: string): void
  /** A debug-log line only (`$.ui.log(text, { to: 'debug' })`): never on screen. */
  debug(text: string): void
  /** `$.session.append`: `user` = an isMeta row the model reads; `system` = a notice it never reads. */
  append(type: 'user' | 'system', text: string): Promise<void>
  every(ms: number, fn: () => void): { cancel(): void }
  /** `$.clock.sleep`: the mod has no setTimeout; what a request races to bound itself. */
  sleep(ms: number): Promise<void>
}

/** memhub-staging → staging, memhub → prod; any other name is not ours to guess. */
export function envOf(pluginName: string): Env | undefined {
  if (pluginName === 'memhub-staging') return 'staging'
  if (pluginName === 'memhub') return 'prod'
  return undefined
}

/** The last non-empty stdout line as JSON, or undefined. Scripts may print progress first. */
export function lastJson(stdout: string): unknown {
  const lines = stdout.split('\n').map(l => l.trim()).filter(Boolean)
  const last = lines.at(-1)
  if (last === undefined) return undefined
  try {
    return JSON.parse(last)
  } catch {
    return undefined
  }
}

/**
 * The scope label of a REST rule row's rulebook ("Everyone in Acme", "Just
 * you"): who the rule applies to. '' for a backend that sends none.
 */
export function audienceOf(row: Record<string, unknown>): string {
  const book = row.rulebook
  const label = book && typeof book === 'object' ? (book as { label?: unknown }).label : undefined
  // one line, bounded: it is drawn on the band
  // eslint-disable-next-line no-control-regex
  return typeof label === 'string' ? label.replace(/[\x00-\x1f\x7f]+/g, ' ').trim().slice(0, 120) : ''
}

export const MOD_CLI = 'scripts/rulebook_mod_cli.py'

export function makeCtx(io: Io, env: Env): Ctx {
  // The resolved credential, kept for the session. A signed-out or failed
  // answer is not kept: a person who runs /memhub:login mid-session is picked
  // up on the next refresh tick (only the tick and the judge ask).
  let said: SignIn = 'unknown'
  // what a lookup said is kept only while it is the lookup in `cached`: one
  // overtaken by a forgetApi() and a newer lookup must not overwrite its answer
  const resolve = async (): Promise<{ api?: Api; said: SignIn }> => {
    const r = await io.run(['python3', `${io.root}/${MOD_CLI}`, 'api-info', '--env', env], { timeoutMs: 10_000 })
    if (r.exitCode !== 0) throw new Error(`api-info exited ${r.exitCode}: ${r.stderr.slice(0, 200)}`)
    const got = lastJson(r.stdout) as Partial<Api> | undefined
    if (got && typeof got.base === 'string' && got.base && typeof got.bearer === 'string' && got.bearer) {
      const studio = typeof got.studio === 'string' ? got.studio.replace(/\/+$/, '') : ''
      return { api: { base: got.base.replace(/\/+$/, ''), bearer: got.bearer, ...(studio ? { studio } : {}) }, said: 'in' }
    }
    // `{}` is api-info's signed out; anything else on stdout is not an answer
    return { said: got && typeof got === 'object' && Object.keys(got).length === 0 ? 'out' : 'unknown' }
  }
  let cached: Promise<{ api?: Api; said: SignIn }> | undefined
  const api = async (): Promise<Api | undefined> => {
    if (!cached) cached = resolve()
    const mine = cached
    try {
      const got = await mine
      if (cached === mine) {
        said = got.said
        if (!got.api) cached = undefined
      }
      return got.api
    } catch {
      if (cached === mine) {
        said = 'unknown'
        cached = undefined
      }
      return undefined
    }
  }
  let settleActive: (value: Active) => void = () => undefined
  const firstActive = new Promise<Active>(r => {
    settleActive = r
  })
  // the latest answer, once there is one: the first waits on the shell's boot
  let current: Active | undefined
  let bookInfo: BookInfo | undefined
  return {
    env,
    root: io.root,
    active: () => (current ? Promise.resolve(current) : firstActive),
    setActive(value) {
      current = value
      settleActive(value)
    },
    book: () => bookInfo,
    setBook(info) {
      bookInfo = info
    },
    api,
    forgetApi() {
      cached = undefined
    },
    signIn: () => said,
  }
}

/** `$.plugin.root` names the folder holding plugin.json; the scripts sit beside `.claude-plugin/`. */
export const rootOf = (pluginRoot: string) => pluginRoot.replace(/\/\.claude-plugin\/?$/, '')
