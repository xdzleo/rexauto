# xenon-jumptables

Recover computed-jump (`switch`) tables from Xbox 360 / Xenon PowerPC code for
static recompilation. It analyses a raw code image in IDA Pro and emits a
switch-table TOML for [XenonRecomp](https://github.com/hedge-dev/XenonRecomp)
(`switch_table_file_path`) or ReXGlue (`[[switch_tables]]`).

Unresolved jump tables are one of the main reasons a freshly recompiled title
traps and dies: the recompiler turns each `bctr` it cannot resolve into a trap.
XenonRecomp's own docs call this out — functions with jump tables "look like tail
calls without enough information", with "currently no solution" beyond manually
annotating function boundaries. This tool resolves those tables (the targets
themselves), which the recompiler — and the off-the-shelf switch analysis in IDA
and Ghidra — miss.

## Why a dedicated tool

The Xbox 360 compiler emits jump-table idioms that stock switch recovery does
not match, so it silently treats the `bctr` as a tail call:

- **Bound check by conditional *return*** (`cmplwi r,N` / `bgelr`) instead of a
  branch to a default label — IDA's and Ghidra's matchers expect the latter.
- **Index scaled in place** (`rlwinm r11, r11, 2`) and **reloaded** into a
  different register for the table access.
- **Two-level "relative" tables**: a packed byte/halfword table of offsets added
  to an anchor (`target = anchor + table[i]*scale`), not a table of pointers.
- **Tables embedded inline in `.text`**, immediately after the `bctr`.

Measured on two independent retail titles, each with the recompiler's full
function list defined in IDA so every `bctr` is analysed (not just those reachable
from the entry point):

| switch tables resolved | Title A (~7 MB, ~11k funcs) | Title B (~9 MB, ~17k funcs) |
|---|---|---|
| IDA 9.2 auto-analysis + Hex-Rays | 6 | 74 |
| Ghidra 12 auto-analysis | 7 | — |
| **xenon-jumptables** | **74** | **186** |

Every table the tool emits was independently re-derived from the raw bytes —
each target is the pointer read at `table + i·4` (absolute) or `anchor +
tableN[i]·scale` (the two-level "relative" form) — and cross-checked against IDA
on the shared subset, where the two agree exactly (identical case counts). On
Title B that shared subset is 73 plain absolute tables both find; the tool's
remaining **113 are the two-level relative tables IDA treats as tail calls**.
(IDA in turn resolves one table the tool skips — its bound check sits ~190
instructions and many blocks back from the `bctr`, past where localized matching
follows; see [Scope and limits](#scope-and-limits).)

How much a title needs this depends on its compiler's idiom mix: Title A leans
almost entirely on idioms IDA misses (12× gain), Title B on a mix (still 2.5×).
The idioms are not per-title; the counts are.

## How it works

It drives IDA's disassembler with idiom-aware dataflow rather than relying on
IDA's switch heuristic:

1. Define every function from the recompiler's function list, so *all* `bctr`s
   are analysed — not just those reachable from the entry point.
2. For each `bctr`, walk back over the materialisation idiom: find the table
   base (`lis`/`addi`), the scaled index, and the bound check — following the
   index through copies, in-place scaling, and reloads, and stopping at block
   boundaries so a compare from another path is never mistaken for the bound.
3. Read the table (absolute pointers, or anchor + relative offsets), validating
   that every target is a 4-aligned address inside `.text`.
4. Iterate: a resolved table reveals its case bodies as code, which may contain
   further `bctr`s — loop to a fixpoint.

See [docs/idioms.md](docs/idioms.md) for the full idiom catalogue and the tricky
cases (in-place clobber, reload, inline `.text` tables, overlapping tables).

## Requirements

- **IDA Pro** with the PowerPC processor module (developed against IDA 9.2; the
  analysis pass is plain IDAPython and avoids version-specific APIs). IDA Pro is
  commercial software — this is an IDA pass, not a standalone disassembler.
- **Python 3** for the driver and converters.
- A **raw code image** of the title and a **function-address list** (one hex
  address per line). Both come straight from your recompiler: it dumps the image
  and prints the address ranges at startup, and the per-function sources give the
  function list (see [`extract_funcs.py`](src/extract_funcs.py)).

## Usage

Write a config (addresses are exactly what the recompiler prints at startup):

```jsonc
{
  "image":      "game.bin",       // raw code image dump
  "image_base": "0x82000000",
  "image_end":  "0x826D0000",
  "text_start": "0x82080000",
  "text_end":   "0x8230EC00",
  "functions":  "functions.txt",  // one hex function address per line
  "format":     "xenonrecomp",    // or "rexglue"
  "toml":       "switch_tables.toml"
}
```

(Annotated above for readability — copy [`examples/config.example.json`](examples/config.example.json),
which is plain JSON.) Then run the driver:

```sh
python src/recover.py config.json --ida "/path/to/idat"
```

It wraps the image as an ELF, runs the IDA pass headless, and writes
`switch_tables.toml`. To run the steps yourself, see the comments in
[`recover.py`](src/recover.py); each stage (`make_elf.py`, `ida_jumptables.py`,
`gen_toml.py`) is usable on its own.

Getting the function list from a recompiler checkout:

```sh
python src/extract_funcs.py <generated_sources_dir> -o functions.txt
```

## Output

`gen_toml.py` writes one entry per table, in either recompiler's schema
(`--format`, default `xenonrecomp`):

```toml
# format = xenonrecomp        # format = rexglue
[[switch]]                    # [[switch_tables]]
base = 0x820B12D0             # address = 0x820B12D0
r = 11                        # register = 11
labels = [0x820B4428, 0x82308398, 0x820B34D0, 0x820B3DC0, 0x820B3ED8, 0x820B45B8]
```

`labels` is the ordered list of case targets; `r` / `register` is the index GPR.
Both recompilers key the generated `switch` on that register, so the
[`switch-on-ctr`](patches/) patch is recommended (it makes the register
irrelevant). The raw `jumptables.json` carries more detail (kind, anchor, scale,
bound) if you need it.

See [docs/integration.md](docs/integration.md) for wiring the TOML into
XenonRecomp / ReXGlue.

## The codegen patch

[`patches/switch-on-ctr.patch`](patches/) is a small, recommended change to the
recompiler's `build_bctr`: switch on the computed `ctr` value rather than the
index register (which the compiler often clobbers before the `bctr`), and make
the out-of-range case a runtime indirect call instead of `__builtin_trap()`.
Together with the recovered tables it both fixes a class of latent crashes and
makes an imperfect table degrade gracefully. See [patches/README.md](patches/README.md).

## Function-boundary overrides

A separate class of manual work: the recompiler splits some functions at the
wrong address and teams fix it by hand (the Skate 3 port ships ~3500 `end` /
`parent` overrides). These split points are *not* in the binary — but they fall
out of the recompiler's own discovery grid via two trivial rules. On Skate 3,
`src/boundaries.py` reproduces **1107/1160 (95%)** of the `parent` and
**1250/1740 (72%)** of the `end` overrides, with no answer key. See
[docs/boundaries.md](docs/boundaries.md).

## Scope and limits

- 32-bit big-endian PowerPC, Xbox 360 layout. The idiom recognisers assume the
  Xenon compiler's patterns.
- It resolves `bctr` **jump tables**. Indirect `bctr`/`bctrl` *calls* (virtual
  dispatch, function-pointer tail calls) are not jump tables and are left to the
  recompiler's runtime indirect-call path, which is correct for them.
- A target list is only emitted when the table is proven: a static base, a
  bound, and every entry a valid in-`.text` instruction address. Ambiguous cases
  are skipped rather than guessed.
- The bound check is found by walking back from the `bctr` and stopping at block
  boundaries, so a compare from another path is never mistaken for it. The trade
  is that a bound sitting many blocks before the dispatch (a whole-function
  dataflow question) is not followed — a full decompiler may catch those (one
  such absolute table in Title B above). This is deliberately conservative: it
  trades a rare miss for never emitting a wrong bound.
- It needs no game binaries to be checked in here, and you should not commit
  yours — keep dumps out of the repo.

## License

BSD 3-Clause. See [LICENSE](LICENSE).
