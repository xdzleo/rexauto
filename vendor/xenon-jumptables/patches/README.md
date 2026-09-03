# `switch-on-ctr` codegen patch

A one-function change to ReXGlue / XenonRecomp's `build_bctr`
(`src/codegen/builders/control_flow.cpp`) that makes recompiled jump tables
correct regardless of how the original index register was used, and turns the
out-of-range case into a self-healing fallback instead of a hard trap.

## The problem

The stock codegen emits, at a `bctr`:

```cpp
switch (ctx.r<indexRegister>.u32) {
  case 0: /* target 0 */ ...
  case 1: /* target 1 */ ...
  default: __builtin_trap();
}
```

This assumes `r<indexRegister>` still holds the raw `0..N-1` case index at the
`bctr`. Very often it does not. The Xbox 360 compiler routinely scales the index
*in place* right before the dispatch:

```
rlwinm r11, r11, 2, 0, 29   ; r11 = index * 4   (index destroyed)
lwzx   r11, r11, r10        ; r11 = table[index] (now a code address)
mtctr  r11
bctr
```

At the `switch`, `ctx.r11` is the loaded **target address**, never `0..N-1`, so
every dispatch hits `default: __builtin_trap()` and the process dies the moment
that function executes. (Same problem when the index is reloaded into a different
register for the table access.)

## The fix

Switch on **`ctx.ctr`** — which always holds the resolved target at the `bctr`
(it was just set by `mtctr`) — with the target addresses as case labels:

```cpp
switch (ctx.ctr.u32) {
  case 0x820B4428: sub_820B4428(ctx, base); return;
  case 0x82308398: goto loc_82308398;
  ...
  default: REX_CALL_INDIRECT_FUNC(ctx.ctr.u32); return;  // self-heal
}
```

This is correct for every idiom (index preserved, scaled in place, or reloaded)
because it does not depend on the index register at all — only on the value the
faithfully-translated idiom already computed into CTR. Targets are deduplicated
(several indices commonly map to one target). The `default` resolves the target
at runtime instead of trapping, so an incomplete or imperfect table degrades to
an ordinary indirect call rather than crashing.

## A caveat worth knowing

The self-healing `default` resolves an out-of-table CTR value as an indirect
*function* call. If a missing target were actually an internal label inside the
same function, that re-enters through the function ABI rather than doing a local
`goto` — fine as a crash-avoidance fallback, but it relies on the table being
complete for full correctness. With complete tables (the normal case) the default
is never taken. So: prefer a complete, verified table; treat the default as a
safety net, not a substitute for one.

## Applying

```sh
cd <your ReXGlue checkout>
git apply /path/to/switch-on-ctr.patch
```

The context matches ReXGlue `0.8.0.0-dev`. **XenonRecomp upstream is different**:
it has no `build_bctr` / `control_flow.cpp`; its `bctr` codegen is inline in
`XenonRecomp/recompiler.cpp` and also switches on the index register
(`switch (r(switchTable.r).u64)`), so it has the same latent issue. The patch
won't apply there — make the equivalent change by hand. After patching, rebuild
the recompiler and regenerate.

---

# `dump-image` helper patch

[`dump-image.patch`](dump-image.patch) is an optional convenience for the
**input** side of the workflow, not a correctness fix. The jump-table pass needs
a raw image of the *decompressed, decrypted* guest code — but a XEX is usually
LZX-compressed and AES-encrypted, so you cannot just analyse the file on disk.
The recompiler already decompresses and decrypts it into guest memory; this patch
just writes that image out.

It adds, right after ReXGlue builds the executable `BinaryView`
(`src/codegen/project_recompiler.cpp`), a guarded dump: if the environment
variable `REX_DUMP_IMAGE` is set, it reconstructs a contiguous image (each section
copied to `section_base - image_base`; header/padding gaps stay zero) and writes
it there. With the variable unset, codegen is byte-for-byte unchanged.

```sh
cd <your ReXGlue checkout>
git apply /path/to/dump-image.patch
# rebuild, then:
REX_DUMP_IMAGE=game.bin rexglue codegen your_manifest.toml
```

`game.bin` is then the `image` for [`config.example.json`](../examples/config.example.json),
with `image_base` / `text_start` / `text_end` taken from the ranges ReXGlue logs
at `--log-level trace` (`BinaryView: section '.text' at … size …`). XenonRecomp
has its own image-loading path; the equivalent there is to dump the mapped image
after it loads the XEX.
