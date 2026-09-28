// Layer 1 — art, data only. goose_band.py's palette and sprites: one
// character a pixel, `.` transparent. The goose faces LEFT, toward the
// bubble; `mirror` gives the right-facing flyer for `leave`.
//
// Row pairs at size 1: (4,5) head, (6,7) neck over back, (8,9) body,
// (10,11) feet over grass. Every feature sits inside one pair.

import type { Palette } from '../../pixels'

export const PAL: Palette = {
  W: [244, 244, 236],
  w: [156, 166, 188],
  O: [247, 164, 40],
  o: [140, 60, 20],
  K: [22, 20, 30],
  R: [240, 124, 150],
  B: [48, 116, 212],
  G: [84, 146, 70],
  H: [30, 60, 34],
  C: [217, 119, 87],
  c: [142, 64, 48],
  z: [190, 188, 226],
  Y: [250, 214, 84],
}

export const STAND = [
  '..WWW........',
  'OOWWW........',
  '...WW........',
  '...WWwwwwwww.',
  '..WWWwwwwwwww',
  '...WWWWWWWWw.',
  '.............',
]

/**
 * Asleep: sat down in the grass, head laid back along the wing. One pixel
 * lower than standing and no feet, so the belly sits on row 10 over the grass
 * the way the feet do.
 */
export const TUCK = [
  '.............',
  '.............',
  '.............',
  '...WwWOO.....',
  '..WWWWwwwwww.',
  '..WWWwwwwwwww',
  '...WWWWWWWWw.',
]

export type Body = 'stand' | 'tuck' | 'flapU' | 'flapD'

/** rise and leave: shaking the wings out. */
export const FLAP: Readonly<Record<'flapU' | 'flapD', readonly string[]>> = {
  flapU: [
    '..WWW...w..w.',
    'OOWWW...ww.ww',
    '...WW...wwwww',
    '...WWwwwwwww.',
    '..WWWwwwwwwww',
    '...WWWWWWWWw.',
    '.............',
  ],
  flapD: [
    '..WWW........',
    'OOWWW........',
    '...WW........',
    '...WWWWWWWWW.',
    '..WWWWWWwwwww',
    '...WWWWWwwwww',
    '..........ww.',
  ],
}

export const FEET: Readonly<Record<number, string>> = { 0: '.....O..O....', 1: '....O....O...' }

/** `row,col` -> character, stamped over the head. The eye lives at (1,3). */
export type Eyes = 'open' | 'shut' | 'left' | 'right' | 'stern'
export const EYES: Readonly<Record<Eyes, Readonly<Record<string, string>>>> = {
  open: { '1,3': 'K' },
  shut: { '1,3': 'w' },
  left: { '1,2': 'K' },
  right: { '1,4': 'K' },
  stern: { '1,3': 'K', '0,2': 'K' },
}
export const BEAK_OPEN = { '0,1': 'O', '1,1': 'o' }
export const BLUSH = { '1,4': 'R' }
/** one solid cell on the chest */
export const BADGE = { '4,3': 'B', '5,3': 'B' }

/** The flyer: 15 x 7, faces left. Rows 0-3 goose, 4-6 legs and talons. */
export type Wings = 'up' | 'down' | 'dive'
export const FLY_BODY: Readonly<Record<Wings, readonly string[]>> = {
  up: [
    '....wwwww......',
    '......wwwWW....',
    'OOWKWWWWWWWWWw.',
    '..WWW..WWWWWww.',
  ],
  down: [
    '...............',
    '...............',
    'OOWKWWWWWWWWWw.',
    '..WWW..wwwwWww.',
  ],
  dive: [
    '...............',
    '.......wwwwwww.',
    'OOWKWWWWWWWWWw.',
    '..WWW..WWWWWww.',
  ],
}
/** the 'down' wing hangs below the body, clear of the legs at cols 9 and 11 */
export const WING_DOWN = { '4,5': 'w', '4,6': 'w', '4,7': 'w', '5,4': 'w', '5,5': 'w' }
export type Talons = 'open' | 'grip' | 'tuck'
export const FLY_LEGS: Readonly<Record<Talons, readonly string[]>> = {
  open: [
    '.........O.O...',
    '........O.O.O..',
    '...............',
  ],
  grip: [
    '.........O.O...',
    '........O...O..',
    '.......O....O..',
  ],
  tuck: [
    '...........OO..',
    '...............',
    '...............',
  ],
}
export const FLY_W = 15
/** a gripped squid's box, relative to the flyer's */
export const GRIP_DX = 7
export const GRIP_DY = 6

/** The squid: 6 x 5, faces left. */
export const SQUID = [
  '.CCCC.',
  'CCCCCC',
  'CCCCCC',
  'CCCCCC',
]
export type SquidEyes = 'fwd' | 'left' | 'right' | 'up' | 'shut'
export const SQUID_EYES: Readonly<Record<SquidEyes, Readonly<Record<string, string>>>> = {
  fwd: { '2,1': 'K', '2,4': 'K' },
  left: { '2,0': 'K', '2,3': 'K' },
  right: { '2,2': 'K', '2,5': 'K' },
  up: { '1,1': 'K', '1,4': 'K' },
  shut: { '2,1': 'c', '2,4': 'c' },
}
export const SQUID_LEGS: Readonly<Record<number, string>> = { 0: 'c.c.c.', 1: '.c.c.c' }

export const mirror = (rows: readonly string[]) => rows.map(r => [...r].reverse().join(''))

export function stamp(rows: readonly string[], marks: Readonly<Record<string, string>>): string[] {
  const grid = rows.map(r => [...r])
  for (const [at, ch] of Object.entries(marks)) {
    const [r, c] = at.split(',').map(Number)
    grid[r!]![c!] = ch
  }
  return grid.map(r => r.join(''))
}

/** The standing goose's 13 x 7 rows for one pose. */
export function gooseRows(
  body: Body, eyes: Eyes = 'open', beak: 'shut' | 'open' = 'shut',
  blush = false, badge = false, step = 0,
): string[] {
  let rows: string[]
  if (body === 'tuck') {
    rows = [...TUCK]
  } else {
    const base = body === 'flapU' || body === 'flapD' ? FLAP[body] : STAND
    const marks: Record<string, string> = { ...EYES[eyes] }
    if (beak === 'open') Object.assign(marks, BEAK_OPEN)
    if (blush) Object.assign(marks, BLUSH)
    if (badge) Object.assign(marks, BADGE)
    rows = stamp(base, marks)
  }
  if (body === 'tuck') {
    // sitting: no feet
  } else if (rows[6]!.replace(/\./g, '') === '') {
    rows[6] = FEET[step]!
  } else if (body === 'flapD') {
    rows[6] = [...FEET[step]!].map((f, i) => (f !== '.' ? f : rows[6]![i]!)).join('')
  }
  return rows
}

export function flyerRows(wings: Wings, talons: Talons, facing: 'L' | 'R' = 'L'): string[] {
  let rows = [...FLY_BODY[wings], ...FLY_LEGS[talons]]
  if (wings === 'down') rows = stamp(rows, WING_DOWN)
  return facing === 'R' ? mirror(rows) : rows
}

export const squidRows = (eyes: SquidEyes = 'fwd', legs = 0) =>
  [...stamp(SQUID, SQUID_EYES[eyes]), SQUID_LEGS[legs]!]
