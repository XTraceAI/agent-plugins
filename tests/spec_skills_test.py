"""Focused spec workflows must remain navigable inside an installed plugin copy."""
import re
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as tmp:
        plugin = (Path(tmp) / 'memhub').resolve()
        shutil.copytree(ROOT / 'plugins/memhub', plugin,
                        ignore=shutil.ignore_patterns('__pycache__'))
        files = [plugin / 'skills' / name / 'SKILL.md'
                 for name in ('spec', 'spec-work', 'spec-check', 'spec-maintain')]
        files.append(plugin / 'references/spec-workflow.md')
        for path in files:
            text = path.read_text()
            if path.name == 'SKILL.md':
                assert text.startswith('---\nname: ' + path.parent.name + '\n'), path
                assert '\ndescription: ' in text and len(text.splitlines()) < 500
            for target in re.findall(r'\]\(([^)#]+)(?:#[^)]*)?\)', text):
                if '://' in target:
                    continue
                resolved = (path.parent / target).resolve()
                assert resolved.is_relative_to(plugin), (path, target)
                assert resolved.is_file(), (path, target)
        assert (plugin / 'scripts/spec_owns.py').is_file()
    print('installed spec workflow links and skill entry points passed')


if __name__ == '__main__':
    main()
