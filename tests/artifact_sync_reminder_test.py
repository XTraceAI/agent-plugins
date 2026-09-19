"""The real hook names owns-matched specs, debounces, and never uploads."""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/memhub/scripts'

def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root/'.git').mkdir(); (root/'docs/specs/retired').mkdir(parents=True)
        text='---\nspec: limits\nowns: [app/limit.py]\n---\nMaximum 100.\n'
        (root/'docs/specs/limits.md').write_text(text)
        (root/'docs/specs/retired/old.md').write_text(text)
        sid=str(uuid.uuid4())
        def run(path,session=sid):
            result=subprocess.run([sys.executable,str(SCRIPTS/'artifact_sync_reminder.py')],
                input=json.dumps({'cwd':tmp,'session_id':session,'tool_input':{'file_path':str(root/path)}}),
                text=True,capture_output=True,env={**os.environ,'TMPDIR':tmp})
            assert result.returncode==0
            return result.stdout
        output=run('app/limit.py')
        assert 'docs/specs/limits.md' in output and 'old.md' not in output
        assert 'save_artifact' not in output
        assert run('app/limit.py')==''
        assert run('app/unowned.py',str(uuid.uuid4()))==''
        assert run('docs/specs/limits.md',str(uuid.uuid4()))==''
        (root/'docs/specs/limits.md').unlink()
        assert run('app/limit.py',str(uuid.uuid4()))==''
    print('owns reminder fire, silence, retirement and debounce passed')
if __name__=='__main__': main()
