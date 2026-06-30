# Changelog

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
