// Every animal in the registry, put through the ring the director walks. An
// animal that passes this is one the director can drive; what it looks like
// is its own business. See docs/companion/ANIMALS.md.

import { describe, expect, test } from 'claude-code/testing'

import type { Build, Painted } from '../animal'
import { ANIMALS } from '../animals'

/** No pose may run longer than this; the endless two are cut off here. */
const LIMIT = 2000

function framesOf(build: Build, gen: Generator<unknown>, limit = LIMIT): Painted[] {
  const out: Painted[] = []
  for (const frame of gen) {
    out.push(build.render(frame, out.length))
    if (out.length >= limit) break
  }
  return out
}

describe('animals', () => {
  for (const animal of ANIMALS) {
    for (const build of animal.builds) {
    const { poses } = build
    const who = `${animal.name} at ${build.pixelSize}x`

    test(`${who}: the finite poses end`, () => {
      for (const [name, gen] of [
        ['enter', poses.enter()], ['wake', poses.wake()],
        ['rise', poses.rise()], ['leave', poses.leave()],
      ] as const) {
        const drawn = framesOf(build, gen)
        expect(drawn.length, `${name} drew nothing`).toBeGreaterThan(0)
        expect(drawn.length, `${name} never ended`).toBeLessThan(LIMIT)
      }
    })

    test(`${who}: the endless poses keep going`, () => {
      for (const gen of [poses.sleep(), poses.look()]) {
        expect(framesOf(build, gen, 400).length).toBe(400)
      }
    })

    test(`${who}: speak types the whole text out, then ends`, () => {
      const text = 'Rule fired: never force-push to a shared branch'
      for (const tone of ['advice', 'blocked'] as const) {
        const drawn = framesOf(build, poses.speak(text, 1, tone))
        expect(drawn.length, 'speak never ended').toBeLessThan(LIMIT)
        const said = drawn.map(f => f.bubble).filter(b => b != null)
        expect(said.length, 'speak showed no bubble').toBeGreaterThan(0)
        expect(said.every(b => b!.text === text)).toBe(true)
        expect(said.every(b => b!.tone === tone)).toBe(true)
        expect(Math.max(...said.map(b => b!.shown))).toBeGreaterThanOrEqual(text.length)
        expect(said[0]!.shown, 'the whole text showed at once').toBeLessThan(text.length)
        // held still long enough to read: 2s of frames with the text complete
        expect(said.filter(b => b!.shown >= text.length).length).toBeGreaterThanOrEqual(40)
      }
    })

    test(`${who}: the waiting poses say nothing`, () => {
      for (const gen of [poses.sleep(), poses.look()]) {
        expect(framesOf(build, gen, 200).every(f => !f.bubble)).toBe(true)
      }
    })

    test(`${who}: every frame is drawn to its own size`, () => {
      const { columns, rows } = build.size
      expect(columns, 'wider than a Raster').toBeLessThanOrEqual(512)
      expect(rows / 2, 'taller than the band should be').toBeLessThanOrEqual(24)
      expect(build.bubbleAt.column).toBeLessThanOrEqual(columns)
      expect(build.bubbleAt.row).toBeLessThanOrEqual(rows)
      for (const drawn of [framesOf(build, poses.enter()), framesOf(build, poses.sleep(), 120)]) {
        for (const f of drawn) {
          expect(f.canvas.length).toBe(rows)
          expect(f.canvas.every(row => row.length === columns)).toBe(true)
          for (const g of f.glyphs ?? []) {
            expect([...g.ch].length, 'a glyph is one character').toBe(1)
            expect(g.ch.codePointAt(0)!, 'a glyph outside the BMP').toBeLessThanOrEqual(0xffff)
          }
        }
      }
    })

    }

    test(`${animal.name}: its command name is its own, and it has a build`, () => {
      expect(animal.name).toMatch(/^[A-Za-z][A-Za-z0-9-]{0,20}$/)
      expect(ANIMALS.filter(a => a.name.toLowerCase() === animal.name.toLowerCase()).length).toBe(1)
      expect(animal.builds.length).toBeGreaterThan(0)
      const sizes = animal.builds.map(b => b.pixelSize)
      expect(new Set(sizes).size, 'two builds for one pixel size').toBe(sizes.length)
    })
  }
})
