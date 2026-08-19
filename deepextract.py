"""deepextract.py -- static function/vtable recovery + the pure-addition safety gate.

A deep IDA pass (deep_extract.py in xenon-jumptables, run on the .i64 the jumptables
stage already produced) harvests the function/vtable-target set that the linear scan
misses -- ~96% of the addresses run-heal otherwise discovers by launching the game N
times. This module gates those candidates and folds the safe ones into functions.toml
BEFORE the first build, so run-heal is left as a rare backstop for the genuinely-dynamic
residue instead of the primary mechanism.

THE PURE-ADDITION GATE (the safety contract): a candidate is accepted ONLY if adding it
is a pure addition -- it codegens to its OWN new function with a real (non-stub) body,
introduces no dangling `goto` (a split), and changes no pre-existing function's body.
This inspects the ACTUAL codegen output, not an IDA heuristic, so it cannot be fooled by
boundary/timing mismatches, and it structurally forbids the crash-mask (a return-only
stub that turns a real "invalid function" abort into a silent return).
"""
import os
import re
import glob
import struct
import json
import shutil
import bisect
import subprocess

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
_GOTO = re.compile(r"goto loc_([0-9A-Fa-f]{8})")
_LOC = re.compile(r"^loc_([0-9A-Fa-f]{8}):")


def read_ranges(gen, name):
    """(image_base, code_base, code_size, image_size) from the generated init.h."""
    init = os.path.join(gen, "%s_init.h" % name)
    if not os.path.exists(init):
        return None
    txt = open(init, encoding="utf-8", errors="replace").read()

    def g(key):
        m = re.search(key + r"\s+0x([0-9A-Fa-f]+)", txt)
        return int(m.group(1), 16) if m else None
    ib, cb, cs, isz = g("REX_IMAGE_BASE"), g("REX_CODE_BASE"), g("REX_CODE_SIZE"), g("REX_IMAGE_SIZE")
    if None in (ib, cb, cs, isz):
        return None
    return ib, cb, cs, isz


def func_bodies(gen, name):
    """{func_addr: body_text}. Body = lines from this DEFINE to the next; the leading
    DEFINE line is dropped (identity). Guest-address comments are stable, so equal text
    == same recompiled body."""
    bodies = {}
    for f in glob.glob(os.path.join(gen, "%s_recomp.*.cpp" % name)):
        lines = open(f, encoding="utf-8", errors="replace").readlines()
        defs = [(i, m.group(1)) for i, l in enumerate(lines) for m in [_DEF.search(l)] if m]
        for idx, (start, addr) in enumerate(defs):
            end = defs[idx + 1][0] if idx + 1 < len(defs) else len(lines)
            bodies[int(addr, 16)] = "".join(lines[start + 1:end])
    return bodies


def is_stub(body):
    """A return-only / no-effective-work body = the crash-mask. Strip prologue/comments/
    braces; a stub has no statement other than `return;`."""
    for l in body.splitlines():
        s = l.strip()
        if (not s or s.startswith("//") or s in ("{", "}")
                or s == "REX_FUNC_PROLOGUE();" or s == "return;"):
            continue
        return False   # found an effective statement
    return True


def count_dangling(gen, name):
    """Total `goto loc_X` whose loc_X: is not emitted in the same file (a split)."""
    n = 0
    for f in glob.glob(os.path.join(gen, "%s_recomp.*.cpp" % name)):
        g, loc = set(), set()
        for l in open(f, encoding="utf-8", errors="replace"):
            m = _GOTO.search(l)
            if m:
                g.add(m.group(1))
            m2 = _LOC.match(l)
            if m2:
                loc.add(m2.group(1))
        n += len(g - loc)
    return n


def _write_candidates(functions_toml, addrs):
    txt = open(functions_toml, encoding="utf-8", errors="ignore").read() \
        if os.path.exists(functions_toml) else "[functions]\n"
    add = "".join('"0x%08X" = {}\n' % a for a in sorted(addrs))
    fm = re.search(r"(?m)^\s*\[functions\]\s*$", txt)
    if fm:
        nxt = re.search(r"(?m)^\s*\[[^\]]+\]\s*$", txt[fm.end():])
        ins = fm.end() + nxt.start() if nxt else len(txt)
        txt = txt[:ins].rstrip() + "\n" + add + txt[ins:]
    else:
        txt = txt.rstrip() + "\n[functions]\n" + add
    open(functions_toml, "w", encoding="utf-8", newline="\n").write(txt)




_LOC_RE = re.compile(r"^\s*loc_([0-9A-Fa-f]{8}):", re.M)
_BCTR, _BLR = 0x4E800420, 0x4E800020


def emitted_landing_pads(gen, name):
    """Every address the recompiled code can actually be entered at: emitted function
    definitions plus emitted in-function labels. functions.toml is what was ASKED FOR;
    this is what the compiler will accept a branch into."""
    pads = set()
    for f in glob.glob(os.path.join(gen, "*.cpp")):
        txt = open(f, encoding="utf-8", errors="replace").read()
        for m in _DEF.finditer(txt):
            pads.add(int(m.group(1), 16))
        for m in _LOC_RE.finditer(txt):
            pads.add(int(m.group(1), 16))
    return pads


def bctr_fallthrough_candidates(gen, name, image_path, image_base, code_base, code_size):
    """Code that follows an unconditional `bctr` and that NOTHING emits.

    The recompiler ends a function at `bctr`. Where real code follows one -- the next
    thunk in a run of virtual-dispatch stubs, a cold tail -- it is emitted by nobody:
    not a function, not even a loc_ label. It is simply absent, and the runtime FATALs
    the moment the guest dispatches into it.

    Found the hard way: Forza Horizon died at 0x8257D340, which is exactly such an
    address -- the 4-instruction thunk before it (lwz/lwz/mtctr/bctr) is emitted whole
    and stops at the bctr, and the next 24 bytes are real code that no TU contains.
    Curing this class is what took that title from a guaranteed FATAL to reaching
    gameplay; the runtime fix alone does not, verified by control run.

    Deliberately conservative, because a false positive here is a function registered
    over data: the address must be uncovered, decode to a valid primary opcode, and
    reach a bctr/blr within 12 instructions -- i.e. look like a routine, not like a
    constant pool. Everything it returns is still a CANDIDATE: the pure-add gate has
    the final word, and it rejects a lot (43% on Forza -- 49 of 115 -- and applying
    them ungated broke the title outright).
    """
    if not os.path.exists(image_path):
        return []
    img = open(image_path, "rb").read()
    covered = emitted_landing_pads(gen, name)
    end = code_base + code_size

    def word(a):
        o = a - image_base
        return struct.unpack_from(">I", img, o)[0] if 0 <= o + 4 <= len(img) else None

    out, a = [], code_base
    while a < end - 8:
        if word(a) == _BCTR:
            t = a + 4
            if t < end and t not in covered:
                x = word(t)
                if x and (x >> 26) != 0:
                    for k in range(1, 13):
                        v = word(t + 4 * k)
                        if v in (_BCTR, _BLR):
                            out.append(t)
                            break
                        if v is None or (v >> 26) == 0:
                            break
        a += 4
    return out


def split_by_grid(cands, gen, name):
    """(gap_candidates, interior_candidates) against the recompiler's OWN emitted grid.

    A deep-extract candidate that falls strictly inside an already-emitted function is
    not a missing function -- it is an in-function address (a vtable slot or a computed
    branch target landing mid-routine). Registering it as a standalone {} asks the
    recompiler to SPLIT a routine it already emitted whole, which it declines to do, so
    the pure-add gate drops it as "swallowed" and the address is lost. Measured on
    budokai3: 115 of 116 candidates are interior, offsets 108..332 bytes into their
    owning function, and the gate accepted 0.

    heal.py's register_or_seed already routes this correctly -- but it keys on `end`
    OVERRIDE spans, and the whole 30-title fleet holds about ten of those, so in practice
    it routes almost nothing. The emitted grid is the signal that actually exists: it is
    the recompiler's own multi-phase discovery output, the same grid boundaries.py reads.
    """
    heads = sorted(func_bodies(gen, name))
    gap, interior = [], []
    for a in cands:
        i = bisect.bisect_right(heads, a) - 1
        (interior if (i >= 0 and heads[i] < a) else gap).append(a)
    return gap, interior


def landing_gate(name, gen, forced_path, manifest, codegen_fn, landings, log=print,
                 switch_path=None):
    """Accept interior candidates as forced landings only if the emitted tree stays sane.

    A landing is a `loc_` label inside an existing body, so the safety question is not
    the pure-add gate's (which is about function heads) but this: after adding them, the
    SET of emitted functions must be unchanged -- no head gained, none lost -- and no
    dangling goto may appear. Either failure means a landing landed somewhere that is not
    an instruction boundary, and the whole batch is reverted rather than gated down: a
    bad label is a miscompile, not a missed opportunity.
    """
    import heal as _h
    heads = sorted(func_bodies(gen, name))
    before = set(heads)
    bak = forced_path + ".q15.bak"
    had = os.path.exists(forced_path)
    if had:
        shutil.copyfile(forced_path, bak)
    n = _h.write_forced(forced_path, landings)
    # NOT "if not n: return": a previous run may already hold these landings while the
    # dispatcher cases were never written (the case half was starved by the end-override
    # spans). Zero NEW labels does not mean zero work left.
    _h.ensure_manifest_include(manifest, os.path.basename(forced_path))
    # The label alone is inert for an INDIRECT target: the routine's bctr still falls to
    # `default: REX_CALL_INDIRECT_FUNC`. The dispatcher needs a `case`, and with the
    # switch-on-ctr lowering a case is keyed on the computed CTR value, not on an index,
    # so adding one is purely additive -- an unreachable case costs nothing, a reachable
    # one turns a FATAL into `goto loc_X`. extend_switch_table wants routine spans; give
    # it the emitted grid, since the `end`-override spans it was written against barely
    # exist (about ten across all 30 titles, so it returned 0 every time).
    swbak = None
    if switch_path and os.path.exists(switch_path):
        swbak = switch_path + ".q16.bak"
        shutil.copyfile(switch_path, swbak)
        spans = [(heads[i], heads[i + 1]) for i in range(len(heads) - 1)]
        ncase = _h.extend_switch_table(landings, switch_path, spans)
        if ncase:
            log("  landing gate: +%d dispatcher case(s) from the emitted grid" % ncase)
        n += ncase
    if not n:
        return 0
    codegen_fn()
    after = set(func_bodies(gen, name))
    dangling = count_dangling(gen, name)
    if after != before or dangling:
        log("  landing gate: REVERTING %d landing(s) -- heads %+d, dangling %d"
            % (n, len(after) - len(before), dangling))
        if had:
            shutil.copyfile(bak, forced_path)
        else:
            try:
                os.remove(forced_path)
            except OSError:
                pass
        if swbak:
            shutil.copyfile(swbak, switch_path)
        codegen_fn()
        n = 0
    else:
        log("  landing gate: +%d in-function landing(s); function set unchanged, 0 dangling" % n)
    for b in (bak, swbak):
        if b:
            try:
                os.remove(b)
            except OSError:
                pass
    return n


def pure_add_gate(rexglue, port, name, manifest, gen, functions_toml, candidates, codegen_fn, log=print,
                  baseline_current=False):
    """Return the subset of `candidates` that are provably pure additions. `codegen_fn()`
    must run a raw rexglue codegen over the current functions.toml (no heal). Backs up and
    RESTORES functions.toml (the caller applies the accepted set).

    baseline_current=True: the caller GUARANTEES generated/ already reflects the current
    functions.toml (e.g. _codegen_module runs do_codegen as the immediately preceding
    step), so the opening baseline codegen is skipped -- on a giant companion module that
    probe alone costs ~284s (fifadllzf, 101k funcs). A stale guarantee would corrupt the
    base snapshot and mis-gate, so only pass True from a call site where the codegen is
    literally the previous statement."""
    bak = functions_toml + ".deepx.bak"
    shutil.copyfile(functions_toml, bak)
    codegened = None   # the `accepted` set the current generated/ reflects (skip redundant passes)
    try:
        if not baseline_current:
            codegen_fn()
        base = func_bodies(gen, name)
        base_heads = sorted(base)
        accepted = set(candidates)
        for it in range(1, 7):
            shutil.copyfile(bak, functions_toml)
            _write_candidates(functions_toml, accepted)
            codegen_fn()
            codegened = frozenset(accepted)
            new = func_bodies(gen, name)
            new_heads = set(new) - set(base)
            drop = set(a for a in accepted if a not in new_heads)          # swallowed
            drop |= set(a for a in (accepted & new_heads) if is_stub(new[a]))  # stub / crash-mask
            # a changed existing body means a candidate split it -- drop the candidate(s)
            # that fall inside that function's original span
            for c in sorted(a for a in base if a in new and base[a] != new[a]):
                i = bisect.bisect_right(base_heads, c) - 1
                if i < 0:
                    continue
                fn = base_heads[i]
                nxt = base_heads[i + 1] if i + 1 < len(base_heads) else None
                for a in accepted:
                    if fn <= a and (nxt is None or a < nxt):
                        drop.add(a)
            if not drop:
                if it > 1:
                    log("  pure-add gate: converged after %d passes" % it)
                break
            log("  pure-add gate: dropping %d unsafe (swallow/stub/split); re-checking" % len(drop))
            accepted -= drop
        # Final safety assertion on the accepted set. When the loop broke via
        # `not drop`, generated/ ALREADY reflects `accepted` (the last loop pass
        # codegen'd exactly this set) -- re-codegen'ing it is pure waste. Only
        # re-run when the set changed since the last pass (loop exhausted 6 iters
        # and shrank accepted after its final codegen). (audit: SAFE-BYTE-IDENTICAL)
        if codegened != frozenset(accepted):
            shutil.copyfile(bak, functions_toml)
            _write_candidates(functions_toml, accepted)
            codegen_fn()
        if count_dangling(gen, name) != 0:
            log("  pure-add gate: residual dangling goto after gating -> REJECT ALL (unsafe)")
            return []
        return sorted(accepted)
    finally:
        # Restore functions.toml; the CALLER re-applies the accepted set. No restore
        # codegen: stage_build always re-runs do_codegen from the authoritative toml
        # before any compile (_gen_restore_unchanged md5-keeps byte-identical files),
        # so a restore pass here only re-does work stage_build redoes. (audit: SAFE)
        shutil.copyfile(bak, functions_toml)
        try:
            os.remove(bak)
        except OSError:
            pass
