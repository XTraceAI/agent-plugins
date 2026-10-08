// What the companion reads from MemHub and from the mod, as plain data: the
// fires the mod's act side writes to `$.state` (fires), the proposed rules
// the poll writes there (proposals), and the REST answers
// the poll and the Activate / Reject buttons get back. Pure: no `$`, so every
// rule here is a unit test away (feed.test.ts).
//
// The REST shapes mirror scripts/rule_decide.py byte for byte (it stays, for
// the skills): `GET /v1/team/rulebook/rules?status=eq.proposed&author=eq.xtrace
// &order=created_at.desc`, kept when `source_ref` starts with `<session>#`, and
// `PATCH /v1/team/rulebook/rules/<id>` with `{"status": "active"|"dismissed"}`.

import { audienceOf, type Api } from '../mod/ctx'
import type { FireNote, LaneName, ProposalNote } from '../types'
import type { Decision, Proposed } from './proposed'

/** One fire's identity: the same note read twice is one announcement. */
export const fireKey = (f: FireNote) => `${f.at}|${f.ruleId}|${f.isBlocked ? 1 : 0}|${f.rule}`

/** `$.state` fires as written, or [] for anything that is not a list of notes. */
export function firesOf(value: unknown): FireNote[] {
  if (!Array.isArray(value)) return []
  return value.filter(
    (f): f is FireNote =>
      !!f && typeof f === 'object' && typeof f.rule === 'string' && f.rule.trim() !== '' &&
      typeof f.isBlocked === 'boolean' && typeof f.at === 'number',
  )
}

/**
 * The notes in `fires` not in `seen`, oldest first, and the keys to remember
 * next: every key the list holds now. The writer keeps the newest 50 and only
 * appends, so a key that left the list never comes back and need not be kept.
 */
export function freshFires(fires: readonly FireNote[], seen: ReadonlySet<string>): { fresh: FireNote[]; seen: Set<string> } {
  const keys = new Set<string>()
  const fresh: FireNote[] = []
  for (const f of fires) {
    const key = fireKey(f)
    if (!keys.has(key) && !seen.has(key)) fresh.push(f)
    keys.add(key)
  }
  return { fresh, seen: keys }
}

/** `$.state` lanes as written, or [] for anything else. */
export function lanesOf(value: unknown): LaneName[] {
  return Array.isArray(value) ? value.filter((l): l is LaneName => typeof l === 'string') : []
}

/**
 * Whether the classic hook for `lane` is the fallback that may announce: only
 * while the mod has not claimed that lane. A claimed lane's fires come through
 * `$.state` fires; reading the classic answer too would say each one twice.
 */
export const isClassicLane = (lanes: readonly LaneName[], lane: 'pre' | 'post') => !lanes.includes(lane)

/** `$.state` proposals as written, or null when never written or not a list. */
export function proposalsOf(value: unknown): ProposalNote[] | null {
  if (!Array.isArray(value)) return null
  return value.filter(
    (p): p is ProposalNote =>
      !!p && typeof p === 'object' && typeof p.ruleId === 'string' && p.ruleId !== '' &&
      typeof p.title === 'string' && typeof p.at === 'number',
  )
}

/** The ids in a list of proposals, in order: what changed between two reads. */
export const proposalsKey = (notes: readonly ProposalNote[]) => notes.map(p => p.ruleId).join(',')

/**
 * The poll's answer, `GET …/rules?status=eq.proposed&author=eq.xtrace…`, as
 * this session's waiting rules — rule_decide.py `proposed()`'s filter: a row
 * whose `source_ref` starts with `<session>#`. null when the reply is not the
 * REST envelope with a `data.rules` list: "could not ask" is not "nothing is
 * waiting", and only a real list may take an ask off the band.
 */
export function listedRules(text: string, session: string, at: number): ProposalNote[] | null {
  let payload: unknown
  try {
    payload = JSON.parse(text || '{}')
  } catch {
    return null
  }
  const data = payload && typeof payload === 'object' ? (payload as { data?: unknown }).data : undefined
  const rules = data && typeof data === 'object' ? (data as { rules?: unknown }).rules : undefined
  if (!Array.isArray(rules)) return null
  if (!session) return []
  return rules
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object' && !Array.isArray(r))
    .filter(r => String(r.source_ref ?? '').startsWith(`${session}#`))
    .map(r => {
      const note: ProposalNote = { ruleId: String(r.rule_id ?? ''), title: String(r.title ?? '').trim(), at }
      if (typeof r.rulebook_id === 'string' && r.rulebook_id) note.rulebookId = r.rulebook_id
      const audience = audienceOf(r)
      if (audience) note.audience = audience
      return note
    })
    .filter(p => p.ruleId)
}

/**
 * `$.state` proposals after a poll: the server's whole list, each note keeping
 * the `at` it was first written with, plus any note written since the poll
 * began (`since`) that the list does not name yet — a note another writer
 * pushed while the poll was in flight, which the poll's list may predate.
 * Everything else the list no longer names was decided, so it goes.
 */
export function mergedProposals(current: readonly ProposalNote[], listed: readonly ProposalNote[], since: number): ProposalNote[] {
  const byId = new Map(current.map(p => [p.ruleId, p]))
  const named = new Set(listed.map(p => p.ruleId))
  const kept = listed.map(p => {
    const was = byId.get(p.ruleId)
    if (!was) return p
    const rulebookId = p.rulebookId ?? was.rulebookId
    const audience = p.audience ?? was.audience
    // no `rulebookId: undefined`: $.state takes JSON data
    return { ...p, at: was.at, ...(rulebookId ? { rulebookId } : {}), ...(audience ? { audience } : {}) }
  })
  const pushed = current.filter(p => !named.has(p.ruleId) && p.at >= since)
  return [...kept, ...pushed]
}

/** A note as the band asks it: the env is the API's the companion reaches. */
export const proposedOfNote = (p: ProposalNote, env: string): Proposed =>
  ({ title: p.title, ruleId: p.ruleId, env, ...(p.audience ? { audience: p.audience } : {}) })

/** The env as the band's words name it (proposed.ts decisionSaid): the mod's ctx.env, spelled as harness_stop does. */
export const envName = (env: 'staging' | 'prod') => (env === 'prod' ? 'production' : 'staging')

/**
 * harness_stop.rule_url(): where the rule opens in MemHub Studio, or, with no
 * id, the rulebook page itself (the demo's). `studio` is the web app api-info
 * pairs with the API (plugin_onboarding._ORIGINS); '' when it pairs none — a
 * link into another MemHub would open a rule that is not there.
 */
export function studioUrl(studio: string, ruleId: string): string {
  const origin = studio.replace(/\/+$/, '')
  if (!/^https:\/\/[^/?#\s]+$/.test(origin)) return ''
  if (ruleId && !UUID.test(ruleId)) return ''
  return ruleId ? `${origin}/studio/rulebook?open=${ruleId}` : `${origin}/studio/rulebook`
}

/** rule_decide.py's `_UUID`: an id the PATCH is sent for. */
export const UUID = /^[0-9a-fA-F-]{36}$/

/** rule_decide.py's `_STATUS`: the only two ways out of `proposed`. */
export const STATUS_OF = { activate: 'active', reject: 'dismissed' } as const

/**
 * rule_decide.py `decide()`'s outcome, from the PATCH's status and body: 403
 * forbidden, 404 gone, 400/409 decided, any other failure an error; a 200
 * whose envelope `code` is not 0 is a failure the transport reported as
 * success; otherwise the rule's new status.
 */
export function decisionOf(status: number, text: string, action: keyof typeof STATUS_OF): Decision {
  let payload: unknown
  try {
    payload = JSON.parse(text || '{}')
  } catch {
    payload = undefined
  }
  const body = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : undefined
  if (status < 200 || status >= 300) {
    const msg = String(body?.msg ?? '').slice(0, 200)
    const outcome = ({ 403: 'forbidden', 404: 'gone', 400: 'decided', 409: 'decided' } as Record<number, string>)[status] ?? 'error'
    return { outcome, msg: msg || `HTTP ${status}` }
  }
  if (payload === undefined) return { outcome: 'error', msg: 'unreadable reply' }
  if (body && body.code !== undefined && body.code !== null && body.code !== 0) {
    return { outcome: 'error', msg: String(body.msg ?? JSON.stringify(body)).slice(0, 200) }
  }
  const data = body?.data
  const now = data && typeof data === 'object' ? (data as { status?: unknown }).status : undefined
  return { outcome: typeof now === 'string' && now ? now : STATUS_OF[action], msg: '' }
}

/** A usable credential from ctx.api(): a base and a bearer. */
export const isApi = (a: unknown): a is Api =>
  !!a && typeof a === 'object' && typeof (a as Api).base === 'string' && (a as Api).base !== '' &&
  typeof (a as Api).bearer === 'string' && (a as Api).bearer !== ''

/** The manifest's version, for `X-MemHub-Plugin-Version`; '' when unreadable. */
export function versionOfManifest(text: unknown): string {
  try {
    const v = (JSON.parse(String(text)) as { version?: unknown }).version
    return typeof v === 'string' ? v : ''
  } catch {
    return ''
  }
}

/** The headers rule_decide.py sends: its key, plugin_version.request_headers(), and a JSON body's type. */
export function headersOf(api: Api, version: string, hasBody: boolean): Record<string, string> {
  return {
    Authorization: `Bearer ${api.bearer}`,
    ...(version ? { 'X-MemHub-Plugin-Version': version } : {}),
    ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
  }
}

/** rule_decide.py `proposed()`'s request. */
export const PROPOSED_PATH = '/v1/team/rulebook/rules?status=eq.proposed&author=eq.xtrace&order=created_at.desc'

/** rule_decide.py `decide()`'s request path. */
export const rulePath = (ruleId: string) => `/v1/team/rulebook/rules/${ruleId}`
