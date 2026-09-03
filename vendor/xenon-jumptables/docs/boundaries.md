# Function-boundary overrides

A second class of manual work in Xbox 360 recompilation, separate from jump
tables: **function boundaries**. The recompiler splits some functions at the
wrong address — the boundary that `.pdata` or a disassembler reports is coarser
or finer than the compiler's real function — and teams fix it by hand. The
[Skate 3 port](https://github.com/mchughalex/skate3recomp) ships ~3500 such
`end` / `parent` overrides across two config files.

`src/boundaries.py` derives most of them automatically.

## The finding

The split points are **not marked in the binary**. We checked, exhaustively and
with measurement, on Skate 3's 1750 overrides:

- not `.pdata` entries (15/1750),
- not call / branch / jump-table targets (<5% each),
- not C++ exception-unwinder funclets (0/1160 — the `.xdata` parse was replicated),
- not recoverable from vtables / function-pointer tables (the 154 parent functions:
  142 are referenced by *nothing* in the image).

So no static read of the bytes reproduces them (IDA full-power: 9/1160).

But they **do** fall out of the recompiler's *own* discovery. When XenonRecomp /
ReXGlue runs, it produces a grid of function starts and basic-block labels by
combining all its phases (prologue scan + call graph + gap fill + merge). Two
trivial rules over that grid:

```
parent(A) = largest discovered function start  < A     # the function A belongs to
end(A)    = next discovered block boundary      > A     # where A's region ends
```

Measured against Skate 3's hand-made overrides (the grid comes purely from the
recompiler's output — no answer key, non-circular):

| | reproduced | of |
|---|---|---|
| `parent` | **1107** | 1160 (95.4%) |
| `end` | **1250** | 1740 (71.8%) |

Reproduce it:

```sh
python src/boundaries.py grid  <generated_cpp_dir> -o grid.json
python src/boundaries.py check  grid.json  their_overrides.toml
```

## Workflow

```sh
# 1. run the recompiler once with no boundary overrides; it discovers the grid
#    and reports the addresses it can't resolve / mis-splits.
# 2. parse its generated C++ into a grid:
python src/boundaries.py grid generated/ -o grid.json
# 3. derive overrides for the addresses you need (one hex addr per line):
python src/boundaries.py derive grid.json addresses.txt -o function_overrides.toml
# 4. add function_overrides.toml to your config and regenerate.
```

`addresses.txt` is the set of functions needing a boundary fix — in practice the
addresses the recompiler flags as unresolved or mis-split.

## Honest limits

This is not 100%, and the gap is structural, not effort:

- **~5% of parents** (53/1160) are cold functions reachable only through the C++
  exception unwinder; the recompiler never discovers them, so they aren't in the
  grid. They need `.xdata` parsing or stay manual.
- **~28% of ends** (490/1740) fall where the recompiler placed no boundary at all
  (the true end isn't a discovered start or label). `next boundary > A` is the
  best simple rule measured (variants did worse); closing this needs region-end
  info the discovery grid doesn't carry.

It is also one title. The mechanism is general, but the percentages are not a
promise — run `check` against any overrides you have to see where you land.
