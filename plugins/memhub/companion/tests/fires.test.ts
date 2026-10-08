// A fire, as the companion hears of it (spec §4.6 point 4, §4.11): the mod's
// act side writes a FireNote to `$.state` fires, and the band, which read that
// value while drawing, is drawn again and says it — once, crossly when the
// rule blocked the call. The classic PreToolUse / PostToolUse answer is read
// only for a lane the mod has not claimed (`$.state` lanes), so a fire both
// paths see is said once.
//
// The test's `on` stands for the engine and the rest of the mod: it holds
// `$.state` (beneath.ts), answers the classic PostToolUse with the Python
// hook's disclosure line, and takes the blits.

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

import type { FireNote } from '../../types'
import { ran, restBeneath, stateBeneath, textOf } from './beneath'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-fires-1'
// short enough that `Rule fired: <rule>` sits on one line of the bubble
const RULE = 'Keep tests fast'
const LINE = `📏 Rule fired: ${RULE}`

const BAND = {
  hasSurvey: false,
  isWorking: false,
  maxRows: 24,
  bodyColumns: 120,
  scroll: { offset: 0, bodyRows: 24 },
  view: {},
}

const STEP_MS = 250
const PLAY_MS = 30_000

const note = (over: Partial<FireNote> = {}): FireNote => ({
  ruleId: 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', line: LINE, rule: RULE, isBlocked: false, at: 1_000, ...over,
})

type Beneath = { state?: Record<string, unknown>; context?: string[] }

async function started($: Engine, on: On, beneath: Beneath = {}) {
  const clock = mock.clock(on)
  const store: Record<string, unknown> = { 'companion.pin': 'goose' }
  on('store.get', async (_, e) => ({ value: store[e.key] }))
  on('store.set', async (_, e) => {
    store[e.key] = JSON.parse(JSON.stringify(e.value))
    return { value: undefined }
  })
  on('store.delete', async (_, e) => {
    delete store[e.key]
    return { value: undefined }
  })
  on('store.keys', async () => ({ value: Object.keys(store) }))
  mock.env(on, { HOME: '/home/tester' })
  on('session.id', async () => ({ value: SESSION }))
  on('session.start', async (_, e) => ({ cwd: e.cwd }))
  on('command.register', async (_, e) => ({ value: { command: e.name } }))
  on('classic.PostToolUse', async () => ({ additionalContext: beneath.context ?? [] }))
  const state = stateBeneath(on, beneath.state)
  const rest = restBeneath(on)
  on('process.run', async (_, e) => rest.apiInfo(e.argv) ?? ran(1))
  const frames: string[] = []
  let columns = 0
  on('ui.blit', async (_, e) => {
    if ('cells' in e) frames.push(textOf(e.cells, columns))
    return { value: {} }
  })
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: BAND })
  const raster = await band.find({ type: 'Raster', key: 'companion' })
  expect(raster, 'the band draws the animal as one Raster').toBeDefined()
  columns = raster!.props.columns as number
  /** Another part of the mod writes `key`; the host draws the band that read it again. */
  const write = async (key: string, value: unknown) => {
    state.write(key, value)
    await band.redraw()
  }
  const play = async (ms = PLAY_MS) => {
    for (let t = 0; t < ms; t += STEP_MS) await clock.advance(STEP_MS)
  }
  /** How many separate times a frame said `text`: runs of frames holding it, each a saying. */
  const sayings = (text: string, from = 0) => {
    let n = 0
    let was = false
    for (const f of frames.slice(from)) {
      const is = f.includes(text)
      if (is && !was) n += 1
      was = is
    }
    return n
  }
  return { band, clock, frames, play, sayings, state, write }
}

const postToolUse = ($: Engine) =>
  $.classic.PostToolUse({ tool_name: 'Bash', tool_input: { command: 'ls' }, tool_response: {}, tool_use_id: 'toolu_1' })

describe('a fire written to $.state', () => {
  test('the animal says it, once', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, frames, play, sayings, write } = await started($, on)
    await play(2_000)
    await write('fires', [note()])
    await play()
    expect(sayings(`Rule fired: ${RULE}`), 'said once').toBe(1)
    // the band is drawn again many times over (the host redraws a reader on
    // any write, a resize, a reload of its props): the same note is not news
    const after = frames.length
    await band.redraw()
    await write('fires', [note()])
    await play()
    expect(frames.length, 'the band went on animating').toBeGreaterThan(after)
    expect(sayings(`Rule fired: ${RULE}`, after), 'said again').toBe(0)
  })

  test('a call the rule blocked is said crossly', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings, write } = await started($, on)
    await play(2_000)
    await write('fires', [note({ isBlocked: true, line: `⛔️ Rule fired: ${RULE}` })])
    await play()
    expect(sayings(`Blocked: ${RULE}`)).toBe(1)
    expect(sayings(`Rule fired: ${RULE}`)).toBe(0)
  })

  test('each new note is said, the ones already said are not', { timeoutMs: 90_000 }, async ($, on) => {
    const { frames, play, sayings, write } = await started($, on)
    await play(2_000)
    await write('fires', [note()])
    await play()
    const after = frames.length
    await write('fires', [note(), note({ rule: 'Never force-push', at: 2_000 })])
    await play()
    expect(sayings('Never force-push', after)).toBe(1)
    expect(sayings(`Rule fired: ${RULE}`, after)).toBe(0)
  })

  test('fires already there when the band first looks were said before a reload: not again', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings } = await started($, on, { state: { fires: [note()] } })
    await play()
    expect(sayings(RULE)).toBe(0)
  })

  test('a malformed fires value says nothing and breaks nothing', { timeoutMs: 60_000 }, async ($, on) => {
    const { frames, play, write } = await started($, on)
    await play(2_000)
    await write('fires', [{ rule: 42 }, 'nonsense', null] as never)
    await play(5_000)
    await write('fires', 'not a list' as never)
    await play(5_000)
    expect(frames.some(f => f.includes('Rule fired')), 'something was said').toBe(false)
    expect(frames.length).toBeGreaterThan(10)
  })
})

describe('the classic hooks: the fallback for lanes the mod has not claimed', () => {
  test('no lanes claimed: the Python hook\'s disclosure line is announced', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings } = await started($, on, { context: [LINE] })
    await play(2_000)
    await postToolUse($)
    await play()
    expect(sayings(`Rule fired: ${RULE}`)).toBe(1)
  })

  test('the post lane claimed: the classic answer is ignored', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings } = await started($, on, { context: [LINE], state: { lanes: ['pre', 'post'] } })
    await play(2_000)
    await postToolUse($)
    await play()
    expect(sayings(RULE)).toBe(0)
  })

  test('only the pre lane claimed: the classic PostToolUse is still the fallback', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings } = await started($, on, { context: [LINE], state: { lanes: ['pre'] } })
    await play(2_000)
    await postToolUse($)
    await play()
    expect(sayings(`Rule fired: ${RULE}`)).toBe(1)
  })

  test('a fire both paths see, while the lanes are claimed, is said once', { timeoutMs: 60_000 }, async ($, on) => {
    const { play, sayings, write } = await started($, on, { context: [LINE], state: { lanes: ['pre', 'post'] } })
    await play(2_000)
    await postToolUse($)
    await write('fires', [note()])
    await play()
    expect(sayings(`Rule fired: ${RULE}`)).toBe(1)
  })
})
