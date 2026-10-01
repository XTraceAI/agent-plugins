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
      for (const tone of ['advice', 'blocked', 'proposed'] as const) {
        const drawn = framesOf(build, poses.speak(text, 1, tone))
        expect(drawn.length, 'speak never ended').toBeLessThan(LIMIT)
        const said = drawn.map(f => f.bubble).filter(b => b != null)
        expect(said.length, 'speak showed no bubble').toBeGreaterThan(0)
        expect(said.every(b => b!.text === text)).toBe(true)
        // every tone is said in its own bubble: a proposal never as advice
        expect(said.every(b => b!.tone === tone)).toBe(true)
        expect(Math.max(...said.map(b => b!.shown))).toBeGreaterThanOrEqual(text.length)
        expect(said[0]!.shown, 'the whole text showed at once').toBeLessThan(text.length)
        // held still long enough to read: 2s of frames with the text complete
        expect(said.filter(b => b!.shown >= text.length).length).toBeGreaterThanOrEqual(40)
      }
    })

    test(`${who}: presents a proposal with a ! first, then holds it 8s with a ?`, () => {
      const text = 'New rule proposed, not active yet: Pin the MCP server when spawning claude -p'
      const drawn = framesOf(build, poses.speak(text, 1, 'proposed'))
      expect(drawn.length, 'the proposal never ended').toBeLessThan(LIMIT)
      const has = (f: Painted, ch: string) => (f.glyphs ?? []).some(g => g.ch === ch)
      const firstSaid = drawn.findIndex(f => f.bubble != null)
      expect(drawn.slice(0, Math.max(0, firstSaid)).some(f => has(f, '!')), 'no ! before it spoke').toBe(true)
      expect(drawn.filter(f => has(f, '?')).length).toBeGreaterThanOrEqual(8 * 20)
      const whole = drawn.filter(f => f.bubble && f.bubble.shown >= text.length)
      expect(whole.length, 'held less than 8s once typed').toBeGreaterThanOrEqual(8 * 20)
      expect(whole.every(f => f.bubble!.tone === 'proposed' && f.bubble!.tag === 'new rule')).toBe(true)
    })

    test(`${who}: a pet starts and ends where look begins, and is short`, () => {
      expect(poses.pet, 'no pet pose').toBeDefined()
      const pet = [...poses.pet!()]
      expect(pet.length).toBeGreaterThanOrEqual(20)
      expect(pet.length).toBeLessThanOrEqual(40)
      const rest = JSON.stringify(poses.look().next().value)
      expect(JSON.stringify(pet[0]), 'pet does not start on look').toBe(rest)
      expect(JSON.stringify(pet[pet.length - 1]), 'pet does not end on look').toBe(rest)
      const drawn = pet.map((f, i) => build.render(f, i))
      expect(drawn.every(f => !f.bubble), 'a pet says nothing').toBe(true)
      expect(drawn.some(f => (f.glyphs ?? []).some(g => g.ch === '♥')), 'no heart').toBe(true)
    })

    test(`${who}: the demo offers propose and pet, and its loop proposes`, () => {
      expect(build.demo, 'no demo').toBeDefined()
      expect(build.demoPoses).toContain('propose')
      expect(build.demoPoses).toContain('pet')
      const proposes = (f: Painted) => f.bubble?.tone === 'proposed'
      expect(framesOf(build, build.demo!('propose')).some(proposes)).toBe(true)
      expect(framesOf(build, build.demo!(null), 3000).some(proposes), 'the demo loop never proposes').toBe(true)
    })

    test(`${who}: the new poses draw inside the canvas`, () => {
      const { columns, rows } = build.size
      const drawn = [
        ...framesOf(build, poses.speak('New rule proposed', 1, 'proposed')),
        ...framesOf(build, poses.pet!()),
      ]
      for (const f of drawn) {
        expect(f.canvas.length).toBe(rows)
        for (const g of f.glyphs ?? []) {
          expect(g.x >= 0 && g.x < columns && g.y >= 0 && g.y < rows, `${g.ch} at ${g.x},${g.y}`).toBe(true)
          expect(g.ch.codePointAt(0)!, 'a glyph outside the BMP').toBeLessThanOrEqual(0xffff)
        }
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

describe("the goose's face", () => {
  const goose = ANIMALS.find(a => a.name === 'Goose')!
  const build = goose.builds[0]!
  const PINK = '240,124,150'

  test('stays white while it blushes: no pink pixel by the eye, which read as a red eye', () => {
    const { poses } = build
    const frames = [
      ...framesOf(build, poses.speak('New rule proposed, not active yet: x', 1, 'proposed')),
      ...framesOf(build, poses.pet!()),
    ]
    const pink = frames.filter(f => f.canvas.some(row => row.some(px => px !== null && px.join(',') === PINK)))
    expect(pink.length, 'a pink pixel on the goose').toBe(0)
  })
})

describe('the goose presents a proposed rule, in sparkles', () => {
  const goose = ANIMALS.find(a => a.name === 'Goose')!
  const build = goose.builds[0]!
  const text = 'New rule proposed, not active yet. Activate it in MemHub Studio: pin the MCP server'
  const drawn = framesOf(build, build.poses.speak(text, 1, 'proposed'))
  const marks = (ch: string) => drawn.filter(f => (f.glyphs ?? []).some(g => g.ch === ch)).length

  test('in its own gold bubble, tagged as a new rule', () => {
    const said = drawn.map(f => f.bubble).filter(b => b != null)
    expect(said.every(b => b!.tone === 'proposed' && b!.tag === 'new rule')).toBe(true)
  })

  test('with a ! and sparkles before it speaks, then a ? while it waits', () => {
    const firstSaid = drawn.findIndex(f => f.bubble != null)
    const before = drawn.slice(0, Math.max(0, firstSaid))
    expect(before.some(f => (f.glyphs ?? []).some(g => g.ch === '!'))).toBe(true)
    expect(before.some(f => (f.glyphs ?? []).some(g => g.ch === '*'))).toBe(true)
    expect(marks('?')).toBeGreaterThanOrEqual(8 * 20)
  })

  test('holds the finished text long enough to reach for a button: 8s, not advice\'s 2s', () => {
    const whole = drawn.filter(f => f.bubble && f.bubble.shown >= text.length).length
    expect(whole).toBeGreaterThanOrEqual(8 * 20)
    const advice = framesOf(build, build.poses.speak(text, 1, 'advice'))
      .filter(f => f.bubble && f.bubble.shown >= text.length).length
    expect(whole).toBeGreaterThan(advice)
  })
})
