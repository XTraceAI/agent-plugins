// The two override forms (rulebook_hook.py §5.3) and the dismissals they
// carry: a port of find_override, strip_override, _raw_token,
// find_edit_override, split_named_override, named_rules, resolve_dismissals
// and _dismissal_lines. Pure.

import type { HookRule } from './index'
import { pyExec, pyFinditer, pyMatch } from './py'
import { pyStr, pyStrip } from './rules'
import { logicalLines, segmentOp, shellOnly, shlexSplit } from './shell'

export const BRAND = 'XTrace'
const PREFIX = 'RULEBOOK_OVERRIDE='
const TOKEN_RX = String.raw`RULEBOOK_OVERRIDE=(?:'[^']*'|"[^"]*"|\S*)\s*`
const EDIT_RX = String.raw`rulebook-override(?:\[([^\]\r\n]*)\])?\s*:[ \t]*(\S[^\r\n]*)`
const COMMENT_CLOSE_RX = String.raw`\s*(?:\*/|-->|--\}\}|\}\}|#\}|\*\))\s*$`
const NAMED_REASON_RX = String.raw`^\[([^\]\r\n]+)\]\s*(.*)$`

/** (reason, line, raw token) */
export type Found = [string, string, string | null]

function valueOf(token: string): string | null {
  try {
    const first = shlexSplit(token)[0]
    if (first === undefined) return null
    return first.slice(PREFIX.length)
  } catch {
    return null
  }
}

/** `_raw_token(line, reason)` */
function rawToken(line: string, reason: string): string | null {
  for (const m of pyFinditer(TOKEN_RX, line)) {
    const v = valueOf(m[0])
    if (v !== null && pyStrip(v) === reason) return m[0]
  }
  return null
}

/** `find_override(cmd)`: the first non-empty `RULEBOOK_OVERRIDE=<why>` that begins a shell segment. */
export function findOverride(cmd: string): Found | null {
  const text = shellOnly(cmd).replace(/\\\n/g, ' ')
  for (const [line, toks] of logicalLines(text)) {
    if (toks === null) continue
    let atStart = true
    for (const tok of toks) {
      if (atStart && tok.startsWith(PREFIX)) {
        const reason = pyStrip(tok.slice(PREFIX.length))
        if (reason) return [reason, line, rawToken(line, reason)]
      }
      atStart = segmentOp(tok)
    }
  }
  return null
}

/** `str.replace(old, new, 1)` without JS replacement-pattern surprises. */
const replaceOnce = (s: string, old: string, rep: string): string => {
  const i = s.indexOf(old)
  return i < 0 ? s : s.slice(0, i) + rep + s.slice(i + old.length)
}

/** `strip_override(cmd, found)`: exactly the validated assignment removed. */
export function stripOverride(cmd: string, found: Found): string {
  const [reason, line, raw] = found
  if (raw && cmd.includes(line)) return replaceOnce(cmd, line, replaceOnce(line, raw, ''))
  for (const m of pyFinditer(TOKEN_RX, cmd)) {
    const v = valueOf(m[0])
    if (v !== null && pyStrip(v) === reason) return cmd.slice(0, m.index) + cmd.slice(m.index + m[0].length)
  }
  return cmd
}

/** `find_edit_override(body)`: {rule name lowered: reason}, first marker per name wins; "" = unnamed. */
export function findEditOverride(body: string): Map<string, string> {
  const found = new Map<string, string>()
  for (const m of pyFinditer(EDIT_RX, body || '', 'i')) {
    const g2 = pyStrip(m[2] ?? '')
    const close = pyExec(COMMENT_CLOSE_RX, g2)
    const reason = pyStrip(close ? g2.slice(0, close.index) + g2.slice(close.index + close[0].length) : g2)
    if (!reason) continue
    const name = pyStrip(m[1] ?? '').toLowerCase()
    if (!found.has(name)) found.set(name, reason)
  }
  return found
}

/** `split_named_override(reason)`: `[<label>] <why>` → [label lowered, why]; a bare reason → [null, reason]. */
export function splitNamedOverride(reason: string): [string | null, string] {
  const m = pyMatch(NAMED_REASON_RX, reason || '', 's')
  if (!m) return [null, reason]
  return [pyStrip(m[1]!).toLowerCase(), pyStrip(m[2]!)]
}

const idOf = (r: HookRule) => pyStr(r.id).toLowerCase()
export const labelOf = (r: HookRule) => pyStr(r._label || r.id).toLowerCase()

/** `named_rules(label, rules, pool)`: an exact id anywhere in the book wins outright. */
export function namedRules(label: string, rules: readonly HookRule[], pool?: readonly HookRule[]): HookRule[] {
  const p = pool ?? rules
  if (rules.some((r) => idOf(r) === label)) return p.filter((r) => idOf(r) === label)
  return p.filter((r) => labelOf(r) === label)
}

/** `resolve_dismissals(rules, dismissals)` → [[rule, why]], [[label, n]] */
export function resolveDismissals(
  rules: readonly HookRule[],
  dismissals: ReadonlyMap<string, string>,
): [[HookRule, string][], [string, number][]] {
  const done: [HookRule, string][] = []
  const ambiguous: [string, number][] = []
  for (const [label, why] of dismissals) {
    if (!label || !why) continue
    const hits = namedRules(label, rules)
    if (hits.length > 1) ambiguous.push([label, hits.length])
    else if (hits.length) done.push([hits[0]!, why])
  }
  return [done, ambiguous]
}

/** `_dismissal_lines(set_aside, ambiguous)` → (agent lines, user lines) */
export function dismissalLines(setAside: readonly [string, string][], ambiguous: readonly [string, number][]): [string[], string[]] {
  const agent = setAside.map(([label, why]) => `_Recorded: [${label}] set aside — ${why}_`)
  const user = setAside.map(([label, why]) => `${BRAND} ▸ [${label}] set aside: ${why}`)
  for (const [label, n] of ambiguous) {
    agent.push(`_\`[${label}]\` fits ${n} rules with a fire pending — nothing recorded; name one by its rule id instead_`)
    user.push(`${BRAND} ▸ [${label}] fits ${n} rules — name one by its rule id`)
  }
  return [agent, user]
}
