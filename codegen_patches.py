#!/usr/bin/env python3
"""codegen_patches.py -- declarative, per-project post-codegen source patches.

Some titles need a tiny host-side hook spliced into the *generated* recompiled
code that no boundary override / function cure can express -- e.g. Skate 3's
projection-FOV override and its ultrawide game-frustum widening, which the
community port injects via a hand-written CMake step
(cmake/ApplySkate3CodegenPatches.cmake). This module generalizes that idea into
the rexauto pipeline: a title ships a `<name>_codegen_patches.toml` and the
codegen stage applies it to the generated `<name>_recomp.*.cpp` after codegen
converges and before compile. No file -> no-op (fleet stays byte-identical).

Two patch KINDS cover the real cases and are reusable for any game:

  [[patch]]
  kind    = "literal"                 # exact find -> replace in the one file that
  name    = "projection_fov"          # contains every `require` string
  require = ["ctx.f27.f64 = ctx.f1.f64;",
             "ctx.f4.f64 = double(float(ctx.f1.f64 * ctx.f0.f64));"]
  find    = "ctx.f27.f64 = ctx.f1.f64;"
  replace = "ctx.f1.f64 = double(Skate3MaybeOverrideProjectionFovRadians(float(ctx.f1.f64)));\n\tctx.f27.f64 = ctx.f1.f64;"
  marker  = "Skate3MaybeOverrideProjectionFovRadians"   # idempotency guard
  include = "skate3_fov.h"

  [[patch]]
  kind    = "insert_before_call_after_anchor"   # find the first generated guest
  name    = "ultrawide_frustum"                 # call (// bl / ctx.lr / sub_(ctx,base))
  anchor  = "ctx.r6.u64 = REX_LOAD_U32(ctx.r4.u32 + 5260);"   # within `window` chars
  window  = 12000                               # after `anchor`, and inject a line
  inject  = "Skate3UltrawideGameFrustumPatchScope skate3_ultrawide_game_frustum_patch_scope(\n\t\tctx, base, ctx.r4.u32);"
  marker  = "Skate3UltrawideGameFrustumPatchScope"
  include = "skate3_ultrawide_guest.h"

`marker` makes each patch idempotent (skipped if already present). A declared
patch whose anchor/require is not found is a HARD FAIL (mirrors the community's
FATAL): a codegen re-layout must never silently drop a shipped behaviour.
"""
import os
import re
import glob

# a rexglue-emitted guest call: the "// bl 0x..", the return-address store, and
# the direct call. Universal across the SDK's codegen, so the frustum-style
# "wrap the next guest call in a scope" patch needs no per-title regex.
_CALL_RE = re.compile(
    r"\t// bl 0x[0-9a-fA-F]+\n\tctx\.lr = 0x[0-9A-Fa-f]+;\n\tsub_[0-9A-Fa-f]+\(ctx, base\);")
_LR_RE = re.compile(r"(\tctx\.lr = 0x[0-9A-Fa-f]+;\n)")


def _gen_dir(ctx):
    g = getattr(ctx, "gen", None)
    return g if g else os.path.join(ctx.port, "generated", "default")


def _config_path(ctx):
    return os.path.join(ctx.port, "%s_codegen_patches.toml" % ctx.name)


def _load(ctx):
    path = _config_path(ctx)
    if not os.path.exists(path):
        return []
    import tomllib
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return data.get("patch", [])


def _add_include(contents, name, include):
    """Insert `#include "<include>"` right after the generated unit's own
    `#include "<name>_init.h"` line (once)."""
    want = '#include "%s"\n' % include
    if want in contents:
        return contents
    init_inc = '#include "%s_init.h"\n' % name
    if init_inc in contents:
        return contents.replace(init_inc, init_inc + want, 1)
    return want + contents           # fallback: no init include -> prepend


def _apply_literal(p, c):
    if not all(r in c for r in p.get("require", [p["find"]])):
        return None
    if p["find"] not in c:
        return None
    return c.replace(p["find"], p["replace"])


def _apply_insert(p, c, name, f):
    ai = c.find(p["anchor"])
    if ai == -1:
        return None
    win = c[ai:ai + int(p.get("window", 12000))]
    m = _CALL_RE.search(win)
    if not m:
        raise SystemExit(
            "[codegen-patch] %s: anchor found in %s but no guest call within "
            "%d chars after it -- codegen layout changed" % (p["name"], os.path.basename(f),
                                                             int(p.get("window", 12000))))
    call = m.group(0)
    newcall = _LR_RE.sub(lambda mm: mm.group(1) + "\t" + p["inject"] + "\n", call, count=1)
    return c.replace(call, newcall, 1)


def _apply_one(p, files, name, log):
    marker = p.get("marker")
    kind = p.get("kind", "literal")
    for f in files:
        c = open(f, encoding="utf-8", errors="replace").read()
        if marker and marker in c:
            return "already"                    # idempotent
        if kind == "literal":
            nc = _apply_literal(p, c)
        elif kind == "insert_before_call_after_anchor":
            nc = _apply_insert(p, c, name, f)
        else:
            raise SystemExit("[codegen-patch] %s: unknown kind %r" % (p.get("name"), kind))
        if nc is None:
            continue                            # not the target file; keep looking
        inc = p.get("include")
        if inc:
            nc = _add_include(nc, name, inc)
        open(f, "w", encoding="utf-8").write(nc)
        return os.path.basename(f)
    return "notfound"


def apply(ctx, log=None):
    """Apply the title's declared post-codegen patches to the generated tree.
    Returns the number applied (0 = no config / all already applied). HARD-FAILS
    if a declared patch's anchor is nowhere to be found."""
    patches = _load(ctx)
    if not patches:
        return 0
    gen = _gen_dir(ctx)
    name = ctx.name
    files = sorted(glob.glob(os.path.join(gen, "%s_recomp.*.cpp" % name)))
    applied = 0
    for p in patches:
        r = _apply_one(p, files, name, log)
        if r == "notfound":
            raise SystemExit(
                "[codegen-patch] %s: anchor/require not found in any %s_recomp.*.cpp "
                "(codegen re-layout?) -- refusing to ship a silently-dropped patch"
                % (p.get("name"), name))
        if r != "already":
            applied += 1
            if log:
                log("  codegen-patch: applied %s -> %s" % (p.get("name"), r))
    return applied


# --- built-in correctness fix: in-place VMX pack aliasing --------------------
# Not a per-title patch. ReXGlue lowers vpkuwus/vpkuhus as an element-by-element
# loop that writes the destination's NARROW element array while still reading the
# source's WIDE one. Both views alias the same 128-bit register, so when the
# destination is also a source -- `vpkuwus128 v63,v61,v63` -- each write corrupts
# the read on the next line:
#
#   ctx.v63.u16[7] = ctx.v61.u32[3] ...   <- writes bytes 14-15 of v63
#   ctx.v63.u16[3] = ctx.v63.u32[3] ...   <- reads bytes 12-15 of v63
#
# hells-gate-recomp traced Dante's Inferno's VP6/Bink FMV corruption to exactly
# this and fixed it in the SDK (v0.10.0, builders/vector.cpp). We are on a
# prebuilt 0.8.2 with no source, so the same defect is repaired here in the
# emitted C++: snapshot both source registers, then pack out of the snapshots.
# Blocks whose destination is NOT one of the sources are left untouched, byte for
# byte, so a title that never packs in place is unaffected.
_PACK_HDR = re.compile(
    r"^\t// (vpk[a-z]+)(?:128)? v(\d+),v(\d+),v(\d+)\s*$")
_PACK_ROW = re.compile(
    r"^\tctx\.v(\d+)\.(u\d+)\[(\d+)\] = ctx\.v(\d+)\.(u\d+)\[(\d+)\] ")


def fix_vector_pack_inplace(gen_dir, log=None):
    """Rewrite in-place vpk* blocks to read from snapshots. Returns sites fixed."""
    fixed = 0
    for fp in sorted(glob.glob(os.path.join(gen_dir, "*.cpp"))):
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().split("\n")
        out, i, changed = [], 0, False
        while i < len(lines):
            m = _PACK_HDR.match(lines[i])
            if not m:
                out.append(lines[i])
                i += 1
                continue
            d, a, b = m.group(2), m.group(3), m.group(4)
            j = i + 1
            body = []
            while j < len(lines) and _PACK_ROW.match(lines[j]) \
                    and _PACK_ROW.match(lines[j]).group(1) == d:
                body.append(lines[j])
                j += 1
            # only an in-place block needs repair; anything else stays identical
            if not body or (d != a and d != b):
                out.append(lines[i])
                i += 1
                continue
            out.append(lines[i])
            out.append("\t{  // rexauto: snapshot sources -- dest aliases a source")
            out.append("\t\tconst auto _rex_pa = ctx.v%s; const auto _rex_pb = ctx.v%s;" % (a, b))
            for row in body:
                # rewrite only the SOURCE reads (right of '='), never the dest write
                lhs, _, rhs = row.partition(" = ")
                rhs = rhs.replace("ctx.v%s." % a, "_rex_pa.").replace("ctx.v%s." % b, "_rex_pb.")
                out.append("\t" + lhs.lstrip("\t") + " = " + rhs)
            out.append("\t}")
            fixed += 1
            changed = True
            i = j
        if changed:
            with open(fp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(out))
    if fixed and log:
        log("  codegen fix: %d in-place VMX pack site(s) rewritten to snapshot "
            "their sources (SDK 0.8.2 aliasing bug)" % fixed)
    return fixed


# --- built-in correctness fix: vector element loads ------------------------
# lvebx / lvehx / lvewx load ONE element of vD from memory and leave the other
# elements untouched. SDK 0.8.2 lowers all three as a full 16-byte `lvx`: it
# masks the address to 16 bytes and byte-reverses a whole vector over the
# destination, so 15/14/12 bytes that must be preserved are clobbered and the
# addressed element is read from the wrong offset.
#
# Its own STORE counterparts are already right and are the reference used here:
#     stvewx -> ea = (...) & ~0x3;  REX_STORE_U32(ea, vD.u32[3 - ((ea & 0xF) >> 2)]);
# so the load is simply that mirrored. hells-gate-recomp added the same three
# builders to ReXGlue 0.10.0 with the identical index arithmetic.
#
# Dante's Inferno emits 96 lvewx + 4 lvehx; Gears of War Judgment emits no lvewx,
# which is why the defect never showed there.
_VEC_ELEM = {
    "lvebx": ("~0x0", "u8", "15 - (ea & 0xF)", "REX_LOAD_U8"),
    "lvehx": ("~0x1", "u16", "7 - ((ea & 0xF) >> 1)", "REX_LOAD_U16"),
    "lvewx": ("~0x3", "u32", "3 - ((ea & 0xF) >> 2)", "REX_LOAD_U32"),
}
_VEC_ELEM_HDR = re.compile(r"^\t// (lveb|lveh|lvew)x(?:128)? v(\d+),")
_VEC_ELEM_EA = re.compile(r"^\tea = (.*) & ~0xF;$")
_VEC_ELEM_BAD = re.compile(
    r"^\tsimde_mm_store_si128\(\(simde__m128i\*\)ctx\.v(\d+)\.u8, "
    r"simde_mm_shuffle_epi8\(.*VectorMaskL.*\)\);$")


def fix_vector_element_loads(gen_dir, log=None):
    """Rewrite lvebx/lvehx/lvewx from a full-vector load to a single-element
    load. Returns the number of sites fixed."""
    fixed = 0
    for fp in sorted(glob.glob(os.path.join(gen_dir, "*.cpp"))):
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().split("\n")
        out, i, changed = [], 0, False
        while i < len(lines):
            h = _VEC_ELEM_HDR.match(lines[i])
            if not (h and i + 2 < len(lines)):
                out.append(lines[i])
                i += 1
                continue
            ea_m = _VEC_ELEM_EA.match(lines[i + 1])
            bad_m = _VEC_ELEM_BAD.match(lines[i + 2])
            # only rewrite the exact full-vector lowering, and only when the
            # clobbered register is the instruction's own destination
            if not (ea_m and bad_m and bad_m.group(1) == h.group(2)):
                out.append(lines[i])
                i += 1
                continue
            mask, field, idx, load = _VEC_ELEM[h.group(1) + "x"]
            out.append(lines[i])
            out.append("\tea = %s & %s;  // rexauto: element load, was a full lvx" 
                       % (ea_m.group(1), mask))
            out.append("\tctx.v%s.%s[%s] = %s(ea);" % (h.group(2), field, idx, load))
            fixed += 1
            changed = True
            i += 3
        if changed:
            with open(fp, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(out))
    if fixed and log:
        log("  codegen fix: %d vector element load(s) (lvebx/lvehx/lvewx) rewritten "
            "from a full-vector load (SDK 0.8.2 lowers them as lvx)" % fixed)
    return fixed
