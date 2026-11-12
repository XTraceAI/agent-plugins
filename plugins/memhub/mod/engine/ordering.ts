// The ordering engine: a port of rulebook_hook.py's `OrderingEngine` (the
// obligation state machine: an edit arms, a green receipt discharges, the
// gated command fires while armed), plus `arms_on` and `session_scoped`.
//
// Pure: the per-worktree state object lives behind OrderingStore, which the
// lane layer backs with whatever holds `state/wt-<worktree_key>.json`. The
// Python holds an exclusive flock around every read-modify-write and fails
// open (feed → None) past LOCK_WAIT_S; `lock()` returning false is that.
//
// Python name → TS name:
//   OrderingEngine(root, branch).feed(rule, hook_phase=, tool=, cmd=, file_path=, ok=, armed=)
//     → new OrderingEngine(store, shell?).feed(rule, { hookPhase, tool, cmd, filePath, ok, armed })
//   arms_on → armsOn          session_scoped → sessionScoped
//
// SHELL HELPERS. `feed` needs five of the shell layer's functions:
// shell_only, command_fires (shell.ts's contract) and executes,
// receipt_segments, self_discharging, which shell.ts does not declare yet.
// They are injected (OrderingShell). The default reads them off shell.ts by
// their camelCase names (executes, receiptSegments, selfDischarging); a name
// shell.ts does not export throws "shell.ts: <name> not ported yet", like the
// stub's own functions.

import type { HookRule } from './index'
import { pyCompile, pySearch } from './pyre'
import * as shellMod from './shell'
import { EDIT_TOOLS, cpSlice, get, isDict, pyIter, pySplitWs, pyStr, truthy } from './rules'

type Dict = Record<string, unknown>

export interface OrderingShell {
  shellOnly(cmd: string): string
  commandFires(rx: string, text: string, notRx?: string | null, flags?: string): boolean
  /** executes(segment, rx) */
  executes(segment: string, rx: string): boolean
  /** receipt_segments(shell, whole_chain) */
  receiptSegments(shell: string, wholeChain: boolean): string[]
  /** self_discharging(shell, spec) */
  selfDischarging(shell: string, spec: Readonly<Record<string, unknown>>): boolean
  /** unquoted(text) — command-armed rules only; shell.ts's when absent */
  unquoted?(text: string): string
  /** every segment, split outside quotes — command-armed rules only */
  allSegments?(shell: string): [string, string][]
}

const fromShellModule = (name: string) => (...args: unknown[]): never => {
  const f = (shellMod as unknown as Record<string, unknown>)[name]
  if (typeof f !== 'function') throw new Error(`shell.ts: ${name} not ported yet`)
  return (f as (...a: unknown[]) => never)(...args)
}

/** shell.ts's functions, looked up at call time. */
export const DEFAULT_SHELL: OrderingShell = {
  shellOnly: (cmd) => shellMod.shellOnly(cmd),
  commandFires: (rx, text, notRx, flags) => shellMod.commandFires(rx, text, notRx, flags),
  executes: fromShellModule('executes'),
  receiptSegments: fromShellModule('receiptSegments'),
  selfDischarging: fromShellModule('selfDischarging'),
}

/** One obligation's slot: `{count, last_edit}`, plus the legacy fire ids
 *  older builds kept here. */
export type ObligationSlot = {
  count: number
  last_edit: string | null
  open_fire?: string
  open_fires?: string[]
  resolved_fires?: string[]
  [k: string]: unknown
}

/** The worktree state file: `{"*": {rule_id: slot}}`. */
export type OrderingState = Record<string, Record<string, ObligationSlot>>

/** The worktree's state, held for one read-modify-write. */
export interface OrderingStore {
  /** Take the exclusive lock; false = timed out (feed fails open → null). */
  lock(): boolean
  unlock(): void
  /** The state as stored, or undefined when absent/unreadable (→ `{}`).
   *  The engine mutates what it gets, so hand out a copy. */
  read(): unknown
  write(state: OrderingState): void
}

export type FeedEvent = {
  hookPhase: 'pre' | 'post' | string
  tool: string
  cmd?: string
  filePath?: string
  /** `bash_ok(...)` of the post-call result; only `true` discharges. */
  ok?: boolean | null
  /** "session" | "prompt" when the SESSION's own state armed this rule. */
  armed?: string | null
  /** The call did not fail (lenient `bash_ok`): only then does a command arm. */
  ran?: boolean | null
}

export type FeedOutcome = 'fired' | 'allowed' | 'discharged' | null

/** `pyName`: the Python exception this stands for, which the golden-vector
 *  tests compare against a `raises` vector's recorded name. */
class PyTypeError extends TypeError {
  readonly pyName = 'TypeError'
}

class PyKeyError extends Error {
  readonly pyName = 'KeyError'
}

/** `int(x)` for the values a `min_edits` can hold off the wire. */
function pyInt(v: unknown): number {
  if (typeof v === 'boolean') return Number(v)
  if (typeof v === 'number' && Number.isFinite(v)) return Math.trunc(v)
  if (typeof v === 'string' && /^\s*[+-]?\d+(_\d+)*\s*$/.test(v)) return Number(v.replace(/_/g, ''))
  throw new PyTypeError(`int() argument: ${typeof v}`)
}

const ARMING_DEFAULT = ['edit', 'write']

/** `tuple(spec.get("armed_by_events", ("edit", "write")))` */
function armedBy(spec: Dict): unknown[] {
  if (!Object.prototype.hasOwnProperty.call(spec, 'armed_by_events')) return ARMING_DEFAULT
  return pyIter(spec.armed_by_events ?? null)
}

export class OrderingEngine {
  /** Branch is recorded on fires, never a key: every rule lives under "*". */
  readonly branch = '*'

  constructor(private readonly store: OrderingStore, private readonly sh: OrderingShell = DEFAULT_SHELL) {}

  /** Returns "fired" | "allowed" | "discharged" | null; mutates the state
   *  under lock (null on lock timeout: fail open). Sets `rule._gate_msg` on
   *  "fired" and `rule._legacy_fires` on "discharged", as Python does.
   *  Throws where Python raises (a rule with no `ordering`, a junk
   *  `armed_by_events`/`min_edits`); the lane catches. */
  feed(rule: HookRule | Dict, e: FeedEvent): FeedOutcome {
    const r = rule as Dict
    const cmd = e.cmd ?? ''
    const filePath = e.filePath ?? ''
    if (!Object.prototype.hasOwnProperty.call(r, 'ordering')) throw new PyKeyError("KeyError: 'ordering'")
    const spec = r.ordering as Dict
    if (!isDict(spec)) throw new PyTypeError('ordering is not a dict')
    const by = armedBy(spec)
    const byCall = ['session', 'prompt'].some((k) => by.includes(k))
    const isEdit = EDIT_TOOLS.includes(e.tool) && e.hookPhase === 'post'
    if (isEdit && !['edit', 'write'].some((k) => by.includes(k))) return null
    if (isEdit && truthy(get(spec, 'path_rx')) && !pySearch(req(spec, 'path_rx'), filePath)) return null
    const seg = cmd ? this.sh.shellOnly(cmd) : ''
    const isReceipt = e.hookPhase === 'post' && e.tool === 'Bash' && !!seg &&
      this.sh.receiptSegments(seg, byCall).some((part) => this.sh.executes(part, req(spec, 'required_command_rx')))
    const isGate = e.hookPhase === 'pre' && e.tool === 'Bash' && !!seg &&
      this.sh.commandFires(req(spec, 'gated_command_rx'), seg, null, '')
    if (isGate && byCall && this.sh.selfDischarging(seg, spec)) return null
    const cmdLane = by.includes('command') && !!seg
    const arm = cmdLane && e.ran === true && e.hookPhase === 'post' ? this.commandArms(seg, spec) : ''
    if (!(isEdit || isReceipt || isGate || arm)) return null

    if (!this.store.lock()) return null
    try {
      const raw = this.store.read()
      const st = (raw === undefined ? {} : raw) as Dict
      if (!isDict(st)) throw new PyTypeError('state is not a dict')
      if (!Object.prototype.hasOwnProperty.call(st, this.branch)) st[this.branch] = {}
      const bucket = st[this.branch]
      if (!isDict(bucket)) throw new PyTypeError('state bucket is not a dict')
      const rid = pyStr(r.id ?? null)
      if (!Object.prototype.hasOwnProperty.call(bucket, rid)) bucket[rid] = { count: 0, last_edit: null }
      const s = bucket[rid] as Dict
      if (isEdit || arm) {                                // handler 1: mutation arms
        const c = s.count
        if (typeof c !== 'number' && typeof c !== 'boolean') throw new PyTypeError('count')
        s.count = Number(c) + 1
        if (isEdit) {
          s.last_edit = filePath
          delete s.last_cmd
        } else {
          s.last_cmd = arm
        }
        this.store.write(st as OrderingState)
        return null
      }
      if (isReceipt) {                                    // handler 2: green receipt
        if (e.ok === true) {
          s.count = 0
          const legacy = [get(s, 'open_fire'), ...pyIter(truthy(get(s, 'open_fires')) ? s.open_fires : [])]
            .filter(truthy)
          r._legacy_fires = [...new Set(legacy)]
          for (const k of ['open_fire', 'open_fires', 'resolved_fires', 'last_cmd']) delete s[k]
          this.store.write(st as OrderingState)
          return 'discharged'
        }
        return null
      }
      // handler 3: the gate — read-only
      const name = Object.prototype.hasOwnProperty.call(spec, 'display_name') ? spec.display_name ?? null : r.id ?? null
      const minEdits = pyInt(Object.prototype.hasOwnProperty.call(spec, 'min_edits') ? spec.min_edits ?? null : 1)
      if (typeof s.count !== 'number' && typeof s.count !== 'boolean') throw new PyTypeError('count')
      if (Number(s.count) >= minEdits) {
        r._gate_msg = `${pyStr(s.count)} edit(s) since the last passing '${pyStr(name)}' ` +
          `(last: ${pyStr(get(s, 'last_edit'))}). Run it first.`
        if (truthy(get(s, 'last_cmd'))) {
          r._gate_msg = `${pyStr(s.count)} change(s) since the last passing '${pyStr(name)}' ` +
            `(last: ran \`${pyStr(s.last_cmd)}\`). Run it first.`
        }
        return 'fired'
      }
      if (cmdLane) {
        // rulebook_moments.command_gate_msg: this call arms, then runs the gated command
        const now = this.armsBeforeGate(seg, spec)
        if (now && Number(s.count) + 1 >= minEdits) {
          r._gate_msg = `this call runs \`${now}\` before the gated command, with no passing ` +
            `'${pyStr(name)}' after it. Run it first.`
          return 'fired'
        }
      }
      if (truthy(e.armed)) {
        const since = e.armed === 'session' ? 'this session started' : 'your prompt armed this rule'
        r._gate_msg = `no passing '${pyStr(name)}' since ${since}. Run it first.`
        return 'fired'
      }
      return 'allowed'
    } finally {
      this.store.unlock()
    }
  }

  private unq(t: string): string {
    return this.sh.unquoted ? this.sh.unquoted(t) : shellMod.unquoted(t)
  }

  private pieces(seg: string): [string, string][] {
    return this.sh.allSegments ? this.sh.allSegments(seg) : shellMod.allSegments(seg)
  }

  /** rulebook_moments.command_arms: what a call that did not fail arms — any
   *  segment, whatever joins it; a required one after it clears only when the
   *  exit status vouches for it. */
  private commandArms(seg: string, spec: Dict): string {
    if (!commandArmed(spec)) return ''
    const rx = req(spec, 'armed_by_command_rx')
    const rq = req(spec, 'required_command_rx')
    const parts = this.pieces(seg).map(([, p]) => shellMod.pyStrip(p))
    let at = -1
    parts.forEach((part, i) => {
      if (this.sh.executes(part, rx)) at = i
    })
    if (at < 0) return ''
    const vouched = new Set(this.sh.receiptSegments(seg, true).map((p) => shellMod.pyStrip(p)))
    if (parts.slice(at + 1).some((p) => vouched.has(p) && this.sh.executes(p, rq))) return ''
    return armText(parts[at]!, rx, (t) => this.unq(t))
  }

  /** rulebook_moments._runs_gated: the segment read from its own start and
   *  with its separator before it, quoted spans blanked. */
  private runsGated(sep: string, part: string, gated: string): boolean {
    const text = this.unq(part)
    return this.sh.commandFires(gated, shellMod.pyStrip(text, 'left'), null, '') ||
      (sep !== '' && this.sh.commandFires(gated, sep + text, null, ''))
  }

  /** rulebook_moments.arms_before_gate: an arming segment, then the gated
   *  one, with no required segment between, in this one call. */
  private armsBeforeGate(seg: string, spec: Dict): string {
    if (!commandArmed(spec)) return ''
    const rx = req(spec, 'armed_by_command_rx')
    const rq = req(spec, 'required_command_rx')
    const gated = req(spec, 'gated_command_rx')
    let pending = ''
    for (const [sep, part] of this.pieces(seg)) {
      if (pending && this.runsGated(sep, part, gated)) return pending
      if (this.sh.executes(part, rx)) pending = armText(part, rx, (t) => this.unq(t))
      else if (this.sh.executes(part, rq)) pending = ''
    }
    return ''
  }
}

/** `command_armed(spec)`: arms on a command, with a pattern to arm on. */
function commandArmed(spec: Dict): boolean {
  return armedBy(spec).includes('command') && truthy(get(spec, 'armed_by_command_rx'))
}

/** `_arm_text(part, rx, unquoted)`: the matched text, whitespace folded, ≤60. */
function armText(part: string, rx: string, unq: (t: string) => string): string {
  const re = pyCompile(rx)
  if (re === null) throw new Error(`unportable pattern: ${rx}`)
  const m = re.exec(unq(part))
  return cpSlice(pySplitWs(m ? m[0] : part).join(' '), 0, 60)
}

/** `spec[k]` as a pattern (KeyError / TypeError where Python raises). */
function req(spec: Dict, k: string): string {
  const v = spec[k]
  if (typeof v !== 'string') throw new PyTypeError(`${k} is not a pattern`)
  return v
}

/** `session_scoped(rule)`: is the obligation the SESSION's (armed by the
 *  session or a prompt) rather than the checkout's? */
export function sessionScoped(rule: HookRule | Dict): boolean {
  const r = rule as Dict
  const o = get(r, 'on') === 'ordering' ? get(r, 'ordering') : null
  const spec = truthy(o) ? (o as Dict) : {}
  const by = armedBy(spec)
  return ['session', 'prompt'].some((k) => by.includes(k))
}

/** `arms_on(rule, event, prompt)`: does `event` arm this ordering rule? */
export function armsOn(rule: HookRule | Dict, event: string, prompt = ''): boolean {
  const r = rule as Dict
  if (get(r, 'on') !== 'ordering') return false
  const o = get(r, 'ordering')
  const spec = truthy(o) ? (o as Dict) : {}
  if (!armedBy(spec).includes(event)) return false
  if (event === 'prompt') {
    const rx = get(spec, 'armed_by_rx')
    return truthy(rx) && pySearch(req(spec, 'armed_by_rx'), prompt, 'i')
  }
  return true
}
