"""Handler for faraesport://replay/<match_id>?t=<seconds>&api=<url>&token=<token> links.

Packaged (FaraEsportReplay.exe, see build.ps1): double-clicking the exe
installs it — copies itself to %LOCALAPPDATA%\\FaraEsport and registers the
link there, so the downloaded file can be moved or deleted afterwards.

    FaraEsportReplay.exe                     # install / update
    FaraEsportReplay.exe "D:\\Old major patches"   # = drag a folder onto the exe:
                                             # use the backed-up clients in it
    FaraEsportReplay.exe --unregister

From source (dev):
    python faraesport_link.py --register     # once per Windows user, no admin
    python faraesport_link.py --unregister
    python faraesport_link.py "faraesport://replay/2850472_0?t=918"   # what Windows runs

On a link: download the .rofl through the backend's /matches/<id>/replay
endpoint (cached under %LOCALAPPDATA%\\FaraEsport\\Replays), then open it at
the requested time. If that same game is already open, only seek; if another
replay is open, close it first. Runs under pythonw (no console), so failures
are shown in a message box and appended to helper.log.
"""
import ctypes
import json
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import winreg
from pathlib import Path

import open_replay as rp

SCHEME = "faraesport"
DEFAULT_API = "https://api.faraesport.win"
# A link can name the API to download from; only these are trusted, so a
# crafted link can't make the helper fetch from — or send the token to — elsewhere.
ALLOWED_APIS = {DEFAULT_API, "http://127.0.0.1:8000", "http://localhost:8000"}
MATCH_ID_RE = re.compile(r"^\w+$")

HOME = Path.home() / "AppData" / "Local" / "FaraEsport"
CACHE = HOME / "Replays"
LAST_MATCH = HOME / "last_match.txt"
LOG = HOME / "helper.log"
INSTALLED_EXE = HOME / "FaraEsportReplay.exe"
FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller exe


# --- registration ------------------------------------------------------------

def register(target: Path | None = None) -> None:
    if FROZEN:
        command = f'"{target or Path(sys.executable).resolve()}" "%1"'
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = f'"{pythonw}" "{Path(__file__).resolve()}" "%1"'
    root = rf"Software\Classes\{SCHEME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:FaraEsport Replay")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    print(f"Registered {SCHEME}:// -> {command}")


def unregister() -> None:
    root = rf"Software\Classes\{SCHEME}"
    for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, root + sub)
        except FileNotFoundError:
            pass
    print(f"Unregistered {SCHEME}://")


def add_clients(folder: Path) -> None:
    """A folder dropped onto the exe: remember the backed-up clients in it."""
    clients = rp.add_client_folder(folder)
    if not clients:
        return report(f"No League client found in {folder}")
    lines = "\n".join(f"{rp.exe_build(c / 'Game' / rp.GAME_EXE) or '?'}   {c}" for c in clients)
    ctypes.windll.user32.MessageBoxW(None, f"Clients added:\n\n{lines}", "FaraEsport replay", 0x40)


def install() -> None:
    """Exe double-clicked: copy it to a stable place and register that copy."""
    me = Path(sys.executable).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    if me != INSTALLED_EXE.resolve():
        try:
            INSTALLED_EXE.write_bytes(me.read_bytes())
        except PermissionError:
            raise SystemExit("Close any open replay link, then run the installer again.")
    register(INSTALLED_EXE)
    ctypes.windll.user32.MessageBoxW(None, "Replay links are ready.", "FaraEsport replay", 0x40)


# --- link handling -----------------------------------------------------------

def parse_link(link: str) -> tuple[str, float, str, str | None]:
    """-> (match_id, seconds, api, token)"""
    u = urllib.parse.urlsplit(link)
    match_id = urllib.parse.unquote(u.path.strip("/"))
    if u.scheme != SCHEME or u.netloc != "replay" or not MATCH_ID_RE.match(match_id):
        raise SystemExit(f"Unrecognised link: {link}")
    q = urllib.parse.parse_qs(u.query)
    seconds = rp.parse_time(q.get("t", ["0"])[0])
    api = q.get("api", [DEFAULT_API])[0].rstrip("/")
    if api not in ALLOWED_APIS:
        raise SystemExit(f"Link points at an untrusted server: {api}")
    return match_id, seconds, api, q.get("token", [None])[0]


def cached_replay(match_id: str, api: str, token: str | None) -> Path:
    path = CACHE / f"{match_id}.rofl"
    if path.exists():
        return path
    url = f"{api}/matches/{urllib.parse.quote(match_id)}/replay"
    if token:
        url += "?" + urllib.parse.urlencode({"token": token})
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            data = r.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail") or exc.reason
        except ValueError:
            detail = exc.reason
        reason = "not authorised — reopen the link from the web app" if exc.code == 401 else detail
        raise SystemExit(f"Replay download failed ({exc.code}): {reason}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Replay download failed: {exc.reason}") from exc
    if not data.startswith(b"RIOT"):
        raise SystemExit("Replay download failed: the server did not return a .rofl file.")
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def handle(link: str) -> None:
    match_id, seconds, api, token = parse_link(link)
    running = rp.replay_running()
    if running and LAST_MATCH.exists() and LAST_MATCH.read_text().strip() == match_id:
        rp.seek(seconds)
        return
    if not running and rp.api() is not None:
        raise SystemExit("League is already running a game. Close it before opening a replay.")

    # Everything that can fail on the new link (download, no client on its
    # build) happens BEFORE touching the open replay: force-closing a game that
    # is still loading, for a link that then fails anyway, was followed by every
    # later launch freezing at startup (2026-09-16).
    rofl = cached_replay(match_id, api, token)
    rp.pick_install(rp.rofl_build(rofl), None)
    if running:
        rp.close_replay()
    elif rp.game_processes():
        raise SystemExit("A replay is still loading. Wait for it, or close League, then try again.")
    HOME.mkdir(parents=True, exist_ok=True)
    LAST_MATCH.write_text(match_id)
    rp.open_replay(rofl, seconds)


def single_instance() -> bool:
    """False if another link is already being handled (e.g. a double click)."""
    ctypes.windll.kernel32.CreateMutexW(None, False, "FaraEsportReplayLink")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def report(message: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    ctypes.windll.user32.MessageBoxW(None, message.splitlines()[0][:500], "FaraEsport replay", 0x10)


def main() -> None:
    if FROZEN and len(sys.argv) == 1:
        try:
            return install()
        except SystemExit as exc:
            return report(str(exc))
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    arg = sys.argv[1]
    if arg == "--register":
        return register()
    if arg == "--unregister":
        return unregister()
    if Path(arg).is_dir():
        return add_clients(Path(arg))
    if not single_instance():
        return
    try:
        handle(arg)
    except SystemExit as exc:
        report(str(exc))
    except Exception:  # noqa: BLE001 — pythonw has no console; surface everything
        report(traceback.format_exc().strip().splitlines()[-1] + "\n" + traceback.format_exc())


if __name__ == "__main__":
    main()
