# Changelog

## 2.5.1 — "boot deeper" (2026-07-01)

Run-heal now keeps hand-written asm routines WHOLE instead of splitting them, so
Gears of War Judgment boots far past its intro decompressor (GPU up → movies →
networking → media verification, vs the old ~1s crash). **rexauto-only; SDK
unchanged** (`rexglue.exe` `06b93244`, `rexruntime.dll` `0ce11411`).

- **Root cause:** the intro decompressor (`sub_830AFE28`, a switch-on-ctr state machine
  with a shared-tail loop-back) had an under-recovered jump table (IDA found 7 of ~10
  landings). At runtime the missing landings hit `default: REX_CALL_INDIRECT_FUNC` →
  "invalid function"; the play-and-heal loop then registered them as standalone `{}`
  functions, which SPLIT the routine — turning a healable "invalid function" into an
  UN-healable `REX_FATAL("Unresolved branch")` when the split copy's loop-back branched
  into the parent.
- **`heal.register_or_seed`:** an unregistered-function address that falls INSIDE an
  existing function's `end`-override span is a landing of that routine, not a new
  function → route it to forced_landings (keeps the routine whole), never a `{}` split.
- **`heal.extend_switch_table`:** such a landing is also added as a `case` to the
  routine's bctr switch table (so the dispatch resolves it instead of hitting the
  default), paired with its forced_landings `loc_`. Under-recovered bctr tables now
  self-heal at runtime.
- Zero regression: `regression_gate.py` codegen byte-identical across all 10 baselined
  fleet games (these are run-heal changes; they never touch a passing game's data).
  Gears baseline re-blessed. Remaining Gears walls (media-verification DRM loop, an
  "Invalid UTF-8" async completion) are runtime/kernel-emulation, not recompilation.

## 2.5.0 — "Gears builds" (2026-07-01)

Adds **Gears of War Judgment** — the fleet's largest title (59,396 functions, 124 codegen
units, XGD3) — as a port that **builds, boots, and converges**, via a small opt-in
**codegen** SDK change. `rexruntime.dll` is byte-identical, so no game's runtime changes.

- **Root cause:** a hand-written computed-goto routine (`sub_830AFE28`, a stateful
  decompressor loop) is dispatched by a `bctr` jump table whose landings the heuristic
  `detectJumpTable` under-recovers — 3 stay dangling `goto loc_T` with no block →
  permanent `use of undeclared label` stall. Splitting the landings into functions
  passes the build but severs the loop's back-edge into a runtime `REX_FATAL`; the
  routine must stay whole.
- **Fix (SDK, codegen-only):** new `forced_landings = [0x..]` config array. During block
  discovery, after normal flow, a listed address inside a function that normal control
  flow did NOT reach is seeded as an in-function block — its `loc_` label is emitted and
  the routine stays one whole function. Empty list ⇒ seed inert ⇒ **byte-identical**.
- **Fix (rexauto, self-healing):** the undeclared-label heal now writes the exact
  landing addresses to `<game>_forced_landings.toml`, wires it into the manifest, and
  re-codegens — converging any title with this defect, no per-game hack.
- **Zero regression, proven:** codegen byte-identical across all 10 baselined fleet games
  (`regression_gate.py`); Gears builds → 91 MB exe → boots → run-heal converges with no
  invalid-function FATAL (the decompressor runs — the split approach would have crashed).
  SDK: `rexglue.exe` new codegen pin; `rexruntime.dll` unchanged (`0ce11411`).

## 2.4.2 — "cover art" (2026-07-01)

Cover art for **ISO / GoD / folder** targets in the desktop app. Xbox 360 discs don't
embed cover art (it's a marketplace tile, not on the disc), so ISO targets used to show
a blank card. Now the GUI fetches the game's tile by `title_id`.

- **title_id from the disc's `default.xex`** — a new XEX2 parser reads the execution-info
  header (validated: SVR07 → `545107E0`, skate3 → `454108E6`). `read_package_meta` now
  fills `title_id` for raw XEX / folder / GDFX ISO targets. The ISO reader walks the GDFX
  at every XGD base offset (0x0, XGD2 `0xFD90000`, XGD3 `0x2080000`, …) — proven on real
  Captain America (XGD2), Gears of War Judgment (XGD3), and skate3 (base 0x0) images, and
  it correctly returns nothing for a non-Xbox disc (e.g. the PS3 Skate 3).
- **Cover fetched from XboxUnity** by title_id (`fetch_title_icon`), cached under `covers/`
  so it's pulled once per title. Best-effort and offline-safe — a network failure just
  falls back to the placeholder; a title with no tile is negative-cached.
- No pipeline/codegen change; SDK unchanged (`95010481` / `0ce11411`).

## 2.4.1 — "right target, clear signal" (2026-07-01)

Desktop-app (GUI) + extract UX fixes. No pipeline/codegen change; SDK unchanged.

### GUI state reset on target change
- **Name stuck on the old target.** The name only auto-derived when the field was
  empty/"game", so after the first target set `name="skate_3"`, picking a new target
  kept the old name → the new game recompiled into the wrong project dir. A `nameAuto`
  flag now re-derives the name on every target change until the user types their own.
- **Cover not reset.** Switching from an STFS package (has an embedded thumbnail) to an
  ISO (none) left the *previous* game's art on the card. The no-cover branch now clears
  the stale cover and shows a neutral placeholder (Xbox 360 discs don't embed cover art —
  it's a marketplace tile, not on the disc; the title still shows below the card).

### Clearer extract error on the wrong disc
- Feeding a **PlayStation 3** disc (or any non-Xbox ISO9660 image) failed with an opaque
  `unsupported container (magic=b'\x00\x00\x00\x04')`. extract now probes for an ISO9660
  PVD + PS3 markers (`PS3_GAME`/`EBOOT.BIN`/`PS3_DISC.SFB`) and says plainly it's a PS3
  disc and that rexauto needs the Xbox 360 version (a very common mistake with
  multi-platform games like Skate 3).

### SDK
- Unchanged (`SDK_PIN` still `95010481` / `0ce11411`); `rexglue-sdk-win64.zip` identical
  to 2.3.0/2.4.0. Only `rexauto.exe` changed.

## 2.4.0 — "parse once" (2026-07-01)

Fleet-wide **build-perf** release — the recompile is faster with **zero codegen change**
(the generated C++ and every title's binary stay byte-identical; the regression gate is
unaffected because a PCH touches compile speed, not emitted code).

### Precompiled header for the `<name>_init.h` monolith
- Every generated recomp TU opens with `#include "<name>_init.h"` — a huge header (tens of
  thousands of `DECLARE_REX_FUNC` externs + heavy C++23 STL; skate3's is 1.56 MB / 48.6k
  lines). Its front-end parse was a fixed per-TU floor paid once **per TU** (a 24-function
  TU still cost ~3.7s = pure header parse).
- rexauto now injects `target_precompile_headers(<name> PRIVATE generated/default/<name>_init.h)`
  into every port at build time so clang parses it **once**. Idempotent; extra recompiled
  modules (e.g. skate3's EAWebkit, which include their own init header) are marked
  `SKIP_PRECOMPILE_HEADERS`. Opt out with `REXAUTO_NO_PCH=1`.
- **Measured** (skate3, eawebkit as an in-build no-PCH control): default-module per-TU
  compile **9.9s → 7.83s (~21%)**, small TUs **3.71s → 1.17s (3×)**. Proven on skate3
  (multi-module) and joust (single-module). Single-module titles — most of the fleet — get
  the full per-TU cut on the wall-clock (no un-PCH'd module tail).
- **Output-neutral by construction**: a PCH caches the parsed AST, never the emitted code.

### Profiling note (why this is the lever)
A 16-agent profile of the real `.ninja_log` found the recompile wall-clock lives in two
co-dominant ~90s sinks: a compile phase that is **already 16-thread-saturated** (link is a
negligible ~1s) and a **100%-serial IDA jump-table pass**. So the win is not "more CPU/RAM"
(compile is maxed; link/IDA don't parallelize) but **cutting redundant work** — hence the
PCH. Next on the roadmap: caching IDA's `.i64` database (40–175s off every re-run).

### SDK
- Unchanged from 2.3.0 (`SDK_PIN` still rexglue.exe `95010481` / rexruntime.dll `0ce11411`);
  `rexglue-sdk-win64.zip` is identical. Only `rexauto.exe` changed (the PCH injection).

## 2.3.0 — "the Yukes crack" (2026-07-01)

Cracked **WWE SmackDown vs Raw 2007** (Yukes engine, title 545107E0) — it now boots
to the in-game menu (playable). The community needed a custom `rexglue-sdk-yukes`
fork "with fixes this game depends on"; a 16-agent diff of their **working** build
against ours found the truth was inverted — *ours is the newer, superset SDK*, and
the blocker was a **regression in our own runtime**. Three SDK runtime fixes, all
**codegen-untouched → the whole fleet's generated C++ stays byte-identical**
(regression gate: 10/10 blessed titles identical, skate3 runtime PASS).

### The fatal fix — FPSCR host-thread MXCSR mask leak (fleet-wide)
- `XHostThread::Execute` ran guest FP over a context that never called `InitHost()`,
  so its cached MXCSR was `0` (memset). The guest's flush-mode toggles then wrote
  `MXCSR=0`, **unmasking the inexact FP exception** → the next inexact float op
  trapped as `STATUS_FLOAT_INEXACT_RESULT` (`0xC000008F`) ~13s into play. Guest
  `XThread::Execute` already inits FP; host worker threads did not.
- Fix (`xthread.cpp`): `thread_state_->context()->fpscr.InitHost()` at the top of
  `XHostThread::Execute`. **Generalizes to every title** with host-thread guest
  dispatch, and obsoletes the two per-path re-mask band-aids (audio / xma decoder)
  that were whack-a-moling this exact `STATUS_FLOAT_INEXACT_RESULT`.

### Writable `cache:` VFS device (fleet-wide)
- Yukes titles decompress their PAC asset packs into the Xbox 360 `CACHE:` scratch
  partition; with no device mounted every `CACHE:\...` open returned `0xC000000F`.
  `Runtime::SetupVfs` now mounts a **writable** `HostPathDevice`
  (`cache_root_/guest_cache`) + `RegisterSymbolicLink("cache:")`. Any title that
  uses the 360 cache partition now works.

### Ranged physical-alloc offset (xenia parity)
- Enabled the xenia `ignore_offset_for_ranged_allocations` behaviour in
  `MmAllocatePhysicalMemoryEx` (drop the physical offset for a ranged request; the
  in-code note names WWE SvR `545107E0`/`545108B4`). **Ranged-only** → the common
  `MmAllocatePhysicalMemory` path is byte-identical.

### Fleet / gate
- **SVR07 added as a tracked title** (codegen baseline blessed, 58 files).
- SDK commit `b363c08` (rexglue-skate3 `fork-base`); `SDK_PIN` bumped to
  rexglue.exe `95010481` / rexruntime.dll `0ce11411`. Every runtime change is
  additive/corrective and the regression gate proves no fleet title regressed.
- Sibling Yukes/THQ titles (e.g. WWE SvR 2008 `545108B4`) now inherit all three
  fixes for free — the first game of a family is the hard one; the rest are cheap.

## 2.2.0 — "parity, proven" (2026-06-30)

A full parity audit against the community build (mchughalex/skate3recomp, source
cloned and diffed dimension-by-dimension) confirmed our Skate 3 is **ahead** — same
app layer (DLC/marketplace, ISO installer, profiles, host-side ultrawide, EAWebkit
menus, fonts all byte-identical), a **superset** of recompiled-code coverage, and it
ships the **Title-Update-3-patched image** (the "ours is retail" worry was false:
manifest setjmp/longjmp = their TU3 addresses, and `game/*.xexp` SHA-256 match). The
audit found **three** real user-facing things their build system wired that ours did
not — now closed, generically.

### New pipeline stage: `codegen_patches`
- **`codegen_patches.py`, wired into `do_codegen`**: a declarative, per-project
  `<name>_codegen_patches.toml` splices host-side hooks into the generated
  `<name>_recomp.*.cpp` after codegen converges and before compile. Two reusable
  kinds — `literal` (exact find→replace in the one file matching every `require`) and
  `insert_before_call_after_anchor` (find the first generated guest call after an
  anchor and inject a line). Each patch is **idempotent** (`marker`) and **hard-fails**
  if its anchor is gone (a codegen re-layout must never silently drop a shipped
  behaviour). No config → no-op (fleet byte-identical). This generalizes the
  community's hand-written `cmake/ApplySkate3CodegenPatches.cmake` to the whole fleet.

### Skate 3 parity gaps closed
- **Projection-FOV hook** — the `skate3_field_of_view` / SimpleSettings FOV slider was
  inert (the host fn in `src/skate3_fov.cpp` was compiled but never called from
  generated code). A `literal` codegen patch now injects the override at the
  projection-matrix site. The slider changes FOV.
- **Ultrawide game-frustum hook** — host-side Hor+/NDC ultrawide already worked, but the
  guest cull-frustum wasn't widened (objects culled at screen edges under ultrawide). An
  `insert_before_call_after_anchor` patch injects `Skate3UltrawideGameFrustumPatchScope`
  at the frustum-setup call.
- **Win32 Per-Monitor-V2 DPI manifest** — added `src/skate3_app.manifest` (PerMonitorV2 +
  Common-Controls v6) and linked it via `LINKER:/MANIFESTINPUT`. Fixes high-DPI window
  blur and the skewed monitor-size feed into ultrawide aspect derivation.
- skate3.exe rebuilt against the shipped **v1.9 SDK** (rexruntime `c503f763`); all three
  patches verified compiled/embedded (`Skate3MaybeOverrideProjectionFovRadians`,
  `Skate3UltrawideGameFrustumPatchScope`, `PerMonitorV2` all present in the exe).

The community's demo_path boot-automation (off-by-default QA cvar) and interactive TU
installer wizard remain intentional non-gaps — we pre-stage the identical verified TU3
payloads at build time instead.

## 2.1.0 — "the long-tail, closed" (2026-06-30)

Closes the one open item from 2.0.0: the **switch-on-ctr heal long-tail** that made
sustained Skate 3 play crash non-deterministically (~85s in, at guest `0x82E57160`).
Our fork's `build_bctr` lowers each recovered jump table as `switch (ctx.ctr.u32)` with a
`case 0xTARGET:` per landing; a landing that isn't a registered function/chunk falls back
to `REX_CALL_INDIRECT_FUNC`, which FATALs at runtime if that guest address isn't in the
function table. The community build sidesteps this by lowering switches on an *index*
(inline `goto`), so it never needs the landings registered — we do.

### Headline
- **New pipeline stage `jt_landings`** (`jt_landings.py`, wired into `do_codegen`): after a
  clean codegen it scans the generated tree for every `case 0xT:` that still dispatches
  indirectly, and registers each as a **chained, contiguous chunk** of its enclosing
  function (`end(i)=start(i+1)`, `parent` chained). `classifyTarget` then treats each `case`
  target as a real entry, so `build_bctr` emits a direct `sub_T(...)` call instead of the
  indirect FATAL. A re-codegen converges (the second pass finds none). Fully generic —
  detects the landings of *any* function from the SDK's own table recovery; no IDA pass.
- **Skate 3 now plays sustained**: the 52 residual landings (`0x8270B3D0`×6, `0x829A9280`×5,
  `0x82E56878`×41 incl. the `0x82E57160` crasher) register automatically. Validated **alive
  after 300s (5 min), 0 FATAL, the crasher gone**. Ours is now equal-or-better than the
  community build for sustained play, from the pipeline, with no per-title hand editing.

### Safety / zero-regression
- **No-op for titles whose switches already resolve** (`heal()` returns 0) → codegen stays
  **byte-identical** for the other 9 fleet titles (verified: none have unregistered
  landings). The stage only ever *adds* chunks for genuinely-unregistered landings.
- **Gabarito-seeded configs are safe**: chunks are inserted at the end of the `[functions]`
  table regardless of whether `[meta]` leads (gabarito) or trails (plain port) it, so a
  fresh "clone and re-run" reproduces the playable build (seed → codegen → heal → converge).
- **Idempotent**: re-running against an already-healed config detects 0 and leaves the file
  byte-identical.

## 2.0.0 — "skate 3 born playable" (2026-06-30)

The release where the rexauto pipeline produces a **playable Skate 3** from an Xbox 360
container — plus a runtime-quality gate and several cross-game pipeline fixes, all with
**zero regression** across the 10-title fleet (codegen byte-identical: budokai3, joust,
dragon_ball_z_ultimate_tenkaichi, msmauto, laracroftandtheg, mssplosionman, game,
rayman3hd, skate3, final_exam).

### Headline
- **Skate 3 reaches gameplay from the pipeline** (Title Update 3.0.3.0): boots and runs to
  the `gameplay context reached` milestone in normal mode — it previously only booted.
  Sustained play still registers deep jump-table targets as they surface (the switch-on-ctr
  heal long-tail; the community build sidesteps it via switch-on-index). The runtime gate
  scores "reached gameplay" as the pass and tracks the rest for the heal loop.

### Pipeline
- **Auto-Title-Update** — detect and apply an Xbox 360 TU (`.xexp`) automatically and
  generically; the loader applies the delta in memory at both codegen and runtime, so we
  recompile *and* run the exact patched version. No-op for base-only titles.
- **TU-aware setjmp / exception-guard detection** — the setjmp stage force-dumps a fresh
  image and scans the *patched* (title-update) image, so the CRT structured-exception
  guard is found at its TU address and handled via `ppc_setjmp`. This auto-handles it for
  any TU title with **no per-game hand-coded exception shim** (the community hand-codes
  one per title, per version). Fixes a stale-image-dump bug that mis-detected the guard on
  the un-patched base image.
- **App-glue factory** — a declarative `<game>_appglue.toml` (`[identity]`, `[[alias]]`,
  `[overlay]`, `[dlc]`, `[title_update]`) emits the per-title host glue into the generated
  app's `OnPostSetup`. Forward-looking infra so new titles *declare* host glue instead of
  hand-porting a full app. Gated/no-op: no toml → byte-identical app.
- **RelWithDebInfo by default** — same optimization as Release plus symbols + line info,
  so a crash in the recompiled code points straight at the generated `sub_XXXX` + line
  (the heal/debug loop's biggest pain). Set `REXAUTO_BUILD_TYPE=Release` for a stripped,
  smaller distribution build. Codegen is unaffected → zero-regression for the codegen gate.
  (Maps imported libs to their Release variant under RelWithDebInfo to avoid an
  `_ITERATOR_DEBUG_LEVEL` link mismatch against the SDK's debug spdlog.)

### Gate
- **Runtime tier** (`regression_gate.py --runtime`) — build + headless launch + a
  play-health metric (boots / alive / no new FATAL / reached a gameplay marker) vs a
  blessed runtime baseline. Catches runtime-only and app-glue regressions the codegen tier
  cannot. HEAVY titles get a longer run floor (`REXGATE_RUN_SECONDS_HEAVY`) so late
  gameplay markers are reliably captured.

### SDK (bundled, pinned)
- **vtable-landing discovery fix** — mid-function vtable landings are statically
  registered (`addFunction`, no `registerChunk`): restores coverage while staying
  Budokai3-safe. Runtime carries the caller `lr` in the invalid-call FATAL + GPU
  command-ring memory fixes (battle-freeze).

### Fixes
- `heal.py`: stop doubling CR in CRLF `functions.toml` on rewrite.
- `extract.py`: `xex2_version` bit-order fix (caught by the real Skate 3 TU).

## 1.3 and earlier
Switch-on-CTR `build_bctr`, the jump-table resolver (xenon-jumptables), the boundary/heal
loop, and the all-games codegen regression gate. See git history.
