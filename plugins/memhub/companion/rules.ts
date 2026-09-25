// What the MemHub rulebook hook (scripts/rulebook_hook.py) said on one tool
// call, read off the classic PreToolUse / PostToolUse answer it folds into:
// the rules that fired, each once, and whether the call was stopped by one.
//
// A fire is read from ONE shape: the disclosure line the hook writes for
// every rule it shows, `📏 Rule fired: <rule>` (advised) or `⛔️ Rule fired:
// <rule>` (stopped), the rule already in 20 words or fewer — rulebook_hook's
// `disclosure_line()`, the same string the terminal shows and the agent is
// told to echo. Nothing else counts. The `- **[<label>]** …` bullets beneath
// it are NOT read: directive_recall.py writes lessons and procedures as
// `- **[LESSON]** …` in the same classic answer, and a lesson that merely
// mentions the rulebook would be announced as a rule.
//
// The one fallback is a denied call's reason, and only the rulebook's own —
// `Blocked by the <brand> team rulebook:` then `- [<name>] …` — for a deny
// that arrives without its context.

export type Fired = { rule: string; isBlocked: boolean }

// `[^\S\n]*` and `\S`: an empty description must not let the match run on
// into the next line (the disclosure instruction's closing sentence).
const DISCLOSURE = /^\s*(📏|⛔️?)\s*Rule fired:[^\S\n]*(\S.*?)\s*$/gmu
const RULEBOOK_DENY = /^\s*Blocked by the .+ team rulebook:/
const REASON_BULLET = /^\s*-\s*\[([^\]]+)\]/gm

export function firedOf(contexts: readonly string[], blockReason?: string): Fired[] {
  const byRule = new Map<string, boolean>()
  const note = (rule: string, isBlocked: boolean) => {
    const key = rule.replace(/\s+/g, ' ').trim()
    if (key) byRule.set(key, (byRule.get(key) ?? false) || isBlocked)
  }
  for (const text of contexts) {
    for (const m of text.matchAll(DISCLOSURE)) note(m[2]!, m[1]!.startsWith('⛔'))
  }
  // The deny names each rule by its label, the disclosure by its 20-word
  // description; reading both would announce a labelled rule twice.
  if (byRule.size === 0 && blockReason && RULEBOOK_DENY.test(blockReason)) {
    const named = [...blockReason.matchAll(REASON_BULLET)]
    for (const m of named) note(m[1]!, true)
    if (named.length === 0) note('a team rule stopped that call', true)
  }
  return [...byRule].map(([rule, isBlocked]) => ({ rule, isBlocked }))
}
