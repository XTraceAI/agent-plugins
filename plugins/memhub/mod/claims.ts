// Lane claims (spec §3.3, D2): which Rulebook lanes the mod serves, told to
// the Python hooks through ONE env var per environment —
// `MEMHUB_MOD_LANES_STAGING` / `MEMHUB_MOD_LANES_PROD` =
// "<session_id>:<expires_epoch_s>:<pid>:pre,post,prompt,session".
// `hooks/hook_entry.py` (scripts/mod_lanes.py) skips a lane its own
// environment's variable lists ONLY while the lease is live, the pid is its
// own `CLAUDE_PID` (where both are known), and the stamp is its payload's
// `session_id` (or the payload is a call served here for another session).
//
// A claim must never outlive or escape the module that made it, or a gate is
// lost: nothing evaluates it.
//   * Escape: the variable is inherited by every child process. A `claude`
//     started from a Bash call or a skill sees it, and if that child's mod did
//     not load (allowManagedModsOnly, --safe-mode, disableAllHooks, an older
//     build, a load error), an inherited claim would leave nothing evaluating
//     the rules. The child's hooks carry its own pid and session id. Subagent
//     calls carry the parent's session id, so they stay ours. A /clear or
//     resume changes the id without a session.start: `restamp()` (at the next
//     prompt) writes the new one; in between Python serves.
//   * Outlive: `$.env` writes go straight to the process environment and the
//     engine does not undo them when the module crashes, fails a hot reload
//     or unloads. So the claim is a LEASE: every write sets the expiry to now
//     + LEASE_S, and a `$.clock.every` timer renews it every RENEW_MS. Timers
//     die with the module, so a dead module's claim lapses by itself within
//     LEASE_S and Python serves.
//
// The invariant this file exists for: a gate is never lost. It is doubled
// (the mod and Python both evaluate one call) only in two brief overlaps,
// accepted because the alternative is a gap:
//   * the lease hand-back: the mod serves up to LEASE_LATE_MS past its own
//     lease, while Python already serves the lapsed lane;
//   * parallel calls: a call that passed `has(lane)` before another call's
//     failure took the lane out of the variable is evaluated by the mod, and
//     by the Python hook at its `next`, which now sees the lane unclaimed.
// Its parts:
//   * Claim late: a lane is listed only once the mod can serve it (book loaded).
//   * A lane the variable does not list is served by Python alone: every lane
//     hook asks `has(lane)` before it evaluates anything (lanes.ts).
//   * A write that fails leaves the variable as it was, still listing lanes
//     the module may have dropped: the module stops serving at once (no live
//     lease) and tries to unset the variable, so Python serves; if even that
//     fails, the variable's own lease lapses within LEASE_S.
//   * Release on failure: a failing pre/prompt lane takes itself out of the
//     variable BEFORE it calls `next(e)`, so the Python hook beneath (which the
//     engine starts at the last mod's `next`, spec §1.2) serves that very call.
//     The first failure is forgiven once the call settles (`recover`); the
//     second, or any failure at load, releases the lane for the session and
//     says so on the status line.
//   * The fail→recover window: `recover` runs only after that call's `next`
//     settles, so the lane stays paused for the whole of `next` (for a long
//     Bash call, minutes). Every call that starts in the window finds the
//     lane unclaimed and is evaluated by Python's hook alone: single
//     evaluation, never a gap, but the mod serves nothing on that lane
//     meanwhile. (The paused lane is pre or prompt; a claimed post lane keeps
//     running in the mod for those calls, and Python's post hook skips it.)
//
// Module memory holds the truth; `$.state` `lanes` mirrors it for the
// companion and for a hot reload (which re-claims at its own session.start).

import type { LaneName } from '../types'
import type { Env, Io } from './ctx'

export const LANES: readonly LaneName[] = ['pre', 'post', 'prompt', 'session']
export const FELL_BACK = 'MemHub rules: fell back to the command hooks'
/** Failures a lane survives in a session; the next one releases it. */
const MAX_FAILURES = 2

/**
 * The lease. Renewed every 30 s, valid for 90 s: a live module renews twice
 * before its lease could lapse, so one late or refused tick (a busy event
 * loop, a slow `$.env` write) never hands a lane to Python while the mod still
 * serves it; a dead module's claim lapses within 90 s, which bounds how long
 * gates can go unevaluated after a crash, a failed reload or an unload. A
 * shorter lease buys a shorter gap at the price of a renewal (two `$` calls)
 * more often; 90 s is under two of the book's 60 s refresh periods.
 */
export const LEASE_S = 90
export const RENEW_MS = 30_000
/**
 * How long past its own lease the mod keeps serving. Python checks the lease
 * after the mod has evaluated pre and prompt (its hook runs at the mod's
 * `next`), but before the mod evaluates post; serving a little past the
 * expiry makes the hand-back an overlap (both evaluate) and never a gap.
 */
export const LEASE_LATE_MS = 30_000

/**
 * This plugin's one status line (`$.ui.status` is one per plugin), shared by
 * the parts that want it. The most important message shows: an upgrade notice
 * over a fallback over a lane's health line over a stale book.
 */
export class StatusLine {
  private slots: { upgrade?: string; claims?: string; health?: string; refresh?: string } = {}
  private shown: string | undefined
  constructor(private io: Io) {}
  set(slot: 'upgrade' | 'claims' | 'health' | 'refresh', text: string | undefined) {
    this.slots[slot] = text
    const next = this.slots.upgrade ?? this.slots.claims ?? this.slots.health ?? this.slots.refresh
    if (next === this.shown) return
    this.shown = next
    this.io.status(next)
  }
}

export class Claims {
  private claimed = new Set<LaneName>()
  /** Released for the rest of the session: never re-claimed. */
  private lost = new Set<LaneName>()
  private failures = new Map<LaneName, number>()
  /** Released for one in-flight call after a first failure. */
  private paused = new Set<LaneName>()
  private writes: Promise<void> = Promise.resolve()
  /** The session id the variable was last stamped with. */
  private stampedFor: string | undefined
  /** Epoch ms the variable's lease runs to (0: no live lease). */
  private leaseUntil = 0
  private renewal: { cancel(): void } | undefined
  private pid: Promise<string> | undefined

  /**
   * `clock` is epoch ms, read synchronously so `has()` stays synchronous; it
   * is `Date.now`, which reads what `$.clock.now()` and Python's
   * `time.time()` read (verified on Claude Code 2.1.292: equal).
   */
  constructor(
    private io: Io,
    private env: Env,
    private status: StatusLine,
    private clock: () => number = () => Date.now(),
  ) {}

  /** The mod serves `lane`: claimed, and its lease (written to the variable) not long lapsed. */
  has(lane: LaneName): boolean {
    return this.claimed.has(lane) && this.clock() < this.leaseUntil + LEASE_LATE_MS
  }

  list(): LaneName[] {
    return LANES.filter(l => this.claimed.has(l))
  }

  /**
   * A fresh process (nothing in `$.state` yet) must not trust a variable it
   * did not write: a `claude` started from a Bash call inherits its parent's.
   * Cleared before anything else runs, so Python serves until we claim.
   */
  async forgetInherited(): Promise<void> {
    if ((await this.io.getLanesVar(this.env)) !== undefined) await this.io.setLanesVar(this.env, undefined)
  }

  /** Add lanes the mod is now ready to serve. A lane released for the session stays released. */
  async claim(lanes: readonly LaneName[]): Promise<void> {
    const added = lanes.filter(l => !this.lost.has(l) && !this.claimed.has(l))
    for (const l of added) this.claimed.add(l)
    try {
      await this.write()
    } catch (err) {
      // The variable may not list them: serving them here too would double-fire.
      for (const l of added) this.claimed.delete(l)
      throw err
    }
  }

  /** Give lanes back to Python for the rest of the session. */
  async release(lanes: readonly LaneName[] = LANES): Promise<void> {
    for (const l of lanes) {
      this.claimed.delete(l)
      this.lost.add(l)
    }
    await this.write()
  }

  /** A failure while the mod was loading: nothing is (or stays) claimed, and the person is told. */
  async failLoad(): Promise<void> {
    await this.release(LANES)
    this.status.set('claims', FELL_BACK)
  }

  /**
   * A lane hook failed. The lane leaves the variable at once (so the Python
   * hook serves this call if it has not run yet); on the second failure it
   * stays out for the session.
   */
  async fail(lane: LaneName): Promise<void> {
    // A lane paused by a failure still in flight counts too (parallel calls).
    if (!this.claimed.has(lane) && !this.paused.has(lane)) return
    const n = (this.failures.get(lane) ?? 0) + 1
    this.failures.set(lane, n)
    this.claimed.delete(lane)
    if (n >= MAX_FAILURES) {
      this.lost.add(lane)
      this.paused.delete(lane)
      this.status.set('claims', FELL_BACK)
    } else {
      this.paused.add(lane)
    }
    await this.write()
  }

  /**
   * The session id changed in this process (a /clear or a resume: no
   * session.start fires) — stamp the claim with the new one. Resolves true
   * when the id differs from the stamp the variable carried, i.e. Python
   * served the new session until now (its SessionStart included).
   */
  async restamp(): Promise<boolean> {
    if (this.stampedFor === undefined) return false
    const sid = await this.io.sessionId()
    if (sid === this.stampedFor) return false
    if (this.claimed.size) await this.write()
    else this.stampedFor = sid
    return true
  }

  /**
   * The renewal tick: push the lease out for the lanes still claimed. A
   * released, lost or paused lane is not in `claimed`, so a renewal never
   * brings one back; nothing claimed writes nothing.
   */
  async renew(): Promise<void> {
    if (!this.claimed.size) return
    await this.write()
  }

  /** After the call a first failure handed to Python: take the lane back. */
  async recover(lane: LaneName): Promise<void> {
    if (!this.paused.delete(lane) || this.lost.has(lane)) return
    this.claimed.add(lane)
    await this.write()
  }

  /**
   * The variable and the mirror, written in order (one write at a time). The
   * value is `<session_id>:<expires_epoch_s>:<pid>:<lanes>`, the id and the
   * clock read at write time; no lanes unsets it. A claim that cannot be
   * stamped is not made: with no session id every lane goes back to Python
   * (the variable unset) and the write rejects.
   */
  private write(): Promise<void> {
    this.writes = this.writes
      .catch(() => undefined)
      .then(() => this.writeOnce().catch(err => this.writeFailed(err)))
    return this.writes
  }

  /**
   * A write rejected: the variable may still hold its previous value, listing
   * a lane this module has dropped (a `fail`, a `release`) and so leaving that
   * lane evaluated by nobody. Serve nothing (no live lease: `has()` is false
   * for every lane) and try to unset the variable, so Python serves every
   * lane; if the unset fails too, the variable's own lease lapses within
   * LEASE_S. The next renewal (or claim) writes the claim afresh. Rejects with
   * the write's error.
   */
  private async writeFailed(err: unknown): Promise<never> {
    this.leaseUntil = 0
    const unset = await this.io.setLanesVar(this.env, undefined).then(() => true, () => false)
    if (unset) await this.io.setState('lanes', []).catch(() => undefined)
    throw err
  }

  private async writeOnce(): Promise<void> {
    let lanes = this.list()
    let value: string | undefined
    if (lanes.length) {
      const sid = await this.io.sessionId().catch(() => undefined)
      if (!sid) {
        this.claimed.clear()
        this.leaseUntil = 0
        await this.io.setLanesVar(this.env, undefined)
        lanes = []
        await this.io.setState('lanes', lanes).catch(() => undefined)
        throw new Error('no session id to stamp the claim with')
      }
      const pid = await this.ownPid()
      // Re-read after the awaits: a release that landed meanwhile wins.
      lanes = this.list()
      if (lanes.length) {
        const expires = Math.floor(this.clock() / 1000) + LEASE_S
        this.stampedFor = sid
        value = stampOf(sid, expires, pid, lanes)
        await this.io.setLanesVar(this.env, value)
        this.leaseUntil = expires * 1000
        this.renewing()
      }
    }
    if (value === undefined) {
      this.leaseUntil = 0
      await this.io.setLanesVar(this.env, undefined)
    }
    // The mirror is for display; a failed mirror must not undo a claim.
    await this.io.setState('lanes', lanes).catch(() => undefined)
  }

  /** Start the renewal timer once. It dies with the module, which is what lets a dead module's lease lapse. */
  private renewing(): void {
    if (this.renewal) return
    this.renewal = this.io.every(RENEW_MS, () => {
      void this.renew().catch(() => undefined)
    })
  }

  /**
   * This Claude Code process's pid, which its hook commands see as
   * `CLAUDE_PID`: the parent of a shell `$.process.run` starts (verified on
   * 2.1.292; `$.env.get('CLAUDE_PID')` would read an ANCESTOR's, inherited).
   * '' when it cannot be had (no /bin/sh): Python then matches on the session.
   */
  private ownPid(): Promise<string> {
    this.pid ??= this.io
      .run(['/bin/sh', '-c', 'echo $PPID'], { timeoutMs: 5_000 })
      .then(r => {
        const out = r.stdout.trim()
        return r.exitCode === 0 && /^\d{1,10}$/.test(out) ? out : ''
      })
      .catch(() => '')
    return this.pid
  }
}

/** The variable's value (mod_lanes.py `lanes_for` reads it). */
export function stampOf(sid: string, expiresEpochS: number, pid: string, lanes: readonly LaneName[]): string {
  return `${sid}:${expiresEpochS}:${pid}:${lanes.join(',')}`
}
