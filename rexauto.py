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
import copy
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
import deepextract as _dx

STAGES = ["extract", "xctd", "init", "setjmp", "jumptables", "deepextract", "build", "runheal", "run"]
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
        self.forced = os.path.join(self.port, "%s_forced_landings.toml" % self.name)
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


def add_includes(ctx, names, manifest=None):
    man = manifest or ctx.manifest
    txt = open(man, encoding="utf-8", errors="ignore").read()
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
    open(man, "w", encoding="utf-8").write(txt)


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


def stage_xctd(ctx):
    """Pre-decompress XCTD (XCompress LZXTDECODE 0F F5 12 ED) assets in place.
    On real hardware the KERNEL transparently decompresses these; our runtime's
    XctdCompressionInformation stub makes the game take its "not compressed"
    path, so serving plaintext is exactly what it expects. No-op (0 files) for
    every title that doesn't use it -- fleet regression-free by construction.
    Runs BEFORE init/codegen so the whole pipeline sees the final game dir.
    Proved on Captain America: Super Soldier (asset wall -> gameplay); same
    format ships in Alien: Isolation, Monkey Island 2 SE, XCOM."""
    import xctd as _xctd
    game = ctx.game or ctx._game_out
    if not os.path.isdir(game):
        raise SystemExit("[rexauto] xctd: no game dir at %s -- run extract first" % game)
    backup = os.path.join(ctx.work, "xctd_originals")
    n = _xctd.rip_inplace(game, backup, ctx.env, log=ctx.log)
    ctx.mark("xctd", {"files": n})


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
    image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
    secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
    exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                 for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
    if not exec_secs:
        ctx.log("could not parse exec sections -> skipping setjmp detection")
        return ctx.mark("setjmp", {"skipped": "no-sections"})
    # Hand the freshly-dumped image + parsed ranges to the jumptables stage, which
    # runs immediately after and would otherwise re-run an IDENTICAL image-dump
    # codegen (~46s on GTA-SA, ~4min on GTA V) purely to reproduce this same file.
    # The image dump is the raw decompressed sections (project_recompiler.cpp:251),
    # independent of setjmp/functions.toml, so it is byte-identical between the two
    # stages -- reuse is safe. Only set when running in-process this session; a
    # `--from jumptables` run has no stash and re-dumps as before.
    ctx._jt_image = {"image": image, "base": base, "image_end": image_end,
                     "exec_secs": exec_secs}
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
    reuse = getattr(ctx, "_jt_image", None)
    if reuse and reuse.get("image") == image and os.path.exists(image):
        # The setjmp stage (which ran this same session) already produced this exact
        # image + ranges from an identical codegen. Skip the redundant re-dump.
        ctx.log("reusing image + section ranges from the setjmp stage "
                "(identical codegen; skipping the redundant image-dump pass)")
        base, image_end, exec_secs = reuse["base"], reuse["image_end"], reuse["exec_secs"]
    else:
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
    # --- global IDA cache: identical image => identical analysis --------------
    # The IDA pass is the pipeline's one 100%-serial single-core sink (minutes on
    # a big title) and is fully determined by (image bytes, analysis code). A
    # re-port of the same game (budokai3's fresh regen re-paid a byte-identical
    # analysis) or a wiped work dir should never re-run it. Keyed by
    # sha256(image) + section ranges + the xenon-jumptables revision; the cached
    # artifacts are the switch_tables.toml AND the .i64 (which deepextract
    # reuses, so a hit accelerates that stage too). REXAUTO_NO_IDA_CACHE=1 opts out.
    ida_i64 = image + ".elf.i64"
    cache_hit = False
    cache_dir = None
    if not os.environ.get("REXAUTO_NO_IDA_CACHE"):
        # Key the cache on the CONTENT of the scripts that determine the
        # analysis, not the repo revision: a xenon-jumptables commit that
        # doesn't touch the analysis code (closure_cert, extract_funcs, docs,
        # lint) used to invalidate the whole fleet's cached .i64 analyses --
        # minutes of serial IDA per module re-paid for nothing (three tooling
        # commits on 10/jul forced fifadllzf 29MB + halo's 4 waves modules to
        # re-analyze). Falls back to the git rev if the files are unreadable.
        jt_rev = ""
        try:
            import hashlib as _hl
            h = _hl.sha256()
            for s in ("ida_jumptables.py", "deep_extract.py", "recover.py", "gen_toml.py"):
                p = os.path.join(ctx.env["jt_repo"], "src", s)
                if os.path.exists(p):
                    h.update(open(p, "rb").read())
            jt_rev = h.hexdigest()
        except Exception:
            try:
                r = run(["git", "-C", ctx.env["jt_repo"], "rev-parse", "HEAD"],
                        capture_output=True, text=True)
                jt_rev = (r.stdout or "").strip()
            except Exception:
                pass
        # The function list SEEDS the analysis (cfg "functions"), so it is an
        # analysis input too: today it went 0 -> 101426 entries for fifadllzf,
        # and a hit keyed without it would have replayed the 0-seed analysis.
        key = "%s-%x-%x-%s-%s" % (_sha256(image)[:20], text_start, text_end, jt_rev[:12],
                                  _sha256(funcs)[:12])
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "ida", key)
        c_toml, c_i64 = os.path.join(cache_dir, "switch_tables.toml"), os.path.join(cache_dir, "image.i64")
        if os.path.exists(c_toml) and os.path.exists(c_i64):
            shutil.copyfile(c_toml, ctx.switches)
            shutil.copyfile(c_i64, ida_i64)
            n = open(ctx.switches).read().count("[[switch_tables]]")
            add_includes(ctx, ["%s_switch_tables.toml" % ctx.name])
            ctx.log("jump tables from IDA cache: %d tables (identical image analyzed "
                    "before; delete rexauto/cache/ida to force re-analysis)" % n)
            return ctx.mark("jumptables", {"tables": n, "cache": True})
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
    if cache_dir and os.path.exists(ida_i64):
        try:
            os.makedirs(cache_dir, exist_ok=True)
            shutil.copyfile(ctx.switches, os.path.join(cache_dir, "switch_tables.toml"))
            shutil.copyfile(ida_i64, os.path.join(cache_dir, "image.i64"))
        except OSError as ex:
            ctx.log("  (IDA cache write skipped: %s)" % ex)
    ctx.log("jump tables recovered: %d" % n)
    ctx.mark("jumptables", {"tables": n})


def write_build_bat(ctx, parallel=None):
    # A clang-OOM lesson (see the "LLVM ERROR: out of memory" handlers) is
    # PERSISTENT: once a port's giant TUs prove they can't take the default
    # 18 concurrent frontends, every later bat regeneration -- heal-loop,
    # re-runs, module builds sharing the work dir -- inherits the reduced -j
    # instead of re-discovering the crash. Explicit `parallel` still wins.
    if parallel is None:
        parallel = ctx.load_state().get("build_parallel")
    bat = os.path.join(ctx.work, "_build.bat")
    sdk = ctx.env["sdk"].replace("\\", "/")
    # RelWithDebInfo by default: same optimization as Release but with symbols +
    # line info, so a crash in the recompiled code points straight at the generated
    # sub_XXXX + line -- the heal/gate debug loop's biggest pain. Codegen is
    # unaffected (the build type never changes generated/), so it's zero-regression
    # for the codegen gate. Set REXAUTO_BUILD_TYPE=Release for a stripped, smaller
    # distribution build.
    build_type = os.environ.get("REXAUTO_BUILD_TYPE", "RelWithDebInfo")
    configure = ('cmake --preset win-amd64-release -DCMAKE_BUILD_TYPE=%s '
                 # map imported libs (spdlog/fmt) to their Release variant under
                 # RelWithDebInfo, else CMake links spdlogd.lib (_ITERATOR_DEBUG_LEVEL=2)
                 # against our IDL=0 objects -> lld-link /failifmismatch. Harmless for Release.
                 '-DCMAKE_MAP_IMPORTED_CONFIG_RELWITHDEBINFO=Release -DCMAKE_C_COMPILER="%s" '
                 '-DCMAKE_CXX_COMPILER="%s" -DCMAKE_PREFIX_PATH="%s" '
                 '-Drexglue_DIR="%s/lib/cmake/rexglue"'
                 % (build_type, ctx.env["clang"].replace("\\", "/"),
                    ctx.env["clangxx"].replace("\\", "/"), sdk, sdk))
    # Perf win #4 (strip per-round reconfigure): every heal round used to pay a
    # full `cmake --preset` (~5-15s) even though nothing about the configuration
    # changed. The bat now configures only when the build dir has no
    # CMakeCache.txt; a CHANGE in configure inputs (build type, compilers, SDK
    # path) is detected here python-side via a stamp file and forces a fresh
    # configure by deleting the cache. Output-neutral: the configure command is
    # byte-identical when it does run.
    bdir = os.path.join(ctx.port, "out", "build", "win-amd64-release")
    stamp = os.path.join(ctx.work, "_configure.stamp")
    old = open(stamp, encoding="utf-8").read() if os.path.exists(stamp) else None
    if old != configure:
        try:
            os.remove(os.path.join(bdir, "CMakeCache.txt"))
        except OSError:
            pass
        open(stamp, "w", encoding="utf-8").write(configure)
    lines = [
        "@echo off",
        'call "%s" >nul' % ctx.env["vcvars"],
        'cd /d "%s"' % ctx.port,
        'if not exist "out\\build\\win-amd64-release\\CMakeCache.txt" (',
        "  " + configure,
        ")",
        "cmake --build out/build/win-amd64-release --parallel%s -- -k 0" % (
            " %d" % parallel if parallel else ""),
        # capture the build's errorlevel BEFORE echo resets it -- `echo RC=...`
        # used to be the last command, so the bat's exit code was ALWAYS 0 and
        # a failed heal-round rebuild silently relaunched the stale exe (the
        # Gears of War 3 ghost-target loop).
        "set BUILDRC=%errorlevel%",
        "echo RC=%BUILDRC%",
        "exit /b %BUILDRC%",
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
    skips recompiling them. NOTE: an earlier version also kept the old timestamp
    on the shared init header when its only diff was added DECLARE_REX_FUNC lines
    ("a new extern can't change a compiled TU"). That is UNSOUND with the PCH:
    clang validates the precompiled header against the header's CONTENT/size, so
    a content-changed header with an old mtime leaves the PCH stale and every
    subsequent compile fails ("modified since the precompiled header was built")
    -- which, combined with the always-0 build-bat exit code, made heal rounds
    silently relaunch a stale exe (Gears of War 3 ghost-target loop). A changed
    header now always keeps its new mtime: the PCH and its TUs rebuild."""
    units = 0
    for p, (h, mt, _oldlines) in snap.items():
        try:
            if not os.path.exists(p):
                continue
            data = open(p, "rb").read()
            if hashlib.md5(data).digest() == h:
                os.utime(p, (mt, mt))
                units += 1
        except OSError:
            pass
    headers = 0
    if units or headers:
        ctx.log("  incremental rebuild: reused %d unit(s)%s"
                % (units, " + %d header(s)" % headers if headers else ""))


def _normalize_toml_newlines(ctx):
    """Repair doubled carriage returns (\\r\\r\\n) in the per-project tomls.
    A text-mode writer handed a string that already contained \\r\\n produces
    them (seen once in the wild: a frozen-exe jumptables run corrupted
    switch_tables.toml, and rexglue's toml parser hard-fails on \\r\\r).
    Byte-preserving for healthy files: only rewrites when \\r\\r is present."""
    for p in (ctx.functions, ctx.switches, ctx.forced, ctx.manifest):
        try:
            if p and os.path.exists(p):
                raw = open(p, "rb").read()
                if b"\r\r" in raw:
                    open(p, "wb").write(raw.replace(b"\r\r\n", b"\r\n").replace(b"\r\r", b"\r\n"))
                    ctx.log("repaired doubled line endings in %s" % os.path.basename(p))
        except OSError:
            pass


def do_codegen(ctx, env=None, level="error"):
    """Run codegen, auto-registering unresolved tail-call targets (codegen's
    Validate phase reports them) until it passes. Returns the captured output
    (at trace level it carries the section ranges the jumptables stage needs)."""
    _normalize_toml_newlines(ctx)
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
            # PCH wiring must run AFTER codegen: the only earlier call site
            # (setup_extra_modules) fires before <name>_init.h exists, so its
            # exists() guard silently skipped the injection and the v2.4.0
            # ~21%/TU win quietly vanished for every fresh port (fleet audit:
            # 1/18 ports had the PCH block). Idempotent, so calling per-codegen
            # is free once injected.
            _inject_pch_into_cmake(ctx)
            _inject_debug_diet_into_cmake(ctx)
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
    rc = p.wait()
    if rc == 0:
        apply_game_icon(ctx)  # every relink rewrites the exe -> re-brand it
    return logp, rc


def apply_game_icon(ctx):
    """Brand ctx.exe with the game's marketplace tile as its Windows icon.
    Best-effort: offline/no-tile/locked-exe just skips (never fails a build)."""
    try:
        if not os.path.exists(ctx.exe):
            return
        xex = ctx.xex or os.path.join(ctx.game or "", "default.xex")
        if not os.path.exists(xex):
            return
        with open(xex, "rb") as f:
            tid = _extract._xex_title_id(f.read(0x10000))
        png = _extract.fetch_title_icon(tid) if tid else None
        if not png:
            return
        import exeicon
        if exeicon.set_exe_icon(ctx.exe, png):
            if not getattr(ctx, "_icon_logged", False):
                ctx._icon_logged = True
                ctx.log("exe branded with the game's tile icon (title_id %s)" % tid)
    except Exception as ex:
        ctx.log("icon branding skipped (%s)" % ex)


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
    # Prefer a seed keyed by the module image's sha256 (collision-proof: "gamelogic"
    # is a generic module name across engines, and a wrong seed registers functions
    # at addresses that aren't code in THAT module). Key-named seeds stay as the
    # legacy fallback (eawebkit).
    seed = None
    try:
        h = hashlib.sha256(open(m["xex"], "rb").read()).hexdigest()[:16]
        cand = os.path.join(HERE, "seeds", "%s_functions.toml" % h)
        if os.path.exists(cand):
            seed = cand
    except OSError:
        pass
    if not seed:
        cand = os.path.join(HERE, "seeds", "%s_functions.toml" % m["key"])
        if os.path.exists(cand):
            seed = cand
    if seed:
        shutil.copyfile(seed, fns)
        ctx.log("  module '%s': seeded cures from rexauto/seeds (%s)"
                % (m["key"], os.path.basename(seed)))
    else:
        _heal.write_overrides(fns, {})


def _inject_extra_modules_into_cmake(ctx, mods):
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    keys = " ".join(m["key"] for m in mods)
    if "_rexauto_mod" in txt:   # already injected -> UPDATE the key list if the
        # module set changed (modules can be ADDED after the first build: a second
        # companion declared later, or run-heal's zero-touch auto-detection). A
        # stale list silently drops the new modules from the exe -- Halo 3's
        # waveslib codegen'd fine but never compiled/registered, so its heal
        # looped on an address that could never resolve.
        new = re.sub(r"foreach\(_rexauto_mod [^)]*\)", "foreach(_rexauto_mod %s)" % keys, txt)
        if new != txt:
            open(cml, "w", encoding="utf-8").write(new)
            ctx.log("  extra-module list in CMakeLists updated -> %s" % keys)
        return
    keys = " ".join(m["key"] for m in mods)
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-extra-modules: extra recompiled modules linked into the same exe.\n"
        "foreach(_rexauto_mod %s)\n"
        "    if(EXISTS \"${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}/sources.cmake\")\n"
        "        set(_rexauto_saved \"${GENERATED_SOURCES}\")\n"
        "        include(generated/${_rexauto_mod}/sources.cmake)\n"
        "        target_sources(%s PRIVATE ${GENERATED_SOURCES})\n"
        "        # this module's TUs include their own <mod>_init.h, not the\n"
        "        # entrypoint's, so they must skip the entrypoint PCH (wrong header).\n"
        "        set_source_files_properties(${GENERATED_SOURCES} PROPERTIES\n"
        "            SKIP_PRECOMPILE_HEADERS ON)\n"
        "        target_include_directories(%s PRIVATE\n"
        "            \"${CMAKE_CURRENT_SOURCE_DIR}/generated/${_rexauto_mod}\")\n"
        "        set(GENERATED_SOURCES \"${_rexauto_saved}\")\n"
        "        unset(_rexauto_saved)\n"
        "    endif()\nendforeach()\n" % (keys, ctx.name, ctx.name))
    ctx.log("  wired %d extra module(s) into CMakeLists" % len(mods))


def _inject_pch_into_cmake(ctx):
    """Precompile the entrypoint module's <name>_init.h monolith. Every generated
    recomp TU opens with `#include "<name>_init.h"` -- a huge header (tens of
    thousands of DECLARE_REX_FUNC externs + heavy C++ STL) whose front-end parse
    is otherwise a fixed floor paid once per TU. A PCH parses it ONCE (~20% off
    per-TU compile, small TUs several x). Output-neutral: a PCH caches the parsed
    AST, never the emitted code, so the generated C++ and the binary's .text stay
    byte-identical (codegen gate unaffected). Idempotent; extra modules skip it
    (they include their own init header). Set REXAUTO_NO_PCH=1 to opt out."""
    if os.environ.get("REXAUTO_NO_PCH"):
        return
    if os.path.basename(ctx.gen) != "default":  # extra-module view: no own CMake
        return                                  # target + its init.h lives elsewhere
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "target_precompile_headers" in txt:   # already present (manual or prior run)
        return
    if not os.path.exists(os.path.join(ctx.gen, "%s_init.h" % ctx.name)):
        return
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-pch: parse the %s_init.h monolith once, not once per TU\n"
        "# (build perf; output-neutral -- a PCH caches the AST, not emitted code).\n"
        "target_precompile_headers(%s PRIVATE\n"
        "    \"${CMAKE_CURRENT_SOURCE_DIR}/generated/default/%s_init.h\")\n"
        % (ctx.name, ctx.name, ctx.name))
    ctx.log("  wired PCH for %s_init.h into CMakeLists" % ctx.name)


def _inject_debug_diet_into_cmake(ctx):
    """RelWithDebInfo builds carry FULL codeview debug info (-g -gcodeview via
    CMake's MSVC debug-format abstraction): variable/type info for tens of
    thousands of generated functions = a ~100MB PDB re-linked on EVERY heal
    round / gate rebuild (~70s a cycle measured on gta_san_andreas).
    -gline-tables-only keeps exactly what our tooling uses -- function symbols +
    line tables (cdb guest stacks like sub_82XXXXXX+off still resolve) -- and
    drops the bulk. Output-neutral for the generated C++ AND for .text: debug
    info only. Appended via target_compile_options so it lands AFTER the
    config-level -g and downgrades it. Idempotent; REXAUTO_FULL_DEBUG=1 opts out."""
    if os.environ.get("REXAUTO_FULL_DEBUG"):
        return
    if os.path.basename(ctx.gen) != "default":  # extra-module view: not its own target
        return
    cml = os.path.join(ctx.port, "CMakeLists.txt")
    if not os.path.exists(cml):
        return
    txt = open(cml, encoding="utf-8", errors="ignore").read()
    if "gline-tables-only" in txt:
        return
    open(cml, "a", encoding="utf-8").write(
        "\n# rexauto-debug-diet: keep function symbols + line tables, drop the\n"
        "# variable/type debug info that bloats the PDB and slows every relink\n"
        "# (build perf; debug-info-only -- .text and codegen stay byte-identical).\n"
        "if(CMAKE_CXX_COMPILER_ID MATCHES \"Clang\")\n"
        "    target_compile_options(%s PRIVATE $<$<CONFIG:RelWithDebInfo>:-gline-tables-only>)\n"
        "endif()\n" % ctx.name)
    ctx.log("  wired -gline-tables-only (RelWithDebInfo) into CMakeLists")


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
    if has_mods and mods:
        # UPDATE the already-injected block when the module set changed (modules
        # can be added after the first injection -- a later-declared companion or
        # run-heal's zero-touch auto-detection). A stale extern/config list means
        # the new modules' function tables never register at runtime.
        externs_new = "\n".join('extern const rex::PPCImageInfo %sPPCImageConfig;' % m["symbol_prefix"]
                                for m in mods)
        cfgs_new = ", ".join('&%sPPCImageConfig' % m["symbol_prefix"] for m in mods)
        upd = re.sub(r"(for \(const rex::PPCImageInfo\* _cfg : \{ )[^}]*( \}\))",
                     lambda mm: mm.group(1) + cfgs_new + mm.group(2), txt, count=1)
        upd = re.sub(r"(// rexauto: extra recompiled module\(s\) linked into this exe\.\n)"
                     r"(?:extern const rex::PPCImageInfo \w+PPCImageConfig;\n?)+",
                     lambda mm: mm.group(1) + externs_new + "\n", upd, count=1)
        if upd != txt:
            open(app, "w", encoding="utf-8").write(upd)
            ctx.log("  extra-module dispatcher list in %s_app.h updated -> %d module(s)"
                    % (ctx.name, len(mods)))
            txt = upd
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
            "                                               _cfg->image_base, _cfg->image_size,\n"
            "                                               /*is_entrypoint=*/false,\n"
            "                                               _cfg->function_table_base))\n"
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


def _module_view(ctx, m):
    """A shallow ctx clone whose per-title paths point at an extra module's files,
    so the entrypoint's full IDA pipeline (stage_jumptables / stage_deepextract /
    do_codegen) runs verbatim on the module. Env/work/build paths are inherited
    (same port tree); only the name-derived artifacts diverge. A separate statefile
    keeps the module's stage marks from clobbering the entrypoint's resumable
    state; _jt_image is cleared so jumptables never reuses the entrypoint's dump
    (a different image); log lines are prefixed to stay distinguishable."""
    mc = copy.copy(ctx)
    key = m["key"]
    mc.name = key
    mc.manifest = os.path.join(ctx.port, "%s_manifest.toml" % key)
    mc.functions = os.path.join(ctx.port, "%s_functions.toml" % key)
    mc.switches = os.path.join(ctx.port, "%s_switch_tables.toml" % key)
    mc.forced = os.path.join(ctx.port, "%s_forced_landings.toml" % key)
    mc.gen = os.path.join(ctx.port, "generated", key)
    mc.statefile = os.path.join(ctx.work, ".rexauto_state_%s" % key)
    mc._jt_image = None
    base_log = ctx.log
    mc.log = lambda msg, _b=base_log, _k=key: _b("[mod:%s] %s" % (_k, msg))
    return mc


def _codegen_module(ctx, m):
    """Recompile one extra module through the SAME jump-table + deep-extract IDA
    pipeline the entrypoint gets. A companion XEX (e.g. FIFA Street's fifadllzf,
    Skate 3's EAWebkit) has its own computed branches / jump tables; a bare codegen
    fails validation ('target not in any function', the 12 fatals on fifadllzf).
    Reachable only for titles that declare a <name>_modules.toml -> the fleet is
    untouched (byte-identical)."""
    mc = _module_view(ctx, m)
    image = os.path.join(mc.work, "%s_image.bin" % mc.name)
    mc.log("recompiling companion module through the full IDA pipeline")
    # 1. Dump the raw decompressed image for IDA. REX_DUMP_IMAGE dumps + exits WITHOUT
    #    emitting the C++ sources: that is correct here, because a source emit can't
    #    succeed yet -- the module's own computed branches are unresolved until the
    #    switch tables below exist, and rexglue emits nothing on a failed Validate.
    #    Jump-table recovery needs only the raw image (IDA auto-analysis), not the
    #    sources, so it runs first and breaks the circular dependency.
    r = rexglue(mc, "--log-level", "trace", "codegen", mc.manifest,
                env={"REX_DUMP_IMAGE": image}, capture=True)
    blob = (r.stdout or "") + (r.stderr or "")
    if os.path.exists(image):
        bm = re.search(r"base=0x([0-9A-Fa-f]+), size=0x([0-9A-Fa-f]+)", blob)
        base = int(bm.group(1), 16) if bm else 0x82000000
        image_end = base + (int(bm.group(2), 16) if bm else 0x900000)
        secs = re.findall(r"section '([^']+)' at 0x([0-9A-Fa-f]+) size 0x([0-9A-Fa-f]+) exec=(\w+)", blob)
        exec_secs = [(int(a, 16), int(a, 16) + int(sz, 16))
                     for _, a, sz, ex in secs if ex.lower() in ("true", "1")]
        if exec_secs:
            mc._jt_image = {"image": image, "base": base, "image_end": image_end,
                            "exec_secs": exec_secs}
            # Companion setjmp/longjmp: a module can ship its OWN setjmp pair
            # (fifadllzf embeds Lua 5.1) and needs the same special codegen the
            # entrypoint gets -- recompiled as plain code, longjmp is undefined
            # behavior. FIFA Street: a luaD_throw during the protected lua open
            # unwound wrongly and leaked a partially-initialized lua_State ->
            # AV in luaS_newlstr reading G(L)->strt.hash garbage. Detection is
            # signature-based per-image; no signature -> no write -> no-op.
            # Runs every build: _author_module_manifest rewrites the module
            # manifest each pass, so the addresses must be re-applied here.
            try:
                import detect_setjmp as _dj
                sres = _dj.detect(image, exec_secs, base)
                slj, ssj = sres.get("longjmp_address"), sres.get("setjmp_address")
                if slj and ssj:
                    _dj.write_addresses(mc.manifest, longjmp=slj, setjmp=ssj)
                    mc.log("module setjmp/longjmp detected -> setjmp=0x%X longjmp=0x%X "
                           "(written to module manifest)" % (ssj, slj))
            except Exception as ex:
                mc.log("module setjmp detection skipped (%s)" % ex)
    # 2. IDA jump-table recovery on the raw image -> switch tables. extract_funcs
    #    finds no sources yet (empty funclist), but IDA's own auto-analysis recovers
    #    the tables regardless (proven on fifadllzf: 529 tables from a 0-function list).
    #    ONE-SHOT like deep-extract: this runs on EVERY stage_build, and the cache
    #    key includes the funclist hash -- every heal round that adds a module cure
    #    changes the funclist, misses the cache, and re-paid ~8min of serial IDA
    #    (defining 101k functions on fifadllzf) per build. The tables are recovered
    #    from the raw image; heal-added functions don't need table re-analysis
    #    (build_bctr discovery-trap + forced landings cover their computed branches).
    #    The manifest is rewritten every pass (_author_module_manifest), so the
    #    include wiring stage_jumptables would do must be re-applied on the skip.
    jt_prev = mc.load_state().get("jumptables")
    jt_done = isinstance(jt_prev, dict) and "tables" in jt_prev
    if jt_done and os.path.exists(mc.switches) and os.environ.get("REXAUTO_MODULE_JT") != "force":
        add_includes(mc, ["%s_switch_tables.toml" % mc.name])
        if os.path.exists(mc.forced) and os.path.getsize(mc.forced) > 0:
            _heal.ensure_manifest_include(mc.manifest, os.path.basename(mc.forced))
        mc.log("jump tables already recovered (%s tables) -> skip re-analysis "
               "(REXAUTO_MODULE_JT=force to re-run)" % jt_prev.get("tables"))
    else:
        stage_jumptables(mc)
    # 3. First real codegen: the switch tables now resolve the computed branches, so
    #    this PASSES and emits generated/<key> (auto-register mops up tail calls).
    do_codegen(mc)
    # 4. Deep static extract -- now that the module's sources/init.h exist, the same
    #    funcmap + vtable data-xref pass the entrypoint gets folds in the functions
    #    the linear scan missed (before this reorder it silently skipped: no init.h).
    #    ONE-SHOT per module: it is a static analysis whose folds persist in the
    #    module's functions.toml, but this function runs on EVERY stage_build
    #    (setup_extra_modules), and re-running the extract+gate re-paid ~10-15min
    #    of IDA + codegen probes on a giant module per build. Skip when the
    #    module statefile already marks a completed (non-"skipped") run;
    #    REXAUTO_MODULE_DEEPX=force re-runs it.
    dx_prev = mc.load_state().get("deepextract")
    dx_done = isinstance(dx_prev, dict) and "candidates" in dx_prev
    if dx_done and os.environ.get("REXAUTO_MODULE_DEEPX") != "force":
        mc.log("deep-extract already done (candidates=%s accepted=%s) -> skip "
               "(REXAUTO_MODULE_DEEPX=force to re-run)"
               % (dx_prev.get("candidates"), dx_prev.get("accepted")))
    else:
        # gen_current: do_codegen is the immediately preceding step, so the
        # gate's opening baseline probe (~284s on fifadllzf) is redundant.
        stage_deepextract(mc, gen_current=True)
        # 5. Re-codegen to fold the additions -- ONLY when the gate accepted
        #    something; an unconditional pass re-emitted the whole module
        #    (~284s) to change nothing when accepted=0.
        dx_now = mc.load_state().get("deepextract")
        if isinstance(dx_now, dict) and dx_now.get("accepted"):
            do_codegen(mc)
    if not os.path.exists(os.path.join(mc.gen, "sources.cmake")):
        raise SystemExit("[rexauto] extra module '%s' codegen failed after IDA "
                         "recovery -> see %s" % (m["key"], mc.statefile))
    ctx.log("  module '%s': codegen OK (jump tables recovered) -> generated/%s"
            % (m["key"], m["key"]))


def _relocate_colliding_tables(ctx, mods):
    """Multi-XEX: the runtime places each module's function-pointer dispatch table
    at image_base + image_size by default. When a companion's image loads right
    after the main's (FIFA Street: main [0x82000000,0x821C0000) + companion at
    0x82300000), the MAIN's table [0x821C0000,~0x82413000) overlaps the companion
    image -> InitializeFunctionTable fails -> the companion's functions never
    register -> FATAL on the first inter-module call. Detect that collision from
    the generated init.h ranges and author an explicit `function_table_base` into
    the main manifest: a free 64KiB-aligned guest VA above everything (must stay
    inside the v80000000 heap, < 0x90000000). The SDK's codegen emits
    REX_FUNC_TABLE_BASE + PPCImageInfo.function_table_base only when the field is
    present, so titles without it stay byte-identical. Idempotent: an existing
    manifest value is left alone."""
    RESERVE = 0x10000  # SDK FunctionDispatcher::kThunkReserveSize
    HEAP_END = 0x90000000  # v80000000 heap upper bound (AllocFixed would fail past it)
    man = open(ctx.manifest, encoding="utf-8", errors="ignore").read()
    if re.search(r"^\s*function_table_base\s*=", man, re.M):
        return
    r = _dx.read_ranges(ctx.gen, ctx.name)
    if not r:
        return
    ib, cb, cs, isz = r
    main_tab = (ib + isz, ib + isz + (cs + RESERVE) * 2)
    spans = [(ib, ib + isz)]  # every image+table span the main table must dodge
    collide = False
    for m in mods:
        mr = _dx.read_ranges(os.path.join(ctx.port, "generated", m["key"]), m["key"])
        if not mr:
            continue
        mib, mcb, mcs, misz = mr
        mod_img = (mib, mib + misz)
        mod_tab = (mib + misz, mib + misz + (mcs + RESERVE) * 2)
        spans += [mod_img, mod_tab]
        for lo, hi in (mod_img, mod_tab):
            if main_tab[0] < hi and main_tab[1] > lo:
                collide = True
    if not collide:
        return
    base = max(hi for _, hi in spans + [main_tab])
    base = (base + 0xFFFF) & ~0xFFFF
    if base + (main_tab[1] - main_tab[0]) > HEAP_END:
        ctx.log("WARNING: no free guest VA below 0x%X for the main function table "
                "-> leaving default (companion dispatch will fail)" % HEAP_END)
        return
    man = re.sub(r"(\[entrypoint\]\s*\n)",
                 "\\g<1>function_table_base = 0x%X\n" % base, man, count=1)
    open(ctx.manifest, "w", encoding="utf-8").write(man)
    ctx.log("  main function table would collide with a companion module -> "
            "relocated to 0x%X (function_table_base authored into the manifest)" % base)


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
        _codegen_module(ctx, m)
    if mods:
        _relocate_colliding_tables(ctx, mods)
    # extra-module codegen rewrites generated/rexglue.cmake to point at the last extra;
    # the entrypoint codegen in the build loop runs AFTER this and restores it to default.
    if mods:
        _inject_extra_modules_into_cmake(ctx, mods)
    _inject_app_glue(ctx, mods, glue)
    _inject_pch_into_cmake(ctx)


def stage_deepextract(ctx, gen_current=False):
    """Static function/vtable recovery: a deep IDA pass on the .i64 the jumptables stage
    produced harvests the function/vtable-target set the linear scan misses (~96% of what
    run-heal would otherwise find by launching the game N times), and the pure-addition
    gate folds only the provably-safe ones into functions.toml BEFORE the first build.
    run-heal stays as the backstop for the genuinely-dynamic residue. Fully additive and
    opt-in on IDA: no idat / no .i64 -> skip (byte-identical to before)."""
    if not (ctx.env.get("idat") and ctx.env.get("jt_repo") and ctx.env.get("python")):
        ctx.log("deep-extract: no IDA/repo/python -> skip (run-heal covers it)")
        return ctx.mark("deepextract", {"skipped": "no-ida"})
    i64 = os.path.join(ctx.work, "%s_image.bin.elf.i64" % ctx.name)
    script = os.path.join(ctx.env["jt_repo"], "src", "deep_extract.py")
    ranges = _dx.read_ranges(ctx.gen, ctx.name)
    if not os.path.exists(i64) or not os.path.exists(script) or not ranges:
        ctx.log("deep-extract: no .i64/script/ranges -> skip (run-heal covers it)")
        return ctx.mark("deepextract", {"skipped": "no-i64-or-ranges"})
    ib, cb, cs, isz = ranges
    funclist = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    # The known-set must reflect the CURRENT generated sources. For a companion
    # module, stage_jumptables ran before any sources existed (step 2 of
    # _codegen_module) and wrote an EMPTY funclist; feeding that to deep_extract
    # made every already-emitted function a "candidate" (fifadllzf: 92188) and
    # the pure-add gate rightly rejected the lot -- accepted=0, deep-extract a
    # no-op for every companion, real cures (FIFA 0x827838A0) discarded with the
    # noise. Refresh via the same extract_funcs the jumptables stage uses
    # whenever the list is missing/empty but sources exist; healthy entrypoints
    # (non-empty list) are untouched.
    if (not os.path.exists(funclist) or os.path.getsize(funclist) == 0) \
            and os.path.exists(os.path.join(ctx.gen, "sources.cmake")):
        rf = run([ctx.env["python"], os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
                  ctx.gen, "-o", funclist])
        n = sum(1 for l in open(funclist) if l.strip()) if os.path.exists(funclist) else 0
        ctx.log("deep-extract: refreshed empty funclist from generated sources (%d entries)" % n)
    workcopy = os.path.join(ctx.work, "%s_deepx.i64" % ctx.name)  # NEVER open the original
    cfg = os.path.join(ctx.work, "%s_deepx_cfg.json" % ctx.name)
    outjson = os.path.join(ctx.work, "%s_deepx.json" % ctx.name)
    outtoml = os.path.join(ctx.work, "%s_deepx.toml" % ctx.name)
    shutil.copyfile(i64, workcopy)
    p = lambda x: x.replace("\\", "/")
    json.dump({"image_base": ib, "text_start": cb, "text_end": cb + cs, "image_end": ib + isz,
               "known": p(funclist), "out_toml": p(outtoml), "out_json": p(outjson)},
              open(cfg, "w"))
    ctx.log("deep IDA extraction (funcmap + vtable data-xref) on a .i64 copy")
    if os.path.exists(outjson):
        os.remove(outjson)
    run([ctx.env["idat"], "-A", "-S%s %s" % (p(script), p(cfg)),
         "-L" + p(os.path.join(ctx.work, "%s_deepx_ida.log" % ctx.name)), workcopy])
    if not os.path.exists(outjson):
        ctx.log("deep-extract: IDA produced nothing -> skip")
        return ctx.mark("deepextract", {"skipped": "extract-empty"})
    cands = sorted(set(int(x["addr"], 16) for x in json.load(open(outjson)).get("emitted", []))
                   - set(_heal.load_overrides_full(ctx.functions)))
    if not cands:
        return ctx.mark("deepextract", {"candidates": 0, "accepted": 0})
    ctx.log("deep-extract: %d candidates -> pure-addition gate" % len(cands))
    accepted = _dx.pure_add_gate(
        ctx.env["rexglue"], ctx.port, ctx.name, ctx.manifest, ctx.gen, ctx.functions, cands,
        codegen_fn=lambda: rexglue(ctx, "--log-level", "error", "codegen", ctx.manifest,
                                   capture=True),
        log=ctx.log, baseline_current=gen_current)
    if accepted:
        _heal.register_functions(accepted, ctx.functions)  # additive {} superset-only
    ctx.log("deep-extract: +%d functions folded (pure additions); %d dropped, run-heal backstops the rest"
            % (len(accepted), len(cands) - len(accepted)))
    return ctx.mark("deepextract", {"candidates": len(cands), "accepted": len(accepted)})


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
    oom_parallel = None
    skip_codegen = False
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        ctx.log("codegen + build (attempt %d/%d)" % (attempt, MAX_BUILD_ATTEMPTS))
        if skip_codegen:
            skip_codegen = False  # OOM retry: generated/ is already current
        else:
            do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        txt = _heal._read_text(logp)
        if rc == 0 and os.path.exists(ctx.exe):
            write_game_root(ctx)
            ctx.log("build OK -> %s" % ctx.exe)
            return ctx.mark("build", {"exe": ctx.exe})
        if "LLVM ERROR: out of memory" in txt:
            # Giant-module TUs (~2MB generated C++ each + a multi-MB PCH) at the
            # default parallelism (cores+2 = 18 concurrent clangs) can exceed
            # physical RAM -- fifadllzf hit this twice on 31GB (build died at
            # 214/215). The objs already built persist, so retrying the
            # INCREMENTAL build at reduced -j only recompiles the OOM'd tail.
            # Halve until 4; the bat keeps the reduced value for the rest of
            # this pipeline (heal-round rebuilds inherit the safe -j).
            oom_parallel = max(4, (oom_parallel or ctx.load_state().get("build_parallel") or 18) // 2)
            ctx.mark("build_parallel", oom_parallel)  # persistent lesson (write_build_bat reads it)
            bat = write_build_bat(ctx, parallel=oom_parallel)
            skip_codegen = True  # generated/ didn't change; only the build OOM'd
            ctx.log("  clang OUT OF MEMORY (too many concurrent frontends) -> "
                    "retrying incrementally with --parallel %d" % oom_parallel)
            continue
        if "use of undeclared label" in txt:
            # Two undeclared-label classes: (a) a jump-table landing the SDK's heuristic
            # under-recovered -> force the SDK to recover it as an in-function block
            # (keeps the routine whole, e.g. Gears' decompressor loop); (b) a genuine
            # mid-flow function split -> extend the owning function's end. Apply both;
            # they partition the case space, so this converges either kind.
            landings = _heal.forced_landings_from_log(logp)
            nf = _heal.write_forced(ctx.forced, landings)
            if nf:
                _heal.ensure_manifest_include(ctx.manifest, os.path.basename(ctx.forced))
            nb = _heal.heal_boundaries(logp, ctx.gen, ctx.functions)
            ends = tuple(sorted((a, e) for a, e in _heal.load_overrides(ctx.functions).items() if e))
            state = (tuple(sorted(_heal.load_forced(ctx.forced))), ends)
            if (nf + nb) == 0 or state == last_ends:
                ctx.log("  undeclared-label heal not converging (no new fix) -> see %s" % logp)
                break
            last_ends = state
            ctx.log("  jump-table landing heal -> +%d forced landing(s), +%d boundary fix(es); rebuilding"
                    % (nf, nb))
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


def _autoplay_thread(proc, stop_evt):
    """Press menu-advance keys (Enter=START, Space=A -- the MnK driver defaults)
    every few seconds so title/menu screens advance unattended, and heal windows
    exercise menu->deeper code instead of idling on PRESS START.
    IMPLEMENTATION MATTERS: the runtime window is SDL3, which maps keys by
    HARDWARE SCANCODE -- keybd_event(vk, scan=0) arrives as scancode 0 and SDL
    sees nothing (the first version of this was invisible to every game). Use
    SendInput with KEYEVENTF_SCANCODE (Enter=0x1C, Space=0x39) and force the
    game window to the foreground first (found by pid; SDL only receives key
    events with focus). Opt out with REXAUTO_NO_AUTOPLAY=1."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class INPUT(ctypes.Structure):
        class U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 40)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", U)]

    INPUT_KEYBOARD = 1
    KEYEVENTF_SCANCODE = 0x0008
    KEYEVENTF_KEYUP = 0x0002

    def press_scan(scan):
        down = INPUT(type=INPUT_KEYBOARD)
        down.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE, 0, 0)
        up = INPUT(type=INPUT_KEYBOARD)
        up.ki = KEYBDINPUT(0, scan, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP, 0, 0)
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))

    def find_game_hwnd():
        target = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, lparam):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == proc.pid and user32.IsWindowVisible(hwnd):
                target.append(hwnd)
                return False
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return target[0] if target else None

    SC_ENTER, SC_SPACE = 0x1C, 0x39
    t0 = time.time()
    while not stop_evt.is_set() and proc.poll() is None:
        if time.time() - t0 > 15:  # boot/intro grace
            hwnd = find_game_hwnd()
            if hwnd:
                fg = user32.GetForegroundWindow()
                if fg != hwnd:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.3)
                if user32.GetForegroundWindow() == hwnd:
                    for scan in (SC_ENTER, SC_SPACE, SC_ENTER):
                        if stop_evt.is_set() or proc.poll() is not None:
                            break
                        press_scan(scan)
                        time.sleep(0.9)
        stop_evt.wait(2.5)


def run_once(ctx, seconds, discover=False):
    """Launch the game, let it run, kill it; return (newest-this-launch log text, alive).
    discover=True sets REX_HEAL_DISCOVER so the runtime logs+continues on each
    unregistered indirect target (surfacing many in one run) instead of aborting."""
    logdir = os.path.join(ctx.builddir, "logs")
    before = set(glob.glob(os.path.join(logdir, "*.log")))
    t0 = time.time()
    env = dict(os.environ)
    if discover:
        env["REX_HEAL_DISCOVER"] = "1"
    # Runtime-side autoplay: the MnK driver synthesizes periodic START/A presses
    # (REX_AUTOPLAY, SDK mnk_input_driver.cpp) so unattended windows advance
    # title/menu screens. Works without window focus -- OS-level key injection
    # (the first two attempts) was unreliable: SDL maps by scancode, background
    # processes can't steal foreground, and GetState zeroes input when unfocused.
    if not os.environ.get("REXAUTO_NO_AUTOPLAY"):
        env["REX_AUTOPLAY"] = "1"
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
    """([lo, hi), exact) of the entrypoint module's generated code, for filtering
    discovered targets down to plausible function addresses. exact=False means the
    wide fallback window -- fine for heal filtering, NOT precise enough to persist a
    verified-forever receipt on (in-image DATA addresses would pass as "in code").
    NOTE: ctx.gen already ends in generated/default -- an extra "default" segment
    here used to make the open() always fail, silently pinning every game to the
    fallback window (adversarial review catch)."""
    import re
    try:
        h = open(os.path.join(ctx.gen, ctx.name + "_init.h")).read()
        b = int(re.search(r"REX_CODE_BASE\s+0x([0-9A-Fa-f]+)", h).group(1), 16)
        s = int(re.search(r"REX_CODE_SIZE\s+0x([0-9A-Fa-f]+)", h).group(1), 16)
        return b, b + s, True
    except Exception:
        return 0x82000000, 0x84000000, False


def _prev_list_function(ctx, addr):
    """Largest functions-list start strictly below addr (None if unavailable or
    addr itself is a list entry). Used to find the neighbour whose emitted body
    absorbed a gap containing addr."""
    import bisect
    path = os.path.join(ctx.work, "%s_functions_list.txt" % ctx.name)
    if not os.path.exists(path):
        return None
    try:
        starts = sorted({int(l, 16) for l in open(path) if l.strip()})
    except ValueError:
        return None
    i = bisect.bisect_left(starts, addr)
    if i < len(starts) and starts[i] == addr:
        return None  # addr is a known start; overlap is not the story here
    return starts[i - 1] if i > 0 else None


def _runheal_fingerprint(ctx):
    """What a convergence verdict is actually a property of: the exact game exe, the
    runtime DLL it loads, AND the guest image it executes (xex + staged title-update
    + which game root) -- the runtime re-reads those at every launch, so behavior can
    change while exe+dll stay identical (adversarial review catch). A cure-toml/SDK/
    codegen change flows into the exe hash; a re-rip/TU/game-swap flows into these."""
    dll = os.path.join(ctx.builddir, "rexruntime.dll")
    try:
        return {"exe": _sha256(ctx.exe),
                "runtime": _sha256(dll) if os.path.exists(dll) else "",
                "image": _sha256(ctx.xex) if ctx.xex and os.path.exists(ctx.xex) else "",
                "tu": _sha256(ctx.tu_xexp) if ctx.tu_xexp and os.path.exists(ctx.tu_xexp) else "",
                "game": ctx.game or ""}
    except Exception:
        return None


def _autodetect_companions(ctx, log_text, targets):
    """Zero-touch multi-XEX: when a production run fatals on addresses OUTSIDE
    every recompiled module, find which guest-loaded companion XEX contains them.
    The runtime log records, at load time, a '<file>.dllp / .xexp' patch probe
    immediately before each 'XEX image loaded at LO-HI' line -- pairing the two
    yields (module path, image range). A fatal target inside a companion's range
    + the file on disk being XEX2 => author it into port/<name>_modules.toml,
    where stage_build's setup_extra_modules recompiles it through the full IDA
    pipeline (v2.18). Returns the newly-authored module dicts; [] when nothing
    new (already-declared companions are never re-authored -> the caller's
    anti-loop: a companion that still fatals AFTER recompilation falls through
    to the honest production_fatal verdict)."""
    loads, last_probe = [], None
    for ln in log_text.splitlines():
        m = re.search(r"entry not found for '([^']+\.(?:xex|dll|exe))p'", ln, re.I)
        if m:
            last_probe = m.group(1)
            continue
        m = re.search(r"XEX image loaded at ([0-9A-Fa-f]{8})-([0-9A-Fa-f]{8})", ln)
        if m:
            if last_probe:
                rel = re.sub(r"^\\Device\\[^\\]+\\[^\\]+\\", "", last_probe)
                loads.append((rel, int(m.group(1), 16), int(m.group(2), 16)))
            last_probe = None
    existing_mods = extra_modules(ctx)
    existing_paths = {os.path.normcase(m["xex"]) for m in existing_mods}
    existing_keys = {m["key"] for m in existing_mods} | {ctx.name}
    newmods, seen = [], set()
    for a in targets:
        for rel, mlo, mhi in loads:
            if not (mlo <= a < mhi) or rel.lower() == "default.xex" or rel in seen:
                continue
            path = os.path.join(ctx.game, rel.replace("\\", os.sep))
            if os.path.normcase(path) in existing_paths:
                continue
            try:
                if open(path, "rb").read(4) != b"XEX2":
                    continue
            except OSError:
                continue
            key = re.sub(r"[^a-z0-9]", "", os.path.splitext(os.path.basename(rel))[0].lower()) or "mod"
            if key[0].isdigit():
                key = "m" + key
            while key in existing_keys:
                key += "x"
            existing_keys.add(key)
            seen.add(rel)
            newmods.append({"key": key, "rel": rel.replace("\\", "/"),
                            "lo": mlo, "hi": mhi})
    if not newmods:
        return []
    cfgp = os.path.join(ctx.port, "%s_modules.toml" % ctx.name)
    body = open(cfgp, encoding="utf-8", errors="ignore").read() if os.path.exists(cfgp) else (
        "# Extra recompilable guest modules beyond the entrypoint -- AUTO-DETECTED by\n"
        "# rexauto run-heal: a production run fataled on calls landing inside these\n"
        "# guest-loaded companion XEX images (probe + 'XEX image loaded' log pairs).\n")
    for m in newmods:
        body += ('\n[[modules]]\nkey = "%s"\nname = "%s"\nxex = "%s"\n'
                 'symbol_prefix = "%s_"\n' % (m["key"], m["key"], m["rel"], m["key"]))
        ctx.log("  companion XEX auto-detected: %s @ 0x%X-0x%X (fatal target inside) "
                "-> authored into %s" % (m["rel"], m["lo"], m["hi"], os.path.basename(cfgp)))
    open(cfgp, "w", encoding="utf-8").write(body)
    return newmods


def stage_runheal(ctx):
    bat = write_build_bat(ctx)
    lo, hi, range_exact = _code_range(ctx)
    # Multi-XEX: know each extra module's code range so an invalid-function target
    # inside a companion (e.g. Spider-Man's GameLogic.dll at 0x88080000) is healed
    # in THAT module's functions.toml + a module re-codegen, instead of being
    # written off as "uncurable/out-of-image". Empty for single-module titles ->
    # behavior byte-identical to before.
    mod_heal = []
    for m in extra_modules(ctx):
        mc = _module_view(ctx, m)
        mlo, mhi, mexact = _code_range(mc)
        if mexact:
            mod_heal.append((mc, mlo, mhi))

    def _partition(logged):
        """(main_addrs, [(module_view, addrs)], uncurable) by owning code range."""
        main = [a for a in logged if lo <= a < hi and (a & 3) == 0]
        seen = set(main)
        hits = []
        for mc, mlo, mhi in mod_heal:
            ma = [a for a in logged if a not in seen and mlo <= a < mhi and (a & 3) == 0]
            if ma:
                seen.update(ma)
                hits.append((mc, ma))
        return main, hits, [a for a in logged if a not in seen]
    if not os.path.exists(ctx.exe):
        # A failure must FAIL (SystemExit, like stage_build) -- a truthy mark would
        # make the next plain pipeline run print "skip runheal (done)" and never
        # re-attempt verification (adversarial review catch).
        raise SystemExit("[rexauto] runheal: no exe at %s -- run the build stage first" % ctx.exe)
    rcpt_path = os.path.join(ctx.port, "%s_runheal_receipt.json" % ctx.name)
    # Confirm/discover window floors at 360s. The short heal ROUNDS stay fast
    # (ctx.args.run_seconds, ~22s), but the initial discover pass and the final
    # convergence check run this long so late-loading indirect targets are caught
    # up front instead of surfacing as a crash mid-gameplay. Gears of War Judgment
    # loads sub_824CA490 only ~71s in (past the old 47s window) -> it converged
    # "clean" then FATAL'd in play; a wide window heals it in the same pass. Some
    # titles (565507E4 Crash of the Titans) have a long green-thread-paced loading
    # phase (~2min) before the first gameplay indirect calls surface, so the floor
    # is 360s to reach past loading into actual play.
    confirm_seconds = max(ctx.args.run_seconds * 2, ctx.args.run_seconds + 25, 360)
    # --- Tier 0: convergence receipt = ZERO launches --------------------------
    # A "converged" verdict is a property of the binaries + guest image that ran.
    # Persist it keyed by their hashes: when the same set comes around again (a
    # pipeline re-run, --from build with no change, a GUI reopen) there is nothing
    # new to learn from launching the game, so don't. Honored only if it was
    # verified with a window at least as long as the one requested now. Delete the
    # receipt (or set REXAUTO_FORCE_RUNHEAL=1) to force a live check.
    fp = _runheal_fingerprint(ctx)
    try:
        rcpt = json.load(open(rcpt_path)) if os.path.exists(rcpt_path) else None
    except Exception:
        rcpt = None
    if os.environ.get("REXAUTO_FORCE_RUNHEAL"):
        rcpt = None
    if fp and rcpt and rcpt.get("fingerprint") == fp \
            and rcpt.get("seconds", 0) >= confirm_seconds:
        ctx.log("runheal: receipt matches the current exe+runtime+image -> already "
                "verified (%s); not launching the game (delete %s to re-verify)"
                % (rcpt.get("verdict", "converged"), os.path.basename(rcpt_path)))
        if getattr(ctx.args, "publish_gabarito", False):
            publish_gabarito(ctx)  # cures are on disk; publishing needs no launch
        return ctx.mark("runheal", {"receipt": True, "verdict": rcpt.get("verdict")})
    # --- Tier 1: minimal launches decide ---------------------------------------
    # Discover mode (REX_HEAL_DISCOVER): the runtime logs+continues on each
    # unregistered indirect target instead of aborting, so one run surfaces MANY
    # missing functions at once. The key property: if a discover run logs ZERO
    # targets AT ALL, no call was ever no-op'd, so the execution was identical to
    # a clean run -- that run doubles as the convergence confirmation (zero
    # *logged*, not zero *in-range*: an out-of-image/misaligned no-op'd call means
    # a production run FATALs there, so it must never mint a "survived" verdict --
    # adversarial review catch). This collapses the old discover(22s)xN ->
    # fatal(22s) -> confirm(47s) dance: a cured re-port launches ONCE, and the
    # receipt makes the next pipeline run launch ZERO times. Guards that stay:
    #  * long window on the deciding run (rayman crashed at 0x82162208 ~1s past a
    #    22s window after "converging" on the short one); heal rounds in between
    #    keep the short window for fast bulk iteration.
    #  * the deciding run must not be the port's first-ever launch: first boot
    #    creates saves/caches, and load-existing-state code paths (the v2.6.0
    #    xam_content crash class) only execute on the SECOND boot. A clean
    #    first-ever launch primes state; the next clean run decides.
    #  * a launch that produced no log is no evidence -- never converge on it.
    # "primed" = the CURRENT guest state (image+TU+game root) has been booted at
    # least once, so saves/caches exist and second-boot code paths are reachable.
    # Keyed to the guest fingerprint, not bare log existence: stale logs from a
    # previous game root must not skip the priming run (adversarial review catch).
    primed_path = os.path.join(ctx.port, "%s_runheal_primed.json" % ctx.name)
    guest_fp = {k: fp[k] for k in ("image", "tu", "game")} if fp else None
    try:
        primed = guest_fp is not None and json.load(open(primed_path)) == guest_fp
    except Exception:
        primed = False
    window = confirm_seconds
    resynced = set()  # addresses we've already forced a clean relink for (anti-loop)
    shrunk = set()    # containing functions we've already end-shrunk (anti-loop)
    for it in range(1, ctx.args.heal_iters + 1):
        primed_at_launch = primed
        txt, alive = run_once(ctx, window, discover=True)
        primed = True
        if guest_fp and not primed_at_launch:
            try:
                json.dump(guest_fp, open(primed_path, "w"), indent=1)
            except Exception:
                pass
        if not txt:
            # No log = no evidence either way. FAIL (not mark): a truthy mark would
            # make the next plain run skip the stage as "done" forever.
            raise SystemExit("[rexauto] runheal: launch produced no log -- fix the "
                             "launch environment and re-run")
        # Range-filter: a logged target can be OUTSIDE this module's recompiled
        # code range (e.g. a call into a companion XEX a multi-XEX title loads at
        # 0x88000000+). Registering such an out-of-image address as a {} function
        # corrupts the port -- it killed sonic_adventure's boot (a stray
        # "0x88610000" = {}). Only heal targets that live in this image; report
        # (never "cure") the rest.
        logged = _heal.invalid_functions_ordered(txt)
        log_text = txt  # freshest runtime log (for companion auto-detection)
        # Codegen-baked "Unresolved call from X to Y" fatals: the branch target is
        # neither a discovered function nor a recovered landing, so the generated
        # code traps unconditionally -- launching again can never cure it. Force
        # the target as an in-function landing in the OWNING module (never a {}
        # split: the forced-landings lesson) and rebuild. crash_mind_over_mutant
        # sat through 4 identical runs on this class; Forza Horizon hit it at
        # 0x830ED910 mid-boot.
        ub = _heal.unresolved_branches_from_runtime(txt)
        if ub:
            forced_new = 0
            for owner, olo, ohi in [(ctx, lo, hi)] + [(mc, mlo, mhi) for mc, mlo, mhi in mod_heal]:
                mine = [a for a in ub if olo <= a < ohi]
                if not mine:
                    continue
                # register_or_seed routes each target correctly: a landing INSIDE
                # an existing function -> forced_landings (keeps the routine
                # whole); a target in an override GAP -> a {} FunctionNode so
                # graph().getFunction() is non-null and build_b lowers the branch
                # to a real tail call. A forced-landing alone never creates the
                # node, so gap targets (crash_mind_over_mutant 0x82476040) stayed
                # unresolved and re-fataled every run.
                nr, ns = _heal.register_or_seed(mine, owner.functions, owner.forced, owner.switches)
                if ns:
                    _heal.ensure_manifest_include(owner.manifest, os.path.basename(owner.forced))
                if nr + ns:
                    owner.log("  %d unresolved-branch target(s) cured (%d fn, %d landing): %s; rebuilding"
                              % (nr + ns, nr, ns, ", ".join("0x%X" % a for a in mine)))
                    do_codegen(owner)
                    forced_new += nr + ns
            if forced_new:
                do_codegen(ctx)  # no-op for main-only fixes; restores rexglue.cmake after module codegen
                logp, rc = do_build(ctx, bat)
                if rc != 0 or not os.path.exists(ctx.exe):
                    raise SystemExit("[rexauto] runheal: rebuild failed after forcing %d "
                                     "unresolved-branch landing(s) -> see %s" % (forced_new, logp))
                window = ctx.args.run_seconds
                continue
        addrs, mod_hits, uncurable = _partition(logged)
        if (addrs or mod_hits) and uncurable:
            # Corrupted-continuation guard: after the first no-op'd uncurable call
            # the run executes with corrupt state, so in-range targets logged in
            # the SAME run may be garbage that register_or_seed would enshrine.
            # One fatal-mode run gives ground truth (it aborts at the first
            # invalid target, so everything it logs precedes any corruption).
            # SAME window as the discover run (a shorter one would miss targets
            # first reached late and mint a false "uncurable" verdict); BOTH lists
            # are recomputed from the ground-truth log; a clean fatal run where
            # discover saw targets is timing nondeterminism -> inconclusive,
            # re-observe instead of deciding (adversarial review catches).
            ctx.log("  %d uncurable no-op'd target(s) alongside %d curable -> "
                    "re-reading ground truth with one fatal-mode run"
                    % (len(uncurable), len(addrs)))
            txt2, _ = run_once(ctx, window)
            if not txt2:
                raise SystemExit("[rexauto] runheal: ground-truth launch produced no "
                                 "log -- fix the launch environment and re-run")
            logged2 = _heal.invalid_functions_ordered(txt2)
            if not logged2:
                ctx.log("  fatal-mode run logged nothing (timing nondeterminism); re-observing")
                continue
            log_text = txt2
            addrs, mod_hits, uncurable = _partition(logged2)
        if not addrs and not mod_hits:
            if uncurable:
                # Zero-touch multi-XEX: the fatal may be a call into a companion
                # XEX the guest loaded but we never recompiled. Detect it from
                # this run's own log (probe + "XEX image loaded" pairs), author
                # it into <name>_modules.toml, rebuild (stage_build runs the new
                # module through the full IDA pipeline), and keep healing. Only
                # modules NOT already declared are authored, so a companion that
                # STILL fatals after recompilation falls through to the honest
                # verdict below instead of looping.
                newmods = _autodetect_companions(ctx, log_text, uncurable)
                if newmods:
                    ctx.log("  %d companion XEX(s) auto-detected -> rebuilding with "
                            "them recompiled" % len(newmods))
                    stage_build(ctx)
                    known = {mc.name for mc, _, _ in mod_heal}
                    for m in extra_modules(ctx):
                        mc = _module_view(ctx, m)
                        mlo, mhi, mexact = _code_range(mc)
                        if mexact and mc.name not in known:
                            mod_heal.append((mc, mlo, mhi))
                    continue
                # Honest non-convergence: discover mode no-op'd calls that a
                # production run FATALs on; nothing in THIS module cures them.
                verdict = ("recompilation of this module found no curable targets, but "
                           "%d uncurable target(s) were no-op'd (out-of-image/misaligned,"
                           " e.g. 0x%X) -- a production run FATALs there (companion XEX?)"
                           % (len(uncurable), uncurable[0]))
                ctx.log("run-heal: %s" % verdict)
                if getattr(ctx.args, "publish_gabarito", False):
                    publish_gabarito(ctx)
                # "alive" records what was OBSERVED (discover mode no-ops the calls,
                # so the game may well be alive); the prediction lives in its own key.
                return ctx.mark("runheal", {"iters": it, "alive": alive,
                                            "production_fatal": True,
                                            "uncurable": ["0x%X" % a for a in uncurable[:8]]})
            if window != confirm_seconds:
                ctx.log("  clean at %ds; stretching to the %ds confirm window"
                        % (window, confirm_seconds))
                window = confirm_seconds
                continue
            if not primed_at_launch:
                ctx.log("  clean first-ever launch primed saves/caches; re-running once "
                        "against existing state (second-boot code paths)")
                continue
            verdict = ("survived %ds with no invalid-function fatal" % confirm_seconds) if alive \
                else "exited without an invalid-function fatal (other stop - likely GPU/runtime)"
            ctx.log("run-heal converged in %d launch(es): %s" % (it, verdict))
            if getattr(ctx.args, "publish_gabarito", False):
                publish_gabarito(ctx)
            # The receipt is only minted on POSITIVE evidence: the game was still
            # alive at window end (an early "other stop" exit may be transient --
            # driver, GPU wall -- and is cheap to re-verify precisely because it
            # exits early) and the real code range was known (the fallback window
            # would let in-image DATA addresses masquerade as verified code).
            if alive and range_exact:
                fp = _runheal_fingerprint(ctx)  # recompute: heal rounds relinked the exe
                if fp:
                    json.dump({"fingerprint": fp, "verdict": verdict,
                               "seconds": confirm_seconds, "launches": it},
                              open(rcpt_path, "w"), indent=1)
            return ctx.mark("runheal", {"iters": it, "alive": alive,
                                        "confirmed_seconds": confirm_seconds})
        n = 0
        if addrs:
            n_reg, n_seed = _heal.register_or_seed(addrs, ctx.functions, ctx.forced, ctx.switches)
            if n_seed:
                _heal.ensure_manifest_include(ctx.manifest, os.path.basename(ctx.forced))
            n = n_reg + n_seed
            ctx.log("heal round %d: target(s) @ %s -> +%d (%d fn, %d landing); rebuilding"
                    % (it, ",".join("0x%X" % a for a in addrs), n, n_reg, n_seed))
        # Targets owned by an extra module: cure in ITS functions.toml and re-codegen
        # that module (its objects relink into the same exe in the shared rebuild below).
        for mc, ma in mod_hits:
            mr, ms = _heal.register_or_seed(ma, mc.functions, mc.forced, mc.switches)
            if ms:
                _heal.ensure_manifest_include(mc.manifest, os.path.basename(mc.forced))
            n += mr + ms
            mc.log("heal round %d: target(s) @ %s -> +%d (%d fn, %d landing); re-codegen module"
                   % (it, ",".join("0x%X" % a for a in ma), mr + ms, mr, ms))
            do_codegen(mc)
        window = ctx.args.run_seconds  # short fast rounds while targets keep coming;
        # the final clean round stretches back to confirm_seconds before converging.
        if n == 0:
            # register_or_seed added nothing -> addrs[0] is ALREADY registered in the
            # current sources. But the *running exe* can lag the codegen: an earlier
            # codegen (deep-extract gate churn, or a prior no-op heal) leaves
            # register.cpp newer than the linked exe, so the built exe's dispatch tables
            # never got SetFunction(addr) -> a SPURIOUS "unregistered" fatal on a
            # function that source-registers fine. This exact case made dbz look like an
            # unfixable runtime wall at 0x82415F90 when a plain relink converged it.
            # Force one codegen+relink to resync the exe, then re-run. Only if the same
            # address STILL fatals after a clean relink is it a genuine wall.
            a0 = (addrs + [a for _, ma in mod_hits for a in ma])[0]
            if a0 not in resynced:
                resynced.add(a0)
                ctx.log("  0x%X already registered but still flagged -> resync exe "
                        "(codegen may be newer than the linked exe) and retry" % a0)
                do_codegen(ctx)
                logp, rc = do_build(ctx, bat)
                if rc == 0 and os.path.exists(ctx.exe):
                    continue  # next iteration re-runs against the resynced exe
                ctx.log("  resync rebuild failed -> %s" % logp)
            # Boundary overlap: the address IS registered but codegen ignores the
            # override because a NEIGHBOUR's emitted body extends across it (the
            # scanner absorbed a functions-list gap). Seen as vtable-thunk tables:
            # Captain America 0x822A2040 is a 16-byte virtual-call thunk absorbed
            # into 0x822A2010's body. The runtime just indirect-called the address,
            # so it IS a true entry point -> shrink the containing function with an
            # end-override at the target and re-codegen. Fires only on this exact
            # class (registered + survives resync + a prior list entry spans it).
            # Owner-aware: a module-range a0 shrinks in THAT module's functions.toml
            # (Halo 3 waveslib 0x8A061018 was this class); its funclist is refreshed
            # first -- module funclists are written PRE-emit (0 functions), so the
            # neighbour bisect needs a post-emit regeneration.
            owner = ctx if lo <= a0 < hi else None
            if owner is None:
                for omc, omlo, omhi in mod_heal:
                    if omlo <= a0 < omhi:
                        owner = omc
                        if ctx.env.get("python") and ctx.env.get("jt_repo"):
                            run([ctx.env["python"],
                                 os.path.join(ctx.env["jt_repo"], "src", "extract_funcs.py"),
                                 omc.gen, "-o",
                                 os.path.join(omc.work, "%s_functions_list.txt" % omc.name)])
                        break
            prev = _prev_list_function(owner, a0) if owner is not None else None
            if prev is not None and prev not in shrunk:
                shrunk.add(prev)
                ov = _heal.load_overrides_full(owner.functions)
                cur = ov.get(prev) or {}
                if cur.get("end") is None or cur["end"] > a0:
                    cur["end"] = a0
                    ov[prev] = cur
                    _heal.write_overrides_full(owner.functions, ov)
                    owner.log("  0x%X lies inside 0x%X's emitted body -> shrink it with "
                              "end=0x%X and retry (absorbed-gap/vtable-thunk class)" % (a0, prev, a0))
                    do_codegen(owner)
                    if owner is not ctx:
                        do_codegen(ctx)  # restore generated/rexglue.cmake to the entrypoint
                    logp, rc = do_build(ctx, bat)
                    if rc == 0 and os.path.exists(ctx.exe):
                        continue
                    ctx.log("  shrink rebuild failed -> %s" % logp)
            ctx.log("  stuck on 0x%X (already registered, survives resync) — needs a closer look" % a0)
            return ctx.mark("runheal", {"stuck": "0x%X" % a0})
        do_codegen(ctx)
        logp, rc = do_build(ctx, bat)
        # Label-heal to convergence (rc re-checked each pass -- a 2-deep cascade used
        # to dead-end silently) + ONE plain retry for transient failures (e.g. the
        # relink racing the just-killed game process still holding the exe); a second
        # consecutive non-label failure is a real break -- stop burning rebuilds.
        plain_fails = 0
        for _pass in range(4):
            if rc == 0 and os.path.exists(ctx.exe):
                break
            _txt = _heal._read_text(logp)
            if "LLVM ERROR: out of memory" in _txt:
                # Same auto-fix as stage_build: halve -j, persist the lesson,
                # retry incrementally (objs persist; generated/ unchanged).
                plain_fails = 0
                _oomj = max(4, (ctx.load_state().get("build_parallel") or 18) // 2)
                ctx.mark("build_parallel", _oomj)
                bat = write_build_bat(ctx, parallel=_oomj)
                ctx.log("  clang OUT OF MEMORY in heal rebuild -> retrying with --parallel %d" % _oomj)
            elif "use of undeclared label" in _txt:
                plain_fails = 0
                if _heal.write_forced(ctx.forced, _heal.forced_landings_from_log(logp)):
                    _heal.ensure_manifest_include(ctx.manifest, os.path.basename(ctx.forced))
                _heal.heal_boundaries(logp, ctx.gen, ctx.functions)
                do_codegen(ctx)
            else:
                plain_fails += 1
                if plain_fails >= 2:
                    break
            logp, rc = do_build(ctx, bat)
        if rc != 0 or not os.path.exists(ctx.exe):
            raise SystemExit("[rexauto] runheal: rebuild failed after registering %d "
                             "target(s) -> see %s" % (len(addrs), logp))
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
    # v2.0 (SDK commit b363c08): three runtime fixes from the SVR07 (Yukes) crack —
    # (1) FPSCR host-thread MXCSR mask leak -> no more spurious STATUS_FLOAT_INEXACT_RESULT
    # on host-thread guest dispatch (fleet-wide); (2) writable cache: VFS mount; (3) xenia
    # ranged-alloc offset. Codegen untouched -> gate 10/10 byte-identical + skate3 runtime PASS.
    # v2.1 (Gears of War Judgment): CODEGEN-ONLY fix -- discoverBlocks now seeds the
    # IDA-recovered config switch_tables targets as in-function blocks (function_scanner.cpp
    # /.h + phase_discover.cpp), so a hand-written computed-goto routine the SDK's heuristic
    # detectJumpTable under-recovers (Gears sub_830AFE28, a stateful decompressor loop) emits
    # loc_ for ALL its landings and stays ONE function -- its shared-tail loop-back stays intra-
    # function (splitting the landings would sever it -> runtime FATAL). Inert where discovery
    # was already complete (visited/blockStarts guard) -> fleet codegen byte-identical. rexruntime
    # UNCHANGED (0ce11411; the runtime links no codegen) -> zero runtime-behavior change fleet-wide.
    # v2.9 (guest fibers): RUNTIME-ONLY -- XThread::Reenter + reenter_exception (same
    # mechanism as mainline xenia): KeSetCurrentStackPointers on a fiber'd thread
    # (X_KTHREAD::fiber_ptr set) unwinds the host stack to XThread::Execute and
    # re-enters guest code at the new fiber's LR; the Execute loop resolves reentry
    # addresses via ResolveIndirectFunction so mid-function resume sites flow into
    # the standard heal machinery. Gated on fiber_ptr: titles that never fiber-switch
    # (the whole pre-Korra fleet) never take the path -> runtime spot-check PASS.
    # Required by the PlatinumGames digital titles (Korra 58411447 proved live:
    # dead-at-boot -> engine up, 20 threads, input polling, rendering), Halo 3/
    # Reach/4, Forza 2 (xenia label kernel-KeSetCurrentStackPointers, 15 titles).
    # rexglue.exe UNCHANGED -> codegen byte-identical fleet-wide (gate all-blessed PASS).
    # v2.11 (codegen perf): CODEGEN-ONLY -- GapFill's cleanupAbsorbedGapFills was
    # O(gapfills x total-functions) (~1.8B probes at 42k funcs; quadratic on GTA V):
    # replaced with a walk of the existing sorted-base index. Same predicate, same
    # removal set => byte-identical (gate: blessed fleet PASS twice). 8.2s -> 31ms
    # per codegen pass on GTA-SA; bigger absolute win on every larger title and on
    # every repeated pass (setjmp/image-dump/pure-add gate/heal retries).
    # rexruntime UNCHANGED (20aec5ac).
    # v2.12 (runtime rebuild): the exploratory texture-dump-to-DDS path (a GPU
    # debug feature, cvar-gated OFF by default) was removed from the runtime; no
    # other runtime source changed (fiber HEAD afec3c0). The dll relinks to a new
    # hash (C++ links are non-reproducible) so the pin is re-generated to the
    # actually-shipped dump-free binary. Default-cvar behaviour is identical to
    # 20aec5ac -> runtime spot-check PASS, codegen (rexglue.exe) UNCHANGED.
    # v2.13 (runtime ADDITIVE): xboxkrnl_usbcam.cpp stubs enabled in the kernel
    # (the CMakeLists "TODO: lol eventually" line) -- 'Splosion Man/Ms. 'Splosion
    # Man import XUsbcam* (face-cam) and could not LINK without them; titles that
    # never call the camera never touch the stubs (pure export addition, gate
    # blessed-fleet codegen PASS + CA/Gears runtime alive). rexglue.exe UNCHANGED.
    # v2.14 (codegen 1-line, fleet-wide INTENTIONAL diff): REX_CALL_INDIRECT_FUNC
    # in the generated init.h now writes ctx.last_indirect_target UNCONDITIONALLY.
    # Unregistered slots hold the InvalidFunctionTrap (non-null) so the likely
    # path called the trap without the fallback ever running -> the trap reported
    # a STALE target from an earlier resolved call. That ghost address made the
    # run-heal chase already-registered functions forever (Gears of War 3:
    # 0x8271C710 re-flagged every round while the real unresolved target was a
    # different address). Diff = one macro line in every port's init.h; judged
    # and re-blessed fleet-wide. rexruntime UNCHANGED.
    # + runtime: InvalidFunctionTrap now logs GetFunction(target) before the
    # fatal abort ("trap diagnostics"), bifurcating table-miss from call-path
    # bugs at zero cost outside the abort path (how the ghost-target loop and
    # the stale-exe chain were root-caused).
    # v2.15 (codegen ADDITIVE): [[guest_patches]] manifest support -- community
    # xenia-canary game-patch byte writes applied to the guest image right after
    # the XEX loads in codegen, BEFORE analysis, so fixes are baked permanently
    # into the recompiled native code (first user: Gears of War 3 "Disable
    # Ambient Occlusion" = the greenish ghost-shadow under upscaling, applied as
    # a surgical DepthOfField-gate-only subset that keeps AO alive). Empty/absent
    # section -> byte-identical for every existing project.
    # v2.16 (RUNTIME, additive/gated): two fixes that take 565507E4 Crash of the
    # Titans from crash-at-boot to booting + running its renderer, both structured
    # to not touch any working title:
    #  (1) GREEN-THREAD HOST-FIBER BRIDGE (kernel commit 7db6198): titles that run
    #      their own cooperative scheduler on raw KeSetCurrentStackPointers now
    #      suspend/resume via real host fibers (rex::thread::Fiber) instead of the
    #      lossy Reenter-unwind, so a green context that RETURNS up its own guest
    #      chain (yield epilogue) no longer silently exits the thread. Gated
    #      byte-identical on (fiber_ptr && guest_object()==thread) + same-stack
    #      early-out -> no pre-fiber fleet title runs a new instruction.
    #  (2) TITLE-LIVENESS (commit e580b29): the app no longer quits when the guest
    #      entry thread returns; it waits for all guest-created threads to drain
    #      (HasRunningGuestThreads), matching 360 semantics. Titles whose main
    #      thread never returns are unaffected.
    #  + gpu/shader (commit 29e70b4): stop double-reverting the normalized-coord
    #      tfetch offset by draw_resolution_scale (it was already guest-size
    #      normalized). Byte-identical at scale=1; only scaled-texture normalized
    #      samples at scale>1 change.
    # rexglue.exe relinks (C++ links non-reproducible) but codegen output is
    # UNCHANGED from v2.15 -> pin re-generated to the actually-shipped binaries.
    # v2.17 (SDK-source only, PIN UNCHANGED): in-game settings menu (F1) --
    # ReXApp now wires the curated SimpleSettingsDialog (resolution scale
    # 720p/1440p/2160p, framerate, fullscreen/vsync, + title-conditional FoV/
    # ultrawide), with "Apply & Restart" self-relaunching the exe to apply
    # resolution changes. The wiring lives in the shipped share/rexglue/
    # rex_app.cpp, which every title compiles into its OWN exe -- so neither
    # rexglue.exe nor rexruntime.dll changes (pin stays on the v2.16 binaries;
    # the SimpleSettingsDialog code was already compiled into rexui/rexruntime).
    # Existing ports get the menu on relink; new ports get it automatically.
    # v2.18 (SDK commit af9e790): relocatable per-module function table
    # (function_table_base) -- the multi-XEX collision cure. When a companion
    # image loads right after the main's (FIFA Street: fifadllzf at 0x82300000),
    # the main's dispatch table at image_base+image_size would overlap it and the
    # companion's functions never register (FATAL 0x82612A48). New optional
    # [entrypoint] manifest key relocates the table (rexauto authors it
    # automatically on collision, _relocate_colliding_tables); runtime overlap
    # check now tests image and table as separate ranges. Emitted ONLY when the
    # key is present (exists()-gated templates; this inja treats "" as truthy so
    # an always-present empty value is unsafe) -> fleet codegen byte-identical
    # (gate 18/18 PASS identical; gears' 1 diff = the v2.17 SSAO guest_patch
    # post-dating its baseline, re-blessed). Runtime spot-check: gears survived
    # 360s on the new rexruntime; FIFA main table at 0x86B70000 "(explicit
    # base)" + companion registers + companion code executes.
    # v2.19 (SDK commit f5e5ce1) "the never-boots cohort": kernel/VFS answers
    # (XCTD not-compressed, FILE_DEVICE_DISK, cache0:/cache1: mounts,
    # delete-on-close honored) + sibling-module imports recompiled as guest
    # code + rexauto: zero-touch companion autodetect, module setjmp/longjmp,
    # owner-aware shrink, live injector updates. Gate 25/29 byte-identical;
    # the 4 diffs = the day's cured titles (FIFA title screen, sonic intro,
    # halo 3 walls down, forza ported), judged + re-blessed.
    # v2.21 (SDK 4b224a1) "static harvest + upstream harvest": the recompiler
    # improves itself. CODEGEN: cross-function `b` (tail-call) targets now
    # register as functions (was bl-only) -- kills the largest static-residue
    # class (~39%, the Forza 0x830ED910 REX_FATAL class); a 20-port census
    # proved 0 of the run-heal residue is truly irreducible. Fleet codegen
    # changes uniformly (tail calls lower to registered functions) -> re-blessed;
    # validated runtime on joust/gears/gta-sa/dbz (0 corruption). PLUS harvested
    # from upstream nightly (8dadea6, each verified vs our fork by a dedicated
    # agent -- 5 of 6 candidates rejected because ours was equal/better):
    # PPCContext ungate (removes a fiber-path footgun; byte-identical), conditional-
    # bcctr tail recovery, guest-stack-free-on-exit, spinlock self-deadlock fix,
    # xex2_version MSB packing, + the achievement-tracking backend (XAM unlock
    # reporting; overlay UI deferred). Runtime spot-check: skate3.
    # v2.22 (SDK 09a18ee) "silent-miscompile guard": NORMPACKED64 (4:20:20:20)
    # unpack fix (78af0a8) -- the 20-bit sign-extend was `int32_t(u64<<44)>>44`,
    # UB (shift >= width) AND the cast dropped the field: x/y/z decoded to 0.0
    # unconditionally. Latent-only cure: NO port emits NORMPACKED64 (grep=0),
    # gate byte-identical by construction. Plus tools/codegen_ub_lint.py
    # (09a18ee): a decidable shift-past-width lint over the emitted templates
    # that keeps this whole bug class out forever (green on current builders;
    # regression-tested against the pre-fix pattern). The other 4 conclave
    # "bugs" (32-bit carry, CR0, vcmpbfp NaN, denormal flush) were ground-truth
    # re-verified as DELIBERATE, game-validated choices -- left untouched.
    # v2.23 (SDK db6bd1d) "sibling imports bound + the install-disc flow":
    # CODEGEN+RUNTIME 81ccf82 sibling-import binding (Halo 3 L360 root cause:
    # raw placeholder thunks looped caller<->thunk to stack overflow; now
    # patched to IAT-slot dispatch + runtime binds type-0 slots per module
    # load). RUNTIME: XamContentCreateEnumeratorInternal implemented (GTA V's
    # install discovery -- was a stub, enumeration succeeded empty ->
    # "insert installation disc"); game volume answers FILE_DEVICE_CD_ROM
    # (retail from-disc branch); content-mount device path had a trailing
    # separator that broke "<pkg>:\file" resolution; XamSwapDisc signals its
    # completion KEVENT (one-arg stub swallowed the handle -> eternal wait
    # after the install gate passed). Chain verified by IDA decompile of GTA
    # V's install state machine (sub_8299EE40) + live runs: game now streams
    # from mounted install packages and reaches its loading screen. Gate
    # 30/30 byte-identical (fifa flag = same-day heal growth, proven under
    # old rexglue, re-blessed).
    # v2.24 (SDK 80e886c) "GTA V reaches gameplay": RUNTIME-ONLY, the five
    # RAGE boot walls between the install gate and the game: startup
    # notifications delivered to EVERY XamNotify system listener (80e886c);
    # XNetGetEthernetLinkStatus reports a live LAN link (a83b685);
    # XexCheckExecutablePrivilege(11) -> INSECURE so cache routes to the
    # direct path (16a4948); update: always mounted, empty when no TU --
    # device-not-found was fatal to RAGE (e063379); writable gamecache:/
    # commoncrc: engine scratch mounts (885018a). GTA V boots into GAMEPLAY
    # (user-witnessed; intermittent freeze under investigation). Codegen
    # untouched: gate 30/30 byte-identical (gta_v flag = same-day heal
    # growth, re-blessed).
    "rexglue.exe":    "71b45ddf35f622eec9caa93d2e3783509f62ff1700277d6184cac9310307ef23",
    "rexruntime.dll": "e6a96b0291f5d832af88aef60218bf1b933676390303a952b43f248eea3e51fc",
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
    fns = {"extract": stage_extract, "xctd": stage_xctd, "init": stage_init, "setjmp": stage_setjmp,
           "jumptables": stage_jumptables, "deepextract": stage_deepextract,
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
