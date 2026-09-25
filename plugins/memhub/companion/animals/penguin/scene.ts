// Layer 2 — scene. penguin_band.py's render_canvas: a 22x14 grid of RGB (or
// null for transparent) holding the ice, the hole in whatever state it is in,
// the whale, the squid, the penguin and its loose pixels. Everything is a
// pure function of the frame and `tick`, particles included. Python's `//` is
// Math.floor — it rounds down on negatives too, which the splash relies on.

import { type Canvas, canvasOf } from '../../pixels'
import { PAL, penguin, squidRows, WHALE } from './art'
import { FOOT, H, HOLE, ICE_Y, PX, SPLASH_LEN, W } from './scene-consts'
import type { Frame } from './script'

export { FOOT, H, HOLE, ICE_Y, PX, SPLASH_LEN, W } from './scene-consts'

type Sprite = { name: string; rows: readonly string[]; x: number; y: number }

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

/** Every sprite this frame, back to front. */
function placements(st: Frame): Sprite[] {
  const out: Sprite[] = []
  if (st.whale) {
    const [top, mouth] = st.whale
    out.push({ name: 'whale', rows: WHALE[mouth], x: HOLE[0], y: top })
  }
  if (st.squid) {
    const [x, y, eyes, legs] = st.squid
    out.push({ name: 'squid', rows: squidRows(eyes, legs), x, y })
  }
  const rows = penguin(st.posture, st.eyes, st.mouth, st.lf, st.rf, st.feet)
  out.push({ name: 'penguin', rows, x: PX + st.px, y: FOOT - rows.length + 1 })
  return out
}

/**
 * The ice, and the hole in whatever state it is in: 0 whole, 1-2 cracking,
 * 3 open water, 4-6 freezing over.
 */
function ice(cv: Canvas, hole: number, tick: number) {
  const [a, b] = HOLE
  for (let x = 0; x < W; x++) cv[ICE_Y]![x] = PAL.S!
  if (hole === 1) {
    for (const x of [16, 18]) cv[ICE_Y]![x] = PAL.U!
  } else if (hole === 2) {
    for (const x of [15, 16, 17, 18, 19, 20]) cv[ICE_Y]![x] = PAL.U!
  } else if (hole >= 3) {
    const healed = ({ 3: 0, 4: 2, 5: 3, 6: 4 } as Record<number, number>)[hole]!
    for (let x = a + healed; x <= b - healed; x++) {
      cv[ICE_Y]![x] = (x + Math.floor(tick / 3)) % 3 === 0 ? PAL.D! : PAL.U!
    }
    if (hole === 6) {
      for (const x of [17, 18]) cv[ICE_Y]![x] = PAL.I!
    }
  }
}

/** Loose pixels, as [x, y, colour key]. Seeded off the frame alone. */
function particles(st: Frame): (readonly [number, number, string])[] {
  if (!st.fx) return []
  const [kind, age] = st.fx
  const out: (readonly [number, number, string])[] = []
  if (kind === 'shards' || kind === 'splash') {
    const key = kind === 'shards' ? 'S' : 'U'
    for (let i = 0; i < 6; i++) {
      const h = 2 + (i % 3)
      const x = HOLE[0] + ((i * 3) % 8) + Math.floor((age * ((i % 3) - 1)) / 3)
      const y = FOOT - (h * age - Math.floor((age * age) / 2))
      if (y >= 5 && y < FOOT && x >= 0 && x < W) out.push([x, y, key])
    }
  } else if (kind === 'spray') {
    const x0 = PX + st.px + 10
    for (let i = 0; i < 3 - Math.floor(age / 4); i++) {
      out.push([x0 + i + Math.floor(age / 3), FOOT - 1 + ((i + age) % 2), 'S'])
    }
  }
  return out.filter(([x]) => x >= 0 && x < W)
}

/**
 * One frame's pixels. Overlaid characters are in `st.glyphs` and are not
 * pixels — the band draws those over the top.
 */
export function renderCanvas(st: Frame, tick: number): Canvas {
  const cv = canvasOf(W, H)
  ice(cv, st.hole, tick)
  for (const { name, rows, x, y } of placements(st)) {
    if (name === 'penguin') {
      for (const [px, py, key] of particles(st)) cv[py]![px] = PAL[key]!
    }
    paint(cv, rows, x, y)
  }
  return cv
}
