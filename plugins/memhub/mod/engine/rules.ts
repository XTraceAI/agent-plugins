// Rule shaping and matching: a port of rulebook_hook.py's `to_hook_rule`
// family, `evaluate`, `given_ok` and `disclosure_line`. Pure: no `$`, no Node.
// The golden vectors (scripts/rule_vector_cases/rules.py) hold every export
// here to the Python's answers.
//
// Python name → TS name:
//   to_hook_rule → toHookRule      _book_facts → bookFacts     book_rank → bookRank
//   _clean_text → cleanText        _one_line → oneLine         _version_of → versionOf
//   rx_ok → rxOk                   ordering_rx_ok → orderingRxOk
//   matcher_unsupported → matcherUnsupported   given_unsupported → givenUnsupported
//   given_supported → givenSupported           ordering_unsupported → orderingUnsupported
//   degradation → degradation      _degrade → degrade          version_tuple → versionTuple
//   given_norm → givenNorm         _norm_given → normGiven     evaluate → evaluate
//   given_ok → givenOk (Probes → GivenFacts)   disclosure_desc → disclosureDesc
//   disclosure_line → disclosureLine
//
// What the Python reads from its process (the hook's own version, the
// forward-test claim `_ACTIVE_BASE`, git/transcript probes) comes in as
// arguments: ShapeOptions and GivenFacts.
//
// UNPORTABLE PATTERNS. Every rule pattern runs through pyre.ts. When
// `pyCompile` cannot express one in JS it returns null, and the port cannot
// tell "Python rejects this too" from "Python accepts this, JS cannot say it".
// `rxOk` assumes the second (the server lints every pattern with Python's
// `re`, so an uncompilable one should never reach the wire) and keeps the
// rule; `toHookRule` then marks it `_unportable: [<key>, ...]`, naming every
// field whose pattern pyre refused. `evaluate`/`givenOk` on such a rule are
// NOT trustworthy (a refused pattern throws PyReError, which `evaluate`
// swallows to False exactly as Python swallows re.error): the lane layer must
// route an `_unportable` rule to the Python hook or skip it, never evaluate it
// here. Python never emits `_unportable`; the vector test strips it and
// accepts a Python `None` for such a row (the pattern really was invalid).

import type { HookRule } from './index'
import { pyCompile, pyFindall1, pySearch } from './pyre'
import { commandFires, shellOnly } from './shell'

// ── Python value semantics ─────────────────────────────────────────────────

type Dict = Record<string, unknown>

export const isDict = (v: unknown): v is Dict =>
  v !== null && typeof v === 'object' && !Array.isArray(v)

const hasOwn = (o: object, k: string): boolean => Object.prototype.hasOwnProperty.call(o, k)

/** `d.get(k)` with None as null. */
export const get = (d: Dict, k: string): unknown => (hasOwn(d, k) ? d[k] ?? null : null)

/** `d.get(k, dflt)`: the default only when the key is absent. */
const getOr = (d: Dict, k: string, dflt: unknown): unknown => (hasOwn(d, k) ? d[k] ?? null : dflt)

/** Python truthiness of a JSON value. */
export function truthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === '') return false
  if (Array.isArray(v)) return v.length > 0
  if (isDict(v)) return Object.keys(v).length > 0
  return true
}

/** `pyName`: the Python exception this stands for, which the golden-vector
 *  tests compare against a `raises` vector's recorded name. */
class PyTypeError extends TypeError {
  readonly pyName = 'TypeError'
}

/** The pattern argument Python's `re` would accept (a str), else TypeError. */
const pat = (v: unknown): string => {
  if (typeof v !== 'string') throw new PyTypeError('first argument must be string or compiled pattern')
  return v
}

// str.isspace() / re's `\s` on a str pattern: the same set.
const WS = '\\t\\n\\x0b\\x0c\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'
const WS_RUN = new RegExp(`[${WS}]+`, 'g')
const WS_LEAD = new RegExp(`^[${WS}]+`)
const WS_TRAIL = new RegExp(`[${WS}]+$`)

/** `s.strip()` */
export const pyStrip = (s: string): string => s.replace(WS_LEAD, '').replace(WS_TRAIL, '')

/** `s.split()` */
export const pySplitWs = (s: string): string[] => {
  const t = pyStrip(s)
  return t ? t.split(WS_RUN) : []
}

/** `len(s)` in code points. */
export function cpLen(s: string): number {
  let n = 0
  for (const _ of s) n++
  return n
}

/** `s[start:end]` in code points (negative indexes as Python). */
export function cpSlice(s: string, start?: number, end?: number): string {
  // ASCII fast path: code units are code points.
  // eslint-disable-next-line no-control-regex
  if (/^[\x00-\x7f]*$/.test(s)) return s.slice(start, end)
  return Array.from(s).slice(start, end).join('')
}

const NON_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u

/** `repr(s)` for a str. */
function strRepr(s: string): string {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'"
  let out = q
  for (const ch of s) {
    const c = ch.codePointAt(0)!
    if (ch === '\\') out += '\\\\'
    else if (ch === q) out += '\\' + q
    else if (ch === '\t') out += '\\t'
    else if (ch === '\n') out += '\\n'
    else if (ch === '\r') out += '\\r'
    else if (ch !== ' ' && NON_PRINTABLE.test(ch)) {
      if (c < 0x100) out += '\\x' + c.toString(16).padStart(2, '0')
      else if (c < 0x10000) out += '\\u' + c.toString(16).padStart(4, '0')
      else out += '\\U' + c.toString(16).padStart(8, '0')
    } else out += ch
  }
  return out + q
}

/** `repr(x)` for a float that is not integral. */
function floatRepr(n: number): string {
  if (Number.isNaN(n)) return 'nan'
  if (!Number.isFinite(n)) return n > 0 ? 'inf' : '-inf'
  const [mant, e] = n.toExponential().split('e') as [string, string]
  const exp = Number(e)
  const neg = mant.startsWith('-')
  const digits = mant.replace('-', '').replace('.', '')
  if (exp < -4 || exp >= 16) {
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits
    return `${neg ? '-' : ''}${m}e${exp < 0 ? '-' : '+'}${String(Math.abs(exp)).padStart(2, '0')}`
  }
  let body: string
  if (exp < 0) body = '0.' + '0'.repeat(-exp - 1) + digits
  else if (digits.length > exp + 1) body = digits.slice(0, exp + 1) + '.' + digits.slice(exp + 1)
  else body = digits + '0'.repeat(exp + 1 - digits.length) + '.0'
  return (neg ? '-' : '') + body
}

/** `repr(x)` for a JSON value. A JSON float with a small integral value
 *  (`2.0`) is indistinguishable from an int here and reprs as one. */
export function pyRepr(v: unknown): string {
  if (v === null || v === undefined) return 'None'
  if (v === true) return 'True'
  if (v === false) return 'False'
  if (typeof v === 'number') {
    // JSON cannot tell 3.25e20 from an int; past 2**53 JS cannot hold an
    // int exactly anyway, so a big integral value reprs as Python's float.
    if (Number.isInteger(v) && Math.abs(v) < 1e16) return String(v)
    return floatRepr(v)
  }
  if (typeof v === 'string') return strRepr(v)
  if (Array.isArray(v)) return '[' + v.map(pyRepr).join(', ') + ']'
  if (isDict(v)) return '{' + Object.entries(v).map(([k, x]) => `${strRepr(k)}: ${pyRepr(x)}`).join(', ') + '}'
  return String(v)
}

/** `str(x)` */
export const pyStr = (v: unknown): string => (typeof v === 'string' ? v : pyRepr(v))

/** `iter(x)` / `list(x)`: a str yields code points, a dict its keys. */
export function pyIter(v: unknown): unknown[] {
  if (typeof v === 'string') return Array.from(v)
  if (Array.isArray(v)) return v
  if (isDict(v)) return Object.keys(v)
  throw new PyTypeError(`'${typeof v}' object is not iterable`)
}

/** `isinstance(v, int)` (bool excluded by the callers that exclude it). */
const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v)

// ── module constants ───────────────────────────────────────────────────────

export const EDIT_TOOLS: readonly string[] = ['Edit', 'Write', 'MultiEdit', 'NotebookEdit']
export const READ_TOOLS: readonly string[] = ['Read']
export const RESULT_WINDOW_CHARS = 8000
export const LOCAL_PKGS: ReadonlySet<string> = new Set(['xmem', 'evaluation', 'tests', 'app', 'scripts'])
/** `sys.stdlib_module_names` of CPython 3.14 (what the vectors are generated
 *  under). The Python hook uses whatever interpreter runs it: on < 3.10 its
 *  own short fallback list, on other 3.x a slightly different set. */
export const STDLIB: ReadonlySet<string> = new Set(
  ('__future__ _abc _aix_support _android_support _apple_support _ast _ast_unparse _asyncio _bisect _blake2 _bz2 ' +
    '_codecs _codecs_cn _codecs_hk _codecs_iso2022 _codecs_jp _codecs_kr _codecs_tw _collections _collections_abc ' +
    '_colorize _compat_pickle _contextvars _csv _ctypes _curses _curses_panel _datetime _dbm _decimal _elementtree ' +
    '_frozen_importlib _frozen_importlib_external _functools _gdbm _hashlib _heapq _hmac _imp _interpchannels ' +
    '_interpqueues _interpreters _io _ios_support _json _locale _lsprof _lzma _markupbase _md5 _multibytecodec ' +
    '_multiprocessing _opcode _opcode_metadata _operator _osx_support _overlapped _pickle _posixshmem ' +
    '_posixsubprocess _py_abc _py_warnings _pydatetime _pydecimal _pyio _pylong _pyrepl _queue _random ' +
    '_remote_debugging _scproxy _sha1 _sha2 _sha3 _signal _sitebuiltins _socket _sqlite3 _sre _ssl _stat ' +
    '_statistics _string _strptime _struct _suggestions _symtable _sysconfig _thread _threading_local _tkinter ' +
    '_tokenize _tracemalloc _types _typing _uuid _warnings _weakref _weakrefset _winapi _wmi _zoneinfo _zstd abc ' +
    'annotationlib antigravity argparse array ast asyncio atexit base64 bdb binascii bisect builtins bz2 cProfile ' +
    'calendar cmath cmd code codecs codeop collections colorsys compileall compression concurrent configparser ' +
    'contextlib contextvars copy copyreg csv ctypes curses dataclasses datetime dbm decimal difflib dis doctest ' +
    'email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch fractions ftplib functools ' +
    'gc genericpath getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http idlelib imaplib ' +
    'importlib inspect io ipaddress itertools json keyword linecache locale logging lzma mailbox marshal math ' +
    'mimetypes mmap modulefinder msvcrt multiprocessing netrc nt ntpath nturl2path numbers opcode operator ' +
    'optparse os pathlib pdb pickle pickletools pkgutil platform plistlib poplib posix posixpath pprint profile ' +
    'pstats pty pwd py_compile pyclbr pydoc pydoc_data pyexpat queue quopri random re readline reprlib resource ' +
    'rlcompleter runpy sched secrets select selectors shelve shlex shutil signal site smtplib socket socketserver ' +
    'sqlite3 sre_compile sre_constants sre_parse ssl stat statistics string stringprep struct subprocess symtable ' +
    'sys sysconfig syslog tabnanny tarfile tempfile termios textwrap this threading time timeit tkinter token ' +
    'tokenize tomllib trace traceback tracemalloc tty turtle turtledemo types typing unicodedata unittest urllib ' +
    'uuid venv warnings wave weakref webbrowser winreg winsound wsgiref xml xmlrpc zipapp zipfile zipimport zlib ' +
    'zoneinfo').split(' '),
)

const MATCHER_KEYS: ReadonlyMap<string, string> = new Map([
  ['command_rx', 'rx'], ['command_not_rx', 'not_rx'], ['content_not_rx', 'content_not_rx'],
  ['warn_once_per', 'fire_scope'], ['result_rx', 'rx'],
  ['prompt_rx', 'rx'], ['prompt_not_rx', 'not_rx'],
])
const RESULT_KEYS: ReadonlyMap<string, string> = new Map([
  ...MATCHER_KEYS,
  ['command_rx', 'cmd_rx'], ['command_not_rx', 'cmd_not_rx'],
  ['content_rx', 'rx'], ['content_not_rx', 'exclude_rx'],
])
export const MATCHER_KNOWN: ReadonlySet<string> = new Set([
  'event', 'command_rx', 'command_not_rx', 'content_rx', 'content_not_rx',
  'path_rx', 'path_not_rx', 'match_heredoc_body', 'body_rx', 'warn_once_per',
  'converted_rx', 'predicts_rx', 'min_chars', 'result_rx',
  'prompt_rx', 'prompt_not_rx', 'given',
])
const PROMPT_ONLY_KEYS: ReadonlySet<string> = new Set(['prompt_rx', 'prompt_not_rx'])
const PROMPT_SHARED_KEYS: ReadonlySet<string> = new Set(['warn_once_per', 'given', 'predicts_rx'])
const SCOPE_MAP: ReadonlyMap<string, string> = new Map([['turn', 'call'], ['file', 'session'], ['session', 'session']])
const RESERVED_RULE_KEYS: ReadonlySet<string> = new Set([
  'id', 'text', 'why', 'status', 'mode', '_version', '_label',
  'on', 'repo_scope', '_scope_repos', '_scope_paths',
  '_scope_exclude_paths', 'anchors', 'ordering',
  '_rulebook_id', '_book_name', '_book_scope', '_book_members',
  '_book_kind', '_book_label',
  'min_hook_version', '_degraded',
])
export const RX_KEYS: readonly string[] = [
  'rx', 'not_rx', 'body_rx', 'cmd_rx', 'cmd_not_rx', 'path_rx', 'path_not_rx',
  'content_rx', 'content_not_rx', 'exclude_rx', 'converted_rx',
]
const RX_MAX = 2000
const RX_NESTED = String.raw`\([^()]*[+*|][^()]*\)\s*[+*{]|\(\.\*\)|(\.\*){2,}`
const TEXT_MAX = 400
const BOOK_SCOPES: readonly string[] = ['all_org', 'explicit']
const BOOK_KINDS: readonly string[] = ['org', 'workspace', 'personal']
const BOOK_NAME_MAX = 120
const BOOK_ID_MAX = 64
const BOOK_MEMBERS_MAX = 10 ** 9
const BOOK_KEYS: readonly string[] = ['_rulebook_id', '_book_name', '_book_scope', '_book_members', '_book_kind', '_book_label']
export const CANDIDATE_ID_PREFIX = 'candidate-'

type Kind = 'rx' | 'int' | 'str' | 'bool'
const GIVEN: ReadonlyMap<string, ReadonlyMap<string, Kind>> = new Map([
  ['repo', new Map<string, Kind>([
    ['branch_rx', 'rx'], ['branch_not_rx', 'rx'], ['diff_lines_gt', 'int'],
    ['diff_files_gt', 'int'], ['diff_paths_rx', 'rx'], ['diff_paths_none_rx', 'rx'],
    ['dirty', 'bool'], ['spec_untouched', 'bool'], ['spec_dir', 'str'],
  ])],
  ['user', new Map<string, Kind>([['said_rx', 'rx'], ['not_said_rx', 'rx']])],
  ['file', new Map<string, Kind>([['lines_gt', 'int'], ['bytes_gt', 'int']])],
  ['agent', new Map<string, Kind>([['main', 'bool']])],
])
const ORDERING_KEYS: ReadonlySet<string> = new Set([
  'required_command_rx', 'gated_command_rx', 'armed_by_events',
  'armed_by_rx', 'min_edits', 'display_name', 'path_rx',
])
const ARMED_BY_EVENTS: ReadonlySet<string> = new Set(['edit', 'write', 'session', 'prompt'])

// ── prose ──────────────────────────────────────────────────────────────────

/** `_one_line`: one line, no control characters. */
export function oneLine(v: unknown): string {
  const s = truthy(v) ? pyStr(v) : ''
  // eslint-disable-next-line no-control-regex
  return pyStrip(s.replace(/[\x00-\x1f\x7f]+/g, ' ').replace(WS_RUN, ' '))
}

/** `_clean_text`: `oneLine`, capped at 400 code points. */
export const cleanText = (v: unknown): string => cpSlice(oneLine(v), 0, TEXT_MAX)

/** `_version_of`: an int or a 1–40-char string, else null. */
export function versionOf(v: unknown): number | string | null {
  if (typeof v === 'boolean') return null
  if (isInt(v)) return v
  if (typeof v === 'string' && v.length > 0 && cpLen(v) <= 40) return v
  return null
}

// ── pattern lint ───────────────────────────────────────────────────────────

/** `rx_ok`: a str, ≤2000 code points, none of the backtracking shapes, and
 *  compiles. A pattern pyre cannot express counts as compiling (see the
 *  header); `toHookRule` marks such a rule `_unportable`. */
export function rxOk(p: unknown): boolean {
  if (typeof p !== 'string' || cpLen(p) > RX_MAX || pySearch(RX_NESTED, p)) return false
  return true
}

/** Does pyre express `p` (a str) in JS? */
const portable = (p: unknown): boolean => typeof p !== 'string' || pyCompile(p) !== null

/** `ordering_rx_ok` */
export function orderingRxOk(o: Dict): boolean {
  if (!['required_command_rx', 'gated_command_rx'].every((k) => rxOk(get(o, k)))) return false
  return hasOwn(o, 'armed_by_rx') ? rxOk(o.armed_by_rx ?? null) : true
}

// ── version skew ───────────────────────────────────────────────────────────

export type Version = readonly [number, number, number]

/** `version_tuple`. ASCII digits only: Python's `\d` also takes other Nd
 *  digits ("١.٢.٣"); no server emits those. A component past 2**53 loses
 *  precision here (`degradation` compares exactly). */
export function versionTuple(v: unknown): Version | null {
  if (typeof v !== 'string') return null
  const m = VERSION_RX.exec(pyStrip(v))
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null
}

const VERSION_RX = /^([0-9]+)\.([0-9]+)\.([0-9]+)$/

/** `matcher_unsupported` */
export function matcherUnsupported(m: unknown): string {
  if (!isDict(m)) return ''
  return Object.keys(m).find((k) => !MATCHER_KNOWN.has(k)) ?? ''
}

/** `given_unsupported` */
export function givenUnsupported(g: unknown): string {
  if (!isDict(g)) return ''
  for (const [block, spec] of Object.entries(g)) {
    const kinds = GIVEN.get(block)
    if (!kinds) return cpSlice(block, 0, 40)
    if (isDict(spec)) {
      for (const k of Object.keys(spec)) if (!kinds.has(k)) return `${block}.${cpSlice(k, 0, 40)}`
    }
  }
  return ''
}

/** `given_supported` */
export function givenSupported(g: Dict): Dict {
  const out: Dict = {}
  for (const [block, spec] of Object.entries(g)) {
    const kinds = GIVEN.get(block)
    if (!kinds || !isDict(spec)) continue
    const kept: Dict = {}
    for (const [k, v] of Object.entries(spec)) if (kinds.has(k)) kept[k] = v
    if (Object.keys(kept).length) out[block] = kept
  }
  return out
}

/** `ordering_unsupported` */
export function orderingUnsupported(o: unknown): string {
  if (!isDict(o)) return ''
  for (const k of Object.keys(o)) if (!ORDERING_KEYS.has(k)) return `ordering.${cpSlice(k, 0, 40)}`
  const events = get(o, 'armed_by_events')
  if (Array.isArray(events)) {
    for (const ev of events) {
      if (Array.isArray(ev) || isDict(ev)) throw new PyTypeError('unhashable type')
      if (!(typeof ev === 'string' && ARMED_BY_EVENTS.has(ev))) {
        return `ordering.armed_by_events:${cpSlice(pyStr(ev), 0, 40)}`
      }
    }
  }
  return ''
}

/** `degradation(row, given, ordering)`. `hookVersion` is `hook_version()`:
 *  this build's own version, null when it cannot be read. */
export function degradation(row: Dict, given: unknown, ordering: unknown, hookVersion: Version | null): string {
  const wantRaw = get(row, 'min_hook_version')
  if (wantRaw !== null) {
    // Python ints are unbounded: compare and print the wanted version exactly.
    const m = typeof wantRaw === 'string' ? VERSION_RX.exec(pyStrip(wantRaw)) : null
    if (m === null) return `min_hook_version ${strRepr(cpSlice(pyStr(wantRaw), 0, 20))} is not major.minor.patch`
    const want = [BigInt(m[1]!), BigInt(m[2]!), BigInt(m[3]!)] as const
    const shown = want.join('.')
    if (hookVersion === null) {
      return `this rule needs hook ${shown} and this hook cannot read its own version`
    }
    const have = hookVersion.map((x) => BigInt(x))
    const less = have[0]! !== want[0] ? have[0]! < want[0] : have[1]! !== want[1] ? have[1]! < want[1] : have[2]! < want[2]
    if (less) return `this rule needs hook ${shown}; this is ${hookVersion.join('.')}`
  }
  const unknown = givenUnsupported(given) || orderingUnsupported(ordering)
  return unknown ? `this hook does not understand \`${unknown}\`` : ''
}

// ── given lint ─────────────────────────────────────────────────────────────

/** posixpath.normpath for a relative path (the only kind reaching it). */
function normRel(p: string): string {
  const out: string[] = []
  for (const c of p.split('/')) {
    if (c === '' || c === '.') continue
    if (c === '..' && out.length && out[out.length - 1] !== '..') out.pop()
    else out.push(c)
  }
  return out.join('/') || '.'
}

/** spec_owns.safe_spec_dir */
export function safeSpecDir(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  if (cpLen(raw) > 128) return null
  const p = pyStrip(raw)
  if (!p || p.includes('\\') || p.startsWith('/') || p.includes('\0')) return null
  if (cpLen(p) > 1024) return null
  const norm = normRel(p)
  if (norm === '.' || norm === '' || norm.startsWith('../') || norm === '..' || norm.startsWith('/')) return null
  return norm
}

/** `given_norm`: the block, or null (drops the rule). */
export function givenNorm(g: unknown): Dict | null {
  if (!isDict(g) || !Object.keys(g).length) return null
  const out: Dict = {}
  for (const [block, spec] of Object.entries(g)) {
    const kinds = GIVEN.get(block)
    if (!kinds || !isDict(spec) || !Object.keys(spec).length) return null
    for (const [k, v] of Object.entries(spec)) {
      const kind = kinds.get(k)
      if (kind === 'rx') {
        if (!rxOk(v)) return null
      } else if (kind === 'int') {
        if (typeof v === 'boolean' || !isInt(v) || v < 0) return null
      } else if (kind === 'str') {
        if (safeSpecDir(v) === null) return null
      } else if (kind === 'bool') {
        if (typeof v !== 'boolean') return null
      } else return null
    }
    out[block] = { ...spec }
  }
  return out
}

/** `_norm_given(r)`: normalise `r.given` in place; false drops the rule. */
export function normGiven(r: Dict): boolean {
  const raw = r.given ?? null
  if (!isDict(raw)) return false
  for (const [block, spec] of Object.entries(raw)) {
    if (GIVEN.has(block) && !(isDict(spec) && Object.keys(spec).length)) return false
  }
  const supported = givenSupported(raw)
  const has = Object.keys(supported).length > 0
  const kept = has ? givenNorm(supported) : null
  if (has && kept === null) return false
  if (kept && Object.keys(kept).length) r.given = kept
  else delete r.given
  return true
}

// ── book facts ─────────────────────────────────────────────────────────────

/** `_book_facts` */
export function bookFacts(row: Dict): Dict {
  const braw = get(row, 'rulebook')
  const b: Dict = isDict(braw) ? braw : {}
  const out: Dict = {}
  let rid: unknown = get(row, 'rulebook_id')
  if (!truthy(rid)) rid = get(b, 'rulebook_id')
  const ridS = typeof rid === 'string' ? pyStrip(rid) : ''
  // eslint-disable-next-line no-control-regex
  if (/^[^\x00-\x1f\x7f]+$/u.test(ridS) && cpLen(ridS) <= BOOK_ID_MAX) out._rulebook_id = ridS
  const name = cpSlice(cleanText(get(b, 'name')), 0, BOOK_NAME_MAX)
  if (name) out._book_name = name
  const scope = get(b, 'scope')
  if (typeof scope === 'string' && BOOK_SCOPES.includes(scope)) out._book_scope = scope
  const kind = get(b, 'kind')
  if (typeof kind === 'string' && BOOK_KINDS.includes(kind)) out._book_kind = kind
  const label = cpSlice(cleanText(get(b, 'label')), 0, BOOK_NAME_MAX)
  if (label) out._book_label = label
  const mc = get(b, 'member_count')
  if (isInt(mc) && mc >= 0 && mc <= BOOK_MEMBERS_MAX) out._book_members = mc
  return out
}

/** `book_rank`: sort key, all_org first, then wider membership. */
export function bookRank(rule: Dict): [number, number] {
  const members = get(rule, '_book_members')
  return [get(rule, '_book_scope') === 'all_org' ? 0 : 1, isInt(members) ? -members : 0]
}

// ── shaping ────────────────────────────────────────────────────────────────

export type ShapeOptions = {
  /** `hook_version()`: this build's version; null = unreadable (a
   *  `min_hook_version` row then degrades). */
  hookVersion: Version | null
  /** `bool(_ACTIVE_BASE)`: a /memhub:create-rule forward-test claim is in
   *  force, so `candidate-*` rows are honoured instead of dropped. */
  activeBase?: boolean
}

/** `_degrade(row, r, given, unknown_matcher)` */
export function degrade(row: Dict, r: Dict | null, given: unknown, unknownMatcher: string, opts: ShapeOptions): Dict | null {
  if (r === null) return null
  delete r.min_hook_version
  const id = get(r, 'id')
  if ((truthy(id) ? pyStr(id) : '').startsWith(CANDIDATE_ID_PREFIX) && !opts.activeBase) return null
  let why = degradation(row, given, get(r, 'ordering'), opts.hookVersion)
  if (!why && unknownMatcher) why = `this hook does not understand \`matcher.${unknownMatcher}\``
  if (!why) return r
  r._degraded = why
  r.mode = 'advise'
  return r
}

/** Keys of `r` whose pattern pyre refused (see the header). */
function unportableKeys(r: Dict): string[] {
  const bad: string[] = []
  for (const k of RX_KEYS) if (hasOwn(r, k) && !portable(r[k])) bad.push(k)
  const g = get(r, 'given')
  if (isDict(g)) {
    for (const [block, spec] of Object.entries(g)) {
      const kinds = GIVEN.get(block)
      if (!kinds || !isDict(spec)) continue
      for (const [k, v] of Object.entries(spec)) if (kinds.get(k) === 'rx' && !portable(v)) bad.push(`given.${block}.${k}`)
    }
  }
  const o = get(r, 'ordering')
  if (isDict(o)) {
    for (const k of ['required_command_rx', 'gated_command_rx', 'armed_by_rx', 'path_rx']) {
      if (hasOwn(o, k) && !portable(o[k])) bad.push(`ordering.${k}`)
    }
  }
  return bad
}

function shape(row: unknown, opts: ShapeOptions): Dict | null {
  if (!isDict(row)) return null
  if (hasOwn(row, 'on')) {
    const r: Dict = {}
    for (const [k, v] of Object.entries(row)) if (!BOOK_KEYS.includes(k)) r[k] = v
    if (!hasOwn(r, 'id')) r.id = get(row, 'rule_id')
    if (!hasOwn(r, '_version')) r._version = versionOf(get(row, 'version'))
    Object.assign(r, bookFacts(row))
    const cap = get(r, 'on') === 'session' ? oneLine : cleanText
    for (const k of ['text', 'why']) if (hasOwn(r, k)) r[k] = cap(r[k] ?? null)
    for (const k of ['_label', '_gate_msg']) if (hasOwn(r, k)) r[k] = cleanText(r[k] ?? null)
    if (!truthy(get(r, 'id')) || !RX_KEYS.every((k) => !hasOwn(r, k) || rxOk(r[k] ?? null))) return null
    const o = get(r, 'ordering')
    if (isDict(o) && !orderingRxOk(o)) return null
    const rawGiven = get(r, 'given')
    if (hasOwn(r, 'given') && !normGiven(r)) return null
    return degrade(row, r, rawGiven, '', opts)
  }
  const statement = get(row, 'statement')
  let id: unknown = get(row, 'rule_id')
  if (!truthy(id)) id = get(row, 'id')
  const r: Dict = {
    id,
    text: cleanText(truthy(statement) ? statement : get(row, 'title')),
    why: cleanText(get(row, 'why')),
    status: getOr(row, 'status', 'active'),
    _label: cleanText(get(row, 'title')) || null,
    mode: getOr(row, 'mode', 'advise'),
    _version: versionOf(get(row, 'version')),
  }
  Object.assign(r, bookFacts(row))
  if (!truthy(r.id)) return null
  const scopeRepos = get(row, 'scope_repos')
  const scopes = pyIter(truthy(scopeRepos) ? scopeRepos : []).filter(truthy).map(pyStr)
  r.repo_scope = 'any'
  if (scopes.length) r._scope_repos = scopes
  for (const k of ['scope_paths', 'scope_exclude_paths']) {
    const v = get(row, k)
    const globs = pyIter(truthy(v) ? v : []).filter((x): x is string => typeof x === 'string' && pyStrip(x) !== '')
    if (globs.length) r['_' + k] = globs.slice(0, 64)
  }
  if (get(row, 'delivery') === 'session_context') {
    r.on = 'session'
    return degrade(row, r, null, '', opts)
  }
  const anchorsRaw = get(row, 'anchors')
  if (Array.isArray(anchorsRaw) && anchorsRaw.length) {
    const anchors = anchorsRaw.filter((a): a is string => typeof a === 'string' && pyStrip(a) !== '').map(cleanText)
    if (!anchors.length) return null
    r.on = 'anchor'
    r.anchors = anchors.slice(0, 64)
    r.fire_scope = 'session'
    return degrade(row, r, null, '', opts)
  }
  const o = get(row, 'ordering')
  if (isDict(o)) {
    if (!orderingRxOk(o)) return null
    r.on = 'ordering'
    r.ordering = o
    return degrade(row, r, null, '', opts)
  }
  const m = get(row, 'matcher')
  if (!isDict(m)) return null
  const evRaw = get(m, 'event')
  const ev = truthy(evRaw) ? evRaw : 'bash'
  r.on = ev === 'output' ? 'result' : ev === 'write' ? 'edit' : ev
  const keys = r.on === 'result' ? RESULT_KEYS : MATCHER_KEYS
  const unknown = matcherUnsupported(m)
  for (const [k, v] of Object.entries(m)) {
    if (k === 'event' || !MATCHER_KNOWN.has(k)) continue
    if (k === 'result_rx' && hasOwn(m, 'content_rx')) continue
    if (PROMPT_ONLY_KEYS.has(k) !== (r.on === 'prompt') && !PROMPT_SHARED_KEYS.has(k)) continue
    const dest = keys.get(k) ?? k
    if (RESERVED_RULE_KEYS.has(dest)) continue
    r[dest] = v ?? null
  }
  const fs = hasOwn(r, 'fire_scope') ? r.fire_scope ?? null : 'session'
  r.fire_scope = typeof fs === 'string' && SCOPE_MAP.has(fs) ? SCOPE_MAP.get(fs)! : get(r, 'fire_scope')
  if (!RX_KEYS.every((k) => !hasOwn(r, k) || rxOk(r[k] ?? null))) return null
  if (r.on === 'prompt') {
    if (!truthy(get(r, 'rx'))) return null
    r.mode = 'advise'
  }
  const rawGiven = get(r, 'given')
  if (hasOwn(r, 'given') && !normGiven(r)) return null
  return degrade(row, r, rawGiven, unknown, opts)
}

/** `to_hook_rule(row)`: one `?view=hook` row (or a pilot-shape row with an
 *  `on` key) → the flat rule `evaluate`/OrderingEngine read, or null when the
 *  row is malformed. Never throws. Adds `_unportable` (see the header). */
export function toHookRule(row: unknown, opts: ShapeOptions): HookRule | null {
  let r: Dict | null
  try {
    r = shape(row, opts)
  } catch {
    return null
  }
  if (r === null) return null
  const bad = unportableKeys(r)
  if (bad.length) r._unportable = bad
  return r as HookRule
}

// ── evaluate ───────────────────────────────────────────────────────────────

export type MatchEvent = {
  hookPhase: 'pre' | 'post' | 'prompt' | string
  tool: string
  cmd?: string
  filePath?: string
  body?: string
  resultText?: string
  prompt?: string
  /** What an edit ADDED (`editAddedText`); null reads `body`. */
  added?: string | null
}

/** `rule.get(k) and re.search(rule[k], …)` */
const optSearch = (rule: Dict, k: string, text: string, flags = ''): boolean =>
  truthy(get(rule, k)) && pySearch(pat(rule[k]), text, flags)

/** `len(body) >= rule.get("min_chars", 800)` */
function enough(body: string, rule: Dict): boolean {
  const v = getOr(rule, 'min_chars', 800)
  if (typeof v === 'boolean') return cpLen(body) >= Number(v)
  if (typeof v !== 'number') throw new PyTypeError("'>=' not supported")
  return cpLen(body) >= v
}

function evaluateInner(rule: Dict, e: Required<MatchEvent>): boolean {
  const on = get(rule, 'on')
  const { hookPhase, tool, cmd, filePath, body, resultText, prompt, added } = e
  if (hookPhase === 'prompt' && on === 'prompt' && prompt) {
    if (!pySearch(pat(rule.rx), prompt, 'im')) return false
    return !optSearch(rule, 'not_rx', prompt, 'im')
  }
  if (hookPhase === 'pre' && on === 'bash' && tool === 'Bash' && cmd) {
    const shell = shellOnly(cmd)
    const target = truthy(get(rule, 'match_heredoc_body')) && !truthy(get(rule, 'body_rx')) ? cmd : shell
    const notRx = get(rule, 'not_rx')
    if (!commandFires(pat(rule.rx), target, truthy(notRx) ? pat(notRx) : null)) return false
    if (truthy(get(rule, 'body_rx'))) {
      const kept = new Set(shell.split('\n'))
      const bodyOnly = cmd.split('\n').filter((l) => !kept.has(l)).join('\n')
      return pySearch(pat(rule.body_rx), bodyOnly, 'im')
    }
    return true
  }
  if (hookPhase === 'pre' && on === 'edit' && EDIT_TOOLS.includes(tool)) {
    const pathRx = get(rule, 'path_rx')
    if (pySearch(pat(truthy(pathRx) ? pathRx : ''), filePath) && !optSearch(rule, 'path_not_rx', filePath)) {
      // content_rx asks what this edit WROTE, not the lines it kept.
      const written = added === null ? body : added
      if (hasOwn(rule, 'content_rx') && !pySearch(pat(rule.content_rx ?? null), written, 'm')) return false
      return !optSearch(rule, 'content_not_rx', body, 'm')
    }
    return false
  }
  if (hookPhase === 'pre' && on === 'read' && READ_TOOLS.includes(tool) && filePath) {
    if (truthy(get(rule, 'path_rx')) && !pySearch(pat(rule.path_rx), filePath)) return false
    if (optSearch(rule, 'path_not_rx', filePath)) return false
    if (cmd && optSearch(rule, 'not_rx', cmd, 'i')) return false
    return true
  }
  if (hookPhase === 'pre' && on === 'write_stdlib' && tool === 'Write' &&
      filePath.endsWith('.py') && !filePath.includes('scratchpad') &&
      !optSearch(rule, 'path_not_rx', filePath) && enough(body, rule)) {
    const mods = new Set(pyFindall1(String.raw`^(?:import|from)\s+([A-Za-z_]\w*)`, body, 'm'))
    return mods.size > 0 && ![...mods].some((m) => !STDLIB.has(m) && !LOCAL_PKGS.has(m))
  }
  if (hookPhase === 'post' && on === 'result' && resultText) {
    if (truthy(get(rule, 'cmd_rx')) && !pySearch(pat(rule.cmd_rx), cmd, 'i')) return false
    if (truthy(get(rule, 'cmd_not_rx')) && cmd && pySearch(pat(rule.cmd_not_rx), cmd, 'i')) return false
    const n = cpLen(resultText)
    const spans = n <= 2 * RESULT_WINDOW_CHARS
      ? [resultText]
      : [cpSlice(resultText, 0, RESULT_WINDOW_CHARS), cpSlice(resultText, -RESULT_WINDOW_CHARS)]
    if (truthy(get(rule, 'exclude_rx')) && spans.some((sp) => pySearch(pat(rule.exclude_rx), sp, 'm'))) return false
    return spans.some((sp) => pySearch(pat(rule.rx ?? null), sp, 'm'))
  }
  return false
}

/** `evaluate(rule, *, hook_phase, tool, …)`: does `rule` fire on this event?
 *  No I/O, no dedup. Any exception is False, as in Python — including a
 *  pattern pyre refused (never call this on an `_unportable` rule) and,
 *  until shell.ts lands, every bash-lane event. */
/** `added_lines(old, new)`: the lines of `new` that `old` does not have. */
export function addedLines(oldText: unknown, newText: unknown): string {
  const kept = new Set((truthy(oldText) ? pyStr(oldText) : '').split('\n'))
  return (truthy(newText) ? pyStr(newText) : '').split('\n').filter((l) => !kept.has(l)).join('\n')
}

/** `edit_added_text(inp)`: what an Edit / MultiEdit / Write call ADDED —
 *  each new_string minus its old_string's lines, a Write's whole content. */
export function editAddedText(inp: Dict): string {
  // `str(d.get(k, ""))`: a missing key is "", an explicit null is "None".
  const s = (d: Dict, k: string): string => (hasOwn(d, k) ? pyStr(d[k]) : '')
  const edits = truthy(inp.edits) && Array.isArray(inp.edits) ? (inp.edits as unknown[]).filter(isDict) : []
  // new_string goes in raw: `added_lines` reads it as `str(new or "")`.
  const raw = (d: Dict, k: string): unknown => (hasOwn(d, k) ? d[k] : '')
  return addedLines(inp.old_string, raw(inp, 'new_string')) + s(inp, 'content') +
    edits.map((x) => addedLines(x.old_string, raw(x, 'new_string'))).join('\n')
}

export function evaluate(rule: HookRule | Dict, e: MatchEvent): boolean {
  try {
    return evaluateInner(rule as Dict, {
      cmd: '', filePath: '', body: '', resultText: '', prompt: '', added: null, ...e,
    })
  } catch {
    return false
  }
}

// ── given ──────────────────────────────────────────────────────────────────

/** What a `given` block asks about: Python's `Probes`, answered up front by
 *  the lane (lazily via `givenNeeds`). undefined/null is a probe that failed
 *  or was not asked, and satisfies no predicate (fail open). */
export type GivenFacts = {
  /** The checkout's branch (`Probes.branch`). */
  branch?: string | null
  /** Added+deleted lines against the base, untracked included. */
  diffLines?: number | null
  /** Paths changed against the base, untracked included. */
  diffPaths?: readonly string[] | null
  /** `git status --porcelain` is non-empty. */
  dirty?: boolean | null
  /** What the person typed this session (`user_turns_of`). */
  userTurns?: readonly string[] | null
  /** Main agent (true) or a subagent (false): `CallEvent.agentId === undefined`. */
  agentMain?: boolean | null
  /** spec_dir → the owning specs this branch left alone; only emptiness is read. */
  untouchedSpecs?: Readonly<Record<string, readonly unknown[] | null>>
}

/** `read_facts(...)` for the event: what a read pulls into context. */
export type ReadFacts = { lines?: number | null; bytes?: number | null } | null

export type GivenFactName = Exclude<keyof GivenFacts, 'untouchedSpecs'> | `untouchedSpecs:${string}` | 'read'

/** Which facts `givenOk` will read for this rule, so the lane probes only those. */
export function givenNeeds(rule: Dict): GivenFactName[] {
  const g = get(rule, 'given')
  if (!isDict(g)) return []
  const out = new Set<GivenFactName>()
  if (isDict(g.file) && Object.keys(g.file).length) out.add('read')
  if (isDict(g.agent) && hasOwn(g.agent, 'main')) out.add('agentMain')
  if (isDict(g.user) && Object.keys(g.user).length) out.add('userTurns')
  const repo = isDict(g.repo) ? g.repo : {}
  for (const k of Object.keys(repo)) {
    if (k === 'branch_rx' || k === 'branch_not_rx') out.add('branch')
    else if (k === 'diff_lines_gt') out.add('diffLines')
    else if (k === 'diff_files_gt' || k === 'diff_paths_rx' || k === 'diff_paths_none_rx') out.add('diffPaths')
    else if (k === 'dirty') out.add('dirty')
    else if (k === 'spec_untouched') out.add(`untouchedSpecs:${pyStr(getOr(repo, 'spec_dir', 'docs/specs'))}`)
  }
  return [...out]
}

const gt = (have: unknown, v: unknown): boolean => typeof have === 'number' && typeof v === 'number' && have > v
const nil = (v: unknown): boolean => v === null || v === undefined

/** `given_ok(rule, probes, read)`: every predicate in `rule.given` holds.
 *  Throws PyReError only for a pattern pyre refused (an `_unportable` rule). */
export function givenOk(rule: Dict, facts: GivenFacts, read: ReadFacts = null): boolean {
  const g = get(rule, 'given')
  if (!truthy(g)) return true
  if (!isDict(g)) throw new PyTypeError("'given' is not a dict")   // Python: AttributeError
  const block = (k: string): Dict => {
    const b = get(g, k)
    return truthy(b) && isDict(b) ? b : {}
  }
  for (const [k, v] of Object.entries(block('file'))) {
    const fk = k === 'lines_gt' ? 'lines' : k === 'bytes_gt' ? 'bytes' : ''
    const have = read && fk ? (read as Dict)[fk] : undefined
    if (nil(have) || !gt(have, v)) return false
  }
  for (const [k, v] of Object.entries(block('agent'))) {
    if (k === 'main') {
      const m = facts.agentMain
      if (nil(m) || m !== v) return false
    }
  }
  const repo = block('repo')
  for (const [k, v] of Object.entries(repo)) {
    if (k === 'branch_rx') {
      const b = facts.branch
      if (!b || !pySearch(pat(v), b)) return false
    } else if (k === 'branch_not_rx') {
      const b = facts.branch
      if (!b || pySearch(pat(v), b)) return false
    } else if (k === 'diff_lines_gt') {
      if (nil(facts.diffLines) || !gt(facts.diffLines, v)) return false
    } else if (k === 'diff_files_gt') {
      const ps = facts.diffPaths
      if (nil(ps) || !gt(ps!.length, v)) return false
    } else if (k === 'diff_paths_rx') {
      const ps = facts.diffPaths
      if (nil(ps) || !ps!.some((p) => pySearch(pat(v), p))) return false
    } else if (k === 'diff_paths_none_rx') {
      const ps = facts.diffPaths
      if (nil(ps) || ps!.some((p) => pySearch(pat(v), p))) return false
    } else if (k === 'spec_untouched') {
      const dir = pyStr(getOr(repo, 'spec_dir', 'docs/specs'))
      const u = facts.untouchedSpecs && hasOwn(facts.untouchedSpecs, dir) ? facts.untouchedSpecs[dir] : undefined
      if (nil(u) || (u!.length > 0) !== v) return false
    } else if (k === 'dirty') {
      const d = facts.dirty
      if (nil(d) || d !== v) return false
    }
  }
  for (const [k, v] of Object.entries(block('user'))) {
    const turns = facts.userTurns
    if (nil(turns)) return false
    const said = turns!.some((t) => pySearch(pat(v), t, 'i'))
    if ((k === 'said_rx' && !said) || (k === 'not_said_rx' && said)) return false
  }
  return true
}

// ── disclosure ─────────────────────────────────────────────────────────────

export const DISCLOSE_ADVISORY = '📏'
export const DISCLOSE_BLOCKED = '⛔️'
export const DISCLOSE_PREFIX = 'Rule fired: '
const DESC_WORDS = 20
const DESC_CHARS = 120

/** `disclosure_desc`: the rule in ≤20 words / ≤120 chars, one line. */
export function disclosureDesc(rule: Dict): string {
  let raw: string | undefined
  for (const key of ['_label', 'text']) {
    const v = get(rule, key)
    if (typeof v === 'string' && pyStrip(v)) {
      raw = v
      break
    }
  }
  if (raw === undefined) {
    const id = get(rule, 'id')
    raw = truthy(id) ? pyStr(id) : ''
  }
  let text = pySplitWs(raw.replace(/[*`]+/g, '')).join(' ')
  const words = text.split(' ')
  let clipped = words.length > DESC_WORDS
  text = words.slice(0, DESC_WORDS).join(' ')
  if (cpLen(text) > DESC_CHARS) {
    const head = cpSlice(text, 0, DESC_CHARS)
    const i = head.lastIndexOf(' ')
    text = (i >= 0 ? head.slice(0, i) : head) || head
    clipped = true
  }
  return clipped && text ? text + '…' : text
}

/** `disclosure_line(rule, blocked)`: byte-identical to Python. */
export function disclosureLine(rule: Dict, blocked = false): string {
  return `${blocked ? DISCLOSE_BLOCKED : DISCLOSE_ADVISORY} ${DISCLOSE_PREFIX}${disclosureDesc(rule)}`
}
