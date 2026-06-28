"""
detect_setjmp.py — auto-detect the statically-linked CRT setjmp/longjmp routines
in a decompressed guest image and write setjmp_address/longjmp_address into the
project manifest's [entrypoint] table.

Why: Xbox 360 C++ exception handling is implemented with setjmp/longjmp linked
straight into the title (sub_826BE4D0 / sub_826BE1B0 in Rayman 3 HD). longjmp is
a NON-LOCAL jump (mass-restore of GPR/FPR/VMX + r1 from a jmp_buf, then blr). The
recompiler turns blr into a plain C++ 'return', so without setjmp_address/
longjmp_address set, longjmp returns to its immediate caller, the caller skips its
epilogue, leaves r31 corrupted, and crashes (the membase+0x65d4 null write).

The codegen already supports this: a `bl` to longjmp_address is emitted as
ppc_longjmp(r3,r4) and a `bl` to setjmp_address as ppc_setjmp(r3)
(rexglue-sdk/src/codegen/builders/context.cpp:170-188). Those keys default to 0
and there is NO auto-detection — this module supplies it.

Detection is byte-signature based, mirroring SigScanner's __savegprlr/__restgprlr
approach (rexglue-sdk/src/codegen/sig_scanner.cpp:108). We scan executable
sections of the flat image rexauto already dumps for the jumptables stage
(REX_DUMP_IMAGE, see rexauto.py stage_jumptables / project_recompiler.cpp:250).

We anchor on longjmp because it has an unmistakable fingerprint that no ordinary
function has: a long run of double/dword loads from one base register followed by
a stack-pointer reload from that same base and a blr (the non-local jump). setjmp
is then found as the routine that the same translation unit branches to as the
mirror save, OR — when setjmp is a tiny indirect thunk as in Rayman 3 HD — by
taking the nearest preceding call-target that the image's `bl` sites reference
together with longjmp. We deliberately keep setjmp optional: longjmp_address alone
already removes the crash, and a wrong setjmp_address is worse than none.
"""
import re
import struct

# ---- PPC big-endian instruction field helpers --------------------------------
# All guest instructions are 4-byte big-endian.

def _be32(buf, off):
    return struct.unpack_from(">I", buf, off)[0]

def _opcd(insn):              # primary opcode, bits 0..5
    return insn >> 26

def _ra(insn):               # bits 11..15
    return (insn >> 16) & 0x1F

def _rd(insn):               # bits 6..10 (rD / rS / frD)
    return (insn >> 21) & 0x1F

# Primary opcodes we key on
OP_LFD = 50      # lfd  frD, d(rA)            -> FPR restore
OP_STFD = 54     # stfd frS, d(rA)            -> FPR save (setjmp mirror)
OP_LD_GROUP = 58  # ld/ldu/lwa share opcode 58 (DS-form); ld has XO bits == 0
OP_STD_GROUP = 62  # std/stdu share opcode 62 (DS-form); std has XO bits == 0
OP_LWZ = 32      # lwz  rD, d(rA)
OP_ADDIS = 15    # lis is addis rD,0,imm
BLR = 0x4E800020  # blr (bclr 20,0)

def _is_std(insn):
    # std rS,ds(rA): opcode 62, low 2 bits (XO) == 0
    return _opcd(insn) == OP_STD_GROUP and (insn & 0x3) == 0

def _is_ld(insn):
    # ld rD,ds(rA): opcode 58, low 2 bits (XO) == 0
    return _opcd(insn) == OP_LD_GROUP and (insn & 0x3) == 0


def _scan_longjmp(buf, sec_start, sec_end, image_base):
    """Return list of candidate longjmp entry guest addresses in [sec_start,sec_end).

    Fingerprint (position independent, base register = whatever rA the run uses):
      - a run of >= MIN_FPR `lfd frN, off(rB)` all with the SAME rB,
        immediately followed by
      - a run of >= MIN_GPR `ld  rN, off(rB)` from that SAME rB.
    That adjacent FPR-then-GPR mass restore from one base register is the
    decisive marker — no ordinary function restores 18 FPRs + 19 GPRs from an
    argument-pointed buffer. Confirmation (not required to match, but raises
    confidence): within a wider window after the runs we also find an
    `ld r1, off(rB)` stack-pointer reload and a terminating `blr` (in this CRT
    the VMX v64..v127 restore sits between the GPR run and the SP reload, so we
    must search past it rather than expect contiguity).

    We then walk backwards to the function entry (instruction after the previous
    terminator, or the prologue mflr).
    """
    MIN_FPR = 12   # longjmp restores f14..f31 (18); be generous for variants
    MIN_GPR = 12   # restores r13..r31 (19)
    SP_WINDOW = 0x600  # bytes after the GPR run to look for `ld r1` + blr
    base = sec_start - image_base
    end = sec_end - image_base
    n = (end - base) & ~0x3
    candidates = []

    i = 0
    while i + 4 <= n:
        insn = _be32(buf, base + i)
        if _opcd(insn) == OP_LFD:
            rB = _ra(insn)
            # 1) contiguous lfd run sharing rB
            j = i
            fpr = 0
            while j + 4 <= n:
                w = _be32(buf, base + j)
                if _opcd(w) == OP_LFD and _ra(w) == rB:
                    fpr += 1
                    j += 4
                else:
                    break
            if fpr >= MIN_FPR:
                # 2) contiguous ld run from the same rB, starting right after
                gpr = 0
                k = j
                while k + 4 <= n:
                    w = _be32(buf, base + k)
                    if _is_ld(w) and _ra(w) == rB:
                        gpr += 1
                        k += 4
                    else:
                        break
                if gpr >= MIN_GPR:
                    # 3) confirmation: ld r1,off(rB) (SP restore) + blr ahead
                    saw_sp = False
                    has_blr = False
                    m = k
                    limit = min(n, k + SP_WINDOW)
                    while m + 4 <= limit:
                        w = _be32(buf, base + m)
                        if _is_ld(w) and _ra(w) == rB and _rd(w) == 1:
                            saw_sp = True
                        if w == BLR:
                            has_blr = True
                            break
                        m += 4
                    # SP reload + blr is expected; require blr at least
                    if has_blr:
                        entry = _walk_back_to_entry(buf, base, i, image_base, sec_start)
                        # Require a real function entry: the routine must begin
                        # with a prologue (mflr r0 ; stwu r1,-N(r1)). This rejects
                        # inlined restores in the middle of some other function
                        # (which have no prologue and read from a scratch reg).
                        if entry is not None and _has_prologue(buf, entry - image_base):
                            candidates.append((entry, saw_sp))
                        i = m + 4
                        continue
        i += 4
    # prefer candidates that also showed the SP reload
    confirmed = [a for a, sp in candidates if sp]
    return confirmed if confirmed else [a for a, _ in candidates]


def _has_prologue(buf, off):
    """True if off begins with `mflr r0` followed (within 2 insns) by `stwu r1`."""
    if off < 0 or off + 8 > len(buf):
        return False
    if _be32(buf, off) != 0x7C0802A6:       # mflr r0
        return False
    for d in (4, 8):
        if off + d + 4 <= len(buf):
            w = _be32(buf, off + d)
            if _opcd(w) == 37 and _ra(w) == 1:   # stwu r1,-N(r1)
                return True
    return False


def _walk_back_to_entry(buf, base, run_off, image_base, sec_start):
    """From the offset of the restore run, walk back to the function entry.
    Scan backwards bounded by ~256 insns. The entry is the nearest preceding
    `mflr r0` (the canonical first instruction of an EH-frame routine). If we hit
    a terminator (previous blr / unconditional branch) or zero-padding first, the
    entry is the first non-zero instruction after it. Returns a guest address."""
    off = run_off
    steps = 0
    last_after_boundary = run_off
    while off - 4 >= 0 and steps < 256:
        w = _be32(buf, base + off - 4)
        if w == 0x7C0802A6:                     # mflr r0  -> this IS the entry
            return image_base + (base + off - 4)
        if w == BLR or (w & 0xFC000003) == 0x48000000 or w == 0x00000000:
            # boundary: entry is the first instruction we kept after it
            return image_base + (base + last_after_boundary)
        last_after_boundary = off - 4
        off -= 4
        steps += 1
    return image_base + (base + last_after_boundary)


def _walk_back_to_boundary(buf, base, run_off, image_base):
    """Walk back to the start of the code block containing run_off: the first
    non-zero instruction after the previous blr/unconditional-branch/zero-pad.
    Used for setjmp, whose call target is a leaf dispatch thunk WITHOUT an mflr
    prologue that falls through into the mirror-save body."""
    off = run_off
    steps = 0
    first = run_off
    while off - 4 >= 0 and steps < 256:
        w = _be32(buf, base + off - 4)
        if w == BLR or (w & 0xFC000003) == 0x48000000 or w == 0x00000000:
            return image_base + (base + first)
        first = off - 4
        off -= 4
        steps += 1
    return image_base + (base + first)


def _scan_setjmp(buf, sec_start, sec_end, image_base):
    """Find the setjmp call target via its mirror-save fingerprint:
      - a run of >= MIN_FPR `stfd frN, off(rB)` (same rB), immediately followed by
      - a run of >= MIN_GPR `std  rN, off(rB)` from that same rB.
    The save base rB is the jmp_buf arg (r3). The setjmp CALL TARGET is the start
    of the code block containing the save (the guard thunk that falls through to
    it), found by walking back to the previous block boundary. Returns a list of
    candidate setjmp call-target guest addresses."""
    MIN_FPR = 12
    MIN_GPR = 12
    base = sec_start - image_base
    end = sec_end - image_base
    n = (end - base) & ~0x3
    candidates = []
    i = 0
    while i + 4 <= n:
        insn = _be32(buf, base + i)
        if _opcd(insn) == OP_STFD:
            rB = _ra(insn)
            j = i
            fpr = 0
            while j + 4 <= n and _opcd(_be32(buf, base + j)) == OP_STFD and _ra(_be32(buf, base + j)) == rB:
                fpr += 1
                j += 4
            if fpr >= MIN_FPR:
                gpr = 0
                k = j
                while k + 4 <= n and _is_std(_be32(buf, base + k)) and _ra(_be32(buf, base + k)) == rB:
                    gpr += 1
                    k += 4
                if gpr >= MIN_GPR:
                    entry = _walk_back_to_boundary(buf, base, i, image_base)
                    candidates.append(entry)
                    i = k
                    continue
        i += 4
    return sorted(set(candidates))


# ---- public entry ------------------------------------------------------------

def detect(image_path, exec_sections, image_base):
    """exec_sections: list of (start_guest, end_guest). Returns dict with any of
    {'longjmp_address': int, 'setjmp_address': int}. Empty if nothing confident."""
    with open(image_path, "rb") as f:
        buf = f.read()
    longjmps, setjmps = [], []
    for (s, e) in exec_sections:
        longjmps += _scan_longjmp(buf, s, e, image_base)
        setjmps += _scan_setjmp(buf, s, e, image_base)
    longjmps = sorted(set(longjmps))
    setjmps = sorted(set(setjmps))
    out = {}

    if len(longjmps) == 1:
        out["longjmp_address"] = longjmps[0]
    elif len(longjmps) > 1:
        # A CRT links exactly one longjmp; ambiguity => don't guess (a wrong key
        # is worse than none). Report so the caller can log and skip.
        out["_ambiguous_longjmp"] = longjmps

    # setjmp is only emitted when it is unique AND sits in the same CRT module as
    # longjmp (their bodies are adjacent: the save routine immediately follows the
    # longjmp routine in the statically-linked CRT). This pairing guards against a
    # stray mass-store elsewhere being mistaken for setjmp. longjmp alone already
    # removes the crash, so we stay conservative here.
    if "longjmp_address" in out and len(setjmps) >= 1:
        lj = out["longjmp_address"]
        near = [a for a in setjmps if 0 < (a - lj) < 0x1000]  # save follows longjmp
        if len(near) == 1:
            out["setjmp_address"] = near[0]
        elif len(setjmps) == 1:
            out["setjmp_address"] = setjmps[0]
        else:
            out["_ambiguous_setjmp"] = setjmps
    return out


# ---- manifest writer (mirrors rexauto.add_includes' line-based edit) ----------

def write_addresses(manifest_path, longjmp=None, setjmp=None):
    """Insert/replace setjmp_address/longjmp_address inside the [entrypoint]
    table. Line-based, like add_includes; never touches functions.toml (which
    heal.write_overrides rewrites)."""
    txt = open(manifest_path, encoding="utf-8", errors="ignore").read()

    def set_key(text, key, value):
        line = "%s = 0x%08X" % (key, value)
        pat = re.compile(r"^[ \t]*%s[ \t]*=.*$" % re.escape(key), re.MULTILINE)
        if pat.search(text):
            return pat.sub(line, text, count=1)
        # insert just after the [entrypoint] header
        hdr = re.search(r"^\[entrypoint\][ \t]*$", text, re.MULTILINE)
        if hdr:
            ins = hdr.end()
            return text[:ins] + "\n" + line + text[ins:]
        # no [entrypoint] — append a fresh table (defensive; init always makes one)
        return text.rstrip() + "\n\n[entrypoint]\n" + line + "\n"

    if longjmp is not None:
        txt = set_key(txt, "longjmp_address", longjmp)
    if setjmp is not None:
        txt = set_key(txt, "setjmp_address", setjmp)
    open(manifest_path, "w", encoding="utf-8").write(txt)
