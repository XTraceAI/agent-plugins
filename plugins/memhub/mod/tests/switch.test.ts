// The remote switch (ENG-1206): `GET /v1/plugin/compatibility` →
// `"mod_lanes": false` hands every Rulebook lane to the Python command hooks
// for the session. Read at boot before any claim, then every SWITCH_MS. A
// failed read keeps the current state; a true after a false does not re-claim.

import { describe, expect, test } from 'claude-code/testing'

import { COMPAT_PATH, COMPAT_TIMEOUT_MS, modLanesOf, originOf, SWITCH_MS, SWITCHED_OFF } from '../book'
import { RENEW_MS } from '../claims'
import { boot } from '../register'
import { BOOK, type Fetch, fakeCtx, fakeEngine, type FakeIo, fakeIo, lanesOf, reply, settle, toHook } from './fakes'

const BOOK_TEXT = JSON.stringify({ etag: '"v1"', fetched_at: '2026-10-06T00:00:00.000000+00:00', rules: [{ id: 'r1' }] })
const COMPAT_URL = `https://api.example.test${COMPAT_PATH}`

const compat = (data: Record<string, unknown>) =>
  reply(200, JSON.stringify({ code: 0, msg: 'ok', data: { operations_enforced: true, supported: true, ...data } }))

/** Every compatibility request answered by `answer`; anything else (the book refresh) 304. */
function switchIo(answer: (f: Fetch) => ReturnType<FakeIo['onFetch']>) {
  const io = fakeIo({ onFetch: f => (f.url === COMPAT_URL ? answer(f) : reply(304)) })
  io.files.set(BOOK, BOOK_TEXT)
  return io
}

async function started(io: FakeIo, pastStartup = true) {
  const engine = fakeEngine()
  const shell = await boot(io, fakeCtx(io), () => engine, { cwd: '/work', pastStartup, toHook })
  await shell.ready
  return { engine, shell }
}

const compatFetches = (io: FakeIo) => io.fetches.filter(f => f.url === COMPAT_URL)
/** Runs only the switch's timer (the other timers would fetch and renew too). */
async function switchTick(io: FakeIo) {
  for (const t of io.timers) if (!t.cancelled && t.ms === SWITCH_MS) t.fn()
  await settle()
}
const ALL = ['pre', 'post', 'prompt', 'session']
/** A promise and the function that settles it. */
function gate<T = void>() {
  let open!: (v: T) => void
  const p = new Promise<T>(r => (open = r))
  return { p, open }
}
const ODD = [
  ['mod_lanes is not a bool', () => compat({ mod_lanes: 'no' })],
  ['an error envelope', () => reply(200, JSON.stringify({ code: 1, msg: 'nope', data: { mod_lanes: false } }))],
] as const
const FAILS = [
  [
    'a network error',
    () => {
      throw new Error('$.http.fetch(https://api.example.test/v1/plugin/compatibility) failed: ECONNRESET')
    },
  ],
  [
    'a refused fetch',
    () => {
      throw new Error('plugin: $.http.fetch: refused: policy')
    },
  ],
  ['a 401 twice', () => reply(401)],
  ['a 503', () => reply(503)],
] as const

describe('the request mirrors plugin_compatibility.py', () => {
  test('the origin of the REST base, the bearer, Accept and the plugin version', async () => {
    const io = switchIo(() => compat({ mod_lanes: true }))
    await started(io)
    expect(compatFetches(io)[0]).toEqual({
      url: COMPAT_URL,
      method: 'GET',
      headers: { Authorization: 'Bearer tok', Accept: 'application/json', 'X-MemHub-Plugin-Version': '0.111.0' },
      body: undefined,
    })
  })

  test('the version header is always sent: "unknown" when the manifest has none, as plugin_version.py says', async () => {
    const io = switchIo(() => compat({ mod_lanes: true }))
    io.files.delete('/plugin/.claude-plugin/plugin.json')
    await started(io)
    expect(compatFetches(io)[0]!.headers['X-MemHub-Plugin-Version']).toBe('unknown')
  })

  test('originOf keeps scheme://netloc only', () => {
    expect(originOf('https://api.example.test/mcp/x?y')).toBe('https://api.example.test')
    expect(originOf('http://127.0.0.1:8000')).toBe('http://127.0.0.1:8000')
    expect(originOf('nope')).toBeUndefined()
  })

  test('modLanesOf: false is off; true or absent is on; anything else keeps the state', () => {
    expect(modLanesOf(JSON.stringify({ code: 0, data: { mod_lanes: false } }))).toBe('off')
    expect(modLanesOf(JSON.stringify({ code: 0, data: { mod_lanes: true } }))).toBe('on')
    expect(modLanesOf(JSON.stringify({ code: 0, data: { operations_enforced: true } }))).toBe('on')
    expect(modLanesOf(JSON.stringify({ mod_lanes: false }))).toBe('off')
    expect(modLanesOf(JSON.stringify({ code: 0, data: { mod_lanes: 'no' } }))).toBe('unknown')
    expect(modLanesOf(JSON.stringify({ code: 1, data: { mod_lanes: false } }))).toBe('unknown')
    expect(modLanesOf('not json')).toBe('unknown')
  })
})

describe('at boot', () => {
  test('false: nothing is claimed, ever, and the person is told', async () => {
    const io = switchIo(() => compat({ mod_lanes: false }))
    const { engine, shell } = await started(io, false)
    expect(compatFetches(io)).toHaveLength(1)
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.envWrites.every(w => w.value === undefined)).toBe(true)
    expect(io.statuses.at(-1)).toBe(SWITCHED_OFF)
    expect(engine.calls.session).toEqual([])
    // The first prompt claims `session` in a fresh process: not when switched off.
    await shell.firstPrompt()
    expect(shell.claims.list()).toEqual([])
    // No renewal, no book refresh, no switch timer left running.
    expect(io.timers.filter(t => !t.cancelled)).toEqual([])
  })

  test('the field absent (an older backend): every lane is claimed, as before', async () => {
    const io = switchIo(() => reply(200, JSON.stringify({ code: 0, data: { operations_enforced: false } })))
    const { shell } = await started(io)
    expect(compatFetches(io)).toHaveLength(1)
    expect(shell.claims.list()).toEqual(ALL)
    expect(lanesOf(io)).toEqual(ALL)
    expect(io.statuses).not.toContain(SWITCHED_OFF)
  })

  for (const [what, answer] of [...FAILS, ...ODD]) {
    test(`${what}: the switch was asked, and the default stands — every lane claimed`, async () => {
      const io = switchIo(answer)
      const { shell } = await started(io)
      expect(compatFetches(io).length).toBeGreaterThan(0)
      expect(shell.claims.list()).toEqual(ALL)
      expect(io.statuses).not.toContain(SWITCHED_OFF)
    })
  }

  test('a 401 drops the cached credential and asks once more', async () => {
    let n = 0
    const io = switchIo(() => (++n === 1 ? reply(401) : compat({ mod_lanes: false })))
    const { shell } = await started(io)
    expect(compatFetches(io)).toHaveLength(2)
    expect(shell.claims.list()).toEqual([])
  })

  test('a fetch that never answers times out after COMPAT_TIMEOUT_MS: the default stands', async () => {
    const io = switchIo(() => new Promise(() => undefined))
    const slept: number[] = []
    io.sleep = async ms => {
      slept.push(ms)
    }
    const { shell } = await started(io)
    expect(compatFetches(io)).toHaveLength(1)
    expect(slept).toContain(COMPAT_TIMEOUT_MS)
    expect(shell.claims.list()).toEqual(ALL)
  })
})

describe('mid-session', () => {
  test('false releases every lane for the session, says so, and stops the book', async () => {
    let on = true
    const io = switchIo(() => compat({ mod_lanes: on }))
    const { shell } = await started(io)
    expect(io.timers.filter(t => t.ms === SWITCH_MS)).toHaveLength(1)
    expect(shell.claims.list()).toEqual(ALL)
    on = false
    await switchTick(io)
    expect(compatFetches(io)).toHaveLength(2)
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(lanesOf(io)).toEqual([])
    expect(io.statuses.at(-1)).toBe(SWITCHED_OFF)
    // The book's refresh and the switch stop; the renewal timer stays and writes nothing.
    expect(io.timers.filter(t => !t.cancelled).map(t => t.ms)).toEqual([RENEW_MS])
    // A renewal or a recovery never brings a lane back.
    await shell.claims.renew()
    await shell.claims.recover('pre')
    expect(shell.claims.list()).toEqual([])
  })

  for (const [what, answer] of [...FAILS, ...ODD]) {
    test(`${what} on a tick: the switch was asked, and the lanes stay claimed`, async () => {
      let first = true
      const io = switchIo(() => {
        if (first) {
          first = false
          return compat({ mod_lanes: true })
        }
        return answer()
      })
      const { shell } = await started(io)
      const before = compatFetches(io).length
      await switchTick(io)
      expect(compatFetches(io).length).toBeGreaterThan(before)
      expect(shell.claims.list()).toEqual(ALL)
      expect(io.envVars.staging).toBeDefined()
      expect(io.statuses).not.toContain(SWITCHED_OFF)
    })
  }

  test('once off, a failed read changes nothing: still released, still said', async () => {
    const io = switchIo(() => compat({ mod_lanes: false }))
    const { shell } = await started(io)
    io.onFetch = () => {
      throw new Error('$.http.fetch(x) failed: ENOTFOUND')
    }
    const before = io.fetches.length
    await shell.remote!.poll()
    expect(io.fetches.length).toBe(before + 1)
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(SWITCHED_OFF)
  })

  test('true after false does not re-claim, whatever then asks for a lane', async () => {
    let on = true
    const io = switchIo(() => compat({ mod_lanes: on }))
    const { shell } = await started(io)
    on = false
    await switchTick(io)
    expect(shell.claims.list()).toEqual([])
    // The timer is gone: a tick reads nothing.
    const afterOff = compatFetches(io).length
    await switchTick(io)
    expect(compatFetches(io).length).toBe(afterOff)
    // A read that does happen, and says true, claims nothing back.
    on = true
    await shell.remote!.poll()
    expect(compatFetches(io).length).toBe(afterOff + 1)
    await shell.firstPrompt()
    await shell.claims.claim(ALL as never)
    await shell.claims.recover('pre')
    await shell.claims.renew()
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
  })

  test('a hung read times out, so the next tick reads again (an overlapping tick is skipped)', async () => {
    let n = 0
    const io = switchIo(() => (++n === 1 ? compat({ mod_lanes: true }) : n === 2 ? new Promise(() => undefined) : compat({ mod_lanes: false })))
    const { shell } = await started(io)
    const wake = gate()
    io.sleep = () => wake.p
    const first = shell.remote!.poll()
    await settle()
    expect(compatFetches(io)).toHaveLength(2)
    await shell.remote!.poll() // in flight: skipped
    expect(compatFetches(io)).toHaveLength(2)
    wake.open()
    await first
    expect(shell.claims.list()).toEqual(ALL)
    await shell.remote!.poll()
    expect(compatFetches(io)).toHaveLength(3)
    expect(shell.claims.list()).toEqual([])
  })

  test('an off during an in-flight lease renewal: the release wins, the variable ends unset', async () => {
    let on = true
    const io = switchIo(() => compat({ mod_lanes: on }))
    const { shell } = await started(io)
    const held = gate()
    const set = io.setLanesVar
    let holding = true
    io.setLanesVar = async (env, value) => {
      if (holding) {
        holding = false
        await held.p
      }
      return set(env, value)
    }
    const renewal = shell.claims.renew()
    await settle()
    on = false
    const off = shell.remote!.poll()
    await settle()
    expect(shell.claims.has('pre')).toBe(false) // released at once, before any write lands
    held.open()
    await Promise.all([renewal, off])
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(lanesOf(io)).toEqual([])
  })

  test('an off during an in-flight book refresh: the refresh lands, no lane comes back', async () => {
    let on = true
    const book = gate<ReturnType<typeof reply>>()
    const io = fakeIo({
      onFetch: f => (f.url === COMPAT_URL ? compat({ mod_lanes: on }) : book.p),
    })
    io.files.set(BOOK, BOOK_TEXT)
    const { engine, shell } = await started(io)
    const refresh = shell.book!.refresh()
    await settle()
    on = false
    await shell.remote!.poll()
    expect(shell.claims.list()).toEqual([])
    book.open(reply(200, JSON.stringify({ code: 0, data: { rules: [{ id: 'r9' }] } }), { etag: '"v9"' }))
    expect(await refresh).toBe('updated')
    expect(engine.books.at(-1)!.map(r => r.id)).toEqual(['r9'])
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(SWITCHED_OFF)
  })

  test('a release that fails is said in the debug log', async () => {
    let on = true
    const io = switchIo(() => compat({ mod_lanes: on }))
    const { shell } = await started(io)
    io.setLanesVar = async () => {
      throw new Error('env write refused')
    }
    on = false
    await shell.remote!.poll()
    expect(shell.claims.has('pre')).toBe(false)
    expect(io.debugs.some(d => d.includes('remote switch release failed') && d.includes('env write refused'))).toBe(true)
  })
})
