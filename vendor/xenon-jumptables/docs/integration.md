# Integrating the recovered tables

`gen_toml.py` emits one entry per table in the schema your recompiler expects.
Both carry the same data: the `bctr` address, the index register, and the ordered
case targets (the values CTR can hold at that `bctr`).

## XenonRecomp

`--format xenonrecomp` (the default) writes the `[[switch]]` schema XenonRecomp
parses (`base` / `r` / `labels`):

```toml
[[switch]]
base = 0x820B12D0
r = 11
labels = [0x820B4428, 0x82308398, 0x820B34D0, 0x820B3DC0, 0x820B3ED8, 0x820B45B8]
```

Point your recompiler config at the file and regenerate:

```toml
switch_table_file_path = "switch_tables.toml"
```

This is the same file `XenonAnalyse` would produce, with the tables it can't
recover filled in. (XenonRecomp's parser reads `base`, `r`, and `labels`.)

## ReXGlue

`--format rexglue` writes the `[[switch_tables]]` schema (`address` / `register`
/ `labels`). Add it to your project manifest's `includes`:

```toml
[entrypoint]
file_path = "game/default.xex"
out_directory_path = "generated"
includes = ["functions.toml", "switch_tables.toml"]
```

Then regenerate and rebuild. Recompiling with the tables present replaces the
unresolved-`bctr` traps with real dispatches.

## Recommended: apply the codegen patch first

Without [`patches/switch-on-ctr.patch`](../patches), the recompiler keys each
switch on the index register. For tables where the compiler scaled the index in
place or reloaded it (common), that register no longer holds `0..N-1` at the
`bctr`, so the dispatch is wrong even with a correct table. The patch keys on the
computed CTR value instead, which is correct for every idiom, and makes the
`register` field irrelevant. Apply it, rebuild the recompiler, then regenerate
with the tables.

## Discovering new functions after the fact

A resolved table exposes case bodies that were previously dead code. Some of
those may be functions the recompiler had not seen yet; it will report them
(e.g. "unresolved function at 0x…") on the next run. Add them to your function
overrides and regenerate — the usual iterative recompilation loop. Re-running
`xenon-jumptables` after new code is discovered can also surface additional
tables inside the newly reachable functions.
