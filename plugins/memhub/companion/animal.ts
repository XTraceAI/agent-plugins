// The contract between the director (register.ts, which watches the session)
// and one animal (its art, its poses, its choreography). The director knows
// nothing of an animal beyond this file: not its size, its palette, or what
// its frames hold. See docs/companion/ANIMALS.md for how to write one.

import type { Canvas, RGB } from './pixels'

/** Why the animal is speaking; the bubble's colour follows it. */
export type Tone = 'advice' | 'blocked'

/** One character drawn over the pixels, at canvas pixel (x, y). */
export type Glyph = { x: number; y: number; ch: string; color: RGB }

/** What the animal is saying this frame, as the bubble should show it. */
export type Bubble = {
  text: string
  /** How much of `text` has been typed out so far; `text.length` when done. */
  shown: number
  /** The bubble's corner label: `rule`, `tip 3`, `grr`. */
  tag: string
  tone: Tone
}

/** One drawn frame of an animal. */
export type Painted = {
  canvas: Canvas
  /** Text over the pixels, such as a sleeper's z's. */
  glyphs?: readonly Glyph[]
  /** The speech bubble, or nothing while the animal is silent. */
  bubble?: Bubble | null
}

/**
 * The seven poses the director asks for, each a generator of the animal's own
 * frame type. Four are finite and play whole; `sleep` and `look` are endless
 * and the director cuts them off; `speak` ends on its own.
 *
 * The director only ever walks this ring:
 *
 *   enter → sleep ⇄ (wake → look) → rise → speak+ → leave → enter
 *
 * so every finite pose must end where the next one begins.
 */
export type Poses<F> = {
  /** Offscreen → asleep. Runs at startup and after every `leave`. */
  enter(): Generator<F>
  /** Asleep, endless: nothing is happening in the session. */
  sleep(): Generator<F>
  /** Asleep → awake. */
  wake(): Generator<F>
  /** Awake, endless: Claude is answering, or the person is typing. */
  look(): Generator<F>
  /** Awake → the speaking position. */
  rise(): Generator<F>
  /**
   * Says one thing and holds it long enough to read: type `text` out, then
   * keep still for a few seconds. Several may run back to back, so this must
   * start and end in the same position `rise` left the animal in.
   *
   * @param text what to say, already short enough for the bubble
   * @param n how many times the animal has spoken this session, from 1
   */
  speak(text: string, n: number, tone: Tone): Generator<F>
  /** The speaking position → offscreen, ready for `enter` again. */
  leave(): Generator<F>
}

/**
 * One build of an animal: art drawn for one pixel size, with the poses and
 * the canvas that go with it.
 *
 * A pixel is `pixelSize` cells wide and half that tall, so the same drawing at
 * size 1 packs two canvas rows into one cell and at size 2 gives each row a
 * cell of its own. That is a different drawing problem, not the same one
 * scaled: what reads at size 2 can turn to mush at size 1. An animal may ship
 * a build per size, each with art of its own.
 */
export type Build<F = unknown> = {
  /** Cells per pixel this art is drawn for. */
  pixelSize: number
  /** The canvas every frame paints on, in pixels (two pixels a terminal row). */
  size: { columns: number; rows: number }
  /**
   * Where the bubble hangs, in canvas pixels: its right edge sits at
   * `column`, and its last row at `row` — so the tail points at the animal's
   * head while it speaks. `rowWhenAbove` is that last row for a terminal too
   * narrow to fit the bubble beside the animal, where it sits over its head
   * instead (the animal's own top row suits); `row` is used when absent.
   */
  bubbleAt: { column: number; row: number; rowWhenAbove?: number }
  /**
   * Canvas rows at the top and bottom the band may drop when it is short of
   * room — empty sky, the base of a mound — so the pixels can be drawn bigger
   * instead. Nothing the animal needs to read should live in them.
   */
  trim?: { top?: number; bottom?: number }
  poses: Poses<F>
  /**
   * Draws one frame. Called exactly once per frame, in order, so a build may
   * keep state between calls (particles, a wind, a ripple).
   *
   * @param frame what a pose just yielded
   * @param tick the frame number since the session started, for anything
   *        that moves on its own (a shimmer, a drift)
   */
  render(frame: F, tick: number): Painted
  /**
   * What `/<name> demo` and `/<name> <pose>` replay, when the build has a loop
   * of its own to show off; without it those arguments are refused.
   */
  demo?(only?: string | null): Generator<F>
  /** The poses `demo` accepts by name, for the command's usage line. */
  demoPoses?: readonly string[]
}

/**
 * One animal: its name, and its art. The band draws the smallest build unless
 * someone asks for a bigger pixel, which is what `/<name> scale <n>` is for.
 */
export type Animal = {
  /** Its name, which is also its command: `Hippo` gives `/hippo`. */
  name: string
  /**
   * What the speech bubble's header shows, when that is not just the name:
   * the hippo answers to `/hippo` and introduces itself as Hugo.
   */
  title?: string
  /** One build per pixel size, smallest first; at least one. */
  builds: readonly Build[]
}
