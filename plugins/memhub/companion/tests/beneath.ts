// What the companion's tests put beneath it for MemHub: the host's `$.state`
// (fires, proposals, lanes — an in-memory copy with versions, which the test
// writes as the mod's other parts would), MemHub's REST API (`$.http.fetch`:
// the proposed-rules list and the PATCH that decides one), and the plugin's
// credential as the mod's ctx resolves it (`rulebook_mod_cli.py api-info`,
// mod/ctx.ts makeCtx). Hosts are fakes: no real MemHub host may ship in the
// plugin (promote_export.py). Not a test file: the test files import it.

import type { On } from 'claude-code'

export const BASE = 'https://api.example.test'
/** The Studio web app api-info pairs with BASE. */
export const STUDIO = 'https://app.example.test'
export const BEARER = 'mhk_test_key'

/** The list request rule_decide.py `proposed()` sends. */
export const LIST_URL = `${BASE}/v1/team/rulebook/rules?status=eq.proposed&author=eq.xtrace&order=created_at.desc`

/** A rule row as `GET /v1/team/rulebook/rules` returns it, filed from `session`'s turn 3. */
export const ruleRow = (session: string, ruleId: string, title: string) => ({
  rule_id: ruleId, title, status: 'proposed', author: 'xtrace', source_ref: `${session}#3`,
})

/** The REST envelope around a rules list. */
export const listed = (...rules: object[]) => JSON.stringify({ code: 0, msg: 'ok', data: { rules } })

export type Answer = { status: number; text: string }
export type Sent = { url: string; method: string; headers: Record<string, string>; body?: string }

export type Rest = {
  /** The list's answer, read at each GET; a function answers per call; 'throw' rejects the fetch. */
  list?: Answer | string | (() => Answer | string | 'throw') | 'throw'
  /** The PATCH's answers, one per call in order (the last repeats); 'throw' rejects the fetch. */
  decide?: (Answer | 'throw')[]
  /** Held until the test resolves it: a PATCH still in flight. */
  gate?: Promise<void>
  /** The bearers api-info hands out, one per resolution (the last repeats); '' is no key. */
  bearers?: string[]
  /** The Studio origin api-info hands out; STUDIO unless given ('' pairs none). */
  studio?: string
}

/**
 * The host's `$.state` for this plugin, in memory with a version per key as
 * the host keeps it (a write given `ifVersion` lands only at that version).
 * `write` is a write by another part of the mod (the act side, the lane
 * claims); a drawing that read the value is drawn again by the
 * host, which the test does with the band's `redraw()`.
 */
export function stateBeneath(on: On, seeded: Record<string, unknown> = {}) {
  const held: Record<string, { value: unknown; version: number }> = {}
  for (const [key, value] of Object.entries(seeded)) held[key] = { value, version: 1 }
  on('state.get', async (_, e) => ({ value: held[e.key] ? { ...held[e.key]! } : { value: undefined, version: 0 } }))
  on('state.set', async (_, e) => {
    const was = held[e.key]?.version ?? 0
    const ifVersion = (e as { ifVersion?: number }).ifVersion
    if (ifVersion !== undefined && ifVersion !== was) return { value: { isSet: false, version: was } as const }
    held[e.key] = { value: JSON.parse(JSON.stringify(e.value)), version: was + 1 }
    return { value: { isSet: true, version: was + 1 } as const }
  })
  const write = (key: string, value: unknown) => {
    held[key] = { value: JSON.parse(JSON.stringify(value)), version: (held[key]?.version ?? 0) + 1 }
  }
  const value = (key: string) => held[key]?.value
  return { write, value }
}

/** A finished process, as `$.process.run` resolves it. */
export const ran = (exitCode: number, stdout = '') =>
  ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

/** True for the mod's credential lookup: `python3 <root>/scripts/rulebook_mod_cli.py api-info --env <env>`. */
export const isApiInfo = (argv: readonly string[]) =>
  argv[0] === 'python3' && /\/scripts\/rulebook_mod_cli\.py$/.test(argv[1] ?? '') && argv[2] === 'api-info'

/**
 * MemHub beneath the companion: its REST API and the credential lookup. The
 * test's own `process.run` hook (for openers) calls `apiInfo` first. Returns
 * every request sent and how many times the key was resolved.
 */
export function restBeneath(on: On, rest: Rest = {}) {
  const gets: Sent[] = []
  const patches: Sent[] = []
  let resolutions = 0
  const apiInfo = (argv: readonly string[]) => {
    if (!isApiInfo(argv)) return undefined
    const bearers = rest.bearers ?? [BEARER]
    const bearer = bearers[Math.min(resolutions, bearers.length - 1)] ?? ''
    resolutions += 1
    const answer = bearer ? { base: BASE, bearer, studio: rest.studio ?? STUDIO } : {}
    return ran(0, `${JSON.stringify(answer)}\n`)
  }
  on('http.fetch', async (_, e) => {
    const init = e.init ?? {}
    const sent: Sent = { url: e.url, method: init.method ?? 'GET', headers: { ...(init.headers ?? {}) }, body: init.body }
    if (sent.method === 'PATCH') {
      patches.push(sent)
      await rest.gate
      const answers = rest.decide ?? [{ status: 200, text: JSON.stringify({ code: 0, data: { status: 'active' } }) }]
      const answer = answers[Math.min(patches.length - 1, answers.length - 1)]!
      if (answer === 'throw') throw new Error('network down')
      return { value: { status: answer.status, ok: answer.status < 300, headers: {}, text: answer.text } }
    }
    // the mod's other parts ask MemHub too (the book): not the companion's
    if (!sent.url.startsWith(LIST_URL)) return { value: { status: 404, ok: false, headers: {}, text: '{}' } }
    gets.push(sent)
    const got = typeof rest.list === 'function' ? rest.list() : rest.list ?? listed()
    if (got === 'throw') throw new Error('network down')
    const answer = typeof got === 'string' ? { status: 200, text: got } : got
    return { value: { status: answer.status, ok: answer.status < 300, headers: {}, text: answer.text } }
  })
  return { gets, patches, apiInfo, resolutions: () => resolutions }
}

/** A main-thread turn's end, as the engine raises it: the companion polls then. */
export const TURN_END = { answer: '', durationMs: 1, isAborted: false, turnId: 'turn-1', reason: 'answer' } as const

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** screen.ts encode(), undone: each 12-byte cell's code point, row by row, rows joined by newlines. */
export function textOf(cells: string, columns: number): string {
  const bytes: number[] = []
  const clean = cells.replace(/=+$/, '')
  for (let i = 0; i < clean.length; i += 4) {
    const n = [0, 1, 2, 3].map(k => B64.indexOf(clean[i + k] ?? 'A'))
    const v = (n[0]! << 18) | (n[1]! << 12) | (n[2]! << 6) | n[3]!
    bytes.push((v >> 16) & 255, (v >> 8) & 255, v & 255)
  }
  const chars: string[] = []
  for (let i = 0; i + 12 <= bytes.length; i += 12) {
    const cp = bytes[i]! | (bytes[i + 1]! << 8) | (bytes[i + 2]! << 16) | (bytes[i + 3]! << 24)
    chars.push(String.fromCodePoint(cp || 32))
  }
  const rows: string[] = []
  for (let i = 0; i < chars.length; i += columns) rows.push(chars.slice(i, i + columns).join(''))
  return rows.join('\n')
}
