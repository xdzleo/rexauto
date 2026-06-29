# patches

Source changes to the bundled **ReXGlue SDK** that rexauto relies on. rexauto ships a
prebuilt copy of the SDK; these files document the changes so the build is reproducible.

**As of v1.3 the bundled SDK is built from the Skate-3 ReXGlue fork**
([mchughalex/rexglue-skate3](https://github.com/mchughalex/rexglue-skate3) @
`skate3-sdk-clean`) — the hardened ReXGlue lineage the shipped community ports run on.
The three `sdk-fork-*.patch` files apply on top of that fork. The two older patches at
the bottom targeted the previous upstream `rexglue/rexglue-sdk` v0.8.0 base.

Apply against an SDK checkout and rebuild:

```sh
cd <rexglue-sdk>
git apply /path/to/<patch>.patch
cmake --build out/build/win-amd64 --config Release --target install
```

---

## v1.3 — fork patches

### `sdk-fork-dump-image.patch`

Honors the `REX_DUMP_IMAGE` env var: `rexglue codegen` reconstructs and dumps the
decompressed guest image so the jump-table IDA pass can recover switch tables. The fork
lacked this, so rexauto's `jumptables` stage silently dumped nothing and was skipped —
**breaking any title that needs switch resolution** (Skate 3's ~353 tables, Rayman 3's
250). With the patch, recovery matches the previous base exactly (rayman3hd: 250 tables).

### `sdk-fork-app-header-prefix.patch`

The fork's `init_h.inja` emits the project-prefixed `{{project}}_PPCImageConfig`, but its
`app_header.inja` still referenced the bare `PPCImageConfig` — so a freshly-`init`'d (or
re-codegen'd) project failed to compile with *"use of undeclared identifier
'PPCImageConfig'"*. Fixed the app header to use `{{ names.snake_case }}_PPCImageConfig`.

### `sdk-fork-codegen-ofN-index.patch`

O(F²)->O(F) in the Discover phase. `FunctionGraph::notifyFunctionAdded` scanned *every*
function on *every* function add; it now consults a `target -> source-function-bases`
index (`unresolvedByTarget_`) and touches only the nodes that can actually resolve.
**Output is byte-identical** (verified by `diff -rq` and by hashing the whole generated
tree before/after on rayman3hd — same SHA).

---

## v0.8.0 base patches (previous upstream base)

### `sdk-game-data-root-fallback.patch`

Lets a recompiled title launch on a **plain double-click** of its `.exe`. Without it the
runtime aborts with *"--game_data_root was not provided."* The patch makes
`ReXApp::SetupEnvironment` fall back, when the flag is absent, to the first location that
contains the title (`default.xex`): a `game_root.txt` sidecar next to the exe (rexauto
writes this — see `write_game_root` in `rexauto.py`), then a `game/` folder next to the
exe, then the exe's own folder. Command-line `--game_data_root` still wins.

### `sdk-codegen-speedups.patch`

The original ~2x codegen speedup against v0.8.0. Its **O(F²)->O(F) Discover fix** is the
same idea re-applied for the fork as `sdk-fork-codegen-ofN-index.patch` above; it also
parallelized the Write phase (per-function `emitCpp()` across cores). Output verified
byte-identical on the v0.8.0 base. (Only the O(F²) fix is currently carried onto the
fork; the Write-phase parallelization is a candidate for a future fork patch.)
