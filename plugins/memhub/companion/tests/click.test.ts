// The companion's clicks and which animal a session shows, through the engine.
//
// A click is a press of the ♥ beside the animal's ground (the animal itself
// is a Raster, which takes none): it pets it. There is no double click. The
// animal is the session's own: its id's hash, unless pinned or changed in the
// session, and picked again for the new session a /clear or /resume starts. See docs/specs/companion-upgrades.md.
//
// The test's `on` stands for the engine beneath: it answers the session's id,
// the plugin's store (an in-memory one this test reads back), the processes
// the plugin runs (rule_decide.py and the browser openers) and takes the blits.

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

import { ANIMALS } from '../animals'
import { layoutOf } from '../screen'
import { fnv1a32 } from '../selection'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-click-1'
const RULE_ID = 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4'
const RULEBOOK = 'https://staging.mem.xtrace.ai/studio/rulebook'
const RULE_PAGE = `${RULEBOOK}?open=${RULE_ID}`
/** rule_decide.py `proposed`'s answer for a session whose harness fork filed one rule. */
const FILED = JSON.stringify({ proposed: [{ title: 'Run only the touched test suites', rule_id: RULE_ID, env: 'staging' }] })
const NAMES = ['goose', 'hippo', 'penguin', 'shiba']

const BAND = {
  hasSurvey: false,
  isWorking: false,
  maxRows: 24,
  bodyColumns: 120,
  scroll: { offset: 0, bodyRows: 24 },
  view: {},
}

const TICK_MS = 100
const PLAY_MS = 30_000

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** screen.ts encode(), undone: each 12-byte cell's code point, row by row. */
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
    chars.push(String.fromCodePoint(cp || 32))
  }
  const rows: string[] = []
  for (let i = 0; i < chars.length; i += columns) rows.push(chars.slice(i, i + columns).join(''))
  return rows.join('\n')
}

/** What the openers beneath answer: exit codes for `open` and `xdg-open`. */
type Beneath = {
  store?: Record<string, unknown>
  sessionId?: () => string
  openers?: { open?: number; 'xdg-open'?: number }
  url?: string
  /** rule_decide.py `proposed`'s answer; none lists nothing. */
  listed?: string
  context?: string[]
  /** Mount the band BEFORE session.start, as the engine does: it draws the band first. */
  drawFirst?: boolean
}

async function started($: Engine, on: On, beneath: Beneath = {}) {
  const clock = mock.clock(on)
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
  const sessionId = beneath.sessionId ?? (() => SESSION)
  on('session.id', async () => ({ value: sessionId() }))
  on('session.start', async (_, e) => ({ cwd: e.cwd }))
  on('session.end', async () => ({ sessionId: SESSION }))
  on('classic.Stop', async () => ({}))
  // what the band hands down when it draws nothing (a companion turned off): the engine's own, empty
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Box({ key: 'engine-band' }))
  on('classic.PostToolUse', async () => ({ additionalContext: beneath.context ?? [] }))
  on('command.register', async (_, e) => ({ value: { command: e.name } }))
  const runs: (readonly string[])[] = []
  on('process.run', async (_, e) => {
    runs.push(e.argv)
    const opener = e.argv[0] as 'open' | 'xdg-open'
    if (opener === 'open' || opener === 'xdg-open') {
      return { value: { exitCode: beneath.openers?.[opener] ?? 0, stdout: '', stderr: '' } }
    }
    if (e.argv[2] === 'proposed') {
      return { value: { exitCode: 0, stdout: `${beneath.listed ?? '{"proposed":[]}'}\n`, stderr: '' } }
    }
    const url = e.argv.length > 4 ? RULE_PAGE : (beneath.url ?? RULEBOOK)
    return { value: { exitCode: 0, stdout: `(progress line)\n${JSON.stringify({ url })}\n`, stderr: '' } }
  })
  const frames: string[] = []
  let columns = 0
  on('ui.blit', async (_, e) => {
    if ('cells' in e) frames.push(textOf(e.cells, columns))
    return { value: {} }
  })

  const mount = () => $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: BAND })
  const early = beneath.drawFirst ? await mount() : undefined
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const band = early ?? (await mount())
  const raster = await band.find({ type: 'Raster', key: 'companion' })
  // a companion turned off draws no band at all
  const isOff = beneath.store?.['companion.enabled'] === false
  if (!isOff) expect(raster, 'the band draws the animal as one Raster').toBeDefined()
  columns = (raster?.props.columns as number | undefined) ?? 0
  const click = () => band.press({ key: 'companion-pet' })
  /** Plays on in frame-sized steps until `isDone` holds, or PLAY_MS runs out. */
  const playUntil = async (isDone: () => boolean | Promise<boolean>) => {
    for (let t = 0; t < PLAY_MS; t += TICK_MS) {
      if (await isDone()) return true
      await clock.advance(TICK_MS)
    }
    return isDone()
  }
  const play = (ms: number) => clock.advance(ms)
  return { band, clock, frames, runs, store, click, play, playUntil }
}

/** The title the band's bubble introduces the animal by, from the frames. */
const TITLES: Record<string, string> = { goose: 'Gus the Goose', hippo: 'Hugo the Hippo', penguin: 'Penelope the Penguin', shiba: 'Popo the Shiba' }
const hashed = (id: string) => NAMES[fnv1a32(id) % NAMES.length]!
const openerRuns = (runs: (readonly string[])[]) => runs.filter(r => r[0] === 'open' || r[0] === 'xdg-open')
const urlRuns = (runs: (readonly string[])[]) => runs.filter(r => r[2] === 'url')

describe('the ♥ that pets it', () => {
  test('sits on the ground row, just left of where the ground begins, drawn as ♥ alone', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, frames, play } = await started($, on, { store: { 'companion.pin': 'goose' } })
    await play(8_000) // in, and asleep: nothing but the animal and its ground drawn
    const raster = await band.find({ type: 'Raster', key: 'companion' })
    const pet = await band.find({ type: 'Button', key: 'companion-pet' })
    expect(pet, 'no ♥ to press').toBeDefined()
    expect(pet!.props).toMatchObject({ label: '♥', plain: true })
    expect(pet!.props.hotkey, 'a hotkey draws as `p: ♥`').toBeUndefined()
    const at = (await band.findAll({ type: 'Box' })).find(b => b.props.key === 'companion-clicks')!.props as { top: number; left: number }
    const rows = raster!.props.rows as number
    expect(at.top).toBe(rows - 1)
    // the ground row: blank where the ♥ is, drawn from the very next cell
    const ground = [...frames[frames.length - 1]!.split('\n')[rows - 1]!]
    expect(ground[at.left], 'the ♥ sits in the grass').toBe(' ')
    expect(ground[at.left + 1], 'the ground does not begin right of the ♥').not.toBe(' ')
    expect(await band.find({ type: 'Client' }), 'a click surface came back').toBeUndefined()
  })
})

describe('a click pets the animal', () => {
  test('asleep, it wakes and is petted: a heart shows', { timeoutMs: 60_000 }, async ($, on) => {
    const { frames, click, play, playUntil } = await started($, on, { store: { 'companion.pin': 'goose' } })
    await play(8_000) // in, and asleep
    const before = frames.length
    expect(frames.slice(0, before).some(f => f.includes('♥'))).toBe(false)
    await click()
    expect(await playUntil(() => frames.slice(before).some(f => f.includes('♥'))), 'no heart after the click').toBe(true)
  })

  test('saying a fired rule, a click takes the bubble away (Got it)', { timeoutMs: 60_000 }, async ($, on) => {
    const rule = 'Tests first'
    const { frames, click, playUntil } = await started($, on, {
      store: { 'companion.pin': 'goose' },
      context: [`📏 Rule fired: ${rule}`],
    })
    await $.classic.PostToolUse({ tool_name: 'Bash', tool_input: { command: 'ls' }, tool_response: {} })
    expect(await playUntil(() => frames.some(f => f.includes(rule))), 'the rule was never said').toBe(true)
    await click()
    const after = frames.length
    await playUntil(() => frames.length > after + 5)
    expect(frames.slice(after + 1).some(f => f.includes(rule)), 'the bubble stayed after Got it').toBe(false)
  })

  test('presenting a proposal, a click changes nothing: the bubble and buttons stay', { timeoutMs: 60_000 }, async ($, on) => {
    const { band, frames, click, play, playUntil } = await started($, on, { store: { 'companion.pin': 'goose' }, listed: FILED })
    await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
    const hasButtons = async () => (await band.find({ type: 'Button', key: 'rule-activate' })) !== undefined
    expect(await playUntil(hasButtons), 'the buttons never came').toBe(true)
    const before = frames.length
    await click()
    await play(600) // a dozen frames: long enough for a hush to show, well inside the 8s ask
    const after = frames.slice(before)
    expect(after.length, "the band drew nothing after the click").toBeGreaterThan(0)
    expect(after.every(f => f.includes('not active')), 'a click hid the proposal bubble').toBe(true)
    expect(await hasButtons(), 'a click dismissed the proposal').toBe(true)
  })
})

describe('there is no double click', () => {
  test('two quick clicks are a pet, and open nothing', { timeoutMs: 60_000 }, async ($, on) => {
    const { runs, click, play } = await started($, on)
    await play(1_000)
    await click()
    await click()
    await play(1_000)
    expect(openerRuns(runs)).toHaveLength(0)
    expect(urlRuns(runs)).toHaveLength(0)
  })
})

describe("a proposal's rule↗ opens the rule in the browser", () => {
  const proposing = async ($: Engine, on: On, openers: Beneath['openers']) => {
    const x = await started($, on, { store: { 'companion.pin': 'goose' }, listed: FILED, openers })
    await $.classic.Stop({ stop_hook_active: false, session_id: SESSION })
    const pressable = async () => (await x.band.find({ type: 'Button', key: 'rule-link' })) !== undefined
    expect(await x.playUntil(pressable), 'rule↗ never became pressable').toBe(true)
    await x.band.press({ key: 'rule-link' })
    await x.play(500)
    return x
  }

  test('with open', { timeoutMs: 60_000 }, async ($, on) => {
    const { runs } = await proposing($, on, {})
    expect(openerRuns(runs)).toEqual([['open', RULE_PAGE]])
  })

  test('where open fails, with xdg-open', { timeoutMs: 60_000 }, async ($, on) => {
    const { runs } = await proposing($, on, { open: 1 })
    expect(openerRuns(runs)).toEqual([['open', RULE_PAGE], ['xdg-open', RULE_PAGE]])
  })

  test('where neither works, nothing else happens, and nothing is said', { timeoutMs: 60_000 }, async ($, on) => {
    const { runs, frames } = await proposing($, on, { open: 1, 'xdg-open': 127 })
    expect(openerRuns(runs)).toEqual([['open', RULE_PAGE], ['xdg-open', RULE_PAGE]])
    expect(frames.some(f => f.includes('studio')), 'it said the URL instead').toBe(false)
  })
})

describe('which animal a session shows', () => {
  const titleShown = (frames: string[]) => NAMES.filter(n => frames.some(f => f.includes(TITLES[n]!)))

  test('the session id picks it; an old saved pick is not read', { timeoutMs: 60_000 }, async ($, on) => {
    const other = NAMES.find(n => n !== hashed(SESSION))!
    const { frames, play, playUntil } = await started($, on, { store: { 'companion.animal': other } })
    // the bubble names the animal: make it speak through its own demo
    await $.command.run({ command: hashed(SESSION), args: 'speak', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    await play(100)
    expect(await playUntil(() => titleShown(frames).length > 0)).toBe(true)
    expect(titleShown(frames)).toEqual([hashed(SESSION)])
  })

  test('drawn before session.start, the band shows the picked animal from its first frame: no redraw needed', { timeoutMs: 60_000 }, async ($, on) => {
    // The engine draws the band before session.start runs (a probe in a live
    // session: first render at .462s, session.start at .659s). The pick then
    // named the animal but kept the fallback goose's art, so the goose walked
    // in and stayed until a resize (a tmux split) redrew the band. The penguin
    // is the pin here because its drawing is taller than the goose's, so the
    // band's rows tell which art the band was last drawn for.
    const rowsFor = (name: string) => layoutOf(ANIMALS.find(a => a.name === name)!.builds[0]!, BAND.bodyColumns, BAND.maxRows)!.rows
    const gooseRows = rowsFor('Goose')
    const penguinRows = rowsFor('Penguin')
    expect(penguinRows, 'the test needs two animals of different heights').not.toBe(gooseRows)
    const x = await started($, on, { store: { 'companion.pin': 'penguin' }, drawFirst: true })
    await x.play(3_000)
    const raster = await x.band.find({ type: 'Raster', key: 'companion' })
    expect(raster!.props.rows, 'still drawn for the goose: the band was never redrawn for the pick').toBe(penguinRows)
    // no frame since was the goose's: every one is the penguin's height
    expect(x.frames.length).toBeGreaterThan(0)
    expect(x.frames.every(f => f.split('\n').length === penguinRows), 'a frame was drawn at the goose\'s size').toBe(true)
  })

  test('drawn before session.start, a saved scale is drawn at once, even when the build stays', { timeoutMs: 60_000 }, async ($, on) => {
    // the goose has one build, so scale 2 keeps it: only the layout changes
    const goose = ANIMALS.find(a => a.name === 'Goose')!.builds[0]!
    const scaled = layoutOf(goose, BAND.bodyColumns, BAND.maxRows, 2)!
    expect(scaled.columns, 'the test needs a scale that changes the width').not.toBe(layoutOf(goose, BAND.bodyColumns, BAND.maxRows)!.columns)
    const x = await started($, on, { store: { 'companion.pin': 'goose', 'companion.scale': 2 }, drawFirst: true })
    await x.play(1_000)
    const raster = await x.band.find({ type: 'Raster', key: 'companion' })
    expect(raster!.props.columns, 'still laid out at no scale').toBe(scaled.columns)
  })

  test('drawn before session.start, a companion turned off draws nothing, not an empty band', { timeoutMs: 60_000 }, async ($, on) => {
    const x = await started($, on, { store: { 'companion.pin': 'goose', 'companion.enabled': false }, drawFirst: true })
    await x.play(1_000)
    expect(await x.band.find({ type: 'Raster', key: 'companion' }), 'the pre-start band stayed').toBeUndefined()
    expect(await x.band.find({ type: 'Button', key: 'companion-pet' }), 'a dead ♥ stayed').toBeUndefined()
  })

  test('/<animal> always pins it; /<animal> random unpins; never and include edit the pool', async ($, on) => {
    const { store } = await started($, on)
    const run = (command: string, args: string) =>
      $.command.run({ command, args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    await run('hippo', 'always')
    expect(store['companion.pin']).toBe('hippo')
    await run('hippo', 'random')
    expect(store['companion.pin']).toBeUndefined()
    await run('goose', 'never')
    await run('hippo', 'never')
    await run('penguin', 'never')
    expect(store['companion.never']).toEqual(['goose', 'hippo', 'penguin'])
    const refused = await run('shiba', 'never')
    expect(JSON.stringify(refused)).toContain('last animal')
    expect(store['companion.never']).toEqual(['goose', 'hippo', 'penguin'])
    await run('goose', 'include')
    expect(store['companion.never']).toEqual(['hippo', 'penguin'])
    await run('hippo', 'always')
    expect(store['companion.never'], 'always puts it back in the pool').toEqual(['penguin'])
    expect(store['companion.animal'], 'the old global pick is never written').toBeUndefined()
  })

  test('a plain /<animal> changes this session only: remembered under its id, no pin', async ($, on) => {
    const { store } = await started($, on)
    await $.command.run({ command: 'penguin', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    expect(store['companion.sessions']).toEqual([[SESSION, 'penguin']])
    expect(store['companion.pin']).toBeUndefined()
  })

  /** The animal a frame's bubble introduces, played until a rule fire makes it speak. */
  const speaker = async (x: Awaited<ReturnType<typeof started>>) => {
    const from = x.frames.length
    await $$.classic.PostToolUse({ tool_name: 'Bash', tool_input: { command: 'ls' }, tool_response: {} })
    await x.playUntil(() => titleShown(x.frames.slice(from)).length > 0)
    return titleShown(x.frames.slice(from))
  }
  let $$: Engine

  for (const reason of ['clear', 'resume'] as const) {
    test(`a /${reason} shows the new session's own animal, with no command run`, { timeoutMs: 60_000 }, async ($, on) => {
      $$ = $
      const next = Array.from({ length: 50 }, (_, i) => `sess-after-${reason}-${i}`).find(id => hashed(id) !== hashed(SESSION))!
      let id = SESSION
      const x = await started($, on, { sessionId: () => id, context: ['📏 Rule fired: Tests first'] })
      expect(await speaker(x)).toEqual([hashed(SESSION)])
      id = next
      await $.session.end({ reason, sessionId: SESSION, resume: { id: SESSION } } as never)
      await x.play(2_000)
      expect(await speaker(x), 'the old animal stayed').toEqual([hashed(next)])
      expect(x.store['companion.sessions'], 'nothing is saved for a session nobody picked in').toBeUndefined()
    })
  }

  test('a command typed after /clear, before the engine reports the new id, is that session\'s', { timeoutMs: 60_000 }, async ($, on) => {
    $$ = $
    const other = NAMES.find(n => n !== hashed(SESSION))!
    const next = Array.from({ length: 50 }, (_, i) => `sess-racing-${i}`).find(i => hashed(i) !== other)!
    let id = SESSION
    const x = await started($, on, { sessionId: () => id, context: ['📏 Rule fired: Tests first'] })
    await $.session.end({ reason: 'clear', sessionId: SESSION, resume: { id: SESSION } } as never)
    // the engine still says the old id when the person picks
    await $.command.run({ command: other, args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    id = next
    await x.play(2_000)
    expect(x.store['companion.sessions'], 'filed under the wrong session').toEqual([[next, other]])
    expect(await speaker(x), 'the hash undid the command').toEqual([other])
  })

  test('a click from before /<animal> off does not pet the animal once it is back', { timeoutMs: 60_000 }, async ($, on) => {
    const { frames, click, play } = await started($, on, { store: { 'companion.pin': 'goose' } })
    await play(8_000) // asleep
    const run = (args: string) =>
      $.command.run({ command: 'goose', args, origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } })
    await click() // the pet waits on the wake...
    await run('off') // ...which never comes: the band goes first
    await run('')
    const from = frames.length
    await play(8_000)
    expect(frames.slice(from).some(f => f.includes('♥')), 'petted by a click from before off').toBe(false)
  })
})
