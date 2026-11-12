#!/usr/bin/env python3
"""Install a private entry point tied to the selected installed plugin copy."""
import argparse
from pathlib import Path
import shlex
from atomic_write import publish


def install(plugin_root, bin_dir=None):
    directory = Path(bin_dir) if bin_dir else Path.home() / '.local/bin'
    target = directory / 'memhub-compare'
    source = Path(plugin_root).resolve() / 'scripts/compare_session.py'
    if not source.is_file():
        raise ValueError('selected plugin has no rewind runner')
    # A stable selected install path is used; no untrusted command interpolation.
    publish(target, '#!/bin/sh\nexec python3 ' + shlex.quote(str(source)) + ' "$@"\n', mode=0o700)
    print('Installed', target)
    return target

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugin-root', required=True)
    parser.add_argument('--bin-dir')
    args = parser.parse_args()
    install(args.plugin_root, args.bin_dir)
