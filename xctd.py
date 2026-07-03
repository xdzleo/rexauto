"""XCTD (XCompress LZXTDECODE, magic 0F F5 12 ED) offline pre-decompression.

Some Xbox 360 titles ship their assets transparently compressed on disc
(XFileEnableTransparentDecompression): every asset file starts with the magic
0x0FF512ED and the KERNEL decompresses reads on real hardware. Our runtime
stubs NtQueryInformationFile(XFileXctdCompressionInformation) with
INVALID_PARAMETER, so the game takes its "not compressed" path and expects
plaintext -- which is exactly what this stage provides: it decodes every
XCTD file in the extracted game dir IN PLACE (originals kept in a backup dir).
Known titles: Captain America Super Soldier (proved to gameplay), Alien:
Isolation, Monkey Island 2 SE, XCOM Enemy Unknown.

Format (cracked empirically + confirmed by QuickBMS unxmemlzx / UniPyX):
16-byte BE header [magic][ver 0x0100][rsvd][crc][flags]; flags decode to
window (1<<((fl&0xF)+15)), pad boundary zbs (0x8000<<((fl>>4)&3)), segment
count ((fl>>6)&0xFFFF, 0 = raw payload), and table width (bit22: 20-bit packed
or BE32). Then per-segment uncompressed sizes, then [BE16 size][bytes] chunks
(1 chunk = 1 LZX frame of 32KB; a BE16 of 0 = zero padding of arbitrary BYTE
length up to the next zbs boundary -- may be ODD, never read it pairwise).
Each segment is an independent LZX stream; plaintext = segments concatenated.
The decode itself lives in tools/xctd_rip.cpp (links the vendored libmspack
lzxd) and is compiled on demand with the same clang the pipeline already uses.
"""
import os, struct, subprocess, shutil, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
MAGIC = b"\x0f\xf5\x12\xed"

TOOL_SRC = [os.path.join(HERE, "tools", "xctd_rip.cpp"),
            os.path.join(HERE, "thirdparty", "libmspack", "lzxd.c"),
            os.path.join(HERE, "thirdparty", "libmspack", "system.c")]
TOOL_EXE = os.path.join(HERE, "tools", "xctd_rip.exe")


def ensure_tool(env, log=print):
    """Build tools/xctd_rip.exe once (cached by mtime vs sources)."""
    if os.path.exists(TOOL_EXE):
        exe_m = os.path.getmtime(TOOL_EXE)
        if all(os.path.getmtime(s) <= exe_m for s in TOOL_SRC):
            return TOOL_EXE
    clangxx = env.get("clangxx") or env.get("clang")
    if not clangxx:
        raise SystemExit("xctd: clang++ not found (needed to build the XCTD decoder)")
    inc = os.path.join(HERE, "thirdparty", "libmspack")
    cpp, c1, c2 = TOOL_SRC
    o1 = os.path.join(HERE, "thirdparty", "libmspack", "lzxd.o")
    o2 = os.path.join(HERE, "thirdparty", "libmspack", "system.o")
    clang = clangxx.replace("clang++", "clang")
    log("xctd: building decoder (one-time)")
    for src, obj in ((c1, o1), (c2, o2)):
        r = subprocess.run([clang, "-O2", "-I", inc, "-c", src, "-o", obj],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("xctd: clang failed on %s:\n%s" % (src, r.stderr[-800:]))
    r = subprocess.run([clangxx, "-O2", "-D_CRT_SECURE_NO_WARNINGS", "-I", inc,
                        cpp, o1, o2, "-o", TOOL_EXE], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("xctd: clang++ failed:\n%s" % r.stderr[-800:])
    return TOOL_EXE


def find_xctd(root):
    out = []
    for dp, _dn, fn in os.walk(root):
        for name in fn:
            p = os.path.join(dp, name)
            try:
                if os.path.getsize(p) >= 16:
                    with open(p, "rb") as f:
                        if f.read(4) == MAGIC:
                            out.append(p)
            except OSError:
                pass
    return out


def rip_inplace(game_dir, backup_dir, env, log=print, jobs=8):
    """Decode every XCTD file under game_dir in place. Two-phase: everything is
    decoded to <path>.xctdtmp first; only if ALL succeed are files swapped
    (original -> backup_dir preserving relative paths). Returns #files ripped
    (0 = no XCTD in this title, stage is a no-op)."""
    files = find_xctd(game_dir)
    if not files:
        return 0
    tool = ensure_tool(env, log)
    total_in = sum(os.path.getsize(p) for p in files)
    log("xctd: %d transparently-compressed file(s), %.1f MB -- pre-decompressing"
        % (len(files), total_in / 1e6))

    def rip_one(path):
        tmp = path + ".xctdtmp"
        r = subprocess.run([tool, path, tmp], capture_output=True, text=True)
        if r.returncode != 0:
            try:
                os.remove(tmp)
            except OSError:
                pass
            tail = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "rc=%d" % r.returncode
            return path, False, tail
        return path, True, ""

    fails = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for fut in as_completed({ex.submit(rip_one, p) for p in files}):
            path, ok, msg = fut.result()
            if not ok:
                fails.append((os.path.relpath(path, game_dir), msg))
    if fails:
        for rel, msg in fails[:10]:
            log("xctd: FAIL %s: %s" % (rel, msg))
        raise SystemExit("xctd: %d/%d file(s) failed to decode -- game dir left "
                         "untouched (originals intact)" % (len(fails), len(files)))
    total_out = sum(os.path.getsize(p + ".xctdtmp") for p in files)
    for p in files:
        rel = os.path.relpath(p, game_dir)
        bak = os.path.join(backup_dir, rel)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.move(p, bak)
        os.rename(p + ".xctdtmp", p)
    log("xctd: %.1f MB -> %.1f MB plaintext; originals in %s"
        % (total_in / 1e6, total_out / 1e6, backup_dir))
    return len(files)
