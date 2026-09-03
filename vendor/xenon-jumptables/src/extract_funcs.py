"""
Build the function-address list from a recompiler's generated C/C++ sources.

XenonRecomp / ReXGlue emit one function per guest address as ``PPC_FUNC(sub_X)``
/ ``PPC_FUNC_IMPL(__imp__sub_X)`` / ``DEFINE_REX_FUNC(sub_X)``. Those addresses
are the .pdata-derived function boundaries — feeding them to the IDA pass gives
full coverage. (You can also produce this list any other way: a .pdata dump, a
map file, etc. — one hex address per line.)

Usage:
    python extract_funcs.py <generated_dir> -o functions.txt
"""
import argparse
import os
import re

# The optional \w+_ prefix covers companion-module symbols: a multi-XEX module
# emits DEFINE_REX_FUNC(<module>_sub_XXXXXXXX) (e.g. fifadllzf_sub_8270D500),
# while the entrypoint emits the bare sub_XXXXXXXX form. Without it the
# extractor returned 0 functions for every companion -> empty known-list ->
# deep-extract fed IDA everything as "new" (fifadllzf: 93746 candidates) and
# the pure-add gate spent ~40 minutes rejecting the lot.
PAT = re.compile(r"(?:PPC_FUNC(?:_IMPL)?|DEFINE_REX_FUNC)\(\s*(?:__imp__)?(?:\w+_)?sub_([0-9A-Fa-f]{8})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="directory with generated recompiler sources")
    ap.add_argument("-o", "--out", default="functions.txt")
    args = ap.parse_args()

    # Fast path: the generated *_init.h monolith DECLAREs every emitted
    # function (declarations are generated FROM the emitted set, 1:1), so
    # parsing it alone gives the identical address set at ~1/100th the bytes
    # (fifadllzf: 4.3MB vs 425MB, 0.02s vs 1.3s, same 101426 functions).
    # Bytes-mode regex skips the text decode. Falls back to the full walk when
    # no init header exists or it yields nothing (defensive: never emit less).
    addrs = set()
    init_headers = [os.path.join(args.dir, f) for f in os.listdir(args.dir)
                    if f.endswith("_init.h")] if os.path.isdir(args.dir) else []
    bpat = re.compile(
        rb"(?:PPC_FUNC(?:_IMPL)?|DE(?:FINE|CLARE)_REX_FUNC)\(\s*(?:__imp__)?(?:\w+_)?sub_([0-9A-Fa-f]{8})")
    for p in init_headers:
        with open(p, "rb") as f:
            for m in bpat.finditer(f.read()):
                addrs.add(int(m.group(1), 16))
    if not addrs:
        for root, _, files in os.walk(args.dir):
            for fn in files:
                if not fn.endswith((".cpp", ".c", ".cc", ".h", ".hpp")):
                    continue
                with open(os.path.join(root, fn), "r", errors="ignore") as f:
                    for m in PAT.finditer(f.read()):
                        addrs.add(int(m.group(1), 16))

    with open(args.out, "w") as f:
        for a in sorted(addrs):
            f.write("%08X\n" % a)
    print("wrote %s: %d functions (0x%08X .. 0x%08X)" % (
        args.out, len(addrs), min(addrs) if addrs else 0, max(addrs) if addrs else 0))


if __name__ == "__main__":
    main()
