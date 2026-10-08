// The Rulebook lanes on mod events (spec §4.2–§4.5): each builds what the
// engine needs, asks it, and hands the Verdict to act.ts. A lane that is not
// claimed (claims.ts) is NOT evaluated here — Python serves it, and serving
// it twice would double-fire (outside the brief overlaps claims.ts accepts).
//
//   pre + post  tool.call (Bash, Edit, MultiEdit, Write, NotebookEdit, Read):
//               pre before `next`; a gate answers `{ deny }` WITHOUT `next`
//               (so nothing beneath runs, spec §1.2); otherwise
//               `r = await next(e)`, post on `postResultText(tool, r)`
//               / `r.isError`, and `r`
//               comes back with the pre advisories and the post context added
//               to `r.context`.
//   prompt      prompt.submit: `next({ ...e, context: [...e.context, text] })`.
//   session     the posture preamble as an isMeta user row
//               (`$.session.append`, spec §4.5), once per session id.
//
// Failure: a lane that throws gives the call to Python. The pre and prompt
// lanes leave the claim before their `next`, so the Python hook beneath serves
// that very call; the post lane has run past Python's PostToolUse by then (a
// post rule only advises) and counts the failure. A pre hand-off still runs
// the mod's post lane after `next` while `post` is claimed: Python skipped
// its PostToolUse for that lane, so nothing else would. A claims write that
// fails during a hand-off does not cost the call: it still reaches `next`
// (claims.ts unsets the variable, so Python serves). See claims.ts.

import type { LaneName } from '../types'
import type { Act } from './act'
import type { Claims } from './claims'
import type { Io } from './ctx'
import type { CallEvent, Engine, Verdict } from './engine'

export const TOOLS = ['Bash', 'Edit', 'MultiEdit', 'Write', 'NotebookEdit', 'Read'] as const
/**
 * The matcher for TOOLS. A RegExp, not the list: this build has no
 * `MultiEdit` built-in (claude-code.d.ts BuiltinToolName), so the list does
 * not type; the pattern still catches it on a build that has one.
 */
export const TOOL_RX = /^(?:Bash|Edit|MultiEdit|Write|NotebookEdit|Read)$/

/** What a `tool.call` hook sees, as far as the lanes read it. */
export type ToolCallLike = { tool: string; tool_use_id?: string; agentId?: string; [k: string]: unknown }
/** What a `tool.call` resolves to, as far as the lanes read it (ToolCallResult). */
export type ToolResultLike =
  | { deny: string; context?: undefined; text?: undefined; isError?: undefined }
  | { deny?: undefined; context?: readonly string[]; text?: string; isError?: true; [k: string]: unknown }
export type PromptLike = { text: string; context?: readonly string[]; [k: string]: unknown }

/** The `tool_response` keys rulebook_hook.py `result_text` joins, in its order. */
const RESULT_KEYS = ['stderr', 'stdout', 'output', 'error', 'text'] as const

/**
 * The text the post lane's result rules match: for Bash, built from the
 * tool's own record the way rulebook_hook.py `result_text(tool_response)`
 * builds it from Claude Code's PostToolUse payload: the non-empty string
 * fields among RESULT_KEYS, in that order (stderr before stdout), joined by
 * a newline. The engine then scans both ends of it (rules.ts, two
 * RESULT_WINDOW_CHARS spans), as Python's `evaluate` does, and an advise-mode
 * ordering receipt's text check reads it too, as `bash_ok` reads
 * `result_text`.
 *
 * Why not `r.text`: when Claude Code persists a large output to a file, the
 * model, and `r.text`, get only a ~2 KB `<persisted-output>` preview, while
 * the record's `stdout` (the same field PostToolUse sends as
 * `tool_response.stdout`) still holds the first ~30,000 characters. A rule
 * matching past the preview fired in Python and not here.
 *
 * Falls back to `r.text` for every other tool, for a record that is not an
 * object (an error result is the error string), and for a record with no
 * non-empty field: Python would match `json.dumps(tool_response)` there,
 * which the mod does not reproduce (engine.ts, port notes).
 */
export function postResultText(tool: string, r: { text?: string; result?: unknown }): string | undefined {
  if (tool !== 'Bash') return r.text
  const rec = r.result
  if (rec === null || typeof rec !== 'object' || Array.isArray(rec)) return r.text
  const parts = RESULT_KEYS
    .map(k => (rec as Record<string, unknown>)[k])
    .filter((v): v is string => typeof v === 'string' && v !== '')
  return parts.length ? parts.join('\n') : r.text
}

/** The tools whose rules scope by path, and the input fields the engine
 *  reads the path from (engine.ts `file_path`, repo.ts `notebook_path`). */
const PATH_TOOLS: readonly string[] = ['Edit', 'Write', 'NotebookEdit', 'Read']
const PATH_FIELDS = ['file_path', 'notebook_path'] as const

export class Lanes {
  /** Session ids whose posture preamble is in their conversation (by us or by Python). */
  private served = new Set<string>()
  /** `<session>\0<tool>` already reported by `noPathCheck`. */
  private noPathSeen = new Set<string>()

  constructor(
    private io: Io,
    private engine: Engine,
    private claims: Claims,
    private act: Act,
  ) {}

  // ── tool.call ───────────────────────────────────────────────────────────

  /**
   * A self-check on the event's shape, which only hand-written fakes pin: an
   * Edit, Write, NotebookEdit or Read call carrying none of PATH_FIELDS means
   * Claude Code renamed the field, and every path-scoped rule has silently
   * stopped matching. One debug-log line per session per tool says so;
   * nothing else changes (the call is evaluated exactly as before).
   */
  private noPathCheck(tool: string, input: Record<string, unknown>, sessionId: string): void {
    if (!PATH_TOOLS.includes(tool)) return
    if (PATH_FIELDS.some(k => typeof input[k] === 'string' && input[k] !== '')) return
    const key = `${sessionId}\0${tool}`
    if (this.noPathSeen.has(key)) return
    this.noPathSeen.add(key)
    try {
      this.io.debug(`rulebook: a ${tool} tool.call carried no ${PATH_FIELDS.join(' or ')} ` +
        `(input keys: ${Object.keys(input).sort().join(', ') || 'none'}); path-scoped rules cannot match it`)
    } catch {
      // `$.ui.log` is synchronous and may throw: a self-check costs nothing.
    }
  }

  async toolCall<R extends ToolResultLike>(
    e: ToolCallLike,
    next: (e: ToolCallLike) => Promise<R>,
  ): Promise<R | { deny: string }> {
    if (!this.claims.has('pre') && !this.claims.has('post')) return next(e)
    const { tool, tool_use_id: toolUseId, agentId, ...input } = e
    const [sessionId, cwd] = await Promise.all([this.io.sessionId(), this.io.cwd()])
    this.noPathCheck(tool, input, sessionId)
    const base: Omit<CallEvent, 'phase'> = {
      tool,
      input,
      sessionId,
      cwd,
      ...(agentId ? { agentId } : {}),
      ...(toolUseId ? { toolUseId } : {}),
    }

    let pre: Verdict | undefined
    let handed: R | undefined
    if (this.claims.has('pre')) {
      try {
        pre = await this.engine.pre({ ...base, phase: 'pre' })
        this.act.healthy('pre')
      } catch (err) {
        // Python's hook serves this call's pre at `next`; the post lane below
        // still runs here, as it would have without the failure.
        handed = await this.handOff('pre', err, () => next(e))
      }
      if (pre) {
        await this.act.record(pre, sessionId)
        if (pre.deny) return { deny: this.act.denyText(pre) }
      }
    }

    const r = handed ?? (await next(e))
    if (r.deny !== undefined) return r

    let post: Verdict | undefined
    if (this.claims.has('post')) {
      try {
        post = await this.engine.post({ ...base, phase: 'post', result: { text: postResultText(tool, r), isError: r.isError } })
        this.act.healthy('post')
        await this.act.record(post, sessionId)
      } catch (err) {
        // Python's PostToolUse already ran (skipped, the lane being ours):
        // this call's post advice is lost, never a gate. Count it.
        this.act.unhealthy('post', err)
        await this.claims.fail('post').catch(() => undefined)
        await this.claims.recover('post').catch(() => undefined)
        post = undefined
      }
    }

    const extra = [this.act.modelText(pre), this.act.modelText(post)].filter((t): t is string => !!t)
    if (!extra.length) return r
    return { ...r, context: [...(r.context ?? []), ...extra] }
  }

  // ── prompt.submit ───────────────────────────────────────────────────────

  async promptSubmit<R>(e: PromptLike, next: (e: PromptLike) => Promise<R>): Promise<R> {
    const sessionId = await this.io.sessionId()
    await this.deliverSession(sessionId)
    if (!this.claims.has('prompt')) return next(e)
    let v: Verdict
    try {
      v = await this.engine.prompt(e.text, sessionId, await this.io.cwd())
      this.act.healthy('prompt')
    } catch (err) {
      return this.handOff('prompt', err, () => next(e))
    }
    await this.act.record(v, sessionId)
    const text = this.act.modelText(v)
    return next(text ? { ...e, context: [...(e.context ?? []), text] } : e)
  }

  // ── session ─────────────────────────────────────────────────────────────

  /**
   * At session.start. `servedAlready`: the session that is starting already
   * has its preamble — from Python's SessionStart hook in a fresh process
   * (it ran before the lane could be claimed), or from the module before a
   * hot reload. The engine still sees the session start, for its own state.
   */
  async sessionStart(servedAlready: boolean): Promise<void> {
    const sessionId = await this.io.sessionId()
    if (servedAlready) {
      this.served.add(sessionId)
      if (this.claims.has('session') || this.claims.has('pre')) {
        await this.engine.session(sessionId, await this.io.cwd(), { servedAlready: true }).catch(() => undefined)
      }
      return
    }
    await this.deliverSession(sessionId)
  }

  /** After a compaction the preamble is summarised away: the next prompt brings it back (Python: SessionStart `compact`). */
  compacted(sessionId: string): void {
    this.served.delete(sessionId)
  }

  /**
   * The posture preamble for a session that has none yet — in practice a
   * compacted conversation. A new id after `/clear` or a resume is NOT served
   * here: its claim stamp mismatched when Python's SessionStart ran, so Python
   * delivered it, and claims' re-stamp marks the id served (no second copy).
   * Once per session id; only while the lane is ours.
   */
  async deliverSession(sessionId: string): Promise<void> {
    if (!this.claims.has('session') || this.served.has(sessionId)) return
    this.served.add(sessionId)
    try {
      const { context, ledger } = await this.engine.session(sessionId, await this.io.cwd())
      const text = context.join('\n')
      if (text.trim()) await this.io.append('user', text)
      // The posture rules' fire rows (Python's session_digest logs them as it emits).
      if (ledger?.length) await this.act.record({ context: [], fires: [], ledger }, sessionId)
      this.act.healthy('session')
    } catch (err) {
      // Not delivered: let Python's next SessionStart (a /clear, a compaction) serve it.
      this.served.delete(sessionId)
      this.act.unhealthy('session', err)
      await this.claims.fail('session')
      await this.claims.recover('session')
    }
  }

  /**
   * A lane threw before its `next`: out of the claim first, so the Python
   * hook the engine starts at `next` serves this call; back in afterwards
   * unless that was the lane's second failure.
   */
  private async handOff<T>(lane: LaneName, err: unknown, next: () => Promise<T>): Promise<T> {
    this.act.unhealthy(lane, err)
    // A rejected write still left the module serving nothing and the variable
    // unset if it could be (claims.ts writeFailed): carry on to `next`.
    await this.claims.fail(lane).catch((e: unknown) => this.act.unhealthy(lane, e))
    try {
      return await next()
    } finally {
      await this.claims.recover(lane).catch(() => undefined)
    }
  }
}
