// The §4b forward-test hand-off (mod/redirect.ts): when the shell hands a call
// to the Python hook because a live claim covers it (ENG-1250).
import { test, expect } from 'claude-code/testing'

import { REDIRECT_NAME, forwardTestCovers } from '../../redirect'
import { REDIRECT_MAX_AGE_S } from '../shell'

const BASE = '/home/u/.config/memhub-plugin/rulebook/api.example'
const CLAIM = `${BASE}/${REDIRECT_NAME}`
const REPO = '/work/app'
const PREFIX = `${REPO}/.claude/worktrees`
const NOW = 1_790_000_000_000

function io(opts: { text?: string; mtimeMs?: number; real?: Record<string, string>; now?: number } = {}) {
  const asked: string[] = []
  return {
    asked,
    readText: async (p: string) => (p === CLAIM ? opts.text : undefined),
    stat: async (p: string, resolve?: boolean) => {
      asked.push(`${resolve ? 'real ' : ''}${p}`)
      if (p === CLAIM) return opts.text === undefined ? undefined : { mtimeMs: opts.mtimeMs ?? NOW - 1000 }
      const real = opts.real?.[p]
      return real === undefined ? undefined : { mtimeMs: NOW, realPath: real }
    },
    now: async () => opts.now ?? NOW,
  }
}

const claim = (prefix: unknown = PREFIX) => JSON.stringify({ base: '/tmp/x/pretest-base', cwd_prefix: prefix, pid: 1 })

test('no claim file: nothing is handed off, sub-agent or not', async () => {
  const f = io()
  expect(await forwardTestCovers(f, BASE, `${PREFIX}/agent-1`, undefined)).toBe(false)
  expect(await forwardTestCovers(f, BASE, REPO, 'agent-1')).toBe(false)
  expect(await forwardTestCovers(f, undefined, `${PREFIX}/agent-1`, 'agent-1')).toBe(false)
})

test('a session cwd inside the claimed prefix is handed off; the checkout above it is not', async () => {
  const f = io({ text: claim() })
  expect(await forwardTestCovers(f, BASE, `${PREFIX}/agent-1`, undefined)).toBe(true)
  expect(await forwardTestCovers(f, BASE, PREFIX, undefined)).toBe(true)
  expect(await forwardTestCovers(f, BASE, REPO, undefined)).toBe(false)
  expect(await forwardTestCovers(f, BASE, `${REPO}/.claude/worktrees-other/x`, undefined)).toBe(false)
})

test("a sub-agent's call is handed off while a claim is live, whatever the session cwd says", async () => {
  const f = io({ text: claim() })
  expect(await forwardTestCovers(f, BASE, REPO, 'agent-1')).toBe(true)
})

test('the real paths must agree when both resolve: a symlink cannot pull a session in', async () => {
  const f = io({ text: claim(), real: { [`${PREFIX}/agent-1`]: '/elsewhere/agent-1', [PREFIX]: '/real/worktrees' } })
  expect(await forwardTestCovers(f, BASE, `${PREFIX}/agent-1`, undefined)).toBe(false)
  const g = io({ text: claim(), real: { [`${PREFIX}/agent-1`]: '/real/worktrees/agent-1', [PREFIX]: '/real/worktrees' } })
  expect(await forwardTestCovers(g, BASE, `${PREFIX}/agent-1`, undefined)).toBe(true)
})

test('a stale claim (older than REDIRECT_MAX_AGE_S) steers nothing', async () => {
  const f = io({ text: claim(), mtimeMs: NOW - (REDIRECT_MAX_AGE_S + 5) * 1000 })
  expect(await forwardTestCovers(f, BASE, `${PREFIX}/agent-1`, 'agent-1')).toBe(false)
})

test('a malformed claim, or one naming no prefix, steers nothing', async () => {
  expect(await forwardTestCovers(io({ text: '{not json' }), BASE, `${PREFIX}/a`, 'agent-1')).toBe(false)
  expect(await forwardTestCovers(io({ text: '[1]' }), BASE, `${PREFIX}/a`, 'agent-1')).toBe(false)
  expect(await forwardTestCovers(io({ text: claim('') }), BASE, `${PREFIX}/a`, 'agent-1')).toBe(false)
  expect(await forwardTestCovers(io({ text: claim(7) }), BASE, `${PREFIX}/a`, 'agent-1')).toBe(false)
})
