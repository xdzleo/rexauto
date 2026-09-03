"""
boundaries.py — derive function-boundary overrides from the recompiler's own discovery.

Static recompilers (XenonRecomp / ReXGlue) split some functions at the wrong
address: the boundary that .pdata or a disassembler gives is coarser or finer
than the compiler's real function. Teams fix this by hand — the Skate 3 port
ships ~3500 such `end` / `parent` overrides.

Most of that is derivable automatically. When the recompiler runs, it discovers a
grid of function starts and basic-block labels. For an address A that needs a
boundary fix:

    parent(A) = largest discovered function start  < A   (the function A belongs to)
    end(A)    = next discovered block boundary      > A   (where A's region ends)

The grid comes purely from the recompiler's own output (no answer key), so this
is non-circular. Measured on Skate 3 against the hand-made override set:

    parent 1107/1160 (95%)    end 1250/1740 (72%)

Why this works when reading the binary doesn't: the split points are not marked
in the image (not .pdata entries, not call/branch/pointer targets — verified).
They only emerge from the recompiler's *combined* multi-phase discovery
(prologue + call graph + gap fill + merge). So the tool reads that, not the bytes.

The remaining gap is honest: ~5% of parents are cold funcs reachable only through
the C++ exception unwinder (absent from the grid), and ~28% of ends fall where the
recompiler never placed a boundary. Those still need manual attention or .xdata.

Subcommands:
    grid   <generated_dir> -o grid.json        parse the recompiler-generated C++
    derive grid.json addrs.txt -o out.toml      emit overrides for a list of addrs
    check  grid.json gabarito.toml              measure reproduction vs a known set

`addrs.txt` is one hex address per line — the functions you need overrides for
(in practice, the addresses the recompiler reports as unresolved / mis-split).
`out.toml` is a `[functions]` block: `"0xA" = { end = 0xE, parent = 0xP }`.
"""
import argparse
import glob
import json
import os
import re
import bisect


SETFUNC = re.compile(r"SetFunction\(\s*0x([0-9A-Fa-f]{8})")
LOCLBL = re.compile(r"\bloc_([0-9A-Fa-f]{8})\s*:")


def build_grid(generated_dir):
    """Parse the recompiler's generated C++: function starts (SetFunction) and
    basic-block labels (loc_XXXXXXXX:)."""
    starts, labels = set(), set()
    files = glob.glob(os.path.join(generated_dir, "**", "*.cpp"), recursive=True)
    if not files:
        raise SystemExit("no .cpp files under %s" % generated_dir)
    for fp in files:
        with open(fp, "r", errors="ignore") as f:
            text = f.read()
        for m in SETFUNC.finditer(text):
            starts.add(int(m.group(1), 16))
        for m in LOCLBL.finditer(text):
            labels.add(int(m.group(1), 16))
    return sorted(starts), sorted(starts | labels)


def parent_of(starts, a):
    i = bisect.bisect_left(starts, a) - 1   # strictly < a (a itself is a chunk)
    return starts[i] if i >= 0 else None


def end_of(bounds, a):
    i = bisect.bisect_right(bounds, a)      # strictly > a
    return bounds[i] if i < len(bounds) else None


def load_grid(path):
    g = json.load(open(path))
    return g["starts"], g["bounds"]


def cmd_grid(args):
    starts, bounds = build_grid(args.generated_dir)
    json.dump({"starts": starts, "bounds": bounds}, open(args.out, "w"))
    print("grid: %d function starts, %d boundaries -> %s" % (len(starts), len(bounds), args.out))


def cmd_derive(args):
    starts, bounds = load_grid(args.grid)
    addrs = [int(l.strip(), 16) for l in open(args.addresses) if l.strip()]
    lines = ["# Function-boundary overrides derived from the recompiler's discovery grid.",
             "# parent = containing discovered function; end = next discovered boundary.", "",
             "[functions]"]
    n_end = n_par = 0
    for a in sorted(addrs):
        p = parent_of(starts, a)
        e = end_of(bounds, a)
        parts = []
        if e is not None and e > a:
            parts.append("end = 0x%08X" % e); n_end += 1
        if p is not None and p < a:
            parts.append("parent = 0x%08X" % p); n_par += 1
        if parts:
            lines.append('"0x%08X" = { %s }' % (a, ", ".join(parts)))
    open(args.out, "w").write("\n".join(lines) + "\n")
    print("wrote %s: %d addrs (%d with end, %d with parent)" % (args.out, len(addrs), n_end, n_par))


def cmd_check(args):
    starts, bounds = load_grid(args.grid)
    txt = open(args.gabarito).read()
    parents, ends = {}, {}
    for m in re.finditer(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{([^}]*)\}', txt):
        a = int(m.group(1), 16)
        pp = re.search(r'parent\s*=\s*0x([0-9A-Fa-f]+)', m.group(2))
        ee = re.search(r'end\s*=\s*0x([0-9A-Fa-f]+)', m.group(2))
        if pp:
            parents[a] = int(pp.group(1), 16)
        if ee:
            ends[a] = int(ee.group(1), 16)
    pm = sum(1 for a, P in parents.items() if parent_of(starts, a) == P)
    em = sum(1 for a, E in ends.items() if end_of(bounds, a) == E)
    print("parent: %d/%d (%.1f%%)" % (pm, len(parents), 100.0 * pm / max(1, len(parents))))
    print("end:    %d/%d (%.1f%%)" % (em, len(ends), 100.0 * em / max(1, len(ends))))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid", help="parse recompiler-generated C++ into a grid")
    g.add_argument("generated_dir")
    g.add_argument("-o", "--out", default="grid.json")
    g.set_defaults(func=cmd_grid)
    de = sub.add_parser("derive", help="emit overrides for a list of addresses")
    de.add_argument("grid")
    de.add_argument("addresses")
    de.add_argument("-o", "--out", default="function_overrides.toml")
    de.set_defaults(func=cmd_derive)
    ch = sub.add_parser("check", help="measure reproduction against a known override set")
    ch.add_argument("grid")
    ch.add_argument("gabarito")
    ch.set_defaults(func=cmd_check)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
