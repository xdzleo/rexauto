"""
closure.py — how much of a title is actually recompiled, measured from the
emitted C++ instead of asserted.

The headline is BYTE COVERAGE, which is the convention the decomp community
settled on (decomp.dev / frogress report "matched bytes / total code bytes" and
call it the honest metric). Their reason applies here unchanged: counting
*functions* flatters the result, because the easy ones are small and the hard
ones are big -- on their data an average matched function is 46 bytes against
986 for an unmatched one. So this reports the share of the image's code range
that has emitted C++ behind it, weighted by bytes, not by symbol count.

Covered bytes are the UNION of the emitted function extents, not 4x the emitted
instruction count. Codegen writes one `// <mnemonic ...>` comment per translated
PowerPC instruction and every instruction is 4 bytes, so a function starting at
S with N of them covers [S, S+4N) -- but those ranges OVERLAP: a chunk re-emits
part of its parent, and a boundary override can make two functions cover the
same code. Summing instructions therefore double-counts, and on Gears of War
Judgment it produced 100.0209%, a number that cannot exist and that would have
been reported as success. Merging the ranges first is what makes the figure a
real fraction of the image. The denominator is REX_CODE_SIZE from the header.

Note the denominator includes inter-function alignment padding, which is not
code and can never be covered: on Judgment 28,108 of the 28,381 gaps hold only
zeros or `nop`, 112,452 bytes of the 129,064 uncovered. 100% is therefore not
the target; the honest target is "no gap that contains instructions".

Two further numbers, because a port has two very different kinds of
incompleteness and collapsing them into one figure is how you get a percentage
that flatters:

  static closure   Every control target the recompiler could derive statically.
                   A HOLE is one it could not resolve, and it says so itself:
                   codegen bakes a literal REX_FATAL("Unresolved call/branch
                   from 0x.. to 0x..") into the .cpp at that site. So holes are
                   counted from the recompiler's own admissions, never inferred.
                   Denominator = the resolved targets it emitted beside them
                   (`goto loc_` landings + direct `sub_XXXXXXXX(` calls).

  indirect surface The REX_CALL_INDIRECT_FUNC sites, which resolve only when the
                   guest runs. Each one can reach an address the static scan
                   never registered -- that is the entire workload of the
                   run-heal, and no static pass can retire it. Reported as a
                   count plus the cures registered so far, NOT folded into a
                   percentage, because its true denominator is unknowable
                   without running every code path in the game.

Why not metrics/CLOSURE.md: that table came from `closure_cert`, which this
project's own audit records as unable to run on any port (roots hardcoded to a
tree that does not exist) with a coverage predicate that reduces to
`a >= starts[0]` -- a zero-width hole window for 27 of 29 titles, so its
"ZERO static holes" was forced, not earned. Nothing here reuses it.

    python closure.py <port>/generated/default
"""
import json
import os
import re
import sys

TRAP = re.compile(r'REX_FATAL\("Unresolved (?:call|branch) from '
                  r'0x([0-9A-Fa-f]+) to 0x([0-9A-Fa-f]+)"')
DEFN = re.compile(r"DEFINE_REX_FUNC\(sub_[0-9A-Fa-f]+\)")
LAND = re.compile(r"goto loc_[0-9A-Fa-f]+")
CALL = re.compile(r"\bsub_[0-9A-Fa-f]{8}\(")
INDIRECT = re.compile(r"\bREX_CALL_INDIRECT_FUNC\b")
SWITCH = re.compile(r"\bswitch \(ctx\.")
CASE = re.compile(r"\bcase 0x[0-9A-Fa-f]+:")
# one per translated PowerPC instruction: "\t// lwz r11,0(r3)"
INSTR = re.compile(r"^[ \t]+// [a-z][a-z0-9._]*", re.M)
INSTR_LINE = re.compile(r"^[ \t]+// [a-z][a-z0-9._]*")
DEFN_ADDR = re.compile(r"DEFINE_REX_FUNC\((?:[A-Za-z]\w*_)?sub_([0-9A-Fa-f]+)\)")
# Every guest address the emitted body actually names: a `loc_` label, and the
# return address stored before each call. The instruction count alone ends a
# function at start+4N, which is short whenever codegen emits fewer comment
# lines than the routine spans (a boundary-extended function, a chunk): on Gears
# of War Judgment 466 of 236,440 labels fell outside that window. Extending each
# extent to the furthest address its own body names brings that to zero.
LOC_LINE = re.compile(r"^loc_([0-9A-Fa-f]{8}):")
LR_LINE = re.compile(r"^[ \t]+ctx\.lr = 0x([0-9A-Fa-f]+);")
CODE_SIZE = re.compile(r"#define REX_CODE_SIZE 0x([0-9A-Fa-f]+)")
CODE_BASE = re.compile(r"#define REX_CODE_BASE 0x([0-9A-Fa-f]+)")


def measure(gen_dir, image_path=None, image_base=None):
    """Single pass over generated/. Returns a dict; `static_closed_pct` is None
    when there is nothing to divide by (no sources / empty codegen) rather than
    a fabricated 100."""
    m = {"functions": 0, "landings": 0, "direct_calls": 0, "indirect_sites": 0,
         "switch_tables": 0, "switch_cases": 0, "instructions": 0,
         "holes": 0, "hole_targets": [], "files": 0}
    code_base = code_size = None
    spans = []
    if not os.path.isdir(gen_dir):
        return None
    for fn in sorted(os.listdir(gen_dir)):
        if not fn.endswith((".cpp", ".h")):
            continue
        try:
            with open(os.path.join(gen_dir, fn), "r", encoding="utf-8",
                      errors="ignore") as f:
                txt = f.read()
        except OSError:
            continue
        m["files"] += 1
        m["functions"] += len(DEFN.findall(txt))
        m["landings"] += len(LAND.findall(txt))
        m["direct_calls"] += len(CALL.findall(txt))
        m["indirect_sites"] += len(INDIRECT.findall(txt))
        m["switch_tables"] += len(SWITCH.findall(txt))
        m["switch_cases"] += len(CASE.findall(txt))
        m["instructions"] += len(INSTR.findall(txt))
        cur, n, far = None, 0, 0
        for line in txt.splitlines():
            d = DEFN_ADDR.search(line)
            if d:
                if cur is not None and n:
                    spans.append((cur, n, far))
                cur, n, far = int(d.group(1), 16), 0, 0
            elif cur is not None:
                if INSTR_LINE.match(line):
                    n += 1
                    continue
                lm = LOC_LINE.match(line) or LR_LINE.match(line)
                if lm:
                    a = int(lm.group(1), 16)
                    if a > far:
                        far = a
        if cur is not None and n:
            spans.append((cur, n, far))
        if code_size is None:
            mm, mb = CODE_SIZE.search(txt), CODE_BASE.search(txt)
            if mm:
                code_size = int(mm.group(1), 16)
            if mb:
                code_base = int(mb.group(1), 16)
        for t in TRAP.finditer(txt):
            m["holes"] += 1
            m["hole_targets"].append(int(t.group(2), 16))
    if not m["files"]:
        return None
    m["hole_targets"] = sorted(set(m["hole_targets"]))
    # byte coverage -- the headline. 4 bytes per translated instruction over the
    # image's code range. None (not 100) when the header did not carry the range.
    m["code_base"] = code_base
    m["code_bytes"] = code_size
    # union of [start, start+4*instructions) -- ranges overlap, so summing
    # instruction counts double-counts and can exceed the code size outright
    merged = []
    for st, en in sorted((a, max(a + 4 * n, (far + 4) if far > a else 0))
                         for a, n, far in spans):
        if merged and st <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], en)
        else:
            merged.append([st, en])
    lo = code_base if code_base is not None else 0
    hi = lo + code_size if code_size else None
    cov = 0
    for st, en in merged:
        if hi is not None:
            st, en = max(st, lo), min(en, hi)
        if en > st:
            cov += en - st
    m["covered_bytes"] = cov or 4 * m["instructions"]
    m["byte_coverage_pct"] = (round(100.0 * m["covered_bytes"] / code_size, 4)
                              if code_size else None)
    m["uncovered_bytes"] = (code_size - m["covered_bytes"]) if code_size else None
    # Coverage of REAL CODE: the range's denominator includes inter-function
    # alignment padding (zeros / `nop`), which has nothing to recompile and can
    # never be covered. On Gears of War Judgment that is 112,820 bytes -- charging
    # it against the port understates the result by three quarters of a point.
    m["padding_bytes"] = None
    m["code_coverage_pct"] = None
    if image_path and code_size and os.path.exists(image_path):
        try:
            import array
            with open(image_path, "rb") as f:
                img = f.read()
            w = array.array("I")
            w.frombytes(img[:len(img) - (len(img) % 4)])
            if sys.byteorder == "little":
                w.byteswap()
            ib = image_base if image_base is not None else code_base

            def _word(a):
                o = (a - ib) // 4
                return w[o] if 0 <= o < len(w) else None
            gaps, pos = [], lo
            for st, en in merged:
                if st > pos:
                    gaps.append((pos, st))
                pos = max(pos, en)
            if pos < hi:
                gaps.append((pos, hi))
            pad = 0
            for st, en in gaps:
                if all(_word(a) in (0, 0x60000000, None) for a in range(st, en, 4)):
                    pad += en - st
            m["padding_bytes"] = pad
            real = code_size - pad
            if real > 0:
                m["code_coverage_pct"] = round(100.0 * m["covered_bytes"] / real, 4)
        except Exception:
            pass
    m["static_targets"] = m["landings"] + m["direct_calls"] + m["holes"]
    m["static_closed_pct"] = (round(100.0 * (m["static_targets"] - m["holes"])
                                    / m["static_targets"], 4)
                              if m["static_targets"] else None)
    return m


def summary_line(m, cures=None):
    """One line for the pipeline log. Never prints a bare percentage without the
    indirect surface beside it -- static closure at 100% is normal and says
    nothing about whether the game reaches gameplay."""
    if not m:
        return "closure: generated/ not measurable"
    pct = "n/a" if m["static_closed_pct"] is None else "%.4f%%" % m["static_closed_pct"]
    byt = ("recompiled: %.4f%% by code bytes (%s / %s), "
           % (m["byte_coverage_pct"], "{:,}".format(m["covered_bytes"]),
              "{:,}".format(m["code_bytes"]))) if m["byte_coverage_pct"] is not None \
        else "recompiled: n/a by code bytes, "
    s = (byt + "static closure %s (%d hole(s) / %s static targets), %s functions, "
         "%s indirect dispatch site(s) of which %s target(s) are resolved "
         "statically by %d recovered jump table(s)"
         % (pct, m["holes"], "{:,}".format(m["static_targets"]),
            "{:,}".format(m["functions"]), "{:,}".format(m["indirect_sites"]),
            "{:,}".format(m["switch_cases"]), m["switch_tables"]))
    if cures is not None:
        s += ", %d runtime cure(s) registered" % cures
    return s


def covered_ranges(gen_dir):
    """Merged [start, end) of every emitted function, in guest addresses.

    Shared with the pipeline's gap-fill pass so the thing that measures coverage
    and the thing that closes it can never disagree about what "covered" means.
    Each function ends at the furthest address its own body names -- see LOC_LINE
    above for why start+4*instructions is not enough."""
    spans = []
    if not os.path.isdir(gen_dir):
        return []
    for fn in sorted(os.listdir(gen_dir)):
        if not fn.endswith(".cpp"):
            continue
        cur, n, far = None, 0, 0
        with open(os.path.join(gen_dir, fn), "r", encoding="utf-8",
                  errors="ignore") as f:
            for line in f:
                d = DEFN_ADDR.search(line)
                if d:
                    if cur is not None and n:
                        spans.append((cur, n, far))
                    cur, n, far = int(d.group(1), 16), 0, 0
                elif cur is not None:
                    if INSTR_LINE.match(line):
                        n += 1
                        continue
                    lm = LOC_LINE.match(line) or LR_LINE.match(line)
                    if lm:
                        a = int(lm.group(1), 16)
                        if a > far:
                            far = a
        if cur is not None and n:
            spans.append((cur, n, far))
    merged = []
    for st, en in sorted((a, max(a + 4 * n, (f + 4) if f > a else 0))
                         for a, n, f in spans):
        if merged and st <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], en)
        else:
            merged.append([st, en])
    return merged


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit("usage: python closure.py <port>/generated/default [<name>_image.bin]")
    m = measure(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
    if not m:
        raise SystemExit("no generated sources under %s" % sys.argv[1])
    print(summary_line(m))
    print(json.dumps({k: v for k, v in m.items() if k != "hole_targets"}, indent=2))
    if m["hole_targets"]:
        print("holes: " + ", ".join("0x%08X" % a for a in m["hole_targets"][:24]))


if __name__ == "__main__":
    main()
