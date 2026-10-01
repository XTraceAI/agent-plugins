// The companion at a Stop, through the engine: the session starts on an
// interactive terminal, the band above the prompt is mounted, the harness's
// fork has filed a rule from this session, and a classic Stop is raised. What
// the person would see is the Raster's cells as the animal's timer blits them,
// decoded back to text: the proposal, said as not active yet, and never said
// twice in a session however many Stops the server still lists it at.
//
// The test's `on` stands for the engine beneath the plugin: it answers the
// session's start, the Stop hook (harness_stop itself is not run here), the
// processes the plugin runs (rule_decide.py `proposed`, `url` and decisions)
// and takes the blits.

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-stop-announce'
const TITLE = 'Run only the touched test suites'
const RULE_ID = 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4'
/** rule_decide.py `proposed`'s answer (see proposed.test.ts): one rule this session's fork filed. */
const FILED = JSON.stringify({ proposed: [{ title: TITLE, rule_id: RULE_ID, env: 'staging' }] })
/** Where rule_decide.py url says the rule opens in MemHub Studio. */
const STUDIO = `https://staging.mem.xtrace.ai/studio/rulebook?open=${RULE_ID}`
const LINK_ANSWER = JSON.stringify({ url: STUDIO })
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

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** screen.ts encode(), undone: each 12-byte cell's code point, row by row, rows joined by newlines. */
function textOf(cells: string, columns: number): string {
  const bytes: number[] = []
  const clean = cells.replace(/=+$/, '')
  for (let i = 0; i < clean.length; i += 4) {
    const n = [0, 1, 2, 3].map(k => B64.indexOf(clean[i + k] ?? 'A'))
    const v = (n[0]! << 18) | (n[1]! << 12) | (n[2]! << 6) | n[3]!
    bytes.push((v >> 16) & 255, (v >> 8) & 255, v & 255)
  }
  const chars: string[] = []
  for (let i = 0; i + 12 <= bytes.length; i += 12) {
    const cp = bytes[i]! | (bytes[i + 1]! << 8) | (bytes[i + 2]! << 16) | (bytes[i + 3]! << 24)
    chars.push(String.fromCodePoint(cp >>> 0))
  }
  const rows: string[] = []
  for (let i = 0; i < chars.length; i += columns) rows.push(chars.slice(i, i + columns).join(''))
  return rows.join('\n')
}

/**
 * The engine beneath the companion, and a started session with the band
 * mounted. Returns every frame the band blitted, decoded, the rule_decide.py
 * calls it made (`runs` the activate/reject decisions, `urlRuns` the
 * Studio-link lookups, `listRuns` the `proposed` lookups), the mounted band,
 * and the clock.
 *
 * rule_decide.py beneath answers `proposed` with `listed`, `url` with `url`'s
 * JSON line, and a decision with `decide`'s; `gate`, when given, holds a decision's answer until the
 * test resolves it (a call still in flight).
 */
type Beneath = { decide?: string; gate?: Promise<void>; url?: string }
async function started($: Engine, on: On, listed: string, { decide = '', gate, url = LINK_ANSWER }: Beneath = {}) {
  const clock = mock.clock(on)
  mock.store(on)
  mock.env(on, { HOME: '/home/tester' })
  on('session.start', async (_, e) => ({ cwd: e.cwd }))
  // harness_stop is not what is tested: the Stop hook beneath answers nothing
  on('classic.Stop', async () => ({}))
  on('command.register', async (_, e) => ({ value: { command: e.name } }))
  const runs: (readonly string[])[] = []
  const urlRuns: (readonly string[])[] = []
  const listRuns: (readonly string[])[] = []
  on('process.run', async (_, e) => {
    if (e.argv[2] === 'proposed') {
      listRuns.push(e.argv)
      return { value: { exitCode: 0, stdout: `${listed || '{"proposed":[]}'}\n`, stderr: '' } }
    }
    const isUrl = e.argv[2] === 'url'
    ;(isUrl ? urlRuns : runs).push(e.argv)
    if (!isUrl) await gate
    const line = isUrl ? url : decide
    return { value: { exitCode: 0, stdout: `(progress line)\n${line}\n`, stderr: '' } }
  })
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
  return { band, clock, frames, listRuns, runs, urlRuns }
}

describe('a rule the harness filed, at Stop', () => {
  test('the animal rises and says it is proposed, not active, and where to see it', { timeoutMs: 60_000 }, async ($, on) => {
    const { clock, frames } = await started($, on, FILED)
    await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
    // the bubble types out; play until one frame holds all of it
    const isWhole = (f: string) => f.includes(TYPED_OUT)
    for (let t = 0; t < PLAY_MS && !frames.some(isWhole); t += STEP_MS) {
      await clock.advance(STEP_MS)
    }
    const said = frames.find(isWhole)
    expect(said, 'no frame ever said the whole proposal').toBeDefined()
    // the rule's name is in the text; its `rule` is a Markdown link drawn over
    // the word (see the next describe)
    expect(said).toContain('not active')
    expect(said).toContain('New rule↗ proposed')
  })

  test('a rule the server still lists at a later Stop is not said a second time', { timeoutMs: 90_000 }, async ($, on) => {
    const { clock, frames, listRuns } = await started($, on, FILED)
    await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    expect(frames.some(f => f.includes('not active')), 'the first Stop said it').toBe(true)
    const before = frames.length
    await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
    for (let t = 0; t < PLAY_MS; t += STEP_MS) await clock.advance(STEP_MS)
    // the server was asked again and the band animated all along, so the
    // silence is the session's memory at work, not a missed call or a dead timer
    expect(listRuns).toHaveLength(2)
    expect(listRuns[1]!.slice(2)).toEqual(['proposed', '--session', SESSION])
    expect(frames.length).toBeGreaterThan(before)
    expect(frames.slice(before).some(f => f.includes('not active'))).toBe(false)
  })
})

// A waiting proposal is answered on three plain buttons (1: Activate, 2:
// Reject, 3: Later). While the goose presents it they sit INSIDE its bubble,
// on the footer row other bubbles give to "Got it", once the text is typed
// out; after it has said it and gone, on a "new rule waiting:" row under it.
// Activate and Reject answer through scripts/rule_decide.py (Studio's own
// PATCH), whose LAST stdout line is the JSON outcome; the animal says it back.

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
 * Stop, then play until the buttons come, checking at every step before that
 * the text was not typed out yet: no buttons during the prelude or the typing.
 */
async function stopAndAwaitButtons($: Engine, band: Band, clock: Clock, frames: string[]) {
  await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
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
  test('after Stop the buttons appear inside the bubble once its text is typed out', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames } = await started($, on, FILED)
    for (const key of BUTTONS) expect(await band.find({ key }), `${key} before Stop`).toBeUndefined()
    await stopAndAwaitButtons($, band, clock, frames)
    await expectInsideBubble(band)
    expect(await band.find({ type: 'Text', text: 'new rule' })).toBeUndefined()
  })

  test("the bubble's word `rule` opens the rule in MemHub Studio, pressed where it is drawn", { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, runs, urlRuns } = await started($, on, FILED)
    await stopAndAwaitButtons($, band, clock, frames)
    // looked up once, right after Stop, for this rule in its env
    expect(urlRuns).toHaveLength(1)
    const [python, script, ...rest] = urlRuns[0]!
    expect(python).toBe('python3')
    expect(script).toEndWith('/scripts/rule_decide.py')
    expect(rest).toEqual(['url', RULE_ID, '--env', 'staging'])
    // a plain word, not a Link or Markdown: a terminal without hyperlinks
    // draws those as their text AND their URL, which ran past the bubble
    expect(await band.find({ type: 'Markdown' })).toBeUndefined()
    expect(await band.find({ type: 'Link' })).toBeUndefined()
    const link = await band.find({ type: 'Button', key: 'rule-link' })
    expect(link?.props).toMatchObject({ label: 'rule↗', plain: true })
    expect(link?.props.hotkey).toBeUndefined()
    await expectInsideBubble(band)
    await band.press({ key: 'rule-link' })
    expect(runs, 'pressing `rule` did not open its page').toContainEqual(['open', STUDIO])
    // the ♥ that pets the animal is not over the link
    const pet = (await band.findAll({ type: 'Box' })).find(b => b.props.key === 'companion-clicks')!.props as Placed
    const w = (await overlays(band)).link!.props as Placed
    expect(pet.top === w.top && pet.left >= w.left && pet.left < w.left + w.width, 'the ♥ lies over the link').toBe(false)
  })

  test('with no Studio URL to be had, `rule` is drawn plain, with nothing to press over it', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames } = await started($, on, FILED, { url: '{"url":""}' })
    await stopAndAwaitButtons($, band, clock, frames)
    expect(await band.find({ key: 'rule-link' })).toBeUndefined()
    expect((await rasterRows(band)).join('\n')).toContain('New rule↗ proposed')
  })

  test('once the goose has said it and gone, the buttons wait on a row under it', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames } = await started($, on, FILED)
    await stopAndAwaitButtons($, band, clock, frames)
    const waiting = async () => (await band.find({ type: 'Text', text: 'new rule waiting:' })) !== undefined
    expect(await playUntil(clock, waiting), 'the buttons never moved under the goose').toBe(true)
    expect(await overlay(band), 'still in the bubble too').toBeUndefined()
    for (const key of BUTTONS) expect(await band.find({ type: 'Button', key }), `${key} is missing`).toBeDefined()
    // on that row the rule's name opens it: the bubble, and its `rule`, are gone
    expect((await band.find({ type: 'Button', key: 'rule-name' }))?.props.label).toBe(TITLE)
    expect(await band.find({ key: 'rule-link' })).toBeUndefined()
  })

  for (const { press, stdout, action, says } of [
    { press: 'rule-activate', stdout: '{"outcome":"active","msg":""}', action: 'activate', says: 'Activated' },
    { press: 'rule-activate', stdout: '{"outcome":"forbidden","msg":"not an admin"}', action: 'activate', says: 'Only a rulebook admin' },
    { press: 'rule-reject', stdout: '{"outcome":"dismissed","msg":""}', action: 'reject', says: 'Rejected' },
  ] as const) {
    test(`${press} answering ${stdout} runs rule_decide.py ${action} and says "${says}"`, { timeoutMs: 60_000 }, async ($, on) => {
      const { band, clock, frames, runs } = await started($, on, FILED, { decide: stdout })
      await stopAndAwaitButtons($, band, clock, frames)
      await band.press({ key: press })
      expect(runs).toHaveLength(1)
      const [python, script, ...rest] = runs[0]!
      expect(python).toBe('python3')
      expect(script).toEndWith('/scripts/rule_decide.py')
      expect(rest).toEqual([RULE_ID, action, '--env', 'staging'])
      // answered: the ask is gone, and so is every control
      for (const key of BUTTONS) expect(await band.find({ key }), `${key} stayed`).toBeUndefined()
      expect(await band.find({ text: 'asking MemHub' })).toBeUndefined()
      const said = async () => frames.some(f => f.includes(says))
      expect(await playUntil(clock, said), `no frame said "${says}"`).toBe(true)
    })
  }

  test('rule-later drops the ask without calling MemHub', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames, runs } = await started($, on, FILED)
    await stopAndAwaitButtons($, band, clock, frames)
    await band.press({ key: 'rule-later' })
    expect(runs).toHaveLength(0)
    for (const key of BUTTONS) expect(await band.find({ key }), `${key} stayed`).toBeUndefined()
  })

  test('two quick presses of Activate send one call', { timeoutMs: 60_000 }, async ($, on) => {
    let release = () => {}
    const gate = new Promise<void>(r => (release = r))
    const { band, clock, frames, runs } = await started($, on, FILED, { decide: '{"outcome":"active","msg":""}', gate })
    await stopAndAwaitButtons($, band, clock, frames)
    // both presses land while rule_decide.py has not answered yet
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
    expect(runs, 'more than one decision was sent').toHaveLength(1)
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
    const { band, clock, frames, runs, urlRuns } = await started($, on, '')
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
    // the demo's rule has no id and no env: the lookup asks for the bare url
    expect(urlRuns).toHaveLength(1)
    expect(urlRuns[0]!.slice(2)).toEqual(['url'])
    // the demo names its rule in the text, and its `rule` is pressable as a real one's is
    expect((await rasterRows(band)).join('\n')).toContain('the MCP server when spawning')
    expect((await band.find({ type: 'Button', key: 'rule-link' }))?.props.label).toBe('rule↗')

    await band.press({ key: 'rule-activate' })
    expect(runs, 'a demo press sent a decision').toHaveLength(0)
    const note = 'demo: would turn it on (admins only)'
    expect(await band.find({ type: 'Text', text: note })).toBeDefined()
    expect(await band.find({ key: 'rule-activate' }), 'the note stands in for the buttons').toBeUndefined()

    // the pose ends and starts over with its prelude: no proposal, no buttons
    const gone = async () => !(await hasButtons(band)) && !(await band.find({ text: note }))
    await clock.advance(TICK_MS)
    expect(await playUntil(clock, gone), 'the buttons outlived the bubble').toBe(true)
  })

  test('speak: an advice bubble never brings the buttons', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, clock, frames } = await started($, on, '')
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
