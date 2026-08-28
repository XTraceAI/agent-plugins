"""Fail-closed Git provenance for coding-agent session records.

Host artifacts are preferred when they carry provenance themselves. Local
fallbacks are read-only snapshots of the session cwd; they never infer a
branch from a directory or worktree name. Live flushers additionally pin the
observed branch by record UUID so a retry after checkout cannot relabel an
older turn.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from room_map import git_env, git_readonly

_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_BRANCH_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")
_SCP_REMOTE_RE = re.compile(
    r"(?:(?P<user>[^/@:\s]+)@)?(?P<host>[^/@:\s]+):(?P<path>[^\s]+)\Z")
_REMOTE_SCHEMES = frozenset(("git", "http", "https", "ssh"))


def _is_repository_path(value: str) -> bool:
    """Whether a GitHub remote path names exactly one owner/repository pair."""
    path = value[1:] if value.startswith("/") else value
    if path.endswith("/"):
        path = path[:-1]
    parts = path.split("/")
    return (
        len(parts) == 2
        and all(part not in ("", ".", "..") for part in parts)
        and parts[1] != ".git"
    )


def usable_cwd(cwd) -> bool:
    """Whether semi-trusted session content is safe to pass to ``git -C``."""
    if not isinstance(cwd, str) or not cwd or cwd.startswith("-"):
        return False
    try:
        path = Path(cwd)
        return path.is_absolute() and path.is_dir()
    except (OSError, ValueError):
        return False


def normalize_branch(value) -> str | None:
    """A plausible full branch name, excluding detached-HEAD sentinels."""
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if value == "HEAD" or len(value) > 1024:
        return None
    if (value.startswith(("/", ".", "-")) or value.endswith(("/", "."))
            or "//" in value or ".." in value or "@{" in value
            or _BRANCH_FORBIDDEN_RE.search(value)):
        return None
    if any(part.startswith(".") or part.endswith(".lock")
           for part in value.split("/")):
        return None
    return value


def normalize_commit(value) -> str | None:
    if isinstance(value, str) and _COMMIT_RE.fullmatch(value):
        return value.lower()
    return None


def normalize_repository_url(value) -> str | None:
    """A bounded Git remote with all credential-bearing components removed.

    Repository URLs leave the machine as linking metadata, so a convenient
    ``git remote get-url`` result is not safe to persist verbatim. URL-form
    remotes lose userinfo, query, and fragment fields. SCP-form remotes retain
    only Git's conventional non-secret ``git@`` username; any other username
    is rejected rather than risking an access token in state or on the wire.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if len(value) > 4096 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None

    if "://" in value:
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.lower() not in _REMOTE_SCHEMES
                    or not parsed.hostname or not parsed.path
                    or parsed.hostname.lower() != "github.com"
                    or not _is_repository_path(parsed.path)):
                return None
            host = parsed.hostname
            if ":" in host:
                host = f"[{host}]"
            port = parsed.port
        except ValueError:
            return None
        authority = host + (f":{port}" if port is not None else "")
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path, "", ""))

    scp = _SCP_REMOTE_RE.fullmatch(value)
    if not scp:
        return None
    user = scp.group("user")
    if (user not in (None, "git")
            or scp.group("host").lower() != "github.com"
            or not _is_repository_path(scp.group("path"))):
        return None
    prefix = "git@" if user == "git" else ""
    return f"{prefix}{scp.group('host')}:{scp.group('path')}"


def _run(cwd: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    if not usable_cwd(cwd):
        return None
    try:
        # Keep the hardening adjacent to every probe. ``cwd`` is session
        # content, and a repository-local core.fsmonitor is executable config;
        # git_readonly disarms it (plus hooks/credentials/ext transports) while
        # git_env prevents this process's MemHub bearer reaching the child.
        return subprocess.run(
            git_readonly(cwd) + args,
            capture_output=True,
            text=True,
            timeout=2,
            env=git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def snapshot_branch(cwd) -> tuple[bool, str | None]:
    """Return ``(observed, branch)`` for the cwd's current HEAD.

    ``(True, None)`` means Git positively reported a detached HEAD. ``False``
    means no trustworthy observation was possible, so a native artifact value
    may still be used as a fallback.
    """
    if not isinstance(cwd, str):
        return False, None
    result = _run(cwd, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    if result is None:
        return False, None
    if result.returncode == 0:
        branch = normalize_branch(result.stdout.rstrip("\r\n"))
        return (True, branch) if branch else (False, None)
    # `git symbolic-ref --quiet` returns 1 specifically for a non-symbolic
    # (detached) HEAD. Other failures include non-repositories and malformed
    # cwd state, which are unavailable rather than evidence of detachment.
    if result.returncode == 1:
        return True, None
    return False, None


def snapshot(cwd) -> dict[str, str]:
    """Read branch, commit, and origin URL locally, omitting unavailable data."""
    if not isinstance(cwd, str) or not usable_cwd(cwd):
        return {}
    provenance: dict[str, str] = {}
    _, branch = snapshot_branch(cwd)
    if branch:
        provenance["branch"] = branch

    commit_result = _run(cwd, ["rev-parse", "--verify", "HEAD"])
    if commit_result is not None and commit_result.returncode == 0:
        commit = normalize_commit(commit_result.stdout.rstrip("\r\n"))
        if commit:
            provenance["commit_hash"] = commit

    remote_result = _run(cwd, ["remote", "get-url", "origin"])
    if remote_result is not None and remote_result.returncode == 0:
        repository_url = normalize_repository_url(
            remote_result.stdout.rstrip("\r\n"))
        if repository_url:
            provenance["repository_url"] = repository_url
    return provenance


def resolve(cwd, native=None) -> dict[str, str]:
    """Resolve each provenance field from native metadata, then local Git."""
    native = native if isinstance(native, dict) else {}
    provenance: dict[str, str] = {}
    native_branch = native.get("branch")
    branch = normalize_branch(native_branch)
    native_detached = native_branch == "HEAD"
    commit = normalize_commit(native.get("commit_hash"))
    repository_url = normalize_repository_url(native.get("repository_url"))
    if branch:
        provenance["branch"] = branch
    if commit:
        provenance["commit_hash"] = commit
    if repository_url:
        provenance["repository_url"] = repository_url

    if len(provenance) < 3:
        local = snapshot(cwd)
        for key in ("branch", "commit_hash", "repository_url"):
            if key == "branch" and native_detached:
                continue
            if key not in provenance and key in local:
                provenance[key] = local[key]
    return provenance


def attach_branch(records: list[dict], branch) -> None:
    """Attach one validated branch to records without changing their identity."""
    normalized = normalize_branch(branch)
    for record in records:
        if normalized:
            record["gitBranch"] = normalized
        else:
            record.pop("gitBranch", None)


def pin_record_branches(records: list[dict], prior, *, observed: bool,
                        branch) -> dict[str, str | None]:
    """Pin a live branch observation by deterministic record UUID.

    When the local probe was unavailable, the reader's native branch remains
    the fallback. A positive detached-HEAD observation pins ``None`` instead
    of reusing a stale native branch.
    """
    pins = dict(prior) if isinstance(prior, dict) else {}
    present: set[str] = set()
    observed_branch = normalize_branch(branch)
    for record in records:
        record_id = record.get("uuid")
        if not isinstance(record_id, str) or not record_id:
            continue
        present.add(record_id)
        if record_id not in pins:
            pins[record_id] = (observed_branch if observed else
                               normalize_branch(record.get("gitBranch")))
        pinned = normalize_branch(pins.get(record_id))
        if pinned:
            record["gitBranch"] = pinned
        else:
            record.pop("gitBranch", None)
    # Readers emit the complete current artifact. Prune pins for records no
    # longer present after compaction/checkpoint restoration.
    return {record_id: pin for record_id, pin in pins.items()
            if record_id in present}
