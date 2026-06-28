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

## `sdk-codegen-speedups.patch`

Makes `rexglue codegen` much faster (≈2x; rayman3hd 23.5s → 12s) — the run-heal
loop re-runs codegen on every rebuild, so this compounds. **Output is byte-identical**
(verified by hashing the whole generated tree before/after on rayman3hd, and by
determinism across repeated runs on skate3).

Two changes:

1. **Kill an O(F²) in the Discover phase.** `FunctionGraph::notifyFunctionAdded`
   scanned *every* function on *every* function add. It now consults a
   `target → nodes-with-an-unresolved-jump-to-it` index and touches only the nodes
   that can actually resolve (`FunctionNode::tryResolveAgainst` matches
   `target == newFunction->base()` exactly). Same resolutions, no full scan. This
   was the dominant cost (Discover 19s → 10s).
2. **Parallelize the Write phase.** Per-function `emitCpp()` is a pure, const,
   read-only transform, so emit all functions across all cores (thread pool over a
   pre-sized results vector), then assemble units serially in address order.

Note: parallelizing the *Discover analysis* was attempted and reverted — it is
dominated by the (now-fixed) serial graph update, and emitting all functions before
committing loses the intra-phase graph visibility the sequential code relies on,
changing output. The algorithmic fix above is the real, safe win.

```sh
cd <rexglue-sdk>
git apply /path/to/sdk-game-data-root-fallback.patch
git apply /path/to/sdk-codegen-speedups.patch
cmake --build out/build/win-amd64 --config Release --target install
```
