// The companion and a proposed rule, through the engine: the session starts
// on an interactive terminal, the band above the prompt is mounted, the
// harness's fork has filed a rule from this session, and the companion's poll
// finds it — at session start, on its clock, or at a main turn's end (it used
// to ask at each classic Stop, which the built-in guard skips for user mods
// on Team/Enterprise sign-ins; spec §1.4, §4.11). What the person would see is
// the Raster's cells as the animal's timer blits them, decoded back to text:
// the proposal, said as not active yet, and never said twice in a session
// however many polls the server still lists it at.
//
// The test's `on` stands for the engine beneath the plugin: it answers the
// session's start and id, `$.state` and MemHub's REST API (beneath.ts: the
// proposed-rules list and the PATCH that decides one), the credential lookup,
// the browser openers, and takes the blits.

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

import { BASE, BEARER, LIST_URL, listed, type Rest, ran, restBeneath, ruleRow, stateBeneath, STUDIO, textOf, TURN_END } from './beneath'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-stop-announce'
const TITLE = 'Run only the touched test suites'
const RULE_ID = 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4'
/** The server's list: one rule this session's fork filed, and one another session's did. */
const FILED = listed(ruleRow(SESSION, RULE_ID, TITLE), ruleRow('sess-other', '846c4331-1b1d-4295-afe9-18156f44f1df', 'Not ours'))
const NONE = listed()
/** Where the rule opens in MemHub Studio: the web app api-info pairs with the API. */
const RULE_PAGE = `${STUDIO}/studio/rulebook?open=${RULE_ID}`
/**
 * The end of the bubble's text, which names the rule: it wraps at 38 columns
 * as "New rule↗ proposed, not active yet:" / "Run only the touched test suites".
 */
const TYPED_OUT = 'touched test suites'

/** The band as a roomy fullscreen terminal gives it: the animal fits whole beside its bubble. */
const BAND = {
  hasSurvey: false,
  isWorking: false,
  maxRows: 24,
  bodyColumns: 120,
  scroll: { offset: 0, bodyRows: 24 },
  view: {},
}

/** Long enough for enter, wake, rise and the whole bubble to type out and hold. */
const PLAY_MS = 30_000
const STEP_MS = 250

/**
 * The engine beneath the companion, and a started session with the band
 * mounted. The server lists `list` (NONE when not given; a function answers
 * per poll) and answers a PATCH with `decide`'s answers; `gate` holds a PATCH
 * until the test resolves it (a call still in flight). Returns every frame the
 * band blitted, decoded, the requests MemHub got (`rest`), the processes run
 * besides the credential lookup (`runs`: the openers), `$.state` and the store.
 */
type Beneath = Rest & { store?: Record<string, unknown>; state?: Record<string, unknown> }
async function started($: Engine, on: On, beneath: Beneath = {}) {
  const engine = await startedBare($, on, beneath)
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: BAND })
  const raster = await band.find({ type: 'Raster', key: 'companion' })
  expect(raster, 'the band draws the animal as one Raster').toBeDefined()
  engine.setColumns(raster!.props.columns as number)
  const { setColumns: _, ...session } = engine
  return { band, ...session }
}

/** The session started with no band mounted: nothing is drawn, so time is cheap to play. */
async function startedBare($: Engine, on: On, beneath: Beneath = {}) {
  const clock = mock.clock(on)
  // the plugin's store, in memory and read back by the test; `store` seeds it,
  // as a module reload finds what an earlier load wrote
  const store: Record<string, unknown> = { ...(beneath.store ?? {}) }
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
  on('turn.complete', async () => ({ text: '' }))
  on('command.register', async (_, e) => ({ value: { command: e.name } }))
  const state = stateBeneath(on, beneath.state)
  const rest = restBeneath(on, { list: NONE, ...beneath })
  const runs: (readonly string[])[] = []
  on('process.run', async (_, e) => {
    const info = rest.apiInfo(e.argv)
    if (info) return info
    runs.push(e.argv)
    return ran(0)
  })
  const frames: string[] = []
  let columns = 0
  on('ui.blit', async (_, e) => {
    if ('cells' in e) frames.push(textOf(e.cells, columns))
    return { value: {} }
  })

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const setColumns = (n: number) => void (columns = n)
  return { clock, frames, rest, runs, setColumns, state, store }
}

/** A server whose list the test changes: NONE until `file()`; then FILED. */
function server() {
  let answer = NONE
  return { list: () => answer, file: () => void (answer = FILED), clear: () => void (answer = NONE) }
}

/** A main turn ends: the companion asks the server again. */
const turnEnds = ($: Engine) => $.turn.complete(TURN_END)

describe('a rule the harness filed, found by the poll', () => {
  test('at session start: the animal rises and says it is proposed, not active', { timeoutMs: 60_000 }, async ($, on) => {
    const { clock, frames, rest } = await started($, on, { list: FILED })
    // the bubble types out; play until one frame holds all of it
    const isWhole = (f: string) => f.includes(TYPED_OUT)
    for (let t = 0; t < PLAY_MS && !frames.some(isWhole); t += STEP_MS) {
      await clock.advance(STEP_MS)
    }
    const said = frames.find(isWhole)
    expect(said, 'no frame ever said the whole proposal').toBeDefined()
    expect(said).toContain('not active')
    expect(said).toContain('New rule↗ proposed')
    // rule_decide.py proposed()'s request, with the plugin's key and version
    expect(rest.gets[0]?.url).toBe(LIST_URL)
    expect(rest.gets[0]?.method).toBe('GET')
    expect(rest.gets[0]?.headers.Authorization).toBe(`Bearer ${BEARER}`)
    // another session's filing is not this one's to say
    expect(frames.some(f => f.includes('Not ours'))).toBe(false)
  })

  test('a rule filed later is found at the next turn end', { timeoutMs: 120_000 }, async ($, on) => {
    const srv = server()
    const { clock, frames, rest } = await started($, on, { list: srv.list })
    expect(rest.gets, 'asked once at session start').toHaveLength(1)
    srv.file()
    await turnEnds($)
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    expect(rest.gets).toHaveLength(2)
    expect(frames.some(f => f.includes('not active')), 'the turn end found it').toBe(true)
  })

  // With no band mounted: the engine drops the test's mock clock once a test
  // has run 10 s of real time (its hook budget), and five minutes of a drawn
  // animal at 20 fps took longer than that on a loaded CI runner, so the poll
  // never came. Undrawn, the same five minutes play in about a second.
  test('the clock asks every five minutes, whatever the turns do', { timeoutMs: 60_000 }, async ($, on) => {
    const { clock, rest } = await startedBare($, on, { list: FILED })
    await clock.advance(1_000)
    expect(rest.gets, 'asked once as the session starts').toHaveLength(1)
    await clock.advance(299_000)
    expect(rest.gets, 'and again five minutes on').toHaveLength(2)
  })

  test('a rule the server still lists at a later poll is not said a second time', { timeoutMs: 90_000 }, async ($, on) => {
    const { clock, frames, rest } = await started($, on, { list: FILED })
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    expect(frames.some(f => f.includes('not active')), 'the first poll said it').toBe(true)
    const before = frames.length
    await turnEnds($)
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    // the server was asked again and the band animated all along, so the
    // silence is the session's memory at work, not a missed call or a dead timer
    expect(rest.gets).toHaveLength(2)
    expect(frames.length).toBeGreaterThan(before)
    expect(frames.slice(before).some(f => f.includes('not active'))).toBe(false)
  })

  test('the poll writes what is waiting to $.state proposals', { timeoutMs: 60_000 }, async ($, on) => {
    const { clock, state } = await started($, on, { list: FILED })
    await clock.advance(1_000)
    expect(state.value('proposals')).toEqual([{ ruleId: RULE_ID, title: TITLE, at: expect.any(Number) }])
  })

  test('a rule another writer pushes to $.state is asked without waiting for the poll', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, state } = await started($, on)
    state.write('proposals', [{ ruleId: RULE_ID, title: TITLE, at: 1 }])
    await band.redraw()
    let isAsking = false
    for (let t = 0; t < PLAY_MS && !isAsking; t += STEP_MS) {
      await clock.advance(STEP_MS)
      isAsking = (await band.find({ key: 'rule-activate' })) !== undefined
    }
    expect(isAsking, 'the buttons came').toBe(true)
    expect(frames.some(f => f.includes('not active'))).toBe(true)
  })

  test('a failing fetch draws nothing and throws nothing', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, rest } = await started($, on, { list: 'throw' })
    await turnEnds($)
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    expect(rest.gets.length, 'the server was asked').toBeGreaterThanOrEqual(2)
    expect(frames.some(f => f.includes('not active'))).toBe(false)
    expect(await band.find({ key: 'rule-activate' })).toBeUndefined()
    // and the band lives on
    expect(frames.length).toBeGreaterThan(10)
  })

  test('signed out: no key, no request, nothing said', { timeoutMs: 60_000 }, async ($, on) => {
    const { clock, frames, rest } = await started($, on, { list: FILED, bearers: [''] })
    await turnEnds($)
    for (let t = 0; t < 5_000; t += STEP_MS) await clock.advance(STEP_MS)
    expect(rest.gets).toHaveLength(0)
    expect(frames.some(f => f.includes('not active'))).toBe(false)
  })
})

// A waiting proposal is answered on three plain buttons (1: Activate, 2:
// Reject, 3: Later). While the goose presents it they sit INSIDE its bubble,
// on the footer row other bubbles give to "Got it", once the text is typed
// out; after it has said it and gone, on a "new rule waiting:" row under it.
// Activate and Reject answer with Studio's own PATCH over `$.http.fetch` (the
// request rule_decide.py sends); the animal says the outcome back.

const BUTTONS = ['rule-activate', 'rule-reject', 'rule-later']
const TICK_MS = 100

type Band = Awaited<ReturnType<typeof started>>['band']
type Clock = { advance: (ms: number) => Promise<void> }

/** Plays on in frame-sized steps until `isDone` holds, or PLAY_MS runs out. */
async function playUntil(clock: Clock, isDone: () => Promise<boolean>) {
  for (let t = 0; t < PLAY_MS; t += TICK_MS) {
    if (await isDone()) return true
    await clock.advance(TICK_MS)
  }
  return isDone()
}

const hasButtons = async (band: Band) => (await band.find({ type: 'Button', key: 'rule-activate' })) !== undefined

/** The Raster as the same render pass drew it, decoded into rows. */
async function rasterRows(band: Band) {
  const raster = await band.find({ type: 'Raster', key: 'companion' })
  return textOf(raster!.props.cells as string, raster!.props.columns as number).split('\n')
}

/**
 * The Boxes drawn over the Raster, inside the bubble: the link over the word
 * `rule`, and the buttons'. Not the one holding the ♥ at the animal's feet.
 */
async function overlays(band: Band) {
  const boxes = (await band.findAll({ type: 'Box' }))
    .filter(b => b.props.position === 'absolute' && b.props.key !== 'companion-clicks')
  return { link: boxes.find(b => b.props.key === 'rule-word'), buttons: boxes.find(b => b.props.key !== 'rule-word') }
}
const overlay = async (band: Band) => (await overlays(band)).buttons

type Placed = { top: number; left: number; width: number }

/**
 * The buttons sit in the bubble: the overlay's top/left land on a row of the
 * drawn Raster that is the bubble's (its │ borders either side of the span),
 * blank there, and the bubble has no "Got it" anywhere.
 */
async function expectInsideBubble(band: Band) {
  const { link, buttons } = await overlays(band)
  expect(buttons, 'the buttons are not in an overlay').toBeDefined()
  expect(link, 'the rule link is not in an overlay').toBeDefined()
  const at = buttons!.props as Placed
  const rows = await rasterRows(band)
  const row = [...rows[at.top]!]
  expect(row[at.left - 2], `no bubble border left of row ${at.top}`).toBe('│')
  expect(row[at.left + at.width + 1], `no bubble border right of row ${at.top}`).toBe('│')
  expect(row.slice(at.left, at.left + at.width).join('').trim(), `row ${at.top} of the bubble is not blank under its overlay`).toBe('')
  // the link lies exactly over `rule↗` in the bubble's text, above the buttons
  const word = link!.props as Placed
  expect(word.width).toBe(5)
  expect(word.top).toBeLessThan(at.top)
  expect([...rows[word.top]!].slice(word.left, word.left + 5).join(''), 'the link is not over the word').toBe('rule↗')
  expect(rows.join('\n')).not.toContain('Got it')
  for (const key of BUTTONS) {
    const button = await band.find({ type: 'Button', key })
    expect(button, `${key} is missing`).toBeDefined()
    expect(button!.props.plain).toBe(true)
  }
}

/**
 * The rule is filed and a turn ends, then play until the buttons come,
 * checking at every step before that the text was not typed out yet: no
 * buttons during the prelude or the typing.
 */
async function fileAndAwaitButtons($: Engine, band: Band, clock: Clock, frames: string[], file: () => void) {
  file()
  await turnEnds($)
  let sawTyping = false
  const came = await playUntil(clock, async () => {
    if (await hasButtons(band)) return true
    const last = frames[frames.length - 1] ?? ''
    expect(last, 'the text was typed out and still no buttons').not.toContain(TYPED_OUT)
    sawTyping ||= last.includes('not active')
    return false
  })
  expect(sawTyping, 'never saw the bubble mid-typing').toBe(true)
  expect(came, 'the buttons never came').toBe(true)
  expect((await rasterRows(band)).join('\n')).toContain(TYPED_OUT)
}

describe('answering the proposal on the band', () => {
  /** A started session whose server lists nothing until the test files the rule. */
  async function filing($: Engine, on: On, beneath: Beneath = {}) {
    const srv = server()
    const x = await started($, on, { list: srv.list, ...beneath })
    const fileIt = () => fileAndAwaitButtons($, x.band, x.clock, x.frames, srv.file)
    return { ...x, srv, fileIt }
  }

  test('after the poll the buttons appear inside the bubble once its text is typed out', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, fileIt } = await filing($, on)
    for (const key of BUTTONS) expect(await band.find({ key }), `${key} before the rule was filed`).toBeUndefined()
    await fileIt()
    await expectInsideBubble(band)
    expect(await band.find({ type: 'Text', text: 'new rule' })).toBeUndefined()
  })

  test("the bubble's word `rule` opens the rule in MemHub Studio, pressed where it is drawn", { timeoutMs: 60_000 }, async ($, on) => {
    const { band, fileIt, runs } = await filing($, on)
    await fileIt()
    // a plain word, not a Link or Markdown: a terminal without hyperlinks
    // draws those as their text AND their URL, which ran past the bubble
    expect(await band.find({ type: 'Markdown' })).toBeUndefined()
    expect(await band.find({ type: 'Link' })).toBeUndefined()
    const link = await band.find({ type: 'Button', key: 'rule-link' })
    expect(link?.props).toMatchObject({ label: 'rule↗', plain: true })
    expect(link?.props.hotkey).toBeUndefined()
    await expectInsideBubble(band)
    await band.press({ key: 'rule-link' })
    expect(runs, 'pressing `rule` did not open its page').toContainEqual(['open', RULE_PAGE])
    // no python process for it any more: the link is the API's own web app
    // (the mod shell's own rulebook_mod_cli.py calls at boot are not the link's)
    expect(runs.filter(r => r[0] === 'python3' && !String(r[1]).endsWith('/rulebook_mod_cli.py'))).toHaveLength(0)
    // the ♥ that pets the animal is not over the link
    const pet = (await band.findAll({ type: 'Box' })).find(b => b.props.key === 'companion-clicks')!.props as Placed
    const w = (await overlays(band)).link!.props as Placed
    expect(pet.top === w.top && pet.left >= w.left && pet.left < w.left + w.width, 'the ♥ lies over the link').toBe(false)
  })

  test('with no Studio URL to be had, `rule` is drawn plain, with nothing to press over it', { timeoutMs: 60_000 }, async ($, on) => {
    // an API api-info pairs no web app with (a local backend): no link into it
    const { band, fileIt } = await filing($, on, { studio: '' })
    await fileIt()
    expect(await band.find({ key: 'rule-link' })).toBeUndefined()
    expect((await rasterRows(band)).join('\n')).toContain('New rule↗ proposed')
  })

  test('once the goose has said it and gone, the buttons wait on a row under it', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, fileIt } = await filing($, on)
    await fileIt()
    const waiting = async () => (await band.find({ type: 'Text', text: 'new rule waiting:' })) !== undefined
    expect(await playUntil(clock, waiting), 'the buttons never moved under the goose').toBe(true)
    expect(await overlay(band), 'still in the bubble too').toBeUndefined()
    for (const key of BUTTONS) expect(await band.find({ type: 'Button', key }), `${key} is missing`).toBeDefined()
    // on that row the rule's name opens it: the bubble, and its `rule`, are gone
    expect((await band.find({ type: 'Button', key: 'rule-name' }))?.props.label).toBe(TITLE)
    expect(await band.find({ key: 'rule-link' })).toBeUndefined()
  })

  for (const { press, answer, status, says } of [
    { press: 'rule-activate', answer: { status: 200, text: '{"code":0,"data":{"status":"active"}}' }, status: 'active', says: 'Activated' },
    { press: 'rule-activate', answer: { status: 403, text: '{"msg":"not an admin"}' }, status: 'active', says: 'Only an org admin' },
    { press: 'rule-reject', answer: { status: 200, text: '{"code":0,"data":{"status":"dismissed"}}' }, status: 'dismissed', says: 'Rejected' },
  ] as const) {
    test(`${press} answered ${answer.status} PATCHes {"status":"${status}"} and says "${says}"`, { timeoutMs: 60_000 }, async ($, on) => {
      const { band, clock, fileIt, frames, rest } = await filing($, on, { decide: [answer] })
      await fileIt()
      await band.press({ key: press })
      // rule_decide.py decide()'s request: the rule's URL, its body, the plugin's key
      expect(rest.patches).toHaveLength(1)
      expect(rest.patches[0]).toMatchObject({
        url: `${BASE}/v1/team/rulebook/rules/${RULE_ID}`,
        method: 'PATCH',
        body: JSON.stringify({ status }),
        headers: { Authorization: `Bearer ${BEARER}`, 'Content-Type': 'application/json' },
      })
      // answered: the ask is gone, and so is every control
      for (const key of BUTTONS) expect(await band.find({ key }), `${key} stayed`).toBeUndefined()
      expect(await band.find({ text: 'asking MemHub' })).toBeUndefined()
      const said = async () => frames.some(f => f.includes(says))
      expect(await playUntil(clock, said), `no frame said "${says}"`).toBe(true)
    })
  }

  test('a decided rule leaves $.state proposals', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, fileIt, state } = await filing($, on)
    await fileIt()
    expect(state.value('proposals')).toHaveLength(1)
    await band.press({ key: 'rule-activate' })
    expect(state.value('proposals')).toEqual([])
  })

  test('one only an admin can activate stays in $.state proposals: it is still proposed', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, fileIt, state } = await filing($, on, { decide: [{ status: 403, text: '{"msg":"not an admin"}' }] })
    await fileIt()
    await band.press({ key: 'rule-activate' })
    expect(state.value('proposals')).toHaveLength(1)
  })

  test('a 401 resolves the key again and sends once more', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, fileIt, frames, rest } = await filing($, on, {
      bearers: [BEARER, 'mhk_fresh_key'],
      decide: [{ status: 401, text: '{"msg":"expired"}' }, { status: 200, text: '{"code":0,"data":{"status":"active"}}' }],
    })
    await fileIt()
    const before = rest.resolutions()
    await band.press({ key: 'rule-activate' })
    expect(rest.patches).toHaveLength(2)
    expect(rest.patches[0]!.headers.Authorization).toBe(`Bearer ${BEARER}`)
    expect(rest.patches[1]!.headers.Authorization).toBe('Bearer mhk_fresh_key')
    expect(rest.resolutions()).toBe(before + 1)
    expect(await playUntil(clock, async () => frames.some(f => f.includes('Activated'))), 'it said Activated').toBe(true)
  })

  test('a second 401 is not retried again: it asks for a login', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, fileIt, frames, rest } = await filing($, on, { decide: [{ status: 401, text: '{}' }] })
    await fileIt()
    await band.press({ key: 'rule-activate' })
    expect(rest.patches).toHaveLength(2)
    expect(await playUntil(clock, async () => frames.some(f => f.includes('Log in first'))), 'it asked for a login').toBe(true)
  })

  test('a PATCH that cannot be sent is said, and throws nothing', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, fileIt, frames } = await filing($, on, { decide: ['throw'] })
    await fileIt()
    await band.press({ key: 'rule-activate' })
    expect(await playUntil(clock, async () => frames.some(f => f.includes('Could not reach'))), 'it said so').toBe(true)
  })

  test('decided in Studio: the next poll finds it unlisted and the buttons go', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, fileIt, rest, srv } = await filing($, on)
    await fileIt()
    srv.clear()
    await turnEnds($)
    const gone = async () => (await band.find({ key: 'rule-activate' })) === undefined
    expect(await playUntil(clock, gone), 'the buttons outlived the decision').toBe(true)
    for (const key of BUTTONS) expect(await band.find({ key }), `${key} stayed`).toBeUndefined()
    expect(rest.patches, 'nothing was sent from the band').toHaveLength(0)
  })

  test('a poll that could not ask the server keeps the buttons', { timeoutMs: 60_000 }, async ($, on) => {
    let answer: string | 'throw' = NONE
    const { band, clock, frames } = await started($, on, { list: () => answer })
    await fileAndAwaitButtons($, band, clock, frames, () => void (answer = FILED))
    for (const odd of ['throw', '{"code":500}', 'not json'] as const) {
      answer = odd
      await turnEnds($)
      for (let t = 0; t < 2_000; t += STEP_MS) await clock.advance(STEP_MS)
      for (const key of BUTTONS) expect(await band.find({ key }), `${key} went after ${odd}`).toBeDefined()
    }
  })

  test('an announced rule is stored under the session', { timeoutMs: 60_000 }, async ($, on) => {
    const { fileIt, store } = await filing($, on)
    await fileIt()
    expect(store['companion.announced']).toEqual([[SESSION, [RULE_ID]]])
  })

  test('after a reload, a rule the store says was announced is not said again', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, rest } = await started($, on, {
      list: FILED,
      store: { 'companion.announced': [[SESSION, [RULE_ID]]] },
    })
    await turnEnds($)
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    expect(rest.gets.length, 'the server was asked').toBeGreaterThanOrEqual(1)
    expect(frames.some(f => f.includes('not active')), 'it was said again').toBe(false)
    expect(await band.find({ key: 'rule-activate' })).toBeUndefined()
  })

  test('rule-later drops the ask without calling MemHub', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, fileIt, rest } = await filing($, on)
    await fileIt()
    await band.press({ key: 'rule-later' })
    expect(rest.patches).toHaveLength(0)
    for (const key of BUTTONS) expect(await band.find({ key }), `${key} stayed`).toBeUndefined()
  })

  test('two quick presses of Activate send one call', { timeoutMs: 60_000 }, async ($, on) => {
    let release = () => {}
    const gate = new Promise<void>(r => (release = r))
    const { band, fileIt, rest } = await filing($, on, { gate })
    await fileIt()
    // both presses land while MemHub has not answered yet
    const outcome = (p: Promise<unknown>) => p.then(() => 'pressed', () => 'not drawn')
    const first = outcome(band.press({ key: 'rule-activate' }))
    const second = outcome(band.press({ key: 'rule-activate' }))
    await Promise.resolve()
    // in flight: the buttons have given way to a note, so nothing is left to press
    expect(await band.find({ type: 'Text', text: 'asking MemHub' })).toBeDefined()
    expect(await band.find({ key: 'rule-activate' })).toBeUndefined()
    release()
    // the second press may still reach the button (both left before the
    // redraw) or find it gone; either way decide() sends only the first
    const [one, two] = await Promise.all([first, second])
    expect(one).toBe('pressed')
    expect(['pressed', 'not drawn']).toContain(two)
    expect(rest.patches, 'more than one decision was sent').toHaveLength(1)
  })
})

// `/goose propose` (and `/goose demo`, whose cycle includes it) shows the same
// buttons in its proposal bubble, but a press only says, in their place for
// 2s, what it would do: nothing reaches MemHub from a demo.
describe('the demo shows the proposal buttons', () => {
  const GOOSE = (args: string) => ({
    command: 'goose',
    args,
    origin: { kind: 'composer' } as const,
    presentation: { isFullscreen: true, columns: BAND.bodyColumns },
  })

  test('propose: the buttons come in the typed-out bubble, a press only explains, and they go with it', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, rest } = await started($, on)
    await $.command.run(GOOSE('propose'))
    let sawBubble = false
    const came = await playUntil(clock, async () => {
      if (await hasButtons(band)) return true
      // the prelude (! and sparkles) and the typing draw no buttons
      sawBubble ||= frames.some(f => f.includes('Gus the Goose'))
      return false
    })
    expect(came, 'the buttons never came').toBe(true)
    expect(sawBubble, 'the buttons came before the bubble had typed').toBe(true)
    await expectInsideBubble(band)
    // the demo names its rule in the text, and its `rule` is pressable as a real one's is
    expect((await rasterRows(band)).join('\n')).toContain('the MCP server when spawning')
    expect((await band.find({ type: 'Button', key: 'rule-link' }))?.props.label).toBe('rule↗')

    await band.press({ key: 'rule-activate' })
    expect(rest.patches, 'a demo press sent a decision').toHaveLength(0)
    const note = 'demo: would turn it on (admins only)'
    expect(await band.find({ type: 'Text', text: note })).toBeDefined()
    expect(await band.find({ key: 'rule-activate' }), 'the note stands in for the buttons').toBeUndefined()

    // the pose ends and starts over with its prelude: no proposal, no buttons
    const gone = async () => !(await hasButtons(band)) && !(await band.find({ text: note }))
    await clock.advance(TICK_MS)
    expect(await playUntil(clock, gone), 'the buttons outlived the bubble').toBe(true)
  })

  test('speak: an advice bubble never brings the buttons', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames } = await started($, on)
    await $.command.run(GOOSE('speak'))
    let seen = false
    await playUntil(clock, async () => {
      seen ||= await hasButtons(band)
      return false
    })
    // the goose did speak (its bubble header names it), just not a proposal
    expect(frames.some(f => f.includes('Gus the Goose'))).toBe(true)
    expect(seen).toBe(false)
  })
})
