// Lane claims (spec §3.3): claim late, release on failure, never serve a lane
// twice. Driven through the shell's own boot with a fake Io and Engine.

import { describe, expect, test } from 'claude-code/testing'

import { Claims, FELL_BACK, LEASE_LATE_MS, LEASE_S, RENEW_MS, StatusLine, stampOf } from '../claims'
import { boot } from '../register'
import { BOOK, EMPTY, fakeCtx, fakeEngine, fakeIo, lanesOf, ok, SESSION, toHook } from './fakes'

/** A claim value without its lease and pid: `<sid>:<lanes>`, for comparing. */
const bare = (v: string | undefined) => v?.replace(/^(.*):(\d+):(\d*):([^:]*)$/, '$1:$4')
/** The parts of a claim value. */
function partsOf(v: string | undefined) {
  const m = v === undefined ? null : /^(.*):(\d+):(\d*):([^:]*)$/.exec(v)
  return m ? { sid: m[1]!, expires: Number(m[2]), pid: m[3]!, lanes: m[4]! } : undefined
}

const BOOK_TEXT = JSON.stringify({ etag: '"v1"', fetched_at: '2026-10-06T00:00:00.000000+00:00', rules: [{ id: 'r1' }] })

async function started(opts: { withBook?: boolean; pastStartup?: boolean; inherited?: string } = {}) {
  const io = fakeIo()
  if (opts.withBook !== false) io.files.set(BOOK, BOOK_TEXT)
  if (opts.inherited) io.envVars.staging = opts.inherited
  const engine = fakeEngine()
  const shell = await boot(io, fakeCtx(io), () => engine, { cwd: '/work', pastStartup: opts.pastStartup ?? false, toHook })
  return { io, engine, shell }
}

describe('claim late', () => {
  test('nothing is claimed until the book is held; then pre, post and prompt', async () => {
    let release!: () => void
    const gate = new Promise<void>(r => (release = r))
    const io = fakeIo({
      onRun: async argv => {
        if (argv[2] === 'paths') await gate
        return argv[2] === 'paths' ? ok(JSON.stringify({ book: BOOK, repo: 'r' })) : ok('')
      },
    })
    io.files.set(BOOK, BOOK_TEXT)
    const engine = fakeEngine()
    const shell = await boot(io, fakeCtx(io), () => engine, { cwd: '/work', pastStartup: false, toHook })
    // the book is still being located: no claim, nothing in the variable
    expect(io.envVars.staging).toBeUndefined()
    expect(shell.claims.has('pre')).toBe(false)
    release()
    await shell.ready
    expect(engine.books).toHaveLength(1)
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:pre,post,prompt`)
    expect(lanesOf(io)).toEqual(['pre', 'post', 'prompt'])
  })

  test('the session lane waits for the first prompt in a fresh process (Python served startup)', async () => {
    const { io, shell } = await started()
    await shell.ready
    expect(shell.claims.has('session')).toBe(false)
    await shell.firstPrompt()
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:pre,post,prompt,session`)
  })

  test('after a hot reload every lane is claimed at once', async () => {
    const { io, shell } = await started({ pastStartup: true })
    await shell.ready
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:pre,post,prompt,session`)
  })

  test('no book (none cached, none fetched): nothing claimed, no fallback notice', async () => {
    const { io, shell } = await started({ withBook: false })
    await shell.ready
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.filter(Boolean)).toEqual([])
  })

  test('an inherited variable (a claude started from Bash) is cleared before anything', async () => {
    const io = fakeIo({ onRun: () => ok('{}') }) // paths answers nothing: no book
    io.envVars.staging = 'parent-session:pre,post,prompt,session'
    const shell = await boot(io, fakeCtx(io), () => fakeEngine(), { cwd: '/work', pastStartup: false, toHook })
    await shell.ready
    expect(io.envWrites[0]).toEqual({ env: 'staging', value: undefined })
    expect(io.envVars.staging).toBeUndefined()
  })

  test('the prod install writes only its own variable', async () => {
    const io = fakeIo({ pluginName: 'memhub' })
    io.files.set(BOOK, BOOK_TEXT)
    const shell = await boot(io, fakeCtx(io, 'prod'), () => fakeEngine(), { cwd: '/work', pastStartup: true, toHook })
    await shell.ready
    expect(bare(io.envVars.prod)).toBe(`${SESSION}:pre,post,prompt,session`)
    expect(io.envVars.staging).toBeUndefined()
  })
})

describe('release on failure', () => {
  test('a failure at load releases every lane and says so', async () => {
    const io = fakeIo()
    io.files.set(BOOK, BOOK_TEXT)
    const throwing = () => {
      throw new Error('bad row')
    }
    const shell = await boot(io, fakeCtx(io), () => fakeEngine(), { cwd: '/work', pastStartup: true, toHook: throwing })
    await shell.ready
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(FELL_BACK)
  })

  test('no engine yet: nothing claimed, the fallback said', async () => {
    const io = fakeIo()
    io.files.set(BOOK, BOOK_TEXT)
    const shell = await boot(io, fakeCtx(io), undefined, { cwd: '/work', pastStartup: true })
    await shell.ready
    expect(shell.claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
    expect(io.statuses.at(-1)).toBe(FELL_BACK)
  })

  test('the first pre failure hands that call to Python and takes the lane back; the second keeps it out', async () => {
    const { io, engine, shell } = await started({ pastStartup: true })
    await shell.ready
    engine.verdicts.pre = new Error('engine blew up')
    // what the variable said while the call went beneath (where Python's PreToolUse runs)
    const seenBeneath: (string | undefined)[] = []
    const next = async () => {
      seenBeneath.push(io.envVars.staging)
      return { result: 'ok', text: 'ok' }
    }

    const r1 = await shell.lanes.toolCall({ tool: 'Bash', command: 'ls' }, next)
    expect(r1).toEqual({ result: 'ok', text: 'ok' })
    expect(bare(seenBeneath[0])).toBe(`${SESSION}:post,prompt,session`) // Python served this call's pre lane
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:pre,post,prompt,session`) // and the mod took it back
    expect(io.statuses.filter(s => s === FELL_BACK)).toEqual([])

    await shell.lanes.toolCall({ tool: 'Bash', command: 'ls' }, next)
    expect(bare(seenBeneath[1])).toBe(`${SESSION}:post,prompt,session`)
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:post,prompt,session`) // out for the session
    expect(io.statuses.at(-1)).toBe(FELL_BACK)
    expect(lanesOf(io)).toEqual(['post', 'prompt', 'session'])

    // released: the mod no longer evaluates pre, even once the engine recovers
    engine.verdicts.pre = EMPTY
    const before = engine.calls.pre.length
    await shell.lanes.toolCall({ tool: 'Bash', command: 'ls' }, next)
    expect(engine.calls.pre.length).toBe(before)
  })

  test('a released lane is never claimed again in the session', async () => {
    const io = fakeIo()
    const claims = new Claims(io, 'staging', new StatusLine(io))
    await claims.claim(['pre', 'post'])
    await claims.fail('pre')
    await claims.fail('pre')
    await claims.claim(['pre', 'post'])
    expect(claims.list()).toEqual(['post'])
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:post`)
  })
})

describe('a lane not claimed is not evaluated', () => {
  test('before the claim, a tool call and a prompt reach Python untouched', async () => {
    const io = fakeIo({ onRun: () => ok('{}') })
    const engine = fakeEngine()
    const shell = await boot(io, fakeCtx(io), () => engine, { cwd: '/work', pastStartup: true, toHook })
    await shell.ready
    const e = { tool: 'Bash', command: 'git push' }
    let passed: unknown
    const r = await shell.lanes.toolCall(e, async x => {
      passed = x
      return { result: 1, text: 't' }
    })
    expect(passed).toBe(e)
    expect(r).toEqual({ result: 1, text: 't' })
    const p = { text: 'hello' }
    let prompt: unknown
    await shell.lanes.promptSubmit(p, async x => {
      prompt = x
      return x
    })
    expect(prompt).toBe(p)
    expect(engine.calls.pre).toHaveLength(0)
    expect(engine.calls.post).toHaveLength(0)
    expect(engine.calls.prompt).toHaveLength(0)
    expect(io.appends).toHaveLength(0)
  })

  test('pre claimed, post released: only pre runs', async () => {
    const { engine, shell } = await started({ pastStartup: true })
    await shell.ready
    await shell.claims.release(['post'])
    await shell.lanes.toolCall({ tool: 'Read', file_path: '/x' }, async () => ({ result: 1, text: 'x' }))
    expect(engine.calls.pre).toHaveLength(1)
    expect(engine.calls.post).toHaveLength(0)
  })
})

describe('the claim is stamped with its session (a child process never inherits it)', () => {
  test('the value is <session_id>:<lanes>', async () => {
    const { io, shell } = await started({ pastStartup: true })
    await shell.ready
    expect(bare(io.envVars.staging)).toBe('sess-1:pre,post,prompt,session')
    // the $.state mirror stays the bare list (the companion reads it)
    expect(lanesOf(io)).toEqual(['pre', 'post', 'prompt', 'session'])
  })

  test('released to nothing: the variable is unset, never a bare stamp', async () => {
    const { io, shell } = await started({ pastStartup: true })
    await shell.ready
    await shell.claims.release()
    expect(io.envVars.staging).toBeUndefined()
  })

  test('a new session id in-process (/clear, resume): re-stamped at the next prompt', async () => {
    const { io, engine, shell } = await started({ pastStartup: true })
    await shell.ready
    const preambles = io.appends.length
    io.session = 'sess-2'
    // between the change and the prompt the stamp is the old id: Python serves sess-2
    expect(bare(io.envVars.staging)).toBe('sess-1:pre,post,prompt,session')
    await shell.sessionChanged()
    expect(bare(io.envVars.staging)).toBe('sess-2:pre,post,prompt,session')
    // Python's SessionStart served sess-2's preamble (the ids differed): the mod does not add a second
    await shell.lanes.promptSubmit({ text: 'hi' }, async x => x)
    expect(io.appends.length).toBe(preambles)
    expect(engine.calls.session).toContain('sess-2')
    // the same id again: nothing is written
    const writes = io.envWrites.length
    await shell.sessionChanged()
    expect(io.envWrites.length).toBe(writes)
  })

  test('a new id with nothing claimed writes nothing, and a later claim carries the new id', async () => {
    const io = fakeIo()
    const claims = new Claims(io, 'staging', new StatusLine(io))
    await claims.claim(['pre'])
    await claims.fail('pre') // paused: nothing claimed
    expect(io.envVars.staging).toBeUndefined()
    io.session = 'sess-2'
    const writes = io.envWrites.length
    expect(await claims.restamp()).toBe(true)
    expect(io.envWrites.length).toBe(writes)
    await claims.recover('pre')
    expect(bare(io.envVars.staging)).toBe('sess-2:pre')
  })

  test('no session id to stamp with: nothing is claimed and Python serves', async () => {
    const io = fakeIo()
    io.sessionId = async () => ''
    const claims = new Claims(io, 'staging', new StatusLine(io))
    let threw = false
    await claims.claim(['pre', 'post']).catch(() => (threw = true))
    expect(threw).toBe(true)
    expect(claims.list()).toEqual([])
    expect(io.envVars.staging).toBeUndefined()
  })
})

describe('the claim is a lease: it lapses unless the live module renews it', () => {
  const T0 = 1_791_331_434_525
  function leased(pid = '4242') {
    let now = T0
    const io = fakeIo({
      onRun: argv => (argv[0] === '/bin/sh' ? ok(`${pid}\n`) : ok('')),
    })
    const claims = new Claims(io, 'staging', new StatusLine(io), () => now)
    return { io, claims, advance: (ms: number) => void (now += ms), now: () => now }
  }

  test('the value carries the session, the expiry (now + LEASE_S) and this process\'s pid', async () => {
    const { io, claims } = leased()
    await claims.claim(['pre', 'post'])
    expect(io.envVars.staging).toBe(stampOf(SESSION, Math.floor(T0 / 1000) + LEASE_S, '4242', ['pre', 'post']))
    expect(partsOf(io.envVars.staging)).toEqual({ sid: SESSION, expires: Math.floor(T0 / 1000) + 90, pid: '4242', lanes: 'pre,post' })
    // the pid is found once: the parent of a shell the mod starts
    expect(io.runs.filter(r => r.argv[0] === '/bin/sh').map(r => r.argv)).toEqual([['/bin/sh', '-c', 'echo $PPID']])
  })

  test('no pid to be had: the field is empty (Python then matches on the session)', async () => {
    const { io, claims } = leased('not-a-pid')
    await claims.claim(['pre'])
    expect(partsOf(io.envVars.staging)!.pid).toBe('')
  })

  test('a renewal tick pushes the expiry out every RENEW_MS', async () => {
    const { io, claims, advance } = leased()
    await claims.claim(['pre', 'post', 'prompt'])
    const renewals = io.timers.filter(t => t.ms === RENEW_MS)
    expect(renewals).toHaveLength(1)
    const first = partsOf(io.envVars.staging)!.expires
    advance(RENEW_MS)
    await io.tick()
    expect(partsOf(io.envVars.staging)!.expires).toBe(first + RENEW_MS / 1000)
    expect(partsOf(io.envVars.staging)!.lanes).toBe('pre,post,prompt')
    // still served well past the first lease, because it was renewed
    advance(LEASE_S * 1000)
    await io.tick()
    expect(claims.has('pre')).toBe(true)
    // one timer, however many writes
    expect(io.timers.filter(t => t.ms === RENEW_MS)).toHaveLength(1)
  })

  test('a renewal never brings back a released or paused lane', async () => {
    const { io, claims, advance } = leased()
    await claims.claim(['pre', 'post', 'prompt'])
    await claims.release(['post'])
    await claims.fail('prompt') // paused for one call
    advance(RENEW_MS)
    await io.tick()
    expect(partsOf(io.envVars.staging)!.lanes).toBe('pre')
    await claims.release(['pre'])
    const writes = io.envWrites.length
    advance(RENEW_MS)
    await io.tick()
    expect(io.envWrites.length).toBe(writes) // nothing claimed: the tick writes nothing
    expect(io.envVars.staging).toBeUndefined()
  })

  test('no renewal (the module stopped): the mod stops serving shortly after Python takes over', async () => {
    const { io, claims, advance } = leased()
    await claims.claim(['pre'])
    const expiresMs = partsOf(io.envVars.staging)!.expires * 1000
    advance(expiresMs - T0 - 1) // just before expiry: Python still skips, the mod serves
    expect(claims.has('pre')).toBe(true)
    advance(1 + LEASE_LATE_MS - 1) // lapsed for Python; the mod still serves (an overlap, never a gap)
    expect(claims.has('pre')).toBe(true)
    advance(1)
    expect(claims.has('pre')).toBe(false)
  })

  test('nothing is served before the first lease is written', async () => {
    const { claims } = leased()
    expect(claims.has('pre')).toBe(false)
  })
})

describe('a claims write that fails never leaves a lane claimed and unevaluated', () => {
  test('fail(): the write rejects, the module serves nothing, the variable is unset, and the call still runs', async () => {
    const { io, engine, shell } = await started({ pastStartup: true })
    await shell.ready
    engine.verdicts.pre = new Error('engine blew up')
    const realSet = io.setLanesVar
    let refuse = 1
    io.setLanesVar = async (env, value) => {
      // the write that drops `pre` from the variable is refused once
      if (value !== undefined && refuse-- > 0) throw new Error('env.set refused')
      return realSet(env, value)
    }
    const seenBeneath: (string | undefined)[] = []
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'git push' }, async () => {
      seenBeneath.push(io.envVars.staging)
      return { result: 'ok', text: 'ok' }
    })
    expect(r).toEqual({ result: 'ok', text: 'ok' })
    // beneath, Python saw NO claim: it served pre itself (the variable no longer listed it)
    expect(seenBeneath).toEqual([undefined])
    // and once the call settled the lane came back
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:pre,post,prompt,session`)
    expect(shell.claims.has('pre')).toBe(true)
  })

  test('the unset fails too: the module still serves nothing, and the variable keeps only its own lease', async () => {
    const io = fakeIo()
    let now = 1_700_000_000_000
    const claims = new Claims(io, 'staging', new StatusLine(io), () => now)
    await claims.claim(['pre', 'post'])
    const before = io.envVars.staging
    io.setLanesVar = async () => {
      throw new Error('env.set refused')
    }
    await expect(claims.fail('pre')).rejects.toThrow('env.set refused')
    expect(io.envVars.staging).toBe(before) // nothing could be written
    expect(claims.has('pre')).toBe(false)
    expect(claims.has('post')).toBe(false) // no live lease: nothing served here
    // the stale value lapses on its own lease
    expect(partsOf(before)!.expires).toBe(Math.floor(now / 1000) + LEASE_S)
  })

  test('release(): a rejected write unsets the variable rather than leave the lanes listed', async () => {
    const io = fakeIo()
    const claims = new Claims(io, 'staging', new StatusLine(io))
    await claims.claim(['pre', 'post'])
    const realSet = io.setLanesVar
    io.setLanesVar = async (env, value) => {
      if (value !== undefined) throw new Error('env.set refused')
      return realSet(env, value)
    }
    await expect(claims.release(['pre'])).rejects.toThrow('env.set refused')
    expect(io.envVars.staging).toBeUndefined()
    expect(lanesOf(io)).toEqual([])
    expect(claims.has('post')).toBe(false)
    // the next renewal writes the claim afresh, without the released lane
    io.setLanesVar = realSet
    await io.tick()
    expect(bare(io.envVars.staging)).toBe(`${SESSION}:post`)
    expect(claims.has('post')).toBe(true)
    expect(claims.has('pre')).toBe(false)
  })
})
