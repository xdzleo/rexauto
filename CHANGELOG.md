# Changelog

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
