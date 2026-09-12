#!/usr/bin/env python3
"""Is this session running on Claude Code on the web? (stdlib only)

A cloud session runs in a fresh container: nothing under ``~/.config``
survives it, there is no browser, and outbound traffic is governed by the
environment's network policy. Three things the plugin says to a person change
because of that:

* the fix for "not authenticated" is not ``/memhub:login`` — which opens a
  browser the container does not have and writes a cache the next session
  will not see — but ``MEMHUB_TOKEN`` in the environment's variables, holding
  a personal access key minted somewhere with a browser;
* a refused connection is the environment's egress policy, and the fix is an
  allowlist entry, not a firewall on the user's own machine;
* ``login.py`` must not start a browser flow at all.

The session VM carries ``CLAUDE_CODE_REMOTE=true`` (documented at
code.claude.com/docs/en/cloud-environments) and it is never ``true`` locally.
Detection reads only that, deliberately: a heuristic on hostnames or paths
would misfire on a developer's own container and send them to the wrong fix.
"""
from __future__ import annotations

import os
from typing import Mapping


def is_cloud_session(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get("CLAUDE_CODE_REMOTE") or "").strip().lower() == "true"


def token_fix(host: str) -> str:
    """How a cloud session gets a capture credential, in one sentence.

    Names the variable, the key shape and where to mint it, because the person
    reading this is looking at a session that cannot run the usual fix.
    """
    return (
        "This is a Claude Code on the web session, so /memhub:login cannot "
        f"open a browser here: set MEMHUB_TOKEN to a personal access key "
        f"(mhk_…) for {host} in the environment's variables at claude.ai/code "
        "— mint one on your own machine with /memhub:login --cloud-key."
    )


def egress_fix(host: str) -> str:
    """How a cloud session gets to the MemHub host, in one sentence."""
    return (f"Allow {host} under the environment's network access at "
            "claude.ai/code; until then nothing from this session reaches "
            "memory.")
