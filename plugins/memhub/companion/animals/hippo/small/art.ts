// Layer 1 — art, data only. hippo_band (1).py's palette and sprites: the same
// 22x12 canvas as the big build, redrawn to read at one cell a pixel.
//
// Two canvas rows share a cell at this size, so the drawing is made of row
// PAIRS: each ear is a single pink pixel in row 0 over the dark top edge in
// row 1, which at size 1 is the upper half of its cell, pink against the
// terminal with dark beneath. The mouth line carries no pink — that row is the
// top half of the bottom cell and the waterline is its bottom half, so cheeks
// there would read as two dots floating on the water.

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
  '..P......P..',
  '.KKKKKKKKKK.',
  '.KGGGGGGGGK.',
  '.KGWKGGKWGK.',
  'KLLLLLLLLLLK',
  'KLLKLLLLKLLK',
  'KKKKKKKKKKKK',
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
  shut: 'KKKKKKKKKKKK',
  open: 'KKKWMMMMWKKK',
}

// The twitch DROPS the ears rather than raising them: the resting pose already
// fills the cell, and a full pink cell becoming a half one is a change you can
// see at size 1, where a one-pixel sideways tilt is not.
export const EARS_DOWN = ['............', '.KKKKKKKKKK.']

// blocked: the whole head is mouth, tusks top and bottom
export const GAPE = [
  '..P......P..',
  '.KKKKKKKKKK.',
  '.KGGGGGGGGK.',
  'KGLLLLLLLLGK',
  'KLKWMMMMWKLK',
  'KLKMPPPPMKLK',
  'KLKWMMMMWKLK',
]

/**
 * The squid, at 7x6 starting on an even row, so its three cells are head over
 * eyes, ears over body, and legs — which gives the legs a whole cell rather
 * than a half, and lets the walk cycle read as one lifted foot.
 */
export type SquidPose = 'walk' | 'step' | 'ouch' | 'caught'
export const SQUID: Readonly<Record<SquidPose, readonly string[]>> = {
  walk: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    '.C.C.C.',
    '.C.C.C.',
  ],
  step: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    '.C.C.C.',
    '.C...C.',
  ],
  ouch: [
    '.CCCCC.',
    '.CxCxC.',
    'CCCCCCC',
    '.CCCCC.',
    'C.C.C.C',
    '.C.C.C.',
  ],
  caught: [
    'CxCxC',
    '.CCC.',
  ],
}
export const SQUID_W = 7
export const SQUID_Y = 4

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
    rows.splice(0, 2, ...EARS_DOWN)
  }
  return rows
}
