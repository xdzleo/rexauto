"""
Xenon jump-table recovery — IDA Pro analysis pass.

Recovers computed-jump (switch) tables from a raw Xbox 360 / Xenon PowerPC
(big-endian) code image and writes them as JSON. Designed to feed the
``switch_tables`` config consumed by XenonRecomp / ReXGlue, but the JSON is
generic and easy to convert (see ``gen_toml.py``).

Why this exists: the Xbox 360 compiler emits jump-table idioms that the stock
switch recognizers in IDA and Ghidra do not match — bound checks via conditional
*return* (``cmpwi``/``bgelr``) instead of branch-to-default, the index scaled
*in place*, two-level "relative" byte tables, and tables embedded inline in
.text. This pass walks IDA's disassembly with idiom-aware dataflow to resolve
them. See docs/idioms.md.

Run headless:
    idat -A -Sida_jumptables.py <config.json> -c image.elf
(or pass the .i64; ``image.elf`` is a raw->ELF wrapper produced by make_elf.py)

The config path is read from IDA's script argv. See examples/config.example.json.
"""
import json
import bisect
import time as _time
import idaapi, idautils, idc
import ida_auto, ida_bytes, ida_funcs, ida_ua, ida_xref, ida_pro

_T0 = _time.time()
def _lap(tag):
    print("[xjt] timing %-18s %6.1fs" % (tag, _time.time() - _T0))


# --- config -----------------------------------------------------------------
def _int(v):
    return int(v, 0) if isinstance(v, str) else int(v)

_cfg_path = idc.ARGV[1] if len(idc.ARGV) > 1 else None
if not _cfg_path:
    print("[xjt] ERROR: no config path passed (idat -S\"ida_jumptables.py <config.json>\")")
    ida_pro.qexit(2)
with open(_cfg_path) as _f:
    CFG = json.load(_f)

IMG_LO   = _int(CFG["image_base"])
IMG_HI   = _int(CFG["image_end"])
TEXT_LO  = _int(CFG["text_start"])
TEXT_HI  = _int(CFG["text_end"])
OUT_PATH = CFG.get("output", "jumptables.json")
FUNC_PATH = CFG.get("functions")          # optional: one hex address per line
MAX_TABLE = int(CFG.get("max_table_entries", 4096))

# Speed: the jump-table walk needs correct disassembly and data refs, nothing
# else — not FLIRT signatures, stack/argument propagation, no-return tracing, or
# the final cleanup pass. Dropping those analysis flags cuts auto-analysis time
# substantially on a large image with no effect on what the recogniser reads.
try:
    import ida_ida
    _af = ida_ida.inf_get_af()
    for _n in ("AF_FLIRT", "AF_SIGMLT", "AF_SIGCMT", "AF_HFLIRT", "AF_LVAR",
               "AF_STKARG", "AF_REGARG", "AF_TRACE", "AF_VERSP", "AF_ANORET",
               "AF_NULLSUB", "AF_FINAL"):
        _af &= ~getattr(ida_ida, _n, 0)
    ida_ida.inf_set_af(_af)
    print("[xjt] reduced analysis flags for speed")
except Exception as _e:
    print("[xjt] note: could not reduce analysis flags (%s)" % _e)

print("[xjt] analyzing image...")
ida_auto.auto_wait()
_lap("initial-analysis")

# --- function coverage ------------------------------------------------------
# Static recompilers know every function boundary (from the XEX .pdata). Feeding
# that list makes IDA define ALL functions, so every bctr is analysed — not just
# the ones reachable from the entry point. Without it we fall back to whatever
# IDA auto-discovered (lower coverage).
FUNCS = []
if FUNC_PATH:
    with open(FUNC_PATH) as f:
        FUNCS = sorted({int(l.strip(), 16) for l in f if l.strip()})
    added = 0
    for i, ea in enumerate(FUNCS):
        fn = ida_funcs.get_func(ea)
        if fn is None or fn.start_ea != ea:
            if ida_funcs.add_func(ea):
                added += 1
        if i % 4000 == 0 and i:
            print("[xjt] progress defining %d/%d" % (i, len(FUNCS)))
    print("[xjt] analyzing functions...")
    ida_auto.auto_wait()
    print("[xjt] functions=%d (add_func=%d)" % (len(FUNCS), added))
    _lap("func-analysis")
else:
    print("[xjt] no function list given; relying on IDA auto-analysis")

def func_start_of(ea):
    if FUNCS:
        i = bisect.bisect_right(FUNCS, ea) - 1
        if i >= 0:
            return FUNCS[i]
    fn = ida_funcs.get_func(ea)
    return fn.start_ea if fn else max(TEXT_LO, ea - 4 * 64)

def func_end_after(ea):
    if FUNCS:
        j = bisect.bisect_right(FUNCS, ea)
        if j < len(FUNCS):
            return FUNCS[j]
    fn = ida_funcs.get_func(ea)
    return fn.end_ea if fn else TEXT_HI


# --- low-level disasm helpers ----------------------------------------------
def decode(ea):
    insn = ida_ua.insn_t()
    return insn if ida_ua.decode_insn(insn, ea) > 0 else None
def mnem(ea):
    return idc.print_insn_mnem(ea)
def oreg(insn, n):
    op = insn.ops[n]
    return op.reg if op.type == ida_ua.o_reg else None
def oimm(insn, n):
    op = insn.ops[n]
    return op.value if op.type == ida_ua.o_imm else None

NOP = 0x60000000
BCTR = 0x4E800420

COPY_MNEMS = ("mr", "mr.", "extsh", "extsh.", "extsw", "extsb", "extsb.")
UNCOND_CF  = ("b", "ba", "blr", "bctr")   # unconditional control flow = block edge
LOADS      = ("lwz", "lha", "lhz", "lbz", "lwzu", "lhau", "lhzu", "lbzu")
STORES     = ("stw", "sth", "stb", "stwx", "sthx", "stbx", "std", "stdx",
              "stwu", "stdu", "stfs", "stfd", "dcbz", "dcbt")
COND_BR    = ("bgt", "bgtlr", "bge", "bgelr", "ble", "blt", "bgtl", "bgel",
              "blelr", "bltlr", "bnl", "bnllr")

def mem_src(insn):
    # (displacement, base_reg) for a D(B) memory operand, else None
    for k in (1, 2, 3):
        op = insn.ops[k]
        if op.type == ida_ua.o_displ:
            return (op.addr & 0xFFFFFFFF, op.reg)
    return None

def force_window(bctr):
    # PowerPC is fixed 4-byte instructions, so we can safely define the idiom
    # window even where IDA left bytes undefined. Clamp to the function start so
    # we never disassemble into a previous function's data.
    a = max(func_start_of(bctr), bctr - 4 * 28)
    while a <= bctr:
        if not ida_bytes.is_code(ida_bytes.get_flags(a)):
            # create_insn fails silently if these bytes are already defined as
            # DATA (an array / .long IDA left inside .text) — which leaves the
            # whole idiom unreadable and the table unrecovered (e.g. budokai3
            # 0x8221a930, a clean 512-case rel table the recogniser handles but
            # never saw as code). Undefine the stale data item first, then convert.
            if not ida_ua.create_insn(a):
                ida_bytes.del_items(a, 0, 4)
                ida_ua.create_insn(a)
        a += 4


# --- dataflow ----------------------------------------------------------------
def reg_const(reg, ea, depth=0):
    """Resolve a register to a constant address via backward dataflow over the
    standard materialisation idioms (lis/addis/addi/ori/li/mr). Stops at block
    boundaries so an argument register isn't mistaken for a constant 'li' that
    belongs to the previous function."""
    if depth > 8:
        return None
    a = ea
    for _ in range(16):
        a = idc.prev_head(a)
        if a == idaapi.BADADDR or a < IMG_LO:
            break
        insn = decode(a)
        if insn is None:
            continue
        if mnem(a) in UNCOND_CF:
            break
        if oreg(insn, 0) != reg:
            continue
        m = mnem(a)
        if m == "lis":
            return ((oimm(insn, 1) or 0) & 0xFFFF) << 16
        if m == "li":
            return (oimm(insn, 1) or 0) & 0xFFFFFFFF
        if m == "addis":
            s = reg_const(oreg(insn, 1), a, depth + 1)
            return None if s is None else (s + (((oimm(insn, 2) or 0) & 0xFFFF) << 16)) & 0xFFFFFFFF
        if m == "addi":
            # IDA already sign-extends the immediate to 32 bits (e.g. #0xFFFFE350)
            s = reg_const(oreg(insn, 1), a, depth + 1)
            return None if s is None else (s + (oimm(insn, 2) or 0)) & 0xFFFFFFFF
        if m == "ori":
            s = reg_const(oreg(insn, 1), a, depth + 1)
            return None if s is None else (s | ((oimm(insn, 2) or 0) & 0xFFFF)) & 0xFFFFFFFF
        if m in ("mr", "mr."):
            return reg_const(oreg(insn, 1), a, depth + 1)
        return None
    return None

def find_writer(reg, ea, maxback=26):
    """First instruction before `ea` that writes `reg` (op0). Stores are skipped
    (their op0 is a source)."""
    a = ea
    for _ in range(maxback):
        a = idc.prev_head(a)
        if a == idaapi.BADADDR or a < IMG_LO:
            break
        insn = decode(a)
        if insn is None:
            continue
        if mnem(a) in STORES:
            continue
        if oreg(insn, 0) == reg:
            return a, insn, mnem(a)
    return None, None, None

def dref_at(ea):
    # data xref attached to a single instruction, landing outside .text
    for dref in idautils.DataRefsFrom(ea):
        if IMG_LO <= dref < IMG_HI and not (TEXT_LO <= dref < TEXT_HI):
            return dref
    return None

def branch_after(cmp_ea):
    # the conditional branch that consumes a compare may be several instructions
    # later (not adjacent). Stop at the block terminator.
    a = cmp_ea
    for _ in range(24):
        a = idc.next_head(a, cmp_ea + 0x100)
        if a == idaapi.BADADDR:
            break
        m = mnem(a)
        if m in COND_BR:
            return m
        if m in UNCOND_CF:
            break
    return ""

def find_bound(idx_reg, ea, maxback=32):
    """Find the range check `cmplwi/cmpwi idx, N` that guards the switch, plus the
    branch that consumes it. Handles three twists:
      * the index is copied (mr) / sign-extended before the load,
      * the index is scaled in place (slwi) — scaling preserves identity,
      * the index is re-loaded: the bound checks one register loaded from M and
        the table uses another register loaded from the *same* M.
    Never crosses an unconditional branch (a compare on the other side of one
    belongs to a different path)."""
    window = []
    a = ea
    for _ in range(maxback):
        a = idc.prev_head(a)
        if a == idaapi.BADADDR or a < IMG_LO:
            break
        if mnem(a) in UNCOND_CF:
            break
        window.append(a)

    equiv = {idx_reg}
    head = idx_reg
    load_mem = None
    for a in window:
        insn = decode(a)
        if insn is None:
            continue
        if oreg(insn, 0) == head:
            m = mnem(a)
            if m in COPY_MNEMS or m in ("slwi", "rlwinm", "sldi"):
                head = oreg(insn, 1)
                if head is None:
                    break
                equiv.add(head)
            elif m in LOADS:
                load_mem = mem_src(insn)
                break
            else:
                break
    if load_mem is not None:
        for a in window:
            insn = decode(a)
            if insn is None:
                continue
            if mnem(a) in LOADS and oreg(insn, 0) is not None and mem_src(insn) == load_mem:
                equiv.add(oreg(insn, 0))

    for a in window:   # backward order: nearest compare to the load wins
        insn = decode(a)
        if insn is None:
            continue
        if mnem(a) in ("cmpwi", "cmplwi"):
            r = oreg(insn, 1)
            im = oimm(insn, 2)
            if r is None:
                r = oreg(insn, 0)
                im = oimm(insn, 1)
            if r in equiv and im is not None:
                return (im & 0xFFFFFFFF), branch_after(a)
    return None, None

def count_from_bound(n, bm):
    if n is None:
        return None
    if bm in ("bgt", "bgtlr", "bgtl"):
        return n + 1   # idx > N -> default  => indices 0..N
    return n           # bge/bgelr/bnl       => indices 0..N-1

def shift_amount(slwi_ea):
    # SH field (bits 11-15) of the raw rlwinm/slwi word
    return (ida_bytes.get_dword(slwi_ea) >> 11) & 0x1F


# --- table readers ----------------------------------------------------------
def read_bytes_tbl(tbl, esz, count):
    out = []
    for i in range(count):
        a = tbl + i * esz
        if esz == 1:
            out.append(ida_bytes.get_byte(a))
        elif esz == 2:
            out.append(ida_bytes.get_word(a))
        else:
            out.append(ida_bytes.get_dword(a))
    return out

def read_table_abs(tbl, cap):
    # read until a non-pointer (out of .text or misaligned) terminates the table
    out = []
    a = tbl
    for _ in range(cap):
        v = ida_bytes.get_dword(a)
        if v < TEXT_LO or v >= TEXT_HI or (v & 3):
            break
        out.append(v & 0xFFFFFFFF)
        a += 4
    return out

def read_table_abs_strict(tbl, n):
    # read EXACTLY n entries; None if any is not a valid code pointer
    out = []
    a = tbl
    for _ in range(n):
        v = ida_bytes.get_dword(a)
        if v < TEXT_LO or v >= TEXT_HI or (v & 3):
            return None
        out.append(v & 0xFFFFFFFF)
        a += 4
    return out


# --- recognisers ------------------------------------------------------------
def recognize(bctr):
    # mtctr must immediately precede the bctr (skipping NOPs); its GPR is op1
    a = bctr
    ctr_reg = None
    mtctr_ea = None
    for _ in range(4):
        a = idc.prev_head(a)
        if a == idaapi.BADADDR:
            break
        if ida_bytes.get_dword(a) == NOP:
            continue
        if mnem(a) == "mtctr":
            insn = decode(a)
            ctr_reg = oreg(insn, 1)
            mtctr_ea = a
        break
    if ctr_reg is None:
        return None
    wa, wi, wm = find_writer(ctr_reg, mtctr_ea)
    if wi is None:
        return None
    if wm == "lwzx":
        return recog_abs(bctr, wa, wi)
    if wm == "add":
        return recog_rel(bctr, wa, wi)
    return None

def recog_abs(bctr, wa, wi):
    # ctr = table[idx]   — each table entry IS the absolute target address
    ra, rb = oreg(wi, 1), oreg(wi, 2)
    scaled = idx_reg = base_reg = None
    for cand in (ra, rb):
        sa, si, sm = find_writer(cand, wa)
        if sm in ("slwi", "rlwinm", "sldi", "rldicr"):
            scaled = cand
            idx_reg = oreg(si, 1)
            base_reg = rb if cand == ra else ra
            break
    if scaled is None:
        return None
    table = reg_const(base_reg, wa)
    if table is None:
        table = dref_at(wa)
        if table is None:
            ba, bi, bm = find_writer(base_reg, wa)
            if ba is not None:
                table = dref_at(ba)
    if table is None:
        return None
    n, branchm = find_bound(idx_reg, wa)
    cnt = count_from_bound(n, branchm)
    in_text = (TEXT_LO <= table < TEXT_HI)
    if in_text:
        # inline table in .text: read-until-invalid would read code as pointers,
        # so accept only with a reliable in-block bound and all entries valid.
        if not cnt or cnt > MAX_TABLE:
            return None
        targets = read_table_abs_strict(table, cnt)
        if targets is None:
            return None
    else:
        # table in .rodata: terminates at a non-pointer; the bound (when found)
        # trims over-read into a contiguous neighbouring table.
        targets = read_table_abs(table, cnt if (cnt and 0 < cnt <= MAX_TABLE) else MAX_TABLE)
        if cnt and 0 < cnt <= MAX_TABLE:
            targets = targets[:cnt]
    if not targets:
        return None
    return {"bctr": bctr, "table": table & 0xFFFFFFFF, "idx_reg": idx_reg,
            "ncases": len(targets), "targets": targets, "kind": "abs",
            "bound": n, "in_text": in_text, "scaled_inplace": bool(idx_reg == scaled)}

def recog_rel(bctr, wa, wi):
    # ctr = anchor + byteTable[idx]*scale  — two-level "compressed" table
    rx, ry = oreg(wi, 1), oreg(wi, 2)
    cx, cy = reg_const(rx, wa), reg_const(ry, wa)
    if cx is not None and cy is None:
        anchor, offreg = cx, ry
    elif cy is not None and cx is None:
        anchor, offreg = cy, rx
    else:
        return None
    # offreg = slwi(loadedByte)  OR  directly the load (scale = 1)
    sa, si, sm = find_writer(offreg, wa)
    if sm in ("slwi", "rlwinm", "sldi"):
        scale = 1 << shift_amount(sa)
        byte_reg = oreg(si, 1)
        la, li_, lm = find_writer(byte_reg, sa)
    elif sm in ("lbzx", "lhzx", "lwzx"):
        scale = 1
        la, li_, lm = sa, si, sm
    else:
        return None
    if lm not in ("lbzx", "lhzx", "lwzx"):
        return None
    esz = {"lbzx": 1, "lhzx": 2, "lwzx": 4}[lm]
    ba, bb = oreg(li_, 1), oreg(li_, 2)
    cba, cbb = reg_const(ba, la), reg_const(bb, la)
    if cba is not None and cbb is None:
        bytetab, offop = cba, bb
    elif cbb is not None and cba is None:
        bytetab, offop = cbb, ba
    else:
        return None
    # offop = idx*esz for half/word element tables (scaled via slwi); the real
    # index (the bound target) is that slwi's source, else the byte operand.
    idx_reg = offop
    ia, ii, im = find_writer(offop, la)
    if im in ("slwi", "rlwinm", "sldi"):
        idx_reg = oreg(ii, 1)
    n, branchm = find_bound(idx_reg, la)
    cnt = count_from_bound(n, branchm)
    fs = func_start_of(bctr)
    fe = func_end_after(bctr)
    if not cnt or cnt > MAX_TABLE:
        # fallback: read the byte table until a target leaves the function range
        out = []
        for i in range(512):
            v = read_bytes_tbl(bytetab + i * esz, esz, 1)[0]
            t = (anchor + v * scale) & 0xFFFFFFFF
            if not (fs <= t < fe) or (t & 3):
                break
            out.append(t)
        if not out:
            return None
        return {"bctr": bctr, "table": bytetab & 0xFFFFFFFF, "idx_reg": idx_reg,
                "ncases": len(out), "targets": out, "kind": "rel",
                "anchor": anchor & 0xFFFFFFFF, "scale": scale, "esz": esz,
                "bound": None, "scaled_inplace": False}
    raw = read_bytes_tbl(bytetab, esz, cnt)
    targets = [(anchor + v * scale) & 0xFFFFFFFF for v in raw]
    return {"bctr": bctr, "table": bytetab & 0xFFFFFFFF, "idx_reg": idx_reg,
            "ncases": cnt, "targets": targets, "kind": "rel",
            "anchor": anchor & 0xFFFFFFFF, "scale": scale, "esz": esz,
            "bound": n, "scaled_inplace": False}


# --- driver -----------------------------------------------------------------
def all_bctr_raw():
    out = []
    ea = IMG_LO
    while ea < IMG_HI:
        if ida_bytes.get_dword(ea) == BCTR:
            out.append(ea)
        ea += 4
    return out

RAW = all_bctr_raw()
print("[xjt] raw bctr opcodes=%d" % len(RAW))
_lap("bctr-scan")

# Iterate: resolving a table reveals its case bodies as code, which may contain
# further bctrs. Loop until a round finds nothing new (coverage fixpoint).
found = {}
for rnd in range(8):
    new = 0
    for i, b in enumerate(RAW):
        if rnd == 0 and i % 3000 == 0 and i:
            print("[xjt] progress scanning %d/%d (tables=%d)" % (i, len(RAW), len(found)))
        if b in found:
            continue
        force_window(b)
        if mnem(b) not in ("bctr", "bcctr"):
            continue
        rec = recognize(b)
        if rec is None:
            continue
        t = rec["targets"]
        if len(t) < 1:
            continue
        if any((x & 3) or not (TEXT_LO <= x < TEXT_HI) for x in t):
            continue
        # inline-.text tables are accepted (recog_abs validates them strictly);
        # reject only a relative byte-table that lands in executable code.
        if rec["kind"] == "rel" and TEXT_LO <= rec["table"] < TEXT_HI:
            continue
        found[b] = rec
        new += 1
        for tgt in set(t):
            ida_xref.add_cref(b, tgt, ida_xref.fl_JN)
            if not ida_bytes.is_code(ida_bytes.get_flags(tgt)):
                ida_ua.create_insn(tgt)
    print("[xjt] round %d: new=%d total=%d" % (rnd, new, len(found)))
    if new == 0 and rnd > 0:
        break
    ida_auto.auto_wait()

with open(OUT_PATH, "w") as fp:
    for b in sorted(found):
        fp.write(json.dumps(found[b]) + "\n")
    fp.write(json.dumps({"_summary": True, "tables": len(found),
                         "raw_bctr": len(RAW)}) + "\n")

_lap("recognition")
print("[xjt] DONE tables=%d  ->  %s" % (len(found), OUT_PATH))
ida_pro.qexit(0)
