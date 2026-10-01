// What the companion reads from `rule_decide.py proposed` at a Stop: the
// rules this session's harness fork filed that are still waiting. The fixture
// is rule_decide.py's real output shape, `{"proposed": [{title, rule_id, env}]}`
// (tests/rule_decide_test.py pins the Python side).

import { describe, expect, test } from 'claude-code/testing'

import { nameOf, proposalSaid, proposedOf, RULE_WORD } from '../proposed'
import { fitBubble } from '../screen'

const LISTED = JSON.stringify({
  proposed: [
    { title: 'Pin the MCP server when spawning claude -p', rule_id: '846c4331-1b1d-4295-afe9-18156f44f1df', env: 'staging' },
    { title: '  Run only the touched test suites  ', rule_id: 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', env: 'staging' },
  ],
})

describe('proposedOf', () => {
  test('every listed rule is proposed, its title trimmed', () => {
    expect(proposedOf(LISTED)).toEqual([
      { title: 'Pin the MCP server when spawning claude -p', ruleId: '846c4331-1b1d-4295-afe9-18156f44f1df', env: 'staging' },
      { title: 'Run only the touched test suites', ruleId: 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', env: 'staging' },
    ])
  })

  test('only the last stdout line is read, as the script prints one', () => {
    expect(proposedOf(`(progress line)\n${LISTED}\n`)).toHaveLength(2)
  })

  test('a row without a rule id is nothing to answer, so it is dropped', () => {
    expect(proposedOf('{"proposed":[{"title":"t","rule_id":""},[1],null]}')).toEqual([])
  })

  test('malformed or empty output proposes nothing, never throws', () => {
    for (const out of ['', 'not json', '[1,2]', '{"proposed":"x"}', '{}']) expect(proposedOf(out)).toEqual([])
  })
})

describe('what the animal says', () => {
  test('the bubble says it is not active yet, and names the rule', () => {
    const said = proposalSaid({ title: 'Pin the MCP server', ruleId: 'r', env: 'staging' })
    expect(said).toBe('New rule↗ proposed, not active yet: Pin the MCP server')
    expect(fitBubble(said)).toBe(said)
    expect(proposalSaid({ title: '', ruleId: 'r', env: '' })).toContain('an untitled rule')
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
