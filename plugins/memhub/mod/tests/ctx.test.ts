// mod/ctx.ts makeCtx: the credential the mod's parts share, read from
// `rulebook_mod_cli.py api-info` — its base, its bearer, and the Studio web
// app paired with the base (`studio`), which the companion links rules to.

import { describe, expect, test } from 'claude-code/testing'

import { envOf, makeCtx, MOD_CLI } from '../ctx'
import { fakeIo, ok, ROOT } from './fakes'

describe('makeCtx', () => {
  test('api-info is asked once, for this env, and its studio comes through', async () => {
    const io = fakeIo({
      onRun: () => ok(JSON.stringify({ base: 'https://api.example.test/', bearer: 'tok', studio: 'https://app.example.test/' })),
    })
    const ctx = makeCtx(io, 'staging')
    expect(await ctx.api()).toEqual({ base: 'https://api.example.test', bearer: 'tok', studio: 'https://app.example.test' })
    await ctx.api()
    expect(io.runs.map(r => r.argv)).toEqual([['python3', `${ROOT}/${MOD_CLI}`, 'api-info', '--env', 'staging']])
  })

  test('an API with no known Studio has no studio field', async () => {
    for (const studio of ['', undefined, 42]) {
      const ctx = makeCtx(fakeIo({ onRun: () => ok(JSON.stringify({ base: 'https://api.example.test', bearer: 'tok', studio })) }), 'prod')
      expect(await ctx.api()).toEqual({ base: 'https://api.example.test', bearer: 'tok' })
    }
  })

  test('forgetApi asks again; signed out is undefined', async () => {
    let bearer = 'one'
    const io = fakeIo({ onRun: () => ok(JSON.stringify(bearer ? { base: 'https://api.example.test', bearer } : {})) })
    const ctx = makeCtx(io, 'staging')
    expect((await ctx.api())?.bearer).toBe('one')
    bearer = ''
    ctx.forgetApi()
    expect(await ctx.api()).toBeUndefined()
    expect(io.runs).toHaveLength(2)
  })

  test('signIn: in with a credential, out for api-info\'s {}, unknown when it cannot answer', async () => {
    const answers: [number, string][] = [
      [0, JSON.stringify({ base: 'https://api.example.test', bearer: 'tok' })],
      [0, '{}'],
      [1, JSON.stringify({ error: 'ModuleNotFoundError: x' })],
      [0, 'not json'],
    ]
    const want = ['in', 'out', 'unknown', 'unknown']
    for (const [i, [exitCode, stdout]] of answers.entries()) {
      const io = fakeIo({ onRun: () => ({ ...ok(stdout), exitCode }) })
      const ctx = makeCtx(io, 'staging')
      expect(ctx.signIn(), 'before any lookup').toBe('unknown')
      await ctx.api()
      expect(ctx.signIn()).toBe(want[i])
    }
  })

  test('signIn: a lookup overtaken by a forgetApi() and a newer one does not overwrite its answer', async () => {
    const gates: ((stdout: string) => void)[] = []
    const io = fakeIo({ onRun: () => new Promise(resolve => gates.push(stdout => resolve(ok(stdout)))) })
    const ctx = makeCtx(io, 'staging')
    const first = ctx.api()
    ctx.forgetApi()
    const second = ctx.api()
    await new Promise(r => setTimeout(r, 0))
    gates[1]!(JSON.stringify({ base: 'https://api.example.test', bearer: 'tok' }))
    await second
    gates[0]!('{}')
    await first
    expect(ctx.signIn()).toBe('in')
  })

  test('active(): waits for the first setActive, then answers the latest (the strip\'s Yes sets it again)', async () => {
    const ctx = makeCtx(fakeIo(), 'staging')
    let settled = false
    const first = ctx.active().then(a => {
      settled = true
      return a
    })
    await Promise.resolve()
    expect(settled, 'nothing has answered yet').toBe(false)
    ctx.setActive({ enabled: false, repo: 'app', restricted: true, declined: false, identity: 'Acme/app' })
    expect(await first).toEqual({ enabled: false, repo: 'app', restricted: true, declined: false, identity: 'Acme/app' })
    ctx.setActive({ enabled: true, repo: 'app', restricted: true, declined: false, identity: 'Acme/app' })
    expect((await ctx.active()).enabled, 'the latest answer').toBe(true)
  })

  test('the prod export renames the plugin, and the env follows the name', () => {
    expect(envOf('memhub-staging')).toBe('staging')
    expect(envOf('memhub')).toBe('prod')
    expect(envOf('something-else')).toBeUndefined()
  })
})
