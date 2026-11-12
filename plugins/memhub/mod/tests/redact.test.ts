// Credentials out of a Bash result before the session holds it (redact.ts):
// the rules answer as scripts/redact.py does, and the hook's answer is the
// tool's own record with its text fields redacted.

import { describe, expect, test } from 'claude-code/testing'

import { isOff, REDACTED_NOTE, redactSecrets, redactToolResult, shannonEntropy } from '../redact'
// Made up, and spelled in pieces so the release export's secret scan reads no key here.
const KEY = ['sk-ant-', 'api03-', 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'].join('')
const BARE = ['Zk9xQ2pL7vN3mR8t', 'Y1wB5cD0eF6gH4iJ'].join('')

// Rule-for-rule agreement with scripts/redact.py is the generated vectors'
// job (engine/tests/shell.test.ts `redact_text`, whose TS side is this
// module's `redactSecrets`); these are the shapes, read as a person would.
describe('the rules', () => {
  test('a vendor key keeps its prefix; a bare high-entropy token goes whole', () => {
    expect(redactSecrets(`use ${KEY} here`)).toBe('use sk-ant-<redacted> here')
    expect(redactSecrets(`the key is ${BARE}`)).toBe('the key is <redacted>')
    expect(redactSecrets(['AUTH_JWT_', 'SECRET=correct-horse-battery'].join(''))).toBe('AUTH_JWT_SECRET=<redacted>')
    expect(redactSecrets('postgres://app:hunter2secret@db.internal:5432/prod')).toBe('postgres://app:<redacted>@db.internal:5432/prod')
  })

  test('ordinary output is left alone', () => {
    for (const t of ['12 passed in 3.4s', 'token = self.token', 'commit 6285d40179074934f43deba53713a7d286cb3d5f',
      'MemHubBackendCaptureRedactionModule2026', 'ask dana@example.com', '']) {
      expect(redactSecrets(t)).toBe(t)
    }
  })

  test('are idempotent', () => {
    const once = redactSecrets(`a ${KEY} b ${BARE}`)
    expect(redactSecrets(once)).toBe(once)
  })

  test('Shannon entropy is bits per character of the string itself', () => {
    expect(shannonEntropy('')).toBe(0)
    expect(shannonEntropy('aaaaaaaa')).toBe(0)
    expect(shannonEntropy('abababab')).toBe(1)
    expect(shannonEntropy(BARE)).toBeGreaterThanOrEqual(4)
  })
})

describe('a Bash result', () => {
  test('with nothing to redact is the very object core gave, ref and all', () => {
    const r = { ref: 7, result: { stdout: '12 passed', stderr: '', interrupted: false }, text: '12 passed' }
    expect(redactToolResult(r)).toBe(r)
  })

  test('a printed credential is replaced in the record; ref and text go, so core maps the redacted record', () => {
    const r = {
      ref: 7,
      result: { stdout: `ANTHROPIC_KEY is ${KEY}\nok`, stderr: ['warn: AUTH_JWT_', 'SECRET=correct-horse-battery'].join(''), interrupted: false },
      text: `ANTHROPIC_KEY is ${KEY}\nok`,
      isReadOnly: true as const,
      context: ['rule advice'],
    }
    const out = redactToolResult(r) as Record<string, unknown>
    expect(out.result).toEqual({ stdout: 'ANTHROPIC_KEY is sk-ant-<redacted>\nok', stderr: 'warn: AUTH_JWT_SECRET=<redacted>', interrupted: false })
    expect('ref' in out).toBe(false)
    expect('text' in out).toBe(false)
    expect(out.isReadOnly).toBe(true)
    expect(out.context).toEqual(['rule advice', REDACTED_NOTE]) // the lanes' advice stays, the note follows
    expect(JSON.stringify(out)).not.toContain(KEY)
    expect(r.result.stdout).toContain(KEY) // the input is not mutated
  })

  test('a commit SHA and an address are left for the agent to work with', () => {
    const r = { ref: 1, result: { stdout: 'commit 6285d40179074934f43deba53713a7d286cb3d5f by dana@example.com', stderr: '' } }
    expect(redactToolResult(r)).toBe(r)
  })

  test('a command that ERRORED: its redacted text rides a deny, since there is no record to answer with', () => {
    const r = { ref: 3, isError: true as const, result: `exit 1\n${KEY}`, text: `Exit code 1\n${KEY}` }
    const out = redactToolResult(r) as { deny: string }
    expect(out.deny).toBe(`Exit code 1\nsk-ant-<redacted>\n${REDACTED_NOTE}`)
    const clean = { ref: 4, isError: true as const, result: 'exit 1', text: 'Exit code 1\nno such file' }
    expect(redactToolResult(clean)).toBe(clean)
  })

  test('a deny from an inner hook, and a result of another shape, pass through', () => {
    const deny = { deny: 'Blocked by the XTrace team rulebook' }
    expect(redactToolResult(deny)).toBe(deny)
    const odd = { ref: 2, result: [1, 2, 3] }
    expect(redactToolResult(odd)).toBe(odd)
    const text = redactToolResult({ ref: 5, result: `plain ${KEY}` }) as Record<string, unknown>
    expect(text.result).toBe('plain sk-ant-<redacted>')
  })
})

test('MEMHUB_SESSION_REDACT: only an off spelling turns it off', () => {
  for (const v of ['0', 'off', 'OFF', ' false ', 'no']) expect(isOff(v)).toBe(true)
  for (const v of [undefined, null, '', '1', 'on', 'yes']) expect(isOff(v)).toBe(false)
})
