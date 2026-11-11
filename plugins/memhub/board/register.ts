// The agent message board's receiving side (MemHub-Backend
// docs/specs/agent-message-board-spec.md §5): a teammate's agent sent this
// person's agent a message through a shared brain; one of their sessions
// shows it above the prompt with Run / Dismiss, and Run makes it a prompt.
//
// Exactly one session acts: Run claims the message on the server first
// (`POST /v2/agent-messages/{id}/claim`, a compare-and-swap with a 120 s
// lease), and only the winner submits. The claim is renewed while the
// prompt waits for the session to go idle, and the message is marked done
// once the turn has started. Dismiss marks it done, which clears it from
// every session.
//
// MemHub calls go over `$.http.fetch` with the mod's `ctx.api()` (the plugin's
// personal access key), as the companion's proposal poll does; every call
// settles, and a failure only leaves the band as it was. Nothing is shown
// where MemHub is inactive in the session's repository (ctx.active()).
//
// Registered before the companion (mod/register.ts), so its band is outer:
// it draws its row and then the companion's band beneath it.

import type { EngineInterface, On, Timer } from 'claude-code'

import type { Ctx } from '../mod/ctx'
import { headersOf, isApi, versionOfManifest } from '../companion/feed'
import {
  claimOf, claimPath, donePath, forSession, isFree, LIST_PATH, lineOf, MAX_PAGES, type Message, pageAfter, pageOf,
  POLL_MS, promptOf, RENEW_MS,
} from './board'

/** The second poll after setup (see setUp). */
const FIRST_RETRY_MS = 5_000

type State = {
  isSetUp: boolean
  session: string
  /** The messages this session shows, oldest first. */
  messages: Message[]
  /** id → what the band says instead of its buttons ('taking…', 'taken by another session', …). */
  notes: Map<string, string>
  /** Ids this session ran or dismissed: never shown again, whatever a stale poll says. */
  gone: Set<string>
  isPolling: boolean
  pollTimer: Timer | null
}

// A distinct registration: the shell and the companion already hook
// turn.complete, one of them bare, and a module may hook an event bare only once.
const BOARD_TURN_DONE = { reason: /^/ } as const

export function register(on: On, ctx: Ctx) {
  const s: State = {
    isSetUp: false,
    session: '',
    messages: [],
    notes: new Map(),
    gone: new Set(),
    isPolling: false,
    pollTimer: null,
  }

  // A main turn's end is a natural moment to look (and the first one sets up).
  on('turn.complete', BOARD_TURN_DONE, async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined) {
      if (!s.isSetUp) setUp($, s, ctx)
      else void poll($, s, ctx)
    }
    return result
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!s.isSetUp) setUp($, s, ctx)
    const m = s.messages[0]
    if (!m || e.props.hasSurvey || (e.surface !== 'terminal' && e.surface !== 'desktop')) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const more = s.messages.length > 1 ? ` (+${s.messages.length - 1} more)` : ''
    const note = s.notes.get(m.id)
    const row = Box({
      key: 'agent-message',
      flexDirection: 'row',
      gap: 1,
      children: [
        Text({ wrap: 'truncate-end', children: lineOf(m, 120) + more }),
        ...(note
          ? [Text({ dimColor: true, children: note })]
          : [
              Button({ key: 'run', hotkey: 'r', label: 'Run', onPress: () => void run($, s, ctx, m) }),
              Button({ key: 'dismiss', hotkey: 'd', label: 'Dismiss', onPress: () => void dismiss($, s, ctx, m) }),
            ]),
      ],
    })
    const below = await next(e)
    return below ? Box({ flexDirection: 'column', children: [row, below] }) : row
  }).catch(($, e, next) => next(e))
}

/**
 * Once a session: learn its id, then poll now, again shortly, and on the clock.
 * The first draw comes before the shell's session.start has bound ctx (live:
 * that poll had no key to send and the band waited a full minute), so a second
 * poll a few seconds in shows a message already waiting.
 */
function setUp($: EngineInterface, s: State, ctx: Ctx) {
  if (s.isSetUp) return
  s.isSetUp = true
  void $.session.id().then(id => {
    s.session = id
    s.pollTimer ??= $.clock.every(POLL_MS, () => void poll($, s, ctx))
    void poll($, s, ctx)
    $.clock.after(FIRST_RETRY_MS, () => void poll($, s, ctx))
  }, () => undefined)
}

// ── MemHub over `$.http.fetch` ──────────────────────────────────────────────
// In this file, beside the hooks: `claude plugin validate` follows `$` into no
// imported function (companion/register.ts says the same of its own).

type Sent = { status: number; text: string }

let manifestVersion: Promise<string> | undefined

/** One request with the plugin's key; a 401 drops the cached key and tries once more. null: no key or no answer. */
async function send($: EngineInterface, ctx: Ctx, path: string, method = 'GET', body?: unknown): Promise<Sent | null> {
  manifestVersion ??= $.fs
    .read(`${$.plugin.root.replace(/\/\.claude-plugin\/?$/, '')}/.claude-plugin/plugin.json`)
    .then(versionOfManifest, () => '')
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const api = await ctx.api().then(a => (isApi(a) ? a : undefined), () => undefined)
    if (!api) return null
    const headers = headersOf(api, await manifestVersion, body !== undefined)
    const init = body === undefined ? { method, headers } : { method, headers, body: JSON.stringify(body) }
    const res = await $.http.fetch(`${api.base.replace(/\/+$/, '')}${path}`, init).catch(() => null)
    if (!res) return null
    if (res.status === 401 && attempt === 0) {
      ctx.forgetApi()
      continue
    }
    return { status: res.status, text: typeof res.text === 'string' ? res.text : '' }
  }
  return null
}

/** Read every page of the caller's unread messages, keep this checkout's, and redraw if that changed. */
async function poll($: EngineInterface, s: State, ctx: Ctx) {
  if (s.isPolling || !s.session) return
  s.isPolling = true
  try {
    const active = await ctx.active().catch(() => undefined)
    if (active && !active.enabled) return setMessages($, s, [])
    const all: Message[] = []
    let path = LIST_PATH
    for (let n = 0; n < MAX_PAGES; n += 1) {
      const sent = await send($, ctx, path)
      if (!sent || sent.status < 200 || sent.status >= 300) return // no answer: keep what is shown
      const page = pageOf(sent.text)
      if (!page) return
      all.push(...page.messages)
      if (!page.nextCursor) break
      path = pageAfter(page.nextCursor)
    }
    const now = await $.clock.now()
    const mine = forSession(all, active?.identity ?? '', active?.repo ?? '').filter(m => !s.gone.has(m.id))
    // A message another live session holds stays listed with a note, until it is done.
    for (const m of mine) {
      if (!isFree(m, s.session, now) && !s.notes.has(m.id)) s.notes.set(m.id, 'taken by another session')
      if (isFree(m, s.session, now) && s.notes.get(m.id) === 'taken by another session') s.notes.delete(m.id)
    }
    setMessages($, s, mine)
  } finally {
    s.isPolling = false
  }
}

function setMessages($: EngineInterface, s: State, next: Message[]) {
  const key = (ms: Message[]) => ms.map(m => `${m.id}:${s.notes.get(m.id) ?? ''}`).join(',')
  const before = key(s.messages)
  s.messages = next
  if (key(next) !== before) $.ui.invalidate('ui.render')
}

function note($: EngineInterface, s: State, m: Message, text: string | null) {
  if (text === null) s.notes.delete(m.id)
  else s.notes.set(m.id, text)
  $.ui.invalidate('ui.render')
}

function drop($: EngineInterface, s: State, m: Message) {
  s.gone.add(m.id)
  s.notes.delete(m.id)
  setMessages($, s, s.messages.filter(x => x.id !== m.id))
}

/** Run: claim it; only on a win submit it as a prompt, renewing the claim until the turn starts; then mark it done. */
async function run($: EngineInterface, s: State, ctx: Ctx, m: Message) {
  if (s.notes.has(m.id)) return
  note($, s, m, 'taking…')
  const claim = async () => {
    const sent = await send($, ctx, claimPath(m.id), 'POST', { session_id: s.session })
    return sent ? claimOf(sent.status, sent.text) : null
  }
  const got = await claim()
  if (!got) return note($, s, m, null) // no answer: the buttons come back
  if (!got.claimed) {
    if (got.reason === 'claimed') return note($, s, m, 'taken by another session')
    return drop($, s, m) // already done, expired, or not ours: gone
  }
  note($, s, m, 'queued — runs when this session is idle')
  const renew = $.clock.every(RENEW_MS, () => void claim())
  try {
    await $.prompt.submit({ text: promptOf(m) })
  } finally {
    renew.cancel()
  }
  await send($, ctx, donePath(m.id), 'POST')
  drop($, s, m)
}

/** Dismiss: mark it done, which takes it off every session's band. */
async function dismiss($: EngineInterface, s: State, ctx: Ctx, m: Message) {
  if (s.notes.has(m.id)) return
  note($, s, m, 'dismissing…')
  const sent = await send($, ctx, donePath(m.id), 'POST')
  if (sent && sent.status >= 200 && sent.status < 300) drop($, s, m)
  else note($, s, m, null)
}
