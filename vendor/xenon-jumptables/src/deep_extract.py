"""deep_extract.py -- IDA deep-analysis function/vtable-target extractor.

Runs inside idat AFTER the jump-table pass (or standalone) with FULL analysis --
it does NOT clear the AF_ flags that ida_jumptables.py disables for speed. It
harvests the candidate function-start set from two mechanisms and emits it as an
ADDITIVE, superset-only overlay the pipeline folds into <name>_functions.toml:

  (1) funcmap   -- every function IDA's flow/cref/no-return analysis discovered
                   (catches lis/addi-referenced targets the linear .pdata scan can
                    reach but our resolver's flow scan misses)
  (2) data-xref -- big-endian dwords in NON-text data that point at is_code text
                   (the vtable / address-taken target class -- the load-bearing
                    NEW signal: exactly the "invalid function"/PPC_CALL_INDIRECT
                    class we heal reactively at runtime today)

Config (JSON, path in argv[1] via idat -S"deep_extract.py cfg.json"):
  { "image_base":..., "text_start":..., "text_end":..., "image_end":...,
    "known":  "path/to/functions_list.txt"   (optional; addrs to EXCLUDE as already known),
    "out_toml": "path/extracted_functions.toml",
    "out_json": "path/extracted.json" }

Emits ONLY addresses NOT already known, 4-aligned, in [text_start,text_end), and
is_code -- so the pipeline's superset-only merge can never downgrade an existing
{end}/{parent}/size cure. Provenance (funcmap|dataref|both, vtbl_base) is recorded
so a later code/data-classification pass can refine.
"""
import json

import ida_auto, ida_bytes, ida_funcs, idautils, ida_pro, idc


def _cfg():
    # idat -S"deep_extract.py cfg.json" -> the cfg path arrives via idc.ARGV[1]
    path = idc.ARGV[1] if len(idc.ARGV) > 1 else "deep_extract_cfg.json"
    return json.load(open(path))


def run(cfg):
    ida_auto.auto_wait()  # FULL analysis (AF flags left at default/deep)

    BASE = int(cfg["image_base"])
    TS, TE = int(cfg["text_start"]), int(cfg["text_end"])
    IMG_END = int(cfg.get("image_end", TE))

    def in_text(a):
        return TS <= a < TE

    # known set to exclude (the linear .pdata / functions_list)
    known = set()
    kp = cfg.get("known")
    if kp:
        try:
            for line in open(kp, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line:
                    try:
                        known.add(int(line, 0) if line.lower().startswith("0x") else int(line, 16))
                    except ValueError:
                        pass
        except OSError:
            pass

    # (1) funcmap
    funcs = set(idautils.Functions(TS, TE))

    # (2) data-xref: non-text dwords pointing at is_code text; remember a source addr
    dataref = {}
    ea = BASE
    step = 4
    while ea < IMG_END:
        if not in_text(ea):
            v = ida_bytes.get_dword(ea)
            if in_text(v) and (v & 3) == 0 and ida_bytes.is_code(ida_bytes.get_flags(v)):
                if v not in dataref:
                    dataref[v] = ea
        ea += step

    # (3) split-immediate (lis/addi | lis/ori) code-address materialization -- the
    # SPLITIMM class, ~31% of the run-heal residue (a 20-port census). The SDK has
    # a functionPointerScan for exactly this but it is DISABLED (analyze.cpp:59,
    # "too many false positives") because it registered targets DIRECTLY. Here it
    # is safe: the candidates flow through the pipeline's pure-add gate, which
    # re-runs codegen and drops any split/stub/swallow. Track the most-recent lis
    # hi-half per GPR across a text linear scan; on a following addi/ori that
    # forms an in-text address, emit it -- but only if it is NOT the interior of a
    # known function (the HEAD guardrail: an interior alternate-entry must never
    # become a {} function, which would skip frame setup = silent stack corruption
    # -- census-confirmed on Forza 0x82489360).
    splitimm = {}
    lis_hi = [None] * 32
    ea = TS
    while ea < TE:
        if not ida_bytes.is_code(ida_bytes.get_flags(ea)):
            ea += 4
            continue
        w = ida_bytes.get_dword(ea)
        op = w >> 26
        rt = (w >> 21) & 31
        ra = (w >> 16) & 31
        if op == 15:                     # addis/lis: rt = (ra? ra : 0) + (imm<<16)
            if ra == 0:
                lis_hi[rt] = (w & 0xFFFF) << 16
            else:
                lis_hi[rt] = None        # addis off another reg -> not a plain hi-load
        elif op == 14 and ra != 0 and lis_hi[ra] is not None:   # addi rt,ra,lo (signed)
            lo = w & 0xFFFF
            if lo & 0x8000:
                lo -= 0x10000
            full = (lis_hi[ra] + lo) & 0xFFFFFFFF
            if in_text(full) and (full & 3) == 0 and full not in splitimm:
                fn = ida_funcs.get_func(full)
                if fn is None or fn.start_ea == full:   # HEAD gate: never mid-function
                    splitimm[full] = ea
            lis_hi[rt] = None            # rt now holds an address, not a hi-half
        elif op == 24 and lis_hi[ra] is not None:               # ori rt,ra,lo (unsigned)
            full = (lis_hi[ra] | (w & 0xFFFF)) & 0xFFFFFFFF
            if in_text(full) and (full & 3) == 0 and full not in splitimm:
                fn = ida_funcs.get_func(full)
                if fn is None or fn.start_ea == full:
                    splitimm[full] = ea
            lis_hi[rt] = None
        else:
            # any other write to rt invalidates its tracked hi-half (conservative)
            if op in (12, 13, 28, 29) or (op == 31):   # addic/addic./andi/andis/X-form
                if rt < 32:
                    lis_hi[rt] = None
        ea += 4

    union = funcs | set(dataref) | set(splitimm)
    out = []
    for a in sorted(union):
        if a in known:
            continue
        if (a & 3) != 0 or not in_text(a):
            continue
        # accept IDA-code OR a split-immediate target (a lis/addi may materialize a
        # real function IDA parked as data; the pure-add gate is the final arbiter).
        if not (ida_bytes.is_code(ida_bytes.get_flags(a)) or a in splitimm):
            continue
        tags = []
        if a in funcs:
            tags.append("funcmap")
        if a in dataref:
            tags.append("dataref")
        if a in splitimm:
            tags.append("splitimm")
        rec = {"addr": "0x%08X" % a, "src": "+".join(tags)}
        if a in dataref:
            rec["vtbl_ref"] = "0x%08X" % dataref[a]
        if a in splitimm:
            rec["imm_ref"] = "0x%08X" % splitimm[a]
        out.append(rec)

    # emit: additive superset-only overlay in heal.py's {} format + provenance json
    with open(cfg["out_toml"], "w", encoding="utf-8") as f:
        f.write("# Auto-generated by deep_extract.py (IDA deep analysis).\n")
        f.write("# Additive, superset-only: function/vtable-target starts the linear scan\n")
        f.write("# missed. The pipeline merges these only where no cure already exists.\n")
        f.write("[functions]\n")
        for r in out:
            f.write('"%s" = {}\n' % r["addr"])
    with open(cfg["out_json"], "w", encoding="utf-8") as f:
        json.dump({"count": len(out), "funcmap": len(funcs), "dataref": len(dataref),
                   "splitimm": len(splitimm), "union": len(union),
                   "known_excluded": len(known & union), "emitted": out}, f, indent=1)
    print("[deep_extract] emitted %d new starts (funcmap=%d dataref=%d splitimm=%d union=%d, "
          "excluded %d known)"
          % (len(out), len(funcs), len(dataref), len(splitimm), len(union),
             len(known & union)))


if __name__ == "__main__":
    try:
        run(_cfg())
    finally:
        ida_pro.qexit(0)
