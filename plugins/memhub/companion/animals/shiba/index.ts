// The shiba as an Animal: shiba_band.py bound to the seven poses the director
// asks for. One build, drawn for one cell a pixel — the size the band gives.

import type { Animal, Build, Glyph, Painted, Tone } from '../../animal'
import { GLYPH, PAL } from './art'
import { renderCanvas } from './scene'
import { H, W } from './scene-consts'
import { BUBBLE_AT, type Frame, POSES, cycle, enter, leave, look, pet, rise, sleep, speak, wake } from './script'

const build: Build<Frame> = {
  pixelSize: 1,
  size: { columns: W, rows: H },
  bubbleAt: { column: BUBBLE_AT.column, row: BUBBLE_AT.row },

  poses: {
    enter,
    sleep,
    wake,
    look,
    rise,
    speak: (text: string, _n: number, tone: Tone) => speak(text, tone),
    leave,
    pet,
  },

  render(st: Frame): Painted {
    // a glyph is named by its palette key, which is also its colour
    const glyphs: Glyph[] = st.glyphs.map(([x, y, key]) => ({ x, y, ch: GLYPH[key]!, color: PAL[key]! }))
    return {
      canvas: renderCanvas(st),
      glyphs,
      bubble: st.said
        ? { text: st.said, shown: st.shown, tag: st.tone === 'proposed' ? 'new rule' : 'rule', tone: st.tone }
        : null,
    }
  },

  demo: (only?: string | null) => (only ? POSES[only]!() : cycle()),
  demoPoses: Object.keys(POSES),
}

export const shiba: Animal = {
  name: 'Shiba',
  title: 'Popo the Shiba',
  builds: [build],
}
