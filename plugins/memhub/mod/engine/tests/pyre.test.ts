// pyre.ts against CPython: every vector in ./vectors/regex.ts (generated for
// the run by scripts/test-mod.sh from scripts/rule_vector_cases/regex.py)
// replayed through the port.
import { test, expect } from 'claude-code/testing'
import { VECTORS, type Vector } from './vectors/regex'
import { belowRatchet } from './vector_checks'
import { pyCheck, pyCompile, pySearch, pyFindall1, isPortable, PyReError } from '../pyre'

const by = (fn: string) => VECTORS.filter((v) => v.fn === fn)
const one = (fn: string, ...args: unknown[]) =>
  VECTORS.find((v) => v.fn === fn && JSON.stringify(v.args) === JSON.stringify(args))!
const show = (v: Vector) => JSON.stringify(v.args).slice(0, 160)

const CORPUS = one('corpus').out as string[]
type Ranges = [number, number][]

function bitmap(r: Ranges): Uint8Array {
  const b = new Uint8Array(0x110000)
  for (const [lo, hi] of r) b.fill(1, lo, hi + 1)
  return b
}

test('vectors loaded', () => {
  // Recorded counts (vector_checks.ts): raise them when cases are added.
  expect([belowRatchet('regex', VECTORS.length, 969), belowRatchet('regex corpus', CORPUS.length, 74)])
    .toEqual([null, null])
})

test('check: pyCheck gives CPython’s verdict (ok / invalid / unportable)', () => {
  const bad: string[] = []
  for (const v of by('check')) {
    const [p, f] = v.args as [string, string]
    if (pyCheck(p, f) !== v.out) bad.push(`${show(v)} → ${pyCheck(p, f)} want ${v.out}`)
  }
  for (const v of by('check_many')) {
    const [ps] = v.args as [string[]]
    ps.forEach((p, k) => {
      const want = (v.out as string[])[k]
      if (pyCheck(p) !== want) bad.push(`${JSON.stringify(p).slice(0, 120)} → ${pyCheck(p)} want ${want}`)
    })
  }
  expect(bad).toEqual([])
})

test('search: pySearch == bool(re.search), and throws where Python would raise / port refuses', () => {
  const bad: string[] = []
  for (const v of by('search')) {
    const [p, t, f] = v.args as [string, string, string]
    if (v.raises) {
      let threw = false
      try {
        pySearch(p, t, f)
      } catch (e) {
        threw = e instanceof PyReError
      }
      if (!threw) bad.push(`${show(v)}: expected PyReError (${v.raises})`)
      if (pyCompile(p, f) !== null || isPortable(p, f)) bad.push(`${show(v)}: compiled though unportable`)
      continue
    }
    let got: unknown
    try {
      got = pySearch(p, t, f)
    } catch (e) {
      got = String(e)
    }
    if (got !== v.out) bad.push(`${show(v)} → ${got} want ${v.out}`)
  }
  expect(bad).toEqual([])
})

test('findall: pyFindall1 == re.findall (one group or none)', () => {
  const bad: string[] = []
  for (const v of by('findall')) {
    const [p, t, f] = v.args as [string, string, string]
    let got: unknown
    try {
      got = pyFindall1(p, t, f)
    } catch (e) {
      got = e instanceof PyReError ? 'raises' : String(e)
    }
    const want = v.raises ? 'raises' : v.out
    if (JSON.stringify(got) !== JSON.stringify(want)) bad.push(`${show(v)} → ${JSON.stringify(got)} want ${JSON.stringify(want)}`)
  }
  expect(bad).toEqual([])
})

// The three sweeps below run thousands of patterns or every code point: about
// 1-5 s alone, past the default 5 s timeout on a loaded CI runner, so each
// takes a minute.
test('real rule-book patterns: every pattern, corpus + generated texts, flags "" i m im', { timeoutMs: 60_000 }, () => {
  const bad: string[] = []
  let hits = 0, total = 0
  for (const v of by('book_row')) {
    const [p] = v.args as [string]
    const o = v.out as Record<string, string | string[]>
    if (o.check !== 'ok') {
      if (isPortable(p)) bad.push(`${p.slice(0, 80)}: portable here, ${o.check} in Python`)
      continue
    }
    const texts = [...CORPUS, ...(o.extra as string[])]
    for (const f of ['', 'i', 'm', 'im']) {
      const want = o[f || '-'] as string
      let got = ''
      try {
        got = texts.map((t) => (pySearch(p, t, f) ? '1' : '0')).join('')
      } catch (e) {
        bad.push(`${p.slice(0, 80)} [${f}]: ${String(e)}`)
        continue
      }
      for (let k = 0; k < texts.length; k++) {
        total++
        if (want[k] === '1') hits++
        if (got[k] !== want[k]) bad.push(`${JSON.stringify(p).slice(0, 100)} [${f}] on ${JSON.stringify(texts[k]).slice(0, 60)}: ${got[k]} want ${want[k]}`)
      }
    }
  }
  expect(bad.slice(0, 30)).toEqual([])
  expect(hits).toBeGreaterThan(total / 20) // the generated texts really hit
})

test('re.I: every CPython case class matches pairwise, also inside [..] and negated', { timeoutMs: 60_000 }, () => {
  const bad: string[] = []
  const esc = (c: string) => '\\U' + c.codePointAt(0)!.toString(16).padStart(8, '0')
  for (const cls of one('fold_classes').out as string[]) {
    const cs = Array.from(cls)
    for (const a of cs) {
      for (const b of cs) {
        if (!pySearch(`^${esc(a)}$`, b, 'i')) bad.push(`${a}~${b}`)
        if (!pySearch(`^[${esc(a)}]$`, b, 'i')) bad.push(`[${a}]~${b}`)
        if (pySearch(`^[^${esc(a)}]$`, b, 'i')) bad.push(`[^${a}]!~${b}`)
      }
      // and nothing outside the class: probe its neighbours
      for (const d of [-1, 1]) {
        const n = String.fromCodePoint(Math.max(0, a.codePointAt(0)! + d))
        if (!cs.includes(n) && !(n >= '\ud800' && n <= '\udfff') && pySearch(`^${esc(a)}$`, n, 'i')) bad.push(`${a}!~${n}`)
      }
    }
  }
  expect(bad.slice(0, 30)).toEqual([])
})

test('\\w \\d \\s: identical to CPython on every code point CPython has assigned', { timeoutMs: 60_000 }, () => {
  const unassigned = bitmap(one('unassigned_ranges').out as Ranges)
  const bad: string[] = []
  for (const cls of ['w', 'd', 's']) {
    const py = bitmap(one('class_ranges', cls).out as Ranges)
    const rx = pyCompile(`\\${cls}`)!
    const neg = pyCompile(`[^\\${cls}]`)!
    for (let cp = 0; cp < 0x110000; cp++) {
      if (cp >= 0xd800 && cp <= 0xdfff) continue
      const ch = String.fromCodePoint(cp)
      const js = rx.test(ch)
      if (js !== !!py[cp] && !unassigned[cp]) bad.push(`\\${cls} U+${cp.toString(16)} js=${js}`)
      if (neg.test(ch) === js) bad.push(`[^\\${cls}] U+${cp.toString(16)} not the complement`)
      if (bad.length > 30) break
    }
  }
  expect(bad).toEqual([])
})

test('pyCompile caches, and its RegExp carries no lastIndex state', () => {
  const a = pyCompile('git\\s+push', 'im')
  expect(a).not.toBeNull()
  expect(pyCompile('git\\s+push', 'im')).toBe(a)
  expect(a!.global || a!.sticky).toBe(false)
  expect(pySearch('git\\s+push', 'GIT push', 'im')).toBe(true)
  expect(pySearch('git\\s+push', 'GIT push', 'im')).toBe(true)
})
