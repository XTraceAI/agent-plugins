// Layer 2 — scene. shiba_band.py's render_canvas: a 22x12 grid of RGB (or
// null for transparent) holding the grass, a squid, the dog, a bone and the
// loose pixels. No terminal code, no clock, no randomness.

import { type Canvas, canvasOf } from '../../pixels'
import { BONE, dogSprite, PAL, spriteTop, squidRows } from './art'
import { FLOOR, GROUND, H, W } from './scene-consts'
import type { Frame } from './script'

export { FLOOR, GROUND, H, HOME, W } from './scene-consts'
/** Dark grass blades poking up into row 10. */
const TUFTS = [1, 4, 9, 13, 18, 20]

function paint(cv: Canvas, rows: readonly string[], x: number, y: number) {
  rows.forEach((row, r) => {
    for (let c = 0; c < row.length; c++) {
      const ch = row[c]!
      if (ch !== '.' && y + r >= 0 && y + r < H && x + c >= 0 && x + c < W) {
        cv[y + r]![x + c] = PAL[ch]!
      }
    }
  })
}

/** One frame's pixels. `tick` is unused — nothing here moves on its own. */
export function renderCanvas(st: Frame): Canvas {
  const cv = canvasOf(W, H)
  for (let x = 0; x < W; x++) cv[GROUND]![x] = x % 7 === 3 ? PAL.g! : PAL.G!
  for (const x of TUFTS) cv[FLOOR]![x] = PAL.g!
  if (st.squid) {
    const [sx, sy, eyes, legs] = st.squid
    paint(cv, squidRows(eyes, legs), sx, sy)
  }
  const sp = dogSprite(st)
  if (sp) {
    const [rows, front] = sp
    paint(cv, rows, st.x - front, spriteTop(rows))
  }
  if (st.bone) {
    const [bx, by, size] = st.bone
    paint(cv, BONE[size], bx, by)
  }
  for (const [x, y, key] of st.specks) {
    if (x >= 0 && x < W && y >= 0 && y < H) cv[y]![x] = PAL[key]!
  }
  return cv
}
