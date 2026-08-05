"""The two memhub plugins must declare the SAME version.

`memhub-staging` shares its scripts, hooks and skills with `memhub` by symlink,
so the code genuinely never drifts. The VERSION does — and that is what gates
delivery: the plugin cache is keyed by version
(`~/.claude/plugins/cache/memhub/<plugin>/<version>/`), so a plugin whose
manifest still reads an old number is never re-fetched. `/plugin update`
reports success and installs nothing.

That happened: prod was bumped for a release and staging was not, so everyone on
the staging build silently stayed on code from several releases earlier while
believing they were current. Shared code, unshared version number, no error
anywhere.

Run: python3 version_parity_test.py   (stdlib only)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MANIFESTS = {
    "memhub": ROOT / "plugins" / "memhub" / ".claude-plugin" / "plugin.json",
    "memhub-staging": ROOT / "plugins" / "memhub-staging" / ".claude-plugin" / "plugin.json",
}


def main() -> int:
    versions = {}
    for name, path in MANIFESTS.items():
        if not path.exists():
            print(f"FAIL missing manifest: {path}")
            return 1
        versions[name] = json.loads(path.read_text()).get("version")
        print(f"  {name:<16} {versions[name]}")

    distinct = set(versions.values())
    if len(distinct) != 1 or None in distinct:
        print("\nFAIL versions differ — the stale one will not be re-fetched by\n"
              "     any client, because the plugin cache is keyed by version.\n"
              "     Bump both manifests together.")
        return 1
    print("\nok  both plugins declare the same version")
    return 0


if __name__ == "__main__":
    sys.exit(main())
