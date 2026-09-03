"""
closure.py — how much of a title is actually recompiled, measured from the
emitted C++ instead of asserted.

Two numbers, because a port has two very different kinds of incompleteness and
collapsing them into one figure is how you get a percentage that flatters:

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


def measure(gen_dir):
    """Single pass over generated/. Returns a dict; `static_closed_pct` is None
    when there is nothing to divide by (no sources / empty codegen) rather than
    a fabricated 100."""
    m = {"functions": 0, "landings": 0, "direct_calls": 0, "indirect_sites": 0,
         "switch_tables": 0, "switch_cases": 0,
         "holes": 0, "hole_targets": [], "files": 0}
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
        for t in TRAP.finditer(txt):
            m["holes"] += 1
            m["hole_targets"].append(int(t.group(2), 16))
    if not m["files"]:
        return None
    m["hole_targets"] = sorted(set(m["hole_targets"]))
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
    s = ("closure: static %s (%d hole(s) / %s static targets), %s functions, "
         "%s indirect dispatch site(s) of which %s target(s) are resolved "
         "statically by %d recovered jump table(s)"
         % (pct, m["holes"], "{:,}".format(m["static_targets"]),
            "{:,}".format(m["functions"]), "{:,}".format(m["indirect_sites"]),
            "{:,}".format(m["switch_cases"]), m["switch_tables"]))
    if cures is not None:
        s += ", %d runtime cure(s) registered" % cures
    return s


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python closure.py <port>/generated/default")
    m = measure(sys.argv[1])
    if not m:
        raise SystemExit("no generated sources under %s" % sys.argv[1])
    print(summary_line(m))
    print(json.dumps({k: v for k, v in m.items() if k != "hole_targets"}, indent=2))
    if m["hole_targets"]:
        print("holes: " + ", ".join("0x%08X" % a for a in m["hole_targets"][:24]))


if __name__ == "__main__":
    main()
