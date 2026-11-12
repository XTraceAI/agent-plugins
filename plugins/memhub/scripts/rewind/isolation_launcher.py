"""Run only AFTER the native sandbox has been entered. No plugin placeholders in outer argv."""
import base64
import os
import sys


def main():
    shell, encoded = sys.argv[1:]
    command = base64.b64decode(encoded, validate=True).decode('utf-8')
    # Codex's plugin hook substitution happens before shell evaluation. Do that
    # inside the sandbox so quotes in a plugin path cannot escape the wrapper.
    for key in ('PLUGIN_ROOT', 'CLAUDE_PLUGIN_ROOT', 'PLUGIN_DATA', 'CLAUDE_PLUGIN_DATA'):
        if key in os.environ:
            command = command.replace('${' + key + '}', os.environ[key])
    os.execve(shell, [shell, '-c', command], os.environ)


if __name__ == '__main__':
    main()
