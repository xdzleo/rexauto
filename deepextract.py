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
import json
import shutil
import bisect
import subprocess

_DEF = re.compile(r"DEFINE_REX_FUNC\(sub_([0-9A-Fa-f]{8})\)")
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


def pure_add_gate(rexglue, port, name, manifest, gen, functions_toml, candidates, codegen_fn, log=print):
    """Return the subset of `candidates` that are provably pure additions. `codegen_fn()`
    must run a raw rexglue codegen over the current functions.toml (no heal). Backs up and
    RESTORES functions.toml (the caller applies the accepted set)."""
    bak = functions_toml + ".deepx.bak"
    shutil.copyfile(functions_toml, bak)
    try:
        codegen_fn()
        base = func_bodies(gen, name)
        base_heads = sorted(base)
        accepted = set(candidates)
        for it in range(1, 7):
            shutil.copyfile(bak, functions_toml)
            _write_candidates(functions_toml, accepted)
            codegen_fn()
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
        # final safety assertion on the accepted set
        shutil.copyfile(bak, functions_toml)
        _write_candidates(functions_toml, accepted)
        codegen_fn()
        if count_dangling(gen, name) != 0:
            log("  pure-add gate: residual dangling goto after gating -> REJECT ALL (unsafe)")
            return []
        return sorted(accepted)
    finally:
        shutil.copyfile(bak, functions_toml)
        try:
            os.remove(bak)
        except OSError:
            pass
        codegen_fn()
