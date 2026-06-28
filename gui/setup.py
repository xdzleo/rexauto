"""
setup.py — in-app dependency installer.

The app can't *contain* a C++ toolchain, but it can fetch and wire everything up
for you:
  • ReXGlue SDK   downloaded (prebuilt) from the rexauto release and unzipped next
                  to the app, where detect_env() finds it automatically.
  • LLVM/clang    winget install LLVM.LLVM
  • VS BuildTools winget install Microsoft.VisualStudio.2022.BuildTools + VCTools
  • IDA           optional, commercial — cannot be auto-installed (status only).

deps_status() reports what's present; run(target, emit) installs one thing and
streams progress as {"type":"setup",...} / refreshes {"type":"deps",...}.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

REXGLUE_URL = os.environ.get(
    "REXGLUE_BUNDLE_URL",
    "https://github.com/xdzleo/rexauto/releases/latest/download/rexglue-sdk-win64.zip")


def app_dir():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else ROOT


def _env():
    import rexauto
    return rexauto.detect_env()


def deps_status():
    e = _env()
    return [
        {"key": "rexglue", "name": "ReXGlue SDK", "found": bool(e["rexglue"] and e["sdk"]),
         "detail": e["rexglue"] or "not found", "action": "rexglue",
         "note": "the recompiler + runtime (bundled — one click)"},
        {"key": "clang", "name": "LLVM / clang", "found": bool(e["clang"] and e["clangxx"]),
         "detail": e["clang"] or "not found", "action": "llvm",
         "note": "C++ compiler (winget)"},
        {"key": "vcvars", "name": "VS Build Tools", "found": bool(e["vcvars"]),
         "detail": e["vcvars"] or "not found", "action": "vs",
         "note": "MSVC linker + Windows SDK (winget)"},
        {"key": "python", "name": "Python", "found": bool(e["python"]),
         "detail": e["python"] or "not found", "action": "python",
         "note": "optional — only for the jump-table stage"},
        {"key": "idat", "name": "IDA Pro", "found": bool(e["idat"]),
         "detail": e["idat"] or "not found", "action": None,
         "note": "optional, commercial — install manually for extra jump tables"},
    ]


def _winget(args, emit):
    cmd = ["winget", "install", "-e", "--accept-source-agreements",
           "--accept-package-agreements"] + args
    emit({"type": "setup", "level": "info", "text": "› " + " ".join(cmd)})
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, bufsize=1)
    except FileNotFoundError:
        emit({"type": "setup", "level": "err",
              "text": "winget not found — install it from the Microsoft Store (App Installer)"})
        return False
    for line in p.stdout:
        line = line.rstrip()
        if line:
            emit({"type": "setup", "level": "dim", "text": line})
    return p.wait() in (0, -1978335189)  # 0 ok; the latter = 'already installed'


def install_rexglue(emit):
    dest_root = app_dir()
    emit({"type": "setup", "level": "info", "text": "downloading ReXGlue SDK…"})
    tmp = os.path.join(tempfile.gettempdir(), "rexglue-sdk-win64.zip")
    try:
        req = urllib.request.urlopen(REXGLUE_URL, timeout=30)
    except Exception as ex:
        emit({"type": "setup", "level": "err", "text": "download failed: %s" % ex})
        emit({"type": "setup", "level": "warn",
              "text": "set REXGLUE_BUNDLE_URL or drop the SDK into %s\\rexglue" % dest_root})
        return False
    total = int(req.headers.get("Content-Length", 0))
    got = 0
    last = -1
    with open(tmp, "wb") as f:
        while True:
            chunk = req.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            pct = int(got * 100 / total) if total else 0
            if pct != last and pct % 5 == 0:
                last = pct
                emit({"type": "setup", "level": "info", "progress": pct,
                      "text": "downloading… %d%% (%.0f/%.0f MB)"
                      % (pct, got / 1e6, total / 1e6)})
    emit({"type": "setup", "level": "info", "text": "extracting…"})
    # remove a stale copy, then unzip rexglue/ + xenon-jumptables/ next to the app
    for sub in ("rexglue", "xenon-jumptables"):
        p = os.path.join(dest_root, sub)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(dest_root)
    try:
        os.remove(tmp)
    except OSError:
        pass
    emit({"type": "setup", "level": "good", "text": "ReXGlue SDK installed -> %s\\rexglue" % dest_root})
    return True


def run(target, emit):
    try:
        if target == "rexglue":
            ok = install_rexglue(emit)
        elif target == "llvm":
            ok = _winget(["--id", "LLVM.LLVM"], emit)
        elif target == "vs":
            ok = _winget(["--id", "Microsoft.VisualStudio.2022.BuildTools", "--override",
                          "--passive --add Microsoft.VisualStudio.Workload.VCTools "
                          "--includeRecommended"], emit)
        elif target == "python":
            ok = _winget(["--id", "Python.Python.3.12"], emit)
        elif target == "all":
            ok = True
            for st in deps_status():
                if not st["found"] and st["action"]:
                    ok = run(st["action"], emit) and ok
        else:
            emit({"type": "setup", "level": "err", "text": "unknown target: %s" % target})
            ok = False
    except Exception as ex:
        emit({"type": "setup", "level": "err", "text": "install error: %s" % ex})
        ok = False
    emit({"type": "deps", "items": deps_status()})
    emit({"type": "setup", "level": "good" if ok else "warn",
          "text": ("done — %s ready" % target) if ok else ("%s did not complete" % target),
          "final": True, "ok": ok})
    return ok
