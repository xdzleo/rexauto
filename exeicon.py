"""Embed the game's cover tile as the built exe's Windows icon.

Xbox 360 discs don't ship a Windows-style icon, but rexauto already fetches the
marketplace tile PNG by title_id (extract.fetch_title_icon, cached in covers/).
This module converts that PNG into a proper multi-size icon resource and
injects it into the linked exe with the Win32 resource-update API — no .rc
file, no CMake change, works on every relink.
"""
import ctypes
import ctypes.wintypes as wt
import io
import struct

RT_ICON = 3
RT_GROUP_ICON = 14
LANG_NEUTRAL = 0


def _dib_entries(png_bytes, sizes=(16, 24, 32, 48, 64)):
    """PNG -> list of (w, h, dib_bytes) classic 32bpp icon DIBs (Pillow).
    Falls back to a single raw-PNG entry (Vista+ accepts PNG icon payloads)."""
    try:
        from PIL import Image
        src = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        out = []
        for s in sizes:
            if s > max(src.size):  # never upscale the tile
                continue
            im = src.resize((s, s), Image.LANCZOS)
            w, h = im.size
            # BITMAPINFOHEADER with doubled height (XOR + AND masks), BGRA bottom-up
            hdr = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
            px = im.tobytes("raw", "BGRA")
            rows = [px[y * w * 4:(y + 1) * w * 4] for y in range(h)]
            xor = b"".join(reversed(rows))
            and_mask = b"\x00" * (((w + 31) // 32) * 4 * h)  # alpha carries transparency
            out.append((w, h, hdr + xor + and_mask))
        if out:
            return out
    except Exception:
        pass
    # fallback: raw PNG entry
    w = h = 0
    if png_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png_bytes[16:24])
    return [(min(w, 255), min(h, 255), png_bytes)]


def _mir(i):
    """MAKEINTRESOURCE: small ints pass as LPCWSTR pointers."""
    return ctypes.cast(ctypes.c_void_p(i), wt.LPCWSTR)


def set_exe_icon(exe_path, png_bytes):
    """Inject png_bytes as the exe's main icon group. Returns True on success."""
    entries = _dib_entries(png_bytes)
    k32 = ctypes.windll.kernel32
    # explicit 64-bit-clean signatures (handles silently truncate without these)
    k32.BeginUpdateResourceW.argtypes = [wt.LPCWSTR, wt.BOOL]
    k32.BeginUpdateResourceW.restype = wt.HANDLE
    k32.UpdateResourceW.argtypes = [wt.HANDLE, wt.LPCWSTR, wt.LPCWSTR, wt.WORD, wt.LPVOID, wt.DWORD]
    k32.UpdateResourceW.restype = wt.BOOL
    k32.EndUpdateResourceW.argtypes = [wt.HANDLE, wt.BOOL]
    k32.EndUpdateResourceW.restype = wt.BOOL
    h = k32.BeginUpdateResourceW(str(exe_path), False)
    if not h:
        return False
    ok = True
    grp = struct.pack("<HHH", 0, 1, len(entries))
    for i, (w, hh, data) in enumerate(entries, start=1):
        buf = ctypes.create_string_buffer(data, len(data))
        if not k32.UpdateResourceW(h, _mir(RT_ICON), _mir(i), LANG_NEUTRAL,
                                   ctypes.cast(buf, wt.LPVOID), len(data)):
            ok = False
        grp += struct.pack("<BBBBHHIH", w % 256, hh % 256, 0, 0, 1, 32, len(data), i)
    gbuf = ctypes.create_string_buffer(grp, len(grp))
    if not k32.UpdateResourceW(h, _mir(RT_GROUP_ICON), _mir(1), LANG_NEUTRAL,
                               ctypes.cast(gbuf, wt.LPVOID), len(grp)):
        ok = False
    if not k32.EndUpdateResourceW(h, not ok):  # discard on failure
        return False
    return ok
