"""Rebuild a League client folder to an older release, from Riot's own CDN.

Use case: Riot moved a tournament realm to a new build (or a patch got
half-applied) and replays recorded on the previous build no longer play. Given
the previous release's manifest, this rewrites only the files that differ,
reusing every piece of data already on disk and downloading the rest.

    python restore_client.py "D:\\Old major patches\\16.16" 1EAEB997C4491F68.manifest 36C28DF549304177.manifest

  client    a client folder (the one holding Game\\ and Game.manifest)
  target    manifest of the release to restore
  current   manifest of the release currently on disk (its Game.manifest)

Release ids come from the client's own log, Logs\\LeagueClient Logs\\*_LeagueClient.log:
"Patcher Latest published game release ID <new> is different than current release ID <old>".
Manifests: https://lol.secure.dyn.riotcdn.net/channels/public/releases/<ID>.manifest

Safe to re-run: downloads are cached, files are built in a staging folder and
only swapped in once ALL of them built and passed their size checks; swapped
files are recorded so a re-run knows which release each file on disk holds.
"""
import os
import re
import shutil
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from compression import zstd

import rman

BUNDLE_URL = "https://lol.dyn.riotcdn.net/channels/public/bundles/{:016X}.bundle"
LOCALE_RE = re.compile(r"\.([a-z]{2}_[A-Z]{2})\.wad\.client$")
MERGE_GAP = 256 * 1024  # fetch neighbouring chunks of a bundle in one range request
MAX_DOWNLOAD = 2.5e9    # stop rather than silently pull far more than announced


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def fetch(url: str, start: int, end: int, dest: Path) -> None:
    """Download bytes [start, end) of `url` to `dest`, skipping if already there."""
    if dest.exists() and dest.stat().st_size == end - start:
        return
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end - 1}", "User-Agent": "faraesport-restore"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            if len(data) != end - start:
                raise OSError(f"short read {len(data)} != {end - start}")
            tmp = dest.with_name(dest.name + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
            return
        except OSError as exc:
            if attempt == 3:
                raise
            log(f"retry {url} [{start}-{end}]: {exc}")
            time.sleep(2 * (attempt + 1))


class Restore:
    def __init__(self, client: Path, target_path: str, current_path: str):
        self.client, self.target_path = client, target_path
        self.game = client / "Game"
        work = client.parent / "_restore" / client.name
        self.cache, self.staging, self.swapped_log = work / "cache", work / "staging", work / "swapped.txt"
        self.target, self.current = rman.load(target_path), rman.load(current_path)
        self.swapped = set(self.swapped_log.read_text(encoding="utf-8").splitlines()) if self.swapped_log.exists() else set()
        self.locales = {m.group(1) for p in self.current["files"]
                        if (m := LOCALE_RE.search(p)) and (self.game / p).exists()}
        self.on_disk = self._map_disk()
        self.located: dict[int, tuple[Path, int, int]] = {}

    def installed(self, p: str) -> bool:
        m = LOCALE_RE.search(p)
        return m is None or m.group(1) in self.locales

    def disk_version(self, p: str) -> dict | None:
        """Which release's copy of `p` is on disk (None = unknown, don't trust)."""
        fp = self.game / p
        if not fp.exists():
            return None
        size = fp.stat().st_size
        if p in self.swapped:
            return self.target
        if p in self.current["files"] and size == self.current["files"][p]["size"]:
            return self.current
        if p in self.target["files"] and size == self.target["files"][p]["size"]:
            return self.target
        return None

    def _map_disk(self) -> dict[int, tuple[Path, int, int]]:
        """chunk id -> (file on disk, offset, size) for every trusted file."""
        found = {}
        for p in set(self.target["files"]) | set(self.current["files"]):
            manifest = self.disk_version(p) if self.installed(p) else None
            if manifest is None:
                continue
            off = 0
            for cid in manifest["files"][p]["chunks"]:
                usize = manifest["chunks"][cid][3]
                found.setdefault(cid, (self.game / p, off, usize))
                off += usize
        return found

    def todo(self) -> list[str]:
        return [p for p in self.target["files"] if self.installed(p) and self.disk_version(p) is not self.target
                and not (self.disk_version(p) is self.current and self.current["files"][p]["chunks"] == self.target["files"][p]["chunks"])]

    def download(self, chunk_ids: set[int]) -> None:
        by_bundle: dict[int, list[tuple[int, int, int]]] = {}
        for cid in chunk_ids:
            bundle, off, csize, _ = self.target["chunks"][cid]
            by_bundle.setdefault(bundle, []).append((off, off + csize, cid))
        ranges = []
        for bundle, items in by_bundle.items():
            items.sort()
            group = [items[0]]
            for it in items[1:] + [None]:
                if it and it[0] - group[-1][1] <= MERGE_GAP:
                    group.append(it)
                    continue
                start, end = group[0][0], group[-1][1]
                dest = self.cache / f"{bundle:016X}_{start}_{end}.bin"
                ranges.append((BUNDLE_URL.format(bundle), start, end, dest))
                for off, e, cid in group:
                    self.located[cid] = (dest, off - start, e - off)
                group = [it] if it else []
        # The limit applies to what is actually fetched: merged ranges include the gaps between chunks.
        fetched = sum(end - start for _, start, end, _ in ranges)
        if fetched > MAX_DOWNLOAD:
            raise SystemExit(f"Download would be {fetched / 1e9:.2f} GB, over the {MAX_DOWNLOAD / 1e9:.1f} GB limit. Stopping.")
        self.cache.mkdir(parents=True, exist_ok=True)
        log(f"downloading {len(ranges)} ranges ({fetched / 1e9:.2f} GB)...")
        done, t0 = 0, time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fetch, *r): r for r in ranges}
            for i, fut in enumerate(as_completed(futures), 1):
                fut.result()
                done += futures[fut][2] - futures[fut][1]
                if i % 100 == 0 or i == len(ranges):
                    rate = done / 1e6 / max(time.monotonic() - t0, 1e-3)
                    log(f"  {i}/{len(ranges)} ranges, {done / 1e9:.2f} GB, {rate:.1f} MB/s")

    def chunk(self, cid: int, from_disk: bool = True) -> bytes:
        usize = self.target["chunks"][cid][3]
        if from_disk and cid in self.on_disk:
            fp, off, _ = self.on_disk[cid]
            with fp.open("rb") as fh:
                fh.seek(off)
                data = fh.read(usize)
        else:
            dest, off, csize = self.located[cid]
            with dest.open("rb") as fh:
                fh.seek(off)
                data = zstd.decompress(fh.read(csize))
        if len(data) != usize:
            raise OSError(f"chunk {cid:016X}: {len(data)} bytes, expected {usize}")
        return data

    def self_test(self) -> None:
        """Rebuild an intact on-disk file purely from CDN downloads; must be byte-identical."""
        candidates = [p for p in self.target["files"]
                      if self.installed(p) and self.disk_version(p) is self.current
                      and self.current["files"][p]["chunks"] == self.target["files"][p]["chunks"]
                      and 1e6 < self.target["files"][p]["size"] < 8e6 and len(self.target["files"][p]["chunks"]) >= 3]
        p = candidates[0]
        chunks = self.target["files"][p]["chunks"]
        self.download(set(chunks))
        rebuilt = b"".join(self.chunk(c, from_disk=False) for c in chunks)
        if rebuilt != (self.game / p).read_bytes():
            raise SystemExit(f"SELF-TEST FAILED: {p} rebuilt from the CDN differs from the file on disk. Nothing changed.")
        log(f"self-test OK: {p} ({len(rebuilt):,} bytes) rebuilt from CDN is byte-identical to disk")

    def run(self) -> None:
        log(f"target {self.target['id']}, current {self.current['id']}, locales {sorted(self.locales)}")
        self.self_test()

        todo = self.todo()
        needed = {cid for p in todo for cid in self.target["files"][p]["chunks"]} - self.on_disk.keys()
        total = sum(self.target["chunks"][c][2] for c in needed)
        log(f"{len(todo)} files to restore, {len(needed)} chunks to download ({total / 1e9:.2f} GB)")
        if total > MAX_DOWNLOAD:
            raise SystemExit(f"Download would be {total / 1e9:.2f} GB, over the {MAX_DOWNLOAD / 1e9:.1f} GB limit. Stopping.")
        self.download(needed)

        # Build everything in staging first: on-disk sources must stay untouched until all are built.
        log("building files in staging...")
        for i, p in enumerate(todo, 1):
            out, size = self.staging / p, self.target["files"][p]["size"]
            if not (out.exists() and out.stat().st_size == size):
                out.parent.mkdir(parents=True, exist_ok=True)
                tmp = out.with_name(out.name + ".part")
                with tmp.open("wb") as fh:
                    for cid in self.target["files"][p]["chunks"]:
                        fh.write(self.chunk(cid))
                if tmp.stat().st_size != size:
                    raise OSError(f"{p}: built {tmp.stat().st_size} bytes, expected {size}")
                tmp.replace(out)
            if i % 25 == 0 or i == len(todo):
                log(f"  built {i}/{len(todo)}")

        log("swapping restored files in...")
        with self.swapped_log.open("a", encoding="utf-8") as record:
            for p in todo:
                dest = self.game / p
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.staging / p, dest)
                record.write(p + "\n")
                record.flush()
        shutil.copyfile(self.target_path, self.client / "Game.manifest")
        shutil.rmtree(self.staging, ignore_errors=True)
        log(f"done: {len(todo)} files restored to release {self.target['id']}. Download cache kept in {self.cache}")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    Restore(Path(sys.argv[1]), sys.argv[2], sys.argv[3]).run()


if __name__ == "__main__":
    main()
