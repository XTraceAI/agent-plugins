// Layer 3 — choreography. penguin_band.py's poses, one frame per 1/20 s,
// ported frame for frame. Finite poses end where the next begins; the endless
// two only ever move the eyes and the overlaid characters.

import type { Tone } from '../../animal'
import type { Eyes, Feet, LeftFlipper, Mouth, Posture, RightFlipper, SquidEyes, WhaleMouth } from './art'
import { FOOT, PX, SPLASH_LEN, W } from './scene-consts'

export type Glyph = readonly [x: number, y: number, ch: string, color: string]
export type Squid = readonly [x: number, y: number, eyes: SquidEyes, legs: number]
export type Whale = readonly [top: number, mouth: WhaleMouth]
export type Fx = readonly [kind: 'shards' | 'splash' | 'spray', age: number]

export type Frame = {
  px: number
  posture: Posture
  eyes: Eyes
  mouth: Mouth
  lf: LeftFlipper
  rf: RightFlipper
  feet: Feet
  squid: Squid | null
  whale: Whale | null
  hole: number
  fx: Fx | null
  glyphs: readonly Glyph[]
  said: string | null
  shown: number
  tone: Tone
}

/** The bubble's anchor: column 5, row 10, in canvas pixels. */
export const BUBBLE_AT = { column: 5, row: 10 }
/** px at which the whole sprite is off the left edge. */
const OFF_X = -15
const SQUID_X = 15
const SQUID_Y = FOOT - 4
const ALARM_AT: readonly [number, number] = [SQUID_X + 2, SQUID_Y - 2]
/** Every duration in frames, in one place: this table is the timing budget. */
const T = {
  waddle: 2, walk: 2, notice: 8, shuffle: 3, pocket: 4, ring: 10, talk: 8, point: 8,
  crack: 3, breach: 2, chomp: 6, fall: 2, hangup: 3, hold: 44, turn: 3,
  flop: 2, heal: 3, stand: 4, home: 3,
}
/** px the penguin shuffles back to, away from the squid. */
const BACK = -2
/** The phone's ring, a cell left of the phone. */
const RING_AT: readonly [number, number] = [PX + BACK - 1, 6]
/** px per frame, easing out. */
const SLIDE = [-3, -3, -3, -4, -4, -4, -4, -5, -5, -5]

/** One frame: plain values only. */
export function S(kw: Partial<Frame> = {}): Frame {
  return {
    px: 0, posture: 'stand', eyes: 'open', mouth: 'shut', lf: 'down',
    rf: 'down', feet: 'stand', squid: null, whale: null, hole: 0, fx: null,
    glyphs: [], said: null, shown: 0, tone: 'advice',
    ...kw,
  }
}

/** The speaking position: flippers out. */
const SPEAKING = { lf: 'out', rf: 'out' } as const

/** A frame in the speaking position. */
const F = (kw: Partial<Frame> = {}): Frame => S({ ...SPEAKING, ...kw })

function* hold(st: Frame, n: number): Generator<Frame> {
  for (let i = 0; i < n; i++) yield st
}

/** Walk from px x0 to x1, one pixel every `step` frames, feet alternating. */
function* waddle(x0: number, x1: number, eyes: Eyes, step: number, kw: Partial<Frame> = {}): Generator<Frame> {
  const d = x1 > x0 ? 1 : -1
  let i = 0
  for (let x = x0 + d; d > 0 ? x < x1 + d : x > x1 + d; x += d, i++) {
    yield* hold(S({ px: x, eyes, feet: i % 2 ? 'b' : 'a', ...kw }), step)
  }
}

/** Offscreen → asleep: waddles in from the left, tucks its head down. */
export function* enter(): Generator<Frame> {
  yield S({ px: OFF_X })
  yield* waddle(OFF_X, 0, 'right', T.waddle)
  yield* hold(S(), 6)
  yield* hold(S({ eyes: 'shut' }), 4)
  yield* hold(S({ posture: 'hunch', eyes: 'shut' }), 4)
}

/** Asleep, endless. Only the z's move. */
export function* sleep(): Generator<Frame> {
  const spots: (readonly [number, number, string])[] = [[14, 4, 'z'], [15, 2, 'z'], [16, 0, 'Z']]
  for (let f = 0; ; f++) {
    let zs: Glyph[] = []
    if (f >= 16) {
      const g = Math.floor((f - 16) / 10)
      zs = [0, 1].map(i => {
        const spot = spots[(g + i) % 3]!
        return [spot[0], spot[1], spot[2], 'Z'] as const
      })
    }
    yield S({ posture: 'hunch', eyes: 'shut', glyphs: zs })
  }
}

/** Asleep → awake. */
export function* wake(): Generator<Frame> {
  yield* hold(S({ posture: 'hunch', eyes: 'shut' }), 4)
  yield* hold(S({ posture: 'hunch', eyes: 'open' }), 4)
  yield* hold(S(), 4)
}

/** Awake, endless: looks about and blinks. The body never moves. */
export function* look(): Generator<Frame> {
  for (let f = 0; ; f++) {
    const g = f % 80
    const eyes: Eyes =
      g === 40 || g === 41 ? 'shut' : g >= 20 && g < 30 ? 'left' : g >= 30 && g < 40 ? 'right' : 'open'
    yield S({ eyes })
  }
}

/** Awake → the speaking position: flippers out, one then the other. */
export function* rise(): Generator<Frame> {
  yield* hold(S(), 3)
  yield* hold(S({ lf: 'out' }), 2)
  yield* hold(F(), 3)
}

/** Type the text out at 1.6 characters a frame, beak flapping. */
function* typed(text: string, tone: Tone, kw: Partial<Frame> = {}): Generator<Frame> {
  let k = 0.0
  let f = 0
  while (k < text.length) {
    k += 1.6
    f += 1
    yield F({
      mouth: f % 2 ? 'open' : 'shut', said: text,
      shown: Math.min(text.length, Math.trunc(k)), tone, ...kw,
    })
  }
}

function* adviceScript(text: string): Generator<Frame> {
  const said = { said: text, tone: 'advice' as const }
  yield F({ shown: 0, ...said })
  yield* typed(text, 'advice')
  for (let i = 0; i < T.hold; i++) {
    yield F({ eyes: i === 30 ? 'shut' : 'open', shown: text.length, ...said })
  }
  yield F({ shown: text.length, ...said })
}

/**
 * The rule intercepts the agent: the squid wanders onto the ice, the penguin
 * shuffles back a step, phones it in and points, a whale takes the squid
 * through the ice, and only then is the rule read out, calmly. Then a
 * belly-slide clear of the hole, the ice heals, and it waddles back.
 */
function* blockedScript(text: string): Generator<Frame> {
  const sq: Squid = [SQUID_X, SQUID_Y, 'fwd', 0]
  const on: Partial<Frame> = { tone: 'blocked' }
  yield F(on)
  // the squid walks in
  for (let i = 0, x = W - 1; x >= SQUID_X; x--, i++) {
    for (let k = 0; k < T.walk; k++) {
      yield F({ squid: [x, SQUID_Y, 'left', i % 2], ...on })
    }
  }
  for (let i = 0; i < T.notice; i++) yield F({ eyes: 'right', squid: sq, ...on })
  yield* waddle(0, BACK, 'right', T.shuffle, { squid: sq, ...SPEAKING, ...on })
  const back: Partial<Frame> = { ...on, px: BACK } // everything from here happens a step back
  for (let i = 0; i < T.pocket; i++) yield S({ eyes: 'right', lf: 'pocket', squid: sq, ...back })
  for (let i = 0; i < T.pocket; i++) yield S({ eyes: 'right', lf: 'phone', squid: sq, ...back })
  const ring: Glyph[] = [[RING_AT[0], RING_AT[1], '(', 'Z']]
  for (let i = 0; i < T.ring; i++) {
    yield S({ eyes: 'right', lf: 'ear', squid: sq, ...back, glyphs: Math.floor(i / 3) % 2 === 0 ? ring : [] })
  }
  for (let i = 0; i < T.talk; i++) {
    yield S({
      eyes: 'right', lf: 'ear', squid: sq, ...back,
      mouth: Math.floor(i / 2) % 2 === 0 ? 'open' : 'shut',
    })
  }
  for (let i = 0; i < T.point; i++) yield S({ eyes: 'right', lf: 'ear', rf: 'point', squid: sq, ...back })
  const alarm: Glyph[] = [[ALARM_AT[0], ALARM_AT[1], '!', 'Z']]
  for (const h of [1, 2]) {
    for (let i = 0; i < T.crack; i++) {
      yield S({
        eyes: 'right', lf: 'ear', rf: 'point', squid: [SQUID_X, SQUID_Y, 'up', 0],
        hole: h, glyphs: alarm, ...back,
      })
    }
  }
  let age = 0
  for (const [top, sy] of [[8, SQUID_Y], [4, SQUID_Y - 4], [2, SQUID_Y - 6]] as const) {
    for (let i = 0; i < T.breach; i++) {
      yield S({
        eyes: 'right', lf: 'ear', squid: [SQUID_X, sy, 'up', 1],
        whale: [top, 'open'], hole: 3, fx: ['shards', age], ...back,
      })
      age += 1
    }
  }
  for (let i = 0; i < T.chomp; i++) {
    yield S({ eyes: 'right', lf: 'ear', whale: [2, 'shut'], hole: 3, fx: ['shards', age + i], ...back })
  }
  const falls = [6, 10]
  for (let i = 0; i < falls.length; i++) {
    for (let k = 0; k < T.fall; k++) {
      yield S({
        eyes: 'right', lf: 'ear', whale: [falls[i]!, 'shut'], hole: 3,
        fx: ['splash', i * T.fall + k], ...back,
      })
    }
  }
  const a0 = 2 * T.fall
  const hang: Partial<Frame>[] = [
    { eyes: 'right', lf: 'phone' }, { eyes: 'right', lf: 'pocket' }, { ...SPEAKING },
  ]
  for (let j = 0; j < hang.length; j++) {
    for (let k = 0; k < T.hangup; k++) {
      const at = a0 + j * T.hangup + k
      yield S({ hole: 3, fx: at < SPLASH_LEN ? ['splash', at] : null, ...hang[j], ...back })
    }
  }
  yield* typed(text, 'blocked', { hole: 3, px: BACK })
  const said = { said: text, shown: text.length, tone: 'blocked' as const }
  for (let i = 0; i < T.hold; i++) {
    yield F({ px: BACK, hole: 3, eyes: i === 30 ? 'shut' : 'open', ...said })
  }
  for (let i = 0; i < T.turn; i++) yield S({ px: BACK, eyes: 'left', hole: 3, ...said })
  for (let i = 0; i < T.flop; i++) yield S({ posture: 'lie', px: BACK, hole: 3, ...said })
  for (let i = 0; i < SLIDE.length; i++) {
    yield S({ posture: 'lie', px: SLIDE[i]!, hole: i < 5 ? 3 : 4, fx: ['spray', i], ...said })
  }
  for (const h of [4, 5, 6]) {
    for (let i = 0; i < T.heal; i++) yield S({ posture: 'lie', px: SLIDE[SLIDE.length - 1]!, hole: h, ...said })
  }
  for (const e of ['shut', 'open'] as const) {
    for (let i = 0; i < Math.floor(T.stand / 2); i++) {
      yield S({ px: SLIDE[SLIDE.length - 1]!, eyes: e, ...said })
    }
  }
  yield* waddle(SLIDE[SLIDE.length - 1]!, 0, 'right', T.home, said)
  yield F(said)
}

/** speaking position → the same position. Types out, holds 44 frames. */
export function* speak(text: string, tone: Tone = 'advice'): Generator<Frame> {
  yield* tone === 'blocked' ? blockedScript(text) : adviceScript(text)
}

/** The speaking position → offscreen, left, ready for `enter` again. */
export function* leave(): Generator<Frame> {
  yield* hold(F(), 3)
  yield* hold(S(), 3)
  yield* hold(S({ eyes: 'left' }), 3)
  yield* waddle(0, OFF_X, 'left', T.waddle)
  yield S({ px: OFF_X })
}

export const SAMPLE: Readonly<Record<Tone, string>> = {
  advice: 'Rule fired: run only the touched test suites',
  blocked: 'Blocked: never force-push to a shared branch',
}

/** The ring the mod walks, for `/penguin demo`. */
export function* cycle(): Generator<Frame> {
  for (;;) {
    yield* enter()
    const s = sleep()
    for (let i = 0; i < 60; i++) yield s.next().value
    yield* wake()
    const lk = look()
    for (let i = 0; i < 60; i++) yield lk.next().value
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
