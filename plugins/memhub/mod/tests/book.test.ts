// The book on the mod path (spec §4.1): load the cache Python reads, refresh
// it every minute with If-None-Match, 200 → file + engine, 304 → nothing,
// 426 → stop serving and let Python record the upgrade notice.

import { describe, expect, test } from 'claude-code/testing'

import {
  Book,
  FETCH_REFUSED,
  isFetchRefused,
  isoOf,
  isSecure,
  msOfIso,
  parseBook,
  quoteAll,
  REFRESH_GAVE_UP,
  REFRESH_MS,
  RELEASE_AFTER_FAILURES,
  shapeRules,
} from '../book'
import { Claims, StatusLine } from '../claims'
import { BOOK, fakeCtx, fakeEngine, type FakeIo, fakeIo, REPO, reply, ROOT, toHook } from './fakes'

const CACHED = JSON.stringify({ etag: '"v1"', fetched_at: '2026-10-06T00:00:00.000000+00:00', rules: [{ id: 'r1' }] })
const SERVED = { code: 0, msg: 'ok', data: { rules: [{ id: 'r1' }, { id: 'r2' }, { id: 'r2' }, { nope: true }] } }

async function loaded(onFetch?: FakeIo['onFetch'], cached = CACHED) {
  const io = fakeIo(onFetch ? { onFetch } : {})
  io.files.set(BOOK, cached)
  const engine = fakeEngine()
  const status = new StatusLine(io)
  const claims = new Claims(io, 'staging', status)
  const book = new Book(io, fakeCtx(io), engine, claims, status, { hookVersion: '0.111.0', toHook })
  expect(await book.load('/work')).toBe(true)
  await claims.claim(['pre', 'post', 'prompt', 'session'])
  return { io, engine, claims, book }
}

describe('load', () => {
  test('reads the book file `paths` names and hands its rows to the engine', async () => {
    const { io, engine } = await loaded()
    expect(io.runs[0]!.argv).toEqual(['python3', `${ROOT}/scripts/rulebook_mod_cli.py`, 'paths', '--env', 'staging', '--cwd', '/work'])
    expect(engine.books).toEqual([[{ id: 'r1', on: 'bash', mode: 'advise' }]])
    expect(io.fetches).toHaveLength(0) // a cached book needs no network at start
  })

  test('no cache: fetched once at load', async () => {
    const io = fakeIo({ onFetch: () => reply(200, JSON.stringify(SERVED), { etag: '"v2"' }) })
    const engine = fakeEngine()
    const status = new StatusLine(io)
    const book = new Book(io, fakeCtx(io), engine, new Claims(io, 'staging', status), status, { toHook })
    expect(await book.load('/work')).toBe(true)
    expect(engine.books[0]!.map(r => r.id)).toEqual(['r1', 'r2'])
  })

  test('a recorded upgrade notice: the mod does not serve (Python suspends and says why)', async () => {
    const io = fakeIo()
    io.files.set(BOOK, CACHED)
    io.files.set(`${BOOK}.upgrade`, '{"minimum_version":"9.9.9"}')
    const status = new StatusLine(io)
    const book = new Book(io, fakeCtx(io), fakeEngine(), new Claims(io, 'staging', status), status, { toHook })
    expect(await book.load('/work')).toBe(false)
  })
})

describe('refresh', () => {
  test('runs every minute', async () => {
    const { io, book } = await loaded()
    book.start()
    expect(io.timers.filter(t => t.ms === REFRESH_MS)).toHaveLength(1)
    await io.tick()
    expect(io.fetches).toHaveLength(1)
  })

  test('the request: view=hook, the repo, hook_version, bearer and If-None-Match', async () => {
    const { io, book } = await loaded()
    await book.refresh()
    expect(io.fetches[0]).toEqual({
      url: `https://api.example.test/v1/team/rulebook/rules?view=hook&repo=${quoteAll(REPO)}&hook_version=0.111.0`,
      method: 'GET',
      headers: {
        Authorization: 'Bearer tok',
        Accept: 'application/json',
        'X-MemHub-Plugin-Version': '0.111.0',
        'If-None-Match': '"v1"',
      },
      body: undefined,
    })
  })

  test('200: the engine gets the new book and the file Python reads is rewritten', async () => {
    const { io, engine, book } = await loaded(() => reply(200, JSON.stringify(SERVED), { etag: '"v2"' }))
    expect(await book.refresh()).toBe('updated')
    expect(engine.books.at(-1)!.map(r => r.id)).toEqual(['r1', 'r2'])
    const written = JSON.parse(io.files.get(BOOK)!)
    expect(written).toEqual({ etag: '"v2"', fetched_at: isoOf(1_700_000_000_000), rules: SERVED.data.rules })
    expect(parseBook(io.files.get(BOOK)!)).toBeDefined()
    // the next request carries the new ETag
    io.onFetch = () => reply(304)
    await book.refresh()
    expect(io.fetches.at(-1)!.headers['If-None-Match']).toBe('"v2"')
  })

  test('304: nothing is written and the engine keeps its book', async () => {
    const { io, engine, book } = await loaded(() => reply(304))
    expect(await book.refresh()).toBe('unchanged')
    expect(io.writes).toHaveLength(0)
    expect(engine.books).toHaveLength(1)
  })

  test('426: every lane goes back to Python, the person is told, Python records the notice, the timer stops', async () => {
    const body = JSON.stringify({ code: 426, data: { error_code: 'PLUGIN_UPGRADE_REQUIRED', minimum_version: '0.120.0', scope: 'rulebook_fetch' } })
    const { io, claims, book } = await loaded(() => reply(426, body))
    book.start()
    expect(await book.refresh()).toBe('upgrade')
    expect(claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe('MemHub: update the plugin (minimum 0.120.0) — team rules are paused')
    expect(io.runs.at(-1)!.argv).toEqual(['python3', `${ROOT}/scripts/rulebook_hook.py`, 'fetch', REPO])
    expect(io.timers.find(t => t.ms === REFRESH_MS)!.cancelled).toBe(true)
    expect(io.writes).toHaveLength(0)
    // released for the session: a later claim does not take the lanes back
    await claims.claim(['pre'])
    expect(claims.list()).toEqual([])
  })

  test('401: the credential is re-resolved once and the call retried', async () => {
    let n = 0
    const io = fakeIo({ onFetch: () => (n++ === 0 ? reply(401) : reply(304)) })
    io.files.set(BOOK, CACHED)
    let forgot = 0
    const ctx = { ...fakeCtx(io), forgetApi: () => void forgot++ }
    const status = new StatusLine(io)
    const book = new Book(io, ctx, fakeEngine(), new Claims(io, 'staging', status), status, { toHook })
    await book.load('/work')
    expect(await book.refresh()).toBe('unchanged')
    expect(forgot).toBe(1)
    expect(io.fetches).toHaveLength(2)
  })

  test('a server that keeps failing is said on the status line after three tries, cleared on success', async () => {
    const { io, book } = await loaded(() => reply(500))
    for (let i = 0; i < 2; i++) await book.refresh()
    expect(io.statuses.filter(Boolean)).toEqual([])
    await book.refresh()
    expect(io.statuses.at(-1)).toContain('team rules not refreshed')
    io.onFetch = () => reply(304)
    await book.refresh()
    expect(io.statuses.at(-1)).toBeUndefined()
  })

  test('a cleartext base never gets the bearer', async () => {
    expect(isSecure('https://api.memhub.xtrace.ai')).toBe(true)
    expect(isSecure('http://localhost:8000')).toBe(true)
    expect(isSecure('http://[::1]:8000')).toBe(true)
    expect(isSecure('http://api.memhub.xtrace.ai')).toBe(false)
  })
})

describe('the on-disk shape', () => {
  test('fetched_at is what Python `fromisoformat` reads: microseconds and an offset', () => {
    expect(isoOf(Date.UTC(2026, 9, 6, 1, 2, 3, 456))).toBe('2026-10-06T01:02:03.456000+00:00')
  })

  test('rows are shaped as _norm_rules does: unrunnable dropped, first id kept', () => {
    expect(shapeRules([{ id: 'a', on: 'edit' }, { id: 'a', on: 'bash' }, null, { x: 1 }], toHook).map(r => [r.id, r.on])).toEqual([['a', 'edit']])
  })

  test('the repo is quoted as urllib.parse.quote(safe="")', () => {
    expect(quoteAll("o/r (x)!*'")).toBe('o%2Fr%20%28x%29%21%2A%27')
  })
})

// What the engine rejects with (Claude Code 2.1.292): a refusal before any
// request, a hook's deny, and a request that went out and failed.
const POLICY = new Error('memhub-staging: $.http.fetch: refused: Network access from plugins is disabled by your organization')
const DENIED = new Error('memhub-staging: $.http.fetch: blocked by acme-guard')
const BLIP = new Error('memhub-staging: $.http.fetch(https://api.example.test/v1/team/rulebook/rules?view=hook) failed: ECONNRESET: socket hang up')
const SLOW = new Error('memhub-staging: $.http.fetch(https://api.example.test/x) aborted: no complete answer within 30000ms')
/** A cached book an hour older than the fake clock: past Python's 60 s window. */
const STALE = JSON.stringify({ etag: '"v1"', fetched_at: isoOf(1_700_000_000_000 - 3_600_000), rules: [{ id: 'r1' }] })
/** A cached book fetched just now. */
const FRESH = JSON.stringify({ etag: '"v1"', fetched_at: isoOf(1_700_000_000_000 - 1_000), rules: [{ id: 'r1' }] })

describe('a refused $.http.fetch hands the rules to Python', () => {
  test('refusals and blips are told apart by the engine\'s message shape', () => {
    expect(isFetchRefused(POLICY)).toBe(true)
    expect(isFetchRefused(DENIED)).toBe(true)
    expect(isFetchRefused(BLIP)).toBe(false)
    expect(isFetchRefused(SLOW)).toBe(false)
    expect(isFetchRefused(new Error('rules fetch HTTP 500'))).toBe(false)
    expect(isFetchRefused('memhub-staging: $.http.fetch: refused: x')).toBe(true)
  })

  test('the org web-fetch policy refuses: every lane released at once, said, the timer stopped', async () => {
    const { io, claims, book } = await loaded(() => {
      throw POLICY
    }, FRESH)
    book.start()
    expect(await book.refresh()).toBe('failed')
    expect(claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(FETCH_REFUSED)
    expect(io.timers.find(t => t.ms === REFRESH_MS)!.cancelled).toBe(true)
    // for the session: a later claim does not take them back, and no further fetch
    await claims.claim(['pre'])
    expect(claims.list()).toEqual([])
    expect(await book.refresh()).toBe('skipped')
  })

  test('a network blip is retried and the lanes stay claimed', async () => {
    const { io, claims, book } = await loaded(() => {
      throw BLIP
    }, STALE)
    for (let i = 1; i < RELEASE_AFTER_FAILURES; i++) await book.refresh()
    expect(claims.list()).toEqual(['pre', 'post', 'prompt', 'session'])
    expect(io.envVars.staging).toMatch(/^sess-1:\d+:\d*:pre,post,prompt,session$/)
  })

  test(`${RELEASE_AFTER_FAILURES} failures in a row with a book past Python's window: released`, async () => {
    const { io, claims, book } = await loaded(() => {
      throw BLIP
    }, STALE)
    for (let i = 0; i < RELEASE_AFTER_FAILURES; i++) await book.refresh()
    expect(claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(REFRESH_GAVE_UP)
  })

  test('as many failures with a book Python would not refresh either: kept, the failure shown', async () => {
    const { io, claims, book } = await loaded(() => reply(500), FRESH)
    for (let i = 0; i < RELEASE_AFTER_FAILURES; i++) await book.refresh()
    expect(claims.list()).toEqual(['pre', 'post', 'prompt', 'session'])
    expect(io.statuses.at(-1)).toContain('team rules not refreshed')
  })

  test('a success in between resets the count', async () => {
    let fail = true
    const { claims, book } = await loaded(() => {
      if (fail) throw BLIP
      return reply(304)
    }, STALE)
    for (let i = 1; i < RELEASE_AFTER_FAILURES; i++) await book.refresh()
    fail = false
    await book.refresh()
    fail = true
    for (let i = 1; i < RELEASE_AFTER_FAILURES; i++) await book.refresh()
    expect(claims.list()).toEqual(['pre', 'post', 'prompt', 'session'])
  })

  test('a refusal on the load-time fetch (no cache): nothing claimed, said', async () => {
    const io = fakeIo({
      onFetch: () => {
        throw POLICY
      },
    })
    const status = new StatusLine(io)
    const claims = new Claims(io, 'staging', status)
    const book = new Book(io, fakeCtx(io), fakeEngine(), claims, status, { toHook })
    expect(await book.load('/work')).toBe(false)
    expect(io.statuses.at(-1)).toBe(FETCH_REFUSED)
    await claims.claim(['pre'])
    expect(claims.list()).toEqual([])
  })

  test("Python's fetched_at reads back", () => {
    expect(msOfIso('2026-10-06T01:02:03.456789+00:00')).toBe(Date.UTC(2026, 9, 6, 1, 2, 3, 456))
    expect(msOfIso(isoOf(1_700_000_000_000))).toBe(1_700_000_000_000)
    expect(msOfIso('nope')).toBeUndefined()
    expect(msOfIso(null)).toBeUndefined()
  })
})
