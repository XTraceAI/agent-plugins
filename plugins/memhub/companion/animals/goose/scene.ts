// Layer 2 — scene. goose_band.py's render_canvas: a 22x12 grid of RGB (or
// null for transparent) holding the grass, the goose, a squid, a hawk's
// shadow and the flyer. No terminal code, no clock, no randomness.

import { type Canvas, canvasOf } from '../../pixels'
import { flyerRows, gooseRows, PAL, squidRows } from './art'
import { GROUND_Y, GY, H, W } from './scene-consts'
import type { Frame } from './script'

export { GROUND_Y, GX, GY, H, W } from './scene-consts'
/** Grass tufts on row 10, clear of the goose's spot. */
const TUFTS = [1, 3, 20]

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
  for (let x = 0; x < W; x++) cv[GROUND_Y]![x] = PAL.G!
  for (const x of TUFTS) cv[GROUND_Y - 1]![x] = PAL.G!
  if (st.shadow) {
    const [cx, w] = st.shadow
    for (let x = cx - Math.floor(w / 2); x < cx + Math.floor((w + 1) / 2); x++) {
      if (x >= 0 && x < W) cv[GROUND_Y]![x] = PAL.H!
    }
  }
  if (st.squid) {
    const [x, y, eyes, legs] = st.squid
    paint(cv, squidRows(eyes, legs), x, y)
  }
  if (st.body) {
    paint(cv, gooseRows(st.body, st.eyes, st.beak, st.blush, st.badge, st.step), st.x, GY)
  }
  if (st.flyer) {
    const [x, y, wings, talons, facing] = st.flyer
    paint(cv, flyerRows(wings, talons, facing), x, y)
  }
  return cv
}
