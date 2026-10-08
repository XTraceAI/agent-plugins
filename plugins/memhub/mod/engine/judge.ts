// The rule judge (rule-judge-spec §5): a port of rulebook_hook.py judgeable,
// judge_backed_off / _judge_back_off, ask_judge, judge_fires and unfire.
//
// Differences from the Python, all forced by the mod's I/O:
//   * the turn comes from `$.session.messages({ as: "api" })` (EngineIO.turn),
//     not the transcript file; its `turn_id` is EngineIO's per-turn id, not
//     the transcript uuid of the human message;
//   * `$.http.fetch` has no timeout, so the call races JUDGE_TIMEOUT_S here and
//     a timeout is "nothing judged", exactly as Python's client timeout is;
//   * no `.last_error` breadcrumb is written (a health line is the shell's).

import type { EngineIO, HookRule } from './index'
import { MAX_ENTRIES as TURN_MAX_ENTRIES, readTurn } from './probes'
import { pyExec } from './py'
import { pyStr } from './rules'
import { judgeMatchedOn, judgeRedact, redactedTokens, scrubTokens, shellOnly } from './shell'
import { RESULT_WINDOW_CHARS, cpLen, cpSlice, isDict } from './rules'
import { join } from './shell'

export const JUDGE_MAX_RULES = 10
export const JUDGE_BACKOFF_S = 600
const JUDGE_CALL_CHARS = 600
const JUDGE_MATCHED_CHARS = 200
const JUDGE_ID_CHARS = 200
const JUDGE_RESULT_CHARS = 3000  // the output an output rule matched; the server takes 4000
const VERDICTS = new Set(['fit', 'no_fit', 'timeout', 'failed', 'no_rule', 'disabled'])
const JUDGED_ON = new Set(['bash', 'edit', 'read', 'result', 'anchor'])

export type Verdict = { verdict: string; show: boolean; p_fit: number | null }
export type JudgeState = { judge?: Record<string, Record<string, Verdict>> }

/** `_timeout(1.5)`: MEMHUB_RULEBOOK_TIMEOUT_S when it is a positive number. */
export function judgeTimeoutMs(raw: string | undefined): number {
  const v = Number((raw ?? '').trim())
  return (raw ?? '').trim() !== '' && Number.isFinite(v) && v > 0 ? v * 1000 : 1500
}

/** `judgeable(rule)`: a matcher or anchor rule whose id is a UUID. */
export function judgeable(rule: HookRule): boolean {
  if (!JUDGED_ON.has(rule.on)) return false
  const hex = pyStr(rule.id ?? null).replace(/urn:/g, '').replace(/uuid:/g, '').replace(/^[{}]+|[{}]+$/g, '').replace(/-/g, '')
  return /^[0-9a-fA-F]{32}$/.test(hex)
}

const backoffPath = (base: string) => join(base, 'judge.json')

export async function judgeBackedOff(io: EngineIO, base: string): Promise<boolean> {
  try {
    const t = await io.readText(backoffPath(base))
    if (t === undefined) return false
    const until = (JSON.parse(t) as Record<string, unknown>).judge_disabled_until
    return typeof until === 'number' && io.now() / 1000 < until
  } catch {
    return false
  }
}

async function backOff(io: EngineIO, base: string): Promise<void> {
  await io.writeText(backoffPath(base), JSON.stringify({ judge_disabled_until: io.now() / 1000 + JUDGE_BACKOFF_S })).catch(() => undefined)
}

/** `p`, or undefined once `ms` has passed (`$.clock.sleep`: the mod has no setTimeout). */
const withTimeout = <T>(io: EngineIO, p: Promise<T>, ms: number): Promise<T | undefined> =>
  Promise.race([p.catch(() => undefined), io.sleep(ms).then(() => undefined, () => undefined)])

/** `ask_judge(body)` → [verdicts by rule id, enforce], or null on any client failure. */
export async function askJudge(
  io: EngineIO,
  base: string,
  body: Record<string, unknown>,
  timeoutMs: number,
): Promise<[Record<string, Verdict>, boolean] | null> {
  try {
    const reply = await withTimeout(io, io.judge(body), timeoutMs)
    if (!reply) return null
    if (reply.status === 404) {
      await backOff(io, base)
      return null
    }
    const data = reply.data
    if (reply.status !== 200 || !isDict(data) || typeof data.enforce !== 'boolean' || !Array.isArray(data.verdicts)) return null
    const asked = new Set((body.rules as { rule_id: string }[]).map((r) => r.rule_id))
    const out: Record<string, Verdict> = {}
    for (const v of data.verdicts as unknown[]) {
      const p = isDict(v) ? v.p_fit : undefined
      if (!isDict(v) || typeof v.verdict !== 'string' || !VERDICTS.has(v.verdict) || typeof v.show !== 'boolean' ||
          !(p === undefined || p === null || (typeof p === 'number' && p >= 0 && p <= 1))) {
        return null
      }
      const rid = pyStr(v.rule_id ?? null)
      if (asked.has(rid)) out[rid] = { verdict: v.verdict, show: v.show, p_fit: (p as number | undefined) ?? null }
    }
    const vals = Object.values(out)
    if (vals.length && vals.every((v) => v.verdict === 'disabled')) await backOff(io, base)
    return [out, data.enforce]
  } catch {
    return null
  }
}

export type JudgeArgs = {
  repo: string
  session: string
  tool: string
  cmd: string
  fp: string
  root: string
  firedNow: readonly HookRule[]
  firedOn: ReadonlyMap<string, { fp?: string; tool?: string; rtext?: string }>
  enabled: boolean
  timeoutMs: number
  base: string
}

/**
 * `judge_result_excerpt(rule, rtext)`: the part of a tool's output an OUTPUT
 * rule fired on, centred on the rule's own match (`m`, over the same head and
 * tail spans `evaluate` scans), the tail when nothing matches. Offsets are
 * code points, as Python's. Not redacted here: the caller redacts.
 */
export function judgeResultExcerpt(rule: HookRule, rtext: string): string {
  if (!rtext) return ''
  const half = Math.floor(JUDGE_RESULT_CHARS / 2)
  const n = cpLen(rtext)
  const spans: [number, string][] = n <= 2 * RESULT_WINDOW_CHARS
    ? [[0, rtext]]
    : [[0, cpSlice(rtext, 0, RESULT_WINDOW_CHARS)], [n - RESULT_WINDOW_CHARS, cpSlice(rtext, -RESULT_WINDOW_CHARS)]]
  const rx = (rule as { rx?: unknown }).rx
  for (const [offset, span] of spans) {
    let m: RegExpExecArray | null = null
    if (typeof rx === 'string' && rx) {
      try { m = pyExec(rx, span, 'm') } catch { m = null }
    }
    if (m) {
      const lo = Math.max(0, offset + cpLen(span.slice(0, m.index)) - half)
      return cpSlice(rtext, lo, lo + JUDGE_RESULT_CHARS)
    }
  }
  return cpSlice(rtext, -JUDGE_RESULT_CHARS)
}

/**
 * `judge_fires(st, …)` → [verdicts {rule id: {verdict, p_fit}}, held, fresh].
 * Never throws: any failure is "nothing judged".
 */
export async function judgeFires(
  io: EngineIO,
  st: JudgeState,
  a: JudgeArgs,
): Promise<[Map<string, { verdict: string; p_fit: number | null }>, Set<string>, Set<string>]> {
  const verdicts = new Map<string, { verdict: string; p_fit: number | null }>()
  const held = new Set<string>()
  const fresh = new Set<string>()
  try {
    if (!a.enabled) return [verdicts, held, fresh]
    const todo = a.firedNow.filter(judgeable)
    if (!todo.length) return [verdicts, held, fresh]
    const redact = (s: string) => judgeRedact(s, io.home)
    const got = await io.turn().catch(() => undefined)
    const turn = got
      ? readTurn(got.messages, got.id, redact)
      : { turn_id: '', person_request: '', turn: [] as { kind: string; text: string }[] }
    const turnId = cpSlice(pyStr(turn.turn_id || ''), 0, JUDGE_ID_CHARS)
    const cache = isDict(st.judge) ? st.judge : {}
    const knownRaw = turnId ? cache[turnId] : undefined
    const known: Record<string, Verdict> = isDict(knownRaw) ? { ...knownRaw } : {}
    const ask = todo.filter((r) => !(pyStr(r.id) in known)).slice(0, JUDGE_MAX_RULES)
    let answered: Record<string, Verdict> = {}
    if (ask.length && !(await judgeBackedOff(io, a.base))) {
      const handle = a.tool === 'Bash' && a.cmd ? shellOnly(a.cmd) : a.fp
      // An output rule fired on this call's result, which the turn does not
      // hold yet: hand the judge the part it fired on.
      const results = ask.filter((r) => r.on === 'result')
        .map((r) => ({ kind: 'result', text: judgeRedact(judgeResultExcerpt(r, a.firedOn.get(r.id)?.rtext ?? ''), io.home) }))
        .filter((e) => e.text).slice(0, 1)
      // The server refuses more than MAX_ENTRIES; the newest are kept.
      const prior = turn.turn || []
      const body: Record<string, unknown> = {
        repo: cpSlice(pyStr(a.repo || ''), 0, JUDGE_ID_CHARS),
        session_id: cpSlice(pyStr(a.session || ''), 0, JUDGE_ID_CHARS),
        turn_id: turnId,
        person_request: turn.person_request || '',
        turn: results.length ? [...prior.slice(results.length - TURN_MAX_ENTRIES), ...results] : prior,
        call: { tool: cpSlice(pyStr(a.tool || ''), 0, 200), text: cpSlice(judgeRedact(handle, io.home), 0, JUDGE_CALL_CHARS) },
        rules: ask.map((r) => ({
          rule_id: pyStr(r.id),
          matched_on: cpSlice(judgeRedact(judgeMatchedOn(r, a.firedOn.get(r.id) ?? null, handle, a.root), io.home), 0, JUDGE_MATCHED_CHARS),
        })),
      }
      const secrets = redactedTokens(handle, judgeRedact(handle, io.home))
      if (secrets.length) {
        body.person_request = scrubTokens(body.person_request as string, secrets)
        body.turn = (body.turn as unknown[]).map((e) =>
          isDict(e) ? { ...e, text: scrubTokens(typeof e.text === 'string' ? e.text : '', secrets) } : e)
      }
      const reply = await askJudge(io, a.base, body, a.timeoutMs)
      if (reply) {
        const [g, enforce] = reply
        answered = Object.fromEntries(Object.entries(g).map(([rid, v]) => [rid, { ...v, show: v.show || !enforce }]))
      }
    }
    for (const r of todo) {
      const rid = pyStr(r.id)
      const v = answered[rid] ?? known[rid]
      if (!isDict(v) || !VERDICTS.has(v.verdict)) continue
      verdicts.set(r.id, { verdict: v.verdict, p_fit: v.p_fit ?? null })
      if (v.show === false) {
        held.add(r.id)
        if (rid in answered) fresh.add(r.id)
      }
    }
    if (turnId) {
      for (const [rid, v] of Object.entries(answered)) if (v.verdict !== 'disabled') known[rid] = v
      st.judge = { [turnId]: known }
    }
    return [verdicts, held, fresh]
  } catch {
    return [new Map(), new Set(), new Set()]
  }
}

/** The per-session marks `unfire` restores, as they stood before the fire pass. */
export type Marks = { fired: string[]; counts: Record<string, number>; spec_pending: Record<string, unknown> }
export type FireState = {
  fired: string[]
  counts: Record<string, number>
  raw: Record<string, number>
  spec_pending: Record<string, unknown>
}

const count = (xs: readonly string[], k: string) => xs.reduce((n, x) => n + (x === k ? 1 : 0), 0)

/** `unfire(st, rule, key, before, reset_raw=)`: undo this call's marks for a held rule. */
export function unfire(st: FireState, rule: HookRule, key: string | undefined, before: Marks, resetRaw: boolean): void {
  const rid = rule.id
  if (key !== undefined && count(st.fired, key) > count(before.fired, key)) {
    const i = st.fired.lastIndexOf(key)
    if (i >= 0) st.fired.splice(i, 1)
  }
  if (rid in before.counts) st.counts[rid] = before.counts[rid]!
  else delete st.counts[rid]
  for (const k of Object.keys(st.spec_pending)) {
    if (k in before.spec_pending && JSON.stringify(st.spec_pending[k]) === JSON.stringify(before.spec_pending[k])) continue
    let mine = false
    try {
      mine = (JSON.parse(k) as unknown[])[0] === rid
    } catch {
      mine = false
    }
    if (!mine) continue
    if (k in before.spec_pending) st.spec_pending[k] = before.spec_pending[k]
    else delete st.spec_pending[k]
  }
  if (resetRaw) st.raw[rid] = 0
}
