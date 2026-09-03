# Xenon jump-table idioms

This is the catalogue of `bctr` jump-table shapes the Xbox 360 compiler emits,
why the stock switch recovery in IDA and Ghidra misses most of them, and how the
analyser resolves each. Registers and addresses below are illustrative.

All dispatches end the same way:

```
mtctr  rT          ; rT already holds the resolved target
bctr               ; 0x4E800420
```

So the question is always: *what is the set of values `rT` can hold here?* The
recompiler's `switch-on-ctr` codegen (see `patches/`) consumes exactly that set.

## 1. Absolute table

```
cmplwi cr6, r11, 6
bgelr  cr6                 ; <-- bound by conditional RETURN, not branch-to-default
lis    r10, off@ha
slwi   r11, r11, 2         ; index * 4
addi   r10, r10, off@l     ; r10 = table base (in .rodata)
lwzx   r11, r11, r10       ; r11 = table[index]  (an absolute code pointer)
mtctr  r11
bctr
```

`target[i] = *(u32be*)(base + i*4)`. Each entry is the target address itself.

**Why stock tools miss it.** The bound is `bgelr` (return if out of range), not
`bgt default`. IDA's and Ghidra's switch matchers key on a branch to a default
label, so they decline and treat the `bctr` as an indirect tail call — even
though IDA resolves the table reference itself.

**Bound semantics.** `cmplwi r,N` + `bgelr` ⇒ valid `0..N-1` ⇒ `N` cases.
`cmplwi r,N` + `bgt default` ⇒ valid `0..N` ⇒ `N+1` cases. The recogniser reads
the *branch* that consumes the compare to pick `N` vs `N+1`.

## 2. Index scaled in place

```
slwi   r11, r11, 2         ; r11 = index*4   -- the index register is destroyed
lwzx   r11, r11, r10
mtctr  r11
bctr
```

After the `slwi`, no register holds the raw `0..N-1` index, so the stock
`switch (r<index>)` codegen is *wrong* (it reads `table[index]`, a code address,
which never equals `0..N-1` → always `default`). This is the single most common
reason a recompiled title traps. `switch-on-ctr` makes it moot — it never reads
the index. For table reading, the index identity is followed *through* the
`slwi` to recover the bound.

## 3. Index reloaded

```
lwz    r11, 0(r4)          ; bound checks this copy
cmpwi  r11, 6
bgelr  cr6
...
lwz    r10, 0(r4)          ; table uses a fresh load of the same field
slwi   r10, r10, 2
lwzx   r11, r10, r11
```

The bound is on a register loaded from memory `M`; the table is indexed by a
*different* register loaded from the same `M`. A naive backward search for "a
compare on the index register" finds nothing. The recogniser records the index's
memory source `(disp, base)` and treats any register loaded from the same source
as the same value, so the bound is found.

## 4. Relative (two-level) table

```
cmplwi cr6, r3, 0xD
bgt    cr6, default
lis    r12, bytes@ha
addi   r12, r12, bytes@l   ; r12 = byte table (in .rodata)
lbzx   r0, r12, r3         ; r0 = byteTable[index]   (a small selector)
slwi   r0, r0, 2           ; * 4   (omitted when the byte is already the offset)
lis    r12, anchor@ha
addi   r12, r12, anchor@l  ; r12 = anchor (usually bctr+4, the jump island)
add    r12, r12, r0
mtctr  r12
bctr
```

`target[i] = anchor + byteTable[i] * scale`. The byte table compresses a wide
index range onto a handful of distinct case bodies. Element size is `1` (`lbzx`),
`2` (`lhzx`), or `4` (`lwzx`); for half/word tables the load index itself is
scaled, and the real bound index is that scaling's source. Scale is `1` (byte
added directly) or `1<<SH` from the `slwi`.

## 5. Inline `.text` tables

The pointer/byte table is placed immediately after the `bctr`, inside `.text`:

```
addi   r12, r12, table@l   ; table@l resolves to bctr+4
slwi   r0, r11, 2
lwzx   r0, r12, r0
mtctr  r0
bctr                       ; <-- table bytes start at the next address
```

Reading until a "non-pointer" terminates the table is unsafe here: the bytes
after the table are *code*, and the first instruction may itself look like an
in-`.text` pointer. Inline tables are therefore accepted only with a reliable
in-block bound, and read for exactly that many entries, each validated as a
4-aligned `.text` address.

## 6. Contiguous and overlapping tables

Tables are packed densely in `.rodata`, so the bytes after one table are often
the next table — `read-until-invalid` would merge them. The bound trims this:
with a proven `N`, only `N` entries are read.

What does *not* work is bounding a table by "the next recognised table base":
tables can **overlap**. One dispatcher's table can start in the middle of
another's (tail sharing), so the next base is not an upper bound. The bound check
is the only sound length signal; when there is no bound (and the table is in
`.rodata`), `read-until-invalid` is used and is reliable in practice because such
tables are followed by non-pointer data.

## What is intentionally *not* resolved

An indirect `bctr`/`bctrl` whose CTR comes from an **object pointer**
(`lwz r11, 0(r3); lwz r11, off(r11); mtctr r11` — a virtual call) or a
**function-pointer table** indexed by a runtime field is not a jump table. It has
no finite, bound-checked case set, so no static `switch` is emitted; the
recompiler's runtime indirect-call path handles it correctly.
