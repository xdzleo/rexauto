"""
xenon-jumptables driver: raw image -> recovered jump tables -> switch_tables TOML.

Steps: wrap the raw image as an ELF, run the IDA analysis pass headless, then
emit the TOML. Point it at IDA's text-mode console (idat / idat64).

Usage:
    python recover.py config.json --ida "C:/Program Files/IDA Professional 9.2/idat.exe"

config.json keys (addresses may be hex strings or ints):
    image       raw code image dump (required)
    image_base  virtual base of the image, e.g. "0x82000000"   (required)
    image_end   end of the image, e.g. "0x826D0000"            (required)
    text_start  start of executable .text                       (required)
    text_end    end of executable .text                         (required)
    functions   file with one hex function address per line     (optional, recommended)
    output      recovered tables JSON     (default: jumptables.json)
    toml        switch_tables TOML output (default: switch_tables.toml)

The image_base / image_end / text_* values are exactly what your recompiler
prints at startup, e.g. ReXGlue/XenonRecomp:
    Function table initialized for module: code=82080000-8230EC00, image=82000000-826D0000
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def resolve(base_dir, p):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base_dir, p))


def find_idat(arg):
    cand = arg or os.environ.get("XJT_IDA")
    if not cand:
        for name in ("idat", "idat64", "idat.exe", "idat64.exe"):
            cand = shutil.which(name)
            if cand:
                break
    if not cand:
        sys.exit("could not find idat; pass --ida <path to idat[64]>")
    if not os.path.basename(cand).lower().startswith("idat"):
        print("warning: %r is not idat (the text-mode console); it may open a GUI "
              "or hang headless. Use idat/idat64." % cand)
    return cand


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="JSON config (see header)")
    ap.add_argument("--ida", default=None, help="path to idat / idat64")
    ap.add_argument("--keep-elf", action="store_true", help="keep the intermediate ELF")
    args = ap.parse_args()

    cfg_dir = os.path.dirname(os.path.abspath(args.config))
    cfg = json.load(open(args.config))

    def need(k):
        if k not in cfg:
            sys.exit("config missing required key: %s" % k)
        return cfg[k]

    image = resolve(cfg_dir, need("image"))
    base = int(str(need("image_base")), 0)
    out_json = resolve(cfg_dir, cfg.get("output", "jumptables.json"))
    out_toml = resolve(cfg_dir, cfg.get("toml", "switch_tables.toml"))
    funcs = resolve(cfg_dir, cfg["functions"]) if cfg.get("functions") else None

    # config the IDA pass reads (absolute paths, normalised)
    work_cfg = out_json + ".cfg.json"
    json.dump({
        "image_base": str(need("image_base")),
        "image_end":  str(need("image_end")),
        "text_start": str(need("text_start")),
        "text_end":   str(need("text_end")),
        "functions":  funcs,
        "output":     out_json,
        "max_table_entries": cfg.get("max_table_entries", 4096),
    }, open(work_cfg, "w"))

    elf = image + ".elf"
    subprocess.check_call([sys.executable, os.path.join(HERE, "make_elf.py"),
                           image, elf, "--base", hex(base)])

    idat = find_idat(args.ida)
    # quote both paths: IDA splits the -S string on whitespace into idc.ARGV, so a
    # space in the checkout dir or config path would otherwise truncate the argv.
    script_arg = '"%s" "%s"' % (os.path.join(HERE, "ida_jumptables.py"), work_cfg)
    log = out_json + ".idalog.txt"
    env = dict(os.environ, TVHEADLESS="1")
    print("running IDA analysis (this can take a few minutes on a full image)...")
    subprocess.check_call([idat, "-A", "-S" + script_arg, "-L" + log, "-c", elf], env=env)

    if not os.path.exists(out_json):
        sys.exit("IDA pass produced no output; see %s" % log)

    subprocess.check_call([sys.executable, os.path.join(HERE, "gen_toml.py"),
                           out_json, "-o", out_toml,
                           "--format", cfg.get("format", "xenonrecomp"),
                           "--text-start", str(need("text_start")),
                           "--text-end", str(need("text_end"))])

    if not args.keep_elf:
        for p in (elf, work_cfg):
            try:
                os.remove(p)
            except OSError:
                pass
    print("done: %s" % out_toml)


if __name__ == "__main__":
    main()
