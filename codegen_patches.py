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
