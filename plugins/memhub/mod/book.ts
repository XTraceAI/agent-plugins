// The rule book on the mod path (spec §4.1): load the cached book at
// session.start, keep it fresh with `$.clock.every`, and hand the rows to the
// engine. The book file is the one rulebook_hook.py reads and writes —
// `{"etag", "fetched_at", "rules"}` at the path `rulebook_mod_cli.py paths`
// names — so the Python fallback is never staler than the mod.
//
// Mirrors rulebook_hook.py `fetch_book` (GET /v1/team/rulebook/rules
// ?view=hook&repo=<repo>&hook_version=<v> with If-None-Match; no `status=`)
// and `_norm_rules` (to_hook_rule per row, first id wins). Replaces the
// detached `fetch` child `maybe_refresh` spawns (W4).

import type { Claims, StatusLine } from './claims'
import type { Ctx, Io } from './ctx'
import { lastJson, MOD_CLI } from './ctx'
import type { Engine, HookRule } from './engine'
import { toHookRule as shapeRow, versionTuple } from './engine/rules'

/**
 * One served row → the flat hook rule (engine/rules.ts `toHookRule`), shaped
 * for this build's own version so a `min_hook_version` row degrades exactly
 * as it does in the Python hook beside it. No forward-test claim here
 * (`activeBase` false): /memhub:create-rule's private-base forward test is
 * served by the Python hook, which reads the claim.
 */
export const hookRuleFor = (hookVersion: string | undefined) => (row: Record<string, unknown>): HookRule | null =>
  shapeRow(row, { hookVersion: versionTuple(hookVersion ?? null), activeBase: false })

export const toHookRule = hookRuleFor(undefined)

export const API_PATH = '/v1/team/rulebook'
export const REFRESH_MS = 60_000
/** Consecutive refresh failures before the status line says so (a blip is not news). */
const REFRESH_FAILURES_SHOWN = 3
/**
 * Consecutive refresh failures after which the mod gives every lane back to
 * Python for the session — once its book is also older than Python's own
 * staleness window (rulebook_hook.py `REFRESH_AFTER_S`), so Python, whose
 * urllib takes a different road than `$.http.fetch`, would refresh it.
 */
export const RELEASE_AFTER_FAILURES = 5
/** rulebook_hook.py `REFRESH_AFTER_S` (60 s): Python refreshes a book older than this. */
export const PYTHON_REFRESH_AFTER_MS = 60_000

export const FETCH_REFUSED = "MemHub rules: your organization's web-fetch policy refuses the mod — the command hooks serve the rules"
export const REFRESH_GAVE_UP = 'MemHub rules: the mod could not refresh the rules — the command hooks serve them'

/**
 * Whether a `$.http.fetch` rejection is a refusal, never a network blip.
 *
 * The engine refuses before any request leaves with
 * `<plugin>: $.http.fetch: refused: <why>` — the organization's web-fetch
 * policy (`allow_web_fetch`: "Network access from plugins …"),
 * CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, a confined eval — and a hook's
 * `{ deny }` on `http.fetch` rejects as `<plugin>: $.http.fetch: <deny>`. A
 * request that went out and failed reads `$.http.fetch(<url>) failed: …` or
 * `… aborted: …`, with the URL in parentheses. (Claude Code 2.1.292; the
 * d.ts says only "unless the organization's web-fetch policy refuses it".)
 * A refusal does not heal by retrying, so it is acted on at once.
 */
export function isFetchRefused(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err)
  return /\$\.http\.fetch: /.test(msg)
}

/** A `fetched_at` Python wrote (microseconds, an offset) as epoch ms; undefined when unreadable. */
export function msOfIso(iso: string | null | undefined): number | undefined {
  if (typeof iso !== 'string') return undefined
  const ms = Date.parse(iso.replace(/(\.\d{3})\d+/, '$1'))
  return Number.isFinite(ms) ? ms : undefined
}

export type BookFile = { etag?: string | null; fetched_at?: string | null; rules: Record<string, unknown>[] }
export type Paths = { book: string; repo: string; ledger?: string }

/** The cached book, as `load_book` accepts it: a dict whose `rules` is a list. */
export function parseBook(text: string): BookFile | undefined {
  try {
    const b = JSON.parse(text) as unknown
    if (b && typeof b === 'object' && !Array.isArray(b) && Array.isArray((b as BookFile).rules)) return b as BookFile
  } catch {
    // unreadable is no book, as in Python
  }
  return undefined
}

/** `_norm_rules`: each row through toHookRule, unrunnable rows dropped, the first of an id kept. */
export function shapeRules(
  rows: readonly unknown[],
  toHook: (row: Record<string, unknown>) => HookRule | null = toHookRule,
): HookRule[] {
  const out: HookRule[] = []
  const seen = new Set<string>()
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    const r = toHook(row as Record<string, unknown>)
    if (r && !seen.has(r.id)) {
      seen.add(r.id)
      out.push(r)
    }
  }
  return out
}

/** `_now()`: ISO with microseconds and an explicit offset, which every Python `fromisoformat` reads. */
export function isoOf(ms: number): string {
  return new Date(ms).toISOString().replace(/\.(\d{3})Z$/, '.$1000+00:00')
}

/** `urllib.parse.quote(s, safe="")`: everything but letters, digits and `_.-~` escaped. */
export function quoteAll(s: string): string {
  return encodeURIComponent(s).replace(/[!'()*]/g, c => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
}

/** `require_secure`: a credential rides https, or loopback http only. */
export function isSecure(url: string): boolean {
  const m = /^([a-z][a-z0-9+.-]*):\/\/(\[[^\]]*\]|[^/:?#]*)/i.exec(url)
  if (!m) return false
  if (m[1]!.toLowerCase() === 'https') return true
  const host = m[2]!.toLowerCase().replace(/^\[|\]$/g, '')
  return m[1]!.toLowerCase() === 'http' && (host === 'localhost' || host === '127.0.0.1' || host === '::1')
}

/** `rulebook_mod_cli.py paths --env <env> --cwd <dir>`: the book file, the repo and the ledger dir. */
export async function pathsFor(io: Io, ctx: Ctx, cwd: string): Promise<Paths | undefined> {
  const r = await io.run(['python3', `${ctx.root}/${MOD_CLI}`, 'paths', '--env', ctx.env, '--cwd', cwd], { timeoutMs: 10_000 })
  if (r.exitCode !== 0) throw new Error(`paths exited ${r.exitCode}: ${r.stderr.slice(0, 200)}`)
  const got = lastJson(r.stdout) as Record<string, unknown> | undefined
  if (!got || typeof got !== 'object') throw new Error('paths printed no JSON')
  const book = got.book ?? got.book_path
  const repo = got.repo
  const ledger = got.ledger ?? got.ledger_dir
  // No repo (not a git checkout, or no remote): no book to serve, nothing to claim.
  if (typeof repo !== 'string' || !repo || typeof book !== 'string' || !book) return undefined
  return { book, repo, ledger: typeof ledger === 'string' ? ledger : undefined }
}

export type RefreshOutcome = 'updated' | 'unchanged' | 'upgrade' | 'skipped' | 'failed'

export class Book {
  repo: string | undefined
  path: string | undefined
  private etag: string | undefined
  private timer: { cancel(): void } | undefined
  private inFlight = false
  private failures = 0
  private stopped = false
  /** When the book the engine holds was last known current (epoch ms). */
  private freshAt: number | undefined

  constructor(
    private io: Io,
    private ctx: Ctx,
    private engine: Engine,
    private claims: Claims,
    private status: StatusLine,
    private opts: { hookVersion?: string; toHook?: (row: Record<string, unknown>) => HookRule | null } = {},
  ) {}

  /**
   * At session.start: find the book, read it, hand it to the engine. A
   * missing cache is fetched once, here. Resolves true once the engine holds
   * a book (then the lanes may be claimed), false when there is none to hold
   * (signed out with no cache, no repo): Python then serves, with no book
   * either. Rejects on a real failure (the caller releases every lane).
   */
  async load(cwd: string): Promise<boolean> {
    const paths = await pathsFor(this.io, this.ctx, cwd)
    if (!paths) return false
    this.repo = paths.repo
    this.path = paths.book
    // A recorded upgrade notice suspends the cached rules (rulebook_hook
    // `upgrade_status`); only Python can validate and clear it, so it serves.
    if (await this.io.readFile(`${paths.book}.upgrade`).then(() => true, () => false)) return false
    const text = await this.io.readFile(paths.book).catch(() => undefined)
    const cached = text === undefined ? undefined : parseBook(text)
    if (cached) {
      this.etag = cached.etag ?? undefined
      this.freshAt = msOfIso(cached.fetched_at)
      await this.hand(cached.rules)
      return true
    }
    return (await this.refresh()) === 'updated'
  }

  /** Every minute until stopped. A tick never overlaps the one before it. */
  start(): void {
    if (this.timer || this.stopped) return
    this.timer = this.io.every(REFRESH_MS, () => {
      void this.refresh()
    })
  }

  stop(): void {
    this.stopped = true
    this.timer?.cancel()
    this.timer = undefined
  }

  /** One GET with If-None-Match: 200 → write the file + setBook; 304 → nothing; 426 → stop serving. */
  async refresh(): Promise<RefreshOutcome> {
    if (this.inFlight || this.stopped || !this.repo || !this.path) return 'skipped'
    this.inFlight = true
    try {
      return await this.fetchOnce(true)
    } catch (err) {
      await this.failed(err)
      return 'failed'
    } finally {
      this.inFlight = false
    }
  }

  private async fetchOnce(retryOn401: boolean): Promise<RefreshOutcome> {
    const api = await this.ctx.api()
    if (!api) return 'skipped'
    if (!isSecure(api.base)) throw new Error('refusing to send credentials over cleartext')
    let q = `view=hook&repo=${quoteAll(this.repo!)}`
    if (this.opts.hookVersion) q += `&hook_version=${this.opts.hookVersion}`
    const headers: Record<string, string> = { Authorization: `Bearer ${api.bearer}`, Accept: 'application/json' }
    if (this.opts.hookVersion) headers['X-MemHub-Plugin-Version'] = this.opts.hookVersion
    if (this.etag) headers['If-None-Match'] = this.etag
    const reply = await this.io.fetch(`${api.base}${API_PATH}/rules?${q}`, { method: 'GET', headers })
    if (reply.status === 304) {
      this.freshAt = await this.io.now()
      this.succeeded()
      return 'unchanged'
    }
    if (reply.status === 401 && retryOn401) {
      this.ctx.forgetApi()
      return this.fetchOnce(false)
    }
    if (reply.status === 426) {
      await this.upgradeRequired(reply.text)
      return 'upgrade'
    }
    if (reply.status !== 200) throw new Error(`rules fetch HTTP ${reply.status}`)
    const rules = rulesOf(reply.text)
    if (!rules) throw new Error('rules fetch: unexpected reply shape')
    const etag = reply.headers['etag']
    const now = await this.io.now()
    const file: BookFile = { etag: etag ?? null, fetched_at: isoOf(now), rules }
    // The engine first: what the mod serves must never lag the file Python reads.
    await this.hand(rules)
    this.etag = etag
    this.freshAt = now
    await this.io.writeFile(this.path!, JSON.stringify(file))
    this.succeeded()
    return 'updated'
  }

  private async hand(rows: readonly unknown[]): Promise<void> {
    const rules = shapeRules(rows, this.opts.toHook ?? hookRuleFor(this.opts.hookVersion))
    this.engine.setBook(rules, { repo: this.repo!, fetchedAt: await this.io.now() })
  }

  /**
   * The server refuses this plugin version: cached rules are suspended. The
   * mod stops serving every lane, and Python's own fetch records the upgrade
   * notice beside the book (`<book>.upgrade`), which is what makes the Python
   * lanes suspend the cached rules and tell the agent.
   */
  private async upgradeRequired(body: string): Promise<void> {
    this.stop()
    let minimum: string | undefined
    try {
      const p = JSON.parse(body) as { data?: { minimum_version?: unknown } }
      if (typeof p?.data?.minimum_version === 'string' && /^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(p.data.minimum_version)) {
        minimum = p.data.minimum_version
      }
    } catch {
      // the status line says it without the number
    }
    this.status.set(
      'upgrade',
      `MemHub: update the plugin${minimum ? ` (minimum ${minimum})` : ''} — team rules are paused`,
    )
    await this.claims.release()
    await this.io
      .run(['python3', `${this.ctx.root}/scripts/rulebook_hook.py`, 'fetch', this.repo!], { timeoutMs: 15_000 })
      .catch(() => undefined)
  }

  private succeeded(): void {
    this.failures = 0
    this.status.set('refresh', undefined)
  }

  /**
   * A refresh failed. A refusal (the org's web-fetch policy, a hook's deny)
   * gives every lane back to Python at once: the mod would serve a stale book
   * forever and the judge fails the same way, while Python's own fetch is not
   * subject to that policy. A network blip is retried; after
   * RELEASE_AFTER_FAILURES in a row with a book older than Python's staleness
   * window, the lanes go back to Python too.
   */
  private async failed(err: unknown): Promise<void> {
    if (isFetchRefused(err)) return this.giveUp(FETCH_REFUSED)
    this.failures += 1
    if (this.failures >= RELEASE_AFTER_FAILURES) {
      const age = this.freshAt === undefined ? Infinity : (await this.io.now()) - this.freshAt
      if (age > PYTHON_REFRESH_AFTER_MS) return this.giveUp(REFRESH_GAVE_UP)
    }
    if (this.failures >= REFRESH_FAILURES_SHOWN) {
      const why = err instanceof Error ? err.message : String(err)
      this.status.set('refresh', `MemHub: team rules not refreshed — ${why}`.slice(0, 200))
    }
  }

  /** Stop refreshing and release every lane for the session, saying why on the status line. */
  private async giveUp(why: string): Promise<void> {
    this.stop()
    this.status.set('refresh', undefined)
    this.status.set('claims', why)
    await this.claims.release().catch(() => undefined)
  }
}

/** `{"code":0,"msg":"ok","data":{"rules":[…]}}` or the bare `{"rules":[…]}`; undefined otherwise. */
function rulesOf(text: string): Record<string, unknown>[] | undefined {
  try {
    let p = JSON.parse(text) as Record<string, unknown> | null
    if (p && typeof p === 'object' && 'code' in p) {
      if (p.code !== 0) return undefined
      if ('data' in p) p = p.data as Record<string, unknown> | null
    }
    const rules = p && typeof p === 'object' ? (p as { rules?: unknown }).rules : undefined
    return Array.isArray(rules) ? (rules as Record<string, unknown>[]) : undefined
  } catch {
    return undefined
  }
}

// ── the remote switch (ENG-1206) ────────────────────────────────────────────
//
// `GET /v1/plugin/compatibility` answers `"mod_lanes": true|false`: false
// tells every mod to leave the Rulebook lanes to the Python command hooks. A
// missing field means true, so an older backend changes nothing. Mirrors
// plugin_compatibility.py `check`: the endpoint at the REST base's origin, the
// bearer, `Accept: application/json` and `X-MemHub-Plugin-Version`
// (mcp_http.rest + plugin_version.request_headers), the `{code, data}`
// envelope unwrapped.
//
// Read at boot before any lane is claimed, then every SWITCH_MS while lanes
// are claimed. Each request races COMPAT_TIMEOUT_MS (Python's `timeout=2`;
// `$.http.fetch` has none of its own). Fail direction: a read that does not
// answer (network, timeout, 401, a non-200, a refused `$.http.fetch`, an
// unexpected shape) changes nothing —
// a blip never flips lanes; a refused fetch is already acted on by the book's
// own refresh. A false is final for the session: lanes released then are
// never re-claimed by this module, even if the switch reads true again (a hot
// reload is a fresh module and reads the switch afresh at its own boot).

export const COMPAT_PATH = '/v1/plugin/compatibility'
export const SWITCH_MS = 5 * 60_000
/** plugin_compatibility.check's `timeout=2`. */
export const COMPAT_TIMEOUT_MS = 2_000
export const SWITCHED_OFF = 'MemHub rules: served by the command hooks (remote switch)'

/** on: the mod may serve (true, or the field absent); off: give every lane to Python; unknown: keep the current state. */
export type SwitchRead = 'on' | 'off' | 'unknown'

/** `scheme://netloc` of a URL, as plugin_compatibility.py builds the endpoint; undefined when unparseable. */
export function originOf(url: string): string | undefined {
  const m = /^([a-z][a-z0-9+.-]*:\/\/[^/?#]+)/i.exec(url)
  return m ? m[1] : undefined
}

/** The compatibility reply's `mod_lanes`, as a switch read. */
export function modLanesOf(text: string): SwitchRead {
  try {
    let p = JSON.parse(text) as unknown
    if (p && typeof p === 'object' && !Array.isArray(p) && 'code' in p) {
      const env = p as { code?: unknown; data?: unknown }
      if (env.code !== 0) return 'unknown'
      if ('data' in env) p = env.data
    }
    if (!p || typeof p !== 'object' || Array.isArray(p)) return 'unknown'
    const v = (p as { mod_lanes?: unknown }).mod_lanes
    if (v === false) return 'off'
    if (v === true || v === undefined) return 'on'
    return 'unknown'
  } catch {
    return 'unknown'
  }
}

export class RemoteSwitch {
  /** The switch read false this session: lanes are released and stay so. */
  off = false
  private timer: { cancel(): void } | undefined
  private inFlight = false

  constructor(
    private io: Io,
    private ctx: Ctx,
    private claims: Claims,
    private status: StatusLine,
    private opts: { hookVersion?: string; onOff?: () => void } = {},
  ) {}

  /** One read of the switch; never rejects (a failure is 'unknown'). */
  async read(): Promise<SwitchRead> {
    try {
      return await this.readOnce(true)
    } catch (err) {
      this.io.debug(`MemHub rules: remote switch not read — ${err instanceof Error ? err.message : String(err)}`)
      return 'unknown'
    }
  }

  /** At boot, before any claim: false when the switch is off (every lane then stays Python's). */
  async allowsClaim(): Promise<boolean> {
    if ((await this.read()) !== 'off') return true
    await this.turnOff()
    return false
  }

  /** Re-read every SWITCH_MS while the module lives and the switch is not off (`turnOff` cancels the timer). */
  start(): void {
    if (this.timer || this.off) return
    this.timer = this.io.every(SWITCH_MS, () => {
      void this.poll()
    })
  }

  /**
   * One read: an off releases every lane for the session; on or unknown
   * changes nothing — in particular an on after an off claims nothing back.
   * Overlapping reads are skipped; a hung one is bounded by COMPAT_TIMEOUT_MS,
   * so the next tick reads again.
   */
  async poll(): Promise<void> {
    if (this.inFlight) return
    this.inFlight = true
    try {
      if ((await this.read()) === 'off') await this.turnOff()
    } finally {
      this.inFlight = false
    }
  }

  private async readOnce(retryOn401: boolean): Promise<SwitchRead> {
    const api = await this.ctx.api()
    if (!api) return 'unknown'
    if (!isSecure(api.base)) throw new Error('refusing to send credentials over cleartext')
    const origin = originOf(api.base)
    if (!origin) throw new Error('no origin in the API base')
    // Always sent, as mcp_http.rest does: plugin_version.py says 'unknown'
    // when the manifest has no readable x.y.z, and so does this.
    const headers: Record<string, string> = {
      Authorization: `Bearer ${api.bearer}`,
      Accept: 'application/json',
      'X-MemHub-Plugin-Version': this.opts.hookVersion ?? 'unknown',
    }
    const timedOut = this.io.sleep(COMPAT_TIMEOUT_MS).then(() => {
      throw new Error(`compatibility: no answer in ${COMPAT_TIMEOUT_MS} ms`)
    })
    const reply = await Promise.race([this.io.fetch(`${origin}${COMPAT_PATH}`, { method: 'GET', headers }), timedOut])
    if (reply.status === 401 && retryOn401) {
      this.ctx.forgetApi()
      return this.readOnce(false)
    }
    if (reply.status !== 200) throw new Error(`compatibility HTTP ${reply.status}`)
    return modLanesOf(reply.text)
  }

  /** Release every lane for the rest of the session and say so; the renewal and recovery paths never bring one back (claims.ts `lost`). */
  private async turnOff(): Promise<void> {
    if (this.off) return
    this.off = true
    this.timer?.cancel()
    this.timer = undefined
    this.status.set('claims', SWITCHED_OFF)
    this.opts.onOff?.()
    // A failed write already serves nothing and tries to unset the variable
    // (claims.ts `writeFailed`); the variable's lease lapses within LEASE_S
    // if even that failed. Said in the debug log, never on screen.
    await this.claims.release().catch(err => {
      this.io.debug(`MemHub rules: remote switch release failed — ${err instanceof Error ? err.message : String(err)}`)
    })
  }
}
