# FaraEsport replay helper

Opens a League of Legends replay at a given game time when you click a replay
icon in the FaraEsport analytics app.

## Install

1. Download **[FaraEsportReplay.exe](https://github.com/CoachFara/faraesport-replay-helper/releases/latest/download/FaraEsportReplay.exe)**.
2. Double-click it. Windows SmartScreen warns that the file is unsigned: click
   **More info → Run anyway**.
3. A "Replay links are ready." box confirms the install. The downloaded file can
   be deleted: the helper copies itself to `%LOCALAPPDATA%\FaraEsport\`.

The first time you click a replay icon, your browser asks to open the link:
tick "always allow".

## Old patches

A replay only plays on the exact client build that recorded it. To keep an old
patch playable, copy the whole client folder aside before Riot updates it, then
**drag that folder (or a folder of such copies) onto `FaraEsportReplay.exe`**.

## Uninstall

```
%LOCALAPPDATA%\FaraEsport\FaraEsportReplay.exe --unregister
```

## Development

Python 3.14, standard library only.

| File | Role |
|---|---|
| `faraesport_link.py` | `faraesport://` link handler, install, client folders |
| `open_replay.py` | pick the matching client, launch, wait, seek (also a CLI) |
| `restore_client.py` + `rman.py` | rebuild a client folder to an older release from Riot's CDN |
| `build.ps1` | builds `dist\FaraEsportReplay.exe` with PyInstaller |

```
python open_replay.py "path\to\game.rofl" 15:18 --dry-run
powershell -ExecutionPolicy Bypass -File build.ps1
```

Release: build, then attach `dist\FaraEsportReplay.exe` to a new GitHub release.
Keep old link formats working: players update the helper later than the web app.
