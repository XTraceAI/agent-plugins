"""Harness extension point. Shared runner owns storage, auth and lifecycle.

Adapters fork WITHOUT submitting input, returning an opaque native branch id.
They must persist no server credentials and never implement their own claims.
Codex (codex.py) and Claude Code (claude.py) are registered by their wire harness name.
"""
from __future__ import annotations
from typing import Protocol

class Adapter(Protocol):
    def capabilities(self) -> dict: ...
    def resolve_boundary(self, checkpoint: dict) -> dict: ...
    def preflight(self, checkpoint: dict, requested_config: dict) -> dict: ...
    def prepare(self, checkpoint: dict, config: dict, cwd: str) -> None: ...
    def fork(self, checkpoint: dict, requested_config: dict, cwd: str) -> dict: ...
    def command(self, native_branch_id: str, cwd: str, config: dict,
                input_parts: list | None = None) -> list[str]: ...
    def close(self) -> None: ...


def resolve(harness):
    if harness == 'codex':
        from .codex import Codex
        return Codex()
    if harness == 'claude_code':
        from .claude import Claude
        return Claude()
    from .state import Unavailable
    raise Unavailable('harness_adapter_unavailable')
