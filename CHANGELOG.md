# Changelog

## 2.27.1 — "the button that did nothing" (2026-09-01)

**Setup → Install ReXGlue SDK now actually installs it.** `rexauto`-only; **SDK
unchanged** (`rexglue-sdk-win64.zip` and `SDK_PIN` identical to 2.27.0).

### The bug

The one-click SDK install downloaded the bundle every time and then reported
"ReXGlue SDK installed" — and the Setup row kept saying **not found**. The download
was never the problem. `rexglue-sdk-win64.zip` is a plain install tree (`bin/`
`include/` `lib/` `share/` at its root), and `install_rexglue()` just `extractall()`'d
it next to the app. `detect_env()` looks for `rexglue\tool\rexglue.exe` and
`rexglue\sdk`, so nothing was ever found, while `<app>\bin`, `<app>\include`, … piled
up beside `rexauto.exe` on every click.

### The fix (`gui/setup.py`)

- The archive is laid out as `rexglue\sdk` (the CMake prefix the build stage passes as
  `CMAKE_PREFIX_PATH`) with `rexglue.exe` / `rexruntime.dll` / `TracyClient.dll`
  mirrored into `rexglue\tool`. A bundle already wrapped in `rexglue/` is still accepted.
- After extraction the installer re-runs `detect_env()` and only reports success if the
  SDK is actually visible. Before, success was unconditional.
- A truncated download, or a non-zip response (a GitHub error page), is reported as an
  error instead of raising inside `ZipFile`. Requests carry a User-Agent; timeout 60 s.

If you clicked the old button, the stray `bin/ cmake/ include/ lib/ licenses/ share/`
folders next to `rexauto.exe` can be deleted.

## 2.27.0 — "the copy nobody needed" (2026-08-20)

**Codegen is 20.7x faster and its output is byte-identical.** One line did it, and the
line was not slow because of what it computed — it was slow because it computed
something and threw it away, 65,000 times per title.

### The line

`phase_discover.cpp` opened each discovered function with:

```cpp
auto effectiveKnownFunctions = knownFunctions;
```

A deep copy of an `unordered_set` holding roughly 65,000 addresses — taken **once per
discovered function**, and discovery runs about 65,000 times on a large title. Order
n² node allocations against a per-call budget of ~1.9 ms.

The copy existed only so two `erase()` loops below it could subtract configured chunks.
Those loops read `chunksByParent`, which is populated exclusively from `functions.toml`
entries carrying `parent`. **The entire 30-title fleet contains exactly one such
entry** (`dragon_ball_z_burst_limit`). `forza_horizon` has none. So in almost every
call both loops iterated an empty map and the copy was discarded bit-identical to its
source.

It is now an overlay: build the excluded set — nearly always empty — and materialise a
copy only when something must actually be removed. The set is consulted downstream
solely through `contains()`, so the two formulations are equivalent by construction.
No threading, no ordering change, nothing to get subtly wrong.

### Measured, not estimated

Same manifest, same machine, the v2.26 pinned binary against this one:

| title | functions | before | after | |
|---|---:|---:|---:|---:|
| joust | 7,045 | 2.0s | 1.0s | 2.1x |
| ms_pac_man | 8,656 | 2.6s | 1.0s | 2.7x |
| budokai3 | 11,687 | 3.9s | 1.0s | 4.0x |
| halo_3 | 22,349 | 13.6s | 2.5s | 5.4x |
| gears_of_war_3 | 56,880 | 105.6s | 5.9s | 18.0x |
| gears_of_war_judgment | 59,681 | 110.5s | 6.4s | 17.2x |
| forza_horizon | 76,936 | 188.8s | 6.0s | 31.7x |
| grand_theft_auto_v | 94,334 | 234.4s | 8.2s | 28.6x |
| **total** | | **661.4s** | **31.9s** | **20.7x** |

Eight titles, chosen to span the fleet's size range; the other 22 were not timed.

The shape is the evidence. Dividing each *before* by the square of its function count
gives the same constant to within 2.6–4.0 × 10⁻⁸ across all eight — the signature of an
O(n²) defect, arrived at from the stopwatch without looking at the code. Divide each
*after* by function count, minus a ~0.7s process floor, and it is flat: 0.7–0.96 × 10⁻⁴
seconds per function. That is the linear work that was always there.

It also explains why Joust "only" gained 2.1x. Joust never had the problem. The fix
pays out where n is large, which is exactly where it hurt: `grand_theft_auto_v` cost
four minutes per codegen iteration, which in practice meant nobody iterated on it.

### Byte-identical, proven three ways

- The 30-title gate reports **28 PASS** and the two known intentional diffs.
- Codegen of `forza_horizon` and `gears_of_war_judgment` under the old and new
  compilers differs in **zero files** — so neither title's diff is attributable to this
  change.
- The whole-fleet gate now completes in **26 seconds**.

### The gate found the wrong compiler again

Preparing this release, a fleet run reported **19 REGRESSIONs** — the same count, and
the same cause, as the v2.26 investigation. `find_rexglue()` listed
`out/install/win-amd64` and not `out/install/win-amd64-pin`; those are different trees,
the first being whatever the box last built for itself. With the pin check switched off
by hand, the log's only record of which compiler produced the verdict was a path, and a
path is not an identity.

Two changes. The release build is now first in the candidate list. And when
`REXAUTO_SKIP_SDK_CHECK` is set, the gate prints the compiler's **sha256**, so a verdict
is attributable to a binary rather than to a directory name. The remedy the gate printed
on that bad run was `--bless`; taking it would have frozen a stale compiler's output as
the fleet's truth. That is the second time, and the reason the check exists.

### From Xenia Canary, validated in gameplay

Adopted by content — our `src/graphics` derives from `src/xenia/gpu` but is
restructured, so these are ports, not cherry-picks:

- **Empty resolve regions** now return true with a zero extent instead of failing.
  Eliminated 20,256 failed draws per run.
- **7-bit float mantissa** decoded with `<< 16`, not `<< 3`. The 3 was copied from the
  20e4 decoder, where it is correct; a 7-bit mantissa needs 23−7.
- **D3D12 scaled-resolve texture dimensions** now scale with the resolve factor. Vulkan
  already did this; D3D12 did not, and the result was device removal — live by default.
- **`XamMediaVerification`** implemented, killing a 600 ms spin in Gears of War
  Judgment (281 calls → 1).
- **`BaseHeap::Reset()`** recomputes `unreserved_page_count_`. Without it, a heap reset
  left the counter permanently stale and the guest was told it had less free physical
  memory than it did.
- **Three `PhysicalHeap` parent-reservation leaks** released instead of TODO'd.
- **`draw_resolution_scale`** defaults to 1, not 2.
- **Perf frame counters** wired up: `ResetFrameCounters()` and `WriteCsvFrame()` were
  fully implemented and called by nobody, and `PROFILE_FRAME_TIME_US` was never invoked
  at all — anything read from that column was leftover array contents.

### Community game patches

`gamepatches.py` matches the xenia-canary game-patches catalogue against a port. The
distinction it enforces is the one that matters for a *static* recompiler: a write
landing in `.text` becomes native code and cannot be toggled at runtime. Each patch
carries the worse of its writes' seals — **RECOMPILAR** or **RUNTIME** — rather than
promising a switch that cannot exist.

Gears of War Judgment ships with **Unlock FPS** and **Resolution Scaling Fix** applied.

### Known, named, not fixed

- **The thunk pool leaks.** `GetProcAddressByOrdinal` calls `AllocateThunk` on every
  invocation with no memoization and no reclamation, so a guest resolving the same
  export twice burns two 4-byte slots. `kThunkReserveSize` was raised 64KB → 1MB, which
  moves the wall from 16,384 calls to 262,144 and fixes nothing. The real fix is a cache
  keyed on (module, target) — which is also what hardware does — but that changes
  dispatcher behaviour fleet-wide and is not runtime-validated, so it is not here. An
  earlier version of that constant's comment cited a "Thunk address space exhausted"
  log line as evidence; **no such line exists in any fleet log, and the claim is
  withdrawn.**
- **`frame_time_us` does not yet validate.** It is measured now, but summed frame times
  come to 2.56x wall clock — almost certainly multi-thread contention on the static
  `last_frame_close`. Treat the column as indicative, not authoritative.

## 2.26.0 — "the law was off" (2026-08-19)

**The regression gate had been enforcing nothing, and nobody could rebuild the SDK it
was supposed to enforce against.** This release fixes both, and the discovery order
matters: the second bug was found *by* fixing the first.

### The gate could not find the fleet, or its compiler

`AUTOPORTS` was hardcoded to `C:\Skate3Recomp\autoports`. The fleet moved,
`projects()` returned `[]`, and `main()` exited *"no matching projects"* — a line that
reads like a mistyped filter, not like *the law is off*. `find_rexglue()` carried the
same dead root, so repairing only the fleet path would still have died one line later.
Both now honour `REXAUTO_WORK`, the env `rexauto.py --work` already reads, so the two
cannot drift apart again; the old roots stay last for a machine still laid out that way.
An absent root now says **DISARMED**.

### The gate never checked which compiler it ran

`regression_gate.py` contained **zero** references to `SDK_PIN`. Its first live run in
weeks gated all 30 titles against an SDK **93 commits behind** the pin — a tree checked
out on a different branch that nobody switched back — reported **19 REGRESSIONs**, and
printed `--bless` as the remedy. Blessing would have frozen a stale compiler's output as
the fleet's truth, permanently. With the pinned SDK the same run is **28/30
byte-identical**: not one title had regressed.

`verify_pin()` now runs before a single title is codegen'd, and the log names the full
path of the binary that produced the verdict. `REXAUTO_SKIP_SDK_CHECK=1` still allows
gating a deliberate SDK change — explicitly.

### The pinned SDK could not be rebuilt, and never matched its own commit

The shipped `rexglue.exe` already carried the `register_cpp.inja` whitespace trim, which
is a **later** commit than the `981cab8` the pin named — so building the named commit
produced a different binary and the pin was unreproducible by construction. Three
independent defects kept it that way:

- `CMakeLists.txt` aborts on MSVC without naming where Clang is expected.
- The preset carries `-march=x86-64-v3`; a raw `cmake` invocation misses it and
  `_mm_shuffle_epi8` fails to compile.
- `INJA_TEMPLATE_FILES` was a hand-kept list that **omitted `register_cpp.inja`**, so
  editing that template did not re-trigger the embed step: you rebuild, the binary keeps
  the old template, and the output is unchanged. Found by experiment — edit, rebuild,
  diff, find it identical. Fixed by adopting upstream `d33efdf` (GLOB_RECURSE +
  CONFIGURE_DEPENDS) by content.

**The SDK now builds from source and its codegen is byte-identical to the old pinned
binary.** `SDK_PIN` is re-pinned to that from-source build.

### Upstream harvest, measured

Two codegen fixes adopted by content from `rexglue/rexglue-sdk` (no shared ancestry):
`10cf1ad` recovers the function tail after a **conditional** `bcctr` — previously any
`bcctr` terminated the function, so everything after a conditional one was dropped — and
`f2b91f2` gives SEH funclets their owner's live non-volatiles. Both are **inert on this
fleet**: adopting them left all 30 titles byte-identical. That verdict was only possible
because the tree builds.

`650a8c3` (`vaddsws`) was **rejected with cause, again**: v2.25 already refused it
because our scalar version also sets `vscr_sat` and theirs does not. It is named here so
a future bulk merge does not silently reintroduce it.

### Static recovery reaches companion modules for the first time

`DEFINE_REX_FUNC` was matched prefix-blind, so every companion module's generated sources
read as **empty**: `func_bodies()` returned `{}`, every deep-extract candidate looked
"swallowed", and the pure-add gate dropped all of them — **46,131 candidates across the
fleet's 10 companion modules, 0 accepted, every time**. Failing closed is why nobody
noticed; it also meant static recovery never contributed one function to Halo 3, FIFA,
Forza Horizon, Sonic or Spider-Man's extra modules. An entrypoint emits no prefix, so
single-module titles are byte-identical by construction.

And deep-extract folded every accepted candidate as a bare `{}` **function head** — but
an address INTERIOR to an emitted function can never be one: registering it asks the
recompiler to split a routine it emitted whole, which it declines by design. So the gate
dropped it and the address was lost. `stage_deepextract` now partitions against the
recompiler's own emitted grid: gap candidates go to the unchanged pure-add gate, interior
candidates go to a landing gate whose contract is that the emitted function SET must be
unchanged and `count_dangling` must be 0, or the whole batch reverts.

### Proof

Full-fleet gate **PASS, 30/30 byte-identical**, pin verified. `func_bodies()` over the
companion modules: **0 -> 139,854**, with the budokai3 entrypoint control unchanged at
11,452. Landings folded with the function set unchanged and 0 dangling: budokai3 +115
(holes 85 -> 84), spider_man/gamelogic +2,526 (holes 2,410 -> 2,364), sonic_adventure +68.

Three baselines re-blessed, each with its cause recorded. `gears_of_war_judgment` is
blessed on the **owner's decision, not on analysis**: its baseline predates the current
pin and its inputs have not changed since 2 Jul, but the previous content is
unrecoverable and the v2.25 jump-table hypothesis does not hold — three of its four
changed files contain no switch case at all.

### Known issues

- One **unexplained transient**: `joust` failed codegen Validate once with 4 unresolved
  calls, then passed twice with nothing changed. Not reproduced; recorded rather than
  dismissed.
- Every port's `CMakeCache.txt`, `_build.bat` and `game_root.txt` carried the dead root
  too. Fixed on this machine's fleet; a port tree from elsewhere needs the same.
- Graphics artefacts under `draw_resolution_scale=2x2` at 1440p are a rendering
  configuration, not a recompilation regression.

## 2.25.0 — "both upstreams, merged" (2026-07-27)

**The fork stops drifting.** rexglue has two living upstreams and we had fallen
behind both: 82 commits behind `mchughalex/rexglue-skate3` (the Skate 3 fork our
SDK descends from) and nothing at all from `rexglue/rexglue-sdk` 0.9.0-dev. This
release harvests both.

### What Skate 3 recomp v2.0 actually had for us

Their headline — "the whole game renders natively, 2x the frame rate at a
quarter of the GPU power" — is a renderer **hand-written for Skate 3**: it hooks
the game's own functions (`Sk8::PresentationEntity::BindConstants`), reads the
scene out of guest memory at reverse-engineered struct offsets, and redraws it
with bespoke HLSL. ~1.2 MB of title-specific C++. That does not generalize and
we did not take it.

What we took is everything underneath it, by **full merge** of
`skate3-sdk-clean@7eb0faf`:

- **Native RHI (nrhi)** — a title-agnostic D3D12 + Vulkan abstraction and the
  native-guest-output renderer hook. **Inert here**: no title registers a
  renderer, so `TryRenderNativeGuestOutput` returns false and the emulated path
  runs exactly as before (verified: no `native`/`nrhi` activity in any launch
  log). It is the door, not the room.
- **Fixes that apply to every title today** — SDL audio credit-pacing
  starvation on large device quanta (the robotic/slowed audio); the timer queue
  blocks instead of yield-spinning; forced-exit watchdog on window close plus
  the process heap lock held across close-time thread suspension (a real
  teardown deadlock: UI thread in `RtlpFreeHeap`, suspended guest thread in
  `RtlpAllocateHeap`); W^X for guest pages; the D3D9 half-pixel offset applied
  in **host** pixels under resolution scaling, which kills resolve-boundary
  seams — we ship 2x2 scaling, so this is live; `GetExecutablePath` via
  `GetModuleFileNameW` instead of `_get_wpgmptr`; NVIDIA
  prefer-max-performance application profile so a load-screen lull cannot park
  the GPU in a low P-state; discrete-GPU preference and a device picker; atomic
  cvar saves with malformed-config recovery.
- Their imgui pin turned out to be a **private, unpublished fork** (it adds
  `ImFontConfig::RasterizerGamma`, which exists in no ocornut branch). The font
  gamma tweak is now compiled in only when the member exists, so the tree builds
  against stock imgui.

### What rexglue 0.9.0-dev had for us

No shared git ancestry — their public history is squashed at "Release v0.8.0",
so `git merge-base` is empty and adoption is by content. Seven picks:

- **DLL `code_base` via `ReXModule_GetImageInfo` (#371)** — companion modules
  now hand the runtime their real image/code layout, so indirect calls into them
  resolve correctly. Verified live: Spider-Man's `gamelogic` module initializes
  its function table at `code=880D0000-886B2F60` with no layout error.
- **Jump-table targets that are known functions stay separate (#370)** — a case
  that used to *call* its landing (`sub_X(ctx, base)`: fresh frame, returns to
  the dispatcher instead of continuing the flow) now does `goto loc_X`, the
  correct lowering of a computed goto. Same bug class as `forced_landings` and
  switch-on-CTR, and the kind of miscompile that never crashes, so run-heal
  never sees it.
- XMA loop wrap emitting garbage and dropping the loop-end frame; ffmpeg buffer
  flush on context release; `XPresenceInitialize`; message-box title/body
  byte-swap; opt-in per-device `FILE_SHARE_DELETE`.

Five upstream commits were **rejected with cause**, not skipped: their inline
XMA decode (we already decode inline, with FPSCR save/restore — theirs would
double-decode), their SIMD `vaddsws` (our scalar version also sets `vscr_sat`,
theirs does not), a `vpkd3d128` revert with no stated rationale, and
"always overwrite generated output" (it kills the hash-skip that keeps heal
rounds incremental). The cooperative `TerminateTitle` drain is deferred — it is
better design than ours, but it rewrites teardown and deserves its own runtime
round.

### Proof

Codegen changed in 18 titles, and the change is an improvement: fleet-wide only
**3 addresses** left a function table (budokai3 x2, crash_mind_over_mutant x1)
and **all 3 became in-function labels** — 0 orphans, 0 dangling labels across
2456 generated files, the rest dead-block removal. Baselines re-blessed, gate
then **30/30 byte-identical**. Runtime: `gears_of_war_3`, `gta_san_andreas`,
`budokai3` and `spider_man_shattered_dimensions` all boot, live 30s, 0 FATAL.
The other 26 titles carry codegen proof only.

## 2.24.0 — "GTA V reaches gameplay" (2026-07-10)

**The milestone: GTA V (545408A7, 2-disc) boots from "insert installation
disc" all the way INTO GAMEPLAY.** v2.23 cured the install-content chain
(enumerator, CD_ROM volume semantics, content mounts, XamSwapDisc completion
event); this release adds the five RAGE boot walls that stood between the
install gate and the game itself — every one a general runtime fix, verified
against the engine's actual expectations:

- **Startup notifications reach every system listener (SDK 80e886c).** RAGE
  registers its own XamNotify listener and waits for the boot notification
  set; delivering only to the first listener left the engine parked forever.
- **XNetGetEthernetLinkStatus reports a live LAN link (a83b685).** The
  network-init gate refused to complete on "no cable"; a dead link parked
  boot before the render loop.
- **XexCheckExecutablePrivilege(11) answers INSECURE (16a4948)** — routes the
  engine's cache traffic to the direct path instead of the privileged one.
- **update: is always mounted — empty when no TU (e063379).** RAGE treats
  device-not-found on update: as fatal; an empty mount is the truthful state.
- **Writable gamecache:/commoncrc: mounts (885018a)** — the engine's scratch
  volumes for streamed/verified data.

Known issue: gameplay freezes intermittently — under investigation (next
release). The install-disc flow generalizes: any 2-disc title stages its
install packages into the user content root and boots the play disc.

SDK_PIN -> 80e886c binaries. Gate: fleet codegen re-verified.

## 2.23.0 — "sibling imports bound" (2026-07-10)

**The Halo 3 "L360" wall, root-caused and fixed structurally (SDK 81ccf82).**
A guest module's imports from a SIBLING guest module (L360.dll <-
WavesLibDLL.dll) were recompiled as raw XEX placeholder thunks — the 360
loader rewrites those at bind time, but recompiled code compiles the
pre-bind bytes: the leftover `mtctr r11; bctr` used a stale r11 that pointed
back into the CALLER, so the first sibling call recursed caller<->thunk until
guest stack overflow (~3ms after the Waves DLLs load). Fix on both sides:
codegen patches each unresolved-import thunk in the loaded image into a real
IAT-slot dispatch (`lis/lwz r11, slot; mtctr; bctr` — exactly 16 bytes) and
registers it as a function; the runtime binds the type-0 slots against the
sibling's export table after every module load, before DllMain, load-order
independent. Single-module titles have none of these: fleet gate 30/30
byte-identical by construction.

**The pipeline got dramatically cheaper on multi-XEX (the fifadllzf case
study, 101k funcs):**
- companion deep-extract and jump-table recovery are now ONE-SHOT per module
  (they re-ran on every build: ~8min of serial IDA + ~15min of codegen
  probes re-paid per heal round; now a module heal round is codegen +
  incremental build + run — zero IDA)
- the pure-add gate skips its opening baseline probe when the caller just
  codegen'd (284s) and the fold re-codegen when nothing was accepted (284s)
- clang OOM is an auto-fixed build failure: halve --parallel, retry
  incrementally, and the lesson PERSISTS in the port statefile (heal-loop
  rebuilds inherit it; the heal loop has the same handler)
- heal rounds no longer pay a full cmake reconfigure (configure only when
  CMakeCache.txt is missing; input changes force it via a stamp)
- the IDA cache key is content-based (analysis scripts + funclist), so
  tooling commits in xenon-jumptables stop invalidating the fleet's cache
- STFS extraction streams (constant memory) — a 2.1GB entry (GTA V install
  parts) MemoryError'd the old whole-file read

**Multi-disc installs: the GTA V "insert installation disc" wall, killed
end-to-end.** An IDA decompile of the game's install state machine
(sub_8299EE40) + xenia-canary source + a working xenia session log pinned
the full chain, and each link got its fix:
1. `XamContentCreateEnumeratorInternal` implemented (was a stub — the
   install discovery enumerated EMPTY and the game asked for the disc);
2. the game volume answers `FILE_DEVICE_CD_ROM` (retail from-disc branch;
   answered DISK the game took the installed-to-HDD-build path);
3. content packages stage EXTRACTED in the real content root
   (`Documents/<title>/0000000000000000/<title_id>/00000002/<pkg>/`) with
   `.header` files generated from the true PIRS metadata ("gtav - part0");
4. content-mount device paths carried a trailing separator that broke
   `<pkg>:\partN.rpf` resolution (double backslash = empty path component);
5. `XamSwapDisc` now signals its completion KEVENT — the one-arg stub
   swallowed the handle, so after the install gate PASSED the game waited
   forever ("stuck loading").
Result: GTA V streams from its mounted install packages and reaches the
loading screen; remaining walls are ordinary run-heal cures. The extractor's
STFS reader streams now (constant memory — the 2.1GB part rpfs MemoryError'd
the old whole-file read, truncation audit preserved). Full `--install-disc`
automation lands next.

xenon-jumptables (live-referenced): closure_cert splitimm exactness — the
whole 30-port fleet now certifies ZERO static holes; extract_funcs matches
companion-module prefixed symbols (the empty-known-list root cause) and
gained a 65x init.h fast path.

SDK_PIN -> 81ccf82 binaries. Gate: 30/30 codegen byte-identical.

## 2.22.0 — "silent-miscompile guard" (2026-07-10)

The ">100%" axis: hunting codegen that is WRONG but never crashes (run-heal
is structurally blind to it). A 15-agent audit of every risk builder vs the
PPC/Xenon/IEEE specs produced 5 candidate bugs; ground-truthing each against
the live source proved 4 are deliberate, game-validated choices (32-bit
carry/CR0 = the 360 ABI's dominant case; vcmpbfp NaN = matches Xenia's
lowering; denormal flush = the documented Skate 3 audio fix) and exactly one
real: **NORMPACKED64 (4:20:20:20) unpack decoded x=y=z to 0.0 always** — the
20-bit sign-extend `int32_t(u64<<44)>>44` was undefined behavior (shift >=
width) and the cast dropped the field (SDK 78af0a8). No shipping port emits
NORMPACKED64, so the fleet gate is byte-identical 30/30 by construction: a
latent cure, zero risk.

**The class is now guarded forever:** `tools/codegen_ub_lint.py` (SDK
09a18ee) statically flags any `intN_t(...) >> K` / `.uNN >> K` with K >=
width inside the emitted templates — green on the current builders,
regression-tested against the pre-fix pattern, exit 1 so it can gate commits.
It also surfaces (non-failing) same-helper flag inconsistencies like the
known FLOAT16_2/FLOAT16_4 RTZ/RTE split, so a NEW divergence gets a human
look.

**rexauto: companion-module deep-extract fixed (was a silent no-op).** For a
multi-XEX companion, stage_jumptables runs before the module's sources exist
and wrote an EMPTY functions-list; stage_deepextract then fed that empty
known-set to IDA, every already-emitted function came back as a "candidate"
(fifadllzf: 92188) and the pure-add gate rightly rejected the lot —
accepted=0 on every companion, and real cures (FIFA Street's 0x827838A0
FATAL) were discarded with the noise. The known-list is now refreshed from
the generated sources whenever it is missing/empty; healthy entrypoints are
untouched.

SDK_PIN -> 09a18ee binaries. Gate: 30/30 byte-identical (budokai3's flagged
diff was a stale baseline — two deep-extract cures folded after its last
bless, proven identical under the v2.21 and v2.22 rexglue alike — re-blessed).

## 2.14.0 — "truthful diagnostics" (2026-07-03)

A Gears of War 3 report ("recompilação falhou") unraveled a three-bug chain
that could send any port's run-heal into a forever loop — all three fixed:

**Ghost targets (SDK codegen, fleet-wide 1-line diff).** The generated
`REX_CALL_INDIRECT_FUNC` only wrote `ctx.last_indirect_target` on the
fallback path; when a call hit an unregistered slot (which holds the trap),
the trap fired reporting a STALE address from an earlier resolved call. The
heal then chased functions that were already registered, forever. The target
is now written unconditionally, and the trap logs `GetFunction(target)`
before aborting — table-miss vs call-path bugs are now distinguishable at a
glance ("trap diagnostics").

**Stale-exe heal rounds (rexauto, since v2.10).** Restoring the init
header's old mtime after added-DECLARE-only diffs is unsound with the PCH:
clang validates the precompiled header against the header's CONTENT, so the
stale PCH failed every subsequent compile. A changed header now always
keeps its new mtime (PCH and TUs rebuild).

**Always-0 build exit code (rexauto, since forever).** `_build.bat` ended
with `echo RC=%errorlevel%` — the echo RESET the errorlevel, so a failed
rebuild returned success and the heal silently relaunched the stale exe.
The bat now propagates the real build exit code (`exit /b`).

Also: per-project tomls are repaired automatically when a writer corrupts
line endings into doubled carriage returns (rexglue's parser hard-fails on those; seen once
in the wild on a frozen-exe jumptables run).

SDK pin: `rexglue.exe` → `c3c1139d`, `rexruntime.dll` → `1840f9ad`
(SDK commit `f9a5ebb`). The codegen diff is one macro line in every port's
init.h — judged and re-blessed fleet-wide.

## 2.13.1 — "game icons" (2026-07-03)

Built exes now carry the game's marketplace tile as their Windows icon.
`exeicon.py` converts the cached XboxUnity tile (the same one the GUI shows)
into a proper multi-size icon group (16/24/32/48/64, classic 32bpp DIBs) and
injects it post-link with the Win32 resource-update API — no .rc file, no
CMake/codegen change, output `.text` untouched. Hooked into `do_build`, so
every heal relink re-brands automatically; best-effort (offline/no-tile just
skips). Fleet pass branded all 18 existing exes.

## 2.13.0 — "transparent decode" (2026-07-03)

**Captain America: Super Soldier reaches GAMEPLAY** — and the wall it was
stuck behind is now a permanent pipeline capability.

**New stage `xctd`** (between extract and init): titles whose assets ship
transparently compressed (XCompress LZXTDECODE, magic `0F F5 12 ED`; the
Xbox 360 KERNEL decompresses these on real hardware) are pre-decompressed in
place, originals kept in `xctd_originals/`. Our runtime stubs
`XFileXctdCompressionInformation`, so the game takes its "not compressed"
path — serving plaintext is exactly what it expects. No-op (0 files) for
every other title: fleet regression-free by construction.

The format was cracked empirically (a 4-agent workflow root-caused a 1-byte
parser desync: zero padding before 128KB boundaries can be ODD-length) and
confirmed against public prior art (QuickBMS `unxmemlzx`, UniPyX). The
decoder (`tools/xctd_rip.cpp` + vendored libmspack lzxd, built on demand
with the pipeline's clang) handles the full matrix:
- header flags: window, pad boundary, segment count, 20-bit/BE32 size table
- raw payloads (segments=0), single-stream bundles, multi-segment archives
- BOTH segment layouts: contiguous chunk stream (Captain America `DATA.*`)
  and zoned/`k*zbs` (XBLA titles — 'Splosion Man/Ms. 'Splosion Man carry 237
  XCTD files each, 834 MB → 2.4 GB)
- BOTH LZX dialects: XMemCompress omits the CAB realign pad byte after
  odd-sized UNCOMPRESSED blocks — per-segment retry with a dialect switch in
  the vendored lzxd. Every decode is fully verified (frame accounting +
  exact produced sizes) before anything is swapped.

**Runtime: XUsbcam stub exports enabled** (SDK commit `1a9ac64`): the
'Splosion Man pair imports the camera API and could not link. Pure export
addition (no existing title touches the stubs); pin → `9b2c8fb1`.

**Run-heal: absorbed-gap/vtable-thunk cure.** When an address is registered
but the runtime still flags it after an exe resync, the cause is a neighbour
function whose emitted body absorbed a functions-list gap containing the
target (Captain America: a 16-byte virtual-call thunk at 0x822A2040 inside
0x822A2010's body). The heal now shrinks the containing function with an
end-override at the target — a true boundary, since the game just
indirect-called it — and retries. Proved live: CA converged in 5 launches,
survived 150s clean.

## 2.12.0 — "clean runtime, long watch" (2026-07-03)

Two operational hardenings, no new features:

**Run-heal confirm window floors at 150s.** Gears of War Judgment converged
"clean" at the old 47s window and then FATAL'd ~71s into gameplay — a function
(`sub_824CA490`) that only loads late. The heal ROUNDS stay fast (22s); only
the initial discover pass and the final convergence check now run ≥150s, so
late-loading indirect targets are cured up front instead of surfacing as a
mid-gameplay crash. Gears re-verified: converged in 1 launch, survived 150s.

**Runtime rebuilt without the exploratory texture-dump path** (a GPU debug
feature, cvar-gated OFF, never shipped in any release). No tracked runtime
source changed (fiber HEAD `afec3c0` + GapFill `9efdddc`); C++ links are not
byte-reproducible so the SDK pin is re-generated to the shipped dump-free
binary (`rexruntime.dll` → `1258109c`). Validated: Gears 150s survive + GTA-SA
gate PASS (codegen 99 files byte-identical, runtime boots alive 0 FATALs,
runtime baseline blessed).

## 2.11.0 — "codegen fast" (2026-07-02)

One SDK codegen fix, found by measuring instead of guessing: GapFill's
absorbed-cleanup was **O(gapfills × total functions)** — ~1.8 billion
`containsAddress` probes at 42k functions, and quadratic growth on bigger
titles (a real slice of GTA V's ~5min-per-codegen passes). Replaced with a
backward walk of the existing sorted-base index (the same one
`getFunctionContaining` uses), bounded by the largest function size. Same
predicate, same removal set ⇒ **byte-identical output**, proven by the
regression gate twice (blessed fleet PASS, 29–125 files identical per title).

**Measured: 8.2s → 31ms per codegen pass on GTA-SA (264×).** Every title pays
this on EVERY codegen pass (setjmp scan, image dump, pure-add gate passes, heal
retries), so it compounds: a fresh GTA V port runs 6–8 codegens.

Honest engineering note: a parallel-Discover attempt was built, adversarially
reviewed, and REVERTED this same session — it gave ~0 warm-run gain (the 19s
"Discover cost" was cold-cache illusion; warm is 2.3s) and the review proved a
real determinism hazard on chunk/parent titles. The gate + adversarial review
did their job. The wall-clock win came from fixing the algorithm, not from
throwing cores at it.

SDK_PIN: `rexglue.exe` → `7e9591d4` (codegen-only); `rexruntime.dll` unchanged
(`20aec5ac`).

## 2.10.0 — "build fast" (2026-07-02)

Pipeline speed, all byte-identical output. **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `20aec5ac`). Every change is
output-neutral by construction and proven by the regression gate (codegen
byte-identical fleet-wide) plus targeted empirical checks. Driven by an 18-agent
codegen-engine audit that measured where the time actually goes.

- **Heal loop 70s → 6s (11×), measured on GTA San Andreas.** Two build-flag
  fixes: (1) **PCH resurrected** — the v2.4.0 precompiled-header win had silently
  regressed (the injection ran before `<name>_init.h` existed, so its exists()
  guard skipped it; a fleet audit found 1/18 ports actually had the PCH). Now
  wired after codegen, idempotent. (2) **`-gline-tables-only`** on RelWithDebInfo:
  keep function symbols + line tables (cdb guest stacks still resolve) and drop
  the variable/type debug info that bloated the PDB (~100MB → ~63MB) and dominated
  every relink. Both output-neutral (PCH caches the AST, debug flag is debug-info
  only) — `.text` stays byte-identical.
- **Global IDA cache.** The jump-table IDA pass — the pipeline's one serial
  single-core minutes-long sink — is fully determined by the image bytes. Keyed
  by sha256(image) + section ranges + xenon-jumptables rev; a re-port of the same
  game (or a wiped work dir) copies the cached switch_tables.toml + .i64 instead
  of re-analyzing (GTA SA: 2min5s → instant; the .i64 reuse also speeds
  deep-extract). `REXAUTO_NO_IDA_CACHE=1` to disable.
- **setjmp/jumptables image-dump merge.** The setjmp stage ran a FULL codegen
  purely to dump the guest image, which the jumptables stage then re-dumped
  identically. The image dump is the raw decompressed sections
  (project_recompiler.cpp:251), independent of setjmp/functions.toml — proven
  byte-identical across two dumps. jumptables now reuses the setjmp stage's image
  + ranges: **−1 full codegen per port** (GTA SA ~46s, GTA V ~4min).
- **pure-add gate: fewer codegen passes.** deep-extract's pure-add gate re-ran a
  full codegen to restore generated/ (thrown away — stage_build re-materializes it
  from the authoritative toml) and a redundant final safety pass when the loop had
  already converged. Both dropped; the accepted set (the only thing that reaches
  functions.toml) is unchanged. GTA V's ~14min gate roughly halves.

Audit also mapped the SDK-side wins (parallelize the Discover/GapFill worklist to
use all cores, GapFill decode-once) — those need an SDK change + pin bump and land
in a later release, gated the same way.

## 2.9.0 — "fibers" (2026-07-02)

Guest fiber support in the runtime + truncated-container guards in the pipeline.
**SDK runtime changed** (`rexruntime.dll` → `20aec5ac`, `rexglue.exe` unchanged
`06b93244` ⇒ codegen byte-identical fleet-wide; gate all-blessed PASS + runtime
spot-checks).

- **Guest fibers (SDK runtime):** `XThread::Reenter` + `reenter_exception`, and
  `KeSetCurrentStackPointers` now unwinds the host stack back to `XThread::Execute`
  and re-enters guest code at the new fiber's LR when the guest swaps fibers — the
  exact mechanism mainline Xenia uses. Reentry addresses (often MID-function: the
  fiber's own `bl SwapContext` return site) resolve via `ResolveIndirectFunction`,
  so unregistered sites flow into the standard run-heal machinery instead of
  silently ending the thread. Gated on `X_KTHREAD::fiber_ptr`: titles that never
  fiber-switch (the entire pre-Korra fleet) never take the new path. Unlocks the
  PlatinumGames digital titles (Korra/Transformers Devastation/TMNT) and the
  Halo 3/Reach/4 + Forza 2 class (xenia-project label
  kernel-KeSetCurrentStackPointers, 15 titles). Proven live on Korra (58411447):
  dead-at-boot → engine fully up (~20 guest worker threads, input polling, XMA
  audio, real render pipeline; shader storage 6→8).
- **Truncated-container guards (extract):** an incomplete download extracts
  SILENTLY broken — `Stfs.read_chain` reads past-EOF blocks as empty, so a
  truncated STFS yields short/0-byte files that later surface as unexplainable
  runtime behavior. extract now audits every written file against its table
  length and FAILS with the file list + "re-download" hint; a folder source gets
  a 0-byte-file audit with a loud warning. This exact class cost an hours-long
  hunt on Korra: the final wall was a 0-byte `Nickelodeon.usm` intro movie from a
  truncated `.zip.part` download — the game opens it, our kernel honestly reports
  size 0, CRI Mana errors, and the title black-screens forever. Recompilation and
  runtime were correct end-to-end (proven by instrumenting every link of the
  chain: wrapper status poll → Mana internal state → CriFs GetFileSize →
  GetFileSizeEx → NtQueryInformationFile(class 34) → VFS → the file really is 0
  bytes on disk).

## 2.8.0 — "launch once" (2026-07-02)

Verification stops re-launching the game. A cured title now launches **twice ever**
(a priming boot + one long-window confirmation) and then **zero times** on every
subsequent pipeline run, via a persisted convergence receipt. Directly targets "the
pipeline keeps opening and closing the game". **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `4e75b494`).

- **Convergence receipt (Tier 0 = 0 launches):** `<name>_runheal_receipt.json`,
  fingerprinted by sha256(exe) + sha256(rexruntime.dll) + sha256(xex) [+ title-update]
  + game root. Matching receipt (verified with a window ≥ the one requested now) ⇒
  runheal doesn't launch at all; any real change (codegen/cure/SDK/re-rip/game swap)
  changes a hash and re-verifies automatically. `REXAUTO_FORCE_RUNHEAL=1` or deleting
  the receipt forces a live check. Receipts are minted only on POSITIVE evidence:
  game still alive at window end, real code range known (not the fallback window),
  a log actually produced.
- **Merged discover+confirm (Tier 1 = minimal launches):** the old
  discover(22s)×N → fatal(22s) → confirm(47s) dance is one loop of discover-mode runs.
  Soundness: a discover run that logs **zero targets at all** no-op'd nothing, so its
  execution is bit-identical to a clean run — it doubles as the confirmation. Heal
  rounds keep the short window for fast iteration; only the deciding clean run pays 47s.
- **Second-boot coverage kept:** the deciding run must not be the guest state's
  first-ever boot (first boot creates saves/caches; load-existing-state paths — the
  v2.6.0 xam_content crash class — only run on boot 2). A priming marker keyed to the
  guest fingerprint (not stale log files) tracks this.
- **Honest multi-XEX verdicts:** convergence keys on zero **logged** targets, not zero
  in-range. An out-of-image/misaligned call that discover mode no-op'd would FATAL a
  production run — that now yields a "production_fatal" verdict with the uncurable
  list, never a "survived" receipt (old flow's fatal-confirm honesty, preserved).
- **Corrupted-continuation guard:** when a discover run logs uncurable targets
  alongside curable ones, everything after the first no-op ran on corrupt state — one
  fatal-mode run at the SAME window re-reads ground truth before anything is
  registered; a clean fatal re-read is treated as inconclusive, never as a verdict.
- **`_code_range` actually reads the range now:** a doubled "default" path segment made
  it silently fall back to the generic 0x82000000–0x84000000 window for EVERY game
  since inception — the out-of-image guard never used the real per-title code range.
  Fixed; the fallback (exact=False) additionally blocks receipt minting.
- **Failures fail:** no-exe / no-log / rebuild-failed paths raise SystemExit (like
  every other stage) instead of writing a truthy state mark that made the next
  pipeline run print "skip runheal (done)" for a stage that verified nothing.
- Hardened by two adversarial review workflows (4 + 2 agents): 11 findings folded in
  (zero-logged vs zero-filtered convergence, no-evidence receipts, guest-image
  fingerprinting, receipt window honoring, --publish-gabarito on receipt hits,
  label-heal rc re-check, chronological exemplars, plain-retry bounding).
- Proven live on budokai3: run 1 = "converged in 2 launch(es)" + receipt; run 2 =
  "receipt matches → not launching the game". Codegen untouched ⇒ byte-identical
  fleet-wide.

## 2.7.1 — "resync" (2026-07-02)

Run-heal no longer declares a false wall on a stale exe. **rexauto-only; SDK unchanged**
(`rexglue.exe` `06b93244`, `rexruntime.dll` `4e75b494`). Codegen byte-identical across the
fleet (gate: 8 blessed games PASS, only the two intentionally re-ported games changed).

- **Resync-before-stuck** (`stage_runheal`): when a FATAL names a function that is ALREADY
  registered in the current sources, the running exe can lag the codegen — an earlier
  codegen (deep-extract gate churn, or a prior no-op heal) leaves `register.cpp` newer than
  the linked exe, so the built exe's dispatch tables never got `SetFunction(addr)` → a
  SPURIOUS "invalid or unregistered function" fatal on a function that source-registers
  fine. Run-heal now forces one codegen+relink to resync the exe and retries; only if the
  same address STILL fatals after a clean relink is it declared a genuine wall (anti-loop
  guarded, one resync per address).
- **Why it matters:** this exact stale-exe case made **dbz** (`dragon_ball_z_ultimate_tenkaichi`)
  look like an unfixable runtime wall at `0x82415F90` (a registered vtable method) when a
  plain relink converged it. The false "stuck" verdict is the kind of thing that wrongly
  concedes a title as "not recompilable".
- **Fleet: 4 → 5 stable.** dbz re-ported on the v2.7.0 pipeline now converges (47s, into
  gameplay context). **budokai3** re-generated fresh (no hand-tuned `.WORKING74`/`.corrected69`
  cruft): 75 switch tables (≥ the old 74), +69 deep-extracted functions, run-heal a **no-op**
  (deep-extract cured everything statically) — the pipeline alone matched months of manual
  tuning. Both blessed.

## 2.7.0 — "cure once" (2026-07-02)

Static function/vtable recovery is now a **pipeline stage** — a game's "invalid
function" cures are found up front from ONE deep IDA pass instead of by launching the
game N times, so run-heal is left as a rare backstop. Directly targets the "I keep
re-curing every game in runtime" pain. **rexauto-only; SDK unchanged** (`rexglue.exe`
`06b93244`, `rexruntime.dll` `4e75b494`; `deep_extract.py` already in the bundled
xenon-jumptables).

- **New `deepextract` stage** (`extract → init → setjmp → jumptables → deepextract →
  build → runheal → run`): reuses the `.i64` the jumptables stage already produced
  (copied, never the original), runs a deep IDA pass (funcmap ∪ vtable data-xref) to
  harvest the function/vtable-target set the linear scan misses — ~96% of what run-heal
  otherwise discovers dynamically.
- **The pure-addition gate** (`deepextract.py`): a candidate is folded in ONLY if adding
  it is a pure addition — it codegens to its OWN new function with a real (non-stub)
  body, introduces no dangling `goto` (a split), and changes no pre-existing function's
  body. Inspects the ACTUAL codegen output, so it structurally forbids the crash-mask (a
  return-only stub that would turn a real "invalid function" abort into a silent return).
- **run-heal kept as the backstop** for the genuinely-dynamic residue (~4%).
- Proven on joust: 282 candidates → gate accepts 67 (drops 215 as swallow/stub/split) →
  builds + boots + survives 47s, run-heal a no-op (`discover round 1: 0 new`). The wall
  `0x823010C8` (a live vtable-dispatch crash) is cured statically, before the game runs.
- Opt-in on IDA (no idat / no `.i64` → skip → byte-identical), fully additive
  (superset-only `{}`). Zero regression: codegen byte-identical across the fleet.

## 2.6.0 — "Gears boots" (2026-07-01)

A one-line **runtime** fix (found by a multi-agent IDA-Pro diagnosis) makes Gears of
War Judgment boot **stably deep into startup** — it now survives 47s+ alive with no
fatal (was a non-deterministic ~5s crash). **SDK runtime changes** (`rexruntime.dll`);
`rexglue.exe` (codegen) is byte-identical, so the fleet's generated code is untouched.

- **Root cause (a dangling guest string_view):** `xeXamContentCreate` captured
  `root_name = root_name.value()` into its deferred-completion lambda. `MappedPtr<char>::
  value()` returns a `std::string_view` over GUEST memory (no copy); the completion runs
  ~100ms later, by which time the guest recycled the buffer. If the recycled bytes were
  not valid UTF-8, the content-path conversion (the checked utfcpp API) threw
  `utf8::invalid_utf8` → `REX_FATAL("...threw 'Invalid UTF-8'")`. Even when benign, the
  save package mounted under a garbage root, so the `SG0_0:` save device never resolved.
- **Fix:** own the bytes at call time — `root_name = std::string(root_name.value())`.
  Semantics-preserving for any well-behaved caller; only fixes the recycled-buffer case.
  On Gears: the crash is gone AND `SG0_0:` now mounts (via `\Device\Content\N\`).
- Zero regression: codegen byte-identical across all 10 baselined fleet games (rexglue
  unchanged); the change is one localized, semantics-preserving capture. `SDK_PIN`
  `rexruntime.dll` bumped; `rexglue.exe` `06b93244` unchanged. Gears baseline re-blessed.
- Honest limit: the remaining Gears walls (a media-verification watchdog it tolerates,
  intro-movie playback) are runtime/GPU emulation, not recompilation.

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
