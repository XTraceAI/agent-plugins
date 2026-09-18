"""Version of the loaded code, updated with the release manifests.

Do not read a manifest at request time: a host may download an update while an
old Python process or MCP connection is still running.
"""
import json
import re
from _memhub_auth import _plugin_root


def _loaded_version():
    # Snapshot at transport import, never when a request is sent. This also
    # respects the separately released staging package's active root.
    root = _plugin_root()
    for path in (root / ".claude-plugin/plugin.json", root / "plugin.json"):
        try:
            version = json.loads(path.read_text(encoding="utf-8")).get("version", "")
            if isinstance(version, str) and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version):
                return version
        except (OSError, ValueError, AttributeError):
            pass
    return "unknown"


ACTIVE_PLUGIN_VERSION = _loaded_version()
VERSION_HEADER = "X-MemHub-Plugin-Version"


def request_headers():
    return {VERSION_HEADER: ACTIVE_PLUGIN_VERSION}


def _update_action(host):
    staging = "memhub-staging" in _plugin_root().parts
    plugin = "memhub-staging" if staging else "memhub"
    marketplace = "memhub-internal" if staging else "memhub"
    origin = "https://staging.mem.xtrace.ai" if staging else "https://mem.xtrace.ai"
    suffix = "/" + host if host in {"claude-code", "codex", "cursor"} else ""
    guide = origin + "/plugin" + suffix
    if host == "claude-code":
        action = (f"Run /plugin marketplace update {marketplace}, then "
                  f"/plugin update {plugin}@{marketplace}, and restart this agent session.")
    elif host == "codex":
        action = (f"Refresh the {marketplace} marketplace and update {plugin} in Codex Plugins. "
                  "Rerun /memhub:setup for bridge changes, then restart Codex and review the MemHub hooks.")
    elif host == "cursor":
        action = (f"Open Cursor Settings > Plugins, refresh the {marketplace} marketplace "
                  f"and update {plugin}, then restart this agent session.")
    else:
        action = "Update MemHub in this host's plugin manager, then restart this agent session."
    return action, guide


def update_message(latest, *, host=None):
    action, guide = _update_action(host)
    channel = "public marketplace" if host != "cursor" else "GitHub marketplace (official-directory availability may lag)"
    return (f"MemHub update available: {ACTIVE_PLUGIN_VERSION} → {latest} on the {channel}. "
            f"This is an optional update; MemHub continues working. {action} "
            f"Upgrade guide: {guide}")


def upgrade_message(minimum, *, host=None, scope="plugin_operations"):
    action, guide = _update_action(host)
    effect = ("Capture, search, imports and Rulebook are paused." if scope == "plugin_operations"
              else "Rulebook synchronization is unavailable; cached team rules are suspended.")
    return (
        f"UPDATE REQUIRED — PLUGIN_UPGRADE_REQUIRED: Active MemHub plugin {ACTIVE_PLUGIN_VERSION}; "
        f"minimum required {minimum}. {effect} {action} "
        "Pending captures remain queued. If no newer release is available, "
        "keep pending work and retry after the release reaches your marketplace. "
        f"Upgrade guide: {guide}"
    )
