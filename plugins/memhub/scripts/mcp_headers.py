#!/usr/bin/env python3
"""The ``headersHelper`` for the plugin's ``memhub`` MCP server.

Claude Code runs this when it connects the server and sends whatever JSON
object it prints as extra request headers. Printing the credential the hooks
already use makes ``/memhub:login`` authenticate the model's tools too, so the
``/mcp`` connector login is no longer a second, separate step.

Printing ``{}`` is the fallback: with no Authorization header Claude Code
treats the server exactly as before the helper existed — it answers 401 and
the ``oauth`` block's ``/mcp`` login applies. So this must never exit non-zero
or write anything but one JSON object to stdout.

Four things about how Claude Code runs it, each measured, shape the code:

* **A rejected Authorization is fatal for the session.** When the header this
  prints gets a 401, Claude Code marks the server ``failed`` and disables the
  OAuth fallback ("OAuth fallback is disabled when the helper supplies
  Authorization"), where no header at all would have left it ``needs-auth``.
  A key revoked on the server but still unexpired locally would do exactly
  that, so the key is checked against the server first and only handed over
  if it is not refused. A network failure is not a refusal: the key is still
  printed, because the connection it is for would fail the same way.
* **It runs once per connect**, not per request, with a 10s timeout — so the
  check costs one request per connect and must finish well inside that.
* **Credential env vars are scrubbed** for plugin helpers, so
  ``$MEMHUB_TOKEN`` never reaches this process; only the stored access key
  and the plugin's own OAuth cache do.
* **The header goes to the URL in ``.mcp.json``** (``$CLAUDE_CODE_MCP_SERVER_URL``),
  whatever ``$MEMHUB_MCP_BASE_URL`` says. So the key is chosen from that
  config, never from the override, and nothing is printed for any other host —
  otherwise the override hands one backend's key to the other.

``refresh=False``: a stale cached OAuth token would cost two blocking urllib
calls on the connect path (see ``resolve_bearer``). A stored access key is the
normal case and needs none.
"""
from __future__ import annotations

import json
import os
import urllib.parse

_CHECK_TIMEOUT_S = 4.0


def _config_url() -> str:
    """This install's own MCP url, version query stripped, override ignored."""
    from _memhub_auth import _plugin_mcp_config  # noqa: PLC0415
    parts = urllib.parse.urlsplit(_plugin_mcp_config()["url"])
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "memhub_plugin_version"]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _refused(url: str, bearer: str) -> bool:
    """True only when the server itself refuses the credential."""
    import mcp_http  # noqa: PLC0415
    try:
        mcp_http.request(url, bearer, "ping", timeout=_CHECK_TIMEOUT_S)
    except mcp_http.McpError as exc:
        return exc.status in (401, 403)
    except Exception:  # noqa: BLE001 — not a refusal; let the connect decide
        return False
    return False


def headers() -> dict[str, str]:
    try:
        from _memhub_auth import resolve_bearer  # noqa: PLC0415
        url = _config_url()
        target = os.environ.get("CLAUDE_CODE_MCP_SERVER_URL")
        if target and urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(url).netloc:
            return {}
        _url, bearer = resolve_bearer(url, refresh=False)
        if not bearer or _refused(url, bearer):
            return {}
    except Exception:  # noqa: BLE001 — any failure degrades to the /mcp login
        return {}
    return {"Authorization": f"Bearer {bearer}"}


if __name__ == "__main__":
    print(json.dumps(headers()))
