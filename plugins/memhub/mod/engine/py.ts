// Small Python-semantics helpers the lane orchestration (engine.ts) needs
// beyond the ones rules.ts and shell.ts already export: `re.match` /
// `re.finditer` over a Python pattern (through pyre, so `\s`, `\w`, `$` mean
// what CPython means), `posixpath.dirname`, `hashlib.sha1`, and the ISO
// instants `_now()` / `_just_before()` write.

import { PyReError, pyTranslate } from './pyre'

const RX_CACHE = new Map<string, RegExp>()

/** A JS RegExp with CPython's semantics for `pattern`, plus JS-only flags (`g`/`y`). */
export function pyRegExp(pattern: string, pyFlags = '', jsExtra = ''): RegExp {
  const key = `${pyFlags}\0${jsExtra}\0${pattern}`
  let rx = RX_CACHE.get(key)
  if (rx) return rx
  const t = pyTranslate(pattern, pyFlags)
  if (!t) throw new PyReError(pattern)
  rx = new RegExp(t.source, t.flags.replace(/[gy]/g, '') + jsExtra)
  if (RX_CACHE.size > 512) RX_CACHE.clear()
  RX_CACHE.set(key, rx)
  return rx
}

/** `re.match(pattern, text, flags)`: anchored at 0; null or the match. */
export function pyMatch(pattern: string, text: string, flags = ''): RegExpExecArray | null {
  const rx = pyRegExp(pattern, flags, 'y')
  rx.lastIndex = 0
  return rx.exec(text)
}

/** `re.search(pattern, text, flags)`: null or the first match (groups intact). */
export function pyExec(pattern: string, text: string, flags = ''): RegExpExecArray | null {
  const rx = pyRegExp(pattern, flags, 'g')
  rx.lastIndex = 0
  return rx.exec(text)
}

/** `re.finditer(pattern, text, flags)` for patterns that never match empty. */
export function pyFinditer(pattern: string, text: string, flags = ''): RegExpExecArray[] {
  const rx = pyRegExp(pattern, flags, 'g')
  rx.lastIndex = 0
  const out: RegExpExecArray[] = []
  for (let m = rx.exec(text); m; m = rx.exec(text)) {
    out.push(m)
    if (m[0].length === 0) rx.lastIndex++
  }
  return out
}

/** `posixpath.dirname` */
export function dirname(p: string): string {
  const i = p.lastIndexOf('/') + 1
  let head = p.slice(0, i)
  if (head && head !== '/'.repeat(head.length)) head = head.replace(/\/+$/, '')
  return head
}

/** `str.rstrip(chars)` */
export const rstripChars = (s: string, chars: string): string => {
  let end = s.length
  while (end > 0 && chars.includes(s[end - 1]!)) end--
  return s.slice(0, end)
}

// ── hashlib.sha1(text.encode("utf-8")).hexdigest() ──────────────────────────

function utf8(s: string): number[] {
  const out: number[] = []
  for (const ch of s) {
    let c = ch.codePointAt(0)!
    // A lone surrogate cannot be encoded by Python either (it raises); the
    // replacement character is the closest thing a path will ever carry.
    if (c >= 0xd800 && c <= 0xdfff) c = 0xfffd
    if (c < 0x80) out.push(c)
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63))
    else if (c < 0x10000) out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
    else out.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
  }
  return out
}

export function sha1Hex(text: string): string {
  const bytes = utf8(text)
  const bitLen = bytes.length * 8
  bytes.push(0x80)
  while (bytes.length % 64 !== 56) bytes.push(0)
  const hi = Math.floor(bitLen / 0x100000000)
  for (const v of [hi, bitLen >>> 0]) bytes.push((v >>> 24) & 255, (v >>> 16) & 255, (v >>> 8) & 255, v & 255)
  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0
  const w = new Array<number>(80)
  const rotl = (x: number, n: number) => ((x << n) | (x >>> (32 - n))) >>> 0
  for (let off = 0; off < bytes.length; off += 64) {
    for (let i = 0; i < 16; i++) {
      const j = off + i * 4
      w[i] = ((bytes[j]! << 24) | (bytes[j + 1]! << 16) | (bytes[j + 2]! << 8) | bytes[j + 3]!) >>> 0
    }
    for (let i = 16; i < 80; i++) w[i] = rotl(w[i - 3]! ^ w[i - 8]! ^ w[i - 14]! ^ w[i - 16]!, 1)
    let a = h0, b = h1, c = h2, d = h3, e = h4
    for (let i = 0; i < 80; i++) {
      const [f, k] = i < 20 ? [(b & c) | (~b & d), 0x5a827999]
        : i < 40 ? [b ^ c ^ d, 0x6ed9eba1]
          : i < 60 ? [(b & c) | (b & d) | (c & d), 0x8f1bbcdc]
            : [b ^ c ^ d, 0xca62c1d6]
      const t = (rotl(a, 5) + (f >>> 0) + e + k + w[i]!) >>> 0
      e = d
      d = c
      c = rotl(b, 30)
      b = a
      a = t
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + b) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
  }
  return [h0, h1, h2, h3, h4].map((v) => v.toString(16).padStart(8, '0')).join('')
}

// ── instants ────────────────────────────────────────────────────────────────

const pad = (n: number, w = 2) => String(n).padStart(w, '0')

/**
 * `datetime.fromtimestamp(us / 1e6, tz).isoformat(timespec="microseconds")`
 * for an offset of `offsetMin` minutes east: what `_now()` writes.
 */
export function isoMicros(us: number, offsetMin: number): string {
  const localMs = Math.floor(us / 1000) + offsetMin * 60_000
  const micro = ((us % 1_000_000) + 1_000_000) % 1_000_000
  const d = new Date(localMs)
  const sign = offsetMin < 0 ? '-' : '+'
  const off = Math.abs(offsetMin)
  return `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` +
    `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}.${pad(micro, 6)}` +
    `${sign}${pad(Math.floor(off / 60))}:${pad(off % 60)}`
}
