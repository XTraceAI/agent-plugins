// What the companion announces from one classic PreToolUse / PostToolUse
// answer. The fixtures are the hooks' real output, not a paraphrase of it:
// DIRECTIVES is directive_recall._render(), the Rulebook block's disclosure
// lines are rulebook_hook.disclosure_line() and disclosure_instruction(), and
// DENY is the shape of the hook's permissionDecisionReason. When either hook
// changes what it writes, regenerate these from it.

import { describe, expect, test } from 'claude-code/testing'

import { firedOf } from '../rules'

const DIRECTIVES = [
  '## 📋 Relevant team directives for this action',
  "(situated lessons/procedures that fired on what you're touching — act on them)",
  '- **[PROCEDURE]** After editing rulebook_hook.py, rerun the rulebook suite.',
  '1. Run the rulebook tests.  _(fired on: rulebook_hook.py)_',
  '- **[LESSON]** Run uv sync before pytest.  _(as of 2026-09-12)_',
].join('\n')

const RULEBOOK = [
  '## 📏 Rulebook',
  '- **[Name the environment before any live read]** Before any live read or write, name prod or staging.',
  '- **BLOCKED [no-force-push]** never force push',
  '',
  '_Disclose these to the user. Begin your next reply with the following line(s), verbatim and each on its own line, before anything else — including before any tool call narration:_',
  '📏 Rule fired: Name the environment before any live read',
  '⛔️ Rule fired: no-force-push',
  '_This is how the team sees its rules working. Do not paraphrase, do not merge them into a sentence, and do not omit one because it did not change what you were going to do — a rule that fired and changed nothing is exactly the rule the team needs to hear about._',
].join('\n')

const DENY = [
  'Blocked by the XTrace team rulebook:',
  '- [no-force-push] never force push',
  "If this is a legitimate exception, re-run the same command prefixed RULEBOOK_OVERRIDE='<why>'.",
].join('\n')

const FIRED = [
  { rule: 'Name the environment before any live read', isBlocked: false },
  { rule: 'no-force-push', isBlocked: true },
]

describe('firedOf', () => {
  test('a lesson or procedure is never a rule, even one about the rulebook', () => {
    expect(firedOf([DIRECTIVES])).toEqual([])
  })

  test('beside the rulebook, only the rulebook fires are announced', () => {
    expect(firedOf([RULEBOOK, DIRECTIVES])).toEqual(FIRED)
    expect(firedOf([DIRECTIVES, RULEBOOK])).toEqual(FIRED)
  })

  test('the same holds when the engine hands both over as one text', () => {
    expect(firedOf([`${RULEBOOK}\n\n${DIRECTIVES}`])).toEqual(FIRED)
  })

  test('the Rulebook bullets alone announce nothing: the disclosure line is the fire', () => {
    expect(firedOf(['## 📏 Rulebook\n- **[no-force-push]** never force push'])).toEqual([])
  })

  test('a deny that arrives without its context still names the rule', () => {
    expect(firedOf([], DENY)).toEqual([{ rule: 'no-force-push', isBlocked: true }])
  })

  test('a deny with its context is announced once, from the disclosure', () => {
    expect(firedOf([RULEBOOK], DENY)).toEqual(FIRED)
  })

  test("another hook's deny is not a rule, even if it mentions the rulebook", () => {
    expect(firedOf([], 'add_memory is disabled here; see the rulebook for why.')).toEqual([])
  })

  test('an empty disclosure never swallows the line after it', () => {
    // rulebook_hook.disclosure_line() on a rule with no label, text or id
    expect(firedOf(['📏 Rule fired: \n_This is how the team sees its rules working._'])).toEqual([])
  })

  test('a rulebook deny that names no rule still says one stopped the call', () => {
    expect(firedOf([], 'Blocked by the XTrace team rulebook:')).toEqual([
      { rule: 'a team rule stopped that call', isBlocked: true },
    ])
  })
})
