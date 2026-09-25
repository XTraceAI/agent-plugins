// Layer 1 — art, data only. penguin_band.py's palette and sprites: one
// character a pixel, `.` transparent. An Adelie penguin on sea ice.
//
// At size 1 a cell coloured on top and empty below prints an upper half-block,
// and a font that draws that glyph low shows a dark line at the top of the
// cell. So every bottom edge either ends on a cell's lower row or stands on
// the ice: head top | eyes over beak | chest | belly | feet over the ice.

import type { Palette } from '../../pixels'

export const PAL: Palette = {
  K: [52, 64, 100],
  W: [242, 245, 250],
  G: [104, 116, 150],
  O: [250, 170, 40],
  P: [236, 120, 150],
  S: [196, 222, 242],
  I: [110, 170, 214],
  U: [60, 120, 190],
  D: [20, 44, 92],
  V: [92, 104, 132],
  L: [170, 186, 206],
  C: [217, 119, 87],
  c: [142, 64, 48],
  k: [22, 20, 30],
  H: [120, 232, 214],
  Z: [190, 184, 230],
}

/** The penguin, standing, 10 x 9. */
export const STAND = [
  '...KKKK...',
  '..KKKKKK..',
  '..KWKKWK..',
  '..KKOOKK..',
  '..KWWWWK..',
  '..KWWWWK..',
  '..KWWWWK..',
  '..KWWWWK..',
  '..OO..OO..',
]

export type Eyes = 'open' | 'shut' | 'right' | 'left'
/** Rows 2 and 3: the eyes and the beak. */
export const FACE: Readonly<Record<Eyes, readonly [string, string]>> = {
  open: ['..KWKKWK..', '..KKOOKK..'],
  shut: ['..KGKKGK..', '..KKOOKK..'],
  right: ['..KKKKWK..', '..KKKKKKO.'],
  left: ['..KWKKKK..', '.OKKKKKK..'],
}

export type Feet = 'stand' | 'a' | 'b'
export const FEET: Readonly<Record<Feet, string>> = {
  stand: '..OO..OO..',
  a: '..OO..O...',
  b: '...O..OO..',
}

/**
 * Flippers, as [column, row, colour] in STAND's coordinates. Each fills both
 * rows of a cell, or only the lower one: never the upper row alone.
 */
export type LeftFlipper = 'down' | 'out' | 'pocket' | 'phone' | 'ear'
export const LEFT_FLIPPER: Readonly<Record<LeftFlipper, readonly (readonly [number, number, string])[]>> = {
  down: [[1, 6, 'K'], [1, 7, 'K']],
  out: [[1, 4, 'K'], [1, 5, 'K'], [0, 5, 'K']],
  pocket: [[1, 6, 'K'], [1, 7, 'K'], [3, 6, 'K'], [3, 7, 'K']],
  phone: [[1, 4, 'K'], [1, 5, 'K'], [0, 4, 'H'], [0, 5, 'H']],
  ear: [[1, 4, 'K'], [1, 5, 'K'], [1, 2, 'H'], [1, 3, 'H']],
}
export type RightFlipper = 'down' | 'out' | 'point'
export const RIGHT_FLIPPER: Readonly<Record<RightFlipper, readonly (readonly [number, number, string])[]>> = {
  down: [[8, 6, 'K'], [8, 7, 'K']],
  out: [[8, 4, 'K'], [8, 5, 'K'], [9, 5, 'K']],
  point: [[8, 5, 'K'], [9, 5, 'K']],
}

/** Asleep: head sunk into the shoulders, 10 x 7. */
export const HUNCH = [
  '...KKKK...',
  '..KGKKGK..',
  '.KKKOOKKK.',
  '.KKWWWWKK.',
  '..KWWWWK..',
  '..KWWWWK..',
  '..OO..OO..',
]
export const HUNCH_EYES: Readonly<Record<'open' | 'shut', string>> = {
  open: '..KWKKWK..',
  shut: '..KGKKGK..',
}

/**
 * On its belly, facing left, 10 x 5: beak at the left, feet trailing right,
 * belly on the ice.
 */
export const LIE = [
  '..KKKKKK..',
  '.KWKKKKKKO',
  'OKKKKKKKKO',
  'OWWWWWWWWO',
  '..WWWWWW..',
]

/**
 * The Claude squid, from goose_band.py: 6 x 5, faces left. As in the goose,
 * its legs share a cell with the ground, so the gaps between them show ice
 * and the body above them is whole cells.
 */
export const SQUID = [
  '.CCCC.',
  'CCCCCC',
  'CCCCCC',
  'CCCCCC',
]
export type SquidEyes = 'fwd' | 'left' | 'right' | 'up' | 'shut'
export const SQUID_EYES: Readonly<Record<SquidEyes, Readonly<Record<string, string>>>> = {
  fwd: { '2,1': 'k', '2,4': 'k' },
  left: { '2,0': 'k', '2,3': 'k' },
  right: { '2,2': 'k', '2,5': 'k' },
  up: { '1,1': 'k', '1,4': 'k' },
  shut: { '2,1': 'c', '2,4': 'c' },
}
export const SQUID_LEGS: Readonly<Record<number, string>> = { 0: 'c.c.c.', 1: '.c.c.c' }

/**
 * The whale's head, 8 x 12, coming straight up mouth-first the way a humpback
 * lunge-feeds: jaws either side, pale throat pleats running down into the
 * body, which runs off the bottom of the canvas.
 */
export type WhaleMouth = 'open' | 'shut'
export const WHALE: Readonly<Record<WhaleMouth, readonly string[]>> = {
  open: [
    'V......V',
    'VV....VV',
    'VVPPPPVV',
    'VWPPPPWV',
    'VVLLLLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
  ],
  shut: [
    '..VVVV..',
    '.VVVVVV.',
    'VVKKKKVV',
    'VWVLLVWV',
    'VVLLLLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
    'VVLVVLVV',
  ],
}

export type Posture = 'stand' | 'hunch' | 'lie'
export type Mouth = 'shut' | 'open'

const put = (rows: string[], c: number, r: number, ch: string) => {
  rows[r] = rows[r]!.slice(0, c) + ch + rows[r]!.slice(c + 1)
}

export function squidRows(eyes: SquidEyes = 'fwd', legs = 0): string[] {
  const grid = SQUID.map(r => [...r])
  for (const [at, ch] of Object.entries(SQUID_EYES[eyes])) {
    const [r, c] = at.split(',').map(Number)
    grid[r!]![c!] = ch
  }
  return [...grid.map(r => r.join('')), SQUID_LEGS[legs]!]
}

/** The penguin's rows for one pose. */
export function penguin(
  posture: Posture = 'stand', eyes: Eyes = 'open', mouth: Mouth = 'shut',
  lf: LeftFlipper = 'down', rf: RightFlipper = 'down', feet: Feet = 'stand',
): string[] {
  if (posture === 'lie') return [...LIE]
  if (posture === 'hunch') {
    const rows = [...HUNCH]
    rows[1] = HUNCH_EYES[eyes === 'open' ? 'open' : 'shut']
    return rows
  }
  const rows = [...STAND]
  rows[2] = FACE[eyes][0]
  rows[3] = FACE[eyes][1]
  if (mouth === 'open') {
    if (eyes === 'right') put(rows, 7, 3, 'P')
    else if (eyes === 'left') put(rows, 2, 3, 'P')
    else rows[4] = '..KWPPWK..'
  }
  rows[8] = FEET[feet]
  for (const [c, r, ch] of [...LEFT_FLIPPER[lf], ...RIGHT_FLIPPER[rf]]) put(rows, c, r, ch)
  return rows
}
