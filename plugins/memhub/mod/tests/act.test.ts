// The act side (spec §4.6, §4.7, §4.15): the companion's fire feed in
// $.state, the disclosure text, the ledger (a process on fires only), health.

import { describe, expect, test } from 'claude-code/testing'

import { Act, disclosureInstruction, FIRES_CAP, isEventRow, NO_ECHO_NOTE, ruleOfLine, SYSTEM_ROW_DISCLOSURE } from '../act'
import { disclosureLine } from '../engine/rules'
import { StatusLine } from '../claims'
import { MOD_CLI } from '../ctx'
import { fakeCtx, fakeIo, fire, firesOf, ROOT, SESSION } from './fakes'

/** The echo path (system row off): what the Python fallback still does, kept tested. */
function act() {
  const io = fakeIo()
  return { io, act: new Act(io, fakeCtx(io), new StatusLine(io), false) }
}

const logRuns = (io: ReturnType<typeof fakeIo>) => io.runs.filter(r => r.argv[2] === 'log')

describe('the disclosure', () => {
  test('is rulebook_hook.disclosure_instruction, byte for byte', () => {
    // python3 -c 'import rulebook_hook as r; print(repr(r.disclosure_instruction(["📏 Rule fired: a", "⛔️ Rule fired: b"])))'
    const python =
      '\n_Disclose these to the user. Begin your next reply with the following line(s), verbatim and each on its own line, before anything else — including before any tool call narration:_\n📏 Rule fired: a\n⛔️ Rule fired: b\n_This is how the team sees its rules working. Do not paraphrase, do not merge them into a sentence, and do not omit one because it did not change what you were going to do — a rule that fired and changed nothing is exactly the rule the team needs to hear about._'
    expect(disclosureInstruction(['📏 Rule fired: a', '⛔️ Rule fired: b'])).toBe(python)
  })

  test('the transcript system row is on by default now that gate G2 passed', async () => {
    expect(SYSTEM_ROW_DISCLOSURE).toBe(true)
    const io = fakeIo()
    const a = new Act(io, fakeCtx(io), new StatusLine(io))   // the default, as register.ts builds it
    const advice = { context: ['## XTrace Rulebook\n- **[r1]** keep it small'], fires: [fire('r1')], ledger: [] }
    await a.record(advice, SESSION)
    expect(io.appends.filter(r => r.type === 'system')).toEqual([{ type: 'system', text: fire('r1').line }])
    expect(a.modelText(advice)).toBe(advice.context[0])
  })

  test('off (the echo path): no system row, the person still sees the line', async () => {
    const { io, act: a } = act()
    await a.record({ context: [], fires: [fire('r1')], ledger: [] }, SESSION)
    expect(io.appends).toEqual([])
    expect(io.logs).toEqual([fire('r1').line]) // the person's copy
  })

  test('off: the model is told to echo, in context and in a deny', async () => {
    const { io, act: a } = act()
    const advice = { context: ['## XTrace Rulebook\n- **[r1]** keep it small'], fires: [fire('r1')], ledger: [] }
    await a.record(advice, SESSION)
    expect(a.modelText(advice)).toBe(`${advice.context[0]}\n${disclosureInstruction([fire('r1').line])}`)
    const gate = { deny: 'Blocked by the XTrace team rulebook', context: [], fires: [fire('g', true)], ledger: [] }
    await a.record(gate, SESSION)
    expect(a.denyText(gate)).toBe(`${gate.deny}\n${disclosureInstruction([fire('g', true).line])}`)
  })

  test('the rule is the line without its marker', () => {
    expect(ruleOfLine('📏 Rule fired: keep diffs small')).toBe('keep diffs small')
    expect(ruleOfLine('⛔️ Rule fired: never force-push')).toBe('never force-push')
  })
})

describe('the transcript system row (D5, gate G2)', () => {
  function rowAct(over: Parameters<typeof fakeIo>[0] = {}) {
    const io = fakeIo(over)
    return { io, act: new Act(io, fakeCtx(io), new StatusLine(io), true) }
  }

  test('one system row per fire, the disclosure line byte for byte', async () => {
    const { io, act: a } = rowAct()
    const rule = { id: 'r9', _label: 'Ban new direct task creation', text: 'Do not add create_task.' }
    const lines = [disclosureLine(rule), disclosureLine(rule, true)]
    // python3 -c 'import rulebook_hook as r; print(r.disclosure_line({"_label": "Ban new direct task creation"}, blocked=True))'
    expect(lines).toEqual(['📏 Rule fired: Ban new direct task creation', '⛔️ Rule fired: Ban new direct task creation'])
    const fires = lines.map((line, i) => ({ ...fire(`r${i}`, i === 1), line }))
    await a.record({ context: [], fires, ledger: [] }, SESSION)
    expect(io.appends.filter(r => r.type === 'system')).toEqual(lines.map(text => ({ type: 'system', text })))
    expect(io.logs).toEqual(lines) // the person's copy is still $.ui.log
  })

  test('no echo instruction in the context or the deny', async () => {
    const { act: a } = rowAct()
    const advice = { context: ['## XTrace Rulebook\n- **[r1]** keep it small'], fires: [fire('r1')], ledger: [] }
    await a.record(advice, SESSION)
    expect(a.modelText(advice)).toBe(advice.context[0])
    const gate = { deny: 'Blocked by the XTrace team rulebook:\n- [g] no', context: [], fires: [fire('g', true)], ledger: [] }
    await a.record(gate, SESSION)
    expect(a.denyText(gate)).toBe(gate.deny)
    expect(a.denyText(gate)).not.toContain('Disclose these')
  })

  test('the no-echo note: once per session, again after a compaction', async () => {
    const { io, act: a } = rowAct()
    const notes = () => io.appends.filter(r => r.type === 'user')
    await a.record({ context: [], fires: [fire('a')], ledger: [] }, SESSION)
    await a.record({ context: [], fires: [fire('b')], ledger: [] }, SESSION)
    await a.record({ context: [], fires: [], ledger: [] }, SESSION)
    expect(notes()).toEqual([{ type: 'user', text: NO_ECHO_NOTE }])
    expect(io.appends[0]).toEqual({ type: 'user', text: NO_ECHO_NOTE }) // before the first row
    a.compacted(SESSION)
    await a.record({ context: [], fires: [fire('c')], ledger: [] }, SESSION)
    expect(notes()).toHaveLength(2)
    expect(io.appends.filter(r => r.type === 'system')).toHaveLength(3)
  })

  test('parallel fires in one session share one note write', async () => {
    const { io, act: a } = rowAct()
    let release!: () => void
    const held = new Promise<void>(r => (release = r))
    let noteWrites = 0
    const append = io.append
    io.append = async (type, text) => {
      if (type === 'user') {
        noteWrites += 1
        await held // the note write is still in flight
      }
      return append(type, text)
    }
    const both = Promise.all([
      a.record({ context: [], fires: [fire('a')], ledger: [] }, SESSION),
      a.record({ context: [], fires: [fire('b')], ledger: [] }, SESSION),
    ])
    // Let both calls run up to the note before the first write lands.
    for (let i = 0; i < 200 && noteWrites === 0; i++) await Promise.resolve()
    for (let i = 0; i < 200; i++) await Promise.resolve()
    release()
    await both
    expect(noteWrites).toBe(1)
    expect(io.appends.filter(r => r.type === 'user')).toEqual([{ type: 'user', text: NO_ECHO_NOTE }])
    expect(io.appends.filter(r => r.type === 'system')).toHaveLength(2)
  })

  test('one row of two failing: only that line is echoed, not the one already stored', async () => {
    const { io, act: a } = rowAct()
    const append = io.append
    io.append = async (type, text) => {
      if (type === 'system' && text === fire('r2').line) throw new Error('refused')
      return append(type, text)
    }
    const v = { context: ['ctx'], fires: [fire('r1'), fire('r2')], ledger: [] }
    await a.record(v, SESSION)
    expect(a.modelText(v)).toBe(`ctx\n${disclosureInstruction([fire('r2').line])}`)
  })

  test('a row that cannot be written keeps the echo, and says so', async () => {
    const { io, act: a } = rowAct()
    io.append = async type => {
      if (type === 'system') throw new Error('session.append refused: policy')
    }
    const v = { context: ['ctx'], fires: [fire('r1')], ledger: [] }
    await a.record(v, SESSION)
    expect(a.modelText(v)).toBe(`ctx\n${disclosureInstruction([fire('r1').line])}`)
    expect(io.statuses.at(-1)).toContain('MemHub: disclosure')
  })

  test('a note that cannot be written keeps the echo', async () => {
    const { io, act: a } = rowAct()
    io.append = async type => {
      if (type === 'user') throw new Error('refused')
    }
    const v = { context: [], fires: [fire('r1')], ledger: [] }
    await a.record(v, SESSION)
    expect(a.modelText(v)).toBe(disclosureInstruction([fire('r1').line]))
  })
})

describe('$.state fires', () => {
  test('each fire is a FireNote, newest last, capped at 50', async () => {
    const { io, act: a } = act()
    for (let i = 0; i < FIRES_CAP + 7; i++) await a.record({ context: [], fires: [fire(`r${i}`)], ledger: [] }, SESSION)
    await a.record({ deny: 'no', context: [], fires: [fire('gate', true)], ledger: [] }, SESSION)
    const notes = firesOf(io)
    expect(notes).toHaveLength(FIRES_CAP)
    expect(notes[0]!.ruleId).toBe('r8')
    expect(notes.at(-1)).toEqual({
      ruleId: 'gate',
      line: '⛔️ Rule fired: rule gate in twenty words or fewer',
      rule: 'rule gate in twenty words or fewer',
      isBlocked: true,
      at: 1_700_000_000_000,
    })
    expect(notes.at(-2)!.isBlocked).toBe(false)
  })

  test('parallel calls do not lose each other’s fires', async () => {
    const { io, act: a } = act()
    await Promise.all([1, 2, 3, 4].map(i => a.record({ context: [], fires: [fire(`p${i}`)], ledger: [] }, SESSION)))
    expect(firesOf(io).map(n => n.ruleId).sort()).toEqual(['p1', 'p2', 'p3', 'p4'])
  })
})

describe('the ledger', () => {
  test('no fire, no process: events wait for turn.complete', async () => {
    const { io, act: a } = act()
    await a.record({ context: [], fires: [], ledger: [{ event_id: 'e1', kind: 'receipt' }] }, SESSION)
    await a.record({ context: [], fires: [], ledger: [] }, SESSION)
    expect(logRuns(io)).toHaveLength(0)
    await a.flush(SESSION)
    expect(logRuns(io)).toHaveLength(1)
    expect(JSON.parse(logRuns(io)[0]!.stdin!)).toEqual({ session: SESSION, fires: [], events: [{ event_id: 'e1', kind: 'receipt' }] })
    await a.flush(SESSION) // nothing left: no process
    expect(logRuns(io)).toHaveLength(1)
  })

  test('a fire runs the Python ledger at once, carrying the waiting events', async () => {
    const { io, act: a } = act()
    await a.record({ context: [], fires: [], ledger: [{ event_id: 'e1', kind: 'converted' }] }, SESSION)
    const row = { fire_id: 'f1', rule_id: 'r1', mode: 'advise' }
    await a.record({ context: [], fires: [fire('r1')], ledger: [row] }, SESSION)
    const runs = logRuns(io)
    expect(runs).toHaveLength(1)
    expect(runs[0]!.argv).toEqual(['python3', `${ROOT}/${MOD_CLI}`, 'log', '--env', 'staging'])
    expect(JSON.parse(runs[0]!.stdin!)).toEqual({ session: SESSION, fires: [row], events: [{ event_id: 'e1', kind: 'converted' }] })
  })

  test('a failed write keeps the rows for the next flush and says so', async () => {
    const io = fakeIo({ onRun: () => ({ exitCode: 1, stdout: '', stderr: 'locked' }) })
    const a = new Act(io, fakeCtx(io), new StatusLine(io))
    await a.record({ context: [], fires: [fire('r1')], ledger: [{ fire_id: 'f1' }] }, SESSION)
    expect(io.statuses.at(-1)).toContain('MemHub: ledger')
    expect(io.state.health).toContain('ledger')
    io.onRun = () => ({ exitCode: 0, stdout: '', stderr: '' })
    await a.flush(SESSION)
    expect(JSON.parse(io.runs.at(-1)!.stdin!).fires).toEqual([{ fire_id: 'f1' }])
    expect(io.statuses.at(-1)).toBeUndefined() // cleared on the next success
    expect(io.state.health).toBe('')
  })

  test('event rows are told from fire rows', () => {
    expect(isEventRow({ event_id: 'e', kind: 'receipt' })).toBe(true)
    expect(isEventRow({ fire_id: 'f', rule_id: 'r' })).toBe(false)
  })
})

describe('health', () => {
  test('a lane error shows, and its next success clears it', () => {
    const { io, act: a } = act()
    a.unhealthy('pre', new Error('engine blew up'))
    expect(io.statuses.at(-1)).toBe('MemHub: pre — engine blew up')
    a.healthy('post') // another lane's success does not clear it
    expect(io.statuses.at(-1)).toBe('MemHub: pre — engine blew up')
    a.healthy('pre')
    expect(io.statuses.at(-1)).toBeUndefined()
  })
})
