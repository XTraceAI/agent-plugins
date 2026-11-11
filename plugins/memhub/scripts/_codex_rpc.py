"""Shared Codex stdio RPC transport. Requests requiring approval are never auto-approved."""
from __future__ import annotations
import json
import queue
import subprocess
import threading
from pathlib import Path
RPC_TIMEOUT_S = 60

class RpcError(RuntimeError):
    pass


class AppServer:
    """One `codex app-server` over stdio: newline-delimited JSON-RPC."""

    def __init__(self, cmd: list[str], *, cwd: Path, env: dict, log: Path):
        self._log = open(log, "w", encoding="utf-8")
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._log,
                                     text=True, bufsize=1)
        self._next = 0
        self._replies: dict[int, queue.Queue] = {}
        self.notes: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and "method" in msg:        # a request from the server
                self._answer(msg)
            elif "id" in msg:                          # a reply to one of ours
                box = self._replies.get(msg["id"])
                if box is not None:
                    box.put(msg)
            elif "method" in msg:                      # a notification
                self.notes.put(msg)
        self.notes.put(None)                           # the server is gone

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _answer(self, req: dict) -> None:
        # approvalPolicy "never" means none should arrive. One that does is
        # declined, so a case cannot hang on a prompt nobody is there to answer.
        self.notes.put({"method": "harness/serverRequest", "params": {"method": req["method"]}})
        self._send({"id": req["id"], "error": {"code": -32601,
                                               "message": "the harness answers no server requests"}})

    def request(self, method: str, params: dict, timeout_s: float = RPC_TIMEOUT_S) -> dict:
        self._next += 1
        rid = self._next
        box: queue.Queue = queue.Queue()
        self._replies[rid] = box
        self._send({"id": rid, "method": method, "params": params})
        try:
            reply = box.get(timeout=timeout_s)
        except queue.Empty:
            raise RpcError(f"{method}: no reply in {timeout_s}s") from None
        finally:
            self._replies.pop(rid, None)
        if "error" in reply:
            raise RpcError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def close(self, grace_s: float = 10) -> int | None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(grace_s)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._log.close()
        return self.proc.returncode

