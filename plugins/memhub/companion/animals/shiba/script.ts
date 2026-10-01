// Layer 3 — choreography. shiba_band.py's poses, one frame per 1/20 s,
// ported frame for frame. Finite poses end exactly where the next begins; the
// endless two never move the body, only eyes, head turn, tail and overlays,
// so they can be cut on any frame.

import type { Tone } from '../../animal'
import { type Body, BONE_GROUND_Y, BONE_MOUTH_Y, type BoneSize, type Eyes, type Mouth, PROUD_TAIL, SQUID_H, type SquidEyes } from './art'
import { FLOOR, HOME, W } from './scene-consts'

/** A character over the pixels: x, y, and the palette key that names it. */
export type Glyph = readonly [x: number, y: number, key: string]
/** A loose pixel: x, y, palette key. */
export type Speck = readonly [x: number, y: number, key: string]
export type Squid = readonly [x: number, y: number, eyes: SquidEyes, legs: number]
export type Bone = readonly [x: number, y: number, size: BoneSize]

export const FPS = 20

export type Frame = {
  body: Body
  x: number
  facing: 'L' | 'R'
  eyes: Eyes
  mouth: Mouth
  head: 'side' | 'front'
  tail: number
  squid: Squid | null
  bone: Bone | null
  specks: readonly Speck[]
  glyphs: readonly Glyph[]
  said: string | null
  shown: number
  tone: Tone
}

/** The bubble's anchor: column 5, row 10, in canvas pixels. */
export const BUBBLE_AT = { column: 5, row: 10 }
/** Frames the finished message stays up (>= 40). */
const HOLD = 44
/** Frames a proposal stays up: long enough to read it and reach for a button. */
const ASK = 8 * FPS
/** One watching cycle in this many carries the glance; the rest only blink. */
const GLANCE_EVERY = 4
/** Frames offscreen after leave, before it trots back on. */
const GONE_FRAMES = 2 * FPS
/** Over the sitting dog's head, between the ears, and the row above them. */
const HEAD_X = HOME + 4
const ABOVE_HEAD = 2
/** Characters revealed per frame. */
const TYPE_RATE = 1.6

/** One frame: plain values only. */
export function S(kw: Partial<Frame> = {}): Frame {
  return {
    body: 'sit', x: HOME, facing: 'L', eyes: 'open', mouth: 'shut',
    head: 'side', tail: 0, squid: null, bone: null, specks: [],
    glyphs: [], said: null, shown: 0, tone: 'advice',
    ...kw,
  }
}

/** The speaking position. */
const NEUTRAL = () => S()
/** Offscreen, between leave and enter. */
const GONE = () => S({ body: 'gone', x: W })
/** Tail phases, a full wag. */
const WAG = [0, 1, 2, 1] as const
const wag = (i: number) => WAG[((i % 4) + 4) % 4]!

function* hold(st: Frame, n: number): Generator<Frame> {
  for (let i = 0; i < n; i++) yield { ...st }
}

/** The bone crosswise in the jaws of a dog standing at x, facing left. */
const carried = (x: number): Bone => [x - 2, BONE_MOUTH_Y, 'whole']

/** Trot from x0 to x1 (exclusive of x1), kicking up dust behind. */
function* run(x0: number, x1: number, step: number, facing: 'L' | 'R', carry = false, kw: Partial<Frame> = {}): Generator<Frame> {
  const sign = x1 > x0 ? 1 : -1
  for (let f = 0, x = x0; (x - x1) * sign < 0; f++, x += step * sign) {
    const back = x + (facing === 'L' ? 12 : -2)
    const dust: Speck[] = f % 2 === 0 ? [[back, FLOOR, 'u']] : []
    yield S({
      body: f % 2 === 0 ? 'run1' : 'run2', x, facing,
      tail: 1, specks: dust, bone: carry ? carried(x) : null, ...kw,
    })
  }
}

/** Reveal the text, flapping the mouth between the pair in `flap`. */
function* typing(text: string, flap: readonly [Mouth, Mouth], kw: Partial<Frame> = {}): Generator<Frame> {
  let k = 0.0
  for (let f = 0; k < text.length; f++) {
    k = Math.min(text.length, k + TYPE_RATE)
    yield S({ mouth: flap[f % 2]!, said: text, shown: Math.trunc(k), ...kw })
  }
}

/** Offscreen → asleep's starting point: trots in from the right, sits. */
export function* enter(): Generator<Frame> {
  yield GONE()
  yield* run(W - 1, HOME, 3, 'L')
  yield* hold(S({ body: 'stand' }), 3)
  yield* hold(NEUTRAL(), 4)
}

/**
 * Asleep, endless. Settles down first (from sitting, where enter and look
 * both leave it), then dozes; every so often it lifts its eyes, glances
 * around, looks at you, wags, and dozes off again.
 */
export function* sleep(): Generator<Frame> {
  yield NEUTRAL()
  yield* hold(S({ eyes: 'shut' }), 6) // drowsy, still sitting
  yield* hold(S({ body: 'lie', eyes: 'open' }), 3)
  for (let f = 0; ; f++) {
    const p = f % 200
    if (p >= 120 && p < 176) {
      // the glance
      const q = p - 120
      const head = q >= 16 && q < 36 ? 'front' : 'side'
      const eyes: Eyes = [10, 11, 44, 45].includes(q) ? 'shut' : 'open'
      yield S({ body: 'lie', eyes, head, tail: wag(Math.floor(q / 3)) })
    } else {
      const tail = p >= 40 && p < 72 ? wag(Math.floor(f / 8)) : 0 // a lazy thump
      // two z's, rising two rows a step
      const zs: Glyph[] = [Math.floor(f / 12) % 3, (Math.floor(f / 12) + 1) % 3].map(ph => [10 + ph, 4 - 2 * ph, 'z'])
      yield S({ body: 'lie', eyes: 'shut', tail, glyphs: zs })
    }
  }
}

/** Asleep → awake: eyes, head up, onto its feet, sits. */
export function* wake(): Generator<Frame> {
  yield* hold(S({ body: 'lie', eyes: 'shut' }), 3)
  yield* hold(S({ body: 'lie', eyes: 'open' }), 4)
  yield* hold(S({ body: 'lie', eyes: 'open', head: 'front' }), 4)
  yield* hold(S({ body: 'stand' }), 3)
  yield NEUTRAL()
}

/**
 * Awake, endless: sits facing you, tail going. A blink every cycle, and the
 * glance left and right only on one cycle in four, as the goose does:
 * watching someone type is mostly stillness, and a glance every few seconds
 * reads as a fidget.
 */
export function* look(): Generator<Frame> {
  for (let f = 0; ; f++) {
    const p = f % 120
    const glancing = Math.floor(f / 120) % GLANCE_EVERY === 0
    const eyes: Eyes =
      glancing && p >= 20 && p < 34 ? 'left'
      : glancing && p >= 60 && p < 74 ? 'right'
      : p === 95 || p === 96 ? 'shut'
      : 'open'
    yield S({ eyes, tail: wag(Math.floor(f / 4)) })
  }
}

/** Awake → the speaking position: a blink, ears up, a quick wag. */
export function* rise(): Generator<Frame> {
  yield NEUTRAL()
  yield* hold(S({ eyes: 'shut' }), 2)
  for (const tail of [1, 2, 1, 2, 1, 0]) yield S({ tail })
  yield NEUTRAL()
}

/** advice: off to the right, back with a bone, drop it, look at you. */
function* fetch(text: string): Generator<Frame> {
  yield* hold(S({ body: 'stand', facing: 'R' }), 3)
  yield* run(HOME, W + 1, 3, 'R')
  yield* hold(GONE(), 6)
  yield* run(W - 1, HOME, 3, 'L', true, { mouth: 'bone' })
  yield* hold(S({ body: 'stand', mouth: 'bone', bone: carried(HOME) }), 3)
  const bx = HOME + 2 // centred under the chin
  const held: Bone = [bx, BONE_MOUTH_Y, 'whole']
  const down: Bone = [bx, BONE_GROUND_Y, 'whole']
  for (let f = 0; f < 4; f++) yield S({ mouth: 'bone', bone: held, tail: wag(f) })
  yield S({ mouth: 'open', bone: held, tail: 1 }) // lets go...
  yield S({ mouth: 'open', bone: down, tail: 2, specks: [[bx - 1, 10, 'u'], [bx + 6, 10, 'u']] }) // ...thump
  for (let f = 0; f < 4; f++) yield S({ bone: down, tail: wag(f) })
  let i = 0
  for (const st of typing(text, ['open', 'shut'], { bone: down, tone: 'advice' })) {
    yield { ...st, tail: wag(i++) }
  }
  for (let f = 0; f < HOLD; f++) {
    yield S({
      bone: down, tail: wag(Math.floor(f / 2)),
      eyes: f % 24 === 16 || f % 24 === 17 ? 'shut' : 'open',
      said: text, shown: text.length, tone: 'advice',
    })
  }
  // the message is done: it picks the bone back up and eats it in three
  // bites, so the scene ends exactly where it began
  yield* hold(S({ mouth: 'open', bone: down }), 2)
  const next: Record<BoneSize, BoneSize | null> = { whole: 'bitten', bitten: 'nub', nub: null }
  for (const size of ['whole', 'bitten', 'nub'] as const) {
    yield* hold(S({ mouth: 'bone', bone: [bx, BONE_MOUTH_Y, size] }), 4)
    for (let c = 0; c < 2; c++) {
      // crunch
      const left = next[size]
      yield S({
        mouth: 'open', bone: left ? [bx, BONE_MOUTH_Y, left] : null,
        specks: [[bx + 4 + c, 8 + 2 * c, 'b']],
      })
    }
  }
  yield* hold(S({ mouth: 'shut', tail: 1 }), 2)
  yield* hold(S({ mouth: 'shut', tail: 2 }), 2)
}

/** The squid's top row on the ground. */
const SQ_Y = FLOOR - SQUID_H + 1
/** All the way on screen, at the left edge. */
const SQ_STOP = 0
/** The dog stands two columns back to face it. */
const BARK_X = HOME + 2

/**
 * blocked: a squid wanders all the way in and lingers; the dog barks, the
 * squid jumps in fright and skedaddles, the dog sees it off, then sits proud
 * and says it.
 */
function* chase(text: string): Generator<Frame> {
  yield* hold(S({ eyes: 'left' }), 4) // something's coming
  yield* hold(S({ body: 'stand', x: BARK_X }), 3) // up on its feet
  for (let i = 0, sx = -6; sx <= SQ_STOP; i++, sx++) {
    // the squid wanders in
    yield S({ body: 'stand', x: BARK_X, squid: [sx, SQ_Y, 'right', i] })
  }
  const sq = (eyes: SquidEyes, y = SQ_Y, legs = 0): Squid => [SQ_STOP, y, eyes, legs]
  yield* hold(S({ body: 'stand', x: BARK_X, squid: sq('fwd') }), 4) // it lingers
  yield* hold(S({ body: 'stand', x: BARK_X, squid: sq('right') }), 6) // eyes the dog
  yield* hold(S({ body: 'stand', x: BARK_X, squid: sq('fwd') }), 4)
  const bark: Glyph[] = [[BARK_X - 1, 2, 'y']]
  const fright: Glyph[] = [[SQ_STOP + 2, 2, 'y']] // over the jumping squid
  yield S({ body: 'bark', x: BARK_X, squid: sq('up'), glyphs: bark }) // WOOF
  yield* hold(S({ body: 'bark', x: BARK_X, squid: sq('up', SQ_Y - 2), glyphs: [...bark, ...fright] }), 2) // it jumps...
  yield* hold(S({ body: 'stand', x: BARK_X, squid: sq('up', SQ_Y - 2), glyphs: fright }), 2)
  yield S({ body: 'stand', x: BARK_X, squid: sq('left') }) // ...lands, turns
  // and skedaddles, two columns a frame; the dog gives chase
  let x = BARK_X
  const away = [-1, -3, -5, null]
  for (let f = 0; f < away.length; f++) {
    const sx = away[f]!
    x -= 1
    const loud = f % 2 === 1
    yield S({
      body: loud ? 'bark' : (['run1', 'run2'] as const)[f % 2]!, x,
      squid: sx !== null ? [sx, SQ_Y, 'left', f] : null,
      glyphs: loud ? [[x - 1, 2, 'y']] : [], tail: 1,
      specks: loud ? [] : [[x + 12, FLOOR, 'u']],
    })
  }
  yield* hold(S({ body: 'bark', x, glyphs: [[x - 1, 2, 'y']] }), 3)
  yield* hold(S({ body: 'stand', x }), 3) // ...and stay out
  yield* run(x, HOME, 1, 'R') // trots home
  yield* hold(S({ body: 'proud', eyes: 'shut', tail: PROUD_TAIL }), 6) // proud
  yield* typing(text, ['open', 'shut'], { body: 'proud', eyes: 'shut', tail: PROUD_TAIL, tone: 'blocked' })
  for (let f = 0; f < HOLD; f++) {
    const twinkle: Glyph[] = f % 16 < 8 ? [[17, 4, 'w']] : [] // pleased
    yield S({
      body: 'proud', eyes: f >= 20 && f < 30 ? 'open' : 'shut', tail: PROUD_TAIL,
      said: text, shown: text.length, tone: 'blocked', glyphs: twinkle,
    })
  }
  yield* hold(S({ eyes: 'shut' }), 2)
}

/**
 * proposed: something new. Ears up, a look both ways, a "!" over its head,
 * and it spins round twice for joy; then it says it and waits with a "?"
 * bobbing over its head, long enough to reach for a button.
 */
function* present(text: string): Generator<Frame> {
  yield* hold(S({ eyes: 'left' }), 4)
  yield* hold(S({ eyes: 'right' }), 4)
  yield* hold(S({ tail: 1, glyphs: [[HEAD_X, ABOVE_HEAD, 'y']] }), 8)
  for (let i = 0; i < 4; i++) {
    // round and round
    const twinkle: Glyph[] = [[HEAD_X - 4 + 8 * (i % 2), ABOVE_HEAD, 'w']]
    yield* hold(S({ body: 'stand', facing: i % 2 === 0 ? 'R' : 'L', tail: wag(i), glyphs: twinkle }), 3)
  }
  yield* hold(S({ tail: 1 }), 3)
  let i = 0
  for (const st of typing(text, ['open', 'shut'], { tone: 'proposed' })) {
    yield { ...st, tail: wag(i++) }
  }
  for (let g = 0; g < ASK; g++) {
    const bob = Math.floor(g / 10) % 2 ? ABOVE_HEAD - 2 : ABOVE_HEAD
    const eyes: Eyes = g % 60 === 50 || g % 60 === 51 ? 'shut' : g % 120 >= 80 && g % 120 < 92 ? 'left' : 'open'
    yield S({
      eyes, tail: wag(Math.floor(g / 4)), said: text, shown: text.length,
      tone: 'proposed', glyphs: [[HEAD_X, bob, 'q']],
    })
  }
}

/** Starts and ends on the speaking position, so it can run back to back. */
export function* speak(text: string, tone: Tone = 'advice'): Generator<Frame> {
  yield NEUTRAL()
  yield* tone === 'blocked' ? chase(text) : tone === 'proposed' ? present(text) : fetch(text)
  yield NEUTRAL()
}

/**
 * A click: a happy squint and a pant, tail going hard, while two hearts
 * float up off its head. Starts and ends on look's first frame.
 */
export function* pet(): Generator<Frame> {
  const hearts = (k: number): Glyph[] => [[HEAD_X - 3, ABOVE_HEAD - 2 * k, 'h'], [HEAD_X + 3, ABOVE_HEAD - 2 * k, 'h']]
  yield NEUTRAL()
  for (let f = 0; f < 16; f++) {
    yield S({ eyes: 'shut', mouth: Math.floor(f / 4) % 2 === 0 ? 'open' : 'shut', tail: wag(f), glyphs: hearts(Math.floor(f / 8)) })
  }
  yield* hold(S({ eyes: 'shut', tail: 1, glyphs: hearts(1) }), 6)
  yield* hold(S({ tail: 1 }), 6)
  yield NEUTRAL()
}

/**
 * The speaking position → offscreen: a goodbye wag, then off to the right,
 * and stays gone a while — without the empty beat it would trot straight
 * back on, which reads as a glitch rather than a trip.
 */
export function* leave(): Generator<Frame> {
  yield NEUTRAL()
  for (const tail of [1, 2, 1]) yield S({ tail }) // a goodbye wag
  yield* hold(S({ body: 'stand', facing: 'R' }), 3)
  yield* run(HOME, W + 1, 3, 'R') // last frame: tail tip at col 20
  yield* hold(GONE(), GONE_FRAMES)
}

export const SAMPLE: Readonly<Record<Tone, string>> = {
  advice: 'Rule fired: run only the touched test suites',
  blocked: 'Blocked: never force-push to a shared branch',
  proposed: 'New rule↗ proposed, not active yet: Pin the MCP server when spawning claude -p',
}

/** The ring the mod walks, for `/shiba demo`. */
export function* cycle(): Generator<Frame> {
  for (;;) {
    yield* enter()
    const s = sleep()
    for (let i = 0; i < 320; i++) yield s.next().value as Frame
    yield* wake()
    const lk = look()
    for (let i = 0; i < 130; i++) yield lk.next().value as Frame
    yield* rise()
    yield* speak(SAMPLE.advice)
    yield* speak(SAMPLE.blocked, 'blocked')
    yield* speak(SAMPLE.proposed, 'proposed')
    yield* leave()
  }
}

export const POSES: Readonly<Record<string, () => Generator<Frame>>> = {
  enter, sleep, wake, look, rise, leave,
  advice: () => speak(SAMPLE.advice),
  blocked: () => speak(SAMPLE.blocked, 'blocked'),
  propose: () => speak(SAMPLE.proposed, 'proposed'),
  pet,
}
