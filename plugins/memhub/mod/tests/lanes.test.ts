// The rulebook lanes on mod events (spec §4.2–§4.5), with a fake engine:
// a gate denies without `next`, advice and post context ride `r.context`,
// prompt rules ride the prompt's context, and the posture preamble is one
// isMeta row per session id.

import { describe, expect, test } from 'claude-code/testing'

import { boot } from '../register'
import { BOOK, EMPTY, fakeCtx, fakeEngine, fakeIo, fire, toHook } from './fakes'

const BOOK_TEXT = JSON.stringify({ etag: '"v1"', fetched_at: null, rules: [{ id: 'r1' }] })

async function ready(pastStartup = true) {
  const io = fakeIo()
  io.files.set(BOOK, BOOK_TEXT)
  const engine = fakeEngine()
  const shell = await boot(io, fakeCtx(io), () => engine, { cwd: '/work', pastStartup, toHook })
  await shell.ready
  return { io, engine, shell }
}

describe('tool.call', () => {
  test('a gate denies WITHOUT calling next; the line goes to the transcript, not to the model', async () => {
    const { engine, shell, io } = await ready()
    const gate = fire('no-force-push', true)
    engine.verdicts.pre = { deny: 'Blocked by the XTrace team rulebook:\n- [no-force-push] never force-push', context: [], fires: [gate], ledger: [] }
    let nextCalls = 0
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'git push -f' }, async () => {
      nextCalls += 1
      return { result: 'ran', text: 'ran' }
    })
    expect(nextCalls).toBe(0)
    expect('deny' in r && r.deny).toBe(
      `Blocked by the XTrace team rulebook:\n- [no-force-push] never force-push`,
    )
    expect(engine.calls.post).toHaveLength(0)
    expect(io.logs).toEqual([gate.line])
    // D5: the disclosure is a transcript system row the model never reads
    expect(io.appends.filter(a => a.type === 'system').map(a => a.text)).toEqual([gate.line])
  })

  test('the call event carries the tool, its input without the reserved keys, the subagent id and the tool_use id', async () => {
    const { engine, shell } = await ready()
    await shell.lanes.toolCall({ tool: 'Edit', tool_use_id: 'tu1', agentId: 'ag1', file_path: '/a.ts', old_string: 'a', new_string: 'b' }, async () => ({
      result: {},
      text: 'edited',
    }))
    expect(engine.calls.pre[0]).toEqual({
      phase: 'pre',
      tool: 'Edit',
      input: { file_path: '/a.ts', old_string: 'a', new_string: 'b' },
      sessionId: 'sess-1',
      cwd: '/work',
      agentId: 'ag1',
      toolUseId: 'tu1',
    })
    expect(engine.calls.post[0]!.phase).toBe('post')
    expect(engine.calls.post[0]!.result).toEqual({ text: 'edited', isError: undefined })
  })

  test('advice reaches the model through context: after the tool ran, beside what was there', async () => {
    const { engine, shell } = await ready()
    const advice = fire('small-diffs')
    engine.verdicts.pre = { context: ['## XTrace Rulebook', '- **[small-diffs]** keep diffs small'], fires: [advice], ledger: [] }
    let nextCalls = 0
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'git commit -am x' }, async () => {
      nextCalls += 1
      return { result: 'ok', text: 'ok', context: ['from beneath'] }
    })
    expect(nextCalls).toBe(1)
    expect(r).toEqual({
      result: 'ok',
      text: 'ok',
      context: ['from beneath', `## XTrace Rulebook\n- **[small-diffs]** keep diffs small`],
    })
  })

  test('the post lane reads the result and its context is merged after the pre advice', async () => {
    const { engine, shell } = await ready()
    engine.verdicts.pre = { context: ['pre advice'], fires: [], ledger: [] }
    const failed = fire('read-the-error')
    engine.verdicts.post = { context: ['post note'], fires: [failed], ledger: [] }
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'pytest' }, async () => ({
      result: 'boom',
      text: '1 failed',
      isError: true as const,
    }))
    expect(engine.calls.post[0]!.result).toEqual({ text: '1 failed', isError: true })
    expect(r).toEqual({
      result: 'boom',
      text: '1 failed',
      isError: true,
      context: ['pre advice', `post note`],
    })
  })

  test('a persisted Bash output: the post lane reads the record (stderr, then stdout), not the preview', async () => {
    const { engine, shell } = await ready()
    const stdout = 'PASSED t\n'.repeat(3000) + 'SILENT FAIL\n'
    const preview = '<persisted-output>\nOutput too large (27KB).\n\nPreview (first 2KB):\nPASSED t\n...\n</persisted-output>'
    const answered = { result: { stdout, stderr: 'warn: slow', interrupted: false, persistedOutputPath: '/t/b1.txt' }, text: preview }
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'pytest' }, async () => answered)
    expect(engine.calls.post[0]!.result).toEqual({ text: `warn: slow\n${stdout}`, isError: undefined })
    // What the model reads is untouched.
    expect(r).toBe(answered)
  })

  test('a small Bash output: the record\'s stdout, which is what the result read before', async () => {
    const { engine, shell } = await ready()
    await shell.lanes.toolCall({ tool: 'Bash', command: 'pytest' }, async () => ({
      result: { stdout: '3 passed', stderr: '', interrupted: false }, text: '3 passed',
    }))
    expect(engine.calls.post[0]!.result).toEqual({ text: '3 passed', isError: undefined })
  })

  test('falls back to text: an error string, a record with no output, another tool', async () => {
    const { engine, shell } = await ready()
    const calls: [string, Record<string, unknown>][] = [
      ['Bash', { result: 'Error: Exit code 1\nboom', text: 'Error: Exit code 1\nboom', isError: true as const }],
      ['Bash', { result: { stdout: '', stderr: '', interrupted: false }, text: '(No output)' }],
      ['Bash', { result: { interrupted: false }, text: 'odd record' }],
      ['Edit', { result: { stdout: 'not a Bash record' }, text: 'edited' }],
    ]
    for (const [tool, answered] of calls) {
      await shell.lanes.toolCall({ tool, command: 'x', file_path: '/w/x' }, async () => answered as { text: string })
    }
    expect(engine.calls.post.map(c => c.result!.text)).toEqual(['Error: Exit code 1\nboom', '(No output)', 'odd record', 'edited'])
  })

  test('nothing fired: the result comes back as next answered it', async () => {
    const { shell } = await ready()
    const answered = { result: 'ok', text: 'ok', ref: 7 }
    const r = await shell.lanes.toolCall({ tool: 'Read', file_path: '/x' }, async () => answered)
    expect(r).toBe(answered)
  })

  test('a deny from beneath is returned untouched, and the post lane does not run', async () => {
    const { engine, shell } = await ready()
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'rm -rf /' }, async () => ({ deny: 'settings said no' }))
    expect(r).toEqual({ deny: 'settings said no' })
    expect(engine.calls.post).toHaveLength(0)
  })
})

describe('prompt.submit', () => {
  test('prompt rules ride the prompt as context; the text is unchanged', async () => {
    const { engine, shell } = await ready()
    const f = fire('cite-the-spec')
    engine.verdicts.prompt = { context: ['## XTrace Rulebook', '- **[cite-the-spec]** cite it'], fires: [f], ledger: [] }
    let passed: { text: string; context?: readonly string[] } | undefined
    await shell.lanes.promptSubmit({ text: 'implement X', context: ['earlier'] }, async e => {
      passed = e
      return e
    })
    expect(engine.calls.prompt).toEqual(['implement X'])
    expect(passed).toEqual({
      text: 'implement X',
      context: ['earlier', `## XTrace Rulebook\n- **[cite-the-spec]** cite it`],
    })
  })

  test('no prompt rule: the prompt passes as it came', async () => {
    const { shell } = await ready()
    const e = { text: 'hi' }
    let passed: unknown
    await shell.lanes.promptSubmit(e, async x => {
      passed = x
      return x
    })
    expect(passed).toBe(e)
  })
})

describe('the session lane', () => {
  test('the startup session is Python’s; a /clear (new id) gets one row, once', async () => {
    const { engine, io, shell } = await ready(false)
    const pass = async (e: { text: string }) => e
    await shell.firstPrompt()
    await shell.lanes.promptSubmit({ text: 'one' }, pass)
    expect(io.appends).toEqual([]) // startup preamble came from Python's SessionStart
    expect(engine.calls.session).toEqual(['sess-1']) // the engine still saw the session start
    io.session = 'sess-2' // /clear: no session.start fires for the new id
    await shell.lanes.promptSubmit({ text: 'two' }, pass)
    await shell.lanes.promptSubmit({ text: 'three' }, pass)
    expect(io.appends).toEqual([{ type: 'user', text: '## posture: keep diffs small' }])
  })

  test('after a hot reload the current session is not given a second row', async () => {
    const { io, shell } = await ready(true)
    await shell.lanes.promptSubmit({ text: 'one' }, async e => e)
    expect(io.appends).toEqual([])
  })

  test('a compaction brings the preamble back once, at the next prompt', async () => {
    const { io, shell } = await ready(true)
    shell.lanes.compacted('sess-1')
    await shell.lanes.promptSubmit({ text: 'a' }, async e => e)
    await shell.lanes.promptSubmit({ text: 'b' }, async e => e)
    expect(io.appends).toHaveLength(1)
    expect(io.appends[0]!.type).toBe('user')
  })

  test('an empty preamble appends nothing', async () => {
    const { engine, io, shell } = await ready(true)
    engine.sessionContext = []
    io.session = 'sess-9'
    await shell.lanes.promptSubmit({ text: 'a' }, async e => e)
    expect(io.appends).toEqual([])
  })

  test('the session row is not written while the lane is Python’s', async () => {
    const { io, shell } = await ready(false) // no first prompt yet: `session` unclaimed
    io.session = 'sess-3'
    await shell.lanes.deliverSession('sess-3')
    expect(io.appends).toEqual([])
    expect(EMPTY.fires).toEqual([])
  })
})

// ── the tool input the engine reads is the tool's own arguments ──────────────
//
// rulebook_hook.py reads `tool_input` (Claude Code's PreToolUse payload: the
// tool's arguments, nothing else): `file_path`, `new_string`, `content`,
// `edits[].new_string`, and `notebook_path` for the checkout. `tool.call`'s
// event is the same arguments spread beside the reserved `tool`,
// `tool_use_id` and `agentId` (claude-code.d.ts ToolCallInput), so the lane
// passes exactly the arguments, under the same keys. The shapes below are the
// d.ts built-in inputs for each tool the lanes match. (This build has no
// MultiEdit tool: claude-code.d.ts BuiltinToolName lists none.)

describe('tool input mapping', () => {
  const calls: [string, Record<string, unknown>][] = [
    ['Edit', { file_path: '/w/a.py', old_string: 'x = 1', new_string: 'print(x)', replace_all: false }],
    ['Write', { file_path: '/w/b.py', content: 'print(1)\n' }],
    ['NotebookEdit', { notebook_path: '/w/n.ipynb', cell_id: 'c1', new_source: 'print(2)', cell_type: 'code', edit_mode: 'replace' }],
    ['Read', { file_path: '/w/c.lock', offset: 1, limit: 20 }],
    ['Bash', { command: 'git push', description: 'Push', timeout: 1000 }],
  ]
  for (const [tool, args] of calls) {
    test(`${tool}: the engine gets the tool's arguments as tool_input, under the hook's keys`, async () => {
      const { engine, shell } = await ready()
      await shell.lanes.toolCall({ tool, tool_use_id: 'tu9', agentId: 'ag9', ...args }, async () => ({ result: {}, text: 'ok' }))
      expect(engine.calls.pre[0]!.input).toEqual(args)
      expect(engine.calls.post[0]!.input).toEqual(args)
    })
  }

  test('the known path fields log nothing', async () => {
    const { shell, io } = await ready()
    for (const [tool, args] of calls) await shell.lanes.toolCall({ tool, ...args }, async () => ({ result: {}, text: 'ok' }))
    expect(io.debugs).toEqual([])
  })

  test('a path tool with no path field we know logs one debug line per session per tool, and is served as before', async () => {
    const { engine, shell, io } = await ready()
    const drifted = { path: '/w/a.py', old_string: 'x', new_string: 'y' }
    for (let i = 0; i < 2; i++) await shell.lanes.toolCall({ tool: 'Edit', ...drifted }, async () => ({ result: {}, text: 'ok' }))
    await shell.lanes.toolCall({ tool: 'Read', path: '/w/c' }, async () => ({ result: {}, text: 'ok' }))
    await shell.lanes.toolCall({ tool: 'Bash', command: 'ls' }, async () => ({ result: {}, text: 'ok' }))
    expect(io.debugs).toHaveLength(2)
    expect(io.debugs[0]).toContain('a Edit tool.call carried no file_path or notebook_path (input keys: new_string, old_string, path)')
    expect(io.debugs[1]).toContain('a Read tool.call')
    expect(io.logs).toEqual([])
    expect(engine.calls.pre[0]!.input).toEqual(drifted)
    expect(engine.calls.pre).toHaveLength(4)
  })
})

describe('a pre hand-off', () => {
  test('still runs the mod post lane after next while post is claimed, so post advice is not lost', async () => {
    const { engine, shell, io } = await ready()
    engine.verdicts.pre = new Error('engine blew up')
    const advice = fire('read-the-error')
    engine.verdicts.post = { context: ['post advice'], fires: [advice], ledger: [] }
    let nextCalls = 0
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'pytest' }, async () => {
      nextCalls += 1
      return { result: 'out', text: 'out' }
    })
    expect(nextCalls).toBe(1)
    expect(engine.calls.post).toHaveLength(1)
    expect(engine.calls.post[0]!.result).toEqual({ text: 'out', isError: undefined })
    expect(r).toEqual({ result: 'out', text: 'out', context: [`post advice`] })
    expect(io.logs).toEqual([advice.line])
  })

  test('a hand-off whose claims write fails still reaches next exactly once', async () => {
    const { engine, shell, io } = await ready()
    engine.verdicts.pre = new Error('engine blew up')
    io.setLanesVar = async () => {
      throw new Error('env.set refused')
    }
    let nextCalls = 0
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'ls' }, async () => {
      nextCalls += 1
      return { result: 'ok', text: 'ok' }
    })
    expect(nextCalls).toBe(1)
    expect(r).toEqual({ result: 'ok', text: 'ok' })
  })
})

describe('Act.record never throws', () => {
  test('a $.ui.log that throws costs neither the call nor the ledger row', async () => {
    const { engine, shell, io } = await ready()
    io.log = () => {
      throw new Error('ui.log refused')
    }
    const advice = fire('small-diffs')
    engine.verdicts.pre = { context: ['advice'], fires: [advice], ledger: [{ fire_id: 'f1', rule_id: 'small-diffs', hook_phase: 'pre', mode: 'advise' }] }
    const r = await shell.lanes.toolCall({ tool: 'Bash', command: 'git commit' }, async () => ({ result: 'ok', text: 'ok' }))
    expect('context' in r && r.context?.length).toBe(1)
    expect(io.runs.some(run => run.argv.includes('log'))).toBe(true)
  })
})
