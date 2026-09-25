"""Exercise the shared ownership semantics and real git diff probes."""
import os
import json
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts'))
import spec_owns as owns
import rulebook_hook as hook

TEXT = '\ufeff---\r\nspec: "limit"\r\nowns:\r\n  - "app/limit.py" # limit\r\nlast_verified_at: null\r\n---\r\nMaximum 100.\r\n'

def main():
    repo = Path(__file__).resolve().parents[1]
    fixture_bytes = (repo/'contracts/spec-owns.json').read_bytes()
    lock = json.loads((repo/'contracts/rulebook.lock.json').read_text())['spec_owns']
    assert hashlib.sha256(fixture_bytes).hexdigest() == lock['sha256']
    assert hashlib.sha256((repo/'plugins/memhub/scripts/spec_owns.py').read_bytes()).hexdigest() == lock['parser_sha256']
    fixture = json.loads(fixture_bytes)
    with tempfile.TemporaryDirectory() as tmp:
        for path, text in fixture['files'].items():
            target = Path(tmp)/path
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(text)
        specs = owns.load_specs_from_tree(tmp)
        assert [{k:getattr(s,k) for k in ('path','name','owns','last_verified_at')} for s in specs] == fixture['expected']
        hits = owns.owning_specs(fixture['changed'], specs)
        assert [s.path for s,_ in hits if not owns.spec_file_changed(s,fixture['changed'])] == fixture['untouched']

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True)
        git('init', '-b', 'main');git('config', 'user.email', 'test@example.com');git('config', 'user.name', 'Test')
        (root/'app').mkdir();(root/'app/limit.py').write_text('limit = 100\n')
        (root/'docs/specs/retired').mkdir(parents=True)
        (root/'docs/specs/limit.md').write_text(TEXT)
        (root/'docs/specs/retired/old.md').write_text(TEXT)
        git('add', '.');git('commit', '-m', 'baseline');git('checkout', '-b', 'change')
        specs=owns.load_specs_from_tree(root)
        assert len(specs)==1 and specs[0].owns==['app/limit.py']
        assert owns.parse_frontmatter('---\nspec: a\nowns: [a.py, "b.py"]\n---')['owns']==['a.py','b.py']
        (root/'app/limit.py').write_text('limit = 200\n')
        # Probes constructor takes repo root and transcript; branch changes include uncommitted edits.
        probes=hook.Probes(str(root), '')
        rule={'given': {'repo': {'spec_untouched': True, 'spec_dir':'docs/specs'}}}
        assert hook.given_norm(rule['given']) == rule['given']
        assert hook.given_ok(rule,probes)
        assert probes.untouched_specs()[0][0].path=='docs/specs/limit.md'
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from rulebook_hook_test import seed_book, run
        cache = root / "cache"
        seed_book(str(cache), root.name, [{"id":"spec-test", "on":"bash", "rx":r"git\s+push", "fire_scope":"call", "repo_scope":"any", "text":"Update owning spec", "why":"", "given":rule["given"]}])
        env = {"MEMHUB_RULEBOOK_BASE":str(cache), "MEMHUB_RULEBOOK_FETCH":"0"}
        payload = {"cwd":tmp, "session_id":"spec-session", "tool_name":"Bash", "tool_input":{"command":"git push"}}
        code, output = run("pre", payload, env)
        assert code == 0 and "docs/specs/limit.md" in output and "app/limit.py" in output, output
        (root/'docs/specs/limit.md').unlink()
        run("post", {"cwd":tmp,"session_id":"spec-session","tool_name":"Edit", "tool_input":{"file_path":str(root/'docs/specs/limit.md')}}, env)
        event_file = cache/'ledger/events.jsonl'
        deleted_events = [json.loads(line) for line in event_file.read_text().splitlines()] if event_file.exists() else []
        assert not any(e['kind']=='converted' and e['rule_id']=='spec-test' for e in deleted_events)
        (root/'docs/specs/limit.md').write_text(TEXT)
        git('checkout', '-b', 'unrelated')
        (root/'docs/specs/limit.md').write_text(TEXT+'Updated.\n')
        code, output = run("post", {"cwd":tmp,"session_id":"spec-session","tool_name":"Edit", "tool_input":{"file_path":str(root/'docs/specs/limit.md')}}, env)
        event_file = cache/'ledger/events.jsonl'
        events = [json.loads(line) for line in event_file.read_text().splitlines()] if event_file.exists() else []
        assert not any(e['kind']=='converted' and e['rule_id']=='spec-test' for e in events), events
        (root/'docs/specs/limit.md').write_text(TEXT)
        assert "docs/specs/limit.md" in run("pre", payload, env)[1]
        (root/'docs/specs/limit.md').write_text(TEXT+'Updated.\n')
        run("post", {"cwd":tmp,"session_id":"spec-session","tool_name":"Edit", "tool_input":{"file_path":str(root/'docs/specs/limit.md')}}, env)
        events = [json.loads(line) for line in event_file.read_text().splitlines()]
        assert [e['branch'] for e in events if e['kind']=='converted' and e['rule_id']=='spec-test'] == ['unrelated']
        git('checkout', 'change')
        code, output = run("post", {"cwd":tmp,"session_id":"spec-session","tool_name":"Edit", "tool_input":{"file_path":str(root/'docs/specs/limit.md')}}, env)
        events = [json.loads(line) for line in (cache/'ledger/events.jsonl').read_text().splitlines()]
        assert [e['branch'] for e in events if e['kind']=='converted' and e['rule_id']=='spec-test'] == ['unrelated', 'change'], events
        assert run("pre",payload,env)[1].strip() == ""

        assert not hook.given_ok(rule,hook.Probes(str(root), ''))
        assert not hook.given_ok({'given':{'repo':{'spec_untouched':True,'spec_dir':'missing'}}},hook.Probes(str(root),''))
        assert hook.given_norm({'repo':{'spec_untouched':'yes'}}) is None
        assert hook.given_norm({'repo':{'spec_dir':'../outside'}}) is None
        (root/'docs/specs/limit.md').write_text(TEXT)
        git('checkout', '--detach', 'main')
        detached_a = git('rev-parse', 'HEAD').strip()
        detached_payload = {**payload, 'session_id':'detached-test'}
        assert 'docs/specs/limit.md' in run('pre', detached_payload, env)[1]
        git('commit', '--allow-empty', '-m', 'detached B')
        (root/'docs/specs/limit.md').write_text(TEXT+'Updated.\n')
        post = {'cwd':tmp,'session_id':'detached-test','tool_name':'Edit','tool_input':{'file_path':str(root/'docs/specs/limit.md')}}
        run('post', post, env)
        def detached_conversions():
            return [json.loads(line) for line in event_file.read_text().splitlines() if json.loads(line).get('session_id')=='detached-test' and json.loads(line)['kind']=='converted']
        assert not detached_conversions()
        git('checkout', '--detach', detached_a)
        run('post', post, env)
        assert [e['branch'] for e in detached_conversions()] == ['detached@'+detached_a]
        git('checkout', 'change')
        # Git's rename destination alone is insufficient: the removed owned path matters.
        (root/'docs/specs/limit.md').write_text(TEXT)
        (root/'app/limit.py').write_text('limit = 100\n')
        (root/'new').mkdir()
        git('mv', 'app/limit.py', 'new/limit.py')
        rename_probe = hook.Probes(str(root), 'change')
        assert {'app/limit.py', 'new/limit.py'} <= set(rename_probe.diff_paths())
        assert rename_probe.untouched_specs()[0][1] == ['app/limit.py']

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True)
        git('init', '-b', 'main'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
        (root/'app').mkdir(); (root/'docs/specs').mkdir(parents=True)
        for i in range(12):
            (root/f'app/item{i}.py').write_text('limit = 100\n')
            (root/f'docs/specs/item{i}.md').write_text(f'---\nspec: Item {i}\nowns: [app/item{i}.py]\n---\nLimit 100.\n')
        git('add', '.'); git('commit', '-m', 'baseline'); git('checkout', '-b', 'change')
        for i in range(12):
            (root/f'app/item{i}.py').write_text('limit = 200\n')
        cache = root/'cache'
        seed_book(str(cache), root.name, [{"id":"many-specs", "on":"bash", "rx":r"git\s+push", "fire_scope":"branch", "repo_scope":"any", "text":"Update owning specs", "why":"", "given":{"repo":{"spec_untouched":True}}}])
        output = run("pre", {"cwd":tmp,"session_id":"many","tool_name":"Bash","tool_input":{"command":"git push"}}, {"MEMHUB_RULEBOOK_BASE":str(cache),"MEMHUB_RULEBOOK_FETCH":"0"})[1]
        out = json.loads(output)
        context = out['hookSpecificOutput']['additionalContext']
        user = out['systemMessage']
        # The spec list rides INLINE on the rule's own bullet, capped at 3 with the rest counted.
        bullet = [l for l in context.split('\n') if l.startswith('- **[many-specs]**')]
        assert len(bullet) == 1, context
        named = [f'docs/specs/item{i}.md' for i in range(12) if f'docs/specs/item{i}.md' in bullet[0]]
        assert len(named) == 3 and 'Specs not updated: ' in bullet[0] and '(+9 more)' in bullet[0], bullet[0]
        # Every context line is a bullet, a heading, a note or prose — no bare `<spec> owns:` line.
        assert not any(' owns' in l and not l.startswith('- **[') for l in context.split('\n')), context
        # The agent still gets which owned paths each named spec answers for, on the same line.
        assert all(f'{p} owns app/item' in bullet[0] for p in named), bullet[0]
        # The user's systemMessage names the same capped list, one detail line, and no owned-path detail.
        detail = [l for l in user.split('\n') if '[many-specs]' in l and '▸' in l]
        assert len(detail) == 1 and '(+9 more)' in detail[0], user
        assert ' owns ' not in user and 'app/item' not in user, user
        assert len(user.split('\n')) == 2, user

    # ENG-1153: a big shared file co-owned by narrow specs. Once the branch updates the
    # spec that governs the change, the file's other owners are not flagged for it —
    # only for changed paths no updated spec answers for.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def git(*args):
            return subprocess.check_output(['git', '-C', tmp, *args], stderr=subprocess.DEVNULL, text=True)
        git('init', '-b', 'main'); git('config', 'user.email', 'test@example.com'); git('config', 'user.name', 'Test')
        (root/'app').mkdir(); (root/'docs/specs').mkdir(parents=True)
        (root/'app/shared.py').write_text('a = 1\n'); (root/'app/labels.py').write_text('b = 1\n')
        (root/'docs/specs/labels.md').write_text('---\nspec: labels\nowns: [app/shared.py]\n---\nLabels.\n')
        (root/'docs/specs/detect.md').write_text('---\nspec: detect\nowns: [app/shared.py, app/labels.py]\n---\nDetect.\n')
        git('add', '.'); git('commit', '-m', 'baseline'); git('checkout', '-b', 'change')
        (root/'app/shared.py').write_text('a = 2\n')
        assert sorted(s.path for s, _ in hook.Probes(str(root), '').untouched_specs()) == ['docs/specs/detect.md', 'docs/specs/labels.md']
        (root/'docs/specs/labels.md').write_text('---\nspec: labels\nowns: [app/shared.py]\n---\nLabels, amended.\n')
        assert hook.Probes(str(root), '').untouched_specs() == []
        assert not hook.given_ok({'given': {'repo': {'spec_untouched': True}}}, hook.Probes(str(root), ''))
        # A path only the untouched spec owns still reports it — for that path alone.
        (root/'app/labels.py').write_text('b = 2\n')
        hits = hook.Probes(str(root), '').untouched_specs()
        assert [(s.path, paths) for s, paths in hits] == [('docs/specs/detect.md', ['app/labels.py'])], hits
    print('spec ownership parser and git probe cases passed')
if __name__=='__main__': main()
