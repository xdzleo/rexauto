#!/usr/bin/env python3
"""
rexauto — one shot: Xbox 360 content container -> a recompiled build that boots.

Drop in a container (or an extracted game folder) and it runs the whole pipeline
that otherwise takes a day of by-hand steps:

  1. extract      container -> game folder (default.xex + assets)
  2. init         scaffold a ReXGlue project
  3. jumptables   (if IDA present) recover bctr jump tables -> switch_tables.toml
  4. build+heal   codegen + build; auto-extend any function the recompiler split
                  mid-flow until the build is clean (boundary heal)
  5. run+heal     run; register every "invalid/unregistered function" the
                  dispatcher hits, rebuild, repeat until it stops hitting them
  6. run          launch it

What it does NOT do: fix game-specific GPU/emulation gaps (a title whose vertex
formats or kernel calls the runtime doesn't support yet will boot and run but may
not render or stay up). That is runtime work, not recompilation. rexauto gets you
to a booting, guest-code-executing build automatically; the rest is per title.

    python rexauto.py "<container-or-folder>" --name mygame [--run]

Re-run any time: each stage is skipped if already done. --from <stage> restarts
from a point, --only <stage> runs one. Tool paths come from their usual install
locations, PATH, or the env vars REXGLUE / REXSDK_DIR / IDAT / CLANG / CLANGXX /
VCVARS / JT_REPO.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import extract as _extract
import heal as _heal

STAGES = ["extract", "init", "jumptables", "build", "runheal", "run"]
MAX_BUILD_ATTEMPTS = 12


# --------------------------------------------------------------------------- env
def find_first(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


def newest_glob(*patterns):
    hits = []
    for pat in patterns:
        hits.extend(glob.glob(pat))
    return sorted(hits)[-1] if hits else None


def detect_env():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    e = os.environ.get
    # When shipped as a packaged release, ReXGlue and the jump-table scripts sit
    # next to the .exe under rexglue/ and xenon-jumptables/. Those take priority.
    app = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
    def near(*rel):
        return find_first([os.path.join(app, *r.split("/")) for r in rel])
    return {
        "vcvars": e("VCVARS") or newest_glob(
            os.path.join(pf86, "Microsoft Visual Studio", "*", "*", "VC", "Auxiliary",
                         "Build", "vcvars64.bat")),
        "clang": e("CLANG") or shutil.which("clang") or find_first(
            [os.path.join(pf, "LLVM", "bin", "clang.exe")]),
        "clangxx": e("CLANGXX") or shutil.which("clang++") or find_first(
            [os.path.join(pf, "LLVM", "bin", "clang++.exe")]),
        "idat": e("IDAT") or shutil.which("idat") or newest_glob(
            os.path.join(pf, "IDA*", "idat.exe"), os.path.join(pf, "IDA*", "idat64.exe")),
        "sdk": e("REXSDK_DIR") or near("rexglue/sdk")
        or find_first([r"C:\Skate3Recomp\rexglue-sdk\out\install\win-amd64"]),
        "rexglue": e("REXGLUE") or near("rexglue/tool/rexglue.exe") or shutil.which("rexglue")
        or newest_glob(r"C:\Skate3Recomp\rexglue-sdk\out\win-amd64\*\rexglue.exe"),
        "jt_repo": e("JT_REPO") or near("xenon-jumptables")
        or find_first([r"C:\xenon-jumptables"]),
        # a real python interpreter for the jump-table scripts (sys.executable is
        # the frozen .exe when packaged, which can't run .py files)
        "python": (None if getattr(sys, "frozen", False) else sys.executable)
        or e("PYTHON") or shutil.which("python") or shutil.which("python3")
        or newest_glob(r"C:\Program Files\Python3*\python.exe", r"C:\Python3*\python.exe"),
    }


# ------------------------------------------------------------------------- utils
class Ctx:
    def __init__(self, args, env):
        self.args = args
        self.env = env
        self.name = args.name
        self.work = os.path.join(args.work, args.name)
        os.makedirs(self.work, exist_ok=True)
        self.port = os.path.join(self.work, "port")
        self.manifest = os.path.join(self.port, "%s_manifest.toml" % self.name)
        self.functions = os.path.join(self.port, "%s_functions.toml" % self.name)
        self.switches = os.path.join(self.port, "%s_switch_tables.toml" % self.name)
        self.builddir = os.path.join(self.port, "out", "build", "win-amd64-release")
        self.exe = os.path.join(self.builddir, "%s.exe" % self.name)
        self.gen = os.path.join(self.port, "generated", "default")
        self.statefile = os.path.join(self.work, ".rexauto_state")
        self._game_out = os.path.join(self.work, "game")
        ex = self.load_state().get("extract") or {}
        self.game = ex.get("game_dir") or self._game_out
        self.xex = ex.get("xex")

    def log(self, msg):
        print("[rexauto] %s" % msg, flush=True)

    def load_state(self):
        try:
            return json.load(open(self.statefile)) if os.path.exists(self.statefile) else {}
        except Exception:
            return {}

    def mark(self, stage, data=None):
        st = self.load_state()
        st[stage] = data if data is not None else True
        json.dump(st, open(self.statefile, "w"), indent=1)


def run(cmd, **kw):
    return subprocess.run(cmd, **kw)


def rexglue(ctx, *xargs, env=None, capture=False):
    cmd = [ctx.env["rexglue"]] + list(xargs)
    e = dict(os.environ, **(env or {}))
    if capture:
        return subprocess.run(cmd, env=e, cwd=ctx.port, capture_output=True, text=True)
    return subprocess.run(cmd, env=e, cwd=ctx.port)


def add_includes(ctx, names):
    txt = open(ctx.manifest, encoding="utf-8", errors="ignore").read()
    m = re.search(r'includes\s*=\s*\[([^\]]*)\]', txt)
    cur = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    for n in names:
        if n not in cur:
            cur.append(n)
    newline = "includes = [%s]" % ", ".join('"%s"' % c for c in cur)
    if m:
        txt = txt[:m.start()] + newline + txt[m.end():]
    else:
        txt += "\n" + newline + "\n"
    open(ctx.manifest, "w", encoding="utf-8").write(txt)


# ------------------------------------------------------------------------ stages
def stage_extract(ctx):
    xex, game_dir = _extract.extract_container(ctx.args.container, ctx._game_out, log=ctx.log)
    ctx.game, ctx.xex = game_dir, xex
    ctx.mark("extract", {"xex": xex, "game_dir": game_dir})


def stage_init(ctx):
    if os.path.exists(ctx.manifest):
        ctx.log("project already initialised")
    else:
        xex = ctx.xex or os.path.join(ctx.game, "default.xex")
        if not os.path.exists(xex):
            raise SystemExit("default.xex not found (%s) — run the extract stage first" % xex)
        r = run([ctx.env["rexglue"], "init", "--project-name", ctx.name,
                 "--xex-path", xex, "--game-root", ctx.game, "--project-root", ctx.port])
        if r.returncode != 0 or not os.path.exists(ctx.manifest):
            raise SystemExit("rexglue init failed (rc=%s, no manifest at %s)"
                             % (getattr(r, "returncode", "?"), ctx.manifest))
    if not os.path.exists(ctx.functions):
        _heal.write_overrides(ctx.functions, {})
    add_includes(ctx, ["%s_functions.toml" % ctx.name])
    ctx.mark("init")


def _tail_idalog(ctx, idalog, stop):
    """Stream the IDA pass's [xjt] progress lines to the UI while it runs."""
    seen = 0
    while not stop.is_set():
        try:
            if os.path.exists(idalog):
                lines = open(idalog, errors="ignore").read().splitlines()
                for l in lines[seen:]:
                    if "[xjt]" in l:
                        msg = l.split("[xjt]", 1)[1].strip()
                        if msg.startswith("progress "):
                            msg = msg[9:]
                        if any(k in msg for k in ("defining", "scanning", "analyzing",
                                                  "round", "functions=")):
                            ctx.log("@jump tables: " + msg)
                seen = len(lines)
        except OSError:
            pass
        time.sleep(0.4)


def stage_jumptables(ctx):
    if not ctx.env["idat"]:
        ctx.log("IDA not found -> skipping jump-table recovery (built-in switch handling "
                "still applies; boundary/runtime heal still run)")
        return ctx.mark("jumptables", {"skipped": "no-ida"})
    if not ctx.env["jt_repo"]:
        ctx.log("xenon-jumptables repo not found -> skipping jump-table recovery")
        return ctx.mark("jumptables", {"skipped": "no-repo"})
    if not ctx.env["python"]:
        ctx.log("no python interpreter for the jump-table scripts -> skipping (set PYTHON)")
        return ctx.mark("jumptables", {"skipped": "no-python"})
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    ctx.log("dumping decompressed image + reading section ranges")
    try:
        blob = do_codegen(ctx, env={"REX_DUMP_IMAGE": image}, level="trace")
    except SystemExit as ex:
        ctx.log("codegen (for image dump) failed -> skipping jump tables (%s)" % ex)
        return ctx.mark("jumptables", {"skipped": "codegen-fail"})
    if not os.path.exists(image):
        ctx.log("image dump produced nothing (rexglue likely lacks the dump-image patch) "
                "-> skipping jump tables")
        return ctx.mark("jumptables", {"skipped": "no-dump"})
    bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
    base = int(bm.group(1), 16) if bm else 0x82000000
    image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
    secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
    exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                 for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
    if not exec_secs:
        ctx.log("WARNING: could not parse exec sections from the rexglue trace (log format may "
                "have changed) -> skipping jump tables")
        return ctx.mark("jumptables", {"skipped": "no-sections"})
    text_start, text_end = min(s for s, _ in exec_secs), max(e for _, e in exec_secs)
    if not (base <= text_start < text_end <= image_end):
        ctx.log("WARNING: parsed section range looks wrong (0x%X..0x%X in 0x%X..0x%X) -> skipping"
                % (text_start, text_end, base, image_end))
        return ctx.mark("jumptables", {"skipped": "bad-range"})
    funcs = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    rf = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
              ctx.gen, "-o", funcs])
    if rf.returncode != 0 or not os.path.exists(funcs):
        ctx.log("extract_funcs failed -> skipping jump tables")
        return ctx.mark("jumptables", {"skipped": "extract-funcs-fail"})
    cfg = os.path.join(ctx.work, "%s_jt.json" % ctx.name)
    out_json = os.path.join(ctx.work, "jumptables.json")
    json.dump({"image": image, "image_base": hex(base), "image_end": hex(image_end),
               "text_start": hex(text_start), "text_end": hex(text_end), "output": out_json,
               "functions": funcs, "format": "rexglue", "toml": ctx.switches}, open(cfg, "w"))
    idalog = out_json + ".idalog.txt"
    try:
        if os.path.exists(idalog):
            os.remove(idalog)
    except OSError:
        pass
    ctx.log("recovering jump tables (IDA)")
    stop = threading.Event()
    threading.Thread(target=_tail_idalog, args=(ctx, idalog, stop), daemon=True).start()
    rr = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "recover.py"),
              cfg, "--ida", ctx.env["idat"]])
    stop.set()
    if rr.returncode != 0 or not os.path.exists(ctx.switches):
        ctx.log("jump-table recovery failed -> continuing without it")
        return ctx.mark("jumptables", {"skipped": "recover-fail"})
    n = open(ctx.switches).read().count("[[switch_tables]]")
    add_includes(ctx, ["%s_switch_tables.toml" % ctx.name])
    ctx.log("jump tables recovered: %d" % n)
    ctx.mark("jumptables", {"tables": n})


def write_build_bat(ctx):
    bat = os.path.join(ctx.work, "_build.bat")
    sdk = ctx.env["sdk"].replace("\\", "/")
    lines = [
        "@echo off",
        'call "%s" >nul' % ctx.env["vcvars"],
        'cd /d "%s"' % ctx.port,
        ('cmake --preset win-amd64-release -DCMAKE_C_COMPILER="%s" '
         '-DCMAKE_CXX_COMPILER="%s" -DCMAKE_PREFIX_PATH="%s" '
         '-Drexglue_DIR="%s/lib/cmake/rexglue"'
         % (ctx.env["clang"].replace("\\", "/"), ctx.env["clangxx"].replace("\\", "/"), sdk, sdk)),
        "cmake --build out/build/win-amd64-release --parallel -- -k 0",
        "echo RC=%errorlevel%",
    ]
    open(bat, "w").write("\r\n".join(lines) + "\r\n")
    return bat


def _gen_snapshot(ctx):
    """Per generated file: md5 + mtime, plus the line set of headers (every TU
    depends on the shared init header, so we need to reason about its diff)."""
    snap = {}
    for p in glob.glob(os.path.join(ctx.gen, "*.cpp")) + glob.glob(os.path.join(ctx.gen, "*.h")):
        try:
            data = open(p, "rb").read()
            lines = set(data.decode("utf-8", "ignore").splitlines()) if p.endswith(".h") else None
            snap[p] = (hashlib.md5(data).digest(), os.path.getmtime(p), lines)
        except OSError:
            pass
    return snap


def _gen_restore_unchanged(ctx, snap):
    """Restore the mtime of regenerated files that didn't really change, so ninja
    skips recompiling them. The shared init header gains a DECLARE_REX_FUNC line
    on every heal registration — but a new extern declaration cannot change any
    already-compiled TU, so if the header's *only* diff is added DECLARE_REX_FUNC
    lines (nothing removed, no macro/table line touched) it is safe to keep its
    old timestamp. Any other change (a REX_IMAGE_* macro, a removal) falls through
    to a full recompile. Lossless either way."""
    units = headers = 0
    for p, (h, mt, oldlines) in snap.items():
        try:
            if not os.path.exists(p):
                continue
            data = open(p, "rb").read()
            if hashlib.md5(data).digest() == h:
                os.utime(p, (mt, mt))
                units += 1
            elif oldlines is not None:
                new = set(data.decode("utf-8", "ignore").splitlines())
                added, removed = new - oldlines, oldlines - new
                if added and not removed and all("DECLARE_REX_FUNC" in l for l in added):
                    os.utime(p, (mt, mt))
                    headers += 1
        except OSError:
            pass
    if units or headers:
        ctx.log("  incremental rebuild: reused %d unit(s)%s"
                % (units, " + %d header(s)" % headers if headers else ""))


def do_codegen(ctx, env=None, level="error"):
    """Run codegen, auto-registering unresolved tail-call targets (codegen's
    Validate phase reports them) until it passes. Returns the captured output
    (at trace level it carries the section ranges the jumptables stage needs)."""
    snap = _gen_snapshot(ctx)
    for _ in range(10):
        r = rexglue(ctx, "--log-level", level, "codegen", ctx.manifest, env=env, capture=True)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            _gen_restore_unchanged(ctx, snap)
            return out
        targets = _heal.unresolved_calls_from_text(out)
        if not targets:
            tail = "\n".join(out.splitlines()[-15:])
            raise SystemExit("[rexauto] codegen FAILED (rc=%d) — aborting\n%s" % (r.returncode, tail))
        n = _heal.register_functions(targets, ctx.functions)
        ctx.log("  codegen: %d unresolved tail-call target(s) -> registered %d; retrying"
                % (len(targets), n))
        if n == 0:
            raise SystemExit("[rexauto] codegen stuck on unresolved calls: %s"
                             % ", ".join("0x%X" % t for t in targets))
    raise SystemExit("[rexauto] codegen unresolved-call heal did not converge")


def do_build(ctx, bat):
    """Stream the build so ninja's [N/M] progress reaches the UI live."""
    logp = os.path.join(ctx.work, "_build.log")
    p = subprocess.Popen(["cmd", "/c", bat], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, bufsize=1)
    last = 0.0
    with open(logp, "w") as lf:
        for line in p.stdout:
            lf.write(line)
            m = re.search(r"\[(\d+)/(\d+)\]", line)
            if m:
                n, tot = int(m.group(1)), int(m.group(2))
                now = time.time()
                if now - last > 0.3 or n == tot:
                    last = now
                    name = line.strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:42]
                    ctx.log("@build %d/%d %s" % (n, tot, name))
    return logp, p.wait()


def stage_build(ctx):
    miss = [k for k in ("vcvars", "clang", "clangxx", "sdk") if not ctx.env[k]]
    if miss:
        raise SystemExit("missing build tools: %s (set via env vars or install)" % ", ".join(miss))
    bat = write_build_bat(ctx)
    last_ends = None
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        ctx.log("codegen + build (attempt %d/%d)" % (attempt, MAX_BUILD_ATTEMPTS))
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        txt = _heal._read_text(logp)
        if rc == 0 and os.path.exists(ctx.exe):
            ctx.log("build OK -> %s" % ctx.exe)
            return ctx.mark("build", {"exe": ctx.exe})
        if "use of undeclared label" in txt:
            n = _heal.heal_boundaries(logp, ctx.gen, ctx.functions)
            ends = tuple(sorted((a, e) for a, e in _heal.load_overrides(ctx.functions).items() if e))
            if n == 0 or ends == last_ends:
                ctx.log("  boundary heal not converging (no new fix) -> see %s" % logp)
                break
            last_ends = ends
            ctx.log("  boundary split -> +%d function-end extension(s); rebuilding" % n)
            continue
        imports = sorted(set(re.findall(r"undefined symbol:[^\n]*?_([A-Za-z]\w+)", txt)))
        if imports:
            ctx.log("  LINK ERROR: unresolved kernel import(s): %s" % ", ".join(imports[:12]))
            ctx.log("  these need runtime support — implement/enable them in the ReXGlue SDK "
                    "(e.g. uncomment the relevant src/kernel/*.cpp and rebuild the SDK).")
        else:
            ctx.log("  build failed (rc=%d) with no auto-fixable cause -> see %s" % (rc, logp))
        break
    raise SystemExit("build did not converge; see %s" % os.path.join(ctx.work, "_build.log"))


def run_once(ctx, seconds):
    """Launch the game, let it run, kill it; return (newest-this-launch log text, alive)."""
    logdir = os.path.join(ctx.builddir, "logs")
    before = set(glob.glob(os.path.join(logdir, "*.log")))
    t0 = time.time()
    try:
        p = subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir)
    except OSError as ex:
        ctx.log("  could not launch the game: %s" % ex)
        return "", False
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
    if not new:
        ctx.log("  (this launch produced no log of its own)")
        return "", alive
    return _heal._read_text(max(new, key=os.path.getmtime)), alive


def stage_runheal(ctx):
    bat = write_build_bat(ctx)
    for it in range(1, ctx.args.heal_iters + 1):
        txt, alive = run_once(ctx, ctx.args.run_seconds)
        addrs = _heal.invalid_functions_from_text(txt)
        if not addrs:
            verdict = ("ran %ds with no invalid-function fatal" % ctx.args.run_seconds) if alive \
                else "exited without an invalid-function fatal (other stop - likely GPU/runtime)"
            ctx.log("run-heal converged: %s" % verdict)
            return ctx.mark("runheal", {"iters": it, "alive": alive})
        n = _heal.register_functions(addrs, ctx.functions)
        ctx.log("iter %d: fatal @ %s -> +%d registered; rebuilding"
                % (it, ",".join("0x%X" % a for a in addrs), n))
        if n == 0:
            ctx.log("  stuck on 0x%X (already registered) — needs a closer look" % addrs[0])
            return ctx.mark("runheal", {"stuck": "0x%X" % addrs[0]})
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        if rc != 0 or not os.path.exists(ctx.exe):
            if "use of undeclared label" in _heal._read_text(logp):
                _heal.heal_boundaries(logp, ctx.gen, ctx.functions)
                do_codegen(ctx)
                do_build(ctx, bat)
            else:
                ctx.log("  rebuild failed after registering 0x%X -> see %s" % (addrs[0], logp))
                return ctx.mark("runheal", {"build_failed": True})
    ctx.log("run-heal hit max iterations (%d)" % ctx.args.heal_iters)
    ctx.mark("runheal", {"iters": ctx.args.heal_iters})


def stage_run(ctx):
    ctx.log("launching %s" % ctx.exe)
    subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir)
    ctx.log("running. a game window should open. (GPU/playability is per-title and not "
            "auto-solved by rexauto.)")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("container", help="STFS package, or a folder containing default.xex")
    ap.add_argument("--name", required=True, help="project name (a-z0-9_)")
    ap.add_argument("--work", default=os.environ.get("REXAUTO_WORK", r"C:\Skate3Recomp\autoports"),
                    help="output root (or env REXAUTO_WORK)")
    ap.add_argument("--run", action="store_true", help="launch the game at the end")
    ap.add_argument("--from", dest="from_stage", choices=STAGES, help="restart from this stage")
    ap.add_argument("--only", choices=STAGES, help="run just this stage")
    ap.add_argument("--no-jumptables", action="store_true")
    ap.add_argument("--heal-iters", type=int, default=20)
    ap.add_argument("--run-seconds", type=int, default=22)
    args = ap.parse_args()

    env = detect_env()
    ctx = Ctx(args, env)
    ctx.log("tools: rexglue=%s sdk=%s clang=%s ida=%s vcvars=%s"
            % (bool(env["rexglue"]), bool(env["sdk"]), bool(env["clang"]),
               bool(env["idat"]), bool(env["vcvars"])))
    if not env["rexglue"]:
        raise SystemExit("rexglue.exe not found (set REXGLUE or build the ReXGlue SDK).")

    order = STAGES[:]
    if args.no_jumptables:
        order.remove("jumptables")
    want_run = args.run or args.from_stage == "run" or args.only == "run"
    if not want_run:
        order.remove("run")
    if args.from_stage and args.from_stage not in order:
        raise SystemExit("--from %s: that stage is disabled by the current flags" % args.from_stage)
    if args.only and args.only not in STAGES:
        raise SystemExit("--only %s: unknown stage" % args.only)

    state = ctx.load_state()
    fns = {"extract": stage_extract, "init": stage_init, "jumptables": stage_jumptables,
           "build": stage_build, "runheal": stage_runheal, "run": stage_run}
    start = order.index(args.from_stage) if args.from_stage else 0
    selected = [args.only] if args.only else order[start:]

    for stage in selected:
        if not args.only and not args.from_stage and state.get(stage):
            ctx.log("skip %s (done)" % stage)
            continue
        ctx.log("=== stage: %s ===" % stage)
        fns[stage](ctx)
    ctx.log("done. project: %s" % ctx.port)
    if not want_run and os.path.exists(ctx.exe):
        ctx.log('to play:  "%s" --game_data_root="%s"' % (ctx.exe, ctx.game))


if __name__ == "__main__":
    main()
