"""Minimal reader for Riot RMAN release manifests (community-documented format).

Header: "RMAN", major u8, minor u8, flags u16, body offset u32, body length u32,
manifest id u64, decompressed length u32. Body: zstd-compressed FlatBuffer with
root fields 0=bundles, 2=files, 3=directories.
"""
import struct
from compression import zstd


class Table:
    def __init__(self, buf: bytes, pos: int):
        self.buf, self.pos = buf, pos
        vt = pos - struct.unpack_from("<i", buf, pos)[0]
        self.vt_len = struct.unpack_from("<H", buf, vt)[0]
        self.vt = vt

    def _off(self, field: int) -> int:
        o = 4 + 2 * field
        if o >= self.vt_len:
            return 0
        rel = struct.unpack_from("<H", self.buf, self.vt + o)[0]
        return self.pos + rel if rel else 0

    def scalar(self, field: int, fmt: str, default=0):
        o = self._off(field)
        return struct.unpack_from("<" + fmt, self.buf, o)[0] if o else default

    def _indirect(self, o: int) -> int:
        return o + struct.unpack_from("<I", self.buf, o)[0]

    def string(self, field: int) -> str:
        o = self._off(field)
        if not o:
            return ""
        s = self._indirect(o)
        n = struct.unpack_from("<I", self.buf, s)[0]
        return self.buf[s + 4 : s + 4 + n].decode("utf-8")

    def vector(self, field: int) -> tuple[int, int]:
        o = self._off(field)
        if not o:
            return 0, 0
        v = self._indirect(o)
        return v + 4, struct.unpack_from("<I", self.buf, v)[0]

    def tables(self, field: int) -> list["Table"]:
        start, n = self.vector(field)
        return [Table(self.buf, self._indirect(start + 4 * i)) for i in range(n)]

    def u64s(self, field: int) -> list[int]:
        start, n = self.vector(field)
        return list(struct.unpack_from(f"<{n}Q", self.buf, start)) if n else []


def load(path: str) -> dict:
    raw = open(path, "rb").read()
    assert raw[:4] == b"RMAN", "not a manifest"
    offset, length, manifest_id, _ = struct.unpack_from("<IIQI", raw, 8)
    body = zstd.decompress(raw[offset : offset + length])
    root = Table(body, struct.unpack_from("<I", body, 0)[0])

    # chunk id -> (bundle id, offset in bundle, compressed size, uncompressed size)
    chunks = {}
    for b in root.tables(0):
        bundle_id, off = b.scalar(0, "Q"), 0
        for c in b.tables(1):
            cid, csize, usize = c.scalar(0, "Q"), c.scalar(1, "I"), c.scalar(2, "I")
            chunks[cid] = (bundle_id, off, csize, usize)
            off += csize

    dirs = {d.scalar(0, "Q"): (d.scalar(1, "Q"), d.string(2)) for d in root.tables(3)}

    def dir_path(did: int) -> str:
        parts = []
        while did and did in dirs:
            parent, name = dirs[did]
            if name:
                parts.append(name)
            did = parent
        return "/".join(reversed(parts))

    files = {}
    for f in root.tables(2):
        name = f.string(3)
        d = dir_path(f.scalar(1, "Q"))
        path = f"{d}/{name}" if d else name
        files[path] = {"size": f.scalar(2, "I"), "chunks": f.u64s(7), "lang": f.scalar(4, "Q")}
    return {"id": f"{manifest_id:016X}", "chunks": chunks, "files": files}
