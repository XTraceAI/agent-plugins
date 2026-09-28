// Layer 3 — choreography. hippo_band.py's seven poses, one frame per 1/20 s,
// ported frame for frame. The four finite ones end exactly where the next
// begins; the two endless ones hold the body still and idle with the eyes, so
// cutting them on any frame is safe.

import type { Tone } from '../../../animal'
import type { Eyes, Mouth, SquidPose } from './art'
import { SQUID_Y } from './art'
import { DOZE, GONE, LOW, LURK, PEEK, RISEN, type SpawnKind, SX } from './scene'

export type Z = readonly [x: number, y: number, ch: 'z' | 'Z']
export type Squid = readonly [x: number, y: number, pose: SquidPose]

export type Frame = {
  lift: number
  dx: number
  eyes: Eyes
  mouth: Mouth
  ears: boolean
  zs: readonly Z[]
  squid: Squid | null
  said: string | null
  shown: number
  tone: Tone
  spawn: SpawnKind | null
}

/** The bubble's anchor: column 5, row 10, in canvas pixels. */
export const BUBBLE_AT = { column: SX, row: 10 }

/** One frame: plain values only. */
export function S(kw: Partial<Frame> = {}): Frame {
  return {
    lift: GONE, dx: 0, eyes: 'open', mouth: 'shut', ears: false,
    zs: [], squid: null, said: null, shown: 0, tone: 'advice', spawn: null,
    ...kw,
  }
}

function* hold(st: Frame, n: number): Generator<Frame> {
  for (let i = 0; i < n; i++) {
    yield i === 0 ? st : { ...st, spawn: null }
  }
}

/** Two z's drifting up off the right ear. */
function snore(f: number): Z[] {
  const top = 11 - DOZE // HY - DOZE
  return [0, 1].map(i => {
    const p = (f + i * 20) % 40
    return [16 + Math.floor(p / 14), top - 1 - Math.floor(p / 10), p < 20 ? 'z' : 'Z'] as const
  })
}

/** Offscreen → asleep. The ears break the surface first. */
export function* enter(): Generator<Frame> {
  yield* hold(S({ lift: PEEK, eyes: 'shut', spawn: 'bubbles' }), 4)
  yield* hold(S({ lift: PEEK, eyes: 'shut', ears: true }), 3)
  yield* hold(S({ lift: PEEK, eyes: 'shut' }), 3)
  yield* hold(S({ lift: 2, eyes: 'shut' }), 4)
  yield* hold(S({ lift: LOW, eyes: 'shut' }), 4)
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 6)
}

/** Asleep, endless: nothing is happening in the session. */
export function* sleep(): Generator<Frame> {
  for (let f = 0; ; f++) {
    yield S({
      lift: DOZE, eyes: 'shut', zs: snore(f),
      spawn: f % 70 === 40 ? 'bubbles' : null,
    })
  }
}

/** Asleep → awake. Two slow blinks, no change in height. */
export function* wake(): Generator<Frame> {
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 5)
  yield* hold(S({ lift: DOZE, eyes: 'open' }), 3)
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 3)
  yield* hold(S({ lift: DOZE, eyes: 'open' }), 5)
}

/** Awake, endless: Claude is answering, or the person is typing. */
export function* look(): Generator<Frame> {
  for (let f = 0; ; f++) {
    const p = f % 90
    const eyes: Eyes =
      p === 44 || p === 45 ? 'shut' : p >= 20 && p < 30 ? 'left' : p >= 30 && p < 40 ? 'right' : 'open'
    yield S({ lift: DOZE, eyes, ears: [62, 63, 67, 68].includes(p) })
  }
}

/** Awake → the speaking position. */
export function* rise(): Generator<Frame> {
  yield* hold(S({ lift: DOZE, eyes: 'open', spawn: 'splash' }), 2)
  yield* hold(S({ lift: 6, eyes: 'open' }), 2)
  yield* hold(S({ lift: RISEN, eyes: 'open' }), 4)
}

/**
 * What `blocked` looks like: a squid wanders up to the pond, the hippo sinks
 * out of sight, and then objects. Starts and ends at RISEN, dead centre, with
 * the squid gone.
 *
 * The geometry is tight. The gape's mouth interior sits at sprite columns
 * 3..8, so at dx=4 it covers canvas columns 12..17 — which is why the squid
 * stops at 15 and the lunge is worth a full four pixels. Anything less and the
 * jaws shut on open water.
 */
function* intercept(): Generator<Frame> {
  const base = { lift: RISEN, tone: 'blocked' as const }
  const lurk = { lift: LURK, tone: 'blocked' as const, eyes: 'right' as const }
  yield* hold(S({ lift: 6, tone: 'blocked', eyes: 'right' }), 2)
  yield* hold(S({ lift: 4, tone: 'blocked', eyes: 'right' }), 2)
  yield* hold(S({ lift: LOW, tone: 'blocked', eyes: 'right' }), 2)
  // down to just the ear tips: any higher and the hippo's ears cover the
  // squid's legs while it walks past.
  yield* hold(S(lurk), 3)
  const walkIn = [22, 20, 18, 17, 16, 15, 14]
  for (let i = 0; i < walkIn.length; i++) {
    yield* hold(S({ ...lurk, squid: [walkIn[i]!, SQUID_Y, i % 2 ? 'step' : 'walk'] }), 3)
  }
  yield* hold(S({ ...lurk, squid: [14, SQUID_Y, 'walk'] }), 6)
  yield* hold(S({
    lift: 6, tone: 'blocked', mouth: 'open', dx: 2,
    squid: [14, SQUID_Y, 'walk'], spawn: 'splash',
  }), 1)
  yield* hold(S({
    lift: RISEN, tone: 'blocked', mouth: 'gape', dx: 4,
    squid: [13, 8, 'caught'], spawn: 'steam',
  }), 5)
  yield* hold(S({ ...base, mouth: 'open', dx: 2, squid: [16, 6, 'ouch'] }), 2)
  for (const x of [18, 21, 25]) {
    // and off it goes
    yield* hold(S({ ...base, eyes: 'right', squid: [x, SQUID_Y, 'ouch'] }), 2)
  }
  yield* hold(S(base), 4)
}

/** Type it out, then hold it long enough to read. Ends where it started. */
export function* speak(text: string, tone: Tone = 'advice'): Generator<Frame> {
  const base = { lift: RISEN, tone }
  yield S(base) // neutral, and where it ends up again
  if (tone === 'blocked') {
    yield* intercept() // the bite says it first
  }
  let k = 0.0
  let f = 0
  while (k < text.length) {
    k += 1.6
    f += 1
    yield S({ ...base, said: text, shown: Math.trunc(k), mouth: f % 2 ? 'open' : 'shut' })
  }
  for (let g = 0; g < 60; g++) {
    // 3 seconds to read it
    yield S({
      ...base, said: text, shown: text.length,
      eyes: tone === 'advice' && g % 40 === 30 ? 'shut' : 'open',
    })
  }
  yield* hold(S({ ...base, said: text, shown: text.length }), 2)
}

/** The speaking position → offscreen, ready for `enter` again. */
export function* leave(): Generator<Frame> {
  yield* hold(S({ lift: RISEN, eyes: 'open' }), 2)
  yield* hold(S({ lift: RISEN, eyes: 'shut' }), 3)
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 4)
  yield* hold(S({ lift: LOW, eyes: 'shut', spawn: 'bubbles' }), 4)
  yield* hold(S({ lift: 2, eyes: 'shut' }), 3)
  yield* hold(S({ lift: PEEK, eyes: 'shut' }), 3)
  yield* hold(S({ lift: GONE, spawn: 'bubbles' }), 6)
}

export const SAMPLE: Readonly<Record<Tone, string>> = {
  advice: 'Rule fired: run only the touched test suites',
  blocked: 'Blocked: never force-push to a shared branch',
}

/** hippo_band.py's own preview loop: the ring, for `/hippo demo`. */
export function* cycle(): Generator<Frame> {
  for (;;) {
    yield* enter()
    const s = sleep()
    for (let i = 0; i < 90; i++) yield s.next().value
    yield* wake()
    const l = look()
    for (let i = 0; i < 70; i++) yield l.next().value
    yield* rise()
    yield* speak(SAMPLE.advice)
    yield* speak(SAMPLE.blocked, 'blocked')
    yield* leave()
  }
}

export const POSES: Readonly<Record<string, () => Generator<Frame>>> = {
  enter, sleep, wake, look, rise, leave,
  speak: () => speak(SAMPLE.advice),
  blocked: () => speak(SAMPLE.blocked, 'blocked'),
}
