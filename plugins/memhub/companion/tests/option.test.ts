// Whether the ANIMAL shows, and what stands in its place when it does not:
// the plugin's `companion` option (off by default), the `/<animal>` command
// (stored `companion.enabled`), and the machine's repository allowlist
// (repos.json, read by the shell at boot as `paths`' `enabled` and handed on
// as ctx.active()). With the animal off the band is never empty: one plain
// status line (the strip) stands there, quiet and unmoving, and still shows a
// fire or a proposed rule with its buttons.
//
//   option off, nothing stored   → the strip, not the animal (how the plugin installs)
//   option off, /goose           → the animal, and stored for the next session
//   option on, nothing stored    → the animal
//   option on, /goose off        → the strip (the store wins)
//   repository not listed        → the strip says MemHub is inactive here; /goose says why
//   strip + a fire               → "📏 Rule fired: …" on the line, for two turn ends
//   strip + a proposed rule      → its name and 1 Activate / 2 Reject / 3 Later

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

import type { FireNote } from '../../types'
import { listed, ran, restBeneath, ruleRow, stateBeneath, TURN_END } from './beneath'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-option-1'
const RULE_ID = 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4'
const BAND = { hasSurvey: false, isWorking: false, maxRows: 24, bodyColumns: 120, scroll: { offset: 0, bodyRows: 24 }, view: {} }
const RUN = { origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } } as const

type Beneath = {
  store?: Record<string, unknown>
  /** What `rulebook_mod_cli.py paths` answers for the session's cwd; none: the command fails (no book). */
  paths?: Record<string, unknown>
  /** The server's proposed-rules list; none lists nothing. */
  listed?: string
}

/** Lets every pending promise chain run (the fakes answer in microtasks). */
async function settle(): Promise<void> {
  for (let i = 0; i < 200; i++) await Promise.resolve()
}

async function started($: Engine, on: On, beneath: Beneath = {}) {
  mock.clock(on)
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
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Box({ key: 'engine-band' }))
  on('classic.PostToolUse', async () => ({ additionalContext: [] }))
  on('command.register', async (_, e) => ({ value: { command: e.name } }))
  on('turn.complete', async () => ({ text: '' }))
  const state = stateBeneath(on)
  const rest = restBeneath(on, { list: beneath.listed ?? listed() })
  on('process.run', async (_, e) => {
    const info = rest.apiInfo(e.argv)
    if (info) return info
    if (e.argv[2] === 'paths' && beneath.paths) return ran(0, JSON.stringify(beneath.paths) + '\n')
    return ran(1)
  })
  on('ui.blit', async () => ({ value: {} }))
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await settle()
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: BAND })
  const animal = async () => (await band.find({ type: 'Raster', key: 'companion' })) !== undefined
  const strip = async () => (await band.find({ key: 'memhub-strip' }))?.text
  /** Another part of the mod writes a fire; the host draws the band that read it again. */
  const fire = async (note: FireNote) => {
    state.write('fires', [note])
    await band.redraw()
  }
  return { band, store, state, animal, strip, fire }
}

const DORMANT = { book: '/home/tester/.config/memhub-plugin/rulebook/book/repo-abc.json', repo: 'client/app', ledger: '/l', enabled: false, restricted: true }
const LISTED = { ...DORMANT, enabled: true }
const NOTE: FireNote = { ruleId: RULE_ID, line: '📏 Rule fired: Keep tests fast', rule: 'Keep tests fast', isBlocked: false, at: 1_000 }

describe('the companion option, off by default', () => {
  test('nothing stored: the status line, not the animal', async ($, on) => {
    const { animal, strip } = await started($, on)
    expect(await animal()).toBe(false)
    expect(await strip()).toBe('MemHub · capturing')
  })

  test('/goose shows the animal in this session and stores that, so the next session shows it too', async ($, on) => {
    const { band, store, animal, strip } = await started($, on)
    const r = await $.command.run({ command: 'goose', args: '', ...RUN })
    expect('text' in r ? r.text : '').not.toMatch(/inactive/)
    await band.redraw()
    expect(await animal()).toBe(true)
    expect(await strip()).toBeUndefined()
    expect(store['companion.enabled']).toBe(true)
  })

  test('stored from an earlier /goose: the animal, without the option', async ($, on) => {
    const { animal } = await started($, on, { store: { 'companion.enabled': true } })
    expect(await animal()).toBe(true)
  })
})

describe('the companion option, on', () => {
  test('nothing stored: the animal', { options: { companion: true } }, async ($, on) => {
    const { animal, strip } = await started($, on)
    expect(await animal()).toBe(true)
    expect(await strip()).toBeUndefined()
  })

  test('/goose off was run: the status line, the store wins', { options: { companion: true } }, async ($, on) => {
    const { animal, strip } = await started($, on, { store: { 'companion.enabled': false } })
    expect(await animal()).toBe(false)
    expect(await strip()).toBe('MemHub · capturing')
  })

  test('/goose off mid-session returns to the status line and says so', { options: { companion: true } }, async ($, on) => {
    const { band, animal, strip } = await started($, on)
    expect(await animal()).toBe(true)
    const r = await $.command.run({ command: 'goose', args: 'off', ...RUN })
    expect('text' in r ? r.text : '').toMatch(/status line stays/)
    await band.redraw()
    expect(await animal()).toBe(false)
    expect(await strip()).toBe('MemHub · capturing')
  })
})

describe('a repository the allowlist leaves out', () => {
  test('the status line says MemHub is inactive here, and /goose says why instead of showing the animal', { options: { companion: true } }, async ($, on) => {
    const { band, store, animal, strip } = await started($, on, { paths: DORMANT })
    expect(await animal()).toBe(false)
    expect(await strip()).toBe(`MemHub · inactive in this repo (/${PLUGIN}:repos add .)`)
    const r = await $.command.run({ command: 'goose', args: '', ...RUN })
    expect('text' in r ? r.text : '').toMatch(/inactive in this repository/)
    expect('text' in r ? r.text : '').toContain(`/${PLUGIN}:repos add .`)
    await band.redraw()
    expect(await animal()).toBe(false)
    expect(store['companion.enabled']).toBeUndefined()
  })

  test('a listed repository: the animal as usual', { options: { companion: true } }, async ($, on) => {
    const { animal } = await started($, on, { paths: LISTED })
    expect(await animal()).toBe(true)
  })

  test('a session outside any checkout (the parent folder of several repos): dormant, and the strip says why', { options: { companion: true } }, async ($, on) => {
    // `paths` names no repo and no book, and the allowlist says dormant (repo_allow.session_enabled);
    // before this the answer was dropped with the missing book and the strip said "capturing"
    const { band, animal, strip } = await started($, on, { paths: { book: null, repo: '', ledger: '/l', enabled: false, restricted: true } })
    expect(await animal()).toBe(false)
    expect(await strip()).toBe(`MemHub · inactive here: not in a repository (/${PLUGIN}:repos add . inside one)`)
    const r = await $.command.run({ command: 'goose', args: '', ...RUN })
    expect('text' in r ? r.text : '').toMatch(/not inside a repository/)
    await band.redraw()
    expect(await animal()).toBe(false)
  })

  test('a session outside any checkout with NO repos.json: active, as always', { options: { companion: true } }, async ($, on) => {
    const { animal } = await started($, on, { paths: { book: null, repo: '', ledger: '/l', enabled: true, restricted: false } })
    expect(await animal()).toBe(true)
  })
})

describe('the status line still shows what MemHub does', () => {
  test('a fire stands on the line, as the animal would say it, through the turn and the next', async ($, on) => {
    const { band, strip, fire } = await started($, on)
    await fire(NOTE)
    expect(await strip()).toBe('📏 Rule fired: Keep tests fast')
    await $.turn.complete(TURN_END)
    await band.redraw()
    expect(await strip(), 'still there after the turn it fired in').toBe('📏 Rule fired: Keep tests fast')
    await $.turn.complete(TURN_END)
    await band.redraw()
    expect(await strip(), 'gone after the next').toBe('MemHub · capturing')
  })

  test('a blocking fire is said as blocked', async ($, on) => {
    const { strip, fire } = await started($, on)
    await fire({ ...NOTE, line: '⛔️ Rule fired: No force-push', rule: 'No force-push', isBlocked: true })
    expect(await strip()).toBe('⛔️ Blocked: No force-push')
  })

  test('a proposed rule shows its name and the 1 Activate / 2 Reject / 3 Later buttons, with no animal', async ($, on) => {
    const { band, animal } = await started($, on, { listed: listed(ruleRow(SESSION, RULE_ID, 'Run only the touched test suites')) })
    await settle()
    await band.redraw()
    expect(await animal()).toBe(false)
    expect(await band.find({ type: 'Button', key: 'rule-activate' }), '1 Activate').toBeDefined()
    expect(await band.find({ type: 'Button', key: 'rule-reject' }), '2 Reject').toBeDefined()
    expect(await band.find({ type: 'Button', key: 'rule-later' }), '3 Later').toBeDefined()
    const name = await band.find({ type: 'Button', key: 'rule-name' })
    expect(name, "the rule's name is the link to Studio").toBeDefined()
    expect(String(name!.props.label)).toMatch(/touched test suites/)
  })
})
