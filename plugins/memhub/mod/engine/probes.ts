// `given` facts, answered lazily and at most once per call: a port of
// rulebook_hook.py's Probes and given_ok over EngineIO.git, plus the two
// readers of the conversation — user_turns_of (given.user) and
// rule_judge_turn.read_turn (the judge's turn window) — over
// `$.session.messages({ as: "api" })` instead of the transcript file.

import type { EngineIO, TurnMessage } from './index'
import { pyStr, pySplitWs, pyStrip, cpSlice, isDict } from './rules'
import { pyRegExp } from './py'
import { pySearch } from './pyre'
import { join } from './shell'

type Dict = Record<string, unknown>

export const PROBE_TIMEOUT_MS = 1000
const TURNS_KEEP = 200
const TURN_CHARS = 2000
const BASE_ARG = String.raw`--base[=\s]+(?:'([^']*)'|"([^"]*)"|([^\s;&|]+))`
const BRANCH_NAME = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$/

/** One spec the branch left alone: its path and the changed paths it owns. */
export type SpecHit = [string, string[]]

export class Probes {
  private memo = new Map<string, Promise<unknown>>()

  constructor(
    private io: EngineIO,
    readonly root: string,
    private branchName: string,
    private cmd: string,
    private agentId: string | null,
    private turns: () => Promise<readonly TurnMessage[] | undefined>,
    private cwd: string,
    private baseBranchEnv: string,
  ) {}

  /** `_get`: memoized; any exception is None. */
  private get<T>(key: string, compute: () => Promise<T>): Promise<T | null> {
    let p = this.memo.get(key) as Promise<T | null> | undefined
    if (!p) {
      p = compute().then((v) => v, () => null)
      this.memo.set(key, p)
    }
    return p
  }

  /** `_git(*args)`: stdout on exit 0, null otherwise; rejects on a timeout (Python raises). */
  private async git(...args: string[]): Promise<string | null> {
    if (!this.root) return null
    const r = await this.io.git(['-C', this.root, ...args], this.root, PROBE_TIMEOUT_MS)
    return r.code === 0 ? r.stdout : null
  }

  branch(): Promise<string | null> {
    return this.get('branch', async () => this.branchName)
  }

  private async namedBase(): Promise<string> {
    const named = new Set<string>()
    const rx = pyRegExp(BASE_ARG, '', 'g')
    rx.lastIndex = 0
    for (let m = rx.exec(this.cmd); m; m = rx.exec(this.cmd)) named.add([m[1], m[2], m[3]].find((g) => g) ?? '')
    named.delete('')
    if (named.size !== 1) return ''
    const base = [...named][0]!
    if (!BRANCH_NAME.test(base) || base === 'HEAD') return ''
    if ((await this.git('rev-parse', '--verify', '-q', `refs/remotes/origin/${base}^{commit}`)) === null) return ''
    const mb = pyStrip((await this.git('merge-base', `origin/${base}`, 'HEAD')) ?? '')
    const head = pyStrip((await this.git('rev-parse', 'HEAD')) ?? '')
    return !mb || !head || mb === head ? '' : base
  }

  private async remoteHead(): Promise<string> {
    return pyStrip((await this.git('symbolic-ref', '--quiet', 'refs/remotes/origin/HEAD')) ?? '')
  }

  private async mergeBase(cand: string): Promise<string | null> {
    if ((await this.git('rev-parse', '--verify', '-q', cand + '^{commit}')) === null) return null
    const mb = await this.git('merge-base', cand, 'HEAD')
    return mb && pyStrip(mb) ? pyStrip(mb) : null
  }

  base(): Promise<string | null> {
    return this.get('base', async () => {
      const env = pyStrip(this.baseBranchEnv)
      const named = await this.namedBase()
      const explicit = env ? [env] : []
      if (named) explicit.push(`origin/${named}`, named)
      for (const cand of explicit) {
        const mb = await this.mergeBase(cand)
        if (mb) return mb
      }
      const head = pyStrip((await this.git('rev-parse', 'HEAD')) ?? '')
      const rh = await this.remoteHead()
      const usual = [...(rh ? [rh] : []), 'origin/main', 'origin/master', 'origin/develop', 'origin/staging',
        'main', 'master', 'develop', 'staging']
      let nearest: string | null = null
      let best: number | null = null
      let dflt: string | null = null
      for (const cand of usual) {
        const mb = await this.mergeBase(cand)
        if (!mb) continue
        if (dflt === null) dflt = cand
        if (mb === head && cand !== dflt) continue
        const out = pyStrip((await this.git('rev-list', '--count', `${mb}..HEAD`)) ?? '')
        if (!/^[+-]?\d+$/.test(out)) continue
        const n = Number(out)
        if (best === null || n < best) {
          nearest = mb
          best = n
        }
      }
      return nearest
    })
  }

  diffPaths(): Promise<string[] | null> {
    return this.get('diff_paths', async () => {
      const mb = await this.base()
      if (mb === null) return null
      const tracked = await this.git('diff', '--name-only', '--no-renames', mb)
      const untracked = await this.git('ls-files', '--others', '--exclude-standard')
      if (tracked === null || untracked === null) return null
      const set = new Set((tracked + '\n' + untracked).split('\n').map((l) => pyStrip(l)).filter(Boolean))
      return [...set].sort(pyCompare)
    })
  }

  /** `untouched_specs` and `active_spec_paths`, answered together by the Python (spec_owns). */
  private specs(specDir: string): Promise<{ active: string[]; untouched: SpecHit[] } | null> {
    return this.get('specs:' + specDir, async () => {
      const paths = await this.diffPaths()
      if (paths === null) return null
      const r = await this.io.state([{ op: 'specs', root: this.root, spec_dir: specDir, paths }], this.cwd)
      const got = r?.[0]
      if (!isDict(got) || 'error' in got || !Array.isArray(got.active) || !Array.isArray(got.untouched)) return null
      return { active: got.active as string[], untouched: got.untouched as SpecHit[] }
    })
  }

  async untouchedSpecs(specDir = 'docs/specs'): Promise<SpecHit[] | null> {
    return (await this.specs(specDir))?.untouched ?? null
  }

  /**
   * `active_spec_paths(spec_dir)`. Python loads the specs without needing the
   * diff; here they come with it, so a failed diff answers null too — the
   * only caller already has the diff in hand.
   */
  async activeSpecPaths(specDir = 'docs/specs'): Promise<Set<string> | null> {
    const s = await this.specs(specDir)
    return s ? new Set(s.active) : null
  }

  diffLines(): Promise<number | null> {
    return this.get('diff_lines', async () => {
      const mb = await this.base()
      if (mb === null) return null
      const out = await this.git('diff', '--numstat', mb)
      if (out === null) return null
      let n = 0
      for (const line of out.split('\n')) {
        const parts = line.split('\t')
        if (parts.length >= 2 && /^\d+$/.test(parts[0]!) && /^\d+$/.test(parts[1]!)) n += Number(parts[0]) + Number(parts[1])
      }
      const untracked = (await this.git('ls-files', '--others', '--exclude-standard')) ?? ''
      for (const p of untracked.split('\n').map((l) => pyStrip(l)).filter(Boolean).slice(0, 200)) {
        const text = await this.io.readText(join(this.root, p))
        if (text !== undefined) n += countNewlines(text.slice(0, 1 << 20))
      }
      return n
    })
  }

  dirty(): Promise<boolean | null> {
    return this.get('dirty', async () => {
      const out = await this.git('status', '--porcelain')
      return out === null ? null : pyStrip(out) !== ''
    })
  }

  userTurns(): Promise<string[] | null> {
    return this.get('user_turns', async () => {
      const msgs = await this.turns()
      return msgs ? userTurnsOf(msgs) : null
    })
  }

  agentMain(): Promise<boolean | null> {
    return this.get('agent_main', async () => this.agentId === null)
  }
}

/** Python's default `str` ordering: by code point. */
export const pyCompare = (a: string, b: string): number => {
  const A = Array.from(a)
  const B = Array.from(b)
  for (let i = 0; i < Math.min(A.length, B.length); i++) {
    const d = A[i]!.codePointAt(0)! - B[i]!.codePointAt(0)!
    if (d) return d
  }
  return A.length - B.length
}

const countNewlines = (s: string): number => {
  let n = 0
  for (let i = s.indexOf('\n'); i >= 0; i = s.indexOf('\n', i + 1)) n++
  return n
}

const num = (v: unknown): v is number => typeof v === 'number'
/** Python `have > v` for the JSON numbers a `given` holds (bool is an int). */
const gt = (have: unknown, v: unknown): boolean => {
  const a = typeof have === 'boolean' ? Number(have) : have
  const b = typeof v === 'boolean' ? Number(v) : v
  if (!num(a) || !num(b)) throw new TypeError("'>' not supported")
  return a > b
}
const pyEq = (a: unknown, b: unknown): boolean =>
  (typeof a === 'boolean' || typeof a === 'number') && (typeof b === 'boolean' || typeof b === 'number')
    ? Number(a) === Number(b)
    : a === b

const block = (g: Dict, k: string): Dict => {
  const b = g[k]
  return isDict(b) && Object.keys(b).length ? b : {}
}

/**
 * `given_ok(rule, probes, read)`: every predicate holds, probed lazily in
 * Python's order (a failing predicate stops the walk, so later probes never
 * run). Throws where Python raises; the caller treats that as Python's
 * main() would (it propagates).
 */
export async function givenOk(rule: Dict, probes: Probes, read: Dict | null = null): Promise<boolean> {
  const g = rule.given
  if (g === undefined || g === null || g === false || (isDict(g) && !Object.keys(g).length)) return true
  if (!isDict(g)) throw new TypeError("'given' is not a dict")
  for (const [k, v] of Object.entries(block(g, 'file'))) {
    const fk = k === 'lines_gt' ? 'lines' : k === 'bytes_gt' ? 'bytes' : ''
    const have = read && fk && Object.prototype.hasOwnProperty.call(read, fk) ? read[fk] : null
    if (have === null || have === undefined || !gt(have, v)) return false
  }
  for (const [k, v] of Object.entries(block(g, 'agent'))) {
    if (k === 'main') {
      const m = await probes.agentMain()
      if (m === null || !pyEq(m, v)) return false
    }
  }
  const repo = block(g, 'repo')
  for (const [k, v] of Object.entries(repo)) {
    if (k === 'branch_rx') {
      const b = await probes.branch()
      if (!b || !pySearch(pyStr(v), b)) return false
    } else if (k === 'branch_not_rx') {
      const b = await probes.branch()
      if (!b || pySearch(pyStr(v), b)) return false
    } else if (k === 'diff_lines_gt') {
      const n = await probes.diffLines()
      if (n === null || !gt(n, v)) return false
    } else if (k === 'diff_files_gt') {
      const ps = await probes.diffPaths()
      if (ps === null || !gt(ps.length, v)) return false
    } else if (k === 'diff_paths_rx') {
      const ps = await probes.diffPaths()
      if (ps === null || !ps.some((p) => pySearch(pyStr(v), p))) return false
    } else if (k === 'diff_paths_none_rx') {
      const ps = await probes.diffPaths()
      if (ps === null || ps.some((p) => pySearch(pyStr(v), p))) return false
    } else if (k === 'spec_untouched') {
      const dir = Object.prototype.hasOwnProperty.call(repo, 'spec_dir') ? pyStr(repo.spec_dir) : 'docs/specs'
      const u = await probes.untouchedSpecs(dir)
      if (u === null || !pyEq(u.length > 0, v)) return false
    } else if (k === 'dirty') {
      const d = await probes.dirty()
      if (d === null || !pyEq(d, v)) return false
    }
  }
  for (const [k, v] of Object.entries(block(g, 'user'))) {
    const turns = await probes.userTurns()
    if (turns === null) return false
    const said = turns.some((t) => pySearch(pyStr(v), t, 'i'))
    if ((k === 'said_rx' && !said) || (k === 'not_said_rx' && said)) return false
  }
  return true
}

// ── the conversation ────────────────────────────────────────────────────────

type Block = Record<string, unknown>
const blocksOf = (c: TurnMessage['content']): Block[] | null => (Array.isArray(c) ? (c as Block[]) : null)

/** harness_extract._text_of */
function textOf(content: unknown): string {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content
    .filter((b): b is Block => isDict(b) && b.type === 'text')
    .map((b) => (typeof b.text === 'string' ? b.text : ''))
    .join('\n')
}

/**
 * `user_turns_of(transcript)` over the messages: what the person typed,
 * oldest first, last 200, each cut to 2000. A message holding a tool result
 * is not a turn. (The transcript's `isMeta` / compaction-summary rows are not
 * told apart in the Messages API form; see the port notes in engine.ts.)
 */
export function userTurnsOf(msgs: readonly TurnMessage[]): string[] {
  const turns: string[] = []
  for (const m of msgs) {
    if (m.role !== 'user') continue
    let text: string
    if (typeof m.content === 'string') text = m.content
    else {
      const bs = blocksOf(m.content) ?? []
      if (bs.some((b) => isDict(b) && b.type === 'tool_result')) continue
      text = bs.filter((b) => isDict(b) && b.type === 'text').map((b) => (typeof b.text === 'string' ? b.text : '')).join('\n')
    }
    text = pyStrip(text)
    if (text) turns.push(cpSlice(text, 0, TURN_CHARS))
  }
  return turns.slice(-TURNS_KEEP)
}

// rule_judge_turn.read_turn, over the same messages.
export const MAX_ENTRIES = 200
const PERSON_CHARS = 2000
const AGENT_CHARS = 1500
const CALL_CHARS = 300
const RESULT_CHARS = 300
const SLACK = 4
const SYS_BLOCK = String.raw`<system-reminder>.*?</system-reminder>|<task-notification>.*?</task-notification>|<command-name>.*?</command-name>|<local-command-stdout>.*?</local-command-stdout>`
const HARNESS_PREFIX = [
  'MemHub harness: before you stop', 'MemHub harness fork', 'Another Claude session sent a message:',
  'Base directory for this skill', 'Continue from where you left off', 'Caveat: The messages below',
  'Skill /', 'This session is being continued from a previous conversation', '[Request interrupted by user',
  '[Your previous response had no visible output',
]
const HARNESS_TAG = String.raw`<(?:local-command-caveat|command-message|command-args|bash-input|bash-stdout|bash-stderr|user-prompt-submit-hook)\b`

const sysStrip = (s: string): string => s.replace(pyRegExp(SYS_BLOCK, 's', 'g'), '')

/** harness_extract.is_harness_text */
function isHarnessText(t: string): boolean {
  if (!t) return true
  if (HARNESS_PREFIX.some((p) => t.startsWith(p))) return true
  const rx = pyRegExp(HARNESS_TAG, '', 'y')
  rx.lastIndex = 0
  return rx.test(t)
}

export type JudgeEntry = { kind: string; text: string }
export type Turn = { turn_id: string; person_request: string; turn: JudgeEntry[] }

function cut(text: string, cap: number, redact: (s: string) => string): string {
  let t = cpSlice(text || '', 0, cap * SLACK)
  try {
    t = redact(t) || ''
  } catch {
    return ''
  }
  return cpSlice(t, 0, cap)
}

function add(entries: JudgeEntry[], kind: string, text: string, cap: number, redact: (s: string) => string) {
  const t = cut(text, cap, redact)
  if (t) entries.push({ kind, text: t })
}

/** json.dumps(i, default=str) for a tool input (Python's separators). */
function pyJson(v: unknown): string {
  if (v === null || v === undefined) return 'null'
  if (typeof v === 'string') return JSON.stringify(v).replace(/[\u0080-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'))
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v)) return '[' + v.map(pyJson).join(', ') + ']'
  if (isDict(v)) return '{' + Object.entries(v).map(([k, x]) => pyJson(k) + ': ' + pyJson(x)).join(', ') + '}'
  return pyJson(String(v))
}

/** rule_judge_turn._call_text */
function callText(name: string, input: unknown): string {
  const i = isDict(input) ? input : {}
  let what: string
  if (name === 'Bash') what = pyStr(i.command ?? '')
  else {
    what = typeof i.file_path === 'string' ? i.file_path : i.file_path ? pyStr(i.file_path) : ''
    if (!what) what = Object.keys(i).length ? pyJson(i) : ''
  }
  return `${name}: ${pySplitWs(what).join(' ')}`
}

/**
 * `read_turn` over `$.session.messages({ as: "api" })`: the last human turn,
 * oldest first. `turnId` stands in for the transcript uuid of the turn's
 * human message, which the Messages API form does not carry.
 */
export function readTurn(msgs: readonly TurnMessage[], turnId: string, redact: (s: string) => string): Turn {
  const empty: Turn = { turn_id: '', person_request: '', turn: [] }
  try {
    const groups: JudgeEntry[][] = []
    let kept = 0
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i]!
      const content = m.content
      if (m.role === 'assistant' && Array.isArray(content)) {
        if (kept >= MAX_ENTRIES) continue
        const entries: JudgeEntry[] = []
        for (const b of content as Block[]) {
          if (!isDict(b)) continue
          if (b.type === 'text' && pyStrip(pyStr(b.text ?? ''))) {
            add(entries, 'agent', pyStrip(sysStrip(pyStr(b.text))), AGENT_CHARS, redact)
          } else if (b.type === 'tool_use') {
            add(entries, 'call', callText(pyStr(b.name ?? ''), b.input), CALL_CHARS, redact)
          }
        }
        groups.push(entries)
        kept += entries.length
        continue
      }
      if (m.role !== 'user') continue
      const bs = blocksOf(content)
      if (bs && bs.some((b) => isDict(b) && b.type === 'tool_result')) {
        if (kept >= MAX_ENTRIES) continue
        const entries: JudgeEntry[] = []
        for (const b of bs) {
          if (!(isDict(b) && b.type === 'tool_result')) continue
          const body = pyStrip(sysStrip((typeof b.content === 'string' ? b.content : textOf(b.content)) || ''))
          const lines = cpSlice(body, 0, RESULT_CHARS * SLACK).split(/\r\n|\r|\n|\v|\f|\x1c|\x1d|\x1e|\x85|\u2028|\u2029/)
          if (lines.length && lines[lines.length - 1] === '') lines.pop()
          add(entries, 'result', lines.join(' | '), RESULT_CHARS, redact)
        }
        groups.push(entries)
        kept += entries.length
        continue
      }
      const text = pyStrip(sysStrip(textOf(content)))
      if (isHarnessText(text)) continue
      const person = cut(text, PERSON_CHARS, redact)
      const turn: JudgeEntry[] = person ? [{ kind: 'person', text: person }] : []
      for (let j = groups.length - 1; j >= 0; j--) turn.push(...groups[j]!)
      return { turn_id: turnId, person_request: person, turn: turn.slice(-MAX_ENTRIES) }
    }
    return empty
  } catch {
    return empty
  }
}
