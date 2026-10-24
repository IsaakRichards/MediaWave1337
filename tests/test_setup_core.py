import json
import os
import sys

import pytest

import setup_core as core


def test_runtime_requirements_stop_at_development_section(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("# Runtime\nPySide6>=6.5\nrequests\nlibtorrent>=2.0   # wheel name\n\n# Development\npytest\n")
    assert core.runtime_requirements(str(req)) == ["PySide6>=6.5", "requests", "libtorrent>=2.0"]
    # The real file parses too and never includes pytest.
    real = core.runtime_requirements()
    assert any(r.lower().startswith("pyside6") for r in real) and not any("pytest" in r for r in real)


@pytest.mark.parametrize("req,name", [("PySide6>=6.5", "pyside6"), ("requests", "requests"), ("libtorrent >= 2.0", "libtorrent"), ("foo[bar]==1", "foo")])
def test_requirement_name(req, name):
    assert core.requirement_name(req) == name


def test_system_install_command_elevates(monkeypatch):
    monkeypatch.setattr(core.os, "geteuid", lambda: 1000, raising=False)
    which = lambda name: f"/usr/bin/{name}" if name in ("pkexec", "apt-get") else None
    assert core.detect_linux_package_manager(which) == "apt"
    assert core.system_install_command("apt", ["venv", "ffmpeg"], which) == ["pkexec", "apt-get", "install", "-y", "python3-venv", "ffmpeg"]
    assert core.system_install_command("apt", ["ffmpeg"], lambda n: None)[0] == "sudo"
    monkeypatch.setattr(core.os, "geteuid", lambda: 0, raising=False)
    assert core.system_install_command("pacman", ["libtorrent"], which) == ["pacman", "-S", "--noconfirm", "--needed", "libtorrent-rasterbar"]
    assert core.system_install_command("pacman", ["venv"], which) is None  # venv ships with python there


def test_check_python():
    assert core.check_python((3, 10, 12)).status == "ok"
    old = core.check_python((3, 8, 10))
    assert old.status == "fail" and old.fix == "python"


def test_run_command_streams_output():
    lines = []
    code = core.run_command([sys.executable, "-c", "print('one'); print('two'); raise SystemExit(3)"], lines.append)
    assert code == 3 and lines[1:] == ["one", "two"]
    assert core.run_command(["definitely-not-a-real-command-xyz"], lines.append) == 127


def _fake_http(responses):
    calls = []

    def http_get(url, headers=None, timeout=8):
        calls.append((url, headers))
        for prefix, result in responses.items():
            if prefix in url:
                if isinstance(result, Exception):
                    raise result
                return result
        return 404, b""

    return http_get, calls


def test_indexer_test_jackett_and_prowlarr():
    http, calls = _fake_http({"torznab/api": (200, b'<?xml version="1.0"?><caps><server/></caps>')})
    ok, message, kind = core.test_indexer("http://localhost:9117/", "KEY", "auto", http_get=http)
    assert ok and kind == "jackett" and "apikey=KEY" in calls[0][0]

    http, calls = _fake_http({"/api/v1/system/status": (200, json.dumps({"version": "1.20"}).encode())})
    ok, message, kind = core.test_indexer("http://localhost:9696", "KEY", "auto", http_get=http)
    assert ok and kind == "prowlarr" and message.endswith("1.20") and calls[0][1] == {"X-Api-Key": "KEY"}

    # Unknown port: tries Jackett, then Prowlarr.
    http, calls = _fake_http({"/api/v1/system/status": (200, b"{}")})
    ok, _m, kind = core.test_indexer("http://nas.lan:8080", "KEY", "auto", http_get=http)
    assert ok and kind == "prowlarr" and len(calls) == 2


def test_indexer_test_failures():
    http, _ = _fake_http({"/api/v1/system/status": (401, b"")})
    ok, message, _ = core.test_indexer("http://localhost:9696", "bad", "auto", http_get=http)
    assert not ok and "API key" in message
    import urllib.error
    http, _ = _fake_http({"localhost": urllib.error.URLError("Connection refused")})
    ok, message, _ = core.test_indexer("http://localhost:9117", "", "jackett", http_get=http)
    assert not ok and "Connection refused" in message
    assert not core.test_indexer("localhost:9117", "", "auto", http_get=http)[0]


def test_save_indexer_settings_merges(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(json.dumps({"guide_theme": "Sleek Freak", "torrent_buffer_episodes": 5}))
    core.save_indexer_settings(" http://localhost:9696 ", " KEY ", "prowlarr", settings_path=str(path))
    data = json.loads(path.read_text())
    assert data["guide_theme"] == "Sleek Freak" and data["torrent_buffer_episodes"] == 5
    assert data["torrent_indexer_url"] == "http://localhost:9696" and data["torrent_indexer_api_key"] == "KEY"
    core.save_indexer_settings("http://x:1", "", "weird", settings_path=str(tmp_path / "new.json"))
    assert json.loads((tmp_path / "new.json").read_text())["torrent_indexer_provider"] == "auto"


def test_launcher_and_desktop_entry():
    path, content = core.launcher_script("/opt/mw/venv/bin/python")
    if core.IS_WINDOWS:
        assert path.endswith(".bat") and "channelsurfer2000.py" in content
    else:
        assert path.endswith("MediaWave1337.sh")
        assert content.startswith("#!/bin/sh") and 'exec "/opt/mw/venv/bin/python" channelsurfer2000.py' in content
        entry = core.desktop_entry(path)
        assert f'Exec="{path}"' in entry and "Terminal=false" in entry


def test_enable_system_site_packages(tmp_path):
    cfg = tmp_path / "pyvenv.cfg"
    cfg.write_text("home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.10.12\n")
    assert core.enable_system_site_packages(str(tmp_path))
    text = cfg.read_text()
    assert "include-system-site-packages = true" in text and "= false" not in text
    assert not core.enable_system_site_packages(str(tmp_path / "missing"))


def test_create_venv_rebuilds_broken_env(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    python = core.venv_python(str(venv))
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()  # broken: interpreter path exists but no pip
    commands = []
    monkeypatch.setattr(core, "run_command", lambda cmd, out, **kw: commands.append(cmd) or 1)
    lines = []
    assert core.create_venv(lines.append, str(venv)) is False
    assert not venv.exists() or not os.path.isfile(python)
    assert commands and commands[0][1:3] == ["-m", "venv"]
    assert any("incomplete" in line for line in lines)
