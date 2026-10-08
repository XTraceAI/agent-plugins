// The renderer: hippo_band.py's Screen.compose and draw_bubble, made general.
// Any animal's Painted frame becomes terminal cells here — two pixels a cell,
// encoded as the design's own preview encodes them — packed into a Raster (the
// engine owns the terminal, so no escape codes and no frame diffing:
// `$.ui.blit` repaints in place). Raster cells carry no bold, so the bubble's
// bold runs draw at normal weight. The bubble is the one piece of chrome the
// animals share: same frame, same width, the animal's name in its header.

import type { Build, Painted, Tone } from './animal'
import type { RGB } from './pixels'

const BUB_BG: RGB = [35, 29, 44]
/** The bubble's inside, for an element drawn over it (the proposal's buttons). */
export const BUB_BG_HEX = '#231d2c'
const BUB_BORDER: RGB = [74, 63, 92]
const BUB_ACCENT: RGB = [183, 156, 255]
const BUB_TEXT: RGB = [236, 232, 242]
const BUB_DIM: RGB = [150, 140, 165]
const BUB_ANGRY: RGB = [255, 112, 112]
/** A new rule waiting on an answer: the goose's gold, not advice's violet. */
const BUB_PROPOSED: RGB = [250, 214, 84]
export const BUB_W = 42
/** The footer's right-hand line, where hippo_band.py names the dismiss key —
 *  which the band has no equivalent of, the bubble timing out on its own. */
const SIGNATURE = 'powered by XTrace'

type Cell = readonly [ch: string, fg: RGB | null, bg: RGB | null]
type Buf = Cell[][]
const BLANK: Cell = [' ', null, null]

function put(buf: Buf, row: number, col: number, text: string, fg: RGB | null = null, bg: RGB | null = null) {
  const line = buf[row]
  if (!line) return
  for (let i = 0; i < text.length; i++) {
    if (col + i >= 0 && col + i < line.length) line[col + i] = [text[i]!, fg, bg]
  }
}

/** Python's textwrap.wrap for plain prose: greedy on spaces, long words split. */
function wrap(msg: string, width: number): string[] {
  const lines: string[] = []
  let cur = ''
  for (let word of msg.split(/\s+/).filter(Boolean)) {
    while (word.length > width) {
      if (cur) {
        lines.push(cur)
        cur = ''
      }
      lines.push(word.slice(0, width))
      word = word.slice(width)
    }
    if (!cur) cur = word
    else if (cur.length + 1 + word.length <= width) cur += ' ' + word
    else {
      lines.push(cur)
      cur = word
    }
  }
  if (cur) lines.push(cur)
  return lines
}

type Seg = readonly [text: string, fg: RGB]

function bubbleLines(title: string, tag: string, msg: string, shown: number, accent: RGB, tone?: Tone): Seg[][] {
  const inner = BUB_W - 4
  const lines: Seg[][] = [
    [[title, accent], [tag.padStart(Math.max(1, inner - title.length)), BUB_DIM]],
    [],
  ]
  let left = shown
  for (const ln of wrap(msg, inner)) {
    lines.push([[ln.slice(0, Math.max(0, left)), BUB_TEXT]])
    left -= ln.length + 1
  }
  // a proposal's footer is left empty: its Activate / Reject / Later buttons
  // are drawn over that row (see footerOf), where the others say Got it
  lines.push([], tone === 'proposed' ? [] : [['Got it', BUB_DIM], [SIGNATURE.padStart(inner - 6), BUB_BORDER]])
  return lines
}

function drawBubble(
  buf: Buf, top: number, left: number, title: string, tag: string, msg: string, shown: number, tone: Tone,
) {
  const accent = accentOf(tone)
  const lines = bubbleLines(title, tag, msg, shown, accent, tone)
  put(buf, top, left, '╭' + '─'.repeat(BUB_W - 2) + '╮', BUB_BORDER)
  lines.forEach((segs, j) => {
    const i = j + 1
    put(buf, top + i, left, '│', accent)
    put(buf, top + i, left + 1, ' '.repeat(BUB_W - 2), null, BUB_BG)
    let col = left + 2
    for (const [text, fg] of segs) {
      put(buf, top + i, col, text, fg, BUB_BG)
      col += text.length
    }
    put(buf, top + i, left + BUB_W - 1, '│', BUB_BORDER)
  })
  put(buf, top + lines.length + 1, left, '╰' + '─'.repeat(BUB_W - 2) + '╯', BUB_BORDER)
}

const bubbleHeight = (msg: string) => bubbleLines('', '', msg, 0, BUB_ACCENT).length + 2

/** The most text lines fitBubble leaves, and so the tallest a bubble gets. */
const MAX_TEXT_LINES = 4
/**
 * The desktop's cap: its band is a card above the prompt, and room kept for a
 * fourth line showed as empty card over every shorter bubble.
 */
export const DESKTOP_TEXT_LINES = 3
/** A bubble's height in rows, for so many lines of text. */
const tallestFor = (maxLines: number) => maxLines + 6

// East Asian wide ranges, emoji-presentation symbols and variation selectors:
// a Raster cell holds one width-1 BMP character or the whole tree is refused.
const WIDE = /[\u1100-\u115f\u2600-\u27bf\u2e80-\ua4cf\uac00-\ud7a3\uf900-\ufaff\ufe00-\ufe0f\ufe30-\ufe4f\uff00-\uff60\uffe0-\uffe6]/gu

/**
 * Text a bubble can show: printable width-1 BMP characters only, and at most
 * MAX_TEXT_LINES wrapped lines, the last cut with an ellipsis.
 */
export function fitBubble(text: string, maxLines = MAX_TEXT_LINES): string {
  const clean = [...text.replace(WIDE, '')]
    .filter(ch => ch.codePointAt(0)! <= 0xffff && ch >= ' ' && ch !== '\u007f')
    .join('')
    .replace(/\s+/g, ' ')
    .trim()
  const lines = wrap(clean, BUB_W - 4)
  if (lines.length <= maxLines) return clean
  const kept = lines.slice(0, maxLines)
  kept[maxLines - 1] = kept[maxLines - 1]!.slice(0, BUB_W - 5) + '…'
  return kept.join(' ')
}

/**
 * Where the animal sits in the band: hippo_half.py's Screen.resize, for a band
 * of `columns` cells with `maxRows` to spare, and an animal of any size.
 *
 * A pixel is one cell wide and half a cell tall at scale 1, so pixels are
 * square whatever the scale, and scale 1 is the smallest the animal goes with
 * every row of its design drawn. The bubble goes left of the animal where it
 * fits there, else above it, on rows the layout reserves (the terminal
 * version had the whole screen above).
 */
export type Layout = {
  s: number
  columns: number
  rows: number
  col0: number
  row0: number
  isBeside: boolean
  /** The animal's first drawn canvas row; rows above it are trimmed away. */
  cropTop: number
  /** How many canvas rows are drawn, from `cropTop`. */
  cropRows: number
}

export const SCALES = [1, 2, 3, 4] as const

/**
 * One candidate layout. `isTrimmed` drops the animal's `trim` rows, which is
 * a last resort: the band it is drawn in is shorter than the animal, and the
 * choice is between losing those rows and scrolling the animal away.
 */
function fit(
  build: Build,
  bodyColumns: number,
  maxRows: number,
  s: number,
  isTrimmed: boolean,
  mustFit: boolean,
  maxLines: number,
  spare: number,
): Layout | null {
  const cropTop = isTrimmed ? (build.trim?.top ?? 0) : 0
  const cropBottom = isTrimmed ? (build.trim?.bottom ?? 0) : 0
  const cropRows = build.size.rows - cropTop - cropBottom
  if (cropRows < 1) return null
  const cw = build.size.columns * s
  const ch = Math.ceil((cropRows * s) / 2)
  const crop = { cropTop, cropRows }
  if (mustFit && ch > maxRows) return null
  // Rows kept above the canvas for the bubble to hang in: the terminal
  // version had the whole screen above, a trimmed canvas has nothing. Short
  // of room the animal comes first and the bubble gives up its top rows.
  const reserve = (wanted: number) => Math.max(0, Math.min(wanted, maxRows - ch))
  // the bubble's right edge meets the animal's bubble column, as compose() draws it
  const besideWidth = cw + 2 + BUB_W - build.bubbleAt.column * s
  if (bodyColumns >= besideWidth) {
    const row0 = reserve(Math.max(0, tallestFor(maxLines) - 1 + spare - Math.floor(((build.bubbleAt.row - cropTop) * s) / 2)))
    const columns = Math.min(bodyColumns, besideWidth)
    return { s, columns, rows: row0 + ch, col0: columns - cw - 2, row0, isBeside: true, ...crop }
  }
  if (bodyColumns < cw) return null
  // too narrow to sit beside: the bubble hangs over the animal's head, on
  // rows reserved above it (the terminal version had the screen above)
  const row0 = reserve(Math.max(0, tallestFor(maxLines) - aboveBottom(build, s, cropTop)))
  const columns = Math.min(bodyColumns, Math.max(cw + 2, BUB_W + 1))
  return {
    s, columns, rows: row0 + ch, col0: Math.max(0, columns - cw - 2), row0,
    isBeside: false, ...crop,
  }
}

/**
 * `maxLines` is the most text lines a bubble may say, which sizes the rows
 * kept above the animal for it; `spare` is how many rows more are kept over
 * the tallest bubble hung beside it — one on the terminal, as it always was,
 * none on the desktop's card, where it showed as an empty band.
 *
 * The size the animal's art was drawn for, or the largest smaller one the
 * band has room for; `/<animal> scale <n>` overrides it either way. The whole
 * canvas is kept: trimming happens only where even one cell a pixel is taller
 * than the band.
 */
export function layoutOf(
  build: Build,
  bodyColumns: number,
  maxRows: number,
  scale?: number,
  maxLines = MAX_TEXT_LINES,
  spare = 1,
): Layout | null {
  const wanted = scale ?? build.pixelSize
  for (let s = wanted; s >= 1; s--) {
    const laid =
      fit(build, bodyColumns, maxRows, s, false, true, maxLines, spare) ??
      fit(build, bodyColumns, maxRows, s, true, true, maxLines, spare)
    // an asked-for size is theirs to have, even where the band must scroll
    if (laid || scale !== undefined) {
      return laid ?? fit(build, bodyColumns, maxRows, s, false, false, maxLines, spare)
    }
  }
  return fit(build, bodyColumns, maxRows, 1, false, false, maxLines, spare)
}

export function compose(painted: Painted, build: Build, title: string, lay: Layout): Buf {
  const { s, columns, rows, col0, row0 } = lay
  const cv = painted.canvas
  const buf: Buf = Array.from({ length: rows }, () => new Array<Cell>(columns).fill(BLANK))
  const cw = build.size.columns * s
  const ch = Math.ceil((lay.cropRows * s) / 2)
  for (let r = 0; r < ch; r++) {
    for (let c = 0; c < cw; c++) {
      const t = cv[Math.floor((2 * r) / s) + lay.cropTop]?.[Math.floor(c / s)] ?? null
      const b = cv[Math.floor((2 * r + 1) / s) + lay.cropTop]?.[Math.floor(c / s)] ?? null
      if (t === null && b === null) continue
      let cell: Cell
      // Encoded so as little as possible depends on where the font puts a
      // block glyph in its cell. A terminal should draw ▀/▄ as exactly half,
      // but some rasterise from the font's metrics and land a pixel low; what
      // the glyph misses shows the cell's BACKGROUND. So a solid cell is a
      // space with a background and no glyph at all — nothing can get that
      // wrong — and a mixed cell uses the LOWER block over the upper pixel as
      // background, so a bleed repeats the pixel already above it rather than
      // printing a copy of the lower one there. Drawn the other way up, the
      // hippo grows a second pair of eyes above its brow.
      if (t === b) cell = [' ', null, t]
      else if (t === null) cell = ['▄', b, null]
      else if (b === null) cell = ['▀', t, null]
      else cell = ['▄', b, t]
      buf[row0 + r]![col0 + c] = cell
    }
  }
  for (const g of painted.glyphs ?? []) {
    const gy = g.y - lay.cropTop
    if (gy >= 0 && gy < lay.cropRows && g.x >= 0 && g.x < build.size.columns) {
      put(buf, row0 + Math.floor((gy * s) / 2), col0 + Math.floor(g.x * s), g.ch, g.color, null)
    }
  }
  const { bubble } = painted
  const at = bubbleAt(painted, build, lay)
  if (bubble && at) {
    drawBubble(buf, at.top, at.left, title, bubble.tag, bubble.text, bubble.shown, bubble.tone)
  }
  return buf
}

/** Where the bubble's box goes in the band: its top row and left column, in cells. */
function bubbleAt(painted: Painted, build: Build, lay: Layout): { top: number; left: number } | null {
  const { bubble } = painted
  if (!bubble) return null
  const { s, columns, col0, row0 } = lay
  const bh = bubbleHeight(bubble.text)
  let left: number
  let bottom: number
  if (lay.isBeside) {
    left = col0 + build.bubbleAt.column * s - BUB_W
    bottom = row0 + Math.floor(((build.bubbleAt.row - lay.cropTop) * s) / 2)
  } else {
    left = Math.max(0, columns - BUB_W - 1)
    bottom = row0 + aboveBottom(build, s, lay.cropTop) - 1
  }
  return { top: Math.max(0, bottom - bh + 1), left }
}

/**
 * The row inside a proposal's bubble where Got it would be, once its text is
 * all typed out: the cell the buttons are drawn at, and how wide they may be.
 * Null while there is no such bubble, so the buttons come and go with it.
 */
export function footerOf(painted: Painted, build: Build, lay: Layout): { row: number; col: number; width: number } | null {
  const { bubble } = painted
  const at = bubbleAt(painted, build, lay)
  if (!bubble || !at || bubble.tone !== 'proposed' || bubble.shown < bubble.text.length) return null
  // the box's top border, its lines, then the bottom border: the footer is the
  // last line, one above the bottom border
  return { row: at.top + bubbleHeight(bubble.text) - 2, col: at.left + 2, width: BUB_W - 4 }
}

/**
 * Where `word` starts in a proposal's bubble text, as a cell of the band,
 * once it has been typed out: the first whole-word match, on the line the
 * bubble wrapped it to. Null while there is no such bubble or word yet, so a
 * link drawn over it comes and goes with the typing.
 */
export function wordAt(painted: Painted, build: Build, lay: Layout, word: string): { row: number; col: number } | null {
  const { bubble } = painted
  const at = bubbleAt(painted, build, lay)
  if (!bubble || !at || bubble.tone !== 'proposed') return null
  const whole = new RegExp(`(^|[^A-Za-z0-9])(${word})(?![A-Za-z0-9])`)
  let seen = 0
  const lines = wrap(bubble.text, BUB_W - 4)
  for (let i = 0; i < lines.length; i++) {
    const m = whole.exec(lines[i]!)
    if (m) {
      const index = m.index + m[1]!.length
      if (bubble.shown < seen + index + word.length) return null
      // the top border, the title, a blank row, then the text lines
      return { row: at.top + 3 + i, col: at.left + 2 + index }
    }
    seen += lines[i]!.length + 1
  }
  return null
}

/**
 * Where the ♥ that pets the animal goes, as a cell of the band: on the
 * canvas's bottom row (the ground), one column left of where it begins —
 * beside the grass, not in it. At the band's left edge, on the ground's first
 * cell. While a bubble hangs beside the animal its bottom border runs along
 * that row, so the ♥ sits on the border then, still pressable (Got it).
 */
export function petAt(build: Build, lay: Layout): { top: number; left: number } {
  const ch = Math.ceil((lay.cropRows * lay.s) / 2)
  return { top: lay.row0 + ch - 1, left: Math.max(0, lay.col0 - 1) }
}

/** The row the bubble's bottom sits on when it hangs over the animal, in cells. */
const aboveBottom = (build: Build, s: number, cropTop: number) =>
  Math.floor((((build.bubbleAt.rowWhenAbove ?? build.bubbleAt.row) - cropTop) * s) / 2)

const accentOf = (tone: Tone): RGB =>
  tone === 'blocked' ? BUB_ANGRY : tone === 'proposed' ? BUB_PROPOSED : BUB_ACCENT

const DEFAULT_COLOR = 0x01000000
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
const rgb = (c: RGB | null) => (c === null ? DEFAULT_COLOR : (c[0] << 16) | (c[1] << 8) | c[2])

function base64(bytes: Uint8Array): string {
  let out = ''
  let i = 0
  for (; i + 2 < bytes.length; i += 3) {
    const n = (bytes[i]! << 16) | (bytes[i + 1]! << 8) | bytes[i + 2]!
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + B64[(n >> 6) & 63]! + B64[n & 63]!
  }
  const rest = bytes.length - i
  if (rest === 1) {
    const n = bytes[i]! << 16
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + '=='
  } else if (rest === 2) {
    const n = (bytes[i]! << 16) | (bytes[i + 1]! << 8)
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + B64[(n >> 6) & 63]! + '='
  }
  return out
}

/** A Raster's `cells`: row-major little-endian u32 [codePoint, fg, bg] triplets. */
export function encode(buf: Buf): string {
  const cells = buf.flat()
  const view = new DataView(new ArrayBuffer(cells.length * 12))
  cells.forEach(([ch, fg, bg], i) => {
    view.setUint32(i * 12, ch.codePointAt(0)!, true)
    view.setUint32(i * 12 + 4, rgb(fg), true)
    view.setUint32(i * 12 + 8, rgb(bg), true)
  })
  return base64(new Uint8Array(view.buffer))
}

/**
 * A cell's size in CSS pixels where the band is drawn as an SVG (the desktop,
 * whose Raster draws nothing): two square pixels a cell, as in the terminal.
 */
export const CELL_W = 8
export const CELL_H = 16
const FONT = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

const hex = (c: RGB) => `#${((c[0] << 16) | (c[1] << 8) | c[2]).toString(16).padStart(6, '0')}`
const escapeXml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const same = (a: RGB | null, b: RGB | null) => a === b || (!!a && !!b && a[0] === b[0] && a[1] === b[1] && a[2] === b[2])
const isBlock = (ch: string) => ch === ' ' || ch === '▄' || ch === '▀'

/** A cell's upper and lower pixel, as compose() encoded them; null is clear. */
function halvesOf([ch, fg, bg]: Cell): [RGB | null, RGB | null] {
  if (ch === '▄') return [bg, fg]
  if (ch === '▀') return [fg, bg]
  return [bg, bg]
}

/**
 * The same cells compose() lays out, as one SVG: each pixel row's runs of one
 * colour a rect, each run of text a `<text>` stretched to its cells, so the
 * bubble's words and borders land where the terminal draws them.
 */
export function svgOf(buf: Buf): string {
  const rows = buf.length
  const columns = buf[0]?.length ?? 0
  const half = CELL_H / 2
  const out: string[] = []
  buf.forEach((line, r) => {
    for (const h of [0, 1] as const) {
      let c = 0
      while (c < columns) {
        const color = halvesOf(line[c]!)[h]
        let end = c + 1
        if (color) {
          while (end < columns && same(halvesOf(line[end]!)[h], color)) end++
          out.push(`<rect x="${c * CELL_W}" y="${r * CELL_H + h * half}" width="${(end - c) * CELL_W}" height="${half}" fill="${hex(color)}"/>`)
        }
        c = end
      }
    }
    let c = 0
    while (c < columns) {
      const [ch, fg] = line[c]!
      if (isBlock(ch)) {
        c++
        continue
      }
      // a run is the cells up to the next block cell or colour change, the
      // spaces between words included, so a line of the bubble is one text
      let end = c + 1
      while (end < columns && same(line[end]![1], fg) && (!isBlock(line[end]![0]) || line[end]![0] === ' ')) end++
      while (end > c + 1 && line[end - 1]![0] === ' ') end--
      const text = line.slice(c, end).map(cell => cell[0]).join('')
      out.push(
        `<text x="${c * CELL_W}" y="${r * CELL_H + CELL_H * 0.75}" textLength="${(end - c) * CELL_W}" lengthAdjust="spacingAndGlyphs" fill="${hex(fg ?? BUB_TEXT)}">${escapeXml(text)}</text>`,
      )
      c = end
    }
  })
  const width = columns * CELL_W
  const height = rows * CELL_H
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" shape-rendering="crispEdges" font-family="${FONT}" font-size="13" xml:space="preserve">${out.join('')}</svg>`
}
