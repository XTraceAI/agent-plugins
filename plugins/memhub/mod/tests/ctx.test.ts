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

  test('the prod export renames the plugin, and the env follows the name', () => {
    expect(envOf('memhub-staging')).toBe('staging')
    expect(envOf('memhub')).toBe('prod')
    expect(envOf('something-else')).toBeUndefined()
  })
})
