"""Capture configuration is read-only and cannot change cloud-service routing."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/memhub/scripts"
sys.path.insert(0, str(SCRIPTS))
import sinks
import _memhub_auth as auth
import pak

CLOUD = "https://cloud.example.test/mcp-server/mcp"
OTHER = "https://other.example.test/mcp-server/mcp"


@contextlib.contextmanager
def isolated():
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
        home = Path(td)
        config = home / "config.json"
        with patch.object(sinks, "CONFIG_PATH", config), \
                patch.object(auth, "_CACHE_DIR", home / "tokens"), \
                patch.object(pak, "CACHE_DIR", home / "tokens"), \
                patch.object(auth, "_plugin_mcp_config", return_value={"url": CLOUD}):
            yield home, config


def configured(config, *, active=None, entries=None):
    data = {"version": 1, "sinks": entries if entries is not None else [
        {"name": "local", "url": "http://127.0.0.1:47421", "token": "local"}],
        "active": ["local"] if active is None else active}
    config.write_text(json.dumps(data))
    return data


def rejected(fn):
    try:
        fn()
    except sinks.SinkConfigError as error:
        return str(error)
    raise AssertionError("an invalid explicit destination must not silently fall back")


def test_env_base_url_wins_over_config_file():
    with isolated() as (_, config):
        # Invalid file selection cannot defeat an explicit URL override.
        configured(config, active=["missing"])
        os.environ.update(MEMHUB_MCP_BASE_URL="http://127.0.0.1:47422",
                          MEMHUB_MCP_SERVER_PATH="/custom/mcp",
                          MEMHUB_SINKS="also_missing")
        sink = sinks.resolve_capture_sink()
        assert sink.name == "env" and sink.url == "http://127.0.0.1:47422/custom/mcp"
        assert sink.mcp_path == "/custom/mcp" and sink.token is None
        assert sinks.active_sink_names() == ("env",)


def test_config_file_supplies_capture_url_and_constant_token():
    with isolated() as (home, config):
        configured(config)
        before = config.read_bytes()
        sink = sinks.resolve_capture_sink()
        assert sink.name == "local" and sink.is_local
        assert sink.url == "http://127.0.0.1:47421/mcp-server/mcp"
        with patch.object(auth, "resolve_bearer", side_effect=AssertionError("no account lookup needed")):
            assert sinks.resolve_capture_auth(sink) == (sink.url, "local")
        assert config.read_bytes() == before and sorted(home.iterdir()) == [config]
        assert "token" not in repr(sink) and sink.url not in repr(sink)


def test_missing_or_corrupt_config_falls_back_to_mcp_json():
    with isolated() as (_, config):
        for value in [None, "{bad", "[]",
                      '{"version":true,"sinks":[],"active":[]}',
                      '{"version":1,"version":2}', "x" * (64 * 1024 + 1)]:
            if value is not None:
                config.write_text(value)
            sink = sinks.resolve_capture_sink()
            assert sink.name == "cloud" and sink.url == CLOUD and sink.token is None


def test_non_loopback_plain_http_sink_is_refused_before_auth():
    with isolated() as (_, config):
        for url in ["http://remote.example.test", "ftp://127.0.0.1", "https:///missing",
                    "https://user:private@cloud.example.test", "http://127.0.0.2",
                    "https://cloud.example.test:private", "https://cloud.example.test/#private",
                    "https://cloud.example.test\\other", "https://cloud.example.test\n"]:
            configured(config, entries=[{"name": "local", "url": url, "token": "private"}])
            with patch.object(auth, "resolve_bearer", side_effect=AssertionError("no credential access")):
                message = rejected(sinks.resolve_capture_sink)
            assert "private" not in message and url not in message
        for url in ["http://127.0.0.1", "http://localhost:47421", "http://[::1]:47421"]:
            configured(config, entries=[{"name": "local", "url": url, "token": "local"}])
            assert sinks.resolve_capture_sink().is_local


def test_memhub_sinks_selects_named_sink_and_unknown_name_is_reported():
    with isolated() as (_, config):
        configured(config, entries=[{"name": "local", "url": "http://localhost:47421", "token": "local"},
                                    {"name": "cloud", "url": "https://other.example.test"}])
        assert sinks.active_sink_names() == ("local",), "registry entries are not activation"
        os.environ["MEMHUB_SINKS"] = " cloud,cloud "
        assert sinks.active_sink_names() == ("cloud",)
        assert sinks.resolve_capture_sink().url == OTHER
        os.environ["MEMHUB_SINKS"] = "missing"
        assert "unknown" in rejected(sinks.resolve_capture_sink)
        os.environ["MEMHUB_SINKS"] = "local,cloud"
        assert "multiple" in rejected(sinks.resolve_capture_sink)
        os.environ["MEMHUB_SINKS"] = ""
        assert sinks.resolve_capture_sink() is None
        os.environ.pop("MEMHUB_SINKS")
        configured(config, active=[])
        with patch.object(auth, "default_url", side_effect=AssertionError("disabled is not cloud fallback")):
            assert sinks.resolve_capture_sink() is None
        configured(config, active=["cloud"])
        assert sinks.resolve_capture_sink().url == CLOUD, "named cloud synthesizes the legacy destination"


def test_destination_schema_is_unambiguous_and_path_safe():
    with isolated() as (_, config):
        config.write_text('{"version":2,"sinks":[],"active":[]}')
        assert "version" in rejected(sinks.resolve_capture_sink)
        for name in ["../local", "", ".", "a/b", "a\\b", "x" * 65, "λ"]:
            configured(config, entries=[{"name": name, "url": "https://cloud.example.test"}])
            rejected(sinks.resolve_capture_sink)
        for items in [[None], [{"name": "local", "url": "https://cloud.example.test"}] * 2,
                      [{"name": "local", "url": "https://cloud.example.test?"}],
                      [{"name": "local", "url": "https://cloud.example.test?private=value"}],
                      [{"name": "local", "url": "https://cloud.example.test", "mcp_path": "//other"}],
                      [{"name": "local", "url": "https://cloud.example.test", "token": "bad\nheader"}]]:
            configured(config, entries=items)
            rejected(sinks.resolve_capture_sink)
        configured(config, entries=[{"name": "local", "url": "https://cloud.example.test/api/",
                                     "mcp_path": "/mcp?client=example"}])
        assert sinks.resolve_capture_sink().url == "https://cloud.example.test/api/mcp?client=example"


def test_memhub_token_outranks_sink_token():
    with isolated() as (_, config):
        configured(config)
        os.environ["MEMHUB_TOKEN"] = " override "
        sink = sinks.resolve_capture_sink()
        assert sinks.resolve_capture_auth(sink) == (sink.url, "override")
        os.environ["MEMHUB_TOKEN"] = "bad\nheader"
        assert "credential" in rejected(lambda: sinks.resolve_capture_auth(sink))


def test_cloud_sink_without_token_uses_that_urls_stored_pak():
    with isolated() as (_, config):
        configured(config, active=["other"], entries=[{"name": "other", "url": "https://other.example.test"}])
        pak.CACHE_DIR.mkdir()
        pak.key_path(CLOUD).write_text(json.dumps({"secret": "cloud-A"}))
        pak.key_path(OTHER).write_text(json.dumps({"secret": "cloud-B"}))
        sink = sinks.resolve_capture_sink()
        assert sinks.resolve_capture_auth(sink, refresh=False) == (OTHER, "cloud-B")
        pak.key_path(OTHER).unlink()
        assert sinks.resolve_capture_auth(sink, refresh=False) == (OTHER, None)
        assert auth.resolve_bearer(refresh=False) == (CLOUD, "cloud-A")


def test_local_capture_does_not_change_cloud_service_resolvers():
    with isolated() as (_, config):
        configured(config)
        pak.CACHE_DIR.mkdir()
        pak.key_path(CLOUD).write_text(json.dumps({"secret": "cloud-A"}))
        sink = sinks.resolve_capture_sink()
        assert sinks.resolve_capture_auth(sink) == (sink.url, "local")
        assert auth.default_url() == CLOUD
        assert auth.resolve_bearer(refresh=False) == (CLOUD, "cloud-A")
        assert auth.resolve_url_and_auth(interactive=False) == (
            CLOUD, {"Authorization": "Bearer cloud-A"}, None)


def test_oauth_refresh_metadata_cannot_cross_backend_origins():
    with isolated():
        refreshed = []
        with patch.object(auth, "_refresh_cached_token_if_stale", side_effect=refreshed.append), \
                patch.object(auth, "_cached_access_token", side_effect=lambda url: "cached-B" if url == OTHER else "cached-A"):
            assert sinks.resolve_capture_auth(sinks.Sink("other", OTHER)) == (OTHER, "cached-B")
            assert refreshed == [], "other-origin credentials cannot use installed-backend OAuth metadata"
            assert sinks.resolve_capture_auth(sinks.Sink("cloud", CLOUD)) == (CLOUD, "cached-A")
            assert refreshed == [CLOUD], "legacy backend refresh remains available"


def test_equivalent_backend_origins_keep_refresh_available():
    with isolated():
        for installed, selected in [
            ("https://cloud.example.test/mcp-server/mcp", "https://CLOUD.example.test:443/mcp-server/mcp"),
            ("http://localhost/mcp", "http://LOCALHOST:80/mcp"),
            ("http://[::1]/mcp", "http://[::1]:80/mcp")]:
            refreshed = []
            with patch.object(auth, "_plugin_mcp_config", return_value={"url": installed}), \
                    patch.object(auth, "_refresh_cached_token_if_stale", side_effect=refreshed.append), \
                    patch.object(auth, "_cached_access_token", return_value="cached"):
                assert sinks.resolve_capture_auth(sinks.Sink("selected", selected)) == (selected, "cached")
                assert refreshed == [selected]
        with patch.object(auth, "_refresh_cached_token_if_stale", side_effect=AssertionError("different origin")), \
                patch.object(auth, "_cached_access_token", return_value="cached"):
            selected = "https://cloud.example.test:8443/mcp"
            assert sinks.resolve_capture_auth(sinks.Sink("other", selected)) == (selected, "cached")


def test_bare_python_resolution_needs_no_network_or_mcp_sdk():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        config = home / ".config/memhub-plugin/config.json"
        config.parent.mkdir(parents=True)
        configured(config)
        script = '''import builtins,socket,sys
real_import=builtins.__import__
def guarded(name,*args,**kwargs):
    if name == "mcp" or name.startswith("mcp."): raise AssertionError("SDK import")
    return real_import(name,*args,**kwargs)
builtins.__import__=guarded
def denied(*args,**kwargs): raise AssertionError("network access")
socket.socket.connect=denied
sys.path.insert(0,sys.argv[1])
import sinks
selected=sinks.resolve_capture_sink()
assert selected.name == "local"
assert sinks.resolve_capture_auth(selected) == (selected.url,"local")
'''
        env = {"HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", script, str(SCRIPTS)], env=env,
                                text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr


def test_equivalent_origin_reuses_installed_credentials_without_cross_origin_fallback():
    with isolated():
        pak.CACHE_DIR.mkdir()
        pak.key_path(CLOUD).write_text(json.dumps({"secret":"installed-key"}))
        selected="https://CLOUD.example.test:443/custom/mcp"
        sink=sinks.Sink("selected",selected)
        assert sinks.resolve_capture_auth(sink,refresh=False) == (selected,"installed-key")
        pak.key_path(selected).write_text(json.dumps({"secret":"selected-key"}))
        assert sinks.resolve_capture_auth(sink,refresh=False) == (selected,"selected-key")
        pak.key_path(selected).unlink();pak.key_path(CLOUD).unlink()
        auth.token_cache_path(CLOUD).write_text(json.dumps({"access_token":"installed-access"}))
        assert sinks.resolve_capture_auth(sink,refresh=False) == (selected,"installed-access")
        refreshed=[]
        with patch.object(auth,"_refresh_cached_token_if_stale",side_effect=refreshed.append):
            assert sinks.resolve_capture_auth(sink) == (selected,"installed-access")
        assert refreshed == [selected,CLOUD]
        for other in [OTHER,"https://cloud.example.test:8443/mcp","http://cloud.example.test/mcp"]:
            if other.startswith("http:"):
                rejected(lambda:sinks.Sink("other",other))
            else:
                assert sinks.resolve_capture_auth(sinks.Sink("other",other),refresh=False) == (other,None)


def test_sanitized_cache_names_cannot_select_another_origins_credentials():
    with isolated():
        legitimate="https://api.example.test:443/mcp"
        collision="https://api.example.test_443/mcp"
        pak.CACHE_DIR.mkdir()
        pak.key_path(legitimate).write_text(json.dumps({"secret":"origin-specific"}))
        auth.token_cache_path(legitimate).write_text(json.dumps({"access_token":"origin-specific"}))
        assert pak.key_path(legitimate)==pak.key_path(collision),"exercise the legacy collision"
        with patch.object(auth,"resolve_bearer",side_effect=AssertionError("unsafe host must fail before credential access")):
            for url in [collision,"https://host_name.example/mcp","https://api%2eexample.test/mcp", "https://λ.example/mcp"]:
                rejected(lambda:sinks.Sink("other",url))
        assert sinks.resolve_capture_auth(sinks.Sink("valid",legitimate),refresh=False)==(legitimate,"origin-specific")
        local="http://localhost:47421/mcp"
        pak.key_path(local).write_text(json.dumps({"secret":"different-protocol"}))
        assert sinks.resolve_capture_auth(sinks.Sink("local",local),refresh=False)==(local,None)
        assert sinks.resolve_capture_auth(sinks.Sink("local",local,"explicit"),refresh=False)==(local,"explicit")
        assert sinks.Sink("ipv6","https://[2001:db8::1]:443/mcp").url.startswith("https:")


def test_environment_base_query_is_rejected_before_path_composition():
    with isolated():
        for base in ["https://example.test?", "https://example.test?token=opaque", "http://localhost:47421?"]:
            os.environ["MEMHUB_MCP_BASE_URL"]=base
            with patch.object(auth,"default_url",side_effect=AssertionError("reject the base before composition")):
                rejected(sinks.resolve_capture_sink)
        os.environ["MEMHUB_MCP_BASE_URL"]="http://localhost:47421"
        os.environ["MEMHUB_MCP_SERVER_PATH"]="/custom/mcp?mode=local"
        assert sinks.resolve_capture_sink().url=="http://localhost:47421/custom/mcp?mode=local"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
