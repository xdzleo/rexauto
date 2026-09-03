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
        {"key": "jt_repo", "name": "xenon-jumptables", "found": bool(e["jt_repo"]),
         "detail": e["jt_repo"] or "not found", "action": None,
         "note": "bundled — static bctr jump-table recovery; each table it resolves "
                 "is a crash the run-heal doesn't have to find by dying (needs IDA)"},
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


def _lay_out_sdk(z, dest_root, emit):
    """Unzip the SDK into the layout detect_env() looks for:

        <app>/rexglue/sdk/   (bin/ include/ lib/ share/ cmake/ -- the CMake prefix)
        <app>/rexglue/tool/  (rexglue.exe + rexruntime.dll + TracyClient.dll)

    The release zip is a plain install tree (bin/ at its root); older bundles were
    wrapped in rexglue/ (+ xenon-jumptables/). Both shapes are accepted -- what must
    never happen again is dumping bin/ include/ lib/ next to the app, where nothing
    finds them and the UI keeps saying "not found" after a successful download.
    """
    names = [n for n in z.namelist() if not n.endswith("/")]
    if not names:
        raise RuntimeError("the SDK archive is empty")
    top = {n.split("/", 1)[0] for n in names}
    rex = os.path.join(dest_root, "rexglue")
    if "rexglue" in top or "xenon-jumptables" in top:
        # already-wrapped bundle: extract as-is, then make sure tool/ + sdk/ exist
        z.extractall(dest_root)
    elif "bin" in top:
        # raw install tree -> rexglue/sdk, binaries mirrored into rexglue/tool
        z.extractall(os.path.join(rex, "sdk"))
    else:
        raise RuntimeError("unrecognised SDK archive layout (top-level: %s)"
                           % ", ".join(sorted(top)))
    sdk = os.path.join(rex, "sdk")
    tool = os.path.join(rex, "tool")
    if not os.path.isdir(sdk) and os.path.isdir(os.path.join(rex, "bin")):
        sdk = rex
    os.makedirs(tool, exist_ok=True)
    for fn in ("rexglue.exe", "rexruntime.dll", "TracyClient.dll"):
        src = os.path.join(sdk, "bin", fn)
        dst = os.path.join(tool, fn)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)
    exe = os.path.join(tool, "rexglue.exe")
    if not os.path.isfile(exe):
        raise RuntimeError("rexglue.exe missing after extraction (looked in %s)" % tool)
    if not os.path.isdir(os.path.join(sdk, "include")):
        raise RuntimeError("SDK headers missing after extraction (looked in %s)" % sdk)
    emit({"type": "setup", "level": "dim", "text": "tool: " + exe})
    emit({"type": "setup", "level": "dim", "text": "sdk:  " + sdk})


def install_rexglue(emit):
    dest_root = app_dir()
    emit({"type": "setup", "level": "info", "text": "downloading ReXGlue SDK…"})
    emit({"type": "setup", "level": "dim", "text": REXGLUE_URL})
    tmp = os.path.join(tempfile.gettempdir(), "rexglue-sdk-win64.zip")
    try:
        req = urllib.request.urlopen(
            urllib.request.Request(REXGLUE_URL, headers={"User-Agent": "rexauto-setup"}),
            timeout=60)
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
    if total and got != total:
        emit({"type": "setup", "level": "err",
              "text": "download truncated (%d of %d bytes)" % (got, total)})
        return False
    if not zipfile.is_zipfile(tmp):
        emit({"type": "setup", "level": "err",
              "text": "downloaded file is not a zip (%d bytes) — bad URL or a GitHub error page" % got})
        return False
    emit({"type": "setup", "level": "info", "text": "extracting…"})
    # remove a stale copy, then lay out rexglue/ (+ xenon-jumptables/) next to the app
    for sub in ("rexglue", "xenon-jumptables"):
        p = os.path.join(dest_root, sub)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    try:
        with zipfile.ZipFile(tmp) as z:
            _lay_out_sdk(z, dest_root, emit)
    except Exception as ex:
        emit({"type": "setup", "level": "err", "text": "extract failed: %s" % ex})
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # the only verdict that matters: does detect_env() see it now?
    e = _env()
    if not (e["rexglue"] and e["sdk"]):
        emit({"type": "setup", "level": "err",
              "text": "extracted, but detect_env() still can't see the SDK "
                      "(rexglue=%s, sdk=%s)" % (e["rexglue"], e["sdk"])})
        return False
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
