"""Synthetic Claude Code session trees for the agent-capture suites.

Not a suite itself (no ``_test`` suffix). ``mirror_9b073366`` builds a tree
shaped like the real session 9b073366 (MemHub-Backend agent-sessions spec
§10 acceptance): 10 Agent-tool agents ending in ``SubagentHandback``, 5
MemHub harness forks, and 3 dynamic-workflow runs with 11, 46 and 7 agents,
one of them resumed (16 agents of an earlier attempt: 11 done, 5 stopped).
The other builders add the shapes that session does not have.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness_stop import FORK_MARK

SESSION = '9b073366-0000-4000-8000-000000000001'
SECRET = 'mhk_' + 'A' * 40

T0 = datetime(2026, 10, 14, 19, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))


def user(text_or_blocks, uuid, at=None):
    record = {'type': 'user', 'uuid': uuid, 'sessionId': SESSION,
              'message': {'role': 'user', 'content': text_or_blocks}}
    if at is not None:
        record['timestamp'] = iso(at)
    return record


def assistant(request_id, blocks, uuid, at=None, stop_reason=None, model='claude-opus-5-5'):
    record = {'type': 'assistant', 'uuid': uuid, 'requestId': request_id,
              'message': {'role': 'assistant', 'id': f'msg_{request_id}', 'model': model,
                          'content': blocks, 'stop_reason': stop_reason}}
    if at is not None:
        record['timestamp'] = iso(at)
    return record


def tool_use(tool_use_id, name, **input_):
    return {'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': input_}


def tool_result(tool_use_id, text, is_error=False):
    block = {'type': 'tool_result', 'tool_use_id': tool_use_id,
             'content': [{'type': 'text', 'text': text}]}
    if is_error:
        block['is_error'] = True
    return block


class Tree:
    """A project directory holding one session and everything under it."""

    def __init__(self, root: Path, session: str = SESSION):
        self.session = session
        self.root = root
        self.transcript = root / f'{session}.jsonl'
        self.dir = root / session
        self.sub = self.dir / 'subagents'
        self.sub.mkdir(parents=True, exist_ok=True)
        self.records: list[dict] = []

    def write_session(self):
        write_jsonl(self.transcript, self.records)

    def agent(self, agent_id, records, run_id=None, **meta):
        base = self.sub / 'workflows' / run_id if run_id else self.sub
        write_jsonl(base / f'agent-{agent_id}.jsonl', records)
        (base / f'agent-{agent_id}.meta.json').write_text(json.dumps(meta))

    def journal(self, run_id, events):
        write_jsonl(self.sub / 'workflows' / run_id / 'journal.jsonl',
                    [{'type': 'launched'}] + events)

    def run_record(self, run_id, record):
        path = self.dir / 'workflows' / f'{run_id}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    def script(self, name, run_id, phases):
        path = self.dir / 'workflows' / 'scripts' / f'{name}-{run_id}.js'
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = ''.join(f"    {{ title: '{t}', detail: 'about {t}' }},\n" for t in phases)
        path.write_text(f"export const meta = {{\n  name: '{name}',\n  phases: [\n{rows}  ],\n}}\n\n"
                        "const X = `title: 'Not a phase'`\n")


# ── the agents ────────────────────────────────────────────────────────────


def tool_agent_records(agent_id, at, prompt='Audit the db operations'):
    return [
        user(prompt, f'{agent_id}-u1', at),
        assistant(f'req_{agent_id}_1', [tool_use(f'toolu_{agent_id}_b', 'Bash', command='ls')],
                  f'{agent_id}-a1', at + timedelta(seconds=1), 'tool_use'),
        user([tool_result(f'toolu_{agent_id}_b', 'file')], f'{agent_id}-u2', at + timedelta(seconds=2)),
        assistant(f'req_{agent_id}_2', [{'type': 'thinking', 'thinking': ''}],
                  f'{agent_id}-a2', at + timedelta(seconds=3)),
        assistant(f'req_{agent_id}_2',
                  [tool_use(f'toolu_{agent_id}_h', 'SubagentHandback',
                            message=f'Findings of {agent_id}: ' + 'x' * 2500)],
                  f'{agent_id}-a3', at + timedelta(seconds=4)),
    ]


RELAY = ('[Workflow harness — user request] The harness relays, verbatim and indented below, '
         'the user request.\n  do the thing')
COMPUTED = ('[Workflow harness — computed task] The task text below was computed at runtime.'
            ' The computed task text follows:\n  \n  ## Task\n  Do {label}.')


def workflow_agent_records(agent_id, label, at, done=True):
    records = [
        user(RELAY, f'{agent_id}-u1', at),
        user(COMPUTED.format(label=label), f'{agent_id}-u2', at),
        assistant(f'req_{agent_id}_1', [tool_use(f'toolu_{agent_id}_r', 'Read', file_path='a')],
                  f'{agent_id}-a1', at + timedelta(seconds=1), 'tool_use'),
        user([tool_result(f'toolu_{agent_id}_r', 'ok')], f'{agent_id}-u3', at + timedelta(seconds=2)),
    ]
    if done:
        records.append(assistant(
            f'req_{agent_id}_2',
            [tool_use(f'toolu_{agent_id}_s', 'StructuredOutput', answer=f'{label} done')],
            f'{agent_id}-a2', at + timedelta(seconds=3)))
    else:
        records.append(assistant(f'req_{agent_id}_2', [{'type': 'thinking', 'thinking': ''}],
                                 f'{agent_id}-a2', at + timedelta(seconds=3)))
    return records


def fork_records(agent_id, owner_request, spawn_id, at, owner_blocks=None):
    """A fork as Claude Code writes it: a fork-context-ref, a copy of the
    owner's spawning response, then the spawn's result plus the directive."""
    return [
        {'type': 'fork-context-ref', 'agentId': agent_id, 'parentSessionId': SESSION},
        assistant(owner_request, owner_blocks or [tool_use(spawn_id, 'Agent', subagent_type='fork',
                                                           description='x', prompt='p')],
                  f'{agent_id}-copy', at),
        user([tool_result(spawn_id, 'Fork started — processing in background'),
              {'type': 'text', 'text': '<fork-boilerplate>\nYou are a worker fork.'}],
             f'{agent_id}-u1', at + timedelta(seconds=1)),
        assistant(f'req_{agent_id}_1', [{'type': 'text', 'text': f'{agent_id} finished'}],
                  f'{agent_id}-a1', at + timedelta(seconds=2), 'end_turn'),
    ]


# ── the 9b073366 mirror ───────────────────────────────────────────────────

TOOL_AGENTS = [f'a10{i:02d}' for i in range(10)]
TOOL_USE_IDS = [f'toolu_T{i}' for i in range(10)]
HARNESS_FORKS = [f'af{i:02d}' for i in range(5)]

RUN_AUDIT = 'wf_65b3bc6f-33a'
RUN_PERF = 'wf_02c95eb8-149'
RUN_SPLIT = 'wf_d2e94298-cb4'

LAUNCH_AUDIT = 'toolu_013TP1PbjRCigMsbKUPaCjdY'
LAUNCH_PERF = 'toolu_015UjVe4NghhwXHAsHBAGSeY'
RESUME_PERF = 'toolu_019oXcGkg2DaWicntiHey512'
LAUNCH_SPLIT = 'toolu_01EG4dRCsNyQbMUakn4ueaCs'

PERF_STOPPED = ['ap90', 'ap91', 'ap92', 'ap93', 'ap94']


def _workflow_call(tree, tool_use_id, run_id, task_id, at, with_path=True):
    tree.records.append(assistant(f'req_{tool_use_id}', [tool_use(tool_use_id, 'Workflow', script='…')],
                                  f'{tool_use_id}-a', at, 'tool_use'))
    text = f'Workflow launched in background. Task ID: {task_id}\nSummary: s'
    if with_path:
        text += f'\nTranscript dir: {tree.dir}/subagents/workflows/{run_id}'
    tree.records.append(user([tool_result(tool_use_id, text)], f'{tool_use_id}-r',
                             at + timedelta(seconds=1)))


def _run(tree, run_id, name, task_id, start, duration_ms, phases, roster, *, extra=None):
    """``roster``: [(agent_id, label, phase_index)], all done, in the record."""
    progress = [{'type': 'workflow_phase', 'index': i + 1, 'title': t} for i, (t, _) in enumerate(phases)]
    for n, (aid, label, pidx) in enumerate(roster, 1):
        progress.append({'type': 'workflow_agent', 'index': n, 'label': label, 'phaseIndex': pidx,
                         'phaseTitle': phases[pidx - 1][0], 'agentId': aid,
                         'model': 'claude-opus-5-5[1m]', 'state': 'done', 'startedAt': ms(start),
                         'queuedAt': ms(start), 'attempt': 1, 'lastToolName': 'StructuredOutput',
                         'lastToolSummary': '~/secret/path', 'promptPreview': 'prompt',
                         'resultPreview': 'result', 'tokens': 119183, 'toolCalls': 20,
                         'durationMs': 396520})
    record = {'runId': run_id, 'taskId': task_id, 'workflowName': name, 'status': 'completed',
              'startTime': ms(start), 'durationMs': duration_ms, 'agentCount': len(roster),
              'script': 'export const meta = {}', 'scriptPath': '/x.js', 'logs': ['log'],
              'summary': f'Summary of {name} with key {SECRET}', 'result': {'pr_url': 'https://x/1'},
              'phases': [{'title': t, 'detail': d} for t, d in phases],
              'defaultModel': 'claude-opus-5-5[1m]', 'workflowProgress': progress,
              'totalTokens': 959995, 'totalToolCalls': 251}
    if extra:
        record.update(extra)
    tree.run_record(run_id, record)
    for aid, label, pidx in roster:
        tree.agent(aid, workflow_agent_records(aid, label, start + timedelta(seconds=5)), run_id,
                   agentType='workflow-subagent', description=label,
                   workflowPhase=phases[pidx - 1][0], spawnDepth=1, requestShape='foreground')


def mirror_9b073366(root: Path) -> Tree:
    tree = Tree(root)
    at = T0
    tree.records.append(user('audit db operations post sessions tab updates.', 'p0', at))
    # 10 Agent-tool agents, each in its own response.
    for i, (aid, tid) in enumerate(zip(TOOL_AGENTS, TOOL_USE_IDS)):
        t = at + timedelta(minutes=i + 1)
        tree.records.append(assistant(f'req_spawn_{i}', [tool_use(tid, 'Agent', subagent_type='general-purpose',
                                                                  description=f'audit {i}',
                                                                  prompt=f'Audit area {i}')],
                                      f'spawn-{i}', t, 'tool_use'))
        tree.records.append(user([tool_result(tid, 'Async agent launched')], f'spawn-{i}-r', t))
        tree.agent(aid, tool_agent_records(aid, t), agentType='general-purpose',
                   description=f'audit {i}', toolUseId=tid, spawnDepth=1)
    # 5 MemHub harness forks.
    for i, aid in enumerate(HARNESS_FORKS):
        t = at + timedelta(minutes=20 + i)
        tid = f'toolu_H{i}'
        tree.records.append(assistant(f'req_fork_{i}', [tool_use(tid, 'Agent', subagent_type='fork',
                                                                 description='rule pass',
                                                                 prompt=f'{FORK_MARK}, moment {SESSION}#{i}')],
                                      f'fork-{i}', t, 'tool_use'))
        tree.records.append(user([tool_result(tid, 'Fork started')], f'fork-{i}-r', t))
        tree.agent(aid, fork_records(aid, f'req_fork_{i}', tid, t), agentType='fork',
                   description='Review the diff', toolUseId=tid, spawnDepth=1)

    # Run 1: 4 phases, 11 agents (3/4/3/1).
    s1 = at + timedelta(minutes=45)
    _workflow_call(tree, LAUNCH_AUDIT, RUN_AUDIT, 'w2rpn14vi', s1)
    phases1 = [('Map', 'map it'), ('Audit', 'audit it'), ('Verify', 'verify'), ('Plan', 'plan')]
    roster1 = [(f'aa{n:02d}', f'{t.lower()}:{n}', p) for n, (t, p) in
               enumerate([(phases1[p - 1][0], p) for p in [1] * 3 + [2] * 4 + [3] * 3 + [4]])]
    _run(tree, RUN_AUDIT, 'studio-pages-server-efficiency-audit', 'w2rpn14vi', s1, 975534,
         phases1, roster1)
    tree.journal(RUN_AUDIT, [e for aid, label, p in roster1 for e in (
        {'type': 'started', 'agentId': aid, 'label': label, 'phase': phases1[p - 1][0]},
        {'type': 'result', 'agentId': aid, 'result': {}})])

    # Run 2: launched, then resumed; the record describes the resumed attempt.
    s2a = at + timedelta(hours=3)
    s2 = at + timedelta(hours=3, minutes=50)
    _workflow_call(tree, LAUNCH_PERF, RUN_PERF, 'w7gjcrdzr', s2a)
    _workflow_call(tree, RESUME_PERF, RUN_PERF, 'we6rbk483', s2 - timedelta(seconds=1),
                   with_path=False)
    phases2 = [('Implement', 'impl'), ('Review', 'review'), ('Ship', 'ship')]
    roster2 = [(f'ap{n:02d}', f'{phases2[p - 1][0].lower()}:{n}', p)
               for n, p in enumerate([1] * 10 + [2] * 10 + [3] * 10)]
    _run(tree, RUN_PERF, 'studio-dashboards-perf-fixes', 'we6rbk483', s2, 4873930, phases2, roster2)
    earlier = [(f'ap{n}', f'implement:{n}', 1) for n in range(60, 68)] \
        + [(f'ap{n}', f'review:{n}', 2) for n in range(70, 71)] \
        + [(f'ap{n}', f'ship:{n}', 3) for n in range(80, 82)]
    earlier = earlier[:11]
    stopped = [(PERF_STOPPED[0], 'implement:s0', 1), (PERF_STOPPED[1], 'review:s1', 2),
               (PERF_STOPPED[2], 'review:s2', 2), (PERF_STOPPED[3], 'ship:s3', 3),
               (PERF_STOPPED[4], 'ship:s4', 3)]
    events = []
    for aid, label, p in earlier + stopped:
        done = (aid, label, p) in earlier
        tree.agent(aid, workflow_agent_records(aid, label, s2a + timedelta(minutes=1), done=done),
                   RUN_PERF, agentType='workflow-subagent', description=label,
                   workflowPhase=phases2[p - 1][0], spawnDepth=1)
        events.append({'type': 'started', 'agentId': aid, 'label': label, 'phase': phases2[p - 1][0]})
        if done:
            events.append({'type': 'result', 'agentId': aid, 'result': {}})
    for aid, label, p in roster2:
        events.append({'type': 'started', 'agentId': aid, 'label': label, 'phase': phases2[p - 1][0]})
        events.append({'type': 'result', 'agentId': aid, 'result': {}})
    tree.journal(RUN_PERF, events)

    # Run 3: 5 phases, 7 agents (2/1/1/2/1).
    s3 = at + timedelta(hours=24, minutes=47)
    _workflow_call(tree, LAUNCH_SPLIT, RUN_SPLIT, 'wytn8hhue', s3 - timedelta(seconds=1))
    phases3 = [('Design', 'two designs'), ('Judge', 'pick one'), ('Implement', 'build'),
               ('Review', 'review'), ('Ship', 'ship')]
    labels3 = [('design:A', 1), ('design:B', 1), ('judge', 2), ('implement', 3),
               ('review:equivalence', 4), ('review:migration', 4), ('ship', 5)]
    roster3 = [(f'as{n:02d}', label, p) for n, (label, p) in enumerate(labels3)]
    _run(tree, RUN_SPLIT, 'investment-split-rollup', 'wytn8hhue', s3, 3893645, phases3, roster3)
    tree.journal(RUN_SPLIT, [e for aid, label, p in roster3 for e in (
        {'type': 'started', 'agentId': aid, 'label': label, 'phase': phases3[p - 1][0]},
        {'type': 'result', 'agentId': aid, 'result': {}})])
    tree.write_session()
    return tree


def journal_only_run(tree: Tree, run_id='wf_inprogress-1', launch='toolu_Wlive',
                     at=T0 + timedelta(hours=30)):
    """A run still going: no record yet, a script with 3 phases, phase 1's two
    agents done, phase 2 not started."""
    _workflow_call(tree, launch, run_id, 'tlive', at)
    tree.script('live-run', run_id, ['Plan', 'Build', 'Ship'])
    tree.journal(run_id, [
        {'type': 'started', 'agentId': 'al01', 'label': 'plan:a', 'phase': 'Plan'},
        {'type': 'started', 'agentId': 'al02', 'label': 'plan:b', 'phase': 'Plan'},
        {'type': 'result', 'agentId': 'al01', 'result': {}},
        {'type': 'result', 'agentId': 'al02', 'result': {}},
    ])
    for aid, label in (('al01', 'plan:a'), ('al02', 'plan:b')):
        tree.agent(aid, workflow_agent_records(aid, label, at + timedelta(seconds=5)), run_id,
                   agentType='workflow-subagent', description=label, workflowPhase='Plan',
                   spawnDepth=1, model='inherit')
    tree.write_session()
