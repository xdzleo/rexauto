# patches

Patches for the bundled **ReXGlue SDK** ([rexglue/rexglue-sdk](https://github.com/rexglue/rexglue-sdk),
upstream — not this repo). rexauto ships a prebuilt copy of the SDK; these are the
source changes rexauto relies on. Apply against an SDK checkout and rebuild
(`cmake --build out/build/win-amd64 --config Release --target install`), then
rebuild the ports.

## `sdk-game-data-root-fallback.patch`

Lets a recompiled title launch on a **plain double-click** of its `.exe`.

Without it, the runtime aborts with *"--game_data_root was not provided."* when no
`--game_data_root=<path>` is passed. The patch makes `ReXApp::SetupEnvironment`
fall back, when the flag is absent, to the first location that actually contains
the title (`default.xex`):

1. a `game_root.txt` sidecar next to the exe naming the data path
   (rexauto writes this after a successful build — see `write_game_root` in
   `rexauto.py`),
2. a `game/` folder next to the exe,
3. the exe's own folder.

Command-line `--game_data_root` still wins when given.

```sh
cd <rexglue-sdk>
git apply /path/to/sdk-game-data-root-fallback.patch
```
