// Layer 1 — art, data only. shiba_band.py's palette and sprites: one
// character a pixel, `.` transparent. Popo stands and lies facing LEFT, toward
// the bubble, and sits facing you; `mirror` gives the right-facing runner.
//
// Row pairs at size 1: every sprite is an odd number of rows tall and stands
// on row 10, so its top is always even and its rows pair as ears|head,
// eyes|nose, chest|belly, paws|grass. No outlines: the only dark pixels (eyes,
// nose, open mouth) always sit under or beside coloured ones.

import type { Palette } from '../../pixels'
import { FLOOR } from './scene-consts'

export const PAL: Palette = {
  O: [222, 152, 94], // coat
  R: [152, 98, 58], // coat shadow, shut eyes, between the legs
  C: [235, 198, 151], // cream: muzzle, chest, socks
  K: [22, 20, 30], // eyes, nose, open mouth (and the squid's eyes)
  b: [246, 248, 255], // bone
  S: [217, 119, 87], // the squid, exactly as the goose's
  s: [142, 64, 48], // its legs, shut eyes
  G: [96, 160, 78], // grass
  g: [56, 106, 50], // grass, dark tufts
  u: [170, 160, 146], // dust kicked up
  z: [190, 190, 232], // the z's (overlay characters)
  y: [250, 214, 84], // barks and the squid's fright, "!"
  w: [255, 244, 196], // a pleased twinkle, "*"
  q: [250, 214, 84], // the proposal's "?", the barks' yellow
  h: [240, 124, 150], // a pet's hearts, as the goose's
}

/** Characters drawn over the pixels, by colour key: single-width, basic plane. */
export const GLYPH: Readonly<Record<string, string>> = { z: 'z', y: '!', w: '*', q: '?', h: '♥' }

export type Body = 'gone' | 'stand' | 'run1' | 'run2' | 'bark' | 'sit' | 'proud' | 'lie'
export type Eyes = 'open' | 'left' | 'right' | 'shut'
export type Mouth = 'shut' | 'open' | 'bone'
export type SquidEyes = 'fwd' | 'left' | 'right' | 'up' | 'shut'
export type BoneSize = 'whole' | 'bitten' | 'nub'

type Patch = readonly [row: number, col: number, pixels: string]
type Tail = readonly [row: number, col: number, rows: readonly string[]]

/** Side view, facing left. Front edge (the nose) at column 3. */
const STAND = [
  '......O.O.........',
  '.....OOOO.........',
  '...OOOKOO.........',
  '...KOOOOOOOOOOOO..',
  '....CCCOOOOOOOOR..',
  '.....CCCCCOOOORR..',
  '.....C.C....C.C...',
]
const STAND_FRONT = 3
const LEGS: Readonly<Record<'stand' | 'run1' | 'run2', string>> = {
  stand: '.....C.C....C.C...',
  run1: '....C...C..C...C..',
  run2: '.......CC...CC....',
}
/** (row, col, pixels) patches; '.' leaves the base. */
const STAND_MOUTH: Readonly<Record<Mouth, readonly Patch[]>> = {
  shut: [],
  open: [[4, 4, '  '], [5, 3, 'CC']], // jaw dropped
  bone: [], // jaws shut on it
}
const STAND_TAIL: readonly Tail[] = [
  [1, 12, ['.OO.', 'OCCO']],
  [1, 12, ['..OO', '.OCO']],
  [1, 12, ['OO..', 'OCC.']],
]

/** Sitting, facing you. Front edge (left cheek) at column 1. */
const SIT = [
  '..OO....OO....',
  '..OOOOOOOO....',
  '.O___OO___O...', // ___ the eye slots: one dark pixel each
  '.OCCCKKCCCO...',
  '..CCC__CCCOO..', // __ the mouth
  '..OCC__CCOO...',
  '..OCCRRCCOO...', // front legs, shadow between them
]
/** Proud is the same sit: eyes shut, tail held up and still, a twinkle. */
const SIT_PROUD = SIT
export const PROUD_TAIL = 1
const SIT_FRONT = 1
const SIT_EYES: Readonly<Record<Eyes, string>> = { open: 'OKO', left: 'KOO', right: 'OOK', shut: 'ORO' }
/** Rows 4 and 5, columns 5-6. */
const SIT_MOUTH: Readonly<Record<Mouth, readonly [string, string]>> = {
  shut: ['CC', 'CC'],
  open: ['CC', 'KK'], // jaw drops: nose, then mouth
  bone: ['CC', 'CC'], // jaws shut on it
}
const SIT_TAIL: readonly Tail[] = [
  [2, 10, ['....', '..OO', '..OC', '.OC.']],
  [2, 10, ['..OO', '..OC', '..O.', '.O..']],
  [2, 10, ['....', '....', '..OO', 'OOCC']],
]

/** Lying down, facing left, head up. Front edge (the nose) at column 0. */
const LIE = [
  '...O.O.........',
  '..OOOO.........',
  'OOO_OO.........', // _ the eye
  'KOOOOOOOOOOOOO.',
  'CCCCCOOOOOOOOR.',
]
/** The head turned to you. */
const LIE_HEAD_FRONT = ['.O..O.', '.OOOO.', 'O_OO_O', 'OCKKCO']
const LIE_FRONT = 0
const LIE_EYE: Readonly<Record<Eyes, string>> = { open: 'K', shut: 'R', left: 'K', right: 'K' }
const LIE_TAIL: readonly Tail[] = [
  [1, 10, ['.OO.', 'OCCO']],
  [1, 10, ['..OO', '.OCO']],
  [1, 10, ['OO..', 'OCC.']],
]

/** The squid, as the goose's: 6 x 5, faces left, eyes stamped on. */
const SQUID = ['.SSSS.', 'SSSSSS', 'SSSSSS', 'SSSSSS']
const SQUID_EYES: Readonly<Record<SquidEyes, readonly (readonly [number, number, string])[]>> = {
  fwd: [[2, 1, 'K'], [2, 4, 'K']],
  left: [[2, 0, 'K'], [2, 3, 'K']],
  right: [[2, 2, 'K'], [2, 5, 'K']],
  up: [[1, 1, 'K'], [1, 4, 'K']],
  shut: [[2, 1, 's'], [2, 4, 's']],
}
const SQUID_LEGS = ['s.s.s.', '.s.s.s']
export const SQUID_H = SQUID.length + 1

/**
 * One bone, the same shape in the mouth and on the ground. Three rows tall
 * with its bar on the middle row, so it always sits with its top on an odd
 * row and only ever moves a whole cell. Bites shorten it from the right.
 */
export const BONE: Readonly<Record<BoneSize, readonly string[]>> = {
  whole: ['b....b', 'bbbbbb', 'b....b'],
  bitten: ['b...', 'bbbb', 'b...'],
  nub: ['b.', 'bb', 'b.'],
}
/** Bar on the mouth row (canvas row 8). */
export const BONE_MOUTH_Y = 7
/** Bar on row 10, lower knobs in the grass. */
export const BONE_GROUND_Y = 9

/** Overwrite a run of pixels; '.' in `pixels` keeps what is there, ' ' makes it transparent. */
function patch(rows: string[], r: number, c: number, pixels: string) {
  const row = rows[r]!
  let out = ''
  for (let i = 0; i < pixels.length; i++) {
    const ch = pixels[i]!
    out += ch === '.' ? row[c + i]! : ch === ' ' ? '.' : ch
  }
  rows[r] = row.slice(0, c) + out + row.slice(c + pixels.length)
}

function tail(rows: string[], [r0, c0, px]: Tail) {
  px.forEach((p, i) => patch(rows, r0 + i, c0, p))
}

const mirror = (rows: readonly string[]) => rows.map(r => [...r].reverse().join(''))

export type DogState = { body: Body; facing: 'L' | 'R'; eyes: Eyes; mouth: Mouth; head: 'side' | 'front'; tail: number }

/**
 * [rows, front] for the frame's dog: rows are facing-corrected, `front` is the
 * column in them that lands on the frame's `x`. Null when offscreen.
 */
export function dogSprite(st: DogState): readonly [readonly string[], number] | null {
  const { body } = st
  let rows: string[]
  let front: number
  if (body === 'gone') return null
  if (body === 'stand' || body === 'run1' || body === 'run2' || body === 'bark') {
    rows = [...STAND]
    rows[6] = LEGS[body === 'bark' ? 'stand' : body]
    for (const [r, c, px] of STAND_MOUTH[body === 'bark' ? 'open' : st.mouth]) patch(rows, r, c, px)
    if (st.eyes === 'shut') patch(rows, 2, 6, 'R')
    tail(rows, STAND_TAIL[st.tail]!)
    front = STAND_FRONT
  } else if (body === 'sit' || body === 'proud') {
    rows = [...(body === 'proud' ? SIT_PROUD : SIT)]
    rows[2] = rows[2]!.replaceAll('___', SIT_EYES[st.eyes])
    const [m4, m5] = SIT_MOUTH[st.mouth]
    rows[4] = rows[4]!.replaceAll('__', m4)
    rows[5] = rows[5]!.replaceAll('__', m5)
    tail(rows, SIT_TAIL[st.tail]!)
    front = SIT_FRONT
  } else {
    rows = [...LIE]
    if (st.head === 'front') {
      LIE_HEAD_FRONT.forEach((px, i) => { rows[i] = px + rows[i]!.slice(px.length) })
      rows[2] = rows[2]!.replaceAll('_', st.eyes === 'shut' ? 'R' : 'K')
    } else {
      rows[2] = rows[2]!.replaceAll('_', LIE_EYE[st.eyes])
    }
    tail(rows, LIE_TAIL[st.tail]!)
    front = LIE_FRONT
  }
  // flipped in place: same box, same `front`
  return [st.facing === 'R' ? mirror(rows) : rows, front]
}

export function squidRows(eyes: SquidEyes = 'fwd', legs = 0): string[] {
  const rows = SQUID.map(r => [...r])
  for (const [r, c, ch] of SQUID_EYES[eyes]) rows[r]![c] = ch
  return [...rows.map(r => r.join('')), SQUID_LEGS[((legs % 2) + 2) % 2]!]
}

export const spriteTop = (rows: readonly string[]) => FLOOR - rows.length + 1
