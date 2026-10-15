// The strip's enable ask (ENG-1253). In a repository the machine's allowlist
// (repos.json) leaves out, the status strip asks once, at session start,
// whether to enable MemHub there:
//
//   MemHub · enable in <org/repo>?   1 Yes   2 No   3 Later
//
// It shows only when ALL of these hold, read off `rulebook_mod_cli.py paths`
// at boot (nothing re-reads repos.json in TypeScript): a repos.json exists
// (`restricted`), the session is inside a checkout (`repo`, `identity`), the
// person is signed in, and the repository is neither listed (`enabled`) nor
// `declined`. Yes runs `rulebook_mod_cli.py answer --say yes` (what
// `/memhub:repos add .` writes) and the strip flips to the quiet line — or
// the animal, where the option asks for one; No runs `--say no`, which
// records the repository as declined, and the ask is gone for good; Later
// hides it for this session only, and writes nothing.

import type { On } from 'claude-code'
import { describe, type Engine, expect, mock, test } from 'claude-code/testing'

import type { FireNote } from '../../types'
import { listed, ran, restBeneath, stateBeneath, TURN_END } from './beneath'

const PLUGIN = 'memhub-staging'
const SESSION = 'sess-ask-1'
const CWD = '/work/client-app'
const BAND = { hasSurvey: false, isWorking: false, maxRows: 24, bodyColumns: 120, scroll: { offset: 0, bodyRows: 24 }, view: {} }
const RUN = { origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 120 } } as const
const ASK_TEXT = 'MemHub · enable in ClientCo/app?'
const QUIET_INACTIVE = `MemHub · inactive in this repo (/${PLUGIN}:repos add .)`

/** What `paths` answers in a checkout a repos.json leaves out: the ask's case. */
const UNLISTED = {
  book: '/home/tester/.config/memhub-plugin/rulebook/book/app-abc.json', repo: 'app', ledger: '/l',
  enabled: false, restricted: true, declined: false, identity: 'ClientCo/app',
}
const LISTED = { ...UNLISTED, enabled: true }
const DECLINED = { ...UNLISTED, declined: true }
/** The same checkout on a machine with no repos.json: active, as always. */
const NO_FILE = { ...UNLISTED, enabled: true, restricted: false }
/** A session in the parent folder of several checkouts, with a repos.json: dormant, nothing to list. */
const OUTSIDE = { book: null, repo: '', ledger: '/l', enabled: false, restricted: true, declined: false, identity: '' }
/** An older CLI's answer: no `restricted`, `declined` or `identity` at all. */
const OLDER_CLI = { book: UNLISTED.book, repo: 'app', ledger: '/l', enabled: false }

type Beneath = {
  store?: Record<string, unknown>
  paths?: Record<string, unknown>
  /** What `answer` prints (the exit code and the JSON line); none: exit 1 with nothing. */
  answer?: { exitCode: number; out?: Record<string, unknown> }
  /** The bearers api-info hands out, one per resolution ('' is signed out). */
  bearers?: string[]
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
  const rest = restBeneath(on, { list: listed(), ...(beneath.bearers ? { bearers: beneath.bearers } : {}) })
  /** Every `answer` the strip ran, as argv. */
  const answers: string[][] = []
  on('process.run', async (_, e) => {
    const info = rest.apiInfo(e.argv)
    if (info) return info
    if (e.argv[2] === 'paths' && beneath.paths) return ran(0, JSON.stringify(beneath.paths) + '\n')
    if (e.argv[2] === 'answer') {
      answers.push([...e.argv])
      const a = beneath.answer ?? { exitCode: 1 }
      return ran(a.exitCode, a.out ? JSON.stringify(a.out) + '\n' : '')
    }
    return ran(1)
  })
  on('ui.blit', async () => ({ value: {} }))
  await $.session.start({ cwd: CWD, surface: 'terminal', isInteractive: true })
  await settle()
  const band = await $.ui.mount({ plugin: PLUGIN, surface: 'terminal', component: 'AbovePrompt', props: BAND })
  await settle()
  await band.redraw()
  const animal = async () => (await band.find({ type: 'Raster', key: 'companion' })) !== undefined
  const strip = async () => (await band.find({ key: 'memhub-strip' }))?.text
  const asking = async () => (await band.find({ type: 'Text', text: ASK_TEXT })) !== undefined
  const button = async (key: string) => (await band.find({ type: 'Button', key })) !== undefined
  /** Press one of the ask's buttons and let the answer land and redraw. */
  const press = async (key: string) => {
    await band.press({ key })
    await settle()
    await band.redraw()
  }
  const fire = async (note: FireNote) => {
    state.write('fires', [note])
    await band.redraw()
  }
  return { band, store, answers, animal, strip, asking, button, press, fire }
}

const YES = { exitCode: 0, out: { identity: 'ClientCo/app', enabled: true, declined: false, path: '/home/tester/.config/memhub-plugin/repos.json' } }
const NO = { exitCode: 0, out: { identity: 'ClientCo/app', enabled: false, declined: true, path: '/home/tester/.config/memhub-plugin/repos.json' } }

describe('when the strip asks', () => {
  test('an unlisted, undeclined checkout, signed in: the one row with 1 Yes / 2 No / 3 Later, and no quiet line', async ($, on) => {
    const { animal, strip, asking, button } = await started($, on, { paths: UNLISTED })
    expect(await animal()).toBe(false)
    expect(await asking(), 'the ask').toBe(true)
    expect(await button('enable-yes'), '1 Yes').toBe(true)
    expect(await button('enable-no'), '2 No').toBe(true)
    expect(await button('enable-later'), '3 Later').toBe(true)
    expect(await strip(), 'the quiet line stands aside').toBeUndefined()
  })

  test('never when the repository is listed', async ($, on) => {
    const { asking, strip } = await started($, on, { paths: LISTED })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe('MemHub · capturing')
  })

  test('never when the person declined it before', async ($, on) => {
    const { asking, strip } = await started($, on, { paths: DECLINED })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe(QUIET_INACTIVE)
  })

  test('never with no repos.json', async ($, on) => {
    const { asking, strip } = await started($, on, { paths: NO_FILE })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe('MemHub · capturing')
  })

  test('never outside any checkout: there is no repository to list', async ($, on) => {
    const { asking, strip } = await started($, on, { paths: OUTSIDE })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe(`MemHub · inactive here: not in a repository (/${PLUGIN}:repos add . inside one)`)
  })

  test('never off an older CLI that says nothing of restricted, declined or identity', async ($, on) => {
    const { asking, strip } = await started($, on, { paths: OLDER_CLI })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe(QUIET_INACTIVE)
  })

  test('not signed in wins: the not-signed-in line, no ask — until a later poll finds the sign-in', async ($, on) => {
    const { band, asking, strip } = await started($, on, { paths: UNLISTED, bearers: ['', 'mhk_now'] })
    expect(await asking()).toBe(false)
    expect(await strip()).toBe('MemHub · not signed in · /memhub:login')
    // /memhub:login ran meanwhile: the turn's end polls again, api-info now has a key
    await $.turn.complete(TURN_END)
    await settle()
    await band.redraw()
    expect(await asking(), 'asked once signed in').toBe(true)
    expect(await strip()).toBeUndefined()
  })

  test('a fire stands ahead of the ask, and the ask is back once the fire has gone', async ($, on) => {
    const { band, asking, strip, fire } = await started($, on, { paths: UNLISTED })
    await fire({ ruleId: 'b43d6914-4cb3-4a91-84ad-cadbeb6dcfe4', line: '📏 Rule fired: Keep tests fast', rule: 'Keep tests fast', isBlocked: false, at: 1_000 })
    expect(await strip()).toBe('📏 Rule fired: Keep tests fast')
    expect(await asking()).toBe(false)
    await $.turn.complete(TURN_END)
    await $.turn.complete(TURN_END)
    await band.redraw()
    expect(await asking()).toBe(true)
  })
})

describe('what each key does', () => {
  test('1 Yes lists the repository through the CLI, and the strip flips to the quiet line at once', async ($, on) => {
    const { band, store, answers, asking, strip, animal, press } = await started($, on, { paths: UNLISTED, answer: YES })
    await press('enable-yes')
    expect(answers).toHaveLength(1)
    const argv = answers[0]!
    expect(argv[0]).toBe('python3')
    expect(argv[1]).toMatch(/\/scripts\/rulebook_mod_cli\.py$/)
    expect(argv.slice(2)).toEqual(['answer', '--env', 'staging', '--cwd', CWD, '--say', 'yes'])
    expect(await asking(), 'the ask is gone').toBe(false)
    expect(await strip(), 'the quiet line is the active one').toBe('MemHub · capturing')
    expect(await animal(), 'the option is off: still the strip').toBe(false)
    // ctx.active() says enabled now: /goose shows the animal instead of saying MemHub is inactive here
    const r = await $.command.run({ command: 'goose', args: '', ...RUN })
    expect('text' in r ? r.text : '').not.toMatch(/inactive/)
    await band.redraw()
    expect(await animal()).toBe(true)
    expect(store['companion.enabled']).toBe(true)
  })

  test('1 Yes with the companion option on: the animal, as in any listed repository', { options: { companion: true } }, async ($, on) => {
    const { animal, asking, press } = await started($, on, { paths: UNLISTED, answer: YES })
    expect(await animal(), 'inactive here: the strip, with the ask').toBe(false)
    expect(await asking()).toBe(true)
    await press('enable-yes')
    expect(await asking()).toBe(false)
    expect(await animal()).toBe(true)
  })

  test('2 No declines the repository through the CLI; the ask is gone and the quiet line says inactive', async ($, on) => {
    const { answers, asking, strip, press } = await started($, on, { paths: UNLISTED, answer: NO })
    await press('enable-no')
    expect(answers.map(a => a.slice(2))).toEqual([['answer', '--env', 'staging', '--cwd', CWD, '--say', 'no']])
    expect(await asking()).toBe(false)
    expect(await strip()).toBe(QUIET_INACTIVE)
    // and the repository is inactive still: /goose says why, and stores nothing
    const r = await $.command.run({ command: 'goose', args: '', ...RUN })
    expect('text' in r ? r.text : '').toMatch(/inactive in this repository/)
  })

  test('3 Later hides the ask for this session and writes nothing', async ($, on) => {
    const { band, answers, asking, strip, press } = await started($, on, { paths: UNLISTED })
    await press('enable-later')
    expect(answers).toEqual([])
    expect(await asking()).toBe(false)
    expect(await strip()).toBe(QUIET_INACTIVE)
    // and stays hidden through the session's turns
    await $.turn.complete(TURN_END)
    await band.redraw()
    expect(await asking()).toBe(false)
  })

  test('a write that failed leaves the ask up, so it can be tried again', async ($, on) => {
    const { answers, asking, press } = await started($, on, { paths: UNLISTED, answer: { exitCode: 1 } })
    await press('enable-yes')
    expect(answers).toHaveLength(1)
    expect(await asking(), 'still asking').toBe(true)
    await press('enable-yes')
    expect(answers, 'pressed again: asked again').toHaveLength(2)
  })
})
