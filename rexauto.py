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
import jt_landings as _jt
import codegen_patches as _cgp

STAGES = ["extract", "init", "setjmp", "jumptables", "build", "runheal", "run"]
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
        # auto-title-update: the staged .xexp delta (None for a base-only game, so no
        # behaviour change). codegen+runtime auto-apply it in memory; gabarito_key
        # folds it in so a TU build keeps its own cure set.
        self.tu_xexp = ex.get("tu_xexp")

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
    verify_sdk_pin(ctx.env)  # gate SDK use (codegen/init); a pure game run never reaches this
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
    info = {"xex": xex, "game_dir": game_dir}
    # Generic auto-title-update. detect_title_update stages a matching XEX delta
    # (default.xexp) beside the base default.xex in the game dir. rexglue's loader
    # auto-applies a co-located "<base>+p" delta IN MEMORY -- gated by cvar
    # xex_apply_patches (default on) -- at BOTH codegen (before the analysis
    # snapshot) and runtime, so we recompile AND run the exact patched version the
    # user has, with no separate patch step and no SDK change. ctx.xex stays the
    # base xex; gabarito_key folds the .xexp in so the TU build keeps its own cure
    # set. Strictly additive: no TU -> ctx.xex is the base xex and codegen input is
    # byte-identical to before (regression-gate proven).
    if not getattr(ctx.args, "no_title_update", False):
        tu_xexp = _extract.detect_title_update(game_dir, ctx.args.container, xex, log=ctx.log)
        if tu_xexp:
            ctx.tu_xexp = tu_xexp
            info["tu_xexp"] = tu_xexp
            ctx.log("title-update staged (%s) -- codegen + runtime auto-apply it in memory"
                    % os.path.basename(tu_xexp))
    ctx.mark("extract", info)


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


def stage_setjmp(ctx):
    """Detect the statically-linked CRT setjmp/longjmp routines and record their
    guest addresses in the manifest, so codegen emits ppc_setjmp/ppc_longjmp at
    those call sites.

    Xbox 360 C++ exception handling is linked straight into the title. longjmp is
    a *non-local* jump (mass-restore of GPR/FPR/VMX + the stack pointer from a
    jmp_buf, then blr). The recompiler turns blr into a plain C++ `return`, so
    without these addresses set, a guest longjmp returns to its immediate caller,
    the caller skips its epilogue, a non-volatile register is left corrupted and
    the title crashes at startup (a near-null write). Detecting and configuring
    them is what lets exception-using titles boot. Titles that don't use C++
    exceptions have no signature and are left untouched."""
    try:
        import detect_setjmp as _dj
    except Exception as ex:
        ctx.log("detect_setjmp unavailable -> skipping setjmp/longjmp detection (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "no-module"})
    image = os.path.join(ctx.work, "%s_image.bin" % ctx.name)
    # Force a FRESH dump. The guard we must scan is the one codegen actually
    # recompiles, and when a title update is staged (ctx.tu_xexp) rexglue's loader
    # auto-applies the co-located default.xexp delta IN MEMORY at codegen time
    # (cvar xex_apply_patches; user_module.cpp ApplyPatch) -- so the image codegen
    # dumps here is the PATCHED (TU) image, whose setjmp/longjmp guard differs from
    # the base. A stale skate3_image.bin left over from an EARLIER run that predates
    # the .xexp staging would be the un-patched BASE image; scanning it writes the
    # retail guard address, which doesn't even exist in the TU generated set, so
    # ppc_setjmp lands at a no-op site and the title needs hand-fixing. Delete any
    # leftover first so a pre-TU image can never be reused, and so the no-dump guard
    # below can't silently pass on a stale file when codegen fails to re-dump.
    # NO-OP for non-TU titles: ctx.tu_xexp is None -> codegen loads only the base
    # xex -> the re-dumped image is byte-identical to before, and codegen OUTPUT is
    # untouched (this only deletes/rewrites a throwaway analysis dump).
    try:
        if os.path.exists(image):
            os.remove(image)
    except OSError as ex:
        ctx.log("could not remove stale image dump %s (%s) -- continuing; "
                "codegen truncates+overwrites it anyway" % (image, ex))
    tu = getattr(ctx, "tu_xexp", None)
    ctx.log("scanning %s image for setjmp/longjmp (C++ exception support)"
            % ("PATCHED (title-update) " if tu else ""))
    try:
        blob = do_codegen(ctx, env={"REX_DUMP_IMAGE": image}, level="trace")
    except SystemExit as ex:
        ctx.log("codegen for image dump failed -> skipping setjmp detection (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "codegen-fail"})
    if not os.path.exists(image):
        ctx.log("image dump produced nothing (rexglue lacks the dump-image patch) "
                "-> skipping setjmp detection")
        return ctx.mark("setjmp", {"skipped": "no-dump"})
    bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
    base = int(bm.group(1), 16) if bm else 0x82000000
    secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
    exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                 for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
    if not exec_secs:
        ctx.log("could not parse exec sections -> skipping setjmp detection")
        return ctx.mark("setjmp", {"skipped": "no-sections"})
    try:
        res = _dj.detect(image, exec_secs, base)
    except Exception as ex:
        ctx.log("setjmp detection error -> skipping (%s)" % ex)
        return ctx.mark("setjmp", {"skipped": "detect-error"})
    lj, sj = res.get("longjmp_address"), res.get("setjmp_address")
    if lj is None:
        ctx.log("no setjmp/longjmp signature found (title likely uses no C++ exceptions) -> ok")
        return ctx.mark("setjmp", {"found": False})
    if sj is None:
        ctx.log("longjmp 0x%X found but setjmp ambiguous (%s) -> need both; skipping write" % (lj, res))
        return ctx.mark("setjmp", {"longjmp": "0x%X" % lj, "setjmp": "ambiguous"})
    _dj.write_addresses(ctx.manifest, longjmp=lj, setjmp=sj)
    ctx.log("setjmp/longjmp detected on %s image -> setjmp=0x%X longjmp=0x%X (written to manifest)"
            % ("PATCHED" if tu else "base", sj, lj))
    ctx.mark("setjmp", {"setjmp": "0x%X" % sj, "longjmp": "0x%X" % lj,
                        "image": "patched" if tu else "base"})


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
    # RelWithDebInfo by default: same optimization as Release but with symbols +
    # line info, so a crash in the recompiled code points straight at the generated
    # sub_XXXX + line -- the heal/gate debug loop's biggest pain. Codegen is
    # unaffected (the build type never changes generated/), so it's zero-regression
    # for the codegen gate. Set REXAUTO_BUILD_TYPE=Release for a stripped, smaller
    # distribution build.
    build_type = os.environ.get("REXAUTO_BUILD_TYPE", "RelWithDebInfo")
    lines = [
        "@echo off",
        'call "%s" >nul' % ctx.env["vcvars"],
        'cd /d "%s"' % ctx.port,
        ('cmake --preset win-amd64-release -DCMAKE_BUILD_TYPE=%s '
         # map imported libs (spdlog/fmt) to their Release variant under
         # RelWithDebInfo, else CMake links spdlogd.lib (_ITERATOR_DEBUG_LEVEL=2)
         # against our IDL=0 objects -> lld-link /failifmismatch. Harmless for Release.
         '-DCMAKE_MAP_IMPORTED_CONFIG_RELWITHDEBINFO=Release -DCMAKE_C_COMPILER="%s" '
         '-DCMAKE_CXX_COMPILER="%s" -DCMAKE_PREFIX_PATH="%s" '
         '-Drexglue_DIR="%s/lib/cmake/rexglue"'
         % (build_type, ctx.env["clang"].replace("\\", "/"), ctx.env["clangxx"].replace("\\", "/"), sdk, sdk)),
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
            # heal unregistered bctr switch-on-ctr landings the SDK left as an
            # indirect dispatch (would FATAL at runtime); a re-codegen converges
            # them. No-op (returns 0) for a title whose switches all resolve.
            if _jt.heal(ctx, log=ctx.log):
                continue
            # splice any declarative post-codegen source patches (e.g. the skate3
            # FOV / ultrawide-frustum render hooks) once codegen has converged and
            # before compile. No <name>_codegen_patches.toml -> no-op (byte-identical).
            _cgp.apply(ctx, log=ctx.log)
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


def write_game_root(ctx):
    """Drop a 'game_root.txt' sidecar next to the exe naming the game data dir, so
    double-clicking the exe (no --game_data_root) still launches the title -- the
    runtime reads this when the flag is absent."""
    try:
        if ctx.game and os.path.isdir(ctx.game):
            with open(os.path.join(ctx.builddir, "game_root.txt"), "w", encoding="utf-8") as f:
                f.write(os.path.abspath(ctx.game) + "\n")
    except OSError as ex:
        ctx.log("could not write game_root.txt sidecar (%s)" % ex)


def _game_icon_png(ctx):
    """Best-effort PNG bytes to use as the exe icon: the package cover (STFS),
    else the title Thumbnail.png from the extracted game. None if neither."""
    try:
        meta = _extract.read_package_meta(getattr(ctx.args, "container", "") or "")
        if meta.get("cover"):
            return meta["cover"]
    except Exception:
        pass
    if ctx.game:
        thumb = os.path.join(ctx.game, "Thumbnail.png")
        if os.path.exists(thumb):
            try:
                return open(thumb, "rb").read()
            except OSError:
                pass
    return None


def _inject_icon_into_cmake(ctx):
    """Wire src/<name>.rc into the port build (before add_executable), idempotently."""
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "rexauto-game-icon" in txt:
        return
    m = re.search(r"add_executable\(\s*%s\s+WIN32\s+\$\{(\w+)\}\s*\)" % re.escape(ctx.name), txt)
    if not m:
        return
    srcvar = m.group(1)
    block = ('    # rexauto-game-icon: use the game icon for the exe if present\n'
             '    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/%s.rc")\n'
             '        enable_language(RC)\n'
             '        list(APPEND %s src/%s.rc)\n'
             '    endif()\n' % (ctx.name, srcvar, ctx.name))
    open(cml, "w", encoding="utf-8").write(txt[:m.start()] + block + txt[m.start():])


def write_game_icon(ctx):
    """Give the recompiled exe the game's icon: build src/<name>.ico from the
    package cover or the title Thumbnail.png, emit a .rc, and wire it into the
    port build. No-op (keeps the default icon) when no game image is available."""
    png = _game_icon_png(ctx)
    if not png:
        return
    try:
        import io
        from PIL import Image
    except Exception:
        return
    try:
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        w, h = im.size
        s = max(w, h, 16)
        canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        canvas.paste(im, ((s - w) // 2, (s - h) // 2))
        srcdir = os.path.join(ctx.port, "src")
        os.makedirs(srcdir, exist_ok=True)
        sizes = [(n, n) for n in (16, 32, 48, 64, 128, 256) if n <= s] or [(s, s)]
        canvas.save(os.path.join(srcdir, ctx.name + ".ico"), sizes=sizes)
        with open(os.path.join(srcdir, ctx.name + ".rc"), "w", encoding="utf-8") as f:
            f.write('1 ICON "%s.ico"\n' % ctx.name)
        _inject_icon_into_cmake(ctx)
        ctx.log("game icon embedded in the exe")
    except Exception as ex:
        ctx.log("could not generate game icon (%s)" % ex)


# ----------------------------------------------------- extra (multi-binary) modules
# Some titles ship a 2nd recompilable guest module (e.g. Skate 3's EAWebkit.xex at
# guest 0x88xxxxxx) that the entrypoint calls into. The fork SDK already supports it
# (per-manifest out_directory_path + symbol_prefix, one dispatcher spanning both
# ranges); rexauto just orchestrates a 2nd codegen and wires its sources + host
# registration in. Everything below is a NO-OP for single-module titles
# (extra_modules() is empty), so those builds are byte-identical.
def extra_modules(ctx):
    """[{key, name, xex, symbol_prefix}] of recompilable modules beyond the entrypoint.
    Opt-in by data on disk: an optional port/<name>_modules.toml, else a narrow built-in
    for the known skate3/EAWebkit case (only fires when that exact 2nd module is present)."""
    cfg = os.path.join(ctx.port, "%s_modules.toml" % ctx.name)
    if os.path.exists(cfg):
        mods, txt = [], open(cfg, encoding="utf-8", errors="ignore").read()
        for blk in re.split(r'\[\[\s*modules\s*\]\]', txt)[1:]:
            g = lambda k: (re.search(k + r'\s*=\s*"([^"]*)"', blk) or [None, None])[1]
            key, xex = g("key"), g("xex")
            if key and xex:
                mods.append({"key": key, "name": g("name") or key,
                             "xex": os.path.join(ctx.game, xex.replace("/", os.sep)),
                             "symbol_prefix": g("symbol_prefix") or (key + "_")})
        return mods
    ewk = os.path.join(ctx.game, "data", "webkit", "EAWebkit.xex")
    if os.path.exists(ewk):
        return [{"key": "eawebkit", "name": "eawebkit", "xex": ewk, "symbol_prefix": "eawebkit_"}]
    return []


# ----------------------------------------------------- per-title app-glue (factory)
# Beyond the 2nd-module dispatch above, some titles need a little host glue wired
# into the generated app's OnPostSetup(): a signed-in user identity, content-scheme
# symbolic links (e.g. big:/dlcbig:), and a BIG-directory probe overlay. This is the
# mechanical, per-title-but-data-driven layer: the MECHANISM is generic (SDK
# RegisterSymbolicLink / HostPathDevice / SetIdentity) but the values are per-title,
# so they live in an opt-in port/<name>_appglue.toml consumed here. Everything below
# is a strict NO-OP when that file is absent: glue_records() returns {} and the
# injector is never called, so app.h stays byte-identical for every existing title.
def glue_records(ctx):
    """Parse the optional port/<name>_appglue.toml into a dict, or {} if absent.

    Mirrors extra_modules()' lightweight regex/toml parsing (same style as the
    <name>_modules.toml reader). Recognized sections:
      [identity]            xuid, name
      [[alias]]             scheme, target           (one per entry)
      [overlay]             enabled, scan_root, scan_prefix, device_scheme,
                            overlay_subdir, fixed_dirs[], [[overlay.link]] guest/target
      [dlc]                 auto_install, root
      [title_update]        container, url, [[title_update.payload]] src/dest/sha256/size
    Returns {} when the file does not exist."""
    cfg = os.path.join(ctx.port, "%s_appglue.toml" % ctx.name)
    if not os.path.exists(cfg):
        return {}
    txt = open(cfg, encoding="utf-8", errors="ignore").read()

    def _strip_comments(s):
        # drop full-line and trailing '#' comments (none of our values contain '#')
        out = []
        for ln in s.splitlines():
            h = ln.find("#")
            out.append(ln if h < 0 else ln[:h])
        return "\n".join(out)

    txt = _strip_comments(txt)

    def _sect(name):
        # body of a single [name] table up to the next top-level/array header
        m = re.search(r'(?m)^\s*\[%s\]\s*$' % re.escape(name), txt)
        if not m:
            return None
        rest = txt[m.end():]
        nxt = re.search(r'(?m)^\s*\[', rest)
        return rest[:nxt.start()] if nxt else rest

    def _arrays(name):
        # bodies of every [[name]] array-of-tables entry
        out = []
        for m in re.finditer(r'(?m)^\s*\[\[\s*%s\s*\]\]\s*$' % re.escape(name), txt):
            rest = txt[m.end():]
            nxt = re.search(r'(?m)^\s*\[', rest)
            out.append(rest[:nxt.start()] if nxt else rest)
        return out

    def _unescape(raw):
        # decode the TOML basic-string escapes we care about so the dict holds the
        # true string (e.g. "\\Device" -> "\Device"); _cpp_str re-escapes for C++.
        return re.sub(r'\\(.)', lambda mo: {
            "n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(mo.group(1),
            mo.group(1)), raw)

    def _s(blk, k):
        m = re.search(k + r'\s*=\s*"((?:[^"\\]|\\.)*)"', blk)
        return _unescape(m.group(1)) if m else None

    def _b(blk, k):
        m = re.search(k + r'\s*=\s*(true|false)', blk)
        return (m.group(1) == "true") if m else None

    def _i(blk, k):
        m = re.search(k + r'\s*=\s*(-?\d+)', blk)
        return int(m.group(1)) if m else None

    def _list(blk, k):
        m = re.search(k + r'\s*=\s*\[(.*?)\]', blk, re.S)
        if not m:
            return []
        return [_unescape(v.group(1)) for v in re.finditer(r'"((?:[^"\\]|\\.)*)"', m.group(1))]

    glue = {}

    ident = _sect("identity")
    if ident is not None:
        xuid = _s(ident, "xuid")
        if xuid:
            glue["identity"] = {"xuid": xuid, "name": _s(ident, "name") or "Player"}

    aliases = []
    for blk in _arrays("alias"):
        scheme, target = _s(blk, "scheme"), _s(blk, "target")
        if scheme and target:
            aliases.append({"scheme": scheme, "target": target})
    if aliases:
        glue["aliases"] = aliases

    ov = _sect("overlay")
    if ov is not None and _b(ov, "enabled"):
        links = []
        for blk in _arrays("overlay.link"):
            guest, target = _s(blk, "guest"), _s(blk, "target")
            if guest and target:
                links.append({"guest": guest, "target": target})
        glue["overlay"] = {
            "scan_root": _s(ov, "scan_root"),
            "scan_prefix": _s(ov, "scan_prefix"),
            "device_scheme": _s(ov, "device_scheme"),
            "overlay_subdir": _s(ov, "overlay_subdir"),
            "fixed_dirs": _list(ov, "fixed_dirs"),
            "links": links,
        }

    dlc = _sect("dlc")
    if dlc is not None and _b(dlc, "auto_install"):
        glue["dlc"] = {"auto_install": True, "root": _s(dlc, "root") or "dlc"}

    tu = _sect("title_update")
    if tu is not None:
        payloads = []
        for blk in _arrays("title_update.payload"):
            src, dest = _s(blk, "src"), _s(blk, "dest")
            if src and dest:
                payloads.append({"src": src, "dest": dest,
                                 "sha256": _s(blk, "sha256") or "",
                                 "size": _i(blk, "size") or 0})
        container, url = _s(tu, "container"), _s(tu, "url")
        if payloads or container or url:
            glue["title_update"] = {"container": container or "", "url": url or "",
                                    "payloads": payloads}

    return glue


def _author_module_manifest(ctx, m):
    sdkv = "0.8.0.0"
    try:
        mm = re.search(r'sdk_version\s*=\s*"([^"]*)"', open(ctx.manifest).read())
        sdkv = mm.group(1) if mm else sdkv
    except OSError:
        pass
    rel = os.path.relpath(m["xex"], ctx.port).replace("\\", "/")
    open(os.path.join(ctx.port, "%s_manifest.toml" % m["key"]), "w", encoding="utf-8").write(
        '# %s -- extra recompiled module of %s, authored by rexauto.\n'
        '[project]\nname = "%s"\nsdk_version = "%s"\ngame_root = "../game"\n\n'
        '[entrypoint]\nfile_path = "%s"\nout_directory_path = "generated/%s"\n'
        'includes = ["%s_functions.toml"]\nsymbol_prefix = "%s"\n'
        % (m["key"], ctx.name, m["name"], sdkv, rel, m["key"], m["key"], m["symbol_prefix"]))


def _seed_module_functions(ctx, m):
    fns = os.path.join(ctx.port, "%s_functions.toml" % m["key"])
    if _heal.load_overrides(fns):
        return
    seed = os.path.join(HERE, "seeds", "%s_functions.toml" % m["key"])
    if os.path.exists(seed):
        shutil.copyfile(seed, fns)
        ctx.log("  module '%s': seeded cures from rexauto/seeds" % m["key"])
    else:
        _heal.write_overrides(fns, {})


def _inject_extra_modules_into_cmake(ctx, mods):
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "_rexauto_mod" in txt:   # already injected (manually or by a prior run)
        return
    keys = " ".join(m["key"] for m in mods)
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-extra-modules: extra recompiled modules linked into the same exe.\n"
        "foreach(_rexauto_mod %s)\n"
        "    if(EXISTS \"${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}/sources.cmake\")\n"
        "        set(_rexauto_saved \"${GENERATED_SOURCES}\")\n"
        "        include(generated/${_rexauto_mod}/sources.cmake)\n"
        "        target_sources(%s PRIVATE ${GENERATED_SOURCES})\n"
        "        target_include_directories(%s PRIVATE\n"
        "            \"${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}\")\n"
        "        set(GENERATED_SOURCES \"${_rexauto_saved}\")\n"
        "        unset(_rexauto_saved)\n"
        "    endif()\nendforeach()\n" % (keys, ctx.name, ctx.name))
    ctx.log("  wired %d extra module(s) into CMakeLists" % len(mods))


def _cpp_str(s):
    """Escape a Python str (already toml-decoded, so it holds literal backslashes)
    for embedding in a C++ double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _appglue_body(ctx, glue):
    """Render the per-section OnPostSetup lines for the appglue.toml sections that
    are present. Returns ('', set()) when glue is empty so nothing is appended and
    no includes are added. Each section emits nothing when absent."""
    lines, includes = [], set()

    ident = glue.get("identity")
    if ident:
        includes.add("rex/system/xam/user_profile.h")
        lines += [
            "    // rexauto: appglue identity -- sign in a stub user so per-user vtable",
            "    // slots are constructed (else a worker thread calls through guest 0x0).",
            "    if (auto* _kernel = runtime()->kernel_state())",
            "      if (auto* _profile = _kernel->user_profile())",
            '        _profile->SetIdentity(%sULL, "%s");'
            % (ident["xuid"], _cpp_str(ident["name"])),
        ]

    aliases = glue.get("aliases")
    if aliases:
        includes.add("rex/filesystem/vfs.h")
        lines.append("    // rexauto: appglue aliases -- content-scheme symbolic links.")
        lines.append("    if (auto* _fs = runtime()->file_system()) {")
        for a in aliases:
            lines.append('      _fs->RegisterSymbolicLink("%s", "%s");'
                         % (_cpp_str(a["scheme"]), _cpp_str(a["target"])))
        lines.append("    }")

    ov = glue.get("overlay")
    if ov:
        includes.add("rex/filesystem/vfs.h")
        includes.add("rex/filesystem/host_path_device.h")
        scheme = ov.get("device_scheme") or "overlay:"
        subdir = ov.get("overlay_subdir") or "vfs_overlay"
        dirs = list(ov.get("fixed_dirs") or [])
        lines += [
            "    // rexauto: appglue overlay -- pre-create the BIG-directory probe",
            "    // dirs the title checks for existence, then mount them as a host",
            "    // device and fan out the guest probe paths to it.",
            "    {",
            '      auto _overlay_root = cache_root() / "%s";' % _cpp_str(subdir),
            "      std::error_code _ec;",
        ]
        for d in dirs:
            lines.append('      std::filesystem::create_directories(_overlay_root / "%s", _ec);'
                         % _cpp_str(d))
        lines += [
            "      auto _overlay_dev ="
            ' std::make_unique<rex::filesystem::HostPathDevice>("%s", _overlay_root);'
            % _cpp_str(scheme),
            "      _overlay_dev->Initialize();",
            "      if (auto* _fs = runtime()->file_system()) {",
            "        _fs->RegisterDevice(std::move(_overlay_dev));",
        ]
        for ln in (ov.get("links") or []):
            lines.append('        _fs->RegisterSymbolicLink("%s", "%s");'
                         % (_cpp_str(ln["guest"]), _cpp_str(ln["target"])))
        lines += ["      }", "    }"]
        if dirs or ov.get("links"):
            includes.add("<filesystem>")

    dlc = glue.get("dlc")
    if dlc:
        lines += [
            "    // rexauto: appglue dlc -- marketplace DLC auto-install. TODO: wire to",
            "    // an SDK InstallMarketplaceDlc(root) helper once it exists; root=%r."
            % dlc.get("root"),
        ]

    tu = glue.get("title_update")
    if tu:
        lines += [
            "    // rexauto: appglue title_update -- TODO: stage TU payloads via an SDK",
            "    // StageTitleUpdate(container, url, payloads) helper once it exists",
            "    // (%d payload(s); per-title manifest from %s_appglue.toml)."
            % (len(tu.get("payloads") or []), ctx.name),
        ]

    if not lines:
        return "", set()
    body = ("\n    // ---- rexauto: appglue (per-title host glue from %s_appglue.toml) ----\n"
            % ctx.name) + "\n".join(lines) + "\n"
    return body, includes


def _inject_app_glue(ctx, mods, glue):
    """Patch src/<name>_app.h: keep the existing extra-module extern/dispatcher
    block verbatim, and append the per-title appglue sections into the SAME
    generated OnPostSetup() body. Idempotent and a strict no-op when both mods and
    glue are empty (caller guards that, but this stays safe regardless)."""
    app = os.path.join(ctx.port, "src", "%s_app.h" % ctx.name)
    if not os.path.exists(app):
        return
    txt = open(app, encoding="utf-8", errors="ignore").read()
    has_mods = "rexauto: 2nd-module" in txt
    has_glue = "rexauto: appglue" in txt
    if (has_mods or not mods) and (has_glue or not glue):
        return  # nothing new to inject

    glue_body, glue_includes = _appglue_body(ctx, glue) if glue else ("", set())
    inc = "#include <rex/rex_app.h>"

    # ---- includes after the rex_app.h line (only for what's actually emitted) ----
    if not has_mods and mods:
        externs = "\n".join('extern const rex::PPCImageInfo %sPPCImageConfig;' % m["symbol_prefix"]
                            for m in mods)
        txt = txt.replace(inc,
            inc + "\n#include <rex/system/function_dispatcher.h>  // rexauto: 2nd-module\n\n"
            "// rexauto: extra recompiled module(s) linked into this exe.\n" + externs, 1)
    if not has_glue and glue_includes:
        addl = "".join(
            ("\n#include %s  // rexauto: appglue" % h) if h.startswith("<")
            else ("\n#include <%s>  // rexauto: appglue" % h)
            for h in sorted(glue_includes))
        txt = txt.replace(inc, inc + addl, 1)

    # ---- the OnPostSetup() body: dispatcher block (if any) + appglue block ----
    if has_mods:
        # extra-module hook already present; append glue inside the same body, right
        # before its closing brace (the dispatcher loop's "}\n  }\n").
        if glue_body:
            anchor = "    }\n  }\n"      # end of the dispatcher for-loop + method
            pos = txt.rfind(anchor)
            if pos < 0:                  # hand-folded body; fall back to method end
                pos = txt.rfind("  }\n")
                insert_at = pos
            else:
                insert_at = pos + len("    }\n")
            txt = txt[:insert_at] + glue_body + txt[insert_at:]
    else:
        cfgs = ", ".join('&%sPPCImageConfig' % m["symbol_prefix"] for m in mods)
        hook_open = (
            "\n  // rexauto: register the extra module function tables once the\n"
            "  // entrypoint's exists, so guest calls into them resolve.\n"
            "  void OnPostSetup() override {\n")
        hook_dispatch = (
            "    auto* dispatcher = runtime()->function_dispatcher();\n"
            "    if (!dispatcher) return;\n"
            "    for (const rex::PPCImageInfo* _cfg : { %s }) {\n"
            "      if (!_cfg->func_mappings) continue;\n"
            "      if (!dispatcher->InitializeFunctionTable(_cfg->code_base, _cfg->code_size,\n"
            "                                               _cfg->image_base, _cfg->image_size))\n"
            "        continue;\n"
            "      for (int i = 0; _cfg->func_mappings[i].guest != 0; ++i)\n"
            "        if (_cfg->func_mappings[i].host)\n"
            "          dispatcher->SetFunction(\n"
            "              static_cast<uint32_t>(_cfg->func_mappings[i].guest),\n"
            "              _cfg->func_mappings[i].host);\n"
            "    }\n" % cfgs) if mods else ""
        if not mods:
            # appglue only: open the hook with a distinct marker comment.
            hook_open = (
                "\n  // rexauto: appglue -- per-title host glue (identity / aliases /\n"
                "  // overlay) from %s_appglue.toml, wired into OnPostSetup.\n"
                "  void OnPostSetup() override {\n" % ctx.name)
        hook = hook_open + hook_dispatch + glue_body + "  }\n"
        idx = txt.rstrip().rfind("};")
        txt = txt[:idx] + hook + txt[idx:]

    open(app, "w", encoding="utf-8").write(txt)
    what = []
    if mods:
        what.append("%d extra module(s)" % len(mods))
    if glue:
        what.append("appglue [%s]" % ", ".join(sorted(glue.keys())))
    ctx.log("  wired %s into %s_app.h" % (" + ".join(what), ctx.name))


def setup_extra_modules(ctx):
    """Codegen + wire every extra recompiled module, plus per-title app glue.
    No-op for single-module titles with no appglue.toml (early return below)."""
    mods = extra_modules(ctx)
    glue = glue_records(ctx)
    if not mods and not glue:
        return
    for m in mods:
        _author_module_manifest(ctx, m)
        _seed_module_functions(ctx, m)
        man = os.path.join(ctx.port, "%s_manifest.toml" % m["key"])
        r = rexglue(ctx, "--log-level", "error", "codegen", man, capture=True)
        ok = os.path.exists(os.path.join(ctx.port, "generated", m["key"], "sources.cmake"))
        if r.returncode != 0 or not ok:
            out = (r.stdout or "") + (r.stderr or "")
            raise SystemExit("[rexauto] extra module '%s' codegen failed:\n%s"
                             % (m["key"], "\n".join(out.splitlines()[-10:])))
        ctx.log("  module '%s': codegen OK -> generated/%s" % (m["key"], m["key"]))
    # extra-module codegen rewrites generated/rexglue.cmake to point at the last extra;
    # the entrypoint codegen in the build loop runs AFTER this and restores it to default.
    if mods:
        _inject_extra_modules_into_cmake(ctx, mods)
    _inject_app_glue(ctx, mods, glue)


def stage_build(ctx):
    miss = [k for k in ("vcvars", "clang", "clangxx", "sdk") if not ctx.env[k]]
    if miss:
        raise SystemExit("missing build tools: %s (set via env vars or install)" % ", ".join(miss))
    write_game_icon(ctx)
    bat = write_build_bat(ctx)
    if not _heal.load_overrides(ctx.functions):  # fresh project -> seed from the shared gabarito
        fetch_gabarito(ctx)
    setup_extra_modules(ctx)   # codegen + wire any extra recompiled modules (no-op if none)
    last_ends = None
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        ctx.log("codegen + build (attempt %d/%d)" % (attempt, MAX_BUILD_ATTEMPTS))
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        txt = _heal._read_text(logp)
        if rc == 0 and os.path.exists(ctx.exe):
            write_game_root(ctx)
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


def run_once(ctx, seconds, discover=False):
    """Launch the game, let it run, kill it; return (newest-this-launch log text, alive).
    discover=True sets REX_HEAL_DISCOVER so the runtime logs+continues on each
    unregistered indirect target (surfacing many in one run) instead of aborting."""
    logdir = os.path.join(ctx.builddir, "logs")
    before = set(glob.glob(os.path.join(logdir, "*.log")))
    t0 = time.time()
    env = None
    if discover:
        env = dict(os.environ)
        env["REX_HEAL_DISCOVER"] = "1"
    try:
        p = subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
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


def _code_range(ctx):
    """[lo, hi) of the entrypoint module's generated code, for filtering discovered
    targets down to plausible function addresses."""
    import re
    try:
        h = open(os.path.join(ctx.gen, "default", ctx.name + "_init.h")).read()
        b = int(re.search(r"REX_CODE_BASE\s+0x([0-9A-Fa-f]+)", h).group(1), 16)
        s = int(re.search(r"REX_CODE_SIZE\s+0x([0-9A-Fa-f]+)", h).group(1), 16)
        return b, b + s
    except Exception:
        return 0x82000000, 0x84000000


def stage_runheal(ctx):
    bat = write_build_bat(ctx)
    # --- Fast bulk discovery -------------------------------------------------
    # With REX_HEAL_DISCOVER the runtime logs+continues on each unregistered
    # indirect target instead of aborting, so ONE run surfaces MANY missing
    # functions. Register the whole batch and rebuild once; repeat until a run
    # finds nothing new -- this collapses the O(N) one-at-a-time heal into a few
    # rebuilds. Targets are filtered to aligned, in-code-range addresses (the
    # corrupted no-op continuation can surface spurious ones; an extra generated
    # function is harmless).
    lo, hi = _code_range(ctx)
    for r in range(1, 12 + 1):
        txt, _ = run_once(ctx, ctx.args.run_seconds, discover=True)
        addrs = [a for a in _heal.invalid_functions_from_text(txt)
                 if lo <= a < hi and (a & 3) == 0]
        n = _heal.register_functions(addrs, ctx.functions)
        ctx.log("discover round %d: %d in-range targets -> +%d new" % (r, len(addrs), n))
        if n == 0:
            break
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        if rc != 0 or not os.path.exists(ctx.exe):
            if "use of undeclared label" in _heal._read_text(logp):
                _heal.heal_boundaries(logp, ctx.gen, ctx.functions)
                do_codegen(ctx)
                do_build(ctx, bat)
            else:
                ctx.log("  discovery rebuild failed -> %s; falling back to per-fatal heal" % logp)
                break
    # --- Confirm + heal anything discovery missed (FATAL mode) ---------------
    # A short heal window keeps iterations fast, but some titles only reach an
    # unregistered indirect target (e.g. a vtable method) once they get deeper
    # into startup/gameplay -- just past the window. Before declaring convergence,
    # re-run with a longer window so those late fatals aren't missed (this is what
    # made rayman crash at 0x82162208 ~1s past a 22s window after "converging").
    confirm_seconds = max(ctx.args.run_seconds * 2, ctx.args.run_seconds + 25)
    for it in range(1, ctx.args.heal_iters + 1):
        txt, alive = run_once(ctx, ctx.args.run_seconds)
        addrs = _heal.invalid_functions_from_text(txt)
        if not addrs:
            ctx.log("  no invalid-function fatal in %ds; confirming with a %ds window"
                    % (ctx.args.run_seconds, confirm_seconds))
            ctxt, calive = run_once(ctx, confirm_seconds)
            caddrs = _heal.invalid_functions_from_text(ctxt)
            if not caddrs:
                verdict = ("survived %ds with no invalid-function fatal" % confirm_seconds) if calive \
                    else "exited without an invalid-function fatal (other stop - likely GPU/runtime)"
                ctx.log("run-heal converged: %s" % verdict)
                if getattr(ctx.args, "publish_gabarito", False):
                    publish_gabarito(ctx)
                return ctx.mark("runheal", {"iters": it, "alive": calive,
                                            "confirmed_seconds": confirm_seconds})
            ctx.log("  confirmation surfaced %d late fatal(s); continuing heal" % len(caddrs))
            addrs, txt = caddrs, ctxt
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
    if getattr(ctx.args, "publish_gabarito", False):
        publish_gabarito(ctx)
    ctx.mark("runheal", {"iters": ctx.args.heal_iters})


def stage_run(ctx):
    ctx.log("launching %s" % ctx.exe)
    # Detach the game from the pipeline's stdio. If it inherits the GUI Hub's stdout
    # pipe, the Hub's reader blocks until the GAME exits -> the 'done' event never
    # fires -> the GUI stays 'Recompiling' and you cannot start another game.
    subprocess.Popen([ctx.exe, "--game_data_root=%s" % ctx.game], cwd=ctx.builddir,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    ctx.log("running. a game window should open. (GPU/playability is per-title and not "
            "auto-solved by rexauto.)")


# --------------------------------------------------------------------------- main
# --- SDK compatibility pin --------------------------------------------------
# rexauto generates code with a specific rexglue codegen tool and links it
# against a specific runtime. Mixing a DIFFERENT SDK build can silently produce
# broken or crashing exes — the v1.3 fork migration changed the scaffolding and
# the runtime ABI, exactly the kind of mismatch this guards against. rexauto
# refuses to run against an SDK whose binaries don't match the ones it was built
# and tested with. Override (advanced, may produce broken builds) by setting
# REXAUTO_SKIP_SDK_CHECK=1. Bump these when the bundled SDK is updated.
SDK_PIN = {
    # v1.9: vtable mid-function landing discovery restored. phase_discover.cpp now
    # addFunction()s a vtable slot that lands inside a parent (no registerChunk, so
    # the parent's bctr lowering stays byte-identical -> Budokai3-safe), instead of
    # dropping it. Restores clean-SDK coverage: indirect/virtual call targets (e.g.
    # skate3 0x82B30790) are statically discovered instead of runtime-healed.
    # Keeps v1.7's switch-on-ctr build_bctr + discovery-trap. Runtime also carries
    # caller-lr in the invalid-call FATAL + GPU command-ring memory fixes (battle-freeze).
    # SDK commits 8b84c2d (codegen) + 3b0d7cc (runtime); gate 9/9 byte-identical.
    # Pin re-generated to the actually-shipped v1.9 binaries (rebuilt from the same
    # HEAD 3b0d7cc; C++ links are non-reproducible so the exact bytes differ from the
    # first v1.9 build) so the shipped rexauto.exe pin == the sha256 of rexglue/tool/*
    # in the shipped rexglue-sdk-win64.zip. cmake --install refreshed the whole tree.
    "rexglue.exe":    "f79a3881a47bd6ddfe841c12094fd3dd48c3a1e23620bb76aaa4873ac2da9eea",
    "rexruntime.dll": "c503f763ab45f55a113503b3d20b0705e1813b19a90bbf0eb35b9864b9465bd8",
}


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_sdk_pin_checked = False


def verify_sdk_pin(env):
    """Refuse a mismatched SDK so an incompatible rexglue/runtime can't be used.
    Called right before any rexglue.exe use (codegen/init) -- a pure game run (the
    GUI Launch of an already-built title) never reaches it, so launching is never
    blocked by a pin mismatch; only building/codegen is gated. Checked once."""
    global _sdk_pin_checked
    if _sdk_pin_checked:
        return
    _sdk_pin_checked = True
    if os.environ.get("REXAUTO_SKIP_SDK_CHECK"):
        print("[rexauto] WARNING: SDK pin check skipped (REXAUTO_SKIP_SDK_CHECK) — "
              "an incompatible SDK may produce broken builds")
        return
    rexglue = env.get("rexglue")
    if not rexglue:
        return
    targets = [("rexglue.exe", rexglue),
               ("rexruntime.dll", os.path.join(os.path.dirname(rexglue), "rexruntime.dll"))]
    for name, path in targets:
        want = SDK_PIN.get(name)
        if not want or not path or not os.path.exists(path):
            continue
        got = _sha256(path)
        if got != want:
            raise SystemExit(
                "[rexauto] SDK MISMATCH — refusing to run.\n"
                "  %s does not match the SDK this rexauto was built and tested with.\n"
                "    expected sha256 %s\n    found    sha256 %s\n    at %s\n"
                "  Use the rexglue-sdk bundled with this rexauto release (extract it next\n"
                "  to rexauto, or point REXSDK_DIR / REXGLUE at it). To override anyway\n"
                "  (advanced — may produce broken or crashing builds): set "
                "REXAUTO_SKIP_SDK_CHECK=1.\n"
                % (name, want, got, path))


# --- Shared "gabarito" database: per-binary pre-discovered cures --------------
# Once a title's heal has found its missing functions (the functions.toml
# overrides), that set is identical for everyone running the SAME binary. Publish
# it keyed by the default.xex hash so the next person seeds it and skips the slow
# auto-cure cycle. Fetch is public / no-auth; a miss just falls back to healing.
GABARITO_RAW = "https://raw.githubusercontent.com/xdzleo/rexauto/main/gabaritos"


def gabarito_key(ctx):
    """Exact per-binary key: sha256 of the entrypoint default.xex (cures are
    address-specific, so they must match the exact code image). When a title update
    is applied, codegen + runtime recompile/run the PATCHED image, so fold the
    .xexp delta into the key -- the TU build's cures are for the patched image and
    must not collide with (or be seeded from) the base build's."""
    import hashlib
    try:
        if not ctx.xex or not os.path.exists(ctx.xex):
            return None
        key = _sha256(ctx.xex)
        tu = getattr(ctx, "tu_xexp", None)
        if tu and os.path.exists(tu):
            key = hashlib.sha256((key + _sha256(tu)).encode()).hexdigest()
        return key
    except Exception:
        return None


def fetch_gabarito(ctx):
    """Seed functions.toml from the shared gabarito for this exact binary, if one
    exists, so the heal starts (mostly) solved. Returns the number of cures seeded."""
    if os.environ.get("REXAUTO_NO_GABARITO"):
        return 0
    key = gabarito_key(ctx)
    if not key:
        return 0
    try:
        import urllib.request
        with urllib.request.urlopen("%s/%s.toml" % (GABARITO_RAW, key), timeout=15) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception:
        return 0  # no gabarito for this binary -> heal from scratch
    n = len(re.findall(r'"0x[0-9A-Fa-f]+"\s*=', body))
    if n == 0:
        return 0
    with open(ctx.functions, "w", encoding="utf-8") as f:
        f.write(body)
    ctx.log("gabarito: seeded %d known cures from the shared database (xex %s…) -> "
            "auto-heal short or skipped" % (n, key[:12]))
    return n


def publish_gabarito(ctx):
    """Write this title's discovered cures as a gabarito file (keyed by xex hash) in
    the repo's gabaritos/ folder, so it can be committed and shared."""
    key = gabarito_key(ctx)
    if not key or not os.path.exists(ctx.functions):
        ctx.log("gabarito: nothing to publish")
        return
    src = open(ctx.functions, encoding="utf-8", errors="ignore").read()
    m = re.search(r'\[functions\].*', src, re.S)
    n = len(re.findall(r'"0x[0-9A-Fa-f]+"\s*=', src))
    out_dir = os.path.join(HERE, "gabaritos")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, key + ".toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write('# rexauto gabarito — pre-discovered cures for "%s" (%d)\n'
                '[meta]\nname = "%s"\nxex_sha256 = "%s"\ncures = %d\n\n%s'
                % (ctx.name, n, ctx.name, key, n, m.group(0) if m else "[functions]\n"))
    ctx.log("gabarito: wrote gabaritos/%s.toml (%d cures) — commit it to share" % (key[:12], n))


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
    ap.add_argument("--no-title-update", action="store_true",
                    help="do not auto-detect/apply an Xbox 360 title update (.xexp); "
                         "build the base game version")
    ap.add_argument("--heal-iters", type=int, default=20)
    ap.add_argument("--run-seconds", type=int, default=22)
    ap.add_argument("--publish-gabarito", action="store_true",
                    help="write the discovered cures to gabaritos/ (keyed by xex hash) to share")
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
    fns = {"extract": stage_extract, "init": stage_init, "setjmp": stage_setjmp,
           "jumptables": stage_jumptables, "build": stage_build, "runheal": stage_runheal,
           "run": stage_run}
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
