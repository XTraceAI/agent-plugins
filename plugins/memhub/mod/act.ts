// The act side: what the mod DOES with an engine Verdict (spec §4.6, §4.7,
// §4.15). The engine decides; this file denies, adds context, discloses,
// feeds the companion and writes the ledger.
//
// For each fire:
//   (a) `$.state` `fires` gets a FireNote (newest last, capped at 50) — the
//       companion's feed, replacing its regex over hook output (W7).
//   (b) the disclosure. The person sees the line as a dim transcript row
//       (`$.ui.log`), the mod path's stand-in for the Python hook's
//       `systemMessage`. Gate G2 (spec §8.4) has passed: MemHub-Backend
//       stores a disclosure `system` row as a role="system" message (#1493),
//       so the system row is on (SYSTEM_ROW_DISCLOSURE). With it off (Act's
//       `systemRow` = false, the Python path's behaviour), the model is told
//       to echo the line with rulebook_hook.py's exact
//       `disclosure_instruction` text. With it on, each
//       fire is one transcript `system` row the model never reads,
//       and the echo instruction is dropped for every verdict whose rows
//       were all written; a row that could not be written keeps the echo.
//       The first such fire of a session (and of each compaction) also
//       writes NO_ECHO_NOTE, lifting the posture preamble's own "you MUST
//       disclose" sentence.
//   (c) the ledger: fire rows go to `rulebook_mod_cli.py log` at once (one
//       process per fire, D6); per-call event rows wait in memory for
//       `turn.complete`.
//   (d) health: a lane error shows on the status line and in `$.state`
//       `health`; the next success clears both.

import type { FireNote } from '../types'
import type { StatusLine } from './claims'
import type { Ctx, Io } from './ctx'
import { MOD_CLI } from './ctx'
import type { Verdict } from './engine'

/**
 * D5: one `system` row per fire in the transcript (stored as
 * `{type:'system', subtype:'informational', content:<line>}`), which capture
 * uploads and the model never reads, in place of the model's echo. On since
 * gate G2 (spec §8.4) passed: MemHub-Backend stores the row as a
 * `role="system"` message (#1493, `claude_parts.is_disclosure_record`), so a
 * stored session keeps the line without the model writing it. The Python
 * path (Codex, Cursor, the fallback) still asks the model to echo.
 */
export const SYSTEM_ROW_DISCLOSURE = true

/**
 * With the system row on, the model is still carrying the posture preamble's
 * "When one fires, you MUST disclose it …" (Python's SessionStart serves the
 * startup session before the mod can claim it, and the mod's own preamble is
 * byte-identical). Measured live (2.1.293): without this note the model still
 * opens its reply with `📏 Rule fired:` lines — paraphrased, because it never
 * saw the exact line. One `user` row per session (again after a compaction)
 * lifts that instruction; it costs one row, not one per fire.
 */
export const NO_ECHO_NOTE =
  '_MemHub now shows each rule fire to the user and records it in the transcript itself. ' +
  'Do not write `📏 Rule fired:` or `⛔️ Rule fired:` lines in your replies: that part of ' +
  "the Rulebook's session-start instruction no longer applies. Follow the rules themselves as before._"

export const FIRES_CAP = 50
/** Rows kept for a ledger that could not be written, before the oldest drop. */
const PENDING_CAP = 500

/** rulebook_hook.py `disclosure_instruction(lines)`, byte for byte. */
export function disclosureInstruction(lines: readonly string[]): string {
  const quoted = lines.join('\n')
  return (
    '\n_Disclose these to the user. Begin your next reply with the following ' +
    'line(s), verbatim and each on its own line, before anything else — including ' +
    'before any tool call narration:_\n' +
    quoted +
    '\n_This is how the team sees its rules working. Do not paraphrase, do not merge ' +
    'them into a sentence, and do not omit one because it did not change what you were ' +
    'going to do — a rule that fired and changed nothing is exactly the rule the team ' +
    'needs to hear about._'
  )
}

const MARKER = /^\s*(📏|⛔️?)\s*Rule fired:[^\S\n]*/u

/** The rule as the line names it: the disclosure line without its marker. */
export const ruleOfLine = (line: string) => line.replace(MARKER, '').trim()
const isBlockedLine = (line: string) => /^\s*⛔/u.test(line)

/**
 * Which ledger rows are events (`log_event`: `event_id` + `kind`) rather than
 * fires (`log_fires`: `fire_id`). The engine hands both in `Verdict.ledger`.
 */
export const isEventRow = (row: Record<string, unknown>) =>
  typeof row.event_id === 'string' || (typeof row.kind === 'string' && !('fire_id' in row))

export class Act {
  private events: Record<string, unknown>[] = []
  private fires: Record<string, unknown>[] = []
  private notes: Promise<void> = Promise.resolve()
  /** Verdicts whose every fire is a transcript system row: the model gets no echo instruction. */
  private disclosed = new WeakSet<Verdict>()
  /** Sessions whose conversation holds NO_ECHO_NOTE (cleared on compaction). */
  private noted = new Set<string>()
  /** A note write in flight, per session: parallel tool calls share it, so a session gets one note. */
  private noting = new Map<string, Promise<boolean>>()
  /** The lines of a verdict whose system row failed (its note was written): only those are echoed. */
  private unwritten = new WeakMap<Verdict, string[]>()

  constructor(
    private io: Io,
    private ctx: Ctx,
    private status: StatusLine,
    /** D5's transcript system row in place of the echo; SYSTEM_ROW_DISCLOSURE unless a test says. */
    private systemRow: boolean = SYSTEM_ROW_DISCLOSURE,
  ) {}

  /** (a)–(c) for one verdict. Never throws: acting must not cost the call. */
  async record(v: Verdict | undefined, sessionId: string): Promise<void> {
    try {
      await this.recordOnce(v, sessionId)
    } catch (err) {
      // Anything unforeseen (a synchronous `$` call that throws) is a health
      // line, never the call's failure.
      this.unhealthy('ledger', err)
    }
  }

  private async recordOnce(v: Verdict | undefined, sessionId: string): Promise<void> {
    if (!v) return
    const at = await this.io.now().catch(() => 0)
    if (v.fires.length) {
      const notes: FireNote[] = v.fires.map(f => ({
        ruleId: f.ruleId,
        line: f.line,
        rule: ruleOfLine(f.line) || f.text,
        isBlocked: isBlockedLine(f.line),
        at,
      }))
      await this.addNotes(notes)
      const noted = this.systemRow && (await this.noteOnce(sessionId))
      let written = noted
      const failed: string[] = []
      for (const f of v.fires) {
        // `$.ui.log` is synchronous and may throw: the line is best effort.
        try {
          this.io.log(f.line)
        } catch {
          // the transcript copy (the system row, or the model's echo) remains
        }
        if (!this.systemRow) continue
        try {
          await this.io.append('system', f.line)
        } catch (err) {
          // No transcript copy for this fire: the verdict keeps the echo
          // instruction, so the line is not lost, and the person is told.
          written = false
          failed.push(f.line)
          this.unhealthy('disclosure', err)
        }
      }
      if (written) {
        this.disclosed.add(v)
        this.healthy('disclosure')
      } else if (noted && failed.length) {
        // The note is in place, so the model echoes only what we asked it to:
        // just the lines with no row, never one the transcript already holds.
        this.unwritten.set(v, failed)
      }
    }
    for (const row of v.ledger) (isEventRow(row) ? this.events : this.fires).push(row)
    // One process per fire, never per call: events alone wait for the turn's end.
    if (this.fires.length) await this.flush(sessionId)
  }

  /**
   * The text the model reads for a verdict: its context, then the echo
   * instruction — unless `record` wrote every fire as a transcript system row.
   */
  modelText(v: Verdict | undefined): string | undefined {
    if (!v) return undefined
    const lines = [...v.context]
    if (v.fires.length && !this.disclosed.has(v)) {
      lines.push(disclosureInstruction(this.unwritten.get(v) ?? v.fires.map(f => f.line)))
    }
    const text = lines.join('\n')
    return text.trim() ? text : undefined
  }

  /**
   * A deny carries no `context` (ToolCallResult's deny arm), so the model's
   * whole copy rides the reason: the gate's own text, then the context and
   * (unless the fires are system rows) the echo instruction the Python hook
   * would have put in additionalContext.
   */
  denyText(v: Verdict): string {
    return [v.deny ?? '', this.modelText(v)].filter(Boolean).join('\n')
  }

  /**
   * NO_ECHO_NOTE into the session once. False when it could not be written:
   * the preamble's disclosure instruction then stands, so the verdict keeps
   * the verbatim echo instruction rather than leave the model to paraphrase.
   */
  private async noteOnce(sessionId: string): Promise<boolean> {
    if (this.noted.has(sessionId)) return true
    // Parallel tool calls each record a verdict: they share the one write in
    // flight, or a session would get the note once per concurrent call.
    const inFlight = this.noting.get(sessionId)
    if (inFlight) return inFlight
    const write = (async () => {
      try {
        await this.io.append('user', NO_ECHO_NOTE)
        this.noted.add(sessionId)
        return true
      } catch (err) {
        this.unhealthy('disclosure', err)
        return false
      } finally {
        this.noting.delete(sessionId)
      }
    })()
    this.noting.set(sessionId, write)
    return write
  }

  /** A compaction summarised the note away with the preamble: the next fire writes it again. */
  compacted(sessionId: string): void {
    this.noted.delete(sessionId)
  }

  /** Writes every waiting ledger row through the Python ledger (turn.complete, or a fire). */
  async flush(sessionId: string): Promise<void> {
    if (!this.fires.length && !this.events.length) return
    const fires = this.fires
    const events = this.events
    this.fires = []
    this.events = []
    try {
      const r = await this.io.run(['python3', `${this.ctx.root}/${MOD_CLI}`, 'log', '--env', this.ctx.env], {
        stdin: JSON.stringify({ session: sessionId, fires, events }),
        timeoutMs: 10_000,
      })
      if (r.exitCode !== 0) throw new Error(`ledger log exited ${r.exitCode}`)
      this.healthy('ledger')
    } catch (err) {
      // Kept for the next flush; bounded so a broken ledger cannot grow without end.
      this.fires = [...fires, ...this.fires].slice(-PENDING_CAP)
      this.events = [...events, ...this.events].slice(-PENDING_CAP)
      this.unhealthy('ledger', err)
    }
  }

  private failing = new Set<string>()

  /** (d) A lane (or the ledger) hit an error the person should know of. */
  unhealthy(what: string, err: unknown): void {
    this.failing.add(what)
    const why = err instanceof Error ? err.message : String(err)
    const line = `MemHub: ${what} — ${why}`.replace(/\s+/g, ' ').slice(0, 200)
    this.show(line)
  }

  /** (d) The next success clears it. */
  healthy(what: string): void {
    if (!this.failing.delete(what) || this.failing.size) return
    this.show(undefined)
  }

  /** The health line on the status line and in `$.state`. Never throws: it runs in failure paths. */
  private show(line: string | undefined): void {
    try {
      this.status.set('health', line)
    } catch {
      // `$.ui.status` is synchronous; a refused line is not the call's failure
    }
    try {
      void this.io.setState('health', line ?? '').catch(() => undefined)
    } catch {
      // as above
    }
  }

  private addNotes(notes: FireNote[]): Promise<void> {
    // One read-modify-write at a time: parallel tool calls fire together.
    this.notes = this.notes.then(async () => {
      try {
        const { value = [] } = await this.io.getState('fires')
        await this.io.setState('fires', [...value, ...notes].slice(-FIRES_CAP))
      } catch {
        // the companion's feed is best effort
      }
    })
    return this.notes
  }
}
