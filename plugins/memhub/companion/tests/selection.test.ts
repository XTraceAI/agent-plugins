// Which animal a session shows (selection.ts): the hash, the pool, the order
// of the three sources, and the per-session list. Pure functions, no engine.
// selection_vectors.ts is shared with tests/companion_contract_test.py,
// which recomputes every hash in Python — so this one is pinned by an
// implementation that is not its own.

import { describe, expect, test } from 'claude-code/testing'

import { fnv1a32, pick, poolOf, prefsOf, remember, SESSIONS_KEPT, sessionsOf } from '../selection'
import { VECTORS } from './selection_vectors'

const NAMES = ['Goose', 'Hippo', 'Penguin']
const NONE = { pin: null, never: [] }

describe('fnv1a32', () => {
  for (const { s, h } of VECTORS) {
    test(`hashes ${JSON.stringify(s)} as the Python reference does`, () => {
      expect(fnv1a32(s)).toBe(h)
    })
  }

  test('is the published FNV-1a: "foobar" is 0xbf9cf968', () => {
    expect(fnv1a32('foobar')).toBe(0xbf9cf968)
  })
})

describe('poolOf', () => {
  test('keeps the registry order and drops the left-out ones, whatever their case', () => {
    expect(poolOf(NAMES, ['HIPPO'])).toEqual(['Goose', 'Penguin'])
  })

  test('is never empty: leaving every animal out leaves them all in', () => {
    expect(poolOf(NAMES, ['goose', 'hippo', 'penguin'])).toEqual(NAMES)
  })
})

describe('pick', () => {
  const id = '3611ce9d-5dfd-4c37-bc53-96da86d04828'

  test('with nothing set, the session id picks, and picks the same every time', () => {
    const got = pick(NAMES, id, [], NONE)
    expect(got).toBe(NAMES[fnv1a32(id) % NAMES.length])
    expect(pick(NAMES, id, [], NONE)).toBe(got)
  })

  test('different sessions can show different animals', () => {
    const seen = new Set(Array.from({ length: 30 }, (_, i) => pick(NAMES, `session-${i}`, [], NONE)))
    expect(seen.size).toBe(NAMES.length)
  })

  test('the pool narrows the hash; the pin beats it; the session\'s own pick beats both', () => {
    const pool = poolOf(NAMES, ['goose'])
    expect(pick(NAMES, id, [], { pin: null, never: ['goose'] })).toBe(pool[fnv1a32(id) % pool.length])
    expect(pick(NAMES, id, [], { pin: 'penguin', never: [] })).toBe('Penguin')
    expect(pick(NAMES, id, [[id, 'hippo']], { pin: 'penguin', never: [] })).toBe('Hippo')
    expect(pick(NAMES, id, [['another', 'hippo']], { pin: 'penguin', never: [] })).toBe('Penguin')
  })

  test('an unknown name anywhere is ignored, not trusted', () => {
    expect(pick(NAMES, id, [[id, 'dragon']], { pin: 'unicorn', never: ['yeti'] }))
      .toBe(NAMES[fnv1a32(id) % NAMES.length])
  })
})

describe('remember', () => {
  test('keeps one entry a session, the newest last', () => {
    const once = remember([['a', 'goose'], ['b', 'hippo']], 'a', 'Penguin')
    expect(once).toEqual([['b', 'hippo'], ['a', 'penguin']])
  })

  test(`keeps no more than ${SESSIONS_KEPT}, dropping the oldest`, () => {
    let sessions = sessionsOf([])
    for (let i = 0; i < SESSIONS_KEPT + 5; i++) sessions = remember(sessions, `s${i}`, 'goose')
    expect(sessions.length).toBe(SESSIONS_KEPT)
    expect(sessions[0]![0]).toBe('s5')
  })
})

describe('what the store hands back', () => {
  test('a malformed session list is dropped entry by entry', () => {
    expect(sessionsOf([['a', 'goose'], 'x', ['b'], [1, 'hippo'], ['c', 'penguin']]))
      .toEqual([['a', 'goose'], ['c', 'penguin']])
    expect(sessionsOf({ a: 1 })).toEqual([])
  })

  test('a malformed pin or list is treated as absent', () => {
    expect(prefsOf(3, 'goose')).toEqual({ pin: null, never: [] })
    expect(prefsOf('Hippo', ['Goose', 4])).toEqual({ pin: 'hippo', never: ['goose'] })
  })
})
