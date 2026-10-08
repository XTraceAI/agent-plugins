// Python `re` semantics in JS. Every rule regex is authored for Python (the
// server lints it with Python's `re`, the Python hook runs it), so the port
// never hands a rule pattern to `new RegExp` directly: it goes through here.
//
// Flags are Python's, spelled as letters: 'i' (re.I), 'm' (re.M), 's' (re.S),
// 'x' (re.X).
//
// HOW: a parser that mirrors CPython 3.14's `re._parser` token for token
// (same escapes, same "is this `{` a quantifier" rule, same error cases), and
// an emitter that writes a JS pattern spelling out every construct whose
// meaning differs between the two engines. Nothing is passed through on the
// hope that JS reads it the same way:
//
//   `.`        → `[^\n]` (or any code point under s): JS `.` also stops at \r
//                and U+2028/9.
//   `^`  `$`   → lookarounds on `\n` only: Python's `$` also matches before a
//                final `\n`; JS's `m` treats \r and U+2028/9 as line ends too.
//   `\A` `\Z`  → start / end of input.
//   `\w` `\d`  → `[\p{L}\p{N}_]`, `\p{Nd}`: Python 3 str patterns are Unicode
//                aware, JS `\w`/`\d` are ASCII even under `u`.
//   `\b` `\B`  → lookarounds on that `\w` (JS `\b` is ASCII).
//   `\s`       → Python's exact whitespace set (it has \x1c-\x1f and \x85;
//                JS has U+FEFF and not those).
//   re.I       → JS `i` under `u`, which folds by simple case folding. That
//                agrees with Python's simple-lowercase + re._casefix classes
//                on every code point except İ (U+0130) and ı (U+0131), which
//                Python also puts with i/I; emitted as an explicit `[iİı]`.
//                (tests/pyre.test.ts checks every case class Python has.)
//   `{,n}`, a literal `{`, `]` or `}`, and Python-only identity escapes
//   (`\"`, `\#`, `\ `, `\&`, `\~` — `re.escape` emits these) → their JS-legal
//   spelling. `(?P<n>…)` → `(?<n>…)`. `(?x)` whitespace/comments are dropped
//   at parse time. A quantified lookaround is wrapped (`u` mode forbids it).
//   Scoped `(?i:…)`/`(?-i:…)` → JS regexp modifiers, when this engine has
//   them (feature-detected); `m`/`s`/`x` scopes need no JS help.
//
// THE FLAG: `u`, deliberately. It is what makes `\p{…}` and Unicode case
// folding available (without it JS `i` canonicalises by toUpperCase, and the
// Unicode classes would need large tables), it walks code points like
// Python's str, and it turns a malformed escape into a compile error instead
// of an Annex-B guess. `v` adds only class set operations, which Python does
// not have (`[a--b]` is a literal there, with a FutureWarning), and its
// stricter class syntax buys nothing once the emitter escapes every class
// member itself — so `u`, the older and more widely available mode. JS `m`
// and `s` are never set: the emitter spells `^ $ .` out, which is also what
// makes scoped m/s work everywhere.
//
// UNPORTABLE (pyCompile → null, pySearch throws PyReError): anything whose
// meaning differs across the Python versions the hook runs on, or that JS
// cannot say identically — backreferences (`\1`, `(?P=n)`: JS matches an
// unset group as empty, Python fails), conditionals `(?(1)…)`, atomic groups
// and possessive quantifiers (Python 3.11+ only), `\z` (3.14+ only), `\N{…}`,
// ASCII mode `(?a)`, non-ASCII group names, surrogate escapes, and a scoped
// `i` on an engine without modifiers. Invalid Python (what `re.compile`
// rejects — e.g. a variable-width lookbehind, which JS would happily run) is
// null too; pyCheck() tells the two apart.
//
// KNOWN RESIDUE (not decidable from the pattern): which code points are
// letters, digits or case pairs comes from this engine's Unicode tables vs the
// hook's Python's `unicodedata`. They differ only on code points one side has
// not assigned yet — the same drift CPython has between its own versions.
// CPython 3.14 is the reference (e.g. `\B` matches the empty string).

const I = 2, M = 8, S = 16, U = 32, X = 64, A = 256
const FLAGS: Record<string, number> = { i: I, L: 4, m: M, s: S, x: X, a: A, u: U }
const TYPE_FLAGS = A | 4 | U
const MAXREPEAT = 4294967295
const MAXWIDTH = Number.MAX_SAFE_INTEGER

/** CPython would raise re.error. */
class PyErr extends Error {}
/** Valid Python with no identical JS. */
class Unport extends Error {}

type Item = { k: 'cp'; cp: number } | { k: 'range'; lo: number; hi: number } | { k: 'cat'; c: string }
type SetNode = { t: 'set'; neg: boolean; items: Item[] }
type Node =
  | { t: 'lit'; cp: number }
  | { t: 'any' }
  | SetNode
  | { t: 'at'; a: 'bol' | 'eol' | 'A' | 'Z' | 'b' | 'B' }
  | { t: 'group'; cap: boolean; name?: string; add: number; del: number; body: Node[] }
  | { t: 'alt'; branches: Node[][] }
  | { t: 'look'; behind: boolean; neg: boolean; body: Node[] }
  | { t: 'rep'; min: number; max: number; lazy: boolean; body: Node }
  /** Parsed only so CPython's checks run; never emitted (refused). */
  | { t: 'ref'; w: [number, number] }

const DIGITS = '0123456789'
const OCT = '01234567'
const HEX = '0123456789abcdefABCDEF'
const WS = ' \t\n\r\v\f'
const SPECIAL = '.\\[{()*+?^$|'
const isAsciiLetter = (c: string) => /^[A-Za-z]$/.test(c)
const isOne = (t: string | null, set: string) => t !== null && t.length === 1 && set.includes(t)

/** CPython's re._parser.Tokenizer: a token is one code point, or `\` + one. */
class Src {
  private cps: string[]
  private i = 0
  next: string | null = null
  constructor(p: string) {
    this.cps = Array.from(p)
    this.advance()
  }
  private advance() {
    let i = this.i
    if (i >= this.cps.length) {
      this.next = null
      return
    }
    let ch = this.cps[i]
    if (ch === '\\') {
      i++
      if (i >= this.cps.length) throw new PyErr('bad escape (end of pattern)')
      ch += this.cps[i]
    }
    this.i = i + 1
    this.next = ch
  }
  match(c: string): boolean {
    if (this.next === c) {
      this.advance()
      return true
    }
    return false
  }
  get(): string | null {
    const t = this.next
    this.advance()
    return t
  }
  getwhile(n: number, set: string): string {
    let r = ''
    for (let k = 0; k < n && isOne(this.next, set); k++) r += this.get()
    return r
  }
  getuntil(term: string): string {
    let r = ''
    for (;;) {
      const c = this.get()
      if (c === null) throw new PyErr(r ? `missing ${term}, unterminated name` : 'missing group name')
      if (c === term) {
        if (!r) throw new PyErr('missing group name')
        return r
      }
      r += c
    }
  }
  /** Index of `next` in code points. */
  tell(): number {
    return this.i - (this.next === null ? 0 : this.next[0] === '\\' ? 2 : 1)
  }
  seek(idx: number) {
    this.i = idx
    this.advance()
  }
}

class St {
  flags = 0
  groups = 1 // CPython's state.groups: the next group id
  names = new Map<string, number>()
  open = new Set<number>()
  widths = new Map<number, [number, number]>()
  /** CPython's state.lookbehindgroups. */
  lookbehindGroups: number | null = null
  /** Conditional-group numbers, checked once the whole pattern is read. */
  condRefs: number[] = []
  /** First reason the pattern has no identical JS. Parsing goes on, so a
   *  later CPython error still makes the verdict "invalid". */
  unport: string | null = null
  refuse(why: string) {
    this.unport ??= why
  }
  checkLookbehindGroup(gid: number) {
    if (this.lookbehindGroups !== null && gid >= this.lookbehindGroups)
      throw new PyErr('cannot refer to group defined in the same lookbehind subpattern')
  }
}

const XID = (() => {
  try {
    return new RegExp('^[\\p{XID_Start}_][\\p{XID_Continue}]*$', 'u')
  } catch {
    return null
  }
})()

function checkName(name: string, st: St) {
  // CPython accepts any str.isidentifier(); JS group names are a different
  // grammar. ASCII identifiers mean the same in both; a non-ASCII one is left
  // unportable rather than guessed.
  if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return
  if (/^[\x00-\x7f]*$/.test(name) || (XID && !XID.test(name))) throw new PyErr(`bad character in group name ${name}`)
  st.refuse('non-ASCII group name')
}

const ESCAPES: Record<string, number> = {
  '\\a': 7, '\\b': 8, '\\f': 12, '\\n': 10, '\\r': 13, '\\t': 9, '\\v': 11, '\\\\': 92,
}

/** `\N{NAME}`: CPython looks the name up in unicodedata, which JS does not
 *  have — so the syntax is checked and the pattern refused (an unknown name
 *  would be invalid in Python; it is reported unportable here). */
function namedEscape(src: Src, st: St): number {
  if (!src.match('{')) throw new PyErr('missing {')
  src.getuntil('}')
  st.refuse('\\N{...} (no Unicode name table in JS)')
  return 0x3f
}

/** A backreference: refused (JS matches a group that did not take part as
 *  empty; Python fails), but CPython's own checks still apply. */
function backref(gid: number, st: St): Node {
  if (st.open.has(gid)) throw new PyErr('cannot refer to an open group')
  st.checkLookbehindGroup(gid)
  st.refuse('backreference')
  return { t: 'ref', w: st.widths.get(gid) ?? [0, MAXWIDTH] }
}

function hexEscape(src: Src, esc: string, st: St): number {
  const n = esc[1] === 'x' ? 2 : esc[1] === 'u' ? 4 : 8
  const digits = src.getwhile(n, HEX)
  if (digits.length !== n) throw new PyErr(`incomplete escape ${esc}${digits}`)
  const v = parseInt(digits, 16)
  if (v > 0x10ffff) throw new PyErr(`bad escape ${esc}${digits}`)
  if (v >= 0xd800 && v <= 0xdfff) st.refuse('surrogate escape')
  return v
}

/** CPython's _class_escape. */
function classEscape(src: Src, esc: string, st: St): Item {
  if (esc in ESCAPES) return { k: 'cp', cp: ESCAPES[esc] }
  const c = esc[1]
  if ('dDsSwW'.includes(c)) return { k: 'cat', c }
  if (c === 'x' || c === 'u' || c === 'U') return { k: 'cp', cp: hexEscape(src, esc, st) }
  if (c === 'N') return { k: 'cp', cp: namedEscape(src, st) }
  if (OCT.includes(c)) {
    const e = esc + src.getwhile(2, OCT)
    const v = parseInt(e.slice(1), 8)
    if (v > 0o377) throw new PyErr(`octal escape value ${e} outside of range 0-0o377`)
    return { k: 'cp', cp: v }
  }
  if (DIGITS.includes(c) || isAsciiLetter(c)) throw new PyErr(`bad escape ${esc}`)
  return { k: 'cp', cp: esc.codePointAt(1)! }
}

/** CPython's _escape (outside a class). */
function escape(src: Src, esc: string, st: St): Node {
  const c = esc[1]
  switch (c) {
    case 'A': return { t: 'at', a: 'A' }
    case 'Z': return { t: 'at', a: 'Z' }
    case 'z':
      st.refuse('\\z (Python 3.14+ only)')
      return { t: 'at', a: 'Z' }
    case 'b': return { t: 'at', a: 'b' }
    case 'B': return { t: 'at', a: 'B' }
    case 'd': case 'D': case 's': case 'S': case 'w': case 'W':
      return { t: 'set', neg: false, items: [{ k: 'cat', c }] }
  }
  if (esc in ESCAPES) return { t: 'lit', cp: ESCAPES[esc] }
  if (c === 'x' || c === 'u' || c === 'U') return { t: 'lit', cp: hexEscape(src, esc, st) }
  if (c === 'N') return { t: 'lit', cp: namedEscape(src, st) }
  if (c === '0') return { t: 'lit', cp: parseInt((esc + src.getwhile(2, OCT)).slice(1), 8) }
  if (DIGITS.includes(c)) {
    let e = esc
    if (isOne(src.next, DIGITS)) {
      e += src.get()
      if (OCT.includes(e[1]) && OCT.includes(e[2]) && isOne(src.next, OCT)) {
        e += src.get()
        const v = parseInt(e.slice(1), 8)
        if (v > 0o377) throw new PyErr(`octal escape value ${e} outside of range 0-0o377`)
        return { t: 'lit', cp: v }
      }
    }
    const g = parseInt(e.slice(1), 10)
    if (g < st.groups) return backref(g, st)
    throw new PyErr(`invalid group reference ${g}`)
  }
  if (isAsciiLetter(c)) throw new PyErr(`bad escape ${esc}`)
  return { t: 'lit', cp: esc.codePointAt(1)! }
}

function parseSub(src: Src, st: St, verbose: boolean, nested: number): Node[] {
  const items: Node[][] = []
  for (;;) {
    items.push(parse(src, st, verbose, nested + 1, !nested && !items.length))
    if (!src.match('|')) break
    if (!nested) verbose = !!(st.flags & X)
  }
  return items.length === 1 ? items[0] : [{ t: 'alt', branches: items }]
}

/** CPython's _parse_flags; `glob` when the group was `(?flags)`. */
function parseFlags(src: Src, ch: string): { glob: boolean; add: number; del: number } {
  let add = 0, del = 0
  let c: string | null = ch
  if (c !== '-') {
    for (;;) {
      if (c === 'L') throw new PyErr("bad inline flags: cannot use 'L' flag with a str pattern")
      const f = FLAGS[c!]
      add |= f
      if (f & TYPE_FLAGS && (add & TYPE_FLAGS) !== f) throw new PyErr("bad inline flags: flags 'a', 'u' and 'L' are incompatible")
      c = src.get()
      if (c === null) throw new PyErr('missing -, : or )')
      if (c === ')' || c === '-' || c === ':') break
      if (!(c in FLAGS)) throw new PyErr('unknown flag')
    }
  }
  if (c === ')') return { glob: true, add, del }
  if (c === '-') {
    c = src.get()
    if (c === null || !(c in FLAGS)) throw new PyErr('missing flag')
    for (;;) {
      const f = FLAGS[c!]
      if (f & TYPE_FLAGS) throw new PyErr("bad inline flags: cannot turn off flags 'a', 'u' and 'L'")
      del |= f
      c = src.get()
      if (c === null) throw new PyErr('missing :')
      if (c === ':') break
      if (!(c in FLAGS)) throw new PyErr('unknown flag')
    }
  }
  if (add & del) throw new PyErr('bad inline flags: flag turned on and off')
  return { glob: false, add, del }
}

function parseClass(src: Src, st: St): SetNode {
  const items: Item[] = []
  const neg = src.match('^')
  const atom = (tok: string): Item =>
    tok[0] === '\\' ? classEscape(src, tok, st) : { k: 'cp', cp: tok.codePointAt(0)! }
  for (;;) {
    const tok = src.get()
    if (tok === null) throw new PyErr('unterminated character set')
    if (tok === ']' && items.length) break
    const code1 = atom(tok)
    if (src.match('-')) {
      const that = src.get()
      if (that === null) throw new PyErr('unterminated character set')
      if (that === ']') {
        items.push(code1, { k: 'cp', cp: 45 })
        break
      }
      const code2 = atom(that)
      if (code1.k !== 'cp' || code2.k !== 'cp' || code2.cp < code1.cp) throw new PyErr(`bad character range ${tok}-${that}`)
      items.push({ k: 'range', lo: code1.cp, hi: code2.cp })
    } else {
      items.push(code1)
    }
  }
  return { t: 'set', neg, items }
}

/** CPython's _parse: one branch. */
function parse(src: Src, st: St, verbose: boolean, nested: number, first: boolean): Node[] {
  const out: Node[] = []
  for (;;) {
    const tok = src.next
    if (tok === null || tok === '|' || tok === ')') break
    src.get()
    if (verbose) {
      if (WS.includes(tok)) continue
      if (tok === '#') {
        for (;;) {
          const t = src.get()
          if (t === null || t === '\n') break
        }
        continue
      }
    }
    if (tok[0] === '\\') {
      out.push(escape(src, tok, st))
    } else if (!SPECIAL.includes(tok)) {
      out.push({ t: 'lit', cp: tok.codePointAt(0)! })
    } else if (tok === '[') {
      out.push(parseClass(src, st))
    } else if ('*+?{'.includes(tok)) {
      let min = 0, max = MAXREPEAT
      if (tok === '?') max = 1
      else if (tok === '+') min = 1
      else if (tok === '{') {
        if (src.next === '}') {
          out.push({ t: 'lit', cp: 123 })
          continue
        }
        const here = src.tell()
        let lo = '', hi = ''
        while (isOne(src.next, DIGITS)) lo += src.get()
        if (src.match(',')) {
          while (isOne(src.next, DIGITS)) hi += src.get()
        } else hi = lo
        if (!src.match('}')) {
          out.push({ t: 'lit', cp: 123 })
          src.seek(here)
          continue
        }
        if (lo) {
          min = parseInt(lo, 10)
          if (min >= MAXREPEAT) throw new PyErr('the repetition number is too large')
        }
        if (hi) {
          max = parseInt(hi, 10)
          if (max >= MAXREPEAT) throw new PyErr('the repetition number is too large')
          if (max < min) throw new PyErr('min repeat greater than max repeat')
        }
      }
      const item = out[out.length - 1]
      if (!item || item.t === 'at') throw new PyErr('nothing to repeat')
      if (item.t === 'rep') throw new PyErr('multiple repeat')
      let lazy = false
      if (src.match('?')) lazy = true
      else if (src.match('+')) st.refuse('possessive quantifier (Python 3.11+ only)')
      out[out.length - 1] = { t: 'rep', min, max, lazy, body: item }
    } else if (tok === '.') {
      out.push({ t: 'any' })
    } else if (tok === '(') {
      let cap = true
      let name: string | undefined
      let add = 0, del = 0
      if (src.match('?')) {
        const ch = src.get()
        if (ch === null) throw new PyErr('unexpected end of pattern')
        if (ch === 'P') {
          if (src.match('<')) {
            name = src.getuntil('>')
            checkName(name, st)
          } else if (src.match('=')) {
            const n = src.getuntil(')')
            checkName(n, st)
            const gid = st.names.get(n)
            if (gid === undefined) throw new PyErr(`unknown group name ${n}`)
            out.push(backref(gid, st))
            continue
          } else {
            src.get()
            throw new PyErr('unknown extension ?P')
          }
        } else if (ch === ':') {
          cap = false
        } else if (ch === '#') {
          for (;;) {
            if (src.next === null) throw new PyErr('missing ), unterminated comment')
            if (src.get() === ')') break
          }
          continue
        } else if (ch === '=' || ch === '!' || ch === '<') {
          let kind = ch
          const behind = ch === '<'
          if (behind) {
            const c2 = src.get()
            if (c2 === null) throw new PyErr('unexpected end of pattern')
            if (c2 !== '=' && c2 !== '!') throw new PyErr('unknown extension ?<' + c2)
            kind = c2
          }
          const outerLb = st.lookbehindGroups
          if (behind && outerLb === null) st.lookbehindGroups = st.groups
          const body = parseSub(src, st, verbose, nested + 1)
          if (behind && outerLb === null) st.lookbehindGroups = null
          if (!src.match(')')) throw new PyErr('missing ), unterminated subpattern')
          if (behind) {
            const [lo, hi] = width(body)
            if (lo !== hi) throw new PyErr('look-behind requires fixed-width pattern')
          }
          out.push({ t: 'look', behind, neg: kind === '!', body })
          continue
        } else if (ch === '(') {
          out.push(parseConditional(src, st, verbose, nested))
          continue
        } else if (ch === '>') {
          st.refuse('atomic group (Python 3.11+ only)')
          cap = false
        } else if (ch in FLAGS || ch === '-') {
          const f = parseFlags(src, ch)
          if (f.glob) {
            if (!first || out.length) throw new PyErr('global flags not at the start of the expression')
            st.flags |= f.add
            verbose = !!(st.flags & X)
            continue
          }
          if ((f.add | f.del) & A) st.refuse('ASCII flag')
          add = f.add
          del = f.del
          cap = false
        } else {
          throw new PyErr('unknown extension ?' + ch)
        }
      }
      let gid = 0
      if (cap) {
        gid = st.groups++
        if (name !== undefined) {
          if (st.names.has(name)) throw new PyErr(`redefinition of group name ${name}`)
          st.names.set(name, gid)
        }
        st.open.add(gid)
      }
      const subVerbose = (verbose || !!(add & X)) && !(del & X)
      const body = parseSub(src, st, subVerbose, nested + 1)
      if (!src.match(')')) throw new PyErr('missing ), unterminated subpattern')
      if (cap) {
        st.open.delete(gid)
        st.widths.set(gid, width(body))
      }
      out.push({ t: 'group', cap, name, add, del, body })
    } else if (tok === '^') {
      out.push({ t: 'at', a: 'bol' })
    } else if (tok === '$') {
      out.push({ t: 'at', a: 'eol' })
    }
  }
  return out
}

/** `(?(id)yes|no)`: parsed with CPython's checks, then refused (JS has no
 *  conditionals). */
function parseConditional(src: Src, st: St, verbose: boolean, nested: number): Node {
  const cond = src.getuntil(')')
  let gid: number
  if (/^[0-9]+$/.test(cond)) {
    gid = parseInt(cond, 10)
    if (!gid) throw new PyErr('bad group number')
    st.condRefs.push(gid)
  } else {
    checkName(cond, st)
    const g = st.names.get(cond)
    if (g === undefined) throw new PyErr(`unknown group name ${cond}`)
    gid = g
  }
  st.checkLookbehindGroup(gid)
  const yes = parse(src, st, verbose, nested + 1, false)
  let no: Node[] | null = null
  if (src.match('|')) {
    no = parse(src, st, verbose, nested + 1, false)
    if (src.next === '|') throw new PyErr('conditional backref with more than two branches')
  }
  if (!src.match(')')) throw new PyErr('missing ), unterminated subpattern')
  st.refuse('conditional group')
  const [yl, yh] = width(yes)
  const [nl, nh] = no ? width(no) : [0, 0]
  return { t: 'ref', w: [no ? Math.min(yl, nl) : 0, Math.max(yh, nh)] }
}

/** CPython's SubPattern.getwidth(), for the fixed-width lookbehind rule. */
function width(nodes: Node[]): [number, number] {
  let lo = 0, hi = 0
  for (const n of nodes) {
    const [a, b] = nodeWidth(n)
    lo = Math.min(lo + a, MAXWIDTH)
    hi = Math.min(hi + b, MAXWIDTH)
  }
  return [lo, hi]
}
function nodeWidth(n: Node): [number, number] {
  switch (n.t) {
    case 'lit': case 'any': case 'set': return [1, 1]
    case 'at': case 'look': return [0, 0]
    case 'ref': return n.w
    case 'group': return width(n.body)
    case 'alt': {
      let i = MAXWIDTH, j = 0
      for (const b of n.branches) {
        const [l, h] = width(b)
        i = Math.min(i, l)
        j = Math.max(j, h)
      }
      return [i, j]
    }
    case 'rep': {
      const [i, j] = nodeWidth(n.body)
      const hi = n.max === MAXREPEAT && j ? MAXWIDTH : Math.min(j * n.max, MAXWIDTH)
      return [Math.min(i * n.min, MAXWIDTH), hi]
    }
  }
}

// ── emit ───────────────────────────────────────────────────────────────────

/** Python's `\w`: str.isalnum() or '_' — Unicode letters and numbers. */
const W = '\\p{L}\\p{N}_'
/** Python's `\d`: Unicode decimal digits (str.isdecimal()). */
const D = '\\p{Nd}'
/** Python's `\s` (Py_UNICODE_ISSPACE), listed: JS's `\s` is a different set. */
const SP = '\\t-\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
const START = '(?<![\\s\\S])'
const END = '(?![\\s\\S])'
const BOUNDARY = `(?:(?<=[${W}])(?![${W}])|(?<![${W}])(?=[${W}]))`
const NONBOUNDARY = `(?:(?<=[${W}])(?=[${W}])|(?<![${W}])(?![${W}]))`

/** Under re.I CPython puts İ (U+0130: its simple lowercase is i) and ı
 *  (U+0131: re._casefix) in i/I's class; JS simple case folding leaves both
 *  alone. Every other case class agrees (tests/pyre.test.ts walks them all). */
const I_CLASS = [0x49, 0x69, 0x130, 0x131]
const I_CLASS_JS = 'i\\u{130}\\u{131}'
const foldsWithI = (lo: number, hi: number) => I_CLASS.some((c) => lo <= c && c <= hi)

const MODIFIERS_OK = (() => {
  try {
    new RegExp('(?i:a)(?-i:b)', 'u')
    return true
  } catch {
    return false
  }
})()

function lit(cp: number, inClass: boolean): string {
  const c = String.fromCodePoint(cp)
  if (/^[A-Za-z0-9]$/.test(c)) return c
  if ('^$\\.*+?()[]{}|/'.includes(c) || (inClass && c === '-')) return '\\' + c
  if (cp >= 0x20 && cp < 0x7f) return c
  return `\\u{${cp.toString(16)}}`
}

type Ctx = { i: boolean; m: boolean; s: boolean }

function emitSet(n: SetNode, ctx: Ctx): string {
  let pos = ''
  const negs: string[] = []
  let iFold = false
  for (const it of n.items) {
    if (it.k === 'cp') {
      pos += lit(it.cp, true)
      iFold ||= ctx.i && foldsWithI(it.cp, it.cp)
    } else if (it.k === 'range') {
      pos += lit(it.lo, true) + '-' + lit(it.hi, true)
      iFold ||= ctx.i && foldsWithI(it.lo, it.hi)
    } else if (it.c === 'd') pos += D
    else if (it.c === 'D') pos += '\\P{Nd}'
    else if (it.c === 'w') pos += W
    else if (it.c === 's') pos += SP
    else negs.push(it.c === 'W' ? W : SP)
  }
  if (iFold) pos += I_CLASS_JS
  if (!n.neg) {
    const parts = [...(pos ? [`[${pos}]`] : []), ...negs.map((b) => `[^${b}]`)]
    return parts.length === 1 ? parts[0] : `(?:${parts.join('|')})`
  }
  if (!negs.length) return `[^${pos}]`
  // [^…\W…]: outside every listed member, inside every negated category's complement
  let r = pos ? `(?![${pos}])` : ''
  for (let k = 0; k < negs.length - 1; k++) r += `(?=[${negs[k]}])`
  return `(?:${r}[${negs[negs.length - 1]}])`
}

const emitSeq = (nodes: Node[], ctx: Ctx): string => nodes.map((n) => emitNode(n, ctx)).join('')

function emitNode(n: Node, ctx: Ctx): string {
  switch (n.t) {
    case 'lit':
      return ctx.i && foldsWithI(n.cp, n.cp) ? `[${I_CLASS_JS}]` : lit(n.cp, false)
    case 'any':
      return ctx.s ? '[\\s\\S]' : '[^\\n]'
    case 'set':
      return emitSet(n, ctx)
    case 'at':
      switch (n.a) {
        case 'bol': return ctx.m ? '(?<![^\\n])' : START
        case 'eol': return ctx.m ? `(?=\\n|${END})` : `(?=\\n?${END})`
        case 'A': return START
        case 'Z': return END
        case 'b': return BOUNDARY
        case 'B': return NONBOUNDARY
      }
      break
    case 'alt':
      return '(?:' + n.branches.map((b) => emitSeq(b, ctx)).join('|') + ')'
    case 'look':
      return '(?' + (n.behind ? '<' : '') + (n.neg ? '!' : '=') + emitSeq(n.body, ctx) + ')'
    case 'group': {
      const on = (bit: number, cur: boolean) => (cur || !!(n.add & bit)) && !(n.del & bit)
      const inner: Ctx = { i: on(I, ctx.i), m: on(M, ctx.m), s: on(S, ctx.s) }
      const body = emitSeq(n.body, inner)
      if (n.cap) return (n.name ? `(?<${n.name}>` : '(') + body + ')'
      if (inner.i !== ctx.i) {
        if (!MODIFIERS_OK) throw new Unport('scoped i needs regexp modifiers')
        return (inner.i ? '(?i:' : '(?-i:') + body + ')'
      }
      return '(?:' + body + ')'
    }
    case 'rep': {
      const body = emitNode(n.body, ctx)
      const max = n.max === MAXREPEAT ? '' : String(n.max)
      let q: string
      if (n.min === 0 && max === '') q = '*'
      else if (n.min === 1 && max === '') q = '+'
      else if (n.min === 0 && n.max === 1) q = '?'
      else if (String(n.min) === max) q = `{${n.min}}`
      else q = `{${n.min},${max}}`
      return `(?:${body})${q}${n.lazy ? '?' : ''}`
    }
  }
  throw new Error('pyre: node not emittable: ' + n.t)
}

type Compiled = { src: string; flags: string; groups: number; rx: RegExp; g?: RegExp }
type Outcome = { ok: Compiled } | { err: 'invalid' | 'unportable'; why: string }

const CACHE = new Map<string, Outcome>()
const CACHE_MAX = 4000

function translate(pattern: string, flags: string): Outcome {
  try {
    const st = new St()
    for (const f of flags) {
      if (f === 'g') continue
      if (!(f in FLAGS) || f === 'L') throw new PyErr(`unknown flag letter ${f}`)
      st.flags |= FLAGS[f]
    }
    const src = new Src(pattern)
    const nodes = parseSub(src, st, !!(st.flags & X), 0)
    if (src.next !== null) throw new PyErr('unbalanced parenthesis')
    for (const g of st.condRefs) if (g >= st.groups) throw new PyErr(`invalid group reference ${g}`)
    if (st.flags & A) st.refuse('ASCII flag')
    if (st.unport) throw new Unport(st.unport)
    const ctx: Ctx = { i: !!(st.flags & I), m: !!(st.flags & M), s: !!(st.flags & S) }
    const out = emitSeq(nodes, ctx)
    const jsFlags = 'u' + (ctx.i ? 'i' : '')
    let rx: RegExp
    try {
      rx = new RegExp(out, jsFlags)
    } catch (e) {
      // The emitter wrote something this engine rejects — a port bug, but the
      // honest answer is still "no identical JS".
      throw new Unport('emitted pattern rejected: ' + String(e))
    }
    return { ok: { src: out, flags: jsFlags, groups: st.groups - 1, rx } }
  } catch (e) {
    if (e instanceof PyErr) return { err: 'invalid', why: e.message }
    if (e instanceof Unport) return { err: 'unportable', why: e.message }
    throw e
  }
}

function outcome(pattern: string, flags: string): Outcome {
  const key = flags + '\u0000' + pattern
  let o = CACHE.get(key)
  if (!o) {
    o = translate(pattern, flags)
    if (CACHE.size >= CACHE_MAX) CACHE.clear()
    CACHE.set(key, o)
  }
  return o
}

/** The JS RegExp equivalent of `re.compile(pattern, flags)`, or null when the
 *  pattern cannot be expressed in JS with identical semantics (the caller
 *  then treats the rule as unportable — see rules.ts). Cached per (pattern, flags).
 *  The RegExp is shared: use `.test`/`.exec` only (it carries no g/y flag, so
 *  no lastIndex state). A 'g' in `flags` is accepted and ignored; use
 *  pyFindall1 for findall. */
export function pyCompile(pattern: string, flags = ''): RegExp | null {
  const o = outcome(pattern, flags)
  return 'ok' in o ? o.ok.rx : null
}

/** `bool(re.search(pattern, text, flags))`. Throws PyReError when the pattern
 *  is unportable or invalid — Python would raise re.error there, and every
 *  caller already sits inside the same try/except Python's does. */
export function pySearch(pattern: string, text: string, flags = ''): boolean {
  const o = outcome(pattern, flags)
  if (!('ok' in o)) throw new PyReError(pattern, o.why)
  return o.ok.rx.test(text)
}

/** `re.findall` for a pattern with at most ONE capture group (the only form
 *  the engine uses): per match the group's text ('' when it took no part), or
 *  the whole match when there is no group. Throws PyReError for an
 *  unportable/invalid pattern, or one with 2+ groups (Python returns tuples).
 *  Empty matches advance as CPython 3.7+ does: after an empty match at p, a
 *  non-empty match may still start at p. */
export function pyFindall1(pattern: string, text: string, flags = ''): string[] {
  const o = outcome(pattern, flags)
  if (!('ok' in o)) throw new PyReError(pattern, o.why)
  const c = o.ok
  if (c.groups > 1) throw new PyReError(pattern, 'findall with 2+ groups returns tuples')
  const g = (c.g ??= new RegExp(c.src, c.flags + 'g'))
  const pick = (m: RegExpExecArray) => (c.groups ? (m[1] ?? '') : m[0])
  const res: string[] = []
  let pos = 0
  let afterEmpty = false
  while (pos <= text.length) {
    let m: RegExpExecArray | null = null
    if (afterEmpty) {
      // CPython's must_advance: at the spot of the last empty match only a
      // non-empty match counts, and the engine backtracks into one if it can.
      // Said in JS: the match must END at least one code point past `pos`.
      const minEnd = Array.from(text.slice(0, pos)).length + 1
      const ne = new RegExp(`(?:${c.src})(?<=${START}[\\s\\S]{${minEnd},})`, c.flags + 'y')
      ne.lastIndex = pos
      m = ne.exec(text)
      if (!m) {
        if (pos >= text.length) break
        pos += text.codePointAt(pos)! > 0xffff ? 2 : 1
      }
    }
    if (!m) {
      g.lastIndex = pos
      m = g.exec(text)
      if (!m) break
    }
    res.push(pick(m))
    afterEmpty = m[0].length === 0
    pos = m.index + m[0].length
  }
  return res
}

/** Does pyCompile give an identical JS regex? False when unportable AND when
 *  CPython would reject the pattern (such a rule is dropped by rx_ok anyway). */
export function isPortable(pattern: string, flags = ''): boolean {
  return 'ok' in outcome(pattern, flags)
}

/** 'ok'; 'invalid' (CPython 3.14's `re.compile` raises); 'unportable' (valid
 *  Python, or valid on only some Python versions, with no identical JS). */
export function pyCheck(pattern: string, flags = ''): 'ok' | 'invalid' | 'unportable' {
  const o = outcome(pattern, flags)
  return 'ok' in o ? 'ok' : o.err
}

/** The JS source and flags pyCompile built, or null (debugging and tests). */
export function pyTranslate(pattern: string, flags = ''): { source: string; flags: string } | null {
  const o = outcome(pattern, flags)
  return 'ok' in o ? { source: o.ok.src, flags: o.ok.flags } : null
}

export class PyReError extends Error {
  /** Where CPython raised for the same pattern, it raised `re.PatternError`
   *  (the golden-vector tests compare a `raises` row against this). */
  readonly pyName = 'PatternError'
  constructor(pattern: string, why = '') {
    super(`pattern not portable to JS: ${pattern.slice(0, 80)}${why ? ` (${why})` : ''}`)
    this.name = 'PyReError'
  }
}
