"""
Convert recovered jump tables (jumptables.json) into a switch-table TOML.

Two output formats (same data, different field names):

  xenonrecomp  ->  [[switch]]        base / r / labels      (XenonRecomp,
                                                             switch_table_file_path)
  rexglue      ->  [[switch_tables]] address / register / labels

Both recompilers key the generated switch on the index register `r`; consider
the `switch-on-ctr` codegen change (see patches/) so that an in-place-clobbered
index doesn't matter.

Usage:
    python gen_toml.py jumptables.json -o switch_tables.toml \\
        --format xenonrecomp --text-start 0x82080000 --text-end 0x8230EC00
"""
import argparse
import json


def load_tables(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("_summary"):
                continue
            if "targets" not in obj or "bctr" not in obj:
                continue  # tolerate a partially-written / hand-edited file
            out.append(obj)
    return out


def emit(tables, fmt):
    total = sum(len(t["targets"]) for t in tables)
    lines = ["# Jump tables recovered by xenon-jumptables.",
             "# %d tables, %d total case targets." % (len(tables), total), ""]
    for t in sorted(tables, key=lambda x: x["bctr"]):
        labels = ", ".join("0x%08X" % x for x in t["targets"])
        reg = t.get("idx_reg") or 0
        if fmt == "xenonrecomp":
            lines += ["[[switch]]",
                      "base = 0x%08X" % t["bctr"],
                      "r = %d" % reg,
                      "labels = [%s]" % labels]
        else:  # rexglue
            lines += ["[[switch_tables]]",
                      "address = 0x%08X" % t["bctr"],
                      "register = %d" % reg,
                      "labels = [%s]" % labels]
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", help="jumptables.json from the IDA pass")
    ap.add_argument("-o", "--out", default="switch_tables.toml")
    ap.add_argument("--format", choices=["xenonrecomp", "rexglue"], default="xenonrecomp")
    ap.add_argument("--text-start", required=True, help="e.g. 0x82080000")
    ap.add_argument("--text-end", required=True, help="e.g. 0x8230EC00")
    args = ap.parse_args()

    lo, hi = int(args.text_start, 0), int(args.text_end, 0)
    tables = load_tables(args.json)

    bad = []
    for t in tables:
        for x in t["targets"]:
            if (x & 3) or not (lo <= x < hi):
                bad.append((t["bctr"], x))
    if bad:
        for bctr, x in bad[:10]:
            print("  invalid target 0x%08X in table 0x%08X" % (x, bctr))
        raise SystemExit("aborting: %d invalid target(s)" % len(bad))

    with open(args.out, "w") as f:
        f.write(emit(tables, args.format))
    print("wrote %s (%s): %d tables, %d targets" % (
        args.out, args.format, len(tables), sum(len(t["targets"]) for t in tables)))


if __name__ == "__main__":
    main()
