#!/usr/bin/env python3
"""residue_triage.py -- separate the closure residue into evidence tiers.

closure_cert reports every statically-derivable control target with no landing pad in
the emitted code. That raw count is NOT a work list: two of its five target classes are
heuristic and dominate it.

  ptr      -- big-endian dwords in non-text data that fall numerically inside the code
              range. A vtable slot looks exactly like an int constant that happens to be
              0x82xxxxxx, and the fleet is full of the latter.
  splitimm -- lis+addi / lis+ori pairs forming an in-range address. Also how the compiler
              materialises a DATA address, a float pool base, or an offset.

Measured on the 2026-08-19 fleet baseline: 31,491 raw holes, of which only 54 carry a
function prologue and only 50 are named by two independent target classes. The rest show
either nothing (56.8%) or a "border" signal so weak it fires on any address following
zero padding. So the raw number massively overstates what is actually missing, and any
per-title "closed %" built on it is inflated in the same proportion.

This tool applies the two signals that are NOT heuristic and emits the tier that is worth
a human's time:

  prologue -- the word AT the address is a function opener: mflr r12 (7D8802A6),
              mflr r0 (7C0802A6), stwu r1,-x(r1) (9421xxxx), stdu r1,-x(r1) (F821xxxx).
  crossed  -- two independent target classes name the same address. Class-specific false
              positives do not correlate, so agreement is real evidence.

Either one qualifies. Everything else is reported as a count, never as a work item.

Read-only. Consumes metrics/closure_baseline.json plus the raw images.
"""
import argparse
import json
import os
import re
import struct
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASELINE = os.path.join(HERE, "closure_baseline.json")

MFLR12, MFLR0 = 0x7D8802A6, 0x7C0802A6
BLR, BCTR, NOP = 0x4E800020, 0x4E800420, 0x60000000


def _image_base(work, top, name, is_mod):
    p = os.path.join(work, top, "port", "generated",
                     name if is_mod else "default", name + "_init.h")
    h = open(p, encoding="utf-8", errors="ignore").read()
    return int(re.search(r"REX_IMAGE_BASE\s+0x([0-9A-Fa-f]+)", h).group(1), 16)


def triage(baseline):
    d = json.load(open(baseline, encoding="utf-8"))
    work = d["work"]
    out, tiers = {}, Counter()
    for c in d["certs"]:
        port = c["port"]
        is_mod = "/" in port
        top, name = (port.split("/") + [port])[:2] if is_mod else (port, port)
        img_path = os.path.join(work, top, name + "_image.bin")
        if not os.path.exists(img_path):
            continue
        img = open(img_path, "rb").read()
        ib = _image_base(work, top, name, is_mod)

        def word(a):
            o = a - ib
            return struct.unpack_from(">I", img, o)[0] if 0 <= o + 4 <= len(img) else None

        origin = defaultdict(set)
        for cls, e in c["classes"].items():
            for a in e["holes"]:
                origin[a].add(cls)
        hits = []
        for a in c["holes"]:
            w = word(a)
            prologue = w is not None and (w in (MFLR12, MFLR0)
                                          or (w >> 16) == 0x9421 or (w >> 16) == 0xF821)
            crossed = len(origin[a]) >= 2
            if prologue or crossed:
                hits.append({"addr": "0x%08X" % a,
                             "why": "prologue" if prologue else "crossed",
                             "classes": sorted(origin[a]),
                             "word": "%08X" % w if w is not None else None})
                tiers["prologue" if prologue else "crossed"] += 1
            else:
                tiers["low-evidence"] += 1
        if hits:
            out[port] = hits
    return d, out, tiers


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--json", help="write the high-evidence work list here")
    ap.add_argument("--md", help="write the readable work list here")
    a = ap.parse_args()

    d, hits, tiers = triage(a.baseline)
    total = sum(tiers.values())
    hi = tiers["prologue"] + tiers["crossed"]
    prov = d.get("provenance", {})

    lines = ["# Closure residue — the part that is actually evidence", "",
             "From `closure_baseline.json` measured %s, rexglue `%s`."
             % (prov.get("when", "?"), prov.get("rexglue_sha256", "")[:16] or "(none)"), "",
             "| tier | addresses | share |", "|---|---|---|"]
    for k in ("prologue", "crossed", "low-evidence"):
        lines.append("| %s | %s | %.2f%% |" % (k, "{:,}".format(tiers[k]),
                                               100.0 * tiers[k] / total if total else 0))
    lines += ["| **actionable (prologue + crossed)** | **%d** | **%.2f%%** |"
              % (hi, 100.0 * hi / total if total else 0), "",
              "The low-evidence tier is NOT a backlog. It is what two heuristic target",
              "classes produce when a data word happens to look like a code address.", "",
              "## Work list", "",
              "| title / module | n | addresses |", "|---|---|---|"]
    for port in sorted(hits, key=lambda p: -len(hits[p])):
        v = hits[port]
        lines.append("| %s | %d | %s |"
                     % (port, len(v), " ".join("`%s`" % h["addr"] for h in v)))
    md = "\n".join(lines) + "\n"

    print(md)
    if a.md:
        open(a.md, "w", encoding="utf-8", newline="").write(md)
        print("wrote %s" % a.md)
    if a.json:
        json.dump({"provenance": prov, "tiers": dict(tiers), "hits": hits},
                  open(a.json, "w"), indent=1)
        print("wrote %s" % a.json)


if __name__ == "__main__":
    main()
