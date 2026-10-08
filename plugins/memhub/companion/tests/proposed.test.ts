// What the companion reads from the server's proposed-rules list (the poll):
// the rules this session's harness fork filed that are still waiting, kept by
// rule_decide.py `proposed()`'s own filter (`source_ref` starts with
// `<session>#`; tests/rule_decide_test.py pins the Python side). The fixture
// is the REST envelope `GET /v1/team/rulebook/rules` answers with.

import { describe, expect, test } from 'claude-code/testing'

import { listedRules } from '../feed'
import { announcedIn, announcedOf, decisionSaid, nameOf, proposalSaid, RULE_WORD, withAnnounced } from '../proposed'
import { fitBubble } from '../screen'
import { listed, ruleRow } from './beneath'

const SESSION = 'sess-a1'
const LISTED = listed(
  ruleRow(SESSION, '846c4331-1b1d-4295-afe9-18156f44f1df', 'Pin the MCP server when spawning claude -p'),
  { ...ruleRow(SESSION, 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', '  Run only the touched test suites  '), rulebook_id: 'book-1' },
  ruleRow('sess-other', '11111111-1b1d-4295-afe9-18156f44f1df', 'Not this session\'s'),
)

describe('the server\'s list, as the poll reads it', () => {
  test('this session\'s rows are what is waiting, titles trimmed; another session\'s are not', () => {
    expect(listedRules(LISTED, SESSION, 7)).toEqual([
      { ruleId: '846c4331-1b1d-4295-afe9-18156f44f1df', title: 'Pin the MCP server when spawning claude -p', at: 7 },
      { ruleId: 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', title: 'Run only the touched test suites', at: 7, rulebookId: 'book-1' },
    ])
  })

  test('a row\'s scope label is who it applies to', () => {
    const rows = listed({
      ...ruleRow(SESSION, 'c1', 'Small PRs'), rulebook_id: 'book-o',
      rulebook: { rulebook_id: 'book-o', name: 'org', label: 'Everyone in\nAcme' },
    })
    expect(listedRules(rows, SESSION, 3)).toEqual([
      { ruleId: 'c1', title: 'Small PRs', at: 3, rulebookId: 'book-o', audience: 'Everyone in Acme' },
    ])
  })

  test('the session must be the source_ref\'s whole first part, not a prefix of it', () => {
    expect(listedRules(LISTED, 'sess-a', 1)).toEqual([])
  })

  test('a row without a rule id is nothing to answer, so it is dropped', () => {
    const rows = listed({ rule_id: '', title: 't', source_ref: `${SESSION}#1` }, [1], null as never)
    expect(listedRules(rows, SESSION, 1)).toEqual([])
  })

  test('a real list, even an empty one, is the server\'s whole answer', () => {
    expect(listedRules(listed(), SESSION, 1)).toEqual([])
    // a backend that does not return source_ref yet lists nothing for anyone
    expect(listedRules(listed({ rule_id: 'r', title: 't' }), SESSION, 1)).toEqual([])
  })

  test('could not ask, or an odd reply, is null: not "nothing waiting"', () => {
    for (const out of ['', 'not json', '[1,2]', '{"data":{"rules":"x"}}', '{}', '{"code":500,"msg":"boom"}']) {
      expect(listedRules(out, SESSION, 1), out).toBeNull()
    }
  })
})

describe('what the animal says', () => {
  test('an activated rule says who it fires for, from its scope label', () => {
    const p = { title: 'Small PRs', ruleId: 'r', env: 'staging' }
    const said = (audience?: string) => decisionSaid({ ...p, audience }, 'activate', { outcome: 'active' })
    expect(said('Everyone in Acme')).toBe('Activated: Small PRs. It fires for everyone in Acme from now on.')
    expect(said('Just you')).toBe('Activated: Small PRs. It fires for just you from now on.')
    expect(said('Platform workspace')).toBe('Activated: Small PRs. It fires for the Platform workspace from now on.')
    expect(said()).toBe('Activated: Small PRs. It fires for the team from now on.')
    expect(decisionSaid(p, 'activate', { outcome: 'forbidden' })).toContain('Only an org admin can activate it')
  })

  test('the bubble says it is not active yet, and names the rule', () => {
    const said = proposalSaid({ title: 'Pin the MCP server', ruleId: 'r', env: 'staging' })
    expect(said).toBe('New rule↗ proposed, not active yet: Pin the MCP server')
    expect(fitBubble(said)).toBe(said)
    expect(proposalSaid({ title: '', ruleId: 'r', env: '' })).toContain('an untitled rule')
  })

  test('the bubble says who the proposed rule would apply to, from its scope label', () => {
    const p = { title: 'Small PRs', ruleId: 'r', env: 'staging' }
    expect(proposalSaid({ ...p, audience: 'Everyone in Acme' }))
      .toBe('New rule↗ proposed for everyone in Acme, not active yet: Small PRs')
    expect(proposalSaid({ ...p, audience: 'Just you' }))
      .toBe('New rule↗ proposed for just you, not active yet: Small PRs')
    expect(fitBubble(proposalSaid({ ...p, audience: 'Just you', title: 'x'.repeat(300) }))
      .startsWith(`New ${RULE_WORD} proposed for just you, not active yet:`)).toBe(true)
  })

  test('a long name is cut to fit, and the linked `rule` still leads it', () => {
    const fitted = fitBubble(proposalSaid({ title: 'x'.repeat(300), ruleId: 'r', env: 'staging' }))
    expect(fitted.startsWith(`New ${RULE_WORD} proposed, not active yet:`)).toBe(true)
    expect(fitted.endsWith('…')).toBe(true)
  })

})

describe("the rule's name, where it is drawn plain", () => {
  const rule = { title: 'Pin the MCP server', ruleId: '846c4331-1b1d-4295-afe9-18156f44f1df', env: 'staging' }

  test('is the name as written, never Markdown', () => {
    expect(nameOf({ ...rule, title: 'use [x](evil) and *y*' }, 60)).toBe('use [x](evil) and *y*')
    expect(nameOf({ ...rule, title: '' }, 38)).toBe('untitled rule')
  })

  test('a long name is cut with … to the width it is drawn in', () => {
    expect(nameOf({ ...rule, title: 'y'.repeat(60) }, 38)).toBe('y'.repeat(37) + '…')
  })
})

describe('announced, as stored', () => {
  test('ids add up per session, without repeats, newest session last and capped', () => {
    let a = withAnnounced([], 's1', ['r1'])
    a = withAnnounced(a, 's2', ['r2'])
    a = withAnnounced(a, 's1', ['r1', 'r3'])
    expect(a).toEqual([['s2', ['r2']], ['s1', ['r1', 'r3']]])
    expect(announcedIn(a, 's1')).toEqual(['r1', 'r3'])
    expect(announcedIn(a, 'nope')).toEqual([])
    expect(withAnnounced(a, 's3', ['r4'], 2).map(([id]) => id)).toEqual(['s1', 's3'])
  })

  test('anything malformed in the store is dropped', () => {
    expect(announcedOf(undefined)).toEqual([])
    expect(announcedOf([['s', ['r']], ['s'], ['s', [1]], 'x', [2, []]])).toEqual([['s', ['r']]])
  })
})
