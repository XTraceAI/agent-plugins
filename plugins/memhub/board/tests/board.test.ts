// board.ts, the agent message board's plain-data rules: reading the feed,
// which messages a session's checkout takes, when a claim is free, and what a
// taken message becomes.

import { describe, expect, test } from 'claude-code/testing'

import { claimOf, forSession, isFree, lineOf, type Message, pageAfter, pageOf, promptOf, repoMatches } from '../board'

const ID = '0aa2e490-7773-4f3f-b30d-fcf3a9ba0d74'
const ID2 = '2d221a4a-aebe-4a55-82eb-577b763ab68f'

const row = (over: Record<string, unknown> = {}, payload: Record<string, unknown> = {}) => ({
  notification_id: ID,
  kind: 'agent_message',
  created_at: '2026-11-07T00:10:00Z',
  claim_owner: null,
  claim_expires_at: null,
  payload: { text: 'review the migration', sender_name: 'Felix Meng', brain_name: 'felix↔sebastian', ...payload },
  ...over,
})
const body = (messages: unknown[], next: string | null = null) => JSON.stringify({ data: { messages, next_cursor: next } })
const msg = (over: Partial<Message> = {}): Message => ({
  id: ID, text: 'review the migration', senderName: 'Felix Meng', brainName: 'felix↔sebastian', repo: '',
  createdAt: '2026-11-07T00:10:00Z', claimOwner: null, claimExpiresAt: 0, ...over,
})

describe('pageOf', () => {
  test('reads messages and the next cursor', () => {
    const page = pageOf(body([row({ claim_owner: 'S1', claim_expires_at: '2026-11-07T00:12:00Z' }, { repo: 'XTraceAI/MemHub-Backend' })], 'abc'))
    expect(page?.nextCursor).toBe('abc')
    expect(page?.messages).toEqual([msg({
      repo: 'XTraceAI/MemHub-Backend', claimOwner: 'S1', claimExpiresAt: Date.parse('2026-11-07T00:12:00Z'),
    })])
  })

  test('skips a row with no text or no id, names an unnamed sender, and refuses what is not a page', () => {
    const page = pageOf(body([row({}, { text: '  ' }), row({ notification_id: 'x' }), row({}, { sender_name: '' })]))
    expect(page?.messages.map(m => m.senderName)).toEqual(['A teammate'])
    expect(pageOf('not json')).toBeNull()
    expect(pageOf(JSON.stringify({ data: {} }))).toBeNull()
  })

  test('the next page keeps the limit and escapes the cursor', () => {
    expect(pageAfter('a+b/c=')).toBe('/v2/agent-messages?limit=100&cursor=a%2Bb%2Fc%3D')
  })
})

describe('repoMatches', () => {
  test('no repo: any session', () => {
    expect(repoMatches('', '', '')).toBe(true)
  })

  test('owner/name matches the identity, ignoring case', () => {
    expect(repoMatches('xtraceai/memhub-backend', 'XTraceAI/MemHub-Backend', 'MemHub-Backend')).toBe(true)
    expect(repoMatches('other/MemHub-Backend', 'XTraceAI/MemHub-Backend', 'MemHub-Backend')).toBe(false)
  })

  test('a bare name matches the checkout name; owner/name falls back to it only with no identity', () => {
    expect(repoMatches('MemHub-Backend', 'XTraceAI/MemHub-Backend', 'MemHub-Backend')).toBe(true)
    expect(repoMatches('XTraceAI/MemHub-Backend', '', 'memhub-backend')).toBe(true)
    expect(repoMatches('MemHub-Backend', '', 'agent-plugins')).toBe(false)
  })
})

describe('isFree and forSession', () => {
  test('free when unclaimed, lapsed, or this session holds it', () => {
    expect(isFree(msg(), 'S', 100)).toBe(true)
    expect(isFree(msg({ claimOwner: 'T', claimExpiresAt: 50 }), 'S', 100)).toBe(true)
    expect(isFree(msg({ claimOwner: 'S', claimExpiresAt: 500 }), 'S', 100)).toBe(true)
    expect(isFree(msg({ claimOwner: 'T', claimExpiresAt: 500 }), 'S', 100)).toBe(false)
  })

  test("keeps this checkout's messages, oldest first", () => {
    const newer = msg({ id: ID2, createdAt: '2026-11-07T00:20:00Z' })
    const elsewhere = msg({ id: 'e', repo: 'XTraceAI/agent-plugins' })
    expect(forSession([newer, elsewhere, msg()], 'XTraceAI/MemHub-Backend', 'MemHub-Backend').map(m => m.id)).toEqual([ID, ID2])
  })
})

describe('what the person and the model see', () => {
  test('the band line names sender and brain, first line only, cut to width', () => {
    expect(lineOf(msg({ text: 'first\nsecond' }), 200)).toBe('📨 Felix Meng via felix↔sebastian: first')
    const cut = lineOf(msg({ text: 'x'.repeat(300) }), 40)
    expect(cut.length).toBe(40)
    expect(cut.endsWith('…')).toBe(true)
  })

  test("the prompt says who sent it, that it is a teammate's request, and how to answer", () => {
    const p = promptOf(msg())
    expect(p).toContain('[Agent message from Felix Meng via the MemHub brain "felix↔sebastian"')
    expect(p).toContain('review the migration')
    expect(p).toContain("not the user's own words")
    expect(p).toContain(`reply_to="${ID}"`)
  })
})

describe('claimOf', () => {
  test('won, lost to another session, or no answer', () => {
    expect(claimOf(200, JSON.stringify({ data: { claimed: true, claim_owner: 'S' } }))).toEqual({ claimed: true })
    expect(claimOf(200, JSON.stringify({ data: { claimed: false, claim_owner: 'T', reason: 'claimed' } })))
      .toEqual({ claimed: false, owner: 'T', reason: 'claimed' })
    expect(claimOf(403, '{}')).toBeNull()
    expect(claimOf(200, 'nope')).toBeNull()
  })
})
