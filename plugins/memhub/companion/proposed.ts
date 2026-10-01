// The rules the MemHub harness filed for review from this session and still
// waiting, as the server lists them (`scripts/rule_decide.py proposed`). The
// server is the only record of a filing (harness-tied-memory-spec §3.4a): the
// harness's fork files through `create_rule` and writes nothing locally.

export type Proposed = {
  title: string
  ruleId: string
  env: string
  /** Where it opens in MemHub Studio, once rule_decide.py has said; '' for nowhere. */
  url?: string
}

/** rule_decide.py's `proposed` answer; anything malformed is nothing to announce. */
export function proposedOf(stdout: string): Proposed[] {
  let got: unknown
  try {
    got = JSON.parse(stdout.trim().split('\n').pop() || '{}')
  } catch {
    return []
  }
  const rows = (got as { proposed?: unknown })?.proposed
  if (!Array.isArray(rows)) return []
  return rows
    .filter((r): r is Record<string, unknown> => !!r && typeof r === 'object' && !Array.isArray(r))
    .map(r => ({ title: String(r.title ?? '').trim(), ruleId: String(r.rule_id ?? ''), env: String(r.env ?? '') }))
    .filter(p => p.ruleId)
}

/**
 * What the animal says: that a rule was proposed and is not active yet, and
 * which one. Its `rule↗` (RULE_WORD) opens the rule in MemHub Studio: a
 * pressable word drawn over that word; the buttons go on the row under it.
 */
export function proposalSaid(p: Proposed): string {
  return `New ${RULE_WORD} proposed, not active yet: ${p.title || 'an untitled rule'}`
}

/**
 * The word of the proposal that opens the rule in MemHub Studio, with the ↗
 * that says so: a pressable label cannot be underlined until the pointer is
 * over it, so the mark is what makes it read as a link.
 */
export const RULE_WORD = 'rule↗'

/**
 * The rule's name, at most `width` cells, cut with … to fit. It is drawn as
 * plain text, never Markdown: a terminal without hyperlinks draws a Markdown
 * link as its text AND its URL, which does not fit where the name goes.
 */
export function nameOf(p: Proposed, width: number): string {
  const name = p.title || 'untitled rule'
  return name.length > width ? `${name.slice(0, Math.max(1, width - 1))}…` : name
}

/** What rule_decide.py answered, as the animal says it back. */
export type Decision = { outcome: string; msg?: string }

export function decisionSaid(p: Proposed, action: 'activate' | 'reject', d: Decision): string {
  const title = p.title || 'the rule'
  switch (d.outcome) {
    case 'active': return `Activated: ${title}. It fires for the team from now on.`
    case 'dismissed': return `Rejected: ${title}. It will not fire.`
    case 'forbidden':
      return action === 'activate'
        ? `Only a rulebook admin can activate it. It is waiting for one in MemHub Studio: ${title}`
        : `You cannot reject this one; a rulebook admin can, in MemHub Studio: ${title}`
    case 'decided': return `Someone already decided it: ${title}`
    case 'gone': return `That rule is gone from MemHub: ${title}`
    case 'wrong_env': return `Filed in ${p.env || 'another'} MemHub; answer it in Studio there: ${title}`
    case 'no_key': return `Log in first with /memhub:login, then answer it in MemHub Studio: ${title}`
    default: return `Could not reach MemHub (${d.msg || 'error'}); it is still in Studio: ${title}`
  }
}
