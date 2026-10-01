// Layer 3 — choreography. hippo_band (1).py's seven poses, one frame per
// 1/20 s, ported frame for frame.
//
// Every lift is odd, so `top = HY - lift` is always even. Two canvas rows
// share a cell at size 1 and the seams fall between rows 1|2, 3|4, 5|6, 7|8
// and 9|10; an even top keeps each feature pair — ear tips over ears, brow
// over eyes, muzzle over nostrils — inside one cell instead of straddling a
// seam. Moving through an even lift shifts the drawing half a cell, which
// reads as a shimmer rather than a rise, so the transitions step two pixels
// at a time. The four finite ones end exactly where the next
// begins; the two endless ones hold the body still and idle with the eyes, so
// cutting them on any frame is safe.

import type { Tone } from '../../../animal'
import type { RGB } from '../../../pixels'
import type { Eyes, Mouth, SquidPose } from './art'
import { SQUID_Y } from './art'
import { DOZE, GONE, HY, LOW, LURK, PEEK, RISEN, type SpawnKind, SX } from './scene'

export type Z = readonly [x: number, y: number, ch: 'z' | 'Z']
export type Squid = readonly [x: number, y: number, pose: SquidPose]
/** A character over the pixels in a colour of its own: a `!`, a `?`, a heart. */
export type Mark = readonly [x: number, y: number, ch: string, color: RGB]

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
  marks: readonly Mark[]
}

/** The bubble's anchor: column 5, row 10, in canvas pixels. */
export const BUBBLE_AT = { column: SX, row: 10 }

/** One frame: plain values only. */
export function S(kw: Partial<Frame> = {}): Frame {
  return {
    lift: GONE, dx: 0, eyes: 'open', mouth: 'shut', ears: false,
    zs: [], squid: null, said: null, shown: 0, tone: 'advice', spawn: null, marks: [],
    ...kw,
  }
}

function* hold(st: Frame, n: number): Generator<Frame> {
  for (let i = 0; i < n; i++) {
    yield i === 0 ? st : { ...st, spawn: null }
  }
}

/**
 * Two z's drifting up off the right ear. They step two canvas rows at a time
 * so that at size 1 — where a glyph takes a whole cell and two rows share one
 * — each z lands on its own cell row instead of the pair colliding into a
 * single flickering character.
 */
function snore(f: number): Z[] {
  const top = 11 - DOZE // HY - DOZE
  return [0, 1].map(i => {
    const p = (f + i * 21) % 42
    return [16 + Math.floor(p / 14), top - 2 - 2 * Math.floor(p / 14), p < 21 ? 'z' : 'Z'] as const
  })
}

/** Offscreen → asleep. The ears break the surface first. */
export function* enter(): Generator<Frame> {
  yield* hold(S({ lift: PEEK, eyes: 'shut', spawn: 'bubbles' }), 4)
  yield* hold(S({ lift: PEEK, eyes: 'shut' }), 3)
  // the ear twitch waits until LOW: at PEEK the ears are the only thing above
  // the water, and dropping them would make the hippo vanish outright
  yield* hold(S({ lift: LOW, eyes: 'shut' }), 3)
  yield* hold(S({ lift: LOW, eyes: 'shut', ears: true }), 3)
  yield* hold(S({ lift: LOW, eyes: 'shut' }), 2)
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
  yield* hold(S({ lift: RISEN, eyes: 'open' }), 5)
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
  yield* hold(S({ lift: DOZE, tone: 'blocked', eyes: 'right' }), 3)
  yield* hold(S({ lift: LOW, tone: 'blocked', eyes: 'right' }), 3)
  // down to just the ear tips: any higher and the hippo's ears cover the
  // squid's legs while it walks past.
  yield* hold(S(lurk), 3)
  const walkIn = [22, 20, 18, 17, 16, 15, 14]
  for (let i = 0; i < walkIn.length; i++) {
    yield* hold(S({ ...lurk, squid: [walkIn[i]!, SQUID_Y, i % 2 ? 'step' : 'walk'] }), 3)
  }
  yield* hold(S({ ...lurk, squid: [14, SQUID_Y, 'walk'] }), 6)
  yield* hold(S({
    lift: DOZE, tone: 'blocked', mouth: 'open', dx: 2,
    squid: [14, SQUID_Y, 'walk'], spawn: 'splash',
  }), 1)
  yield* hold(S({
    lift: RISEN, tone: 'blocked', mouth: 'gape', dx: 4,
    squid: [13, 8, 'caught'], spawn: 'steam',
  }), 5)
  yield* hold(S({ ...base, mouth: 'open', dx: 2, squid: [16, SQUID_Y, 'ouch'] }), 2)
  for (const x of [18, 21, 25]) {
    // and off it goes
    yield* hold(S({ ...base, eyes: 'right', squid: [x, SQUID_Y, 'ouch'] }), 2)
  }
  yield* hold(S(base), 4)
}

const GOLD: RGB = [240, 200, 60]
const PINK: RGB = [240, 124, 150]
/** The head's middle column, and the row just over it at the speaking lift. */
const HEAD_X = SX + 6
const OVER_HEAD = HY - RISEN - 2
/** Frames a proposal stays up once typed: long enough to read it and reach for a button. */
const ASK_FRAMES = 160

/**
 * The proposal prelude. Something new has surfaced: the hippo looks both
 * ways, then a gold `!` pops over its head in a burst of bubbles while its
 * ears wiggle. Starts and ends on the speaking position.
 */
function* present(tone: Tone): Generator<Frame> {
  const T = { lift: RISEN, tone }
  yield* hold(S({ ...T, eyes: 'right' }), 4)
  yield* hold(S({ ...T, eyes: 'left' }), 4)
  for (let i = 0; i < 8; i++) {
    yield S({
      ...T, ears: Math.floor(i / 2) % 2 === 1, marks: [[HEAD_X, OVER_HEAD, '!', GOLD]],
      spawn: i === 0 ? 'bubbles' : null,
    })
  }
  yield* hold(S(T), 2)
}

/** The asking hold: a gold `?` bobbing over the head, the odd blink and glance. */
function* ask(base: Partial<Frame>, text: string): Generator<Frame> {
  for (let g = 0; g < ASK_FRAMES; g++) {
    const eyes: Eyes = g % 60 === 50 || g % 60 === 51 ? 'shut' : g % 120 >= 80 && g % 120 < 92 ? 'left' : 'open'
    const bob = Math.floor(g / 10) % 2 ? OVER_HEAD - 2 : OVER_HEAD
    yield S({ ...base, eyes, said: text, shown: text.length, marks: [[HEAD_X, bob, '?', GOLD]] })
  }
}

/**
 * Type it out, then hold it long enough to read. Ends where it started.
 * `blocked` has the squid bitten first; `proposed` is presented with a `!`
 * and held longer with a `?`, asking for an answer.
 */
export function* speak(text: string, tone: Tone = 'advice'): Generator<Frame> {
  const base = { lift: RISEN, tone }
  yield S(base) // neutral, and where it ends up again
  if (tone === 'blocked') {
    yield* intercept() // the bite says it first
  } else if (tone === 'proposed') {
    yield* present(tone)
  }
  let k = 0.0
  let f = 0
  while (k < text.length) {
    k += 1.6
    f += 1
    yield S({ ...base, said: text, shown: Math.trunc(k), mouth: f % 2 ? 'open' : 'shut' })
  }
  if (tone === 'proposed') {
    yield* ask(base, text)
    yield S({ ...base, said: text, shown: text.length })
    return
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

/**
 * A click: the ears wiggle, the mouth opens, and a pink heart rises off the
 * nostrils in a few bubbles; then a pleased squint. Starts and ends on look's
 * first frame, so the director can cut it in and out of `look`.
 */
export function* pet(): Generator<Frame> {
  const rest = S({ lift: DOZE })
  yield rest
  for (let i = 0; i < 12; i++) {
    yield S({
      lift: DOZE, eyes: 'shut', ears: Math.floor(i / 3) % 2 === 0, mouth: i >= 3 && i < 9 ? 'open' : 'shut',
      marks: [[SX + 3, HY - DOZE - 2 - 2 * Math.floor(i / 4), '♥', PINK]], spawn: i === 0 ? 'bubbles' : null,
    })
  }
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 8)
  yield* hold(rest, 8)
  yield rest
}

/** The speaking position → offscreen, ready for `enter` again. */
export function* leave(): Generator<Frame> {
  yield* hold(S({ lift: RISEN, eyes: 'open' }), 2)
  yield* hold(S({ lift: RISEN, eyes: 'shut' }), 3)
  yield* hold(S({ lift: DOZE, eyes: 'shut' }), 4)
  yield* hold(S({ lift: LOW, eyes: 'shut', spawn: 'bubbles' }), 5)
  yield* hold(S({ lift: PEEK, eyes: 'shut' }), 4)
  yield* hold(S({ lift: GONE, spawn: 'bubbles' }), 6)
}

export const SAMPLE: Readonly<Record<Tone, string>> = {
  advice: 'Rule fired: run only the touched test suites',
  blocked: 'Blocked: never force-push to a shared branch',
  proposed: 'New rule↗ proposed, not active yet: Pin the MCP server when spawning claude -p',
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
    yield* speak(SAMPLE.proposed, 'proposed')
    yield* leave()
  }
}

export const POSES: Readonly<Record<string, () => Generator<Frame>>> = {
  enter, sleep, wake, look, rise, leave,
  speak: () => speak(SAMPLE.advice),
  blocked: () => speak(SAMPLE.blocked, 'blocked'),
  propose: () => speak(SAMPLE.proposed, 'proposed'),
  pet,
}
