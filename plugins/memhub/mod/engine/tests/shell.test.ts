// The shell layer replayed against Python's answers (vectors/shell.ts and
// vectors/shell_corpus.ts, generated for the run by scripts/test-mod.sh with scripts/rule_vectors.py from
// scripts/rule_vector_cases/shell.py and shell_corpus.py).
import { test, expect } from 'claude-code/testing'
import { VECTORS as OWN, type Vector } from './vectors/shell'
import { VECTORS as CORPUS } from './vectors/shell_corpus'
import {
  anchorHandle, anchorHits, anchorPattern, bashReads, blankQuoted, blankSyntax, cdPrefixTarget,
  commandFires, fnmatch, headTailLines, judgeCallText, judgeMatchedOn, judgeRedact, lastSegment,
  logicalLines, pathInScope, redactEchoes, redactIdentities, redactSecrets, redactText,
  redactedTokens, redirectBase, sedLines, segmentOp, shellOnly, shlexSplit, stripComments,
  stripLeadingAssignments, tokens, trimTerminators, under,
  andOnlySegments, executes, receiptSegments, selfDischarging, unquoted,
} from '../shell'
import { belowRatchet, pyKind } from './vector_checks'

const HOME = '/home/vector'

/** Python `re` flag ints as pyre letters. */
const flagLetters = (f: unknown): string => {
  if (f === undefined) return 'im'
  const n = Number(f)
  return (n & 2 ? 'i' : '') + (n & 8 ? 'm' : '') + (n & 16 ? 's' : '')
}

/** A Python set came back as a list sorted by its JSON text; compare as numbers. */
const numSorted = (v: unknown): unknown => {
  if (!Array.isArray(v)) return v
  const [n, used] = v
  return [n, Array.isArray(used) ? [...used].sort((a, b) => a - b) : used]
}

type Facts = {
  stat: { uid: number; mode: number; mtime: number } | null
  uid: number
  now: number
  text: string | null
  dirs?: string[]
  real?: Record<string, string>
}

const PORTS: Record<string, (a: any[], k: Record<string, any>) => unknown> = {
  shell_only: ([s]) => shellOnly(s),
  last_segment: ([s]) => lastSegment(s),
  strip_comments: ([s]) => stripComments(s),
  strip_leading_assignments: ([s]) => stripLeadingAssignments(s),
  blank_quoted: ([s]) => blankQuoted(s),
  blank_syntax: ([s]) => blankSyntax(s),
  trim_terminators: ([s]) => trimTerminators(s),
  _logical_lines: ([s]) => logicalLines(s),
  _tokens: ([s]) => tokens(s),
  shlex_split: ([s]) => shlexSplit(s),
  _segment_op: ([t]) => segmentOp(t),
  bash_reads: ([cwd, cmd]) => bashReads(cwd, cmd, HOME),
  _head_tail_lines: ([args]) => headTailLines(args),
  _sed_lines: ([args]) => sedLines(args),
  command_fires: ([rx, text], k) => commandFires(rx, text, k.not_rx, flagLetters(k.flags)),
  redact_secrets: ([s]) => redactSecrets(s),
  redact_text: ([s]) => redactText(s),
  redact_identities: ([s, home]) => redactIdentities(s, home ?? HOME),
  judge_redact: ([s]) => judgeRedact(s, HOME),
  redacted_tokens: ([o, r]) => redactedTokens(o, r),
  redact_echoes: ([o, r]) => redactEchoes(o, r),
  anchor_handle: ([s]) => anchorHandle(s),
  judge_call_text: ([s]) => judgeCallText(s, HOME),
  // The shim returns `anchor_rx(a).pattern`: the Python source pyre compiles.
  anchor_rx: ([a]) => anchorPattern(a),
  and_only_segments: ([s]) => andOnlySegments(s),
  receipt_segments: ([s], k) => receiptSegments(s, k.whole_chain ?? false),
  unquoted: ([s]) => unquoted(s),
  executes: ([seg, rx]) => executes(seg, rx),
  self_discharging: ([s, spec]) => selfDischarging(s, spec),
  anchor_hits: ([rule, text]) => anchorHits(rule, text),
  fnmatch: ([name, pat]) => fnmatch(name, pat),
  path_in_scope: ([rule, path], k) => pathInScope(rule, path, k.root ?? ''),
  judge_matched_on: ([rule, ev, handle, root]) => judgeMatchedOn(rule, ev, handle, root),
  _under: ([p, b]) => under(p, b),
  cd_prefix_target: ([c]) => cdPrefixTarget(c),
  repo_of_call_cd: ([c], k) => (k.tool_name ?? 'Bash') === 'Bash' && cdPrefixTarget(c) !== null,
  _redirect_base: ([cwd, f]: [string, Facts]) => {
    let data: unknown
    if (f.text !== null) {
      try {
        data = JSON.parse(f.text)
      } catch {
        data = undefined
      }
    }
    return redirectBase(cwd, {
      stat: f.stat ?? undefined,
      uid: f.uid,
      now: f.now,
      data,
      isDir: (p) => (f.dirs ?? []).includes(p),
      realpath: (p) => f.real?.[p] ?? p,
    })
  },
}

const NORMALISE: Record<string, (v: unknown) => unknown> = {
  _sed_lines: numSorted,
  _head_tail_lines: numSorted,
}

function replay(v: Vector): string | null {
  const port = PORTS[v.fn]
  if (!port) return `no port for ${v.fn}`
  let got: unknown
  try {
    got = port(v.args as any[], v.kwargs)
  } catch (e) {
    if (v.raises) return pyKind(e) === v.raises ? null : `threw ${pyKind(e)}; Python raised ${v.raises}`
    return `threw ${(e as Error).message} (Python: ${JSON.stringify(v.out)})`
  }
  if (v.raises) return `returned ${JSON.stringify(got)}; Python raised ${v.raises}`
  const norm = NORMALISE[v.fn] ?? ((x: unknown) => x)
  const a = JSON.stringify(norm(got))
  const b = JSON.stringify(norm(v.out))
  return a === b ? null : `got ${a}\n   want ${b}`
}

const byFn = new Map<string, Vector[]>()
for (const [layer, vectors] of [['shell', OWN], ['shell_corpus', CORPUS]] as const) {
  for (const v of vectors) {
    const key = `${layer}: ${v.fn}`
    byFn.set(key, [...(byFn.get(key) ?? []), v])
  }
}

test('every shell vector has a port', () => {
  expect([...OWN, ...CORPUS].map((v) => v.fn).filter((fn) => !PORTS[fn])).toEqual([])
  // Recorded counts (vector_checks.ts): raise them when cases are added.
  expect([belowRatchet('shell', OWN.length, 5561), belowRatchet('shell_corpus', CORPUS.length, 4400)])
    .toEqual([null, null])
})

for (const [fn, vs] of byFn) {
  test(`${fn} (${vs.length} vectors)`, () => {
    const bad: string[] = []
    for (const v of vs) {
      const why = replay(v)
      if (why) bad.push(`${fn}${JSON.stringify(v.args).slice(0, 160)} ${JSON.stringify(v.kwargs)}\n   ${why}`)
    }
    expect(bad.slice(0, 15), `${bad.length} of ${vs.length} differ`).toEqual([])
  })
}
