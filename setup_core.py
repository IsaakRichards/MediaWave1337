"""
setup_core.py

Non-GUI logic behind setup_wizard.py: system checks, dependency installation,
indexer connection test, settings and launcher creation. Standard library only,
because it runs before any dependencies are installed.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "MediaWave1337"
MAIN_SCRIPT = "channelsurfer2000.py"
VENV_DIR = os.path.join(APP_DIR, "venv")
MIN_PYTHON = (3, 9)
MIN_FREE_BYTES = 2 * 1024 ** 3
IS_WINDOWS = os.name == "nt"

# Import name for each runtime requirement, used to verify installs.
IMPORT_NAMES = {"pyside6": "PySide6", "requests": "requests", "libtorrent": "libtorrent"}

# System packages per Linux package manager.
LINUX_PACKAGES = {
    "apt": {"venv": ["python3-venv"], "tk": ["python3-tk"], "ffmpeg": ["ffmpeg"], "libtorrent": ["python3-libtorrent"]},
    "dnf": {"venv": [], "tk": ["python3-tkinter"], "ffmpeg": ["ffmpeg-free"], "libtorrent": ["rb_libtorrent-python3"]},
    "pacman": {"venv": [], "tk": ["tk"], "ffmpeg": ["ffmpeg"], "libtorrent": ["libtorrent-rasterbar"]},
    "zypper": {"venv": [], "tk": ["python3-tk"], "ffmpeg": ["ffmpeg"], "libtorrent": ["python3-libtorrent-rasterbar"]},
}
LINUX_INSTALL_ARGS = {
    "apt": ["apt-get", "install", "-y"],
    "dnf": ["dnf", "install", "-y"],
    "pacman": ["pacman", "-S", "--noconfirm", "--needed"],
    "zypper": ["zypper", "--non-interactive", "install"],
}
WINGET_FFMPEG = ["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements", "--accept-package-agreements"]

OutputFn = Callable[[str], None]


@dataclass
class CheckResult:
    name: str
    status: str  # "ok", "warn" or "fail"
    detail: str
    fix: Optional[str] = None  # key of a fix action the wizard can offer
    extra: Dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers

def base_python() -> str:
    """The console interpreter running setup (python.exe, even if launched via pythonw.exe)."""
    exe = sys.executable
    if IS_WINDOWS and os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(console):
            return console
    return exe


def venv_python(venv_dir: str = VENV_DIR, windowed: bool = False) -> str:
    if IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", "pythonw.exe" if windowed else "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def runtime_requirements(path: str = os.path.join(APP_DIR, "requirements.txt")) -> List[str]:
    """Requirement lines before the '# Development' marker, comments stripped."""
    reqs = []
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ["PySide6>=6.5", "requests", "libtorrent>=2.0"]
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("# development"):
            break
        requirement = stripped.split("#", 1)[0].strip()
        if requirement:
            reqs.append(requirement)
    return reqs


def requirement_name(requirement: str) -> str:
    for sep in ("[", "<", ">", "=", "!", "~", ";", " "):
        requirement = requirement.split(sep, 1)[0]
    return requirement.strip().lower()


def detect_linux_package_manager(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    for manager, binary in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"), ("zypper", "zypper")):
        if which(binary):
            return manager
    return None


def system_install_command(manager: str, keys: List[str], which: Callable[[str], Optional[str]] = shutil.which) -> Optional[List[str]]:
    """Command that installs the system packages for `keys`, elevated with pkexec when needed."""
    packages: List[str] = []
    for key in keys:
        packages += LINUX_PACKAGES.get(manager, {}).get(key, [])
    if not packages:
        return None
    command = LINUX_INSTALL_ARGS[manager] + packages
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if which("pkexec"):
            return ["pkexec"] + command
        return ["sudo"] + command
    return command


def run_command(command: List[str], output: OutputFn, cwd: str = APP_DIR, env: Optional[dict] = None) -> int:
    """Run a command, streaming combined stdout/stderr lines to `output`."""
    output("$ " + " ".join(command))
    creationflags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=creationflags,
        )
    except OSError as exc:
        output(f"Could not start {command[0]}: {exc}")
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        output(line.rstrip())
    return proc.wait()


def _quiet(command: List[str], timeout: int = 60) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ---------------------------------------------------------------- checks

def check_python(version: Tuple[int, ...] = tuple(sys.version_info[:3])) -> CheckResult:
    text = ".".join(str(v) for v in version)
    if tuple(version[:2]) >= MIN_PYTHON:
        return CheckResult("Python", "ok", f"Python {text} ({'64' if sys.maxsize > 2 ** 32 else '32'}-bit)")
    return CheckResult("Python", "fail", f"Python {text} is too old; {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.", fix="python")


def check_venv_support(python: Optional[str] = None) -> CheckResult:
    code, out = _quiet([python or base_python(), "-c", "import venv, ensurepip"])
    if code == 0:
        return CheckResult("Virtual environments", "ok", "Python can create a private environment for MediaWave1337.")
    fix = "venv" if not IS_WINDOWS and detect_linux_package_manager() == "apt" else None
    return CheckResult(
        "Virtual environments", "warn",
        "Python's venv module is not available. Install it (recommended) or install packages for your user account instead.",
        fix=fix,
    )


def find_ffmpeg_tool(name: str) -> Optional[str]:
    exe = name + (".exe" if IS_WINDOWS else "")
    found = shutil.which(name)
    if found:
        return found
    candidates = [os.path.join(APP_DIR, "bin", exe)]
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links", exe))
    for candidate in candidates:
        if os.path.isfile(candidate):
            code, _ = _quiet([candidate, "-version"], timeout=15)
            if code == 0:
                return candidate
    return None


def check_ffmpeg() -> CheckResult:
    ffmpeg, ffprobe = find_ffmpeg_tool("ffmpeg"), find_ffmpeg_tool("ffprobe")
    if ffmpeg and ffprobe:
        return CheckResult("FFmpeg", "ok", f"Found {ffprobe}")
    if IS_WINDOWS:
        fix = "ffmpeg" if shutil.which("winget") else None
    else:
        fix = "ffmpeg" if detect_linux_package_manager() else None
    return CheckResult(
        "FFmpeg", "warn",
        "FFmpeg/FFprobe not found. Episode lengths and the converter need them; playback still works.",
        fix=fix,
    )


def check_disk_space(path: str = APP_DIR) -> CheckResult:
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        return CheckResult("Disk space", "warn", "Could not read free disk space.")
    gib = free / 1024 ** 3
    if free >= MIN_FREE_BYTES:
        return CheckResult("Disk space", "ok", f"{gib:.1f} GB free")
    return CheckResult("Disk space", "warn", f"Only {gib:.1f} GB free. Each buffered SD episode needs roughly 200-700 MB.")


def run_system_checks() -> List[CheckResult]:
    return [check_python(), check_venv_support(), check_ffmpeg(), check_disk_space()]


def check_packages(python: str) -> Dict[str, Tuple[bool, str]]:
    """Import every runtime requirement with `python`; returns name -> (ok, version or error)."""
    results = {}
    for requirement in runtime_requirements():
        name = requirement_name(requirement)
        module = IMPORT_NAMES.get(name, name)
        code, out = _quiet([python, "-c", f"import {module} as m; print(getattr(m, '__version__', 'installed'))"])
        results[module] = (code == 0, out.splitlines()[-1] if out else "")
    return results


# ---------------------------------------------------------------- installs

def _has_pip(python: str) -> bool:
    return _quiet([python, "-m", "pip", "--version"])[0] == 0


def create_venv(output: OutputFn, venv_dir: str = VENV_DIR) -> bool:
    python = venv_python(venv_dir)
    if os.path.isfile(python):
        if _has_pip(python):
            output(f"Using existing environment in {venv_dir}")
            return True
        # A failed earlier attempt (e.g. missing ensurepip) leaves a venv without pip.
        output("The existing environment is incomplete (no pip); recreating it.")
        shutil.rmtree(venv_dir, ignore_errors=True)
    if run_command([base_python(), "-m", "venv", venv_dir], output) != 0:
        return False
    return _has_pip(python)


def enable_system_site_packages(venv_dir: str = VENV_DIR) -> bool:
    """Let the venv see system packages (e.g. a distro-packaged libtorrent)."""
    cfg = os.path.join(venv_dir, "pyvenv.cfg")
    try:
        with open(cfg, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    lines = [line for line in lines if not line.lower().startswith("include-system-site-packages")]
    lines.append("include-system-site-packages = true\n")
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return True


def pip_install(python: str, output: OutputFn, user: bool = False) -> Dict[str, bool]:
    """Install runtime requirements one at a time so one failure does not block the rest."""
    base = [python, "-m", "pip", "install", "--disable-pip-version-check"]
    if user:
        base.append("--user")
    run_command(base + ["--upgrade", "pip"], output)
    results = {}
    for requirement in runtime_requirements():
        output("")
        output(f"Installing {requirement} ...")
        results[requirement_name(requirement)] = run_command(base + [requirement], output) == 0
    return results


def install_system_packages(keys: List[str], output: OutputFn) -> bool:
    manager = detect_linux_package_manager()
    command = system_install_command(manager, keys) if manager else None
    if not command:
        output("No supported package manager found. Please install these manually: " + ", ".join(keys))
        return False
    return run_command(command, output) == 0


def install_ffmpeg(output: OutputFn) -> bool:
    if IS_WINDOWS:
        if not shutil.which("winget"):
            output("winget is not available. Download FFmpeg from https://www.gyan.dev/ffmpeg/builds/ "
                   "and put ffmpeg.exe and ffprobe.exe in the bin folder of MediaWave1337.")
            return False
        return run_command(WINGET_FFMPEG, output) == 0
    return install_system_packages(["ffmpeg"], output)


# ---------------------------------------------------------------- indexer

def guess_provider(url: str) -> str:
    try:
        port = urllib.parse.urlparse(url).port
    except ValueError:
        port = None
    return {9696: "prowlarr", 9117: "jackett"}.get(port, "auto")


def _http_get(url: str, headers: Optional[dict] = None, timeout: int = 8) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def test_indexer(url: str, api_key: str, provider: str = "auto", http_get=_http_get) -> Tuple[bool, str, str]:
    """Returns (ok, message, detected_provider)."""
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        return False, "Enter a URL starting with http:// or https://", provider
    if provider == "auto":
        provider = guess_provider(url)
    order = [provider] if provider in ("jackett", "prowlarr") else ["jackett", "prowlarr"]
    last_error = ""
    for kind in order:
        try:
            if kind == "prowlarr":
                status, body = http_get(f"{url}/api/v1/system/status", {"X-Api-Key": api_key})
                if status == 200:
                    version = json.loads(body.decode("utf-8", "replace") or "{}").get("version", "")
                    return True, f"Connected to Prowlarr {version}".strip(), "prowlarr"
            else:
                query = urllib.parse.urlencode({"t": "caps", "apikey": api_key})
                status, body = http_get(f"{url}/api/v2.0/indexers/all/results/torznab/api?{query}")
                if status == 200 and b"<caps" in body:
                    return True, "Connected to Jackett", "jackett"
                if status == 200 and b"error" in body.lower():
                    status = 401
            if status in (401, 403):
                return False, f"{kind.title()} answered but rejected the API key.", kind
            last_error = f"{kind.title()} returned HTTP {status}."
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = f"Could not reach {url}: {getattr(exc, 'reason', exc)}"
    return False, last_error or "No indexer answered.", provider


# ---------------------------------------------------------------- settings & launchers

def save_indexer_settings(url: str, api_key: str, provider: str, settings_path: str = os.path.join(APP_DIR, "app_settings.json")) -> None:
    """Merge indexer settings into the app's settings file (source runs keep it next to the app)."""
    try:
        with open(settings_path, encoding="utf-8") as fh:
            settings = json.load(fh)
        if not isinstance(settings, dict):
            settings = {}
    except (OSError, ValueError):
        settings = {}
    settings.update({
        "torrent_enabled": True,
        "torrent_indexer_url": url.strip(),
        "torrent_indexer_api_key": api_key.strip(),
        "torrent_indexer_provider": provider if provider in ("jackett", "prowlarr") else "auto",
    })
    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)


def launcher_script(python: str) -> Tuple[str, str]:
    """(path, content) of the run script for this platform."""
    if IS_WINDOWS:
        content = (
            "@echo off\r\n"
            'cd /d "%~dp0"\r\n'
            'set "PATH=%LOCALAPPDATA%\\Microsoft\\WinGet\\Links;%PATH%"\r\n'
            f'start "" "{python}" "{MAIN_SCRIPT}"\r\n'
        )
        return os.path.join(APP_DIR, f"{APP_NAME}.bat"), content
    content = (
        "#!/bin/sh\n"
        'cd "$(dirname "$0")" || exit 1\n'
        f'exec "{python}" {MAIN_SCRIPT} "$@"\n'
    )
    return os.path.join(APP_DIR, f"{APP_NAME}.sh"), content


def write_launcher(python: str) -> str:
    path, content = launcher_script(python)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    if not IS_WINDOWS:
        os.chmod(path, 0o755)
    return path


def icon_path() -> str:
    if IS_WINDOWS:
        return os.path.join(APP_DIR, "icons", "mediawave2000.ico")
    return os.path.join(APP_DIR, "icons", "generated", "mediawave2000", "256.png")


def desktop_entry(launcher: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Torrent-fed cable TV for CRT displays\n"
        f'Exec="{launcher}"\n'
        f"Path={APP_DIR}\n"
        f"Icon={icon_path()}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;Player;\n"
    )


def desktop_dir() -> str:
    if not IS_WINDOWS:
        code, out = _quiet(["xdg-user-dir", "DESKTOP"], timeout=5)
        if code == 0 and out and os.path.isdir(out):
            return out
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_shortcuts(python: str, desktop: bool, menu: bool, output: OutputFn) -> List[str]:
    """Create the run script plus desktop / app-menu shortcuts. Returns created paths."""
    created = [write_launcher(python)]
    if IS_WINDOWS:
        windowed = python.replace("python.exe", "pythonw.exe")
        target = windowed if os.path.isfile(windowed) else python
        folders = []
        if desktop:
            folders.append(desktop_dir())
        if menu:
            folders.append(os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"))
        for folder in folders:
            link = os.path.join(folder, f"{APP_NAME}.lnk")
            script = (
                "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MW_LINK); "
                "$s.TargetPath = $env:MW_TARGET; $s.Arguments = $env:MW_ARGS; "
                "$s.WorkingDirectory = $env:MW_DIR; $s.IconLocation = $env:MW_ICON; $s.Save()"
            )
            env = dict(os.environ, MW_LINK=link, MW_TARGET=target, MW_ARGS=f'"{MAIN_SCRIPT}"', MW_DIR=APP_DIR, MW_ICON=icon_path())
            if run_command(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], output, env=env) == 0:
                created.append(link)
        return created
    entry = desktop_entry(created[0])
    targets = []
    if menu:
        targets.append(os.path.join(os.path.expanduser("~"), ".local", "share", "applications", "mediawave1337.desktop"))
    if desktop:
        targets.append(os.path.join(desktop_dir(), "mediawave1337.desktop"))
    for target in targets:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(entry)
        os.chmod(target, 0o755)
        created.append(target)
        if shutil.which("gio") and target.startswith(desktop_dir()):
            _quiet(["gio", "set", target, "metadata::trusted", "true"], timeout=5)
    return created


def launch_app(python: str) -> None:
    env = dict(os.environ)
    if IS_WINDOWS:
        env["PATH"] = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links") + os.pathsep + env.get("PATH", "")
        windowed = python.replace("python.exe", "pythonw.exe")
        python = windowed if os.path.isfile(windowed) else python
        subprocess.Popen([python, MAIN_SCRIPT], cwd=APP_DIR, env=env, creationflags=subprocess.DETACHED_PROCESS)
    else:
        subprocess.Popen([python, MAIN_SCRIPT], cwd=APP_DIR, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def platform_label() -> str:
    if IS_WINDOWS:
        return f"Windows {platform.release()}"
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()
