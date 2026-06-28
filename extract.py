"""
extract.py — Xbox 360 content container -> game folder (default.xex + assets).

Supports STFS packages (CON / LIVE / PIRS) directly; this is the XBLA / patch /
DLC container layout. Block math and struct offsets are replicated from rexglue's
stfs_container_device.cpp / stfs_xbox.h (which derive from xenia). Read-only
("read_only_format") packages — the common XBLA case — are fully handled,
including subdirectories. Read-write packages with *fragmented* files are flagged
(their secondary-hash-table chain selection is not replicated).

For an already-extracted game (a folder that contains default.xex) the folder is
used directly as the game root, so GoD/ISO titles extracted by other means (e.g.
god2iso.ps1) drop straight in with no copy.

Used as a module (extract_container -> (xex_path, game_dir)) or standalone:
    python extract.py <container> <out_dir>
"""
import os
import struct
import sys

BLOCK = 0x1000
L = [170, 28900, 4913000]
END = 0xFFFFFF


def u24le(b):
    return b[0] | (b[1] << 8) | (b[2] << 16)


class Stfs:
    """Minimal read-only STFS (CON/LIVE/PIRS) reader."""

    def __init__(self, path):
        self.f = open(path, "rb")
        d = self.f.read(0x400)
        self.magic = d[0:4]
        if self.magic not in (b"CON ", b"LIVE", b"PIRS"):
            raise ValueError("not an STFS package (magic=%r)" % self.magic)
        self.header_size = struct.unpack_from(">I", d, 0x340)[0]
        vd = 0x379
        if d[vd] != 0x24:
            for off in range(0x360, 0x390):
                if d[off] == 0x24 and d[off + 1] in (0, 1):
                    vd = off
                    break
        self.flags = d[vd + 2]
        self.read_only = self.flags & 1
        self.ft_block_count = struct.unpack_from("<H", d, vd + 3)[0]
        self.ft_block_number = u24le(d[vd + 5:vd + 8])
        self.bpht = 1 if self.read_only else 2
        self.base_off = (self.header_size + BLOCK - 1) & ~(BLOCK - 1)
        self.block_step = [L[0] + self.bpht, L[1] + (L[0] + 1) * self.bpht]
        self._hc = {}
        self._warned_rw = False

    def block_to_offset(self, bi):
        base, block = L[0], bi
        for _ in range(3):
            block += ((bi + base) // base) * self.bpht
            if bi < base:
                break
            base *= L[0]
        return self.base_off + (block << 12)

    def _hash_block_number(self, bi):
        if bi < L[0]:
            return 0
        block = (bi // L[0]) * self.block_step[0]
        block += ((bi // L[1]) + 1) * self.bpht
        return block if bi < L[1] else block + self.bpht

    def next_block(self, bi):
        # Read-write packages keep two hash tables and pick the active one per
        # level; we don't replicate that selection. Fragmented files in such a
        # package would chain wrong — warn loudly rather than corrupt silently.
        if not self.read_only and not self._warned_rw:
            self._warned_rw = True
            sys.stderr.write("[extract] WARNING: read-write STFS with a fragmented file; "
                             "block-chain selection is not fully handled — verify output.\n")
        hoff = self.base_off + (self._hash_block_number(bi) << 12)
        if hoff not in self._hc:
            self.f.seek(hoff)
            self._hc[hoff] = self.f.read(BLOCK)
        info = struct.unpack_from(">I", self._hc[hoff], (bi % L[0]) * 0x18 + 0x14)[0]
        return info & 0xFFFFFF

    def read_chain(self, start, length, contiguous):
        out = bytearray()
        bi, remaining = start, length
        while remaining and bi != END:
            n = min(BLOCK, remaining)
            self.f.seek(self.block_to_offset(bi))
            out += self.f.read(n)
            remaining -= n
            bi = bi + 1 if contiguous else self.next_block(bi)
        return bytes(out)

    def file_table(self):
        """All entries in table order (parent links index into this list)."""
        entries = []
        bi = self.ft_block_number
        for _ in range(self.ft_block_count):
            self.f.seek(self.block_to_offset(bi))
            blk = self.f.read(BLOCK)
            for m in range(BLOCK // 0x40):
                e = blk[m * 0x40:(m + 1) * 0x40]
                if e[0] == 0:
                    break
                flags = e[0x28]
                nlen = flags & 0x3F
                raw = e[0:nlen]
                try:
                    name = raw.decode("ascii")
                except UnicodeDecodeError:
                    name = raw.decode("latin-1")
                    sys.stderr.write("[extract] note: non-ASCII name %r\n" % raw)
                entries.append({
                    "name": name,
                    "contiguous": bool(flags & 0x40),
                    "directory": bool(flags & 0x80),
                    "parent": struct.unpack_from(">H", e, 0x32)[0],  # 0xFFFF = root
                    "start": u24le(e[0x2F:0x32]),
                    "length": struct.unpack_from(">I", e, 0x34)[0],
                })
            bi = self.next_block(bi)
            if bi == END:
                break
        return entries

    @staticmethod
    def rel_path(entries, idx):
        """Sanitized relative path of entries[idx], walking parent links."""
        parts = []
        seen = set()
        i = idx
        while i != 0xFFFF and 0 <= i < len(entries) and i not in seen:
            seen.add(i)
            comp = entries[i]["name"].replace("\\", "/").strip("/")
            comp = "/".join(c for c in comp.split("/") if c not in ("", ".", ".."))
            if comp:
                parts.append(comp)
            i = entries[i]["parent"]
        return "/".join(reversed(parts))


def read_package_meta(container):
    """Best-effort (title, title_id, cover_png_bytes) from an STFS package header.
    Returns a dict with None fields if unavailable (e.g. a plain folder)."""
    meta = {"title": None, "title_id": None, "cover": None}
    try:
        if os.path.isdir(container):
            return meta
        with open(container, "rb") as f:
            d = f.read(0xC000)
    except Exception:
        return meta
    if d[:4] not in (b"CON ", b"LIVE", b"PIRS"):
        return meta
    try:
        name = d[0x411:0x411 + 0x80].decode("utf-16-be", "ignore").split("\x00")[0].strip()
        meta["title"] = name or None
    except Exception:
        pass
    try:
        meta["title_id"] = "%08X" % struct.unpack_from(">I", d, 0x360)[0]
    except Exception:
        pass
    j = d.find(b"\x89PNG\r\n\x1a\n")
    if j >= 0:
        k = d.find(b"IEND\xaeB`\x82", j)
        if k > 0:
            meta["cover"] = d[j:k + 8]
    return meta


def _looks_like_xex(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"XEX2"
    except Exception:
        return False


def _find_default_xex(folder):
    cands = []
    for root, _, files in os.walk(folder):
        for fn in files:
            if fn.lower() == "default.xex":
                cands.append(os.path.join(root, fn))
    return sorted(cands, key=len)[0] if cands else None


def extract_container(src, out_dir, log=print):
    """Return (default_xex_path, game_dir). game_dir is the folder to use as the
    ReXGlue game root (it always contains the returned default.xex)."""
    # already-extracted folder: use it in place, no copy
    if os.path.isdir(src):
        xex = _find_default_xex(src)
        if not xex:
            raise SystemExit("folder has no default.xex: %s" % src)
        log("using extracted game folder in place: %s" % src)
        return xex, os.path.dirname(xex)

    os.makedirs(out_dir, exist_ok=True)

    if _looks_like_xex(src):
        dst = os.path.join(out_dir, "default.xex")
        if os.path.abspath(src) != os.path.abspath(dst):
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
        log("raw default.xex (no bundled assets) -> %s" % dst)
        return dst, out_dir

    with open(src, "rb") as f:
        magic = f.read(4)
    if magic in (b"CON ", b"LIVE", b"PIRS"):
        s = Stfs(src)
        ents = s.file_table()
        files = [(i, e) for i, e in enumerate(ents) if not e["directory"]]
        log("STFS %s: %d files" % (magic.decode("ascii", "ignore").strip(), len(files)))
        xex = None
        written = 0
        for i, e in files:
            rel = Stfs.rel_path(ents, i)
            dst = os.path.normpath(os.path.join(out_dir, rel))
            if os.path.commonpath([os.path.abspath(out_dir), os.path.abspath(dst)]) != \
               os.path.abspath(out_dir):
                sys.stderr.write("[extract] skipping path escape: %s\n" % rel)
                continue
            if not (os.path.exists(dst) and os.path.getsize(dst) == e["length"]):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as o:
                    o.write(s.read_chain(e["start"], e["length"], e["contiguous"]))
                written += e["length"]
            if e["name"].lower() == "default.xex":
                xex = dst
        log("extracted assets (%.1f MB written) -> %s" % (written / 1024 / 1024, out_dir))
        if not xex:
            xex = _find_default_xex(out_dir)
        if not xex:
            raise SystemExit("no default.xex in STFS package")
        return xex, out_dir

    raise SystemExit(
        "unsupported container (magic=%r). GoD/SVOD and raw ISO are not handled here; "
        "extract default.xex + assets first (e.g. god2iso.ps1) and pass the folder." % magic)


if __name__ == "__main__":
    print(extract_container(sys.argv[1], sys.argv[2]))
