// The hippo as an Animal, in two builds: the small one drawn for a single
// cell a pixel (the default, from hippo_band (1).py) and the big one drawn for
// two (`/hippo scale 2`, from hippo_band.py). Same canvas, same poses, but the
// art differs — at one cell a pixel two canvas rows share a cell, which is a
// different drawing problem, not the same one scaled.

import type { Animal, Build, Glyph, Painted, Tone } from '../../animal'
import type { Particle, RGB } from '../../pixels'

import * as bigArt from './big/scene'
import * as bigScript from './big/script'
import * as smallArt from './small/scene'
import * as smallScript from './small/script'

const Z_COLOR: RGB = [190, 180, 230]

type Parts = {
  scene: typeof smallArt
  script: typeof smallScript
}

/** One build over its own scene and choreography; the two never share a frame. */
function buildOf(pixelSize: number, { scene, script }: Parts): Build<smallScript.Frame> {
  let particles: Particle[] = []
  return {
    pixelSize,
    size: { columns: scene.W, rows: scene.H },
    bubbleAt: { column: script.BUBBLE_AT.column, row: script.BUBBLE_AT.row },
    poses: {
      enter: script.enter,
      sleep: script.sleep,
      wake: script.wake,
      look: script.look,
      rise: script.rise,
      speak: (text: string, _n: number, tone: Tone) => script.speak(text, tone),
      leave: script.leave,
    },
    render(st, tick): Painted {
      if (st.spawn) {
        particles = particles.concat(scene.spawn(st.spawn, tick))
      }
      particles = scene.stepParticles(particles)
      const glyphs: Glyph[] = st.zs.map(([x, y, ch]) => ({ x, y, ch, color: Z_COLOR }))
      return {
        canvas: scene.renderCanvas(st, tick, particles),
        glyphs,
        bubble: st.said
          ? { text: st.said, shown: st.shown, tag: 'rule', tone: st.tone }
          : null,
      }
    },
    demo: (only?: string | null) => (only ? script.POSES[only]!() : script.cycle()),
    demoPoses: Object.keys(script.POSES),
  }
}

export const hippo: Animal = {
  name: 'Hippo',
  title: 'Hugo the Hippo',
  builds: [
    buildOf(1, { scene: smallArt, script: smallScript }),
    buildOf(2, { scene: bigArt, script: bigScript }),
  ],
}
