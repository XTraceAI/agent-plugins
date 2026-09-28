// Layer 2 — scene. hippo_band (1).py's render_canvas: a 22x12 grid of RGB (or
// null for transparent) holding the pond, the hippo, the squid and the
// particles, drawn so it reads at one cell a pixel. Nothing here knows about a terminal, a clock or randomness —
// everything moves off `tick`, so a given frame always looks the same.
// Python's `//` is Math.floor and `int()` of a float is Math.trunc throughout.

import { type Canvas, canvasOf, type Particle } from '../../../pixels'
import { type Eyes, type Mouth, PAL, POND, SPRITE_H, SPRITE_W, SQUID, sprite } from './art'
import type { Frame } from './script'

export const W = 22 // 22 x 6 cells at size 1, 44 x 12 at size 2
export const H = 12
export const SX = Math.floor((W - SPRITE_W) / 2) // 5: the sprite's left edge, and the bubble's column
const CX = W / 2
const POND_Y = 9 // the pond's top row
export const HY = 11 // the waterline: the sprite is clipped above it
const WATER: readonly [number, number] = [4, 17] // the water's left and right edge, inclusive

/**
 * How far out of the water each held pose sits: `top = HY - lift`, and the two
 * poses that are held for a long time land on an even row.
 */
export const GONE = 0
export const PEEK = 1
export const LURK = 1
export const LOW = 3
export const DOZE = 5
export const RISEN = 7

export type SpawnKind = 'splash' | 'bubbles' | 'steam'

function drawPond(cv: Canvas) {
  POND.forEach((row, r) => {
    for (let x = 0; x < row.length; x++) {
      const ch = row[x]!
      if (ch !== '.') cv[POND_Y + r]![x] = PAL[ch]!
    }
  })
  // No glints on HY - 1: at pixel size 1 that row shares a cell with the
  // waterline, and two independent sparkles inside one cell read as static.
  // The waterline carries the idle motion on its own, in drawHippo.
}

function drawSquid(cv: Canvas, st: Frame) {
  const [x0, y0, pose] = st.squid!
  SQUID[pose].forEach((row, r) => {
    for (let c = 0; c < row.length; c++) {
      const ch = row[c]!
      const x = x0 + c
      const y = y0 + r
      if (ch !== '.' && x >= 0 && x < W && y >= 0 && y < H) cv[y]![x] = PAL[ch]!
    }
  })
}

function drawHippo(cv: Canvas, st: Frame, tick: number) {
  const top = HY - st.lift
  const sx = SX + st.dx
  const rows = sprite(st.eyes, st.mouth, st.ears)
  rows.forEach((row, r) => {
    const y = top + r
    if (y >= 0 && y < HY) {
      for (let c = 0; c < row.length; c++) {
        const ch = row[c]!
        if (ch !== '.' && sx + c >= 0 && sx + c < W) cv[y]![sx + c] = PAL[ch]!
      }
    }
  })
  const r = HY - 1 - top // the row that meets the water
  const row = r >= 0 && r < SPRITE_H ? rows[r]! : ''
  const fill: number[] = []
  for (let c = 0; c < row.length; c++) if (row[c] !== '.') fill.push(c)
  if (fill.length > 0) {
    // a pose can leave that row empty
    const lo = sx + fill[0]!
    const hi = sx + fill[fill.length - 1]!
    for (let x = lo - 1; x < hi + 2; x++) {
      if (x >= WATER[0] && x <= WATER[1]) {
        // an unbroken line with one bright pixel drifting along it — punching
        // holes back to water colour turns it to speckle at size 1
        const edge = x === lo - 1 || x === hi + 1
        cv[HY]![x] = PAL[edge || (x + Math.floor(tick / 4)) % 6 === 0 ? 'b' : 'w']!
      }
    }
  }
}

const SPLASH: readonly (readonly [number, number])[] = [
  [-5, -0.55], [-2, -0.75], [2, -0.75], [5, -0.55],
]
const STEAM: readonly (readonly [number, number])[] = [
  [SX - 1, -0.32], [SX + SPRITE_W, 0.32],
]

/** Particles, seeded off `tick` so a given frame always looks the same. */
export function spawn(kind: SpawnKind, tick: number): Particle[] {
  if (kind === 'splash') {
    return SPLASH.map(([dx, vy]) => ({
      x: CX + dx, y: HY - 1.0, vx: dx * 0.07, vy, g: 0.12, life: 12, c: 'b',
    }))
  }
  if (kind === 'bubbles') {
    return [0, 1, 2].map(i => ({
      x: CX - 4 + 4 * i + ((Math.floor(tick / 5) + i) % 2), y: HY - 0.5,
      vx: 0, vy: -0.25, g: 0, life: 8, c: 'b',
    }))
  }
  return STEAM.map(([x, vx]) => ({
    x, y: HY - RISEN + 0.5 + (Math.floor(tick / 3) % 2),
    vx, vy: -0.34, g: 0, life: 6, c: 's',
  }))
}

export function stepParticles(ps: readonly Particle[]): Particle[] {
  const out: Particle[] = []
  for (const p of ps) {
    p.vy += p.g
    p.x += p.vx
    p.y += p.vy
    p.life -= 1
    if (p.life > 0 && p.y < HY) out.push(p)
  }
  return out
}

/** One frame's pixels. */
export function renderCanvas(st: Frame, tick: number, particles: readonly Particle[] = []): Canvas {
  const cv = canvasOf(W, H)
  drawPond(cv)
  const caught = st.squid !== null && st.squid[2] === 'caught'
  if (st.squid !== null && !caught) drawSquid(cv, st)
  if (st.lift > 0) drawHippo(cv, st, tick)
  if (caught) drawSquid(cv, st) // drawn last: it is inside the mouth
  for (const p of particles) {
    const x = Math.trunc(p.x)
    const y = Math.trunc(p.y)
    if (x >= 0 && x < W && y >= 0 && y < H) cv[y]![x] = PAL[p.c!]!
  }
  return cv
}

export type { Eyes, Mouth }
