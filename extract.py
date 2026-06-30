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


def title_from_filename(container):
    """A human-readable game title guessed from a container path: drop the
    extension and the region/dump tags '(USA, Europe)', '[!]', etc.
    'Captain America - Super Soldier (USA, Europe).iso' -> 'Captain America - Super Soldier'."""
    import re
    if not container:
        return None
    base = os.path.basename(str(container).rstrip("/\\"))
    stem = base if os.path.isdir(container) else os.path.splitext(base)[0]
    stem = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", "", stem)   # strip (...) [...] {...}
    stem = re.sub(r"\s+", " ", stem).strip(" -_.")
    return stem or None


def project_name_from_title(title, fallback="game"):
    """Sanitize a title into a valid rexglue project identifier
    (lowercase letters/digits/underscore, not starting with a digit)."""
    import re
    if not title:
        return fallback
    n = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40].strip("_")
    if not n or n[0].isdigit():
        n = "g_" + n if n else fallback
    return n or fallback


def read_package_meta(container):
    """Best-effort (title, title_id, cover_png_bytes) from a container.
    STFS packages yield the real title/id/cover from the header; ISO/GoD/folder
    containers fall back to a title derived from the file name (so the GUI still
    shows a sensible name instead of the generic 'game'). None fields if nothing
    is available."""
    meta = {"title": None, "title_id": None, "cover": None}
    fallback_title = title_from_filename(container)
    try:
        if os.path.isdir(container):
            meta["title"] = fallback_title
            return meta
        with open(container, "rb") as f:
            d = f.read(0xC000)
    except Exception:
        meta["title"] = fallback_title
        return meta
    if d[:4] not in (b"CON ", b"LIVE", b"PIRS"):
        # ISO (GDFX), GoD (SVOD) or anything else: no STFS header -> use the
        # file name as the display title.
        meta["title"] = fallback_title
        return meta
    try:
        name = d[0x411:0x411 + 0x80].decode("utf-16-be", "ignore").split("\x00")[0].strip()
        meta["title"] = name or fallback_title
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


# --- Title Update (XEX delta-patch) detection -------------------------------
# An Xbox 360 Title Update ships as an STFS package (Content Type 0x000B0000)
# wrapping a single default.xexp -- itself a XEX2 carrying a Delta Patch
# Descriptor optional header (id 0x000005FF). To recompile the version a user
# actually runs (e.g. Skate 3 3.0.0.0 -> 3.0.3.0) the patch must reach codegen.
# These helpers are generic and read-only: they return None (no-op) when there
# is no TU, so a base-only game is unaffected.
XEX_HEADER_DELTA_PATCH_DESCRIPTOR = 0x000005FF
XEX_HEADER_EXECUTION_INFO = 0x00040006
TU_CONTENT_TYPE = 0x000B0000


def _read_head(path, n=0x4000):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def _xex_opt_headers(d):
    """Yield (id, value) for each XEX2 optional header. value is the be32 that
    follows the id -- a file offset to the data for the ids we read (0x40006,
    0x5FF). Empty if d is not a XEX2."""
    if d[:4] != b"XEX2" or len(d) < 0x18:
        return
    count = struct.unpack_from(">I", d, 0x14)[0]
    for i in range(min(count, (len(d) - 0x18) // 8)):
        yield struct.unpack_from(">II", d, 0x18 + i * 8)


def _xex_opt_value(path, want_id):
    """Return the data-offset value of optional header want_id (or None)."""
    d = _read_head(path)
    for hid, val in _xex_opt_headers(d):
        if hid == want_id:
            return val
    return None


def _xex_version_tuple(v):
    """Decode a xex2_version word: major:4, minor:4, build:16, qfe:8 packed from the
    low bits up. Verified against the real Skate 3 TU3: source 0x3 -> 3.0.0.0,
    target 0x303 -> 3.0.3.0."""
    return (v & 0xF, (v >> 4) & 0xF, (v >> 8) & 0xFFFF, (v >> 24) & 0xFF)


def xex_version_str(v):
    return "%d.%d.%d.%d" % _xex_version_tuple(v)


def is_xex_patch(path):
    """True iff path is a XEX2 carrying a Delta Patch Descriptor (a TU patch)."""
    return _xex_opt_value(path, XEX_HEADER_DELTA_PATCH_DESCRIPTOR) is not None


def read_delta_descriptor(path):
    """For a .xexp patch return {'source_version','target_version'} (raw be32s)
    from the 0x5FF descriptor (size @0, target_version @4, source_version @8),
    else None."""
    o = _xex_opt_value(path, XEX_HEADER_DELTA_PATCH_DESCRIPTOR)
    if o is None:
        return None
    d = _read_head(path, o + 0x60)
    if o + 12 > len(d):
        return None
    _size, target, source = struct.unpack_from(">III", d, o)
    return {"source_version": source, "target_version": target}


def xex_base_version(path):
    """A base xex's own version (execution_info 0x40006, version_value @0x4) so a
    patch's source_version can be matched against it. None if unavailable."""
    o = _xex_opt_value(path, XEX_HEADER_EXECUTION_INFO)
    if o is None:
        return None
    d = _read_head(path, o + 0x20)
    if o + 8 > len(d):
        return None
    return struct.unpack_from(">I", d, o + 4)[0]


def _stfs_content_type(container):
    """STFS Content Type @0x344, or None if not an STFS package / unreadable."""
    d = _read_head(container, 0x400)
    if d[:4] not in (b"CON ", b"LIVE", b"PIRS") or len(d) < 0x348:
        return None
    return struct.unpack_from(">I", d, 0x344)[0]


def _extract_xexp_from_stfs(container, out_dir, log):
    """Pull the lone default.xexp out of a TU STFS package into out_dir."""
    try:
        s = Stfs(container)
    except Exception:
        return None
    try:
        for e in s.file_table():
            if not e["directory"] and e["name"].lower().endswith(".xexp"):
                blob = s.read_chain(e["start"], e["length"], e["contiguous"])
                dest = os.path.join(out_dir, e["name"])
                with open(dest, "wb") as g:
                    g.write(blob)
                log("  extracted %s (%d bytes) from TU package %s"
                    % (e["name"], len(blob), os.path.basename(container)))
                return dest
    except Exception as ex:
        log("  TU package read failed (%s): %s" % (os.path.basename(container), ex))
    finally:
        try:
            s.f.close()
        except Exception:
            pass
    return None


def detect_title_update(game_dir, container, base_xex, log=lambda m: None):
    """Find a Title Update XEX delta-patch for the just-extracted base game and
    stage its default.xexp into game_dir. Returns the staged .xexp path or None.

    Two generic detection paths:
      (a) bundled: a *.xexp already extracted into game_dir (some discs/packages
          ship the patch inside the game container).
      (b) sibling TU package: an STFS file (Content Type 0x000B0000, or a TU_*/tu*
          name) next to the source container whose title_id matches the base game
          -- its lone default.xexp is extracted into game_dir.
    Strictly additive: returns None (no-op) whenever no valid, version-matched
    patch is present, so base-only games are byte-identical to today."""
    base_ver = xex_base_version(base_xex) if base_xex else None

    def _accept(xexp):
        if not xexp or not is_xex_patch(xexp):
            return None
        desc = read_delta_descriptor(xexp)
        if not desc:
            return None
        sv, tv = desc["source_version"], desc["target_version"]
        if base_ver is not None and sv != base_ver:
            log("  title-update %s: source %s != base %s -- skipping (not this build's patch)"
                % (os.path.basename(xexp), xex_version_str(sv), xex_version_str(base_ver)))
            return None
        log("  title-update found: %s -> %s (%s)"
            % (xex_version_str(sv), xex_version_str(tv), os.path.basename(xexp)))
        return xexp

    # (a) a .xexp already sitting in the extracted game tree
    for root, _, files in os.walk(game_dir):
        for fn in files:
            if fn.lower().endswith(".xexp"):
                got = _accept(os.path.join(root, fn))
                if got:
                    return got

    # (b) a sibling TU STFS package next to the source container
    if container and os.path.isfile(container):
        base_title = (read_package_meta(container) or {}).get("title_id")
        folder = os.path.dirname(os.path.abspath(container))
        try:
            siblings = sorted(os.listdir(folder))
        except OSError:
            siblings = []
        for fn in siblings:
            p = os.path.join(folder, fn)
            if not os.path.isfile(p) or os.path.abspath(p) == os.path.abspath(container):
                continue
            ct = _stfs_content_type(p)
            if ct != TU_CONTENT_TYPE and not fn.lower().startswith("tu"):
                continue
            if base_title:
                m = read_package_meta(p)
                if m.get("title_id") and m["title_id"] != base_title:
                    continue
            got = _accept(_extract_xexp_from_stfs(p, game_dir, log))
            if got:
                return got
    return None


GDFX_MAGIC = b"MICROSOFT*XBOX*MEDIA"


def _walk_gdfx(read_sector, root_sector, root_size):
    """Walk the GDFX (Xbox disc) directory tree -> [(relpath, sector, size)]."""
    files = []

    def read_dir(sector, size, prefix):
        data = read_sector(sector, (size + 0x7FF) & ~0x7FF)
        seen, stack = set(), [0]
        while stack:
            pos = stack.pop()
            if pos in seen:
                continue
            seen.add(pos)
            o = pos * 4
            if o + 0x0E > len(data):
                continue
            left, right = struct.unpack_from("<HH", data, o)
            sec, sz = struct.unpack_from("<II", data, o + 4)
            attr, nlen = data[o + 0x0C], data[o + 0x0D]
            name = data[o + 0x0E:o + 0x0E + nlen].decode("latin-1", "ignore")
            if left not in (0, 0xFFFF):
                stack.append(left)
            if right not in (0, 0xFFFF):
                stack.append(right)
            if not name or name in (".", ".."):
                continue
            rel = (prefix + "/" + name) if prefix else name
            if attr & 0x10:
                if sz:
                    read_dir(sec, sz, rel)
            else:
                files.append((rel, sec, sz))
    read_dir(root_sector, root_size, "")
    return files


def _gdfx_extract(read_sector, out_dir, log, only=None):
    """Extract a GDFX filesystem (via read_sector(sector, nbytes)) to out_dir."""
    vd = read_sector(32, 0x800)            # volume descriptor @ sector 32 (0x10000)
    if vd[:20] != GDFX_MAGIC:
        raise SystemExit("no GDFX volume found (layout not handled) — try converting to ISO")
    root_sector, root_size = struct.unpack_from("<II", vd, 0x14)
    files = _walk_gdfx(read_sector, root_sector, root_size)
    log("GDFX volume: %d files" % len(files))
    xex = None
    written = 0
    for rel, sec, sz in files:
        leaf = rel.rsplit("/", 1)[-1]
        if only and leaf.lower() != only:
            continue
        dst = os.path.normpath(os.path.join(out_dir, rel.replace("/", os.sep)))
        if os.path.commonpath([os.path.abspath(out_dir), os.path.abspath(dst)]) != \
           os.path.abspath(out_dir):
            continue
        if not (os.path.exists(dst) and os.path.getsize(dst) == sz):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as o:
                remaining, s = sz, sec
                while remaining > 0:
                    n = min(1 << 20, remaining)
                    o.write(read_sector(s, (n + 0x7FF) & ~0x7FF)[:n])
                    remaining -= n
                    s += ((n + 0x7FF) & ~0x7FF) // 0x800
            written += sz
        if leaf.lower() == "default.xex":
            xex = dst
    log("extracted (%.0f MB) -> %s" % (written / 1e6, out_dir))
    return xex


def _iso_base(path):
    with open(path, "rb") as f:
        for base in (0, 0xFD90000, 0x2080000, 0x18300000, 0xB000):
            f.seek(base + 0x10000)
            if f.read(20) == GDFX_MAGIC:
                return base
    return None


def _svod_reader(src, hdr, log):
    """Build a GDFX read_sector over an SVOD (GoD) container, single-file layout.
    Implemented from rexglue's BlockToOffsetSVOD; validated by the GDFX magic
    check in _gdfx_extract, so a layout it gets wrong fails loudly, not silently."""
    vd = 0x379
    egdf = bool(hdr[vd + 0x18] & 0x40)
    start_data_block = u24le(hdr[vd + 0x1C:vd + 0x1F])
    data_file_count = struct.unpack_from(">I", hdr, 0x39D)[0]
    if data_file_count > 1:
        raise SystemExit("multi-part GoD (%d data files) is not handled — convert to ISO"
                         % data_file_count)
    f = open(src, "rb")
    svod_base_offset = 0
    for off, lay in ((0x2000, "egdf"), (0x12000, "xsf"), (0xD000, "single")):
        f.seek(off)
        if f.read(20) == GDFX_MAGIC:
            svod_base_offset = {"egdf": 0, "xsf": 0x10000, "single": 0xB000}[lay]
            log("SVOD layout: %s (magic @0x%X)" % (lay, off))
            break
    BPF, MAXF = 0x14388, 0xA290000

    def block_to_offset(block):
        tb = block - start_data_block * 2 + (2 if egdf else 0)
        fb, fi = tb % BPF, tb // BPF
        l0 = fb // 0x198 + 1
        offset = l0 * 0x1000 + (l0 // 0xA1C4 + 1) * 0x1000 + svod_base_offset
        addr = fb * 0x800 + offset
        if addr >= MAXF:
            fi += 1
            addr = addr % MAXF + 0x2000
        return addr

    def read_sector(sector, n):
        out = bytearray()
        for i in range((n + 0x7FF) // 0x800):
            f.seek(block_to_offset(sector + i))
            out += f.read(0x800)
        return bytes(out[:n])
    return read_sector


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
        head = f.read(0xC000)
    magic = head[:4]
    if magic in (b"CON ", b"LIVE", b"PIRS"):
        vol_type = struct.unpack_from(">I", head, 0x3A9)[0] if len(head) > 0x3AD else 0
        if vol_type == 1:                  # SVOD volume = GoD
            log("GoD / SVOD container")
            xex = _gdfx_extract(_svod_reader(src, head, log), out_dir, log)
            if not xex:
                raise SystemExit("no default.xex in the GoD image")
            return xex, out_dir
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

    base = _iso_base(src)
    if base is not None:
        log("Xbox 360 ISO (GDFX base 0x%X)" % base)
        f = open(src, "rb")

        def rd(sector, n):
            f.seek(base + sector * 0x800)
            return f.read(n)
        xex = _gdfx_extract(rd, out_dir, log)
        if not xex:
            raise SystemExit("no default.xex in the ISO")
        return xex, out_dir

    raise SystemExit(
        "unsupported container (magic=%r) — not STFS, GoD, ISO, a folder, or a raw XEX." % magic)


if __name__ == "__main__":
    print(extract_container(sys.argv[1], sys.argv[2]))
