// The harness's rule-drafting fork, started by the mod (lane `drafter`).
//
// The Python Stop hook judges every turn, as it always has. Without this
// module, a flagged turn makes it print a line ("MemHub: possible team rule
// spotted in turn N") that continues the agent, which runs `handoff`, launches
// an Agent fork, and is woken again when the fork finishes. The person sees
// all of it. With the `drafter` lane claimed, the Stop writes the fork prompt
// to its queue instead (harness_stop.py "mod drafter"), and at the turn's end
// this module spawns the fork with `$.agent.spawn` — nothing on screen, no
// tool call in the transcript, no completion notice to the agent:
//
//   turn.complete (main loop)
//     └─ python3 scripts/harness_stop.py queued      (this session's entries)
//     └─ each: $.agent.spawn({ subagentType: 'fork', prompt })
//          started → harness_stop.py ack --moment <ref> (the entry goes)
//          refused → release `drafter` for the session; the entries stay, and
//                    the next Stop, finding the lane unclaimed, prints their
//                    lines: the visible path, one turn late, nothing dropped
//   turn.complete (that fork) → its last line: "filed <title>" → one line to
//                    the person ($.ui.log); "none" / "failed" → debug only
//
// Measured on Claude Code 2.1.321 (2026-11-09), auto mode: the spawn is
// accepted, the fork's Bash, Write and memhub MCP calls run, it reads the
// parent's prompt cache (1.8k written over a 457k prefix), and the parent's
// screen and transcript show nothing of it. A fork continues the parent's own
// conversation (same model, system prompt and tools), so nothing that would
// change that prefix is passed: no model, no agent type of ours.
//
// Claimed only in an interactive session: a one-shot `claude -p` ends its
// session with the turn, before a spawn could be made (and has no `fork`
// type), so there the Stop keeps continuing the agent inside the turn.
// The `harnessDrafting` opt-out needs nothing here: Python then judges no
// turn, so nothing is ever queued.

import type { Claims } from './claims'
import type { Io } from './ctx'
import { lastJson } from './ctx'

export const HARNESS_CLI = 'scripts/harness_stop.py'
/** harness_stop.FORK_MARK: what the fork's prompt and its last line begin with. */
export const FORK_MARK = 'MemHub harness fork'
const FILED = /^MemHub harness fork: (?:filed|repaired) /
const RUN_TIMEOUT_MS = 15_000
/** A fork that never reports is forgotten after this long. */
const PENDING_MS = 60 * 60_000

/** What `$.agent.spawn` answered: the started agent's id, or why it started none. */
export type Spawned = { agentId?: string; deny?: string }

/** The `turn.complete` fields this module reads. */
export type TurnEnd = {
  agentId?: string
  isAborted: boolean
  reason: string
  answer: string
  usage?: { cache_read_input_tokens?: number | null; cache_creation_input_tokens?: number | null }
}

type Entry = { ref: string; prompt: string }

/** The last non-empty line of a fork's answer. */
export function lastLine(answer: string): string {
  return answer.split('\n').map(l => l.trim()).filter(Boolean).at(-1) ?? ''
}

export class Drafter {
  /** Forks this module spawned and has not heard from: agent id → moment. */
  private pending = new Map<string, { ref: string; since: number }>()
  /** One hand-off at a time, in turn order. */
  private queue: Promise<void> = Promise.resolve()

  constructor(
    private io: Io,
    private root: string,
    private claims: Claims,
    private spawn: (input: { prompt: string; description: string }) => Promise<Spawned>,
  ) {}

  /** session.start: claim the lane where a spawn can work (see the header). */
  async start(session: { isInteractive: boolean }): Promise<void> {
    if (!session.isInteractive) return
    await this.claims.claim(['drafter'])
  }

  /** Every `turn.complete`. Never holds the turn: the work is chained and returned. */
  turnEnded(e: TurnEnd): Promise<void> {
    if (e.agentId !== undefined) return this.forkEnded(e.agentId, e)
    if (!this.claims.has('drafter')) return Promise.resolve()
    this.queue = this.queue
      .then(() => this.take())
      .catch(err => this.io.debug(`MemHub harness: hand-off failed — ${messageOf(err)}`))
    return this.queue
  }

  /** Spawn a fork for each moment the Stop queued for this session. */
  private async take(): Promise<void> {
    const session = await this.io.sessionId()
    const r = await this.io.run(['python3', `${this.root}/${HARNESS_CLI}`, 'queued'], {
      stdin: JSON.stringify({ session_id: session }),
      timeoutMs: RUN_TIMEOUT_MS,
    })
    const got = lastJson(r.stdout) as { entries?: unknown } | undefined
    if (r.exitCode !== 0 || !got || !Array.isArray(got.entries)) return
    const entries = got.entries.filter(
      (x): x is Entry => !!x && typeof x === 'object' && typeof (x as Entry).ref === 'string' && typeof (x as Entry).prompt === 'string',
    )
    for (const entry of entries) {
      if (!this.claims.has('drafter')) return
      let spawned: Spawned
      try {
        spawned = await this.spawn({ prompt: entry.prompt, description: 'MemHub: draft a team rule' })
      } catch (err) {
        spawned = { deny: messageOf(err) }
      }
      if (!spawned.agentId) {
        // The entries stay queued; the next Stop finds the lane unclaimed and
        // hands them to the agent with its visible line.
        this.io.debug(`MemHub harness: fork for ${entry.ref} not started (${spawned.deny ?? 'no agent id'}); the Stop hook hands it over from here`)
        await this.claims.release(['drafter']).catch(() => undefined)
        return
      }
      const now = await this.io.now()
      for (const [id, p] of this.pending) if (now - p.since > PENDING_MS) this.pending.delete(id)
      this.pending.set(spawned.agentId, { ref: entry.ref, since: now })
      const ack = await this.io.run(['python3', `${this.root}/${HARNESS_CLI}`, 'ack', '--moment', entry.ref], { timeoutMs: RUN_TIMEOUT_MS })
      this.io.debug(`MemHub harness: drafting from ${entry.ref} in the background (${spawned.agentId}); ack exit ${ack.exitCode}`)
    }
  }

  /** A fork this module spawned ended a turn: act on its report's last line. */
  private async forkEnded(agentId: string, e: TurnEnd): Promise<void> {
    const p = this.pending.get(agentId)
    if (!p) return
    const last = lastLine(e.answer)
    if (!e.isAborted && !last.startsWith(FORK_MARK)) return // not its report yet
    this.pending.delete(agentId)
    const u = e.usage
    if (u) {
      this.io.debug(`MemHub harness: ${p.ref}: fork cache_read=${u.cache_read_input_tokens ?? 0} cache_creation=${u.cache_creation_input_tokens ?? 0}`)
    }
    if (FILED.test(last)) {
      // The one thing the person hears about a fork: a rule it filed. The
      // companion's own proposal poll lists it with Activate / Reject.
      this.io.log(last)
      return
    }
    this.io.debug(`MemHub harness: ${p.ref}: ${last || (e.isAborted ? 'fork interrupted' : 'fork ended without a report')}`)
  }
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}
