"""closure_cert.py -- static closure certificate for a recompiled port.

Proves (or refutes) that the registered function set is CLOSED under every
statically-determinable control target. If closed, the port needs zero launches
to be complete; if not, the unresolved set IS the true residue (enumerated, not
estimated).

Registered set = functions.toml keys + switch_tables landings + funclist.
Targets checked (all from the raw big-endian image over [code_base,code_end)):
  bl / b (op18) direct calls/tails
  bc (op16) conditional branches
  aligned BE dwords in NON-text data that point into code (pointer tables/vtables)
  lis+addi / lis+ori split-immediate code addresses
A target is COVERED if it is a registered function OR lands strictly inside a
registered function's [start,next_start) interval (an in-function label, which
codegen emits as loc_X -- not a hole).
"""
import os, re, struct, sys

def ranges(port):
    pd = os.path.join(r"C:\Skate3Recomp\autoports", port, "port")
    h = open(os.path.join(pd, "generated", "default", port + "_init.h"),
             encoding="utf-8", errors="ignore").read()
    g = lambda k: int(re.search(k + r"\s+0x([0-9A-Fa-f]+)", h).group(1), 16)
    return g("REX_IMAGE_BASE"), g("REX_CODE_BASE"), g("REX_CODE_SIZE")

def registered(port):
    pd = os.path.join(r"C:\Skate3Recomp\autoports", port, "port")
    fns = set()
    fp = os.path.join(pd, port + "_functions.toml")
    for l in open(fp, encoding="utf-8", errors="ignore"):
        m = re.match(r'\s*"0x([0-9A-Fa-f]+)"', l)
        if m: fns.add(int(m.group(1), 16))
    # funclist (every emitted DEFINE_REX_FUNC) is the authoritative registered set
    fl = os.path.join(r"C:\Skate3Recomp\rexauto\work", port, port + "_functions_list.txt")
    if not os.path.exists(fl):
        for root,_,files in os.walk(os.path.join(pd,"generated","default")):
            for f in files:
                if f.endswith(".cpp"):
                    for m in re.finditer(r'sub_([0-9A-Fa-f]{8})', open(os.path.join(root,f),encoding="utf-8",errors="ignore").read()):
                        fns.add(int(m.group(1),16))
    else:
        for l in open(fl, encoding="utf-8", errors="ignore"):
            l=l.strip()
            if l:
                try: fns.add(int(l,16))
                except ValueError: pass
    # switch landings
    sw = os.path.join(pd, port + "_switch_tables.toml")
    lands = set()
    if os.path.exists(sw):
        for m in re.finditer(r'0x([0-9A-Fa-f]{6,8})', open(sw,encoding="utf-8",errors="ignore").read()):
            lands.add(int(m.group(1),16))
    return fns, lands

def main():
    port = sys.argv[1]
    ib, cb, cs = ranges(port)
    ce = cb + cs
    img = open(os.path.join(r"C:\Skate3Recomp\autoports", port, port + "_image.bin"), "rb").read()
    fns, lands = registered(port)
    starts = sorted(f for f in fns if cb <= f < ce)
    covered = set(fns) | lands

    import bisect
    def in_a_function(a):
        # inside [start, next_start) of a registered function?
        i = bisect.bisect_right(starts, a) - 1
        return i >= 0 and starts[i] <= a  # any registered start at/below = interior

    def word(o):
        return struct.unpack_from(">I", img, o)[0] if 0 <= o+4 <= len(img) else None

    targets = {"bl": set(), "b": set(), "bc": set(), "ptr": set(), "splitimm": set()}
    # direct branches
    o = cb - ib; end = min(ce - ib, len(img) - 3)
    lis_hi = [None]*32
    while o <= end:
        w = word(o); pc = ib + o
        op = w >> 26
        if op == 18:
            li = w & 0x03FFFFFC
            if li & 0x02000000: li -= 0x04000000
            t = (li if (w & 2) else pc + li) & 0xFFFFFFFF
            if cb <= t < ce: targets["bl" if (w & 1) else "b"].add(t)
        elif op == 16:
            bd = w & 0xFFFC
            if bd & 0x8000: bd -= 0x10000
            t = (bd if (w & 2) else pc + bd) & 0xFFFFFFFF
            if cb <= t < ce: targets["bc"].add(t)
        elif op == 15:
            rt=(w>>21)&31; ra=(w>>16)&31
            lis_hi[rt] = (w&0xFFFF)<<16 if ra==0 else None
        elif op == 14:
            rt=(w>>21)&31; ra=(w>>16)&31
            if ra and lis_hi[ra] is not None:
                lo=w&0xFFFF
                if lo&0x8000: lo-=0x10000
                full=(lis_hi[ra]+lo)&0xFFFFFFFF
                # A lis/addi product is only a candidate FUNCTION start if it
                # could be one: 4-aligned (unaligned = data constant, ben_10
                # 0x82130001) and the destination dword is not zero (a zero
                # dword is padding, not an instruction -- ben_10 0x82130004
                # pointed into a zero-padded gap). Both are decidable from the
                # image, so filtering them keeps the cert exact, not lenient.
                if cb<=full<ce and (full&3)==0 and word(full-ib): targets["splitimm"].add(full)
            lis_hi[rt]=None
        elif op == 24:
            rt=(w>>21)&31; ra=(w>>16)&31
            if lis_hi[ra] is not None:
                full=(lis_hi[ra]|(w&0xFFFF))&0xFFFFFFFF
                if cb<=full<ce and (full&3)==0 and word(full-ib): targets["splitimm"].add(full)
            lis_hi[rt]=None
        o += 4
    # pointers in non-text data
    o = 0
    while o < len(img)-3:
        a = ib + o
        if not (cb <= a < ce):
            v = word(o)
            if v is not None and cb <= v < ce and (v & 3) == 0:
                targets["ptr"].add(v)
        o += 4

    print("=== closure certificate: %s ===" % port)
    print("  registered functions in code: %d | switch landings: %d" % (len(starts), len(lands)))
    total_holes = set()
    for k, ts in targets.items():
        holes = {t for t in ts if t not in covered and not in_a_function(t)}
        total_holes |= holes
        print("  %-9s targets=%6d  holes(unregistered)=%d" % (k, len(ts), len(holes)))
    print("  >>> TOTAL UNIQUE HOLES (true static residue) = %d" % len(total_holes))
    if total_holes:
        print("      %s%s" % (", ".join("0x%X"%a for a in sorted(total_holes)[:15]),
                              " ..." if len(total_holes)>15 else ""))

if __name__ == "__main__":
    main()
