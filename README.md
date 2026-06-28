# rexauto

One app, from an Xbox 360 content container to a recompiled build that boots.

rexauto is a desktop front-end and orchestrator for the
[ReXGlue](https://github.com/) static recompiler. Point it at a game container
and it runs the whole pipeline that otherwise takes a day of by-hand steps:
extract the game, scaffold a project, recover jump tables, build, and run the two
self-heal loops a fresh title needs — then launches it.

<p align="center"><img src="gui/rexauto_icon.png" width="96"></p>

## Download & run

1. Grab `rexauto.exe` from the [latest release](../../releases/latest).
2. Run it. A native window opens (Edge WebView2): a 3D scene, the game's cover
   art and title read straight from the package, a live six-stage tracker, and a
   streaming log.
3. First time, open **Setup** (top-right). It shows what's installed and fetches
   the rest for you:
   - **ReXGlue SDK** — one click; downloaded prebuilt and wired up next to the app.
   - **LLVM/clang** and **VS Build Tools** — installed via `winget`.
   - **IDA Pro** — optional and commercial (only the jump-table stage uses it);
     install it yourself if you want that extra recovery.
4. Point it at a container, hit **Recompile**, watch it go.

> **Honest note.** rexauto can't *be* a C++ compiler — it *drives* one. A real
> recompiler has to build the generated C++, so a C++ toolchain (clang + the
> Windows SDK, a couple of GB) is required. There's no 15 MB "zero-dependency"
> build of a thing that compiles 17k functions. What rexauto does is make getting
> there one screen: the Setup panel installs everything, you don't hunt for it.

## What each stage does

1. **extract** — STFS package (`CON`/`LIVE`/`PIRS`, the XBLA/DLC layout) →
   `default.xex` + all assets. An already-extracted folder is used as-is.
2. **init** — `rexglue init` scaffolds the project.
3. **jumptables** — if IDA is present, dumps the decompressed image and recovers
   `bctr` jump tables into `switch_tables.toml`
   ([xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)). Skipped
   cleanly otherwise; the recompiler's built-in switch handling still applies.
4. **build** — codegen + clang/CMake. When the recompiler splits a function
   mid-flow (a branch into the next one → a `goto` to an `undeclared label`), it
   **auto-extends** the function and rebuilds — the boundary fix teams otherwise
   write by hand. Repeats until clean.
5. **runheal** — runs the game; each `invalid or unregistered function at 0xADDR`
   the dispatcher hits gets **registered**, rebuilt, and re-run, until it stops.
6. **run** — launches it.

## CLI

The same engine without the window:

```sh
python rexauto.py "<container-or-folder>" --name mygame --run
```

Every stage is checkpointed (re-running skips finished ones); `--from <stage>`,
`--only <stage>`, `--no-jumptables`. Tool paths come from the usual install
locations, `PATH`, or env vars (`REXGLUE`, `REXSDK_DIR`, `IDAT`, `CLANG`,
`VCVARS`, `PYTHON`, `JT_REPO`).

## What it does NOT do

It gets you to a **booting, guest-code-executing build, automatically**. It does
**not** fix per-title GPU/emulation gaps: a game whose vertex formats or kernel
calls the ReXGlue runtime doesn't support yet will boot, open a window, and reach
the render loop but may not draw correctly or stay up. That's runtime-emulation
work, separate from recompilation, and inherently per title. rexauto removes the
mechanical pipeline; the runtime backend is still where a given title lives or dies.

## Build from source

```sh
pip install pywebview pyinstaller pillow
python gui/make_icon.py
pyinstaller --noconfirm --onefile --windowed --name rexauto \
  --icon gui/rexauto.ico --add-data "gui/index.html;gui" --paths gui \
  --hidden-import extract --hidden-import heal --hidden-import rexauto \
  --hidden-import server --hidden-import setup --collect-all webview  app.py
```

One binary, two modes: no args → the GUI; `--__pipeline …` → the recompiler (the
GUI re-invokes itself this way to stream the pipeline). Run it as a plain web app
with `python gui/server.py`.

## Credits

- **ReXGlue** — the static recompiler and runtime rexauto drives (© Tom Clay,
  BSD-3; derived from the Xenia project). The release bundles a prebuilt copy for
  one-click setup; see [NOTICE](NOTICE).
- **[xenon-jumptables](https://github.com/xdzleo/xenon-jumptables)** — the
  jump-table / boundary recovery used by the jumptables stage.

## License

MIT — see [LICENSE](LICENSE). Bundled third-party components keep their own
licenses (see [NOTICE](NOTICE)).
