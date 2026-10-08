// feed.ts, the companion's plain-data rules: which `$.state` fires are new,
// which classic answers to read, how a poll's list merges with what the
// poll did not write, and what MemHub's answers to the poll and the PATCH
// come to (rule_decide.py's outcomes, mirrored).

import { describe, expect, test } from 'claude-code/testing'

import type { FireNote, ProposalNote } from '../../types'
import {
  decisionOf, envName, fireKey, firesOf, freshFires, headersOf, isClassicLane, lanesOf, mergedProposals,
  proposalsOf, studioUrl,
} from '../feed'

const RULE_ID = 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4'
const fire = (at: number, rule = 'Keep tests fast', isBlocked = false): FireNote =>
  ({ ruleId: RULE_ID, line: `📏 Rule fired: ${rule}`, rule, isBlocked, at })
const note = (ruleId: string, at: number, title = 't'): ProposalNote => ({ ruleId, title, at })

describe('fires', () => {
  test('the new notes, oldest first, and every key held now to remember', () => {
    const seen = new Set([fireKey(fire(1))])
    const { fresh, seen: next } = freshFires([fire(1), fire(2), fire(3, 'Other')], seen)
    expect(fresh.map(f => f.at)).toEqual([2, 3])
    expect([...next]).toEqual([fire(1), fire(2), fire(3, 'Other')].map(fireKey))
  })

  test('the same rule firing twice at two moments is two fires; one note read twice is one', () => {
    expect(freshFires([fire(5), fire(6)], new Set()).fresh).toHaveLength(2)
    expect(freshFires([fire(5), fire(5)], new Set()).fresh).toHaveLength(1)
  })

  test('a note that left the capped list is forgotten, and nothing comes back', () => {
    const { seen } = freshFires([fire(2)], new Set([fireKey(fire(1)), fireKey(fire(2))]))
    expect(seen.has(fireKey(fire(1)))).toBe(false)
  })

  test('anything that is not a note is dropped, never thrown on', () => {
    expect(firesOf(undefined)).toEqual([])
    expect(firesOf('x')).toEqual([])
    expect(firesOf([null, 1, { rule: '' , isBlocked: false, at: 1 }, { rule: 'r', isBlocked: 'no', at: 1 }, fire(1)])).toEqual([fire(1)])
  })
})

describe('lanes', () => {
  test('the classic answer is read only for a lane the mod has not claimed', () => {
    expect(isClassicLane([], 'pre')).toBe(true)
    expect(isClassicLane(['pre'], 'pre')).toBe(false)
    expect(isClassicLane(['pre'], 'post')).toBe(true)
    expect(isClassicLane(lanesOf(['pre', 'post', 'prompt']), 'post')).toBe(false)
  })

  test('an unwritten or odd lanes value claims nothing', () => {
    expect(lanesOf(undefined)).toEqual([])
    expect(lanesOf({ pre: true })).toEqual([])
  })
})

describe('proposals', () => {
  test('never written is not an empty list: nothing may come off the band for it', () => {
    expect(proposalsOf(undefined)).toBeNull()
    expect(proposalsOf([])).toEqual([])
    expect(proposalsOf([note('a', 1), { ruleId: '', title: 't', at: 1 }, { ruleId: 'b' }])).toEqual([note('a', 1)])
  })

  test('a poll keeps what the server lists, each with the `at` it was first written at', () => {
    const merged = mergedProposals([note('a', 10, 'old title')], [note('a', 50, 'A'), note('b', 50)], 40)
    expect(merged).toEqual([note('a', 10, 'A'), note('b', 50)])
  })

  test('what the server stopped listing was decided: it goes', () => {
    expect(mergedProposals([note('a', 10)], [], 40)).toEqual([])
  })

  test('a rule the harness pushed while the poll was out stays, though the list predates it', () => {
    expect(mergedProposals([note('a', 10), note('h', 45)], [], 40)).toEqual([note('h', 45)])
  })

  test('a rulebook id the list carries is kept; none is written as no field at all', () => {
    const merged = mergedProposals([note('a', 10)], [{ ...note('a', 50), rulebookId: 'book' }], 40)
    expect(merged).toEqual([{ ...note('a', 10), rulebookId: 'book' }])
    expect('rulebookId' in mergedProposals([note('a', 10)], [note('a', 50)], 40)[0]!).toBe(false)
  })
})

describe('deciding, as rule_decide.py decide() reads the PATCH', () => {
  test('the new status, or the action\'s when the body names none', () => {
    expect(decisionOf(200, '{"code":0,"data":{"status":"active"}}', 'activate')).toEqual({ outcome: 'active', msg: '' })
    expect(decisionOf(200, '{}', 'reject')).toEqual({ outcome: 'dismissed', msg: '' })
  })

  test('403 forbidden, 404 gone, 400 and 409 decided, the rest an error, the server\'s msg kept', () => {
    expect(decisionOf(403, '{"msg":"not an admin"}', 'activate')).toEqual({ outcome: 'forbidden', msg: 'not an admin' })
    expect(decisionOf(404, '', 'activate')).toEqual({ outcome: 'gone', msg: 'HTTP 404' })
    expect(decisionOf(400, '{}', 'reject').outcome).toBe('decided')
    expect(decisionOf(409, '{}', 'reject').outcome).toBe('decided')
    expect(decisionOf(500, 'oops', 'reject')).toEqual({ outcome: 'error', msg: 'HTTP 500' })
  })

  test('a 200 whose envelope code is not 0 is a failure the transport called success', () => {
    expect(decisionOf(200, '{"code":7,"msg":"nope"}', 'activate')).toEqual({ outcome: 'error', msg: 'nope' })
    expect(decisionOf(200, 'not json', 'activate').outcome).toBe('error')
  })
})

describe('where things are', () => {
  test('a rule opens in the Studio api-info pairs with the API; none paired, no link', () => {
    expect(studioUrl('https://app.example.test', RULE_ID)).toBe(`https://app.example.test/studio/rulebook?open=${RULE_ID}`)
    expect(studioUrl('https://app.example.test/', RULE_ID)).toBe(`https://app.example.test/studio/rulebook?open=${RULE_ID}`)
    expect(studioUrl('https://app.example.test', '')).toBe('https://app.example.test/studio/rulebook')
    expect(studioUrl('', RULE_ID)).toBe('')
    expect(studioUrl('http://app.example.test', RULE_ID), 'never a link over plain http').toBe('')
    expect(studioUrl('https://app.example.test/x?y', RULE_ID), 'an origin, not a page').toBe('')
    expect(studioUrl('https://app.example.test', 'not-a-uuid')).toBe('')
  })

  test('the env the band names follows the mod\'s ctx.env, spelled as harness_stop does', () => {
    expect(envName('staging')).toBe('staging')
    expect(envName('prod')).toBe('production')
  })

  test('the headers rule_decide.py sends', () => {
    expect(headersOf({ base: 'b', bearer: 'k' }, '0.110.8', true)).toEqual({
      Authorization: 'Bearer k', 'X-MemHub-Plugin-Version': '0.110.8', 'Content-Type': 'application/json',
    })
    expect(headersOf({ base: 'b', bearer: 'k' }, '', false)).toEqual({ Authorization: 'Bearer k' })
  })
})
