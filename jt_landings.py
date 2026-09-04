#!/usr/bin/env python3
"""jt_landings.py -- heal unregistered bctr switch-on-ctr jump-table landings.

The SDK's build_bctr lowers each recovered jump table as `switch (ctx.ctr.u32)`
with a `case 0xTARGET:` per landing. A landing that is not a registered
function/chunk falls back to `REX_CALL_INDIRECT_FUNC(ctr.u32)`, which FATALs at
runtime if that guest address is not in the function table -- the source of
non-deterministic sustained-play crashes (e.g. Skate 3's 0x82E57160 ~85s in).

Fix (proven on skate3): register every such landing as a CHAINED, CONTIGUOUS
chunk of its enclosing function. classifyTarget then treats each case target as a
real entry (chunkParent != 0), so build_bctr lowers `case 0xT:` to a direct
`sub_T(ctx, base)` that resolves via the function table -- no indirect FATAL.

The winning config shape (mirrors the hand-validated skate3 heal):
  - Per parent function, sort its unregistered landings.
  - end(chunk_i) = start(chunk_{i+1})  (CONTIGUOUS -- no gaps), last end = the
    next known instruction boundary after the final landing.
  - parent(chunk_0) = the enclosing function; parent(chunk_i>0) = the PREVIOUS
    landing (a chain), so the whole span resolves to one root function body.

Detection reads only the GENERATED tree (the SDK's own complete table recovery),
so no IDA/switch_tables pass is needed. NO-OP for a title whose bctr switches all
resolve already (returns 0) -> codegen byte-identical.
"""
import os
import re
import glob
import bisect

# The optional prefix is a companion module's `symbol_prefix`: an extra recompiled
# XEX emits DEFINE_REX_FUNC(gamelogic_sub_880D0000), never a bare sub_. Without it
# this pattern read every companion module's generated sources as EMPTY, so
# func_bodies() returned {}, every deep-extract candidate looked "swallowed", and the
# pure-add gate dropped all of them: 46,131 candidates across the fleet's 10 companion
# modules, 0 accepted, every time -- Spider-Man's gamelogic alone 21,749 -> 0. Failing
# closed meant nothing wrong was ever accepted, which is why it went unnoticed for so
# long; it also meant static recovery never contributed one function to Halo 3, FIFA,
# Forza Horizon, Sonic or Spider-Man's extra modules.
# An entrypoint has no prefix, so the group matches empty and single-module titles are
# byte-identical by construction.
_DEF = re.compile(r"DEFINE_REX_FUNC\((?:[A-Za-z]\w*_)?sub_([0-9A-Fa-f]{8})\)")
_CASE = re.compile(r"case 0x([0-9A-Fa-f]{8})[uU]?:")
_LOC = re.compile(r"^\s*loc_([0-9A-Fa-f]{8}):")
_INDIRECT = "REX_CALL_INDIRECT_FUNC"


def _gen_dir(ctx):
    # ctx.gen is <port>/generated/default; fall back to that path. Don't use a
    # getattr default here -- it would evaluate ctx.port eagerly even when ctx.gen
    # exists (and raise on a ctx that only carries gen).
    g = getattr(ctx, "gen", None)
    return g if g else os.path.join(ctx.port, "generated", "default")


def detect_landings(gen_dir, name):
    """Return {parent_addr: sorted[landing_addrs]} for every unregistered
    switch-on-ctr landing across the generated tree."""
    groups = {}
    for f in sorted(glob.glob(os.path.join(gen_dir, "%s_recomp.*.cpp" % name))):
        lines = open(f, encoding="utf-8", errors="replace").readlines()
        defs = [(i, int(m.group(1), 16)) for i, l in enumerate(lines)
                for m in [_DEF.search(l)] if m]
        for idx, (start, parent) in enumerate(defs):
            end = defs[idx + 1][0] if idx + 1 < len(defs) else len(lines)
            body = lines[start:end]
            for i, l in enumerate(body):
                cm = _CASE.search(l)
                if not cm:
                    continue
                target = int(cm.group(1), 16)
                # scan the case body until the next case/default
                indirect = goto = False
                for w in body[i + 1:i + 8]:
                    if _CASE.search(w) or "default:" in w:
                        break
                    if "goto loc_" in w:
                        goto = True
                        break
                    if _INDIRECT in w:
                        indirect = True
                        break
                if indirect and not goto and target != parent:
                    groups.setdefault(parent, set()).add(target)
    return {p: sorted(v) for p, v in groups.items()}


def _known_boundaries(gen_dir, name):
    """Sorted union of every emitted loc_/case address + registered SetFunction
    address -- used to bound the last chunk of each chain."""
    addrs = set()
    for f in glob.glob(os.path.join(gen_dir, "%s_recomp.*.cpp" % name)):
        for l in open(f, encoding="utf-8", errors="replace"):
            for m in _LOC.finditer(l):
                addrs.add(int(m.group(1), 16))
            for m in _CASE.finditer(l):
                addrs.add(int(m.group(1), 16))
    reg = os.path.join(gen_dir, "%s_register.cpp" % name)
    if os.path.exists(reg):
        for l in open(reg, encoding="utf-8", errors="replace"):
            m = re.search(r"SetFunction\(0x([0-9A-Fa-f]{8}),", l)
            if m:
                addrs.add(int(m.group(1), 16))
    return sorted(addrs)


def build_chunks(groups, gen_dir, name, already=None):
    """Emit the contiguous chained { end, parent } chunk TOML lines."""
    if not groups:
        return []
    boundaries = _known_boundaries(gen_dir, name)

    def next_boundary(a):
        i = bisect.bisect_right(boundaries, a)
        return boundaries[i] if i < len(boundaries) else a + 4

    out = ["", "# === bctr switch-on-ctr jump-table landings heal (chained chunks) ===",
           "# Auto-generated by jt_landings.py: each landing is a contiguous chained",
           "# chunk of its parent so its `case` lowers to a direct call, not a FATAL",
           "# indirect dispatch. Idempotent: regenerated only for still-unregistered landings."]
    # One address can be a landing of two different switch tables (Dante's
    # Inferno: 0x82908238 under both 0x82907D90 and 0x82907EA4). Emitting it once
    # per parent writes the same TOML key twice, and rexglue refuses the file --
    # "cannot redefine existing table" -- which aborts codegen outright. A landing
    # belongs to exactly one chunk chain, so the first parent claims it.
    claimed = set(already or ())
    for parent in sorted(groups):
        lands = [a for a in sorted(set(groups[parent])) if a not in claimed]
        if not lands:
            continue
        claimed.update(lands)
        out.append("# --- 0x%08X (%d landings) ---" % (parent, len(lands)))
        for idx, a in enumerate(lands):
            end = lands[idx + 1] if idx + 1 < len(lands) else next_boundary(a)
            par = parent if idx == 0 else lands[idx - 1]
            if end <= a:                      # safety: never emit a zero/negative span
                end = a + 4
            out.append('"0x%08X" = { end = 0x%08X, parent = 0x%08X }' % (a, end, par))
    out.append("")
    return out


def _append_to_functions(txt, block):
    """Insert `block` (the chunk lines) at the END of the [functions] table so the
    keys land INSIDE it, regardless of whether [meta] trails [functions] (a plain
    port config) or leads it (a gabarito-seeded config: comment / [meta] /
    [functions]). Inserting before [meta] blindly would, for a gabarito seed, drop
    the keys above [functions] -> they'd bind to the root table, rexglue would never
    see them, and the landings would stay unregistered (a runtime FATAL). We instead
    find [functions] and insert just before the next top-level table (or at EOF)."""
    body = block.strip("\n")
    fm = re.search(r"(?m)^\s*\[functions\]\s*$", txt)
    if not fm:                                   # no [functions] header -> just append
        return txt.rstrip() + "\n\n" + body + "\n"
    nxt = re.search(r"(?m)^\s*\[[^\]]+\]\s*$", txt[fm.end():])
    ins = fm.end() + nxt.start() if nxt else len(txt)
    head = txt[:ins].rstrip()
    tail = txt[ins:]
    if tail.strip():                             # inserting before a following table
        return head + "\n\n" + body + "\n\n" + tail
    return head + "\n\n" + body + "\n"           # inserting at EOF


def heal(ctx, log=None):
    """Detect + register unregistered switch-on-ctr landings for ctx's title by
    appending the chained chunks to <name>_functions.toml. Returns the number of
    landings registered (0 = no-op). Idempotent: only unregistered landings are
    detected, so a second call after a re-codegen finds none."""
    gen = _gen_dir(ctx)
    name = ctx.name
    groups = detect_landings(gen, name)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        return 0
    fns = ctx.functions
    txt = open(fns, encoding="utf-8", errors="replace").read()
    # Never re-emit a key the config already carries: appending a duplicate makes
    # rexglue reject the whole file ("cannot redefine existing table") and codegen
    # aborts. Covers both a landing the run-heal already registered and a second
    # call after a re-codegen.
    already = set(int(a, 16) for a in
                  re.findall(r'"0x([0-9A-Fa-f]+)"\s*=', txt))
    lines = build_chunks(groups, gen, name, already=already)
    written = sum(1 for l in lines if l.startswith('"0x'))
    if not written:
        return 0
    block = "\n".join(lines) + "\n"
    txt = _append_to_functions(txt, block)
    open(fns, "w", encoding="utf-8").write(txt)
    if log:
        log("  jt-landings: registered %d switch-on-ctr landing(s) in %d function(s)"
            % (written, len(groups)))
    return written
