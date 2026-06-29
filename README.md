# rexauto

**One window: an Xbox 360 game container in, a PC build that boots out.**

rexauto is a desktop front-end and orchestrator for the
[ReXGlue](https://github.com/xdzleo/rexglue-skate3) static recompiler. Point it at
a game container and it runs the whole pipeline that's otherwise a day of by-hand
work — extract, scaffold, recover jump tables, build, and the two self-heal loops
a fresh title needs — then launches the result.

<p align="center"><img src="gui/rexauto_icon.png" width="96"></p>

## Download & run

1. Grab `rexauto.exe` from the [latest release](../../releases/latest).
2. Run it. A native window opens (Edge WebView2): a 3D scene, the game's cover art
   and title read straight from the package, a live six-stage tracker, and a
   streaming log.
3. First run, open **Setup** (top-right) — it shows what's installed and fetches
   the rest:
   - **ReXGlue SDK** — one click, prebuilt, wired up next to the app.
   - **LLVM/clang** + **VS Build Tools** — via `winget`.
   - **IDA Pro** — optional, commercial; only the jump-table stage uses it.
4. Point it at a container, hit **Recompile**, watch it go.

> rexauto *drives* a C++ compiler — it isn't one. A real recompiler has to build
> the C++ it generates (tens of thousands of functions), so a clang + Windows SDK
> toolchain is required; there's no 15 MB "zero-dependency" recompiler. What
> rexauto does is make installing all of it one button instead of a scavenger hunt.

## The pipeline

1. **extract** — container → `default.xex` + assets: **STFS** (`CON`/`LIVE`/`PIRS`
   — XBLA/DLC), **ISO** (GDFX/XDVDFS disc), **GoD** (SVOD single-file), or an
   already-extracted folder.
2. **init** — `rexglue init` scaffolds the project.
3. **setjmp** — finds the statically-linked CRT `setjmp`/`longjmp` and records them
   in the manifest. A guest `longjmp` restores registers + stack from a `jmp_buf`
   and `blr`s; recompiled naïvely that `blr` becomes a plain `return`, corrupting a
   non-volatile register and crashing exception-using titles at startup. Titles
   without exceptions have no signature and are left untouched.
4. **jumptables** — with IDA present, recovers `bctr` jump tables into
   `switch_tables.toml`
   ([xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)). Skipped
   cleanly otherwise — the recompiler's built-in switch handling still applies.
5. **build** — codegen + clang/CMake. When the recompiler splits a function
   mid-flow (a branch into the next one → a `goto` to an undeclared label), rexauto
   **auto-extends** the boundary and rebuilds — the fix porting teams otherwise
   make by hand — until it's clean.
6. **runheal** — runs the game; every `invalid or unregistered function at 0xADDR`
   the dispatcher hits gets registered, rebuilt, and re-run, until none are left.
7. **run** — launches it.

## Shared cures (the gabarito database)

The slow part of a fresh port is the heal loops re-discovering the functions the
static pass missed — and that set is **identical for everyone running the same
binary**. So rexauto publishes it: once a title converges, its cures (a
`functions.toml` keyed by the `default.xex` SHA-256) go into a shared database, and
the next person to recompile that exact binary seeds them up front and skips most
of the heal. Fetch is public and keyless; a miss just heals from scratch.

The bundled SDK is **pinned by hash** — rexauto refuses to run against an SDK build
it wasn't tested with, so a mismatched runtime can't silently produce a broken exe.

## CLI

Same engine, no window:

```sh
python rexauto.py "<container-or-folder>" --name mygame --run
```

Stages are checkpointed (re-running skips finished ones). Flags: `--from <stage>`,
`--only <stage>`, `--no-jumptables`. Tool paths come from the usual install
locations, `PATH`, or env vars (`REXGLUE`, `REXSDK_DIR`, `IDAT`, `CLANG`, `VCVARS`,
`PYTHON`, `JT_REPO`).

## What it does NOT do

rexauto gets you to a **booting, guest-code-executing build, automatically**. It
does **not** close per-title GPU/emulation gaps: a game using vertex formats or
kernel calls the ReXGlue runtime doesn't implement yet will boot, open a window,
and reach the render loop but may not draw correctly or stay up. That's
runtime-emulation work — separate from recompilation, and inherently per title.
rexauto removes the mechanical pipeline; the runtime backend is still where a given
title lives or dies.

## Build from source

```sh
pip install pywebview pyinstaller pillow
python gui/make_icon.py
pyinstaller --noconfirm --onefile --windowed --name rexauto \
  --icon gui/rexauto.ico --add-data "gui/index.html;gui" --paths gui \
  --hidden-import extract --hidden-import heal --hidden-import rexauto \
  --hidden-import detect_setjmp --hidden-import server --hidden-import setup \
  --collect-all webview app.py
```

One binary, two modes: no args → the GUI; `--__pipeline …` → the recompiler (the
GUI re-invokes itself to stream the pipeline). Or run it as a plain web app:
`python gui/server.py`.

## Credits

- **ReXGlue** — the static recompiler + runtime rexauto drives (© Tom Clay,
  BSD-3; derived from [Xenia](https://github.com/xenia-project/xenia)). Releases
  bundle a prebuilt copy of the [skate3 fork](https://github.com/xdzleo/rexglue-skate3)
  for one-click setup — see [NOTICE](NOTICE).
- **[xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)** — the
  jump-table / boundary recovery behind the jumptables stage.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party components keep their own
licenses ([NOTICE](NOTICE)).
