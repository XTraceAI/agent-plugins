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
}

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
  let cached: Promise<Api | undefined> | undefined
  const resolve = async (): Promise<Api | undefined> => {
    const r = await io.run(['python3', `${io.root}/${MOD_CLI}`, 'api-info', '--env', env], { timeoutMs: 10_000 })
    if (r.exitCode !== 0) throw new Error(`api-info exited ${r.exitCode}: ${r.stderr.slice(0, 200)}`)
    const got = lastJson(r.stdout) as Partial<Api> | undefined
    if (got && typeof got.base === 'string' && got.base && typeof got.bearer === 'string' && got.bearer) {
      const studio = typeof got.studio === 'string' ? got.studio.replace(/\/+$/, '') : ''
      return { base: got.base.replace(/\/+$/, ''), bearer: got.bearer, ...(studio ? { studio } : {}) }
    }
    return undefined
  }
  return {
    env,
    root: io.root,
    async api() {
      if (!cached) cached = resolve()
      const mine = cached
      try {
        const api = await mine
        if (!api && cached === mine) cached = undefined
        return api
      } catch {
        if (cached === mine) cached = undefined
        return undefined
      }
    },
    forgetApi() {
      cached = undefined
    },
  }
}

/** `$.plugin.root` names the folder holding plugin.json; the scripts sit beside `.claude-plugin/`. */
export const rootOf = (pluginRoot: string) => pluginRoot.replace(/\/\.claude-plugin\/?$/, '')
