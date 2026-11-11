// Credentials out of a tool's result before the session holds it.
//
// The plugin's upload path redacts what it sends (scripts/redact.py), and
// MemHub's server redacts again at ingest. Both are too late for the session
// itself: a command that prints a secret (`aws lambda
// get-function-configuration`, a `cat` of an env file) puts the value on the
// person's screen, in the model's context and in the local transcript. A
// `tool.call` hook can answer with its own `{ result }`, which core maps for
// the model and records as the tool's result (claude-code.d.ts
// ToolCallResult), so here the value is replaced before any of the three.
//
// `redactSecrets` is scripts/redact.py `redact_secrets` (the server's
// app/core/redaction.py), rule for rule: vendor-prefixed keys, PEM private
// keys, JWTs, DSN passwords, values assigned to secret-named keys, and
// context-free high-entropy tokens (Shannon entropy). Secrets strength only:
// no hex-token and no PII rule, because the agent needs commit SHAs and
// addresses to do its work. The generated vectors hold the two copies together:
// engine/tests/shell.test.ts replays Python's `redact.redact_text` answers
// (scripts/rule_vector_cases/shell.py REDACT_TEXTS) through `redactText`, which
// is this function, so a rule changed on one side alone fails that suite.
//
// Bash only. A file tool's result is left alone on purpose: an agent that
// read `<redacted>` where a value stands, then wrote the file back, would
// destroy the value on disk.

export const REDACTED = '<redacted>'

const PREFIXED: readonly RegExp[] = [
  /\b((?:mhk|xtk)_)[A-Za-z0-9_\-]{16,}/g,
  /\b(sk-(?:ant-|proj-)?)[A-Za-z0-9_\-]{20,}/g,
  /\b(gh[pousr]_)[A-Za-z0-9]{30,}/g,
  /\b(github_pat_)[A-Za-z0-9_]{22,}/g,
  /\b((?:sk|rk|pk)_(?:live|test)_)[A-Za-z0-9]{16,}/g,
  /\b(whsec_)[A-Za-z0-9+/=]{16,}/g,
  /\b(xox[abprse]-)[A-Za-z0-9\-]{10,}/g,
  /\b(AKIA|ASIA)[A-Z0-9]{16}\b/g,
  /\b(ya29\.)[A-Za-z0-9_\-]{20,}/g,
  /\b(AIza)[A-Za-z0-9_\-]{35}/g,
  /\b(glpat-)[A-Za-z0-9_\-]{20,}/g,
  /\b(npm_)[A-Za-z0-9]{36}/g,
  /\b(hf_)[A-Za-z0-9]{30,}/g,
  /\b(sbp_)[a-f0-9]{40}/g,
  /\b(SG\.)[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}/g,
  /(https:\/\/hooks\.slack\.com\/services\/)[A-Za-z0-9/]+/g,
]
const JWT = /\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}/g
const DSN = /(\b[A-Za-z][A-Za-z0-9+.\-]*:\/\/[^\s:/@'"]+:)(?!<redacted)[^\s@/'"]+(@)/g
// ENV-style name: any value with a letter (the name is the signal).
const ASSIGN = new RegExp(
  '(\\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APP_KEY|AUTH_KEY|PRIVATE_KEY' +
    '|ACCESS_KEY|CREDENTIALS?)[A-Z0-9_]*(?:\\\\?["\'])?\\s*[=:]\\s*(?:\\\\?["\'])?)(?![$<])' +
    '(?![^\\s"\'#,;\\\\]*<redacted)' +
    '(?=[^\\s"\'#,;\\\\]*[A-Za-z])[^\\s"\'#,;\\\\]{8,}',
  'g',
)
// json / yaml / kwarg style name, any case: entropy-gated.
const ASSIGN_ANYCASE = new RegExp(
  '(\\b[a-z0-9_.\\-]*(?:secret|token|passw(?:or)?d|api[_\\-]?key|apikey|app[_\\-]?key|auth[_\\-]?key' +
    '|private[_\\-]?key|access[_\\-]?key|credentials?)(?:\\\\?["\'])?\\s*[=:]\\s*(?:\\\\?["\'])?)' +
    '(?!<redacted)([A-Za-z0-9+/=_\\-.~]{8,})(?=$|[\\s"\'\\\\,;})\\]])',
  'gi',
)
const PEM = new RegExp(
  '(-----BEGIN [A-Z ]*PRIVATE\\s+KEY-----)' +
    '(?:[\\s\\S]{0,12000}?(-----END [A-Z ]*PRIVATE\\s+KEY-----)' +
    '|(?:(?:\\s|\\\\n|\\\\r)+(?:[A-Za-z\\-]+: [^\\n\\\\]*|[A-Za-z0-9+/=]{16,}))+)',
  'g',
)

// ── context-free entropy ────────────────────────────────────────────────────
const B64_TOKEN = /(?<![A-Za-z0-9+/=_\-<])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/=_\-])/g
/** Integrity hashes (`sha512-…` in lockfiles) are public digests, not secrets. */
const DIGEST_PREFIX = /(?:sha(?:1|256|384|512)[-:])$/
const DIGEST_TOKEN = /^sha(?:1|256|384|512)-/
/** Dictionary-ish runs: when they cover most of a token it is an identifier. */
const WORDISH = /[A-Z]?[a-z]{3,}/g
/** Longer runs are encoded payloads, not credentials; PEM keys have their own rule. */
const B64_MAX = 1024
/** A token continuing a path or a data URI. */
const PATH_OR_DATA_BEFORE = /(?:[/.~]|;base64,)$/
const ENTROPY_ANYCASE_MIN = 3.0
/** How far back a prefix test has to look: the longest thing either pattern matches. */
const LOOKBACK = 16

/** Bits per character of `s`'s own character distribution. */
export function shannonEntropy(s: string): number {
  if (!s) return 0
  const counts = new Map<string, number>()
  let n = 0
  for (const ch of s) {
    counts.set(ch, (counts.get(ch) ?? 0) + 1)
    n += 1
  }
  let h = 0
  for (const c of counts.values()) h -= (c / n) * Math.log2(c / n)
  return h
}

/** Entropy a random base62 string of length `n` reliably clears (n=32 → 4.0, n=64 → ~4.5). */
function b64Threshold(n: number): number {
  const expected = Math.log2(62) - 61 / (2 * n * Math.LN2)
  return Math.max(4.0, expected - 0.8)
}

function looksLikeB64Secret(tok: string): boolean {
  const body = tok.replace(/=+$/, '')
  if (!(/[A-Z]/.test(body) && /[a-z]/.test(body) && /[0-9]/.test(body))) return false
  // Paths and slugs: many separators splitting real words.
  if (body.split('/').length - 1 > Math.floor(body.length / 16) + 1) return false
  const wordish = (body.match(WORDISH) ?? []).reduce((n, w) => n + w.length, 0)
  if (wordish >= 0.6 * body.length) return false
  const parts = body.split(/[/_\-]/).filter(Boolean)
  if (parts.length >= 3 && parts.filter(p => /^[A-Za-z]{3,}$/.test(p)).length * 2 >= parts.length) return false
  return shannonEntropy(body) >= b64Threshold(body.length)
}

function subB64(tok: string, offset: number, whole: string): string {
  const before = whole.slice(Math.max(0, offset - LOOKBACK), offset)
  if (tok.length > B64_MAX || DIGEST_TOKEN.test(tok) || DIGEST_PREFIX.test(before) || PATH_OR_DATA_BEFORE.test(before)) return tok
  return looksLikeB64Secret(tok) ? REDACTED : tok
}

function subAnycase(match: string, head: string, val: string): string {
  if (val.startsWith('$') || val.startsWith('<')) return match
  const h = shannonEntropy(val)
  const strong = /[0-9]/.test(val) || h >= 3.5
  return strong && h >= ENTROPY_ANYCASE_MIN ? head + REDACTED : match
}

/** scripts/redact.py `redact_secrets`: high-confidence credentials out, a breadcrumb left. */
export function redactSecrets(text: string): string {
  if (!text) return text
  for (const p of PREFIXED) text = text.replace(p, `$1${REDACTED}`)
  text = text.replace(PEM, (_m: string, begin: string, end: string | undefined) => begin + REDACTED + (end ?? ''))
  text = text.replace(JWT, '<redacted-jwt>')
  text = text.replace(DSN, `$1${REDACTED}$2`)
  text = text.replace(ASSIGN, `$1${REDACTED}`)
  text = text.replace(ASSIGN_ANYCASE, subAnycase)
  return text.replace(B64_TOKEN, (tok: string, offset: number, whole: string) => subB64(tok, offset, whole))
}

// ── a tool's result ─────────────────────────────────────────────────────────

/** The Bash record's text fields (lanes.ts RESULT_KEYS). */
const TEXT_KEYS = ['stderr', 'stdout', 'output', 'error', 'text'] as const

/** What the model is told when values were taken out, so a `<redacted>` in the output is not a puzzle. */
export const REDACTED_NOTE =
  '_MemHub replaced credential-shaped values in this output with `<redacted>` before it reached ' +
  'this session, so they are not on screen, in your context or in the transcript. The command ' +
  'itself ran as written. If a later step needs such a value, pass it by its variable or file ' +
  'without printing it._'

type ResultLike = {
  deny?: string
  result?: unknown
  context?: readonly string[]
  ref?: number
  text?: string
  isError?: true
  [k: string]: unknown
}

/**
 * What a Bash `tool.call` resolves to, with credentials out of its text.
 * The very object when nothing was found (core then uses its own messages,
 * `ref`). Otherwise a hook's own answer: the record with its text fields
 * redacted, without `ref` and `text`, so core maps and records the redacted
 * record. A call that ERRORED has no record to answer with (its `result` is
 * the stored error text), so its redacted text rides a `deny`: the model reads
 * that the command ran and what it printed, less the values.
 */
export function redactToolResult<R extends ResultLike>(r: R): R | ResultLike {
  if (!r || r.deny !== undefined) return r
  if (r.isError) {
    const text = typeof r.text === 'string' ? r.text : typeof r.result === 'string' ? r.result : ''
    const clean = redactSecrets(text)
    return clean === text ? r : { deny: `${clean}\n${REDACTED_NOTE}` }
  }
  const rec = r.result
  let out: unknown = rec
  if (typeof rec === 'string') {
    out = redactSecrets(rec)
  } else if (rec && typeof rec === 'object' && !Array.isArray(rec)) {
    const src = rec as Record<string, unknown>
    let copy: Record<string, unknown> | undefined
    for (const k of TEXT_KEYS) {
      const v = src[k]
      if (typeof v !== 'string') continue
      const clean = redactSecrets(v)
      if (clean !== v) (copy ??= { ...src })[k] = clean
    }
    if (copy) out = copy
  }
  if (out === rec) return r
  const { ref: _ref, text: _text, ...rest } = r
  return { ...rest, result: out, context: [...(r.context ?? []), REDACTED_NOTE] }
}

/** `MEMHUB_SESSION_REDACT` set to an off spelling keeps a session's tool output as the tool gave it. */
export const isOff = (value: string | undefined | null) => ['0', 'off', 'false', 'no'].includes((value ?? '').trim().toLowerCase())
