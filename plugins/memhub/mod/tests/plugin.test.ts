// The composed plugin through the engine: hooks.json loads mod/register.ts,
// which registers the shell and then the companion. With no rule book to
// load (here: no process to ask `rulebook_mod_cli.py paths`), the shell must
// claim nothing, leave every call to Python untouched, and say it fell back.

import { expect, mock, test } from 'claude-code/testing'

import { FELL_BACK } from '../claims'

test('with no book to load: no lane claimed, tool calls pass untouched, the fallback said', async ($, on) => {
  mock.env(on, {})
  const envWrites: [string, string | undefined][] = []
  on('env.set', async (_, e) => {
    envWrites.push([e.name, e.value])
    return { value: undefined }
  })
  const statuses: (string | undefined)[] = []
  on('ui.status', async (_, e) => {
    statuses.push(e.text)
    return { value: undefined }
  })
  on('session.start', async (_, e) => ({ cwd: e.cwd }))
  const seen: unknown[] = []
  on('tool.call', async (_, e) => {
    seen.push(e)
    return { result: 'ran', text: 'ran' }
  })

  await $.session.start({ cwd: '/work', surface: null, isInteractive: false })
  const r = await $.tool.call({ tool: 'Bash', command: 'git push -f' })

  expect(envWrites.filter(([, v]) => v !== undefined)).toEqual([])
  expect(statuses.at(-1)).toBe(FELL_BACK)
  expect(seen).toHaveLength(1)
  expect((seen[0] as { command: string }).command).toBe('git push -f')
  expect(r).toMatchObject({ text: 'ran' })
  expect('context' in r ? r.context : undefined).toBeUndefined()
})
