"""Open a League of Legends replay (.rofl) at a given game time.

    python open_replay.py "<path\\to\\game.rofl>" 918
    python open_replay.py "<path\\to\\game.rofl>" 15:18 --dry-run

Standalone, stdlib only, Windows only. Steps:
  1. read the game build from the .rofl header (e.g. 16.16.809.3269);
  2. pick the installed League client with the same build — a pro setup has
     several (live + tournament realms loltmnt01..06), and a replay only plays
     on the exact build that recorded it;
  3. make sure the Replay API is enabled in that install's Config/game.cfg;
  4. launch the game on the replay, wait for the Replay API on port 2999,
     then seek to the requested time.
"""
import argparse
import ctypes
import json
import re
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPLAY_API = "https://127.0.0.1:2999/replay/playback"
GAME_EXE = "League of Legends.exe"
BUILD_RE = re.compile(rb"\d+\.\d+\.\d+\.\d+")
RIOT_DATA = Path(r"C:\ProgramData\Riot Games")
CLIENT_FOLDERS = Path.home() / "AppData" / "Local" / "FaraEsport" / "client_folders.txt"

# The Replay API only listens on localhost with a self-signed certificate.
_SSL = ssl._create_unverified_context()


# --- replay file -------------------------------------------------------------

def rofl_build(rofl: Path) -> str:
    with rofl.open("rb") as f:
        head = f.read(64)
    if not head.startswith(b"RIOT"):
        raise SystemExit(f"Not a .rofl replay: {rofl}")
    m = BUILD_RE.search(head)
    if not m:
        raise SystemExit(f"No game build found in replay header: {rofl}")
    return m.group().decode()


# --- League installs ---------------------------------------------------------

def exe_build(exe: Path) -> str | None:
    """File version of an .exe, e.g. '16.16.809.3269'."""
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(str(exe), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(str(exe), 0, size, buf):
        return None
    ptr, length = ctypes.c_void_p(), ctypes.c_uint()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
        return None
    # VS_FIXEDFILEINFO: signature, struct version, FileVersionMS, FileVersionLS
    ms, ls = struct.unpack_from("<II", ctypes.string_at(ptr.value, 16), 8)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def game_dir(install: Path) -> Path:
    """Folder holding the game exe: Game\\ in a full install, or the folder
    itself when only the Game folder was backed up."""
    return install / "Game" if (install / "Game" / GAME_EXE).exists() else install


def is_client(d: Path) -> bool:
    return (game_dir(d) / GAME_EXE).exists()


def backup_clients() -> list[Path]:
    """Manually backed-up clients (a whole install copied aside to keep an old
    patch playable). CLIENT_FOLDERS lists one folder per line: a client itself,
    or a folder of clients such as 'D:\\Old major patches'."""
    found: list[Path] = []
    if not CLIENT_FOLDERS.exists():
        return found
    for line in CLIENT_FOLDERS.read_text(encoding="utf-8").splitlines():
        folder = Path(line.strip())
        if not line.strip() or not folder.is_dir():
            continue
        if is_client(folder):
            found.append(folder)
        else:
            found += sorted(d for d in folder.iterdir() if d.is_dir() and is_client(d))
    return found


def add_client_folder(folder: Path) -> list[Path]:
    """Remember `folder`; returns the clients it contains (empty = nothing added)."""
    folder = folder.resolve()
    clients = [folder] if is_client(folder) else sorted(d for d in folder.iterdir() if d.is_dir() and is_client(d))
    if clients:
        known = CLIENT_FOLDERS.read_text(encoding="utf-8").splitlines() if CLIENT_FOLDERS.exists() else []
        if str(folder).lower() not in {k.strip().lower() for k in known}:
            CLIENT_FOLDERS.parent.mkdir(parents=True, exist_ok=True)
            CLIENT_FOLDERS.write_text("\n".join([*known, str(folder)]) + "\n", encoding="utf-8")
    return clients


def install_dirs() -> list[Path]:
    """Every usable League install: manual backups first (Riot can patch its own
    installs at any moment), then every install the Riot Client knows about."""
    found: list[Path] = backup_clients()
    for yaml in RIOT_DATA.glob("Metadata/league_of_legends.*/*.product_settings.yaml"):
        m = re.search(r'product_install_full_path:\s*"(.+?)"', yaml.read_text(errors="ignore"))
        if m:
            found.append(Path(m.group(1).replace("\\\\", "\\")))
    installs_json = RIOT_DATA / "RiotClientInstalls.json"
    if installs_json.exists():
        data = json.loads(installs_json.read_text(errors="ignore"))
        found += [Path(p) for p in data.get("associated_client", {})]
    found += [Path(r"C:\Riot Games\League of Legends")]

    unique: dict[str, Path] = {}
    for d in found:
        if is_client(d):
            unique.setdefault(str(d.resolve()).lower(), d.resolve())
    return list(unique.values())


def pick_install(build: str, override: Path | None) -> Path:
    candidates = [override] if override else install_dirs()
    builds = {d: exe_build(game_dir(d) / GAME_EXE) for d in candidates}
    for d, b in builds.items():
        if b == build:
            return d
    listing = "\n".join(f"  {b or '?':<16} {d}" for d, b in builds.items()) or "  (none found)"
    raise SystemExit(
        f"No League client matches replay build {build}. "
        f"Drop a backed-up client folder onto FaraEsportReplay.exe to add it.\n"
        f"Known clients:\n{listing}"
    )


def enable_replay_api(install: Path) -> None:
    cfg = install / "Config" / "game.cfg"
    text = cfg.read_text(errors="ignore") if cfg.exists() else ""
    if re.search(r"^EnableReplayApi=1\s*$", text, re.M):
        return
    if re.search(r"^EnableReplayApi=", text, re.M):
        text = re.sub(r"^EnableReplayApi=.*$", "EnableReplayApi=1", text, flags=re.M)
    elif re.search(r"^\[General\]\s*$", text, re.M):
        text = re.sub(r"^\[General\]\s*$", "[General]\nEnableReplayApi=1", text, count=1, flags=re.M)
    else:
        text = "[General]\nEnableReplayApi=1\n" + text
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)
    print(f"Enabled Replay API in {cfg}")


# --- Replay API --------------------------------------------------------------

def api(payload: dict | None = None) -> dict | None:
    """GET (payload None) or POST the playback state; None if not reachable."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(REPLAY_API, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=2) as r:
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


# No console window flashing up when the packaged (windowed) helper shells out.
_NO_WINDOW = 0x08000000


def game_processes(not_responding: bool = False) -> int:
    """How many League game processes run (optionally: only frozen ones)."""
    cmd = ["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/FO", "CSV", "/NH"]
    if not_responding:
        cmd += ["/FI", "STATUS eq NOT RESPONDING"]
    out = subprocess.run(cmd, capture_output=True, text=True, creationflags=_NO_WINDOW).stdout
    return out.count(f'"{GAME_EXE}"')


FREEZE_GRACE = 15  # seconds "not responding" before force-closing a loading game


def wait_for_replay(timeout: float) -> dict:
    """Wait for the Replay API; fail fast if the game closes, give a frozen one
    FREEZE_GRACE seconds (a heavy load can briefly stop responding) before closing it."""
    deadline = time.monotonic() + timeout
    frozen_since = None
    while time.monotonic() < deadline:
        state = api()
        if state and state.get("length", 0) > 0:
            return state
        if not game_processes():
            raise SystemExit("League closed while loading the replay.")
        if game_processes(not_responding=True):
            frozen_since = frozen_since or time.monotonic()
            if time.monotonic() - frozen_since >= FREEZE_GRACE:
                kill_game()
                raise SystemExit("League froze while loading the replay and was closed.")
        else:
            frozen_since = None
        time.sleep(1)
    raise SystemExit(f"Replay did not load within {timeout:.0f}s.")


def seek(target: float, attempts: int = 10) -> float:
    # The first seeks right after loading can be ignored; retry until it sticks.
    for _ in range(attempts):
        api({"time": target})
        time.sleep(1.5)
        state = api() or {}
        if not state.get("seeking") and abs(state.get("time", -1e9) - target) < 5:
            return state["time"]
    raise SystemExit(f"Replay is open but seeking to {target:.0f}s did not take.")


def replay_running() -> bool:
    state = api()
    return bool(state and state.get("length", 0) > 0)


def kill_game() -> None:
    subprocess.run(["taskkill", "/IM", GAME_EXE, "/F"], capture_output=True, creationflags=_NO_WINDOW)


def close_replay(timeout: float = 20) -> None:
    """Kill the running replay. Only call after replay_running() confirmed one —
    the same exe name is used by live games."""
    kill_game()
    deadline = time.monotonic() + timeout
    while api() is not None and time.monotonic() < deadline:
        time.sleep(0.5)


def launch_command(rofl: Path, install: Path) -> list[str]:
    return [
        str(game_dir(install) / GAME_EXE),
        str(rofl),
        f"-GameBaseDir={install}",
        "-SkipRads",
        "-SkipBuild",
        "-EnableLNP",
        "-UseNewX3D=1",
        "-UseNewX3DFramebuffers=1",
    ]


def open_replay(rofl: Path, seconds: float, league_dir: Path | None = None, timeout: float = 30) -> float:
    """Launch `rofl` on its matching client and seek to `seconds`. Returns the landed time."""
    install = pick_install(rofl_build(rofl), league_dir)
    enable_replay_api(install)
    subprocess.Popen(launch_command(rofl, install), cwd=game_dir(install))
    wait_for_replay(timeout)
    return seek(seconds)


# --- CLI ---------------------------------------------------------------------

def parse_time(value: str) -> float:
    """'918', '15:18' or '1:02:03' -> seconds."""
    seconds = 0.0
    for part in value.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def main() -> None:
    p = argparse.ArgumentParser(description="Open a .rofl replay at a given game time.")
    p.add_argument("rofl", type=Path)
    p.add_argument("time", type=parse_time, help="seconds, or mm:ss")
    p.add_argument("--league-dir", type=Path, help="force a League install folder")
    p.add_argument("--timeout", type=float, default=30, help="max seconds to wait for loading")
    p.add_argument("--dry-run", action="store_true", help="resolve everything, launch nothing")
    args = p.parse_args()

    rofl = args.rofl.resolve()
    if not rofl.exists():
        raise SystemExit(f"Replay not found: {rofl}")
    build = rofl_build(rofl)
    install = pick_install(build, args.league_dir)
    print(f"Replay build {build} -> {install}")
    if args.dry_run:
        print("Would run:", subprocess.list2cmdline(launch_command(rofl, install)))
        print(f"Then seek to {args.time:.0f}s")
        return

    if api() is not None:
        raise SystemExit("A replay is already running (port 2999 answers). Close it first.")
    print("Loading replay...")
    landed = open_replay(rofl, args.time, args.league_dir, args.timeout)
    print(f"At {int(landed // 60)}:{int(landed % 60):02d}")


if __name__ == "__main__":
    sys.exit(main())
