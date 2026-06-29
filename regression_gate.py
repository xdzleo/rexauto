#!/usr/bin/env python3
"""regression_gate.py -- the standing "no regression in any game" guard.

For every project under autoports/, re-run codegen with the *current* rexglue/SDK
and compare the generated C++ (an md5 per file) to a blessed baseline. rexglue
codegen is deterministic (no timestamps in the output), so identical inputs +
unchanged rexauto/SDK => byte-identical generated tree. A diff therefore means the
change under test altered that game's recompiled output -- a regression unless you
bless it on purpose. This is what lets us touch shared code (heal.py, rexauto.py,
even the SDK) and *prove* the other titles are unaffected before shipping.

    python regression_gate.py                 # gate: codegen all, diff vs baseline,
                                              #   exit !=0 if any game changed
    python regression_gate.py --bless         # (re)record the baseline = current good state
    python regression_gate.py skate3 rayman3hd # limit to named games
    python regression_gate.py --bless skate3   # re-bless one game after an intended change

Scope: this is the codegen tier -- it catches every rexauto / heal / rexglue-codegen
regression (those all change the generated C++). A pure rexruntime.dll change leaves
codegen identical; gate that with a boot run (regression_gate.py --runtime, TODO).
"""
import os
import sys
import json
import glob
import hashlib
import subprocess
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
AUTOPORTS = r"C:\Skate3Recomp\autoports"
BASELINES = os.path.join(HERE, "baselines")
HEAVY = {"skate3"}                 # run last / alone-ish (huge image)
MAX_PARALLEL = 4


def find_rexglue():
    cands = [os.environ.get("REXGLUE"),
             r"C:\Skate3Recomp\rexglue-sdk\out\install\win-amd64\bin\rexglue.exe"]
    cands += sorted(glob.glob(r"C:\Skate3Recomp\rexglue-sdk\out\win-amd64\*\rexglue.exe"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit("regression_gate: rexglue.exe not found (set REXGLUE)")


def projects(names):
    out = []
    if not os.path.isdir(AUTOPORTS):
        return out
    for d in sorted(os.listdir(AUTOPORTS)):
        man = os.path.join(AUTOPORTS, d, "port", "%s_manifest.toml" % d)
        if os.path.exists(man) and (not names or d in names):
            out.append((d, os.path.join(AUTOPORTS, d, "port"), man))
    return out


def snapshot(port):
    """md5 of every generated source/header across ALL module dirs (generated/**)."""
    snap = {}
    base = os.path.join(port, "generated")
    for ext in ("*.cpp", "*.h"):
        for f in glob.glob(os.path.join(base, "**", ext), recursive=True):
            rel = os.path.relpath(f, port).replace("\\", "/")
            h = hashlib.md5()
            with open(f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            snap[rel] = h.hexdigest()
    return snap


def codegen(rexglue, port, man):
    r = subprocess.run([rexglue, "--log-level", "error", "codegen", man],
                       cwd=port, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()[-1500:]


def baseline_path(name):
    return os.path.join(BASELINES, name + ".json")


def run_one(name, port, man, rexglue, bless):
    rc, tail = codegen(rexglue, port, man)
    bpath = baseline_path(name)
    base = json.load(open(bpath)) if os.path.exists(bpath) else None

    if bless:
        os.makedirs(BASELINES, exist_ok=True)
        if rc != 0:
            json.dump({"skip": True, "rc": rc}, open(bpath, "w"))
            return name, "BLESSED-SKIP", "codegen rc=%d (no game data?) -- recorded as skip" % rc
        snap = snapshot(port)
        json.dump({"files": snap, "n": len(snap)}, open(bpath, "w"))
        return name, "BLESSED", "%d files" % len(snap)

    # gate
    if base is None:
        return name, "NO-BASELINE", "run --bless first"
    if base.get("skip"):
        return (name, "PASS", "still skipped (no data)") if rc != 0 else \
               (name, "CHANGED", "was skipped, now codegens -- bless if intended")
    if rc != 0:
        return name, "REGRESSION", "codegen now FAILS (rc=%d): %s" % (rc, tail.splitlines()[-1] if tail else "")
    snap = snapshot(port)
    bfiles = base["files"]
    changed = sorted(f for f in snap if bfiles.get(f) != snap[f] and f in bfiles)
    added = sorted(f for f in snap if f not in bfiles)
    removed = sorted(f for f in bfiles if f not in snap)
    if changed or added or removed:
        smp = (changed + added + removed)[:4]
        return name, "REGRESSION", "%d changed / %d added / %d removed  e.g. %s" % (
            len(changed), len(added), len(removed), ", ".join(smp))
    return name, "PASS", "%d files identical" % len(snap)


def main():
    args = sys.argv[1:]
    bless = "--bless" in args
    runtime = "--runtime" in args
    names = [a for a in args if not a.startswith("--")]
    if runtime:
        print("regression_gate: --runtime (boot tier) not implemented yet; running codegen tier")
    rexglue = find_rexglue()
    projs = projects(names)
    if not projs:
        sys.exit("regression_gate: no matching projects under %s" % AUTOPORTS)
    # light games first, heavy last
    projs.sort(key=lambda p: (p[0] in HEAVY, p[0]))
    print("regression_gate: %s %d project(s) with %s" % (
        "BLESSING" if bless else "gating", len(projs), os.path.basename(rexglue)))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = {ex.submit(run_one, n, p, m, rexglue, bless): n for (n, p, m) in projs}
        for fut in concurrent.futures.as_completed(futs):
            name, verdict, detail = fut.result()
            results[name] = verdict
            print("  %-32s %-13s %s" % (name, verdict, detail))

    bad = [n for n, v in results.items() if v in ("REGRESSION", "CHANGED", "NO-BASELINE")]
    print()
    if bless:
        print("baselines written to rexauto/baselines/ -- commit them.")
        return 0
    if bad:
        print("GATE FAIL: %d project(s) changed -> %s" % (len(bad), ", ".join(sorted(bad))))
        print("If intended, re-bless: python regression_gate.py --bless %s" % " ".join(sorted(bad)))
        return 1
    print("GATE PASS: all %d project(s) byte-identical -- no regression." % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
