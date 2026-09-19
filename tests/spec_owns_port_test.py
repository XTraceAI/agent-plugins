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
        (root/'docs/specs/limit.md').write_text(TEXT+'Updated.\n')
        code, output = run("post", {"cwd":tmp,"session_id":"spec-session","tool_name":"Edit", "tool_input":{"file_path":str(root/'docs/specs/limit.md')}}, env)
        events = [json.loads(line) for line in (cache/'ledger/events.jsonl').read_text().splitlines()]
        assert any(e['kind']=='converted' and e['rule_id']=='spec-test' for e in events), events
        assert run("pre",payload,env)[1].strip() == ""

        assert not hook.given_ok(rule,hook.Probes(str(root), ''))
        assert not hook.given_ok({'given':{'repo':{'spec_untouched':True,'spec_dir':'missing'}}},hook.Probes(str(root),''))
        assert hook.given_norm({'repo':{'spec_untouched':'yes'}}) is None
        assert hook.given_norm({'repo':{'spec_dir':'../outside'}}) is None
    print('spec ownership parser and git probe cases passed')
if __name__=='__main__': main()
