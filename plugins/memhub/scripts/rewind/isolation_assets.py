"""Wrap explicitly declared plugin MCP launchers in the private cache copy."""
import json
import stat
from pathlib import Path
from .state import Unavailable, digest, write


def freeze_plugins(cache):
    """Keep the private installed asset snapshot stable during native refresh.

    Codex refresh installs by staging/renaming below the cache's marketplace
    directories. Read-only files alone do not prevent version replacement;
    freeze those directories too. Runtime plugin data remains separately writable.
    """
    if not cache.exists():
        cache.mkdir(parents=True, mode=0o700)
    for path in [*cache.rglob('*'), cache]:
        if path.is_symlink():
            raise Unavailable('plugin_source_not_isolated')
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def wrap_servers(servers, prefix):
    if not isinstance(servers, dict):
        raise Unavailable('plugin_mcp_configuration_unsupported')
    changed = False
    for server in servers.values():
        if not isinstance(server, dict):
            raise Unavailable('plugin_mcp_configuration_unsupported')
        if server.get('command'):
            argv = prefix + [server['command'], *server.get('args', [])]
            server['command'], server['args'] = argv[0], argv[1:]
            changed = True
    return changed


def wrap_plugins(cache, prefix, files):
    # These are Codex's cache layout and manifest formats, not repository-specific names.
    manifests = set()
    for pattern in ('*/*/*/plugin.json', '*/*/*/.codex-plugin/plugin.json', '*/*/*/.claude-plugin/plugin.json'):
        manifests.update(cache.glob(pattern))
    sources = set()
    for manifest in sorted(manifests):
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(cache):
            raise Unavailable('plugin_source_not_isolated')
        plugin = manifest.parent.parent if manifest.parent.name in ('.codex-plugin', '.claude-plugin') else manifest.parent
        data = json.loads(manifest.read_text(encoding='utf-8'))
        sources.update(p for p in (plugin / '.mcp.json', plugin / 'mcp.json') if p.is_file())
        declarations = [data]
        overlay = data.get('extensions', {}).get('com.openai')
        if isinstance(overlay, dict): declarations.append(overlay)
        changed = False
        for declaration in declarations:
            mcp = declaration.get('mcpServers')
            if isinstance(mcp, str):
                path = plugin / mcp
                if not path.resolve().is_relative_to(plugin):
                    raise Unavailable('plugin_source_not_isolated')
                sources.add(path)
            elif isinstance(mcp, dict):
                changed = wrap_servers(mcp, prefix) or changed
            elif mcp is not None:
                raise Unavailable('plugin_mcp_configuration_unsupported')
        if changed:
            write(manifest, data)
        files[str(manifest)] = digest(manifest.read_text(encoding='utf-8'))
    for source in sorted(sources):
        if source.is_symlink() or not source.resolve().is_relative_to(cache):
            raise Unavailable('plugin_source_not_isolated')
        data = json.loads(source.read_text(encoding='utf-8'))
        servers = data.get('mcpServers', data)
        if wrap_servers(servers, prefix):
            write(source, data)
        files[str(source)] = digest(source.read_text(encoding='utf-8'))
