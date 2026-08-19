# LOOP.md — rexauto pipeline optimization ledger

> Created by the read-only fleet audit of **2026-08-19** (`it-000`). Append, never rewrite history.
> Machine of record for every number below: **16 cores / 31.1 GB RAM**, Windows 11 Pro 10.0.28000.
> Repos at seed time: `rexauto` @ `0e20d05`, `xenon-jumptables` @ `019b0c4`. Both trees dirty with
> **untracked scratch only** (`_*.log`, `gamepatches.py`, `ida_extract_probe.py`) — nothing to finish
> or revert before iteration 1.

---

## Scoreboard

Per benchmark title (§4 set: small `joust`/`ms_pac_man` · medium `budokai3`/`rayman3hd` · heavy
`grand_theft_auto_v`/`fifa_street`+`fifadllzf`). **No column here is a timed A/B run.** Every value is
read out of an artifact already on disk. Per-stage cold/warm seconds do not exist and stay
**UNMEASURED** until Q4 ships — `.rexauto_state` has no `timings` member on any of the 30 ports
(verified: key union across all 30 files is `build · build_parallel · deepextract · extract · init ·
jumptables · runheal · setjmp · xctd`).

| title | class | idat `jumptables` s | of which blind `auto_wait` | ninja Σ s | ninja max makespan s | tables | deepx cand→acc | heal iters | confirm window s | holes | health_tier |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ms_pac_man` | small | 15.6 | 14.6 (93.6%) | 155.4 | 10.1 | 128 | 148→37 | 2 | 47 | UNMEASURED† | UNMEASURED |
| `joust` | small | 24.3 | 22.1 (90.9%) | 194.0 | 12.8 | 209 | 282→67 | 1 | 360 | UNMEASURED† | UNMEASURED |
| `budokai3` | medium | 23.0 | 21.4 (93.0%) | 153.0 | 11.6 | 75 | 118→2 | 1 | 360 | UNMEASURED† | UNMEASURED |
| `rayman3hd` | medium | 43.8 | 38.9 (88.8%) | 567.0 | 40.3 | 250 | — (no `deepextract` key) | 1 | 360 | UNMEASURED† | UNMEASURED |
| `grand_theft_auto_v` | heavy | 205.5 | 194.9 (94.8%) | 1398.5 | 14.0 | 527 | 492→228 | 1 | 360 | UNMEASURED† | UNMEASURED |
| `fifa_street` (entry) | heavy | 211.8 | 175.5 (82.9%) | 1521.6 | 170.8 | 24 | 1067→28 | 3 | none recorded‡ | UNMEASURED† | UNMEASURED |
| `fifa_street/fifadllzf` | heavy companion | log overwritten (Q4) | — | (in entry row) | — | — | 1363→**0** | — | — | UNMEASURED† | UNMEASURED |
| `skate3` | flagship | **NO PORT** | — | — | — | — | — | — | — | UNMEASURED† | **3** (baseline only) |

Column definitions — do not reinterpret them later:
- **idat `jumptables` s** = the last cumulative `[xjt] timing recognition` lap in
  `<work>/<title>/jumptables.json.idalog.txt`. These laps are *cumulative-since-start*, not
  per-phase — summing them is wrong and produced a 4× overcount on the first attempt this session.
  A log exists only when `idat` actually ran (`rexauto.py:446` returns early on a cache hit), so every
  value here is a **cache-miss** IDA pass.
- **blind `auto_wait`** = the `initial-analysis` lap, i.e. `ida_jumptables.py:69`, which runs *before*
  the function list is loaded at `ida_jumptables.py:76-90`.
- **ninja Σ s** = Σ over unique outputs of that output's *last recorded* edge duration in the retained
  `.ninja_log` (v7) — a serial-equivalent recompile cost for the current output set, **not** a wall clock.
- **ninja max makespan s** = largest single-invocation `max(end)-min(start)` in that log. For heal-heavy
  ports (e.g. `grand_theft_auto_v`, 133 invocations) this is a *heal round*, not a cold build.
- **confirm window s** = `runheal.confirmed_seconds` — the window the launch was *asked* to survive
  (floored at 360 by `rexauto.py:1875`), an **upper bound**, not measured wall clock: `run_once` breaks
  early on process exit (`rexauto.py:1693-1696`).
- † **holes**: `closure_cert` cannot run on any port (roots hardcoded to a tree that does not exist —
  `closure_cert.py:21/:28/:60`, funclist path `:35`) *and* its coverage predicate is vacuous (see
  **R3**). No per-title hole count has ever existed. Do not seed this column from `CHANGELOG.md:166-167`.
- ‡ `fifa_street` and `forza_horizon` record `runheal` as bare `{"iters": N}` — the max-iterations
  non-convergence shape (`rexauto.py:2196`), which the truthy resume test at `rexauto.py:2599` freezes
  as "done" forever.

---

## Fleet ratchet

One row per iteration. Totals only. **Every cell is monotonic** — a cell that goes the wrong way is a
revert, not a trade-off to argue in prose.

| iter | titles building | titles converging | converging w/ 0 launches | titles w/ 0 holes | titles at tier 3 | fleet cold seconds |
|---|---|---|---|---|---|---|
| it-000 (seed) | **29 / 30** | **17 / 30** | **0 / 30** | **UNMEASURED** (0 measurable) | **0 / 30** | **UNMEASURED** |

How each seed cell was counted, so the next row is comparable:
- **building** = a `build.exe` key in `.rexauto_state`. 29 of 30; the miss is `rise_of_the_tomb_raider`,
  whose `setjmp` stage carries `{"skipped": "codegen-fail"}` and which never reached `build`.
- **converging** = `runheal.alive == true`. 17. Breakdown of the other 13: 9 record `alive=false`
  (`final_exam`, `halo_3`, `laracroftandtheg`, `msmauto`, `mssplosionman`, `sonic_adventure`,
  `spider_man_shattered_dimensions`, `superman_returns`, `wwe_smackdown_vs_raw_2007`), 2 record bare
  `iters` with no verdict (`fifa_street`, `forza_horizon`), 2 have no `runheal` key at all
  (`ben_10_the_rise_of_hex`, `rise_of_the_tomb_raider`).
- **0-launch convergence** = a tier-0 receipt with `launches: 0`. There are **18** receipts on the
  fleet and **none** has `launches: 0`; recorded total is **53 launches** across those 18, and the
  final confirming windows alone sum to **4911 s of guest run time** (12×360 + 3×150 + 3×47).
- **0 holes** = structurally unmeasurable today (see † above and **R3**). Recorded as UNMEASURED, *not* 30.
- **tier 3** = a runtime baseline with `health_tier: 3` **and** a discoverable port. Only `skate3` is
  tier 3 and it has **no port** under either root, so the discoverable fleet is 0. Of the 3 runtime
  baselines, `gears_of_war_3` = tier 2 and `gta_san_andreas` = tier 2 are the only two checkable titles.
- **fleet cold seconds** = UNMEASURED. Two prior estimates conflict — **1090.9 s** (cost lens) and
  **1294.1 s** (fleet-reach lens) — and **neither reproduced this session** under three natural
  definitions of "fleet ninja wall" (last-invocation makespan Σ = 231.9 s; largest-invocation makespan
  Σ = 710.8 s; last-duration-per-unique-output Σ = 18 005.7 s, over 4512 edge records / 2580 unique
  outputs in 30 `win-amd64-release` logs; 38 `.ninja_log` files exist fleet-wide once
  `rexpickup-verify`, `android-arm64`, `fpspatch`, `perfprobe` and `win-vk` build dirs are counted).
  **Pinning one definition is part of Q4.** Until then this cell stays empty — a guessed second here
  poisons every later row.

---

## Queue (merged, ranked)

Merged from three lenses (cost / ratchet-safety / fleet-reach). **The ratchet-safety ordering is the
spine** — the project's own tiebreak makes it so: §5 invariant 1 says *the gate is law*, and two of the
three lenses independently put the gate first. Grafted in from the runners-up: the fleet-reach lens's
companion-module prerequisite (promoted to **Q2**, because Q7's entire blast radius lives in
directories the gate never regenerates), the cost lens's back-fill harvester and per-module IDA log
path (folded into **Q4**), and the cost lens's `make_elf` text-range lever (**Q10**), which neither
other lens saw. One item is in the queue that no lens listed standalone but the charter mandates:
**Q6**, because §5.2 says that when a change's blast radius has no runtime baseline, *blessing one first
is the iteration*.

### Q1 — Revive `regression_gate.py` against the live fleet: one commit, four edits, non-destructive first run
**axis:** precision (+coverage) · **files:** `regression_gate.py`, `rexauto.py:2557`
The fleet law currently enforces nothing. `main()` calls `find_rexglue()` at `regression_gate.py:316`
**before** `projects()` at `:317`, and `find_rexglue`'s only candidates (`:70-72`) are `$REXGLUE` (unset)
and two paths under `C:\Skate3Recomp\rexglue-sdk` — **verified absent**; the live binary is
`C:\Skate3\rexglue-sdk\out\install\win-amd64\bin\rexglue.exe` (3 922 432 bytes, 2026-07-13). So it
`sys.exit`s at `:76` without examining one title. Behind that, `AUTOPORTS` (`regression_gate.py:41`)
points at `C:\Skate3Recomp\autoports`, which holds **exactly one** manifest-less directory
(`gears_of_war_judgment`), while the live fleet is `C:\Skate3\autoports` (44 entries, **30 discoverable**
as `<d>/port/<d>_manifest.toml`). Second reach multiplier: `regression_gate.py:342-345` skips the runtime
tier for any title whose codegen verdict is not `PASS`/`BLESSED`, so the dead gate is also what pins
runtime coverage at 2 checkable titles.

Four edits, **one commit**:
1. Resolve rexglue through the `SDK_PIN` logic `rexauto.py:89-91` already uses — not a fourth literal.
2. Change the shared root default in **both** `regression_gate.py:41` and `rexauto.py:2557` together,
   and **abort if the two resolved roots differ**.
3. Baseline-coverage assertion, conditioned on `not names` (`:315`): enumerate `baselines/*.json` minus
   `*.runtime.json` (**33**), subtract discovered projects (**30**), print `gating 30 of 33 baselined
   titles`, emit a MISSING-PORT row per orphan (**`dante_s_inferno`, `game`, `skate3`**), add them to
   `bad`, exit non-zero. Assert separately that every `*.runtime.json` has a discoverable port —
   `skate3.runtime.json` does not.
4. Snapshot each port's `generated/` before the run. `run_one` (`:115-116`) calls `codegen()`
   **unconditionally first**, `codegen()` (`:105-107`) runs rexglue with `cwd=port`, `snapshot()` (`:137`)
   hashes afterwards, and **nothing restores it** — there is no equivalent of `rexauto.py:552-574`'s
   `_gen_restore_unchanged`. Repointing without this rewrites 30 live trees with current-SDK output.

**blocked_on:** nothing analytical. First invocation is a **build-lane** action: codegen tier only, no
`--bless`, `generated/` snapshotted first. Wall clock is **UNMEASURED** — the gate performs zero
self-timing (`time` imported at `:34`, used only inside `_launch_headless`); wrap `run_one` in
`time.perf_counter` and write one CSV row per title.
**acceptance:** 30 titles enumerated, 3 orphans named, every port's `generated/` byte-identical before
and after, per-title verdicts recorded verbatim as the first real Fleet-ratchet row. Any REGRESSION is
triaged per invariant 1 (addresses that left a function table, orphans, dangling labels) and **never**
reflex-blessed.

### Q2 — Regenerate companion-module manifests in the codegen tier; label PASS as "regenerated N / carried-over M"
**axis:** coverage · **files:** `regression_gate.py:79-108`
The gate discovers only `<d>_manifest.toml` (`:79-87`) and codegens only that one (`:105-108`), but
`snapshot()` globs `generated/**` recursively (`:90-103`) and reports `len(snap)` at `:146`. **358 of 2771
baselined files (12.92%)** are therefore re-hashed without any rexglue process having regenerated them
for the change under test — `fifa_street` 201/214 (94%), `spider_man_shattered_dimensions` 50/76,
`forza_horizon` 40/161, `skate3` 35/149, `halo_3` 24/84, `sonic_adventure` 8/63. Commit `0e20d05` says it
in the repo's own words. Q7 moves **323 baselined files across 5 titles**, all of them inside exactly
those directories — with the gate blind there, the queue's largest completeness item would land unproven.

The **labelling half** (split the PASS detail into `regenerated N / carried-over M (unproven this run)`
while continuing to hash and diff all M) is pure reporting, output-neutral, and shippable with Q1.
The **regeneration half** carries two traps: `rexauto.py:1450-1453` (echoed `:1982`, `:2153`) records that
an extra-module codegen rewrites `generated/rexglue.cmake` to point at the last extra and only a
subsequent **entry** codegen restores it — and `snapshot()` hashes only `*.cpp`/`*.h` (`:93`), so a
clobbered `rexglue.cmake` is invisible; and `rexauto.py:1287-1289` records that a *bare* companion
codegen fails validation with 12 fatals on `fifadllzf`, so it must reuse `do_codegen`'s semantics, not
raw rexglue. Re-run the entry codegen last and assert the restore.
**blocked_on:** Q1.

### Q3 — `mark()` becomes an atomic tmp + `os.replace` write, with an explicit close
**axis:** precision · **files:** `rexauto.py:137-140`
`mark()` is `json.dump(st, open(self.statefile, "w"), indent=1)` — truncate-then-write, no tmp, no
explicit close. `load_state()` (`:131-135`) is a bare `except Exception: return {}`, the resume test at
`:2599` is boolean, and `rexauto.py:120-126` silently falls `ctx.game` back to `_game_out` and nulls
`ctx.xex`/`tu_xexp`. A torn write therefore converts a checkpointed port into a from-scratch re-run
with no error anywhere — against invariant 7. No atomic-write helper exists anywhere in `rexauto\*.py`.
~2 lines, output-neutral, cannot make any title worse. **Must land before Q4 fattens that file.**
Implement as an explicit `with open(tmp,"w") as f: json.dump(...)` then `os.replace` (see **R11**).
**blocked_on:** nothing. Verification is a **plain no-flag** re-run of an already-complete title,
asserting `skip <stage> (done)` (`rexauto.py:2600`) fires for every prior mark, plus a byte diff of the
state file (see **R16**). Build lane.

### Q4 — Per-stage timing instrument, shipped as a harvester, plus the per-module IDA log path fix
**axis:** speed · **files:** `rexauto.py` (Ctx, `:393`, `:447`, `:456-457`, `:639-661`, `:1495`, `:1503-1504`, `:1667-1713`, `:147-153`)
This is §7's own nominated iteration and the precondition for every speed claim in this queue. Shape is
safe: `STAGES` (`rexauto.py:50`) has no `timings` member, `build_parallel` (`:1558`) is existing precedent
for a non-stage top-level key, and the gate's comparison surface is only `generated/**/*.cpp|*.h`
(`regression_gate.py:88-100`) — neither file is visible to it.
Three parts:
- **(a) writer** — a stage-frame timer writing append-only rows to `<work>/.rexauto_timings.jsonl`
  (opened `'a'`, closed per record) plus a compact roll-up under `.rexauto_state["timings"]`, written at
  most once per stage completion, **never inside a retry loop**. Frames at the real choke points, one
  each: `do_build` (`:639-661`), `run_once` (`:1667-1713`), the jumptables idat call (`:456-457`), the
  deepextract idat call (`:1503-1504`), the `.i64` copy (`:1495`), and every rexglue codegen through the
  single wrapper at `:147-153` (which also counts the pure-add gate's probe passes, since `codegen_fn` is
  a lambda over it).
- **(b) back-fill harvester** — read-only, mines the three artifact families already on disk into the
  same schema so the first real Fleet-ratchet row carries numbers before a pipeline runs: per-edge
  `.ninja_log` v7 (30 ports), the `[xjt]` cumulative laps (26 modules — **1907.1 s total, 1736.9 s / 91.1%
  in the single blind `auto_wait`**, reproduced exactly this session), and runtime-log timestamps.
  **It must pin one definition of "fleet build seconds"** and publish it, because the two prior
  estimates conflict and neither reproduces (see Fleet ratchet note).
- **(c) per-module IDA log path** — `rexauto.py:393` and `:447` hand every module the *same* log path, so
  on the 5 multi-module ports the entrypoint's laps are overwritten by a companion's. The loss has
  already happened: `msmauto` and `mssplosionman` idalogs contain **no laps at all**, and the 1907.1 s
  figure is a **floor**, not a total. Fold into the same commit; touches no generated output.
**blocked_on:** Q3. Placement decision required: `.gitignore:10`'s literal `.rexauto_state` pattern
covers neither `.rexauto_timings.jsonl` nor `.rexauto_state_<key>`; they stay out of the repo only
because `--work` defaults outside the repo root and `autoports/` is ignored at `.gitignore:9`.

### Q5 — Split the state-mark vocabulary: free read-only scan → `mark_incomplete` inert → opt-in un-freeze
**axis:** precision · **files:** `rexauto.py:137-140`, `:2599`
`mark()` writes any truthy value and `:2599` is `state.get(stage)`, so ~20 sites that encode a
degradation freeze the stage forever as "done". **Measured blast radius is 4 titles, not a fleet fire:**
exactly 2 ports carry a `"skipped"` mark — `laracroftandtheg` `jumptables = {"skipped":"codegen-fail"}`
(that port ships **no `*_switch_tables.toml` at all**, `alive=false`, never converged) and
`rise_of_the_tomb_raider` `setjmp = {"skipped":"codegen-fail"}` (`rexauto.py:253-265`: without
setjmp/longjmp an exception-using title leaves a non-volatile register corrupted and crashes at
startup; that port is the fleet's only non-building title) — and exactly 2 carry the bare-`iters`
non-convergence shape (`fifa_street` 3, `forza_horizon` 2).
**Step 1 is the scan** and it is free: classify every stage value across all 30 state files as
success / skipped / non-converged, and distinguish an *absent* key (9 titles missing `deepextract`,
4 missing `setjmp`, **30 missing the terminal `run` stage**) from a skipped-with-cause key — today
those behave identically on re-run and mean opposite things.
**Carve-out that must be explicit:** sites encoding a genuine determination (`rexauto.py:330`
`found:False`; `:1511`/`:1522` deepextract ran-and-accepted-nothing) must **not** route through
`mark_incomplete`, or the change re-runs proven negatives and weakens prove-or-skip.
**blocked_on:** scan and inert landing are unblocked. Un-freezing is blocked on Q1 (a `jumptables`
re-run rewrites `switch_tables.toml` → codegen), behind an opt-in flag, re-blessed one title at a time.

### Q6 — Bless a runtime baseline for one multi-XEX title — the charter says this *is* the iteration
**axis:** coverage · **files:** `regression_gate.py:252-307`
Verified this session and it is the sharpest risk in the whole queue: the **3 runtime baselines**
(`skate3`, `gta_san_andreas`, `gears_of_war_3`) and the **5 titles carrying a `port/*_modules.toml`**
(`fifa_street`, `forza_horizon`, `halo_3`, `sonic_adventure`, `spider_man_shattered_dimensions`) are
**disjoint sets** — and 3 of the 5 record `alive=false`. Q7 lands exclusively on titles where a runtime
regression is invisible by construction. §5.2 is unambiguous: *bless one first — that is the iteration.*
`--bless --runtime` already exists; all 5 titles have game data. Stage **one title at a time**; one
blessing does not cover five.
**blocked_on:** Q1 (`:342-345` skips the runtime tier unless the codegen verdict is PASS/BLESSED).

### Q7 — Fix the prefix-blind `DEFINE_REX_FUNC` regex in **both** `deepextract.py:25` and `jt_landings.py:31`
**axis:** completeness · **files:** `deepextract.py:25`, `jt_landings.py:31`
The single largest static-recovery item in the fleet. Both files carry the byte-identical token
`re.compile(r"DEFINE_REX_FUNC\(sub_([0-9A-Fa-f]{8})\)")` — bare-only. For every companion module
`func_bodies` (`deepextract.py:51-57`) returns `{}`, `new_heads` is empty, `drop` is the whole candidate
set on pass 1, and `accepted=0` is **structurally forced** while the log prints the reassuring
"converged after 2 passes" (`:145`). **Measured exactly this session: 10 module statefiles, 46 131
candidates, 0 accepted, 10 of 10.** The same blindness at `jt_landings.py:31` **is** reached on module
views via `do_codegen → _jt.heal` (`rexauto.py:610`) → `detect_landings` (`jt_landings.py:153`), so
switch-on-`ctr` landing healing is *also* a silent no-op on all 10 modules — fix both in one change or
the gate starts folding module functions while the bctr-landing healer stays blind. The correct form is
already proven at `extract_funcs.py:23`.
**blocked_on:** Q1, Q2 and **Q6**. Acceptance criterion: `default/*` byte-identical per file. Post-fix
pass count and wall-clock delta are **UNMEASURED** — the module gate is pinned at 2 passes today because
pass 1 rejects everything; post-fix `deepextract.py:123`'s `range(1,7)` can run 6, plus the final pass at
`:154-157`, plus a `do_codegen` refold. `deepextract.py:112`'s `~284 s` is a **source comment, not a log
line**. No log under the fleet contains any `[mod:` line; the instrument is a re-run with
`REXAUTO_MODULE_DEEPX=force` reading Q4's stage frames.

### Q8 — Capture `codegen_fn()`'s return code in `pure_add_gate`; mark the pass INDETERMINATE instead of folding it into `drop`
**axis:** precision · **files:** `deepextract.py:119/:126/:157`, `rexauto.py:1515-1516`
Three lines, output-neutral while logging-only, and it *is* the measurement — no artifact anywhere
distinguishes "all candidates unsafe" from "the probe codegen died". The rc is discarded at all three
call sites and the call site itself (`rexauto.py:1515-1516`) is a bare `rexglue(..., capture=True)` which
neither raises nor checks (`:147-153`). Since rexglue emits nothing on a failed Validate
(`rexauto.py:1295`), `generated/` is unchanged, `new_heads` is empty, and the whole accepted set is
dropped under an honest-looking log line. Demonstrated false verdict:
`dragon_ball_z_ultimate_tenkaichi` reports 247→0, yet 11 of the 13 entries in its shipping
`functions.toml` are inside that 247, in the identical bare `{}` form, and the port builds and converges
with no invalid-function fatal. `halo_3` (409→0) and `rise_of_the_tomb_raider` (427→0) share the
signature, unconfirmed. Log the **combined** stdout+stderr tail (`rexauto.py:605` builds `out = stdout +
stderr`; the 15-line tail is at `:628`).
**Implementation trap:** an INDETERMINATE pass must `return []` immediately. Leaving `accepted` intact
and looping makes `deepextract.py:154` skip the confirming re-codegen, `:158` count dangling against a
stale tree, and `:161` ship the whole unvalidated batch — flipping the gate from fail-closed to fail-open.
**blocked_on:** logging half unblocked. Bisection half blocked on Q1 (it changes the accepted set) and
needs a final **whole-set** predicate pass, since split/swallow is a property of the set.
**Do not** reattribute the 9 companion-module zeros to this bug — see **R14**.

### Q9 — Repair `closure_cert`, **report-only, before it is ever wired in**
**axis:** precision · **files:** `closure_cert.py:21/:28/:35/:60/:66-69/:77/:96-113/:129`
The coverage predicate at `:66-69` reduces algebraically to `a >= starts[0]`, and with every target class
pre-filtered to `cb <= t < ce` (`:85,:90,:106,:112,:121`) the reportable-hole window is exactly
`[REX_CODE_BASE, lowest registered start)`. **Swept independently this session: of 29 non-empty
funclists, 27 have `min(functions_list)` EXACTLY at `REX_CODE_BASE` — a 0-address window.** The sole
entrypoint exception is `ben_10_the_rise_of_hex` (`0x82130000` vs `0x82130010`, a 16-byte window). So
"ZERO static holes" is mathematically forced, not earned, and `CHANGELOG.md:166-167` licenses nothing
(see **R3**). The cert also **cannot run on any port**: roots hardcoded to `C:\Skate3Recomp\autoports`
(`:21,:28,:60`) and a funclist path under `C:\Skate3Recomp\rexauto\work` (`:35`) that does not exist, so
the loose `.cpp` `sub_` scrape (`:36-41`) has always been the executed path.
Repairs, landing together: **(a)** repoint roots to the live tree and the funclist to
`<work>/<port>_functions_list.txt`, **fail closed** ("no funclist — cert refused") instead of the scrape;
**(b)** per-class cover rules (`{bl,ptr,splitimm}` against `DEFINE_REX_FUNC`; `{b,bc}` against
`DEFINE_REX_FUNC ∪ loc_` within the containing function), with interiors bounded by the **emitted body**
via `boundaries.py`'s existing `grid` subcommand (`:20-23`) — **not** `functions.toml` end-overrides
(see **R15**); **(c)** the cover set must **union every module's funclist per title**, because
`codegen__indirect_call.inja.h:12-15` falls back to a global dispatcher across all loaded modules — a
per-port set mints false holes on all 5 multi-XEX titles; **(d)** bound the `lis`/`addi` pairing at
`:96-113` (`lis_hi` is allocated once at `:77` with no basic-block and no `bl`/ABI-clobber invalidation).
Tightening produces false *positives*, which cost launches, never correctness — the safe direction.
**Why it must be fixed before wiring:** `rexloop.md:222-226` plans this exact cert as a launch-skipping
convergence source and `:236-237` sets "Target: 0" — a target the broken rule already meets for free.
Wiring first would authorize 30 titles to skip their confirming launch on a vacuous proof.
**blocked_on:** Q1 (the ports it reads must be regenerated). Its own per-title runtime is **UNMEASURED**;
no prior cert output survives anywhere (`baselines/*.json` carry only `{files, n}`), so the CHANGELOG's
30/30 line has no receipt to diff against. Print the old count beside the new one.

### Q10 — Give `make_elf` the text range `recover.py` already holds, so IDA stops linear-sweeping data as code
**axis:** speed · **files:** `C:\xenon-jumptables\src\recover.py:88-89`, `make_elf.py:24-34`
The only structurally-certain lever on the largest measured cost pool on the board. `recover.py:88-89`
invokes `make_elf.py` with only `--base`, producing a single RWX `PT_LOAD` over the whole image
(`make_elf.py:24-34`) — while `recover.py:80-81` has `text_start`/`text_end` in hand and `:103-107`
already forwards both to `gen_toml.py`. Across 28 fleet images that declares **484 MB executable against
232 MB of real `.text`: 52% of what IDA sweeps is data.** The pool it attacks is hard-measured:
**1736.9 s of 1907.1 s (91.1%)** sits in the one blind `auto_wait` at `ida_jumptables.py:69`.
**Magnitude is UNMEASURED** — auto-analysis is not linear in bytes.
**blocked_on:** Q4 (no per-stage clock exists, so the A/B has no instrument) **and** Q1 (fewer swept
bytes changes recovered tables → `switch_tables.toml` → codegen). A/B is cold runs with
`REXAUTO_NO_IDA_CACHE=1` on `ms_pac_man` / `budokai3` / `grand_theft_auto_v`, median of 3, with
`switch_tables.toml` diffed against the 19 reference outputs in `cache\ida`. Serialized IDA lane; must be
A/B'd **separately** from any `ida_jumptables` reorder or neither delta is attributable (see **R4**).

### Q11 — Stop the forced cmake reconfigure on all 30 builds
**axis:** speed · **files:** `rexauto.py:502-503`, `generated/default/sources.cmake`
Structurally certain that it fires on every build: all 30 recorded builds printed "Build files have been
written to", including the zero-work ones — and it was not the `_build.bat` guard, since in 22 of 26
ports `CMakeCache.txt` is **older** than `build.ninja` by hours to days, so the regeneration came from
ninja's `RERUN_CMAKE` edge whose input list includes `generated/default/sources.cmake`. Snapshot that
file's mtime when its content is unchanged. **The obvious objection is already refuted by measurement:**
CMake writes `cmake_pch.hxx` copy-if-different, and in 24 of 26 ports that file is *older* than
`build.ninja` (`joust`: 07-01 01:21 vs 07-09 23:18), so a reconfigure does **not** invalidate the PCH.
**Cost per event is UNMEASURED** — the `~5-15 s` at `rexauto.py:502-503` is a repo assertion, not a log
line, and must never be quoted as a number.
**blocked_on:** Q4. `msmauto` is the ready-made control: its last build logged `ninja: no work to do.`
and still reconfigured, so a before/after on that one port isolates the cost with nothing else moving.

### Q12 — Inject the PCH into the four ports that have none
**axis:** speed · **files:** codegen patch / PCH injector
**Verified this session:** exactly 4 of 30 ports have **zero** `cmake_pch` edges in their `.ninja_log` —
`dragon_ball_z_ultimate_tenkaichi`, `laracroftandtheg`, `rayman3hd`, `the_legend_of_korra`. The size-
controlled per-TU medians on the uniform 1.9–2.2 MB generated units are **7.06 s with PCH (n=1798)** vs
**8.12 s without (n=497)**; those 4 ports' median-of-median TU time is 9.00 s vs 6.57 s, which is
**observational and title-confounded**, not a controlled result. The other 303 PCH-less edges are
companion modules deliberately marked `SKIP_PRECOMPILE_HEADERS` — a separate question.
**blocked_on:** Q4 for a matched A/B (one port, one `-j`, built twice with and without the PCH, comparing
per-edge medians from the two `.ninja_log` invocations) — the aggregate mixes ports built at different
job counts and `init.h` monoliths from 182 KB to 4.30 MB, and one pair inverts.

### Q13 — Harvest time-to-last-new-indirect-target from the runtime logs already on disk
**axis:** speed · **files:** read-only parse; fold into Q4's harvester
Attacks the largest per-event cost in the pipeline and costs nothing to measure. `confirm_seconds` floors
at 360 s (`rexauto.py:1875`) and a fresh converged port structurally pays **two** launches at that window
(`:1927` discover, `:2050-2058` stretch-and-confirm), with the short ~22 s rounds only in between
(`:2095`). 16 of 30 titles record 360 s; the 18 receipts' confirming windows alone sum to **4911 s of
guest run time across 53 recorded launches**. The number that would justify a lower window — *when,
inside a window, the last new invalid-function line was logged* — has never been computed, though the
logs exist (up to 229 per title for `grand_theft_auto_v`).
**CAVEAT that must not be papered over:** 360 s is a *configured window*, an upper bound, not measured
wall clock — `run_once` breaks early on process exit (`rexauto.py:1693-1696`). Actual seconds are
**UNMEASURED**; this harvest is exactly what replaces the bound with a number.
**blocked_on:** nothing to *measure*. **No window may be lowered on the result** until Q1 and per-title
runtime baselines exist: `rexauto.py:1866-1874` records why the floor was raised twice, including Gears
of War Judgment loading `sub_824CA490` ~71 s in, past the old 47 s window — it converged "clean", then
FATAL'd in play.

### Q14 — Per-candidate drop-reason ledger, then span-collateral re-admission that re-enters the joint fixpoint
**axis:** completeness · **files:** `deepextract.py:123/:134-142/:147/:148`, `rexauto.py:1521-1523`
Potentially the widest recall win after Q7 — up to 21 entrypoint modules — and the least measured.
`deepextract.py:134-142` drops every accepted candidate in `[fn, next_head)` whenever base function
`fn`'s body changes, `:148` makes it permanent, and `:123`'s `range(1,7)` is a fixpoint iterator that
never bisects. Within a span, candidates *below* `sub_fn`'s emitted end truly truncate the body and must
go; candidates in the dead gap *above* it are byte-neutral pure adds — and the code cannot tell them
apart. **9199 of 13 688 candidates (67.2%) share a span with at least one other** (one `forza_horizon`
span holds 168) — but that is an **UPPER BOUND on collateral, not a count**, and the survival-by-density
gradient is **not** evidence of it (dense spans are exactly where candidates are most likely genuine
interior labels failing the split test on their own merits).
**Step 1 is the instrument, not the fix:** emit `swallowed / stub / split-collateral(fn=0x…) /
reject-all-dangling` per candidate into `<name>_deepx_gate.json` — today `:147` fuses all three reasons
into one integer and `rexauto.py:1521-1523` records only `{candidates, accepted}`, so the exact
collateral count **does not exist**. Output-neutral; ships beside Q8's rc capture.
**Step 2 must not "fold whatever passes individually":** individually-pure candidates can be jointly
impure, and `count_dangling` (`:75-84`) builds goto/label sets **per file**, so an intra-TU split is
invisible to the post-loop assertion. Re-admitted candidates must be folded back into `accepted` and the
existing **joint** fixpoint re-entered to convergence, preserving `deepextract.py:10-12`.
**blocked_on:** Q1 and Q4. Cost **UNMEASURED**; the only figure in existence is the `~284 s` source
comment, which bounds **one** codegen pass, not this round — which is why bisection, not per-candidate
iteration, is mandatory on the heavy modules.

### Q15 — Move the `primed` second-boot marker below the "no log = no evidence" guard
**axis:** precision · **files:** `rexauto.py:1933-1938` vs `:1939-1943`
`primed = True` (`:1933`) and the `json.dump` execute **before** the `if not txt: raise SystemExit` guard
(`:1939`), and `run_once` returns falsy both on `Popen` OSError (`:1691-1692`) and on "no log of its own"
(`:1710-1712`). A launch that produced zero evidence can therefore mint the guest-state marker, letting a
later invocation skip the second-boot guard at `:2055-2058` and mint a receipt on what is actually the
port's first-ever real boot — the exact v2.6.0 class the contract at `:1912-1920` exists to block.
One-line move, output-neutral. **Measured reach today is zero:** all **27** `*_runheal_primed.json` files
on the fleet have at least one log at or before the marker's mtime, so it has never fired. Ranks low for
exactly that reason; ranks in the queue because when it fires it costs one extra iteration at the 360 s
floor.
**blocked_on:** nothing. **Do not** use the originally proposed verification — see **R8**. The correct
invariant is existence/oldest-based: at least one log with mtime ≤ the marker's. The fix closes only the
zero-log subset; a launch that writes a log but fatals before the guest creates saves/caches still mints
the marker, and closing that needs a positive boot signal parsed from the log — a separate, larger change.

### Q16 — Purge the 17 unhittable legacy IDA cache entries and record what a cache hit actually buys
**axis:** speed · **files:** `cache\ida\`, `rexauto.py:435-436/:446/:1495/:1503`
**Verified this session: 19 entries, of which 17 carry the legacy 4-field key** and can never match the
5-field format emitted at `rexauto.py:435-436`. The more useful half is the finding it forces into the
ledger: a hit returns early at `:446` but **still pays** the image-dump codegen (unavoidable — the key is
the sha256 of that very dump, computed at `:435-436` *after* the dump at `:363`), the multi-hundred-MB
`.i64` copy at `:1495`, the second `idat` launch at `:1503`, and the gate's ~3 codegen passes, since
`stage_deepextract` has no cache at all. **So the cache's ceiling is the IDA pass alone**, and only 7 of
39 statefiles ever recorded a hit.
**blocked_on:** nothing to purge (never touch `cache/ida` wholesale — invariant 8; this is a targeted
removal of provably unhittable keys and needs an explicit decision, not a reflex). Quantifying what a hit
buys is blocked on Q4 — the `~46 s` / `~4 min` image-dump figures at `rexauto.py:315` are an author's
source comment and **must not be quoted as measurements**.

### Q17 — Fix `boundaries.py`'s silent `0/0` measurement, then correct the PARENT-gap story
**axis:** completeness · **files:** `C:\xenon-jumptables\src\boundaries.py:114/:122-125`, `docs/boundaries.md:69-71`, `rexloop.md:240`
**The instrument must be fixed first.** Measured against `skate3_gab.csv`, `cmd_check`'s regex (`:114`)
yields **0 matches** and the tool prints `parent: 0/0 (0.0%)` and **exits 0**, because of the
`max(1, ...)` guard at `:124-125` — a silent zero passing as a measurement is exactly the guessed-answer
failure this project forbids. Use `C:\Skate3\skate3_gabarito.toml`. Then **build the broken-hit report
before proposing any rule**: `:122-125` prints totals only and cannot distinguish `1107+32−32` from `1107`.
Only then, the doc correction: `docs/boundaries.md:69-71` blames the exception unwinder for the parent
gap, but of 37 distinct missing parents, **30 are in the grid as `loc_` labels** (in `bounds`, not
`starts`) and only 7 are absent entirely — a label-aware rule has a ceiling of 1139/1160 (98.19%). Since
`boundaries.py:65` defines the grid as `starts | labels`, "they aren't in the grid" is false for 30 of 37;
the exception framing is separately contradicted by the doc's own line 19 (funclets 0/1160). The wrong
story has already propagated into `rexloop.md:240`.
**Zero-risk by construction:** `boundaries.py` is imported nowhere in rexauto (the pipeline uses
`heal.heal_boundaries` at `heal.py:142` via `rexauto.py:1574`/`:2183`), and `skate3` has **no port** under
either root, so no fleet title can move. Wiring derived overrides into any port config is a separate,
ratchet-gated step.
**blocked_on:** the two instrument repairs above. See also the struck numbers under **Rejected with cause**.

---

## Rejected with cause

**Never re-attempt any of these.** Each entry names the refutation, so no future iteration spends an
hour rediscovering it. Where only *part* of an idea died, the surviving part is stated explicitly — do
not close it by accident.

**R1 — "Fold `ida_jumptables` and `deep_extract` into one `idat` invocation."** (`rexloop.md:201-208`)
REJECTED. The deep pass is **1.9%** of the IDA budget. Measured from recorded artifacts: the jumptables
`idat` pass is **1907.1 s over 26 modules** (reproduced exactly this session), of which
`grand_theft_auto_v` alone is 205.5 s with **194.9 s (94.8%)** in the single `auto_wait` at
`ida_jumptables.py:69`; the deep pass, measured as `mtime(<name>_deepx.json) − mtime(<name>_deepx_cfg.json)`,
totals **37.40 s across all 31 recorded modules**, with an observed per-module ceiling of ~4.7 s
(`fifa_street/fifadllzf`). Redirect all IDA-speed effort to `ida_jumptables.py:69` → **Q10**.
*Two supporting arguments were themselves false and must not be re-used:* a merged script **can** set the
`AF_` flags and call `auto_wait` again in-process; and "provably does no work" is not a timing argument —
the real proof is static (`ida_jumptables.py:58-63` clears 12 `AF_` flags, nothing restores them, and
`qexit(0)` at `:548` persists the reduced mask into the `.i64`, so `deep_extract.py:39` opens an
already-drained database and its `:1-6` "FULL analysis" docstring is false as pipelined).
*Envelope caveats that keep this a floor:* it excludes the 286 MiB `shutil.copyfile` at
`rexauto.py:1495` and the database write-back, and 27 of 31 recorded runs predate `abc948d`'s splitimm
scan, so the **current** `deep_extract.py` has never been timed on a large entrypoint.
**STILL OPEN (completeness, not speed):** `AF_ANORET`/`AF_LVAR`-dependent discovery has never run for any
fleet title. One line settles it at zero cost — print `ida_ida.inf_get_af()` at the top of
`deep_extract.run()`.

**R2 — "`.pdata` / `.xdata` unwind entries as a boundary-override source."** (`rexloop.md:239-240`,
`docs/boundaries.md:69-75`) REJECTED as an *override* source. The Xbox 360 PE has **no `.xdata`**
(10 sections; EXCEPTION dir = 33 940 8-byte `IMAGE_CE_RUNTIME_FUNCTION`, only 55 with `ExceptionFlag`),
and a second image confirms this is a platform fact, not a one-title fact. **The ceiling is the proof,
not the regression:** only 2 of 154 distinct true parents are a pdata begin; only 13 of 1674 distinct
true ends are a chunk-end; 0 of 37 missing parents and 0 of 378 not-in-grid ends lie in an
`ExceptionFlag` chunk; and 361 of those 378 lie **inside the same chunk as their own address**. pdata is
strictly coarser than the boundaries the overrides encode. The −715 / −125 recall regressions from
unioning pdata into the grid are corroboration only — that is what any nearest-neighbour rule does when
fed 33 940 coarse addresses. Scope of the doc fix **must include `boundaries.py:28`**, which carries the
identical dead `.xdata` claim in the docstring argparse prints as `--help`.
**STILL OPEN:** the *discovery* half — cold function starts absent from the grid, scored by **hole
counts**, never by parent/end agreement. Rewrite the `rexloop.md:239-240` bullet, do not delete it.
Its instrument is a `closure_cert` hole count with vs without PDATA registration → blocked on **Q9**.

**R3 — "The fleet certifies ZERO static holes (30/30)."** (`CHANGELOG.md:166-167`) REJECTED as evidence
of anything. `closure_cert.py:66-69` reduces algebraically to `a >= starts[0]`; with every target class
pre-filtered to `cb <= t < ce`, the reportable window at `:129` is `[REX_CODE_BASE, first registered
start)`. **Swept this session: 27 of 29 non-empty funclists have `min == REX_CODE_BASE` exactly — a
0-address window.** The docstring's own `[start, next_start)` intent is **also** vacuous, since
`bisect_right` already guarantees `a < starts[i+1]`; only real emitted extents repair it. The milestone
is mathematically forced, not earned. Do not cite it, do not seed a scoreboard cell from it, and above
all do not wire the cert as a launch-skipping convergence source before **Q9**.

**R4 — "Define the known function list before the first `auto_wait` (`ida_jumptables.py:69` vs `:90`) as a
speed lever."** REJECTED **as speed**, re-filed as coverage. The reorder is mechanically feasible —
`:69` does run blind, with the `FUNCS` load and `add_func` loop only at `:76-90` and a second `auto_wait`
at `:90`. But the recorded laps cap the upside: of 1907.1 s total, **1736.9 s is the `:69` sweep and only
~170 s covers everything after the function list is defined** (func-analysis + bctr-scan + recognition).
The sweep still has to happen; defining functions first *relocates* knowledge, it does not delete bytes.
`ida_jumptables.py:73-76` says as much in its own words — feeding the list makes IDA define all functions
so every `bctr` is analysed, i.e. it buys **coverage**. Bytes swept is the only structurally-certain
lever on that pool (**Q10**), and if the reorder is ever pursued it must be A/B'd **separately** from
Q10 in the same cold batch or neither delta is attributable.

**R5 — "Give `regression_gate.py` its own first-existing work-root default."** REJECTED. `REXAUTO_WORK`
is unset at both User and Machine scope and `rexauto.py:2557`'s own fallback is the same dead root, so a
divergent default makes the gate read a tree the pipeline does not write and print the unqualified
`GATE PASS: codegen byte-identical -- no regression` (`:366`) over stale bytes. **That is strictly worse
than today's loud `sys.exit` at `:319`.** Change the one shared default in both files together and abort
when the two resolved roots differ (**Q1**).

**R6 — "Gate the baseline-coverage self-check on 'blessed-with-a-port'."** REJECTED. That clause evaluates
`30 < 30`, passes green, and silently drops the flagship — which is the sole member of `HEAVY`
(`regression_gate.py:43`), the sole `MARKERS` entry (`:51`), and the fleet's only tier-3 runtime baseline.
Enumerate `baselines/*.json` minus `*.runtime.json` (33) against discovered projects (30) and **name the
orphans**. Condition the check on `not names` (`:315`), or every scoped re-bless the gate itself
prescribes at `:363-364` emits ~32 MISSING-PORT rows and trains operators to ignore the exit code.

**R7 — "Stop hashing the subdirectories the gate does not regenerate."** REJECTED. It removes **358 of
2771 baselined files** from comparison and destroys the cross-run drift tripwire — `rexauto.py:1437-1447`
does rewrite `generated/<key>` on every `setup_extra_modules` pass. That is a strict reduction in gate
coverage, i.e. a ratchet cell moving down. The correct fix is honest labelling plus real regeneration
(**Q2**), continuing to hash and diff all 358 either way.

**R8 — "Verify the primed-marker fix with 'marker mtime never older than the newest log'."** REJECTED as
an instrument. It fires **26 false alarms on 27 ports**, because the healthy confirm launch always writes
a log newer than the marker. The correct invariant is existence/oldest-based: at least one log with
mtime ≤ the marker's — which is how all 27 were verified clean.

**R9 — "md5 hashing is the gate's bottleneck."** REJECTED. Measured **733–848 MB/s warm**, i.e. ~6–7 CPU-
seconds over the whole 5.03 GB of generated trees across 30 ports (largest `fifa_street`, 446 MB).
**rexglue is the cost, not the hash.** Do not optimize `snapshot()`.

**R10 — "Use the house refcount-close idiom for `mark()`'s atomic write."** REJECTED. On Windows that
leaves the handle open, `os.replace` then fails **every time**, and the proposed `try/except` fallback
turns the hardening into a permanent silent no-op that *looks* applied. Use an explicit
`with open(tmp,"w") as f: json.dump(...)` before `os.replace` (**Q3**).

**R11 — "Verify `mark()` atomicity with `--from build`."** REJECTED. `rexauto.py:2599`'s
`not args.from_stage` guard disables the skip path entirely under that flag, so the test can never
observe the behaviour it claims to check. Use a **plain no-flag** re-run of an already-complete title,
asserting `skip <stage> (done)` (`:2600`) for every prior mark, plus a byte diff of the state file.

**R12 — "Route the genuine-determination sites through `mark_incomplete`."** REJECTED. `rexauto.py:330`
`{found: False}` and `deepextract`'s ran-and-accepted-nothing at `:1511`/`:1522` encode *proven negatives*.
Re-running them costs launches and weakens the prove-or-skip contract (invariant 4). Carve them out
explicitly in **Q5**.

**R13 — "Attribute the 9 companion-module `deepextract` zeros to the missing rc capture (Q8)."**
REJECTED. `rexauto.py:1476-1490` documents the **empty-funclist** cause with the identical signature, and
it reproduces on disk: **9 of the 38 `*_functions_list.txt` files on the fleet are empty**, all companion
modules (`forza_horizon` ×2, `halo_3` ×4, `sonic_adventure` ×2, `spider_man_shattered_dimensions` ×1).
The 46 131/0 result is forced by the bare-only regex (**Q7**), not by a dying probe.

**R14 — "Source `closure_cert`'s function interiors from `functions.toml` end-overrides."** REJECTED.
`heal.py:105-121` shows bare `{}` is the *normal* entry and `boundaries.py:26-27` admits **~28% of ends
are unknown**, so end-bounded interiors would mint false holes across a quarter of every port. Bound
interiors by the **emitted body** (the `loc_` label set plus emitted instruction addresses) and reuse
`boundaries.py`'s existing `grid` subcommand (`:20-23`) rather than writing a third parser.

**R15 — "The unwind info is already inside the grid."** REJECTED as stated. Only **1022 of 33 940** pdata
begins and **8 of 55** `ExceptionFlag` begins are in the measured grid. The defensible statement is the
*code path*: `phase_register.cpp:670` registers every pdata entry as a `FunctionAuthority::PDATA`
function with SEH scopes, and `:677-688` injects `ipToStateMap` ips as labels.

**R16 — "Fixing the pure-add gate / `_DEF` regex is free."** REJECTED as a cost claim. It is
**cost-ADDING**: today the module gate is pinned at exactly 2 passes because pass 1 rejects everything;
post-fix `deepextract.py:123` can run 6, plus the final pass at `:154-157`, plus the `do_codegen` refold
that only fires when `accepted > 0`. Its value is **recall**, and it must be priced as such (**Q7**).
`deepextract.py:112`'s `~284 s` is a source comment, not a log line.

### Struck numbers — quoted somewhere, backed by no artifact, must be re-derived before reuse
- `~46 s` / `~4 min` image dump (`rexauto.py:315`) — author's source comment.
- `~284 s` `fifadllzf` pure-add baseline probe (`deepextract.py:112`, `CHANGELOG.md:133`) — source comment;
  it bounds **one** codegen pass, not a gate round.
- `~5-15 s` cmake reconfigure (`rexauto.py:502-503`) — repo assertion, never logged.
- "1090.9 s" and "1294.1 s" fleet ninja wall — **mutually contradictory and neither reproduced** this
  session under three natural definitions. **Q4 must pin one definition and publish it.**
- "145 functions referenced by nothing" — contradicts the recorded **142** at `docs/boundaries.md:20-21`.
- "median 1144 bytes" — reproduces as 1044 or 1166 depending on the set.
- "1107 → 705" and "−402 prologue promotion" and "0/37 in `ExceptionFlag` pdata" — left no artifact that
  could be verified. Do not cite until re-derived into a committed artifact (**Q17**).

---

## it-000 — read-only fleet audit that seeded this ledger   [ACCEPTED]

```
axis:       precision (measurement only)
stage:      all   files: none modified
hypothesis: the ledger does not exist, so per §3.1 the instrument IS the iteration; before any
            instrument is built, establish which numbers on this box are real, which are source
            comments, and which are contradicted — with zero writes.
change:     none — measurement only
measured:   read-only sweep, 16 cores / 31.1 GB RAM, no build, no clang, no IDA, no gate run, no
            game launch. Reproduced from artifacts already on disk:
              - 30 discoverable ports at C:\Skate3\autoports (44 entries); the pipeline's own
                default root C:\Skate3Recomp\autoports holds exactly 1 manifest-less directory
              - 36 baselines = 33 codegen + 3 runtime; 3 orphans: dante_s_inferno, game, skate3
              - IDA: 1907.1 s over 26 modules, 1736.9 s (91.1%) in the blind auto_wait at
                ida_jumptables.py:69 (laps are CUMULATIVE; summing them overcounts ~4x)
              - ninja: 30 v7 logs, 4512 edge records, 2580 unique outputs; three candidate
                definitions of "fleet build seconds" give 231.9 / 710.8 / 18005.7 s and neither
                prior estimate (1090.9, 1294.1) reproduces
              - deepextract on companion modules: 10 statefiles, 46131 candidates, 0 accepted
              - closure_cert: 27 of 29 non-empty funclists have min == REX_CODE_BASE exactly
              - 18 convergence receipts, 0 with launches=0, 53 launches, 4911 s of confirmed
                survival window; 27 primed markers, all with a log at or before their mtime
              - 4 ports with zero cmake_pch edges; 19 IDA cache entries, 17 unhittable legacy keys
              - .rexauto_state key union across all 30 ports contains no "timings"
gate:       NOT RUN — regression_gate.py cannot start. find_rexglue() (:316) precedes projects()
            (:317) and sys.exits at :76 because C:\Skate3Recomp\rexglue-sdk does not exist. The
            fleet law has enforced nothing since the 2026-07-27 bless (b4a280c, 0e20d05).
            runtime  n/a — nothing was changed; 2 of 30 discoverable titles have a checkable tier.
metrics:    holes UNMEASURED->UNMEASURED (and the prior "0" is refuted, R3) · tables 38 switch
            table files, laracroftandtheg ships none · accepted/cand entrypoints recorded,
            companion modules 46131->0 · heal iters unchanged
ratchet:    seeded, not moved: building 29/30 · converging 17/30 · 0-launch 0/30 · 0-holes
            UNMEASURED · tier 3 0/30 · fleet cold seconds UNMEASURED. No cell moved down; no cell
            moved up. Nothing was written, built, launched or committed.
verdict:    The board's uncomfortable result, stated plainly: not one item has BOTH a measured cost
            AND a proven-safe removal. Every large cost pool is measured and every removal
            mechanism attached to it is UNMEASURED — so the top of the queue is instruments, and
            that is the lens working, not failing. Underneath that sits a harder fact: the fleet
            law is dead. Q1 is not the largest saving on the board and removes no wall clock at
            all; it is first because a saving you cannot land is worth zero seconds, and because a
            ratchet with no pawl is not a ratchet. Q2 follows for the same reason at smaller scale:
            12.92% of the hashed surface is stale re-hash, and it is exactly the 12.92% where Q7 --
            the single largest static-recovery item in the fleet, 46131 candidates forced to zero
            by one bare regex -- would land. And Q6 is in the queue on the charter's own authority:
            the 3 runtime-baselined titles and the 5 multi-XEX titles are DISJOINT sets, so the
            biggest available ratchet turn is also the least verifiable one. Q9 must precede any
            wiring of closure_cert, because a vacuous cert wired in as a convergence source would
            let 30 titles skip their confirming launch on a proof that is free -- fleet-wide false
            convergence, the single most expensive mistake available here.
queue:      Q1..Q17 above, ranked. Iteration 1 is Q1.
```

**Iteration 1 is Q1** — revive `regression_gate.py` as one commit (rexglue via `SDK_PIN`, one shared root
in both files with an abort-on-divergence, a fail-closed baseline-coverage assertion, and a
`generated/`-preserving first run), then run it codegen-tier read-only, tee the output, and triage every
diff per invariant 1 before any bless. Success criterion: the gate prints `gating 30 of 33 baselined
titles`, names the three orphans, leaves all 30 `generated/` trees byte-identical, and produces the first
per-title PASS/REGRESSION table the fleet has had since 2026-07-27. Wrap `run_one` in
`time.perf_counter` — the gate's own wall clock is **UNMEASURED** and this is the run that creates it.

---

## it-001 — the instrument's two blocking review defects   [ACCEPTED]

axis:       precision
stage:      (tooling) · files: `metrics/fleet_metrics.py`, `rexauto.py` (`Ctx.mark`)
hypothesis: Two of three adversarial reviewers refused it-000's timing diff. Both defects are
            real and both are in the *reader*/*writer*, not the frame machinery: fix them and the
            instrument stops being able to lie or to lose a checkpoint.
change:     uncommitted, working tree.
            (1) `print_ratchet` counted `converging` from `runheal.alive is True` and `zero_launch`
            from `runheal.receipt` — but those keys come from MUTUALLY EXCLUSIVE branches of the
            same `mark()` (`rexauto.py`: receipt path writes `{receipt, verdict}`, launched path
            writes `{iters, alive, confirmed_seconds}`). A receipt title therefore left the very
            column `zero_launch` is announced as a subset of. Convergence is now the union; the
            legend was rewritten in the same edit, because a ledger that documents a rule the code
            does not follow is the same honesty defect one layer up.
            (2) `Ctx.mark` was `json.dump(st, open(path, "w"))` — truncate-then-write, no explicit
            close, and `load_state` swallows a parse failure into `{}`. A kill mid-mark silently
            cost a port every finished stage with no error to explain it. Now tmp + `flush` +
            `os.fsync` + `os.replace` (atomic on NTFS). The timings member had fattened the file,
            widening exactly that window.
measured:   no timing A/B — this is an instrument correctness fix, not a speed change.
            Reader verified against the live 30-title fleet: it parses every checkpoint and emits
            the ratchet row. Numerically the union changes nothing TODAY (no port's last run took
            the receipt path, so `zero_launch` is 0/30); it prevents the phantom the moment one does.
gate:       n/a at the time of the edit — the gate could not run (see it-002). Both files are
            outside the gate's snapshot glob (`port/generated/**`), so neither can move codegen.
metrics:    unchanged: building 29/30 · converging 17/30 · 0-launch 0/30 · holes UNMEASURED · tier3 0/30
ratchet:    no cell moved in either direction.
verdict:    ACCEPTED. Reviewer 1's AST-level output-neutrality proof still holds — the two fixes
            touch a reader and a writer, neither of which is reachable from codegen.
queue:      the remaining review notes are folded into Q3/Q5 and stay queued.

## it-002 — the gate is alive again, and its first run proved something nobody expected   [ACCEPTED]

axis:       precision (+coverage)
stage:      (gate) · files: `regression_gate.py`, `rexauto.py`, `metrics/fleet_metrics.py`
hypothesis: Q1 — the fleet moved to `C:\Skate3\autoports` and the gate still pointed at
            `C:\Skate3Recomp\autoports`, so `projects()` returned `[]` and the project's law was
            a no-op. Repoint it, fail closed, run it read-only.
change:     uncommitted, working tree. SIX dead-root sites across three files, not one:
            `regression_gate.py` AUTOPORTS **and** `find_rexglue()` (which pointed at
            `C:\Skate3Recomp\rexglue-sdk` — a second dead root nobody had noticed, and the reason
            the gate would have died even with the fleet root fixed); `rexauto.py`'s `--work`
            default and its two SDK-discovery roots; `fleet_metrics.py`'s fallback. AUTOPORTS now
            honours `REXAUTO_WORK` — the same env `rexauto.py --work` reads — so the two cannot
            drift again, with the old roots kept last so a machine still laid out that way works.
            Added a fail-closed guard: an absent root now says "the gate is DISARMED", instead of
            "no matching projects", which reads like a mistyped filter.
            NOT DONE, and it turned out unnecessary: the 30 stale `_build.bat` (all `cd /d` into the
            dead root) are rewritten from scratch by `write_build_bat` on every pipeline run, so they
            self-heal. They only matter to the runtime tier, which shells the existing file without
            going through the pipeline → queued, see below.
measured:   gate now resolves 30 projects and finds `rexglue.exe` (was: 0 projects, hard exit).
            First live codegen-tier run, read-only, no `--bless`, no `--runtime`:
            **11 PASS / 19 REGRESSION**, exit 1.
gate:       **The 19 regressions are NOT a verdict on 19 titles.** Root-caused before touching
            anything, exactly as invariant 2 requires:
              * `SDK_PIN` expects `rexglue.exe` sha256 `761d531f…`; the binary on disk is `cc042313…`.
                `rexruntime.dll` mismatches too. No copy anywhere on the box matches the pin
                (8 `rexglue.exe` found, 0 hits).
              * `C:\Skate3\rexglue-sdk` HEAD is `e82476a`, a strict ancestor of the pinned
                `981cab8` — **93 commits BEHIND** — plus 7 uncommitted modified files (audio/kernel;
                no codegen file among them).
              * `regression_gate.py` contained **zero** references to `SDK_PIN`. It had no way to
                notice, and its printed remedy was `--bless` — which would have frozen output from a
                93-commits-old compiler as the fleet's truth, permanently.
            FIX (it-002b, same working tree): the gate now calls rexauto's `verify_sdk_pin` before a
            single title is codegen'd, and logs the full path of the binary that produced the verdict.
            Verified: `python regression_gate.py joust` now exits with SDK MISMATCH and gates nothing.
            `REXAUTO_SKIP_SDK_CHECK=1` still allows a deliberate SDK-change gate — explicitly.
metrics:    gate coverage 0 → 30 titles resolvable. Trustworthy verdicts: still **0**, and now
            honestly reported as 0 instead of as 19 false regressions.
ratchet:    no fleet cell moved. Nothing was blessed. **A cost was paid, and it is recorded here
            rather than omitted:** the gate runs `rexglue codegen` with `cwd=port`, in place, so all
            30 ports' `generated/` trees now hold output from the off-pin SDK (mtime 08:47).
            Nothing authoritative was touched — `functions.toml`, `forced_landings`,
            `switch_tables.toml`, manifests, baselines and gabaritos are all untouched, and
            `generated/` is regenerated by `do_codegen` at the head of every `stage_build`. it-000's
            own success criterion for this run said "leaves all 30 `generated/` trees byte-identical";
            it was run without that preservation. That was avoidable and it is the lesson of this
            iteration.
verdict:    ACCEPTED — the change is right and the run was worth its cost: one read-only run
            converted "we assume the fleet is protected" into "the fleet has not been protected
            since the SDK went off-pin, and here is the hash that proves it". The law is armed AND
            fail-closed for the first time.
queue:      unblocking everything downstream is now a single question, and it is the owner's to
            answer, not the loop's: **restore an SDK whose binaries match `SDK_PIN`, or deliberately
            re-pin against a rebuilt `981cab8` and re-bless the fleet with counted proof.** Until one
            of those happens, every codegen-affecting item (Q7, Q10, Q14, Q17) is UNPROVABLE and must
            not be attempted — attempting them would produce exactly the unfalsifiable diff this
            iteration just refused to bless. New items discovered here:
              * the runtime tier shells `AUTOPORTS/<name>/_build.bat` without going through the
                pipeline, so it inherits the stale `cd` path; it must regenerate the bat or fail loudly.
              * the gate never records its own wall clock (still UNMEASURED).


## it-003 — closure_cert stops lying, and the fleet gets its first hole table   [ACCEPTED]

axis:       completeness · files: `C:\xenon-jumptables\src\closure_cert.py`
hypothesis: Q9 — the certificate reports 0 holes on every port because COVERED is
            vacuous. Repair it report-only, before anything is ever wired to it.
change:     uncommitted. Six defects, four found by wave 1 and **two found only by
            validating the tool's own first non-zero output**:
            (1) `in_a_function` had no upper bound — a tautology.
            (1b) THE DOCUMENTED RULE IS ALSO VACUOUS. `[start,next_start)` cannot work:
                 registered starts TILE the code range end to end, so every in-range
                 address is inside some interval by construction. Fixing the bound
                 changed nothing — budokai3 still certified 0. COVERED is now the
                 EMITTED LANDING-PAD SET: `DEFINE_REX_FUNC` definitions plus `loc_`
                 labels harvested from `generated/**`. That is the only falsifiable
                 rule: it asks whether the recompiled binary has somewhere to land.
            (2) funclist path pointed at a dead tree, so the fallback always ran and
                regexed bare `sub_XXXXXXXX` — matching CALL SITES, inflating the
                registered set. Now definition-only and prefix-aware.
            (3) the decode loop ran one word past `code_end`.
            (4) `lis` half-pairs were never scoped, minting split-immediate targets
                across function boundaries. Cleared at every registered start.
            (5) MY OWN FALSE POSITIVE, caught before reporting: switch landings were
                regexed as every hex in the toml, which swallows each table's
                `address` field — the `bctr`'s own address, mid-function, never a
                label. It invented exactly one phantom "landing with no pad" per
                table (budokai3: 75 tables -> 75 phantoms). Now parses `labels = [...]`.
            (6) MY OWN FALSE POSITIVE, caught by reading the image bytes: the CRT
                save/restore helpers (`__savegprlr_N` et al) are a run of stores with
                an entry point every 4 bytes, and rexglue recognises the family BY
                BYTE PATTERN (`function_scanner.cpp:132-138`) rather than emitting
                labels. 56 of budokai3's holes were interior entries of one such run.
                The certificate now models what the recompiler HANDLES, not only what
                it emits.
measured:   budokai3: 0 (forced) -> 320 (rule fixed) -> 264 (phantoms removed).
            **Fleet: 30/30 certified, 0 titles closed, 228..2279 holes each.** The
            project's first real static-residue table. All 2971 of budokai3's recovered
            case targets DO have pads — jump-table recovery is landing correctly.
gate:       n/a — report-only tool, zero call sites in the orchestrator, cannot move codegen.
metrics:    `holes` column: UNMEASURED on 30/30 -> measured on 30/30.
ratchet:    completeness becomes measurable for the first time. No cell moved down.
verdict:    ACCEPTED, with the number's limits stated: `bl` (160 on budokai3) and `b`
            (21) are the credible classes; `ptr` (74) and `splitimm` (22) are heuristic
            and may still carry false positives. Targets are the ENTRYPOINT image only —
            companion code at 0x88000000+ is not scanned yet. And `generated/` currently
            holds off-pin-SDK output (it-002), so every count is provisional until the
            SDK question is settled. It is a real instrument now, not a rubber stamp.
queue:      per-class validation of `ptr`/`splitimm`; scan companion images too; wire as
            a stage only AFTER the residue is understood — never as a convergence proof
            while any class is unvalidated.

## it-004 — static recovery reaches companion modules for the first time   [ACCEPTED]

axis:       coverage · files: `deepextract.py:25`, `jt_landings.py:31`
hypothesis: Q7 — `_DEF` requires `sub_` immediately after `DEFINE_REX_FUNC(`, but a
            companion module emits `gamelogic_sub_880D0000`. If that is the sole
            blocker, making the pattern prefix-aware makes companions visible.
change:     uncommitted. One regex, both sites (wave 1 found the second; only the
            first was known). Verified beforehand that the file glob is NOT a second
            blocker: `_module_view` sets `name = <key>` and the files really are
            `<key>_recomp.N.cpp`, so `%s_recomp.*.cpp` already matched.
measured:   `func_bodies()` over the fleet's 10 companion modules, before -> after:
              spider_man/gamelogic          0 -> 20949
              fifa_street/fifadllzf         0 -> 101442
              forza_horizon/speech+xmedia   0 -> 9259
              halo_3 (4 Waves modules)      0 -> 8023
              sonic_adventure (2 modules)   0 -> 181
              TOTAL                         0 -> 139854
            Control: budokai3 entrypoint 11452 -> 11452, unchanged.
gate:       not run — it cannot prove this today (SDK off-pin, it-002). But the change
            is provably inert for 25 of 30 titles BY CONSTRUCTION: an entrypoint emits
            no prefix, so the optional group matches empty and the pattern is identical
            for every single-module port. Only the 5 multi-XEX titles can move, and
            only in the direction of seeing MORE.
metrics:    companion modules with a working pure-add gate: 0/10 -> 10/10.
ratchet:    coverage up, nothing down. This is the ratchet turning forward in the
            direction the fleet actually needs: Forza, Spider-Man, Halo 3 and FIFA are
            precisely the titles that are not playable yet.
verdict:    ACCEPTED as a fix to a proven blindness. NOT yet a proven recovery gain:
            the candidates were all dropped as "swallowed" because `base` was empty;
            with `base` populated the pure-add gate finally EVALUATES them, and how
            many it accepts is unknown until deepextract re-runs on those five titles.
            46,131 candidates that were auto-dropped now get a real verdict.
queue:      re-run `--only deepextract` on the five multi-XEX titles and record
            accepted/candidates per module — blocked on the SDK pin decision, since it
            re-codegens. That run is the payoff measurement for this iteration.


## it-005 — the residue is 99.7% noise, and the 0.3% is a work list   [ACCEPTED]

axis:       completeness · files: `metrics/residue_triage.py`, `metrics/RESIDUE.md`,
            `metrics/closure_baseline.json`, `metrics/CLOSURE.md`
change:     the certificate becomes a ratchet (`--save` / `--md` / `--diff`) with the
            RULER travelling with the measurement: rexglue sha256 + closure_cert sha256 +
            work root + date. `--diff` refuses to call anything an improvement when the
            ruler moved, counts a title that stopped being measured as a REGRESSION, and
            flags a changed target count as "codegen moved". Every one of those guards is
            a defect this project already paid for once.
measured:   PowerPC triage of all 31,491 raw holes. Only **54** carry a function prologue
            and only **50** are named by two independent target classes. 13,559 have only
            a "border" signal, which fires on any address following zero padding, and
            17,878 have nothing. **Actionable residue: 104 addresses across 34
            titles/modules — 0.33%.**
verdict:    ACCEPTED, and it CORRECTS it-003 and everything derived from it. The per-title
            "closed %" table is inflated by the same 99.7%: `halo_3/waveshell` is not at
            84.6%. Two of the five target classes (`ptr`, `splitimm`) are heuristic and
            dominate the count — a vtable slot is indistinguishable from an int constant
            that happens to fall in the code range. The raw number was never a backlog and
            must never be reported as one again.
ratchet:    completeness measurable AND honest. Baseline saved for the diff.

## it-006 — the bottleneck is the GATE, not the harvester   [ACCEPTED, unvalidated fix]

axis:       coverage · files: `deepextract.py` (`pure_add_gate`), `metrics/gate_false_rejections.json`
hypothesis: chased on a hunch that the fleet's own history is a labelled dataset.
            FIRST ATTEMPT WAS CIRCULAR AND IS RECORDED AS SUCH: bare `{}` entries in
            functions.toml are NOT run-heal cures — `pure_add_gate` writes `{}` for every
            candidate it ACCEPTS too (ben_10: 1259 bare entries, 1174 of them gate
            acceptances). Measuring "how many static finds were statically visible"
            answers nothing. Do not repeat it.
measured:   Separating what deep_extract NAMED from what the gate ACCEPTED gives the real
            number: **228 addresses fleet-wide that deep_extract emitted as candidates,
            the gate rejected, and that are registered functions TODAY.** They were real;
            the port paid for them later in run-heal launches.
              budokai3   71 named ->  2 accepted   (69)
              fifa_street 88 named -> 28 accepted   (60)
              gears_of_war_3 457 -> 424             (33)
              joust       88 -> 67                  (21)
              fifadllzf   14 -> 0                   (14)
              dbz_ultimate_tenkaichi 11 -> 0        (11)
            So `118 -> 2` on budokai3 was never the harvester finding junk. It found 71
            real functions and the gate kept 2.
            Mechanism, narrowed by elimination:
              * `is_stub` — **ELIMINATED**. 0 stubs among the 715 named addresses across
                five titles; every one emits a substantial body today.
              * span-blame collateral — **CONFIRMED PRESENT, INSUFFICIENT ALONE**. joust
                has 69 of 88 sharing a base span with another candidate and one span
                holding 15; but budokai3 spreads 71 candidates over 42 spans, so blame
                alone cannot account for 69 rejections.
              * **no re-admission — the structural cause.** The shrink loop only ever does
                `accepted -= drop`. A candidate blamed in round 1 for sharing a span with a
                real culprit is never retried once the culprit is gone. Six rounds of that
                collapses the set.
change:     re-admission: after the shrink loop settles, the blamed set is retried as its
            own gate run with the survivors as baseline. A real culprit is blamed again on
            its own; pure collateral survives. Bounded at 4 rounds. The safety contract is
            byte-for-byte unchanged — re-admitted candidates face the identical
            swallow/stub/split tests and the final dangling-goto assertion — so this can
            only add candidates that pass the same proof.
gate:       NOT VALIDATED. Running it needs codegen, which needs the SDK pin decision.
verdict:    ACCEPTED as reasoning and as code; the recovery number is UNMEASURED. What
            makes this different from a guess is that it now has a SCORE: any change to
            the gate is graded on how many of the 228 known-real addresses it recovers
            with zero new dangling gotos. Gate tuning stops being taste.
queue:      run `--only deepextract` on budokai3 + fifa_street + joust first — they carry
            150 of the 228 between them and are the cheapest titles to re-gate. Then the
            five multi-XEX titles, where Q7 just opened the gate for the first time.


## it-007 — Q14 refuted by experiment; the interior/landing routing lands   [MIXED]

axis:       coverage · files: `deepextract.py`, `rexauto.py` (stage_deepextract)
**Q14 (gate re-admission) — REJECTED WITH CAUSE, reverted.** Ran it on budokai3 with the
SDK-pin check explicitly overridden (measurement of MY change; both sides the same SDK,
nothing blessed). Result: all 116 candidates dropped in a SINGLE pass, and re-admission
recovered 0 of 116. The drop is not collateral blame — every candidate fails independently.
The it-006 mechanism was wrong. Do not retry re-admission; it costs a codegen pass per
blamed round and recovers nothing.

**What the failure actually revealed.** 115 of the 116 candidates are INTERIOR to an
already-emitted function (offsets 108..332 bytes in). They are not missing functions. The
gate was right to drop them: registering `{}` asks the recompiler to SPLIT a routine it
emitted whole, and it declines — by design, because splitting breaks the routine's own
loop-back branches.
The asymmetry nobody had seen: `deepextract` folds accepted candidates with
`register_functions` (always a bare `{}` = function head), while run-heal uses
`register_or_seed`, which ROUTES interior addresses to forced_landings. The routing exists
and is starved: it decides "interior?" from `end`-override spans, and the whole 30-title
fleet holds about ten of those.

**Q15 — ACCEPTED.** stage_deepextract now partitions candidates against the recompiler's
OWN emitted grid before gating. Gap candidates go to the unchanged pure-add gate; interior
candidates go to a new `landing_gate` with its own contract: the SET of emitted functions
must be unchanged (no head gained or lost) and `count_dangling` must be 0, else the whole
batch reverts and re-codegens. A bad label is a miscompile, not a missed opportunity.
measured:   budokai3, same SDK both sides: **+115 in-function landings accepted, function
            set unchanged, 0 dangling.** Holes 85 -> 84.
verdict:    ACCEPTED and honest: the routing is structurally right and proven safe, but the
            measured benefit is ONE hole. Emitted labels went 48,754 -> 48,755. A
            forced_landing only materialises as `loc_` when a DIRECT branch targets it, and
            these are INDIRECT targets (vtable / function pointer). An indirect target does
            not need a label — it needs the dispatcher to reach it.

## it-008 — the missing construct in the SDK, pinpointed with a repro   [FINDING]

axis:       completeness · files: `C:\Skate3\rexglue-sdk\src\codegen\function_graph.cpp`
The residue's dominant class is now named exactly: **an indirect-call target that lands in
the MIDDLE of an existing function.** It cannot be a function (splitting breaks the parent)
and a label does not help (nothing branches to it directly).
Tested against the live SDK, on budokai3 0x82082080 (interior to 0x82081FE8, +152):
  * `"0xX" = { size = 96 }`   -> BOTH sub_82081FE8 (143 lines) and sub_82082080 (58 lines)
    are emitted, 0 dangling. **rexglue already permits overlapping functions.**
  * `"0xX" = { parent = 0x82081FE8 }` -> identical output. `parent` does not build a
    multi-entry; it only derives size.
  * BOTH raise a NEW `Unresolved conditional branch to 0x82082024 from 0x82082098` — code
    inside sub_X loops BACKWARD to an address in the parent before X, which sub_X cannot
    reach. That is the latent FATAL `register_or_seed`'s docstring warns about, reproduced.
So the missing feature is **multi-entry emission**: a node whose blocks are the PARENT's
(so every label exists and backward branches resolve) and whose body opens with
`goto loc_X;` after `REX_FUNC_PROLOGUE()`. Insertion point located:
`function_graph.cpp:494` emits `DEFINE_REX_FUNC` + prologue then walks `blocks()`.
Cost: an optional entry field on FunctionNode, config plumbing for it, and one emitted
line — plus an SDK rebuild, which moves the binaries further off `SDK_PIN`.
queue:      implement multi-entry; score it on whether budokai3 0x82082080 emits with no
            unresolved branch, then on how many of the 104 high-evidence residue addresses
            it closes fleet-wide.
