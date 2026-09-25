// The goose as an Animal: goose_band.py bound to the seven poses the director
// asks for. One build, drawn for one cell a pixel — the size the band gives.

import type { Animal, Build, Glyph, Painted, Tone } from '../../animal'
import { PAL } from './art'
import { renderCanvas } from './scene'
import { H, W } from './scene-consts'
import { BUBBLE_AT, type Frame, POSES, cycle, enter, leave, look, rise, sleep, speak, wake } from './script'

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
  },

  render(st: Frame): Painted {
    // the z's and the squid's '!' carry a palette key rather than a colour
    const glyphs: Glyph[] = st.overlays.map(([x, y, ch, key]) => ({ x, y, ch, color: PAL[key]! }))
    return {
      canvas: renderCanvas(st),
      glyphs,
      bubble: st.said
        ? { text: st.said, shown: st.shown, tag: 'rule', tone: st.tone }
        : null,
    }
  },

  demo: (only?: string | null) => (only ? POSES[only]!() : cycle()),
  demoPoses: Object.keys(POSES),
}

export const goose: Animal = {
  name: 'Goose',
  title: 'Gus the Goose',
  builds: [build],
}
