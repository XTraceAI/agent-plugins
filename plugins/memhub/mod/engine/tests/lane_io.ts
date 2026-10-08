// What a lane replay compares and how the fake world keeps the shared state:
// used by lanes.test.ts (the hand-written lane vectors) and replay_parity.ts
// (the same machinery fed from real transcripts, scripts/replay_parity.py).

import { disclosureInstruction } from '../../act'
import { scopeOk } from '../engine'
import type { HookRule, StateOp, Verdict } from '../index'
import { armsOn, OrderingEngine } from '../ordering'
import { sessionFileName, worktreeKey } from '../repo'

export type Dict = Record<string, unknown>

/** The files the fake `state` op reads and writes (the rulebook base, in memory). */
export type StateFiles = {
  base: string
  readJson(path: string): unknown
  put(path: string, text: string): void
}

const sessionPath = (f: StateFiles, session: string) => `${f.base}/state/${sessionFileName(session)}`

/** `load_state`'s armings defaults over the session file. */
export function sessionDoc(f: StateFiles, session: string): Dict {
  const d = f.readJson(sessionPath(f, session))
  const st: Dict = d && typeof d === 'object' ? { ...(d as Dict) } : {}
  st.armed ??= {}
  st.armed_version ??= {}
  st.armed_once ??= []
  return st
}

/** scripts/rulebook_mod_state.py, as the Python functions it calls behave. */
export function applyStateOp(f: StateFiles, op: StateOp): Dict {
  if (op.op === 'feed') {
    const path = `${f.base}/state/wt-${worktreeKey(op.root)}.json`
    const rule = JSON.parse(JSON.stringify(op.rule)) as HookRule
    const eng = new OrderingEngine({
      lock: () => true,
      unlock: () => {},
      read: () => f.readJson(path),
      write: (st) => f.put(path, JSON.stringify(st)),
    })
    const outcome = eng.feed(rule, { hookPhase: op.hook_phase, tool: op.tool, cmd: op.cmd, filePath: op.file_path, ok: op.ok, armed: op.armed })
    return { outcome, gate_msg: rule._gate_msg ?? null, legacy_fires: rule._legacy_fires ?? null }
  }
  if (op.op === 'arm') {
    const arming = op.rules.filter((r) => ((r.status ?? 'active') === 'active') && scopeOk(r, op.repo, op.gitdir) && armsOn(r, op.event, op.prompt))
    if (!arming.length) return { armed: [] }
    const st = sessionDoc(f, op.session)
    const armed: string[] = []
    for (const r of arming) {
      if (op.event === 'session') {
        const once = `session:${r.id}`
        if ((st.armed_once as string[]).includes(once)) continue
        ;(st.armed_once as string[]).push(once)
      }
      const a = st.armed as Dict
      if (!(r.id in a)) a[r.id] = op.event
      ;(st.armed_version as Dict)[r.id] = r._version ?? null
      armed.push(r.id)
    }
    f.put(sessionPath(f, op.session), JSON.stringify(st))
    return { armed }
  }
  if (op.op === 'drop') {
    const st = sessionDoc(f, op.session)
    for (const rid of op.rule_ids) {
      delete (st.armed as Dict)[rid]
      delete (st.armed_version as Dict)[rid]
    }
    f.put(sessionPath(f, op.session), JSON.stringify(st))
    return {}
  }
  return { error: 'specs: not in the lane vectors' }
}

/** A row as rulebook_mod_cli.py `log` (→ log_fires) writes it, minus fire_id/host/source_message_id. */
export function fireAsWritten(row: Dict, session: string): Dict {
  const out: Dict = {
    rule_id: row.rule_id,
    rulebook_id: row.rulebook_id ?? null,
    rule_version: row.rule_version ?? '',
    session_id: session,
    agent_id: row.agent_id ?? null,
    worktree: row.worktree ?? null,
    repo: row.repo ?? null,
    branch: row.branch ?? null,
    tool: row.tool ?? null,
    hook_phase: row.hook_phase,
    mode: row.mode,
    dedup_key: row.dedup_key ?? null,
    raw_matches_before_fire: row.raw_matches_before_fire ?? null,
    fired_at: row.fired_at,
    override_reason: row.override_reason ?? null,
    excerpt: String(row.excerpt ?? '').slice(0, 160),
  }
  if (row.judge_verdict !== undefined && row.judge_verdict !== null) {
    out.judge_score = row.judge_score ?? null
    out.judge_verdict = row.judge_verdict
  }
  return out
}

/** An event as `log` (→ log_event) writes it, minus event_id/host. */
export function eventAsWritten(row: Dict, session: string): Dict {
  return {
    kind: row.kind,
    rule_id: row.rule_id ?? null,
    session_id: session,
    agent_id: row.agent_id ?? null,
    worktree: row.worktree ?? null,
    repo: row.repo ?? null,
    branch: row.branch ?? null,
    reason: row.reason ?? null,
    at: row.at,
  }
}

export const isEvent = (r: Dict) => typeof r.kind === 'string' && !('fire_id' in r)

/** act.ts `modelText`: the context, then the echo instruction. */
export function modelText(v: Verdict): string | undefined {
  const lines = [...v.context]
  if (v.fires.length) lines.push(disclosureInstruction(v.fires.map((f) => f.line)))
  const t = lines.join('\n')
  return t.trim() ? t : undefined
}

export const sortedArgv = (xs: readonly (readonly string[])[]) => [...new Set(xs.map((x) => JSON.stringify(x)))].sort()
