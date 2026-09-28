// Layer 1 — art, data only. hippo_band.py's palette and sprites: 12px-wide
// string grids, one character per pixel, keyed into PAL; `.` is transparent.
// A 22x12 canvas drawn for the band, so the whole animal sits in its bottom
// two thirds with the bubble anchored at column 5, row 10.

import type { Palette } from '../../../pixels'

export const PAL: Palette = {
  K: [36, 27, 46],
  G: [138, 155, 181],
  L: [206, 214, 232],
  P: [242, 160, 181],
  W: [245, 243, 238],
  M: [160, 52, 84],
  D: [16, 52, 86],
  w: [74, 120, 158],
  b: [140, 200, 240],
  R: [112, 80, 148],
  H: [152, 112, 190],
  s: [250, 214, 214],
  z: [190, 180, 230],
  C: [211, 105, 80],
  x: [0, 0, 0],
}

export const HEAD = [
  '..K......K..',  // 0 ear tips
  '.KPKKKKKKPK.',  // 1 ears
  '.KGGGGGGGGK.',  // 2 brow
  '.KGWKGGKWGK.',  // 3 eyes
  'KLLLLLLLLLLK',  // 4 muzzle
  'KLLKLLLLKLLK',  // 5 nostrils
  'KPKKKKKKKKPK',  // 6 mouth line and cheeks
]

export type Eyes = 'open' | 'left' | 'right' | 'shut'
export const EYES: Readonly<Record<Eyes, string>> = {
  open: '.KGWKGGKWGK.',
  left: '.KGKWGGKWGK.',
  right: '.KGWKGGWKGK.',
  shut: '.KGKKGGKKGK.',
}

export type Mouth = 'shut' | 'open' | 'gape'
export const MOUTH: Readonly<Record<'shut' | 'open', string>> = {
  shut: 'KPKKKKKKKKPK',
  open: 'KPKWMMMMWKPK',
}

export const EARS_UP = ['.K........K.', '.KPKKKKKKPK.']

// blocked: the whole head is mouth, tusks top and bottom
export const GAPE = [
  '..K......K..',
  '.KPKKKKKKPK.',
  '.KGGGGGGGGK.',
  'KGLLLLLLLLGK',
  'KLKWMMMMWKLK',
  'KLKMPPPPMKLK',
  'KLKWMMMMWKLK',
]

/**
 * The squid, at 7x5 — the reference's structure at the size the canvas
 * allows: ears as a block off each side, two eyes a column in from each edge,
 * and the outer legs under the head's own edges.
 */
export type SquidPose = 'walk' | 'step' | 'ouch' | 'caught'
export const SQUID: Readonly<Record<SquidPose, readonly string[]>> = {
  walk: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    '.C.C.C.',
  ],
  step: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    '.C...C.',
  ],
  ouch: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    'C.C.C.C',
  ],
  caught: [
    '.C.C.',
    'CxCxC',
    '.CCC.',
  ],
}
export const SQUID_W = 7
export const SQUID_Y = 5

// the near bank runs off the bottom edge
export const POND = [
  '..KHKKKKKKKKKKKKKKHK..',
  '.KHKDDDDDDDDDDDDDDKHK.',
  'KHRKDDDDDDDDDDDDDDKRHK',
]

export const SPRITE_W = 12
export const SPRITE_H = 7

/** The rows for one pose, with the face filled in. */
export function sprite(eyes: Eyes = 'open', mouth: Mouth = 'shut', ears = false): string[] {
  if (mouth === 'gape') {
    return [...GAPE]
  }
  const rows = [...HEAD]
  rows[3] = EYES[eyes]
  rows[6] = MOUTH[mouth]
  if (ears) {
    rows.splice(0, 2, ...EARS_UP)
  }
  return rows
}
