// The drafter lane (mod/drafter.ts): the mod spawns the rule-drafting fork the
// Stop queued, acks it, and on a refused spawn gives the lane back so the Stop
// prints its visible line. Driven with a fake Io and a fake spawn.

import { describe, expect, test } from 'claude-code/testing'

import { Claims, StatusLine } from '../claims'
import { Drafter, type Spawned, type TurnEnd } from '../drafter'
import { defaultRun, fakeIo, ok, ROOT, SESSION, settle } from './fakes'

const REF = `${SESSION}#4`
const PROMPT = 'MemHub harness fork, moment sess-1#4: …'

function setup(opts: { entries?: unknown[]; spawn?: (n: number) => Spawned | Promise<Spawned>; queuedExit?: number } = {}) {
  const io = fakeIo({
    onRun: (argv, stdin) => {
      if (argv[1] === `${ROOT}/scripts/harness_stop.py` && argv[2] === 'queued') {
        expect(JSON.parse(stdin ?? '{}')).toEqual({ session_id: SESSION })
        return { exitCode: opts.queuedExit ?? 0, stdout: JSON.stringify({ entries: opts.entries ?? [{ ref: REF, prompt: PROMPT }] }), stderr: '' }
      }
      if (argv[2] === 'ack') return ok('{"acked":true}')
      return defaultRun(argv)
    },
  })
  const claims = new Claims(io, 'staging', new StatusLine(io), () => 1_700_000_000_000)
  const spawns: { prompt: string; description: string }[] = []
  const drafter = new Drafter(io, ROOT, claims, async input => {
    spawns.push(input)
    return opts.spawn ? opts.spawn(spawns.length) : { agentId: `fork-${spawns.length}` }
  })
  return { io, claims, spawns, drafter }
}

const mainTurn: TurnEnd = { isAborted: false, reason: 'answer', answer: 'done' }
const forkTurn = (agentId: string, answer: string): TurnEnd => ({ agentId, isAborted: false, reason: 'answer', answer })
const subs = (io: ReturnType<typeof fakeIo>) => io.runs.map(r => r.argv.slice(2).join(' '))

describe('claim', () => {
  test('an interactive session claims drafter; the variable lists it', async () => {
    const { io, claims, drafter } = setup()
    await drafter.start({ isInteractive: true })
    expect(claims.has('drafter')).toBe(true)
    expect(io.envVars.staging).toMatch(/:drafter$/)
  })

  test('a one-shot session claims nothing, so its Stop keeps continuing the agent', async () => {
    const { claims, drafter } = setup()
    await drafter.start({ isInteractive: false })
    expect(claims.has('drafter')).toBe(false)
  })

  test("a Rulebook release (book failure) leaves the drafter claimed; only ALL_LANES gives it back", async () => {
    const { claims, drafter } = setup()
    await drafter.start({ isInteractive: true })
    await claims.release()
    expect(claims.has('drafter')).toBe(true)
  })
})

describe('turn end', () => {
  test('a queued moment is spawned as a fork with its prompt verbatim, then acked', async () => {
    const { io, spawns, drafter } = setup()
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    expect(spawns).toEqual([{ prompt: PROMPT, description: 'MemHub: draft a team rule' }])
    expect(subs(io)).toContain(`ack --moment ${REF}`)
    expect(io.logs).toEqual([])
  })

  test('nothing queued: nothing spawned', async () => {
    const { spawns, drafter } = setup({ entries: [] })
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    expect(spawns).toEqual([])
  })

  test('a malformed queue answer spawns nothing', async () => {
    const { spawns, drafter } = setup({ entries: [{ ref: REF }, 'x', null], queuedExit: 0 })
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    expect(spawns).toEqual([])
  })

  test('without the lane the queue is not read', async () => {
    const { io, spawns, drafter } = setup()
    await drafter.turnEnded(mainTurn)
    expect(spawns).toEqual([])
    expect(subs(io).some(s => s.startsWith('queued'))).toBe(false)
  })

  test('a refused spawn releases the lane and acks nothing: the Stop prints the line next turn', async () => {
    const { io, claims, spawns, drafter } = setup({ spawn: () => ({ deny: 'auto mode said no' }) })
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    expect(spawns.length).toBe(1)
    expect(claims.has('drafter')).toBe(false)
    expect(subs(io).some(s => s.startsWith('ack'))).toBe(false)
    expect(io.debugs.some(d => d.includes('not started (auto mode said no)'))).toBe(true)
    // and stays released: a later turn reads no queue
    io.runs.length = 0
    await drafter.turnEnded(mainTurn)
    expect(subs(io).some(s => s.startsWith('queued'))).toBe(false)
  })

  test('a spawn that throws is a refusal too', async () => {
    const { claims, drafter } = setup({ spawn: () => Promise.reject(new Error('no session is bound')) })
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    expect(claims.has('drafter')).toBe(false)
  })

  test('the turn is not held: turnEnded returns a promise the hook does not await', async () => {
    const { drafter } = setup()
    await drafter.start({ isInteractive: true })
    const p = drafter.turnEnded(mainTurn)
    expect(p).toBeInstanceOf(Promise)
    await p
  })
})

describe('the fork reports', () => {
  test('filed: one line to the person', async () => {
    const { io, drafter } = setup()
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    await drafter.turnEnded(forkTurn('fork-1', 'work…\nMemHub harness fork: filed Check X before Y'))
    expect(io.logs).toEqual(['MemHub harness fork: filed Check X before Y'])
  })

  test('none and failed: nothing on screen', async () => {
    const { io, drafter } = setup({ entries: [{ ref: REF, prompt: PROMPT }, { ref: `${SESSION}#5`, prompt: PROMPT }] })
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    await drafter.turnEnded(forkTurn('fork-1', 'MemHub harness fork: none, a design question'))
    await drafter.turnEnded(forkTurn('fork-2', 'MemHub harness fork: failed, no stamp'))
    expect(io.logs).toEqual([])
    expect(io.debugs.some(d => d.includes('none, a design question'))).toBe(true)
  })

  test("another agent's turn, or the fork's own mid-way turn, is not a report", async () => {
    const { io, drafter } = setup()
    await drafter.start({ isInteractive: true })
    await drafter.turnEnded(mainTurn)
    await drafter.turnEnded(forkTurn('someone-else', 'MemHub harness fork: filed Not ours'))
    await drafter.turnEnded(forkTurn('fork-1', 'still working'))
    await drafter.turnEnded(forkTurn('fork-1', 'MemHub harness fork: filed Ours'))
    await settle()
    expect(io.logs).toEqual(['MemHub harness fork: filed Ours'])
  })
})
