// The rules the MemHub harness filed for review from this session and still
// waiting, as the band asks them, and what the animal says of them. Where they
// come from is feed.ts: the server's list (the poll) and `$.state` proposals.
// The server is the only record of a filing (harness-tied-memory-spec §3.4a):
// the harness's fork files through `create_rule` and writes nothing locally.

export type Proposed = {
  title: string
  ruleId: string
  env: string
  /** Where it opens in MemHub Studio, once looked up (feed.studioUrl); '' for nowhere. */
  url?: string
  /** Who it applies to: its rulebook's scope label ("Everyone in Acme", "Just you"). */
  audience?: string
}

/**
 * What the animal says: that a rule was proposed and is not active yet, who it
 * would apply to (its scope label, when the server sent one), and which rule.
 * Its `rule↗` (RULE_WORD) opens the rule in MemHub Studio: a pressable word
 * drawn over that word; the buttons go on the row under it. The title stays
 * last, so a long one is what fitBubble cuts.
 */
export function proposalSaid(p: Proposed): string {
  const forWhom = p.audience ? ` for ${audienceWords(p.audience)}` : ''
  return `New ${RULE_WORD} proposed${forWhom}, not active yet: ${p.title || 'an untitled rule'}`
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

/** What deciding it came to (feed.decisionOf, rule_decide.py's outcomes), as the animal says it back. */
export type Decision = { outcome: string; msg?: string }

export function decisionSaid(p: Proposed, action: 'activate' | 'reject', d: Decision): string {
  const title = p.title || 'the rule'
  switch (d.outcome) {
    case 'active': return `Activated: ${title}. It fires for ${audienceWords(p.audience)} from now on.`
    case 'dismissed': return `Rejected: ${title}. It will not fire.`
    case 'forbidden':
      return action === 'activate'
        ? `Only an org admin can activate it. It is waiting for one in MemHub Studio: ${title}`
        : `You cannot reject this one; an org admin can, in MemHub Studio: ${title}`
    case 'decided': return `Someone already decided it: ${title}`
    case 'gone': return `That rule is gone from MemHub: ${title}`
    case 'wrong_env': return `Filed in ${p.env || 'another'} MemHub; answer it in Studio there: ${title}`
    case 'no_key': return `Log in first with /memhub:login, then answer it in MemHub Studio: ${title}`
    default: return `Could not reach MemHub (${d.msg || 'error'}); it is still in Studio: ${title}`
  }
}

/**
 * A scope label as the end of "It fires for …": "Everyone in Acme" reads
 * "everyone in Acme", "Just you" reads "just you"; a workspace's label is a
 * name and stays as written. No label (an older backend) is "the team".
 */
function audienceWords(label: string | undefined): string {
  if (!label) return 'the team'
  return /^(Everyone|Just) /.test(label) ? label[0].toLowerCase() + label.slice(1) : `the ${label}`
}

/** Per session, the rule ids it has announced; stored so a reload does not announce them again. */
export type Announced = readonly (readonly [sessionId: string, ruleIds: readonly string[]])[]

/** How many sessions' announcements are kept; the oldest go first. */
export const ANNOUNCED_KEPT = 200

/** A stored value read back as Announced; anything malformed is dropped. */
export function announcedOf(stored: unknown): Announced {
  if (!Array.isArray(stored)) return []
  return stored.filter(
    (e): e is [string, string[]] =>
      Array.isArray(e) && e.length === 2 && typeof e[0] === 'string' &&
      Array.isArray(e[1]) && e[1].every(id => typeof id === 'string'),
  )
}

/** The rule ids `sessionId` has announced. */
export function announcedIn(announced: Announced, sessionId: string): string[] {
  return [...(announced.find(([id]) => id === sessionId)?.[1] ?? [])]
}

/** `announced` with `ruleIds` added to this session's, it last, and no more than `cap` sessions. */
export function withAnnounced(
  announced: Announced, sessionId: string, ruleIds: readonly string[], cap = ANNOUNCED_KEPT,
): Announced {
  const ids = [...new Set([...announcedIn(announced, sessionId), ...ruleIds])]
  const kept = announced.filter(([id]) => id !== sessionId)
  return [...kept, [sessionId, ids] as const].slice(-cap)
}
