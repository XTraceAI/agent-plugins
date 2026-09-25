// Layer 3 — choreography. goose_band.py's poses, one frame per 1/20 s,
// ported frame for frame. Finite poses end where the next begins; the endless
// two are cut off mid-frame, so they idle in the eyes and the z's only.
// Every vertical position is even, so nothing ever sits half a cell off.

import type { Tone } from '../../animal'
import type { Body, Eyes, SquidEyes, Talons, Wings } from './art'
import { GRIP_DY, GX, GY, W } from './scene-consts'

export type Overlay = readonly [x: number, y: number, ch: string, color: string]
export type Squid = readonly [x: number, y: number, eyes: SquidEyes, legs: number]
export type Flyer = readonly [x: number, y: number, wings: Wings, talons: Talons, facing: 'L' | 'R']

export const FPS = 20

export type Frame = {
  body: Body | null
  x: number
  eyes: Eyes
  beak: 'shut' | 'open'
  blush: boolean
  badge: boolean
  step: number
  squid: Squid | null
  flyer: Flyer | null
  shadow: readonly [x: number, width: number] | null
  overlays: readonly Overlay[]
  said: string | null
  shown: number
  tone: Tone
}

/** The bubble's anchor: column 5, row 10, in canvas pixels. */
export const BUBBLE_AT = { column: 5, row: 10 }
/** Frames the finished message stays up (>= 40). */
const HOLD_FRAMES = 44
/** One watching cycle in this many carries the glance; the rest only blink. */
const GLANCE_EVERY = 4
/** Frames offscreen before it walks back on — two seconds of empty grass. */
const GONE_FRAMES = 2 * FPS
/** Where the squid stops, under the dive. */
const SQ_STOP = 13
/** The squid's top row on the ground. */
const SQ_Y = 6
const FLY_X = SQ_STOP - 7 // SQ_STOP - GRIP_DX
const SHADOW_X = SQ_STOP + 3

/** One frame: plain values only. */
export function S(kw: Partial<Frame> = {}): Frame {
  return {
    body: 'stand', x: GX, eyes: 'open', beak: 'shut', blush: false,
    badge: false, step: 0, squid: null, flyer: null, shadow: null,
    overlays: [], said: null, shown: 0, tone: 'advice',
    ...kw,
  }
}

const OFF = () => S({ body: null })

function* hold(st: Frame, n: number): Generator<Frame> {
  for (let i = 0; i < n; i++) yield { ...st }
}

/** Offscreen → asleep: waddles in from the right, yawns, tucks in. */
export function* enter(): Generator<Frame> {
  yield OFF()
  for (let x = W; x > GX; x--) {
    yield* hold(S({ x, step: x % 2 }), 2)
  }
  yield* hold(S(), 6)
  yield* hold(S({ eyes: 'shut', beak: 'open' }), 5)
  yield* hold(S({ eyes: 'shut' }), 3)
  yield S({ body: 'tuck' })
}

/** Two z's drifting up from the tucked head, two rows apart so they never share a cell. */
function zs(f: number): Overlay[] {
  const path: Overlay[] = [[11, 4, 'z', 'z'], [12, 2, 'z', 'z'], [13, 0, 'Z', 'z']]
  const shown: Record<number, number[]> = { 0: [0], 1: [0, 1], 2: [1, 2], 3: [2] }
  return shown[Math.floor(f / 16) % 4]!.map(i => path[i]!)
}

/** Asleep, endless. Only the z's move. */
export function* sleep(): Generator<Frame> {
  for (let f = 0; ; f++) {
    yield S({ body: 'tuck', overlays: zs(f) })
  }
}

/** Asleep → awake. */
export function* wake(): Generator<Frame> {
  yield* hold(S({ body: 'tuck' }), 4)
  yield* hold(S({ eyes: 'shut' }), 3)
  yield S()
}

/**
 * Awake, endless: watching. A blink every cycle, and the glance left and
 * right only on one cycle in four — watching someone type is mostly stillness,
 * and a glance that came round every few seconds read as a fidget.
 */
export function* look(): Generator<Frame> {
  for (let f = 0; ; f++) {
    const g = f % 90
    const glancing = Math.floor(f / 90) % GLANCE_EVERY === 0
    const eyes: Eyes =
      g === 60 || g === 61 ? 'shut'
      : glancing && g >= 20 && g < 34 ? 'right'
      : glancing && g >= 40 && g < 50 ? 'left'
      : 'open'
    yield S({ eyes })
  }
}

/** Awake → the speaking position: shakes its wings out, badge on. */
export function* rise(): Generator<Frame> {
  yield* hold(S(), 2)
  for (const body of ['flapU', 'flapD', 'flapU', 'flapD'] as const) {
    yield* hold(S({ body }), 3)
  }
  yield* hold(S({ badge: true }), 3)
}

/** Standing → gone off the top right: two wingbeats, then up and away. */
function* takeoff(kw: Partial<Frame> = {}): Generator<Frame> {
  yield* hold(S({ ...kw, body: 'flapU' }), 2)
  yield* hold(S({ ...kw, body: 'flapD' }), 2)
  let x = GX - 1
  let y = GY
  for (let i = 0; i < 5; i++) {
    // the fifth step is its last visible one
    yield* hold(S({ ...kw, body: null, flyer: [x, y, (['down', 'up'] as const)[i % 2]!, 'tuck', 'R'] }), 2)
    x += 2
    y -= 2
  }
}

/**
 * Off the top right → standing on its spot: glides down, legs out for the
 * touchdown, settles its wings.
 */
function* land(kw: Partial<Frame> = {}): Generator<Frame> {
  const steps = [3, 2, 1, 0]
  for (let i = 0; i < steps.length; i++) {
    const k = steps[i]!
    yield* hold(S({
      ...kw, body: null,
      flyer: [GX + 2 * k, GY - 2 * k, (['up', 'down'] as const)[i % 2]!, k < 2 ? 'open' : 'tuck', 'L'],
    }), 3)
  }
  for (const body of ['flapD', 'flapU', 'flapD'] as const) {
    yield* hold(S({ ...kw, body }), 2)
  }
}

/**
 * The blocked prelude. The goose spots trouble and flies off, the squid
 * wanders in, and the goose comes back as a hawk and carries it away, then
 * flies home to say why. Ends on the speaking position, so `speak` can type
 * from there.
 */
function* hunt(tone: Tone = 'blocked'): Generator<Frame> {
  const T: Partial<Frame> = { tone, badge: true }
  const G: Partial<Frame> = { ...T, body: null }
  const sq = (eyes: SquidEyes, legs = 0): Squid => [SQ_STOP, SQ_Y, eyes, legs]
  yield S(T)
  yield* hold(S({ ...T, eyes: 'right' }), 5) // something's coming
  yield* hold(S({ ...T, eyes: 'stern', blush: true }), 6)
  yield* takeoff(T) // ...and it's off
  let i = 0
  for (let x = W - 1; x > SQ_STOP - 1; x--, i++) {
    // the squid
    yield S({ ...G, squid: [x, SQ_Y, 'left', i % 2] })
  }
  yield* hold(S({ ...G, squid: sq('fwd') }), 3)
  yield* hold(S({ ...G, squid: sq('right') }), 4)
  yield* hold(S({ ...G, squid: sq('fwd') }), 2)
  for (const w of [2, 4, 6]) {
    // a shadow grows
    yield* hold(S({ ...G, squid: sq('fwd'), shadow: [SHADOW_X, w] }), 2)
  }
  yield* hold(S({
    ...G, squid: sq('up'), shadow: [SHADOW_X, 8],
    overlays: [[SQ_STOP + 2, 4, '!', 'Y']],
  }), 6)
  for (const y of [-4, -2]) {
    // the stoop
    yield S({ ...G, squid: sq('up'), shadow: [SHADOW_X, 8], flyer: [FLY_X, y, 'dive', 'open', 'L'] })
  }
  yield* hold(S({ ...G, squid: sq('up'), shadow: [SHADOW_X, 8], flyer: [FLY_X, 0, 'up', 'open', 'L'] }), 2)
  yield* hold(S({ ...G, squid: sq('shut'), shadow: [SHADOW_X, 8], flyer: [FLY_X, 0, 'down', 'grip', 'L'] }), 4)
  let n = 0
  for (let y = -2; y > -12; y -= 2, n++) {
    // carried off
    for (const j of [0, 1]) {
      yield S({
        ...G,
        flyer: [FLY_X, y, (['up', 'down'] as const)[j]!, 'grip', 'L'],
        squid: [SQ_STOP, y + GRIP_DY, 'shut', j],
        shadow: n < 4 ? [SHADOW_X, 8 - 2 * n] : null,
      })
    }
  }
  yield* land(T) // home again
  yield S(T)
}

/**
 * Type it out, then hold it long enough to read. Starts and ends on the same
 * neutral frame. `advice` keeps a steady eye; `blocked` stages the hunt
 * first, then says it with a stern brow.
 */
export function* speak(text: string, tone: Tone = 'advice'): Generator<Frame> {
  const base: Partial<Frame> = { badge: true, tone }
  const talk: Eyes = tone === 'blocked' ? 'stern' : 'open'
  if (tone === 'blocked') {
    yield* hunt(tone)
  } else {
    yield S({ ...base, said: text, shown: 0 })
  }
  let k = 0.0
  let f = 0
  while (k < text.length) {
    k = Math.min(text.length, k + 1.6)
    f += 1
    yield S({ ...base, eyes: talk, beak: f % 2 ? 'open' : 'shut', said: text, shown: Math.trunc(k) })
  }
  for (let g = 0; g < HOLD_FRAMES; g++) {
    const eyes: Eyes = g === 30 || g === 31 ? 'shut' : g < 30 ? talk : 'open'
    yield S({ ...base, eyes, said: text, shown: text.length })
  }
  yield S({ ...base, said: text, shown: text.length })
}

/**
 * The speaking position → offscreen: takes off, up and to the right, and
 * stays gone. The empty beat is the point: without it the goose lands and
 * immediately waddles back on, which reads as a glitch rather than a trip.
 */
export function* leave(): Generator<Frame> {
  yield* hold(S({ badge: true }), 2)
  yield* takeoff()
  yield* hold(OFF(), GONE_FRAMES)
}

export const SAMPLE: Readonly<Record<Tone, string>> = {
  advice: 'Rule fired: run only the touched test suites',
  blocked: 'Blocked: never force-push to a shared branch',
}

/** The ring the mod walks, for `/goose demo`. */
export function* cycle(): Generator<Frame> {
  for (;;) {
    yield* enter()
    const s = sleep()
    for (let i = 0; i < 70; i++) yield s.next().value
    yield* wake()
    const lk = look()
    for (let i = 0; i < 70; i++) yield lk.next().value
    yield* rise()
    yield* speak(SAMPLE.advice)
    yield* speak(SAMPLE.blocked, 'blocked')
    yield* leave()
  }
}

export const POSES: Readonly<Record<string, () => Generator<Frame>>> = {
  enter, sleep, wake, look, rise, leave,
  speak: () => speak(SAMPLE.advice),
  hunt: () => speak(SAMPLE.blocked, 'blocked'),
}
