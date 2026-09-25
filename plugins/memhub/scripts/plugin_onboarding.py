"""Local OAuth completion: fixed guide destinations, no credentials in browser output."""
from __future__ import annotations

import contextvars
import threading
from urllib.parse import urlsplit

HOSTS = ("cursor", "claude-code", "codex")
_ORIGINS = {
    "https://api.memhub.xtrace.ai": "https://mem.xtrace.ai",
    "https://api.staging.memhub.xtrace.ai": "https://staging.mem.xtrace.ai",
}


def guide_url(mcp_url: str, host: str | None = None) -> str | None:
    """Host is explicit; an unknown host gets a chooser, never a guessed guide."""
    try:
        parsed = urlsplit(mcp_url)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    origin = _ORIGINS.get(f"{parsed.scheme}://{parsed.netloc}")
    if origin is None:
        return None
    suffix = f"/{host}" if host in HOSTS else ""
    return f"{origin}/plugin{suffix}"


class LoginCompletion:
    """Bridge the HTTP callback thread to the verified foreground login result."""

    def __init__(self, destination: str | None):
        self.destination = destination
        self.callback_received = False
        self.succeeded = False
        self.finished = threading.Event()
        self.response_sent = threading.Event()

    def finish(self, succeeded: bool) -> None:
        self.succeeded = succeeded
        self.finished.set()


# Per async login, not global success state that another attempt could reuse.
active_completion: contextvars.ContextVar[LoginCompletion | None] = contextvars.ContextVar(
    "memhub_login_completion", default=None
)


def send_callback_response(handler, *, succeeded: bool, destination: str | None = None) -> None:
    """No remote resources, reflected OAuth parameters, or outbound referrers."""
    if succeeded and destination:
        handler.send_response(303)
        handler.send_header("Location", destination)
        body = b""
    else:
        handler.send_response(200)
        body = (
            b"<!doctype html><html><head><meta name='viewport' content='width=device-width, initial-scale=1'>"
            b"<title>MemHub sign-in</title></head><body>"
            + (b"<h1>Return to your coding agent</h1><p>Check the login result there. "
               b"This callback alone does not confirm that sign-in succeeded.</p>"
               if succeeded else
               b"<h1>Sign-in did not complete</h1><p>Return to your coding agent for the error. "
               b"Run MemHub login again to retry. Finish any other pending login first.</p>")
            + b"</body></html>"
        )
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    handler.end_headers()
    handler.wfile.write(body)
