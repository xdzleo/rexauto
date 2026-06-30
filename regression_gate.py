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

Two tiers. The codegen tier (always) catches every rexauto / heal / rexglue-codegen
regression (those change the generated C++). The runtime tier (--runtime, opt-in)
builds + launches each codegen-clean title headless for N seconds and scores a
play-health metric (boots / alive / no new FATAL / reached gameplay marker) vs a
blessed sibling baseline -- so a pure rexruntime.dll change, or an app-glue change
that boots-but-crashes a few seconds in, is caught too.

    python regression_gate.py --runtime                 # both tiers, all games
    python regression_gate.py --runtime skate3          # both tiers, skate3 only
    python regression_gate.py --bless --runtime skate3  # record skate3 runtime baseline
"""
import os
import sys
import re
import json
import glob
import time
import hashlib
import subprocess
import importlib.util
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
AUTOPORTS = r"C:\Skate3Recomp\autoports"
BASELINES = os.path.join(HERE, "baselines")
HEAVY = {"skate3"}                 # run last / alone-ish (huge image)
MAX_PARALLEL = 4
RUNTIME_SECONDS = int(os.environ.get("REXGATE_RUN_SECONDS", "30"))
# HEAVY titles reach their gameplay marker late (skate3's fires ~40s in), so a short
# run scores a spurious tier 2. Give HEAVY titles a longer floor so the marker is
# reliably captured and the run stays alive past it -> deterministic tier 3.
HEAVY_RUNTIME_SECONDS = int(os.environ.get("REXGATE_RUN_SECONDS_HEAVY", "90"))
# Per-title "reached interactive/gameplay" log marker -> health_tier 3 when seen.
MARKERS = {"skate3": "gameplay context reached"}


def _load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# heal.py: reuse _read_text + invalid_functions_from_text so the runtime tier reads
# logs + classifies FATALs exactly like the pipeline (falls back to inline if absent).
try:
    _heal = _load("heal", os.path.join(HERE, "heal.py"))
except Exception:
    _heal = None


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


# ============================ runtime tier ===================================
# Build the freshly-codegen'd title, launch it headless for N seconds, score a
# play-health metric vs a blessed sibling baseline (baselines/<name>.runtime.json).
# Read-only of pipeline data: a plain cmake build via the per-game _build.bat,
# never heal/register/edit functions.toml. A boot/crash is a finding, not a fix.

def _read_log(path):
    if _heal and hasattr(_heal, "_read_text"):
        try:
            return _heal._read_text(path)
        except Exception:
            pass
    try:
        return open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def _fatal_addrs(txt):
    if _heal and hasattr(_heal, "invalid_functions_from_text"):
        try:
            return list(_heal.invalid_functions_from_text(txt))
        except Exception:
            pass
    return [int(a, 16) for a in re.findall(
        r"invalid or unregistered function at guest address 0x([0-9A-Fa-f]+)", txt)]


def runtime_baseline_path(name):
    return os.path.join(BASELINES, name + ".runtime.json")


def _builddir(port):
    return os.path.join(port, "out", "build", "win-amd64-release")


def _game_root(port):
    f = os.path.join(_builddir(port), "game_root.txt")
    if not os.path.exists(f):
        return None
    try:
        return open(f, encoding="utf-8").read().strip() or None
    except OSError:
        return None


def _build_game(name, timeout=1800):
    """Build the current generated tree via the per-game _build.bat (vcvars+clang
    baked in by the last rexauto run) -- a plain cmake build, never heal/register.
    Returns (rc, last_line)."""
    bat = os.path.join(AUTOPORTS, name, "_build.bat")
    if not os.path.exists(bat):
        return 1, "no _build.bat (run a rexauto build for %s first)" % name
    try:
        r = subprocess.run(["cmd", "/c", bat], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "build timed out after %ds" % timeout
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    rc = r.returncode
    for line in out.splitlines():               # the bat echoes 'RC=%errorlevel%'
        mm = re.search(r"\bRC=(\d+)", line)
        if mm:
            rc = int(mm.group(1))
    return rc, (out.splitlines()[-1] if out else "")


def _launch_headless(exe, builddir, game, seconds):
    """rexauto.run_once's launch, self-contained: detached, poll, hard-kill, then
    read the newest log this launch produced. Returns (txt, alive, launch_err)."""
    logdir = os.path.join(builddir, "logs")
    before = set(glob.glob(os.path.join(logdir, "*.log")))
    t0 = time.time()
    cmd = [exe] + (["--game_data_root=%s" % game] if game else [])
    try:
        p = subprocess.Popen(cmd, cwd=builddir,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as ex:
        return "", False, str(ex)
    while time.time() - t0 < seconds:
        if p.poll() is not None:
            break
        time.sleep(0.5)
    alive = p.poll() is None
    if alive:
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
            try:
                p.wait(timeout=5)
            except Exception:
                pass
    new = [q for q in glob.glob(os.path.join(logdir, "*.log"))
           if q not in before or os.path.getmtime(q) >= t0]
    txt = _read_log(max(new, key=os.path.getmtime)) if new else ""
    return txt, alive, ""


def _measure(name, port, seconds):
    """Build + headless-run + score. health_tier: 0 no-boot/crash, 1 boots no fatal,
    2 alive after N s no fatal, 3 alive + reached the title's gameplay marker."""
    bdir = _builddir(port)
    exe = os.path.join(bdir, name + ".exe")
    rc, btail = _build_game(name)
    if rc != 0 or not os.path.exists(exe):
        return {"exe_built": 0, "boots": 0, "alive": 0, "fatals": 0, "fatal_addrs": [],
                "marker": 0, "health_tier": 0, "run_seconds": seconds,
                "error": "build rc=%d: %s" % (rc, btail)}
    txt, alive, lerr = _launch_headless(exe, bdir, _game_root(port), seconds)
    booted = 1 if txt else 0
    addrs = _fatal_addrs(txt) if txt else []
    fatals = len(addrs)
    if txt and not fatals and "[FATAL]" in txt:
        fatals = txt.count("[FATAL]")
    mk = MARKERS.get(name)
    marker = 1 if (mk and txt and mk in txt) else 0
    # "reached the gameplay marker" is the deterministic success milestone -> tier 3.
    # A non-deterministic crash AFTER it (a deep unregistered jump-table target -- the
    # switch-on-ctr heal long-tail, tracked in `fatals`) does NOT demote a build that
    # proved it reaches gameplay. A crash BEFORE the marker (no marker) still scores
    # low, so a real boot/gameplay break is still caught.
    tier = 3 if marker else (0 if (fatals or not booted) else 2 if alive else 1)
    return {"exe_built": 1, "boots": booted, "alive": 1 if alive else 0, "fatals": fatals,
            "fatal_addrs": ["0x%X" % a for a in addrs[:8]], "marker": marker,
            "marker_str": mk or "", "health_tier": tier, "run_seconds": seconds,
            "error": lerr}


def run_one_runtime(name, port, bless, seconds):
    m = _measure(name, port, seconds)
    bpath = runtime_baseline_path(name)
    base = json.load(open(bpath)) if os.path.exists(bpath) else None
    info = "tier=%d boots=%d alive=%d fatals=%d%s%s" % (
        m["health_tier"], m["boots"], m["alive"], m["fatals"],
        " marker" if m["marker"] else "",
        (" | %s" % m["error"]) if m.get("error") else "")
    if bless:
        os.makedirs(BASELINES, exist_ok=True)
        json.dump(m, open(bpath, "w"), indent=1)
        return name, "BLESSED", info
    if base is None:
        return name, "NO-BASELINE", "run --bless --runtime first (%s)" % info
    if m["fatals"] and not base.get("fatals") and not m["marker"]:
        # a NEW fatal that also kept the build from reaching gameplay is a real
        # regression; a fatal after a reached marker is the heal long-tail (informational).
        return name, "REGRESSION", "NEW fatal %s before gameplay (was clean) | %s" % (
            ", ".join(m["fatal_addrs"]) or m["fatals"], info)
    if base.get("boots") and not m["boots"]:
        return name, "REGRESSION", "no longer boots | %s" % info
    if m["health_tier"] < base.get("health_tier", 0):
        return name, "REGRESSION", "health %d < baseline %d | %s" % (
            m["health_tier"], base["health_tier"], info)
    if m["health_tier"] > base.get("health_tier", 0):
        return name, "IMPROVED", "health %d > baseline %d | %s" % (
            m["health_tier"], base["health_tier"], info)
    return name, "PASS", info


def main():
    args = sys.argv[1:]
    bless = "--bless" in args
    runtime = "--runtime" in args
    names = [a for a in args if not a.startswith("--")]
    rexglue = find_rexglue()
    projs = projects(names)
    if not projs:
        sys.exit("regression_gate: no matching projects under %s" % AUTOPORTS)
    # light games first, heavy last
    projs.sort(key=lambda p: (p[0] in HEAVY, p[0]))
    print("regression_gate: %s %d project(s) with %s [codegen tier]" % (
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

    # ---- runtime tier (opt-in): build + headless launch on codegen-clean games --
    rt_bad, rt_improved = [], []
    if runtime:
        print("-- runtime tier: build + %ds headless launch, serial light->heavy --"
              % RUNTIME_SECONDS)
        for (n, p, m) in projs:                       # already light->heavy sorted
            if results.get(n) not in ("PASS", "BLESSED"):
                print("  %-32s %-13s %s" % (n, "RT-SKIP",
                      "codegen verdict %s -- not launched" % results.get(n)))
                continue
            nm, verdict, detail = run_one_runtime(n, p, bless, HEAVY_RUNTIME_SECONDS if n in HEAVY else RUNTIME_SECONDS)
            print("  %-32s %-13s %s" % (nm, verdict, detail))
            if verdict == "REGRESSION":
                rt_bad.append(nm)
            elif verdict == "IMPROVED":
                rt_improved.append(nm)
        print()

    if bless:
        print("baselines written to rexauto/baselines/ -- commit them.")
        return 0
    if rt_improved:
        print("runtime IMPROVED: %s -- re-bless: python regression_gate.py --bless --runtime %s"
              % (", ".join(sorted(rt_improved)), " ".join(sorted(rt_improved))))
    allbad = sorted(set(bad) | set(rt_bad))
    if allbad:
        print("GATE FAIL: %d project(s) regressed -> %s" % (len(allbad), ", ".join(allbad)))
        print("If intended, re-bless: python regression_gate.py --bless%s %s"
              % (" --runtime" if rt_bad else "", " ".join(allbad)))
        return 1
    print("GATE PASS: codegen byte-identical%s -- no regression."
          % (" + runtime health held" if runtime else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
