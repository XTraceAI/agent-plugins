// The pixel bits every animal draws with: a canvas of RGB, sprite rows keyed
// into a palette, and the particle stepper. Nothing here knows about any one
// animal, or about the terminal.

export type RGB = readonly [number, number, number]

/** An animal's picture for one frame: `null` is transparent. */
export type Canvas = (RGB | null)[][]

/** A palette: one character per colour, as a sprite's rows are written. */
export type Palette = Readonly<Record<string, RGB>>

export const canvasOf = (columns: number, rows: number): Canvas =>
  Array.from({ length: rows }, () => new Array<RGB | null>(columns).fill(null))

/**
 * Draws sprite rows at (x, y), `.` transparent, each other character a key
 * into `pal`; rows at `clipAt` and below are dropped (a waterline).
 */
export function paint(
  cv: Canvas,
  rows: readonly string[],
  x: number,
  y: number,
  pal: Palette,
  clipAt = cv.length,
) {
  rows.forEach((row, r) => {
    const cy = y + r
    if (cy < 0 || cy >= Math.min(clipAt, cv.length)) return
    for (let c = 0; c < row.length; c++) {
      const ch = row[c]!
      if (ch !== '.' && x + c >= 0 && x + c < cv[cy]!.length) cv[cy]![x + c] = pal[ch]!
    }
  })
}

export type Particle = {
  x: number
  y: number
  vx: number
  vy: number
  /** Gravity added to `vy` each frame. */
  g: number
  /** Frames left to live. */
  life: number
  /** Sideways drift amplitude, in pixels; still when absent. */
  wobble?: number
  /** Palette key; the animal's own palette reads it. */
  c?: string
  /** Square side in pixels, 1 when absent. */
  size?: number
}

export const uniform = (a: number, b: number) => a + (b - a) * Math.random()
export const randint = (a: number, b: number) => a + Math.floor(Math.random() * (b - a + 1))

/** Moves every particle one frame and drops the dead ones (and any past `floor`). */
export function stepParticles(ps: readonly Particle[], frame: number, floor: number): Particle[] {
  const alive: Particle[] = []
  for (const p of ps) {
    p.vy += p.g
    p.x += p.vx + (p.wobble ? Math.sin(frame + p.y) * p.wobble : 0)
    p.y += p.vy
    p.life -= 1
    if (p.life > 0 && p.y < floor) alive.push(p)
  }
  return alive
}

/** Draws the particles onto the canvas, each a `size` square of its colour. */
export function paintParticles(cv: Canvas, ps: readonly Particle[], pal: Palette, fallback: string) {
  for (const p of ps) {
    const x = Math.trunc(p.x)
    const y = Math.trunc(p.y)
    if (x < 0 || x >= cv[0]!.length || y < 0 || y >= cv.length) continue
    const size = p.size ?? 1
    for (let dx = 0; dx < size; dx++) {
      for (let dy = 0; dy < size; dy++) {
        if (x + dx < cv[0]!.length && y - dy >= 0) cv[y - dy]![x + dx] = pal[p.c ?? fallback]!
      }
    }
  }
}
