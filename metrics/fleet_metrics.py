#!/usr/bin/env python3
"""
fleet_metrics -- read every port's checkpoint and print the two tables the loop
charter pins at the top of LOOP.md: the per-title **Scoreboard** and the one-row
**Fleet ratchet**.

Read-only, always: it opens `<work>/<title>/.rexauto_state`, the companion
statefiles `.rexauto_state_<key>` beside them, and `baselines/*.runtime.json`.
It never writes into a port, a baseline, a gabarito or the cache.

    python metrics/fleet_metrics.py [--work ROOT] [--csv [PATH]] [--iter it-001]

WHAT THIS TOOL WILL NOT DO: print a number it does not have. A stage that ran
before the timing instrument existed prints `?`, not `0.0`; a fleet with no
recorded seconds prints UNMEASURED, not a sum of zeroes; a hole count that no
stage computes prints UNMEASURED, not 0. Every "0" in this project's ledger has
to be a measured zero, because the ratchet is compared cell by cell across
iterations and one fabricated zero in row N makes row N+1 look like a regression
that never happened.

Where the fleet lives: the root defaults to the SAME value rexauto.py's `--work`
resolves to (env REXAUTO_WORK first, then rexauto.py's own literal default, read
out of that file so the two cannot drift). If that root holds no checkpoints the
tool says so loudly and exits non-zero rather than printing an empty fleet that
reads like a healthy one -- see LOOP.md Q1: the pipeline's default root and the
tree the fleet actually lives in have diverged, and a reporter that silently
"finds" a different fleet than the pipeline writes is worse than one that stops.
"""
import argparse
import csv
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REXAUTO_DIR = os.path.dirname(HERE)

# Kept in the same order as rexauto.STAGES so a column never silently changes
# meaning between two runs of this tool.
STAGES = ["extract", "xctd", "init", "setjmp", "jumptables", "deepextract",
          "build", "runheal", "run"]
SHORT = {"extract": "ex", "xctd": "xc", "init": "in", "setjmp": "sj", "jumptables": "jt",
         "deepextract": "dx", "build": "bd", "runheal": "rh", "run": "run"}
FALLBACK_WORK = r"C:\Skate3\autoports"

# Cell vocabulary. Deliberately four distinct symbols, because collapsing any two
# of them into one loses the distinction the ledger is built on.
CELL_ABSENT = "-"      # no such key in the checkpoint: the stage never ran here
CELL_SKIPPED = "skip"  # the last run skipped it (already checkpointed) -- NOT 0s
CELL_UNTIMED = "?"     # the stage ran, but no timing was recorded: UNMEASURED
UNMEASURED = "UNMEASURED"


def pipeline_default_work():
    """Resolve the root the way rexauto.py itself does, by reading rexauto.py's
    own argparse default rather than restating it here. A second literal is a
    second thing to forget: the gate already carries one and it has been pointing
    at a tree with no manifests since the fleet moved."""
    env = os.environ.get("REXAUTO_WORK")
    if env:
        return env
    try:
        src = open(os.path.join(REXAUTO_DIR, "rexauto.py"), encoding="utf-8",
                   errors="ignore").read()
        m = re.search(r'"--work",\s*default=os\.environ\.get\(\s*"REXAUTO_WORK",\s*r?"([^"]*)"',
                      src)
        if m:
            return m.group(1)
    except OSError:
        pass
    return FALLBACK_WORK


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        return {"_unreadable": str(ex)}


def _d(v):
    """A stage mark is a dict on the happy path, but the fleet also carries `true`,
    `null` and a bare int (build_parallel). Never index one blindly."""
    return v if isinstance(v, dict) else {}


def runtime_tiers():
    """health_tier per title, from the blessed runtime baselines. Absent for most
    of the fleet by construction -- the runtime tier has baselines for 3 titles --
    so this reports "no baseline", never a default tier."""
    out = {}
    bdir = os.path.join(REXAUTO_DIR, "baselines")
    if not os.path.isdir(bdir):
        return out
    for fn in sorted(os.listdir(bdir)):
        if not fn.endswith(".runtime.json"):
            continue
        b = _load(os.path.join(bdir, fn))
        out[fn[:-len(".runtime.json")]] = b.get("health_tier")
    return out


def read_port(root, name):
    """Everything this tool knows about one title, straight off its checkpoint."""
    d = os.path.join(root, name)
    st = _load(os.path.join(d, ".rexauto_state"))
    p = {"name": name, "dir": d, "state": st, "unreadable": st.get("_unreadable"),
         "modules": {}}
    for fn in sorted(os.listdir(d)):
        if fn.startswith(".rexauto_state_"):
            p["modules"][fn[len(".rexauto_state_"):]] = _load(os.path.join(d, fn))
    t = st.get("timings")
    p["timings"] = t if isinstance(t, dict) and t.get("schema") == 1 else None
    p["tables"] = _d(st.get("jumptables")).get("tables")
    p["jt_cache"] = _d(st.get("jumptables")).get("cache")
    p["cands"] = _d(st.get("deepextract")).get("candidates")
    p["accepted"] = _d(st.get("deepextract")).get("accepted")
    rh = _d(st.get("runheal"))
    p["iters"] = rh.get("iters")
    p["alive"] = rh.get("alive")
    p["window"] = rh.get("confirmed_seconds")
    p["receipt"] = rh.get("receipt") is True
    p["building"] = bool(_d(st.get("build")).get("exe"))
    # No stage computes a closure-hole count today and no checkpoint carries one.
    # Read the key anyway so the column starts reporting the day one does, instead
    # of needing this file edited at the same time.
    p["holes"] = st.get("holes", _d(st.get("closure")).get("holes"))
    return p


def stage_cell(p, stage):
    """One Scoreboard cell: seconds when they were measured, and a symbol that says
    exactly WHY not when they were not."""
    t = p["timings"]
    if t:
        s = _d(t.get("stages")).get(stage)
        if isinstance(s, dict) and isinstance(s.get("wall_s"), (int, float)):
            return "%.1f" % s["wall_s"]
        if stage in (_d(t.get("last")).get("skipped") or []):
            return CELL_SKIPPED
    if stage not in p["state"]:
        return CELL_ABSENT
    return CELL_UNTIMED


def total_cell(p):
    t = p["timings"]
    last = _d(t.get("last")) if t else {}
    if isinstance(last.get("total_s"), (int, float)):
        return "%.1f%s" % (last["total_s"], "" if last.get("status") == "complete" else "*")
    return CELL_UNTIMED


def fmt(v, none=CELL_ABSENT):
    return none if v is None else str(v)


def print_scoreboard(ports, tiers, out):
    cols = ["title"] + [SHORT[s] for s in STAGES] + \
           ["total", "tables", "cand->acc", "iters", "window", "holes", "tier"]
    # The title column is sized to the longest name present. A truncated title is
    # an ambiguous row, and this table's whole job is to be read per title --
    # "dragon_ball_z_ultimate_tenkaic" and "spider_man_shattered_dimension" are
    # both 30 characters of nothing.
    tw = max([5] + [len(p["name"]) for p in ports] +
             [len(k) + 3 for p in ports for k in p["modules"]])
    w = [tw] + [5] * len(STAGES) + [8, 7, 12, 6, 7, 10, 5]
    line = lambda cells: "  ".join(str(c)[:n].ljust(n) for c, n in zip(cells, w))
    out.append(line(cols))
    out.append("  ".join("-" * n for n in w))
    for p in ports:
        if p["unreadable"]:
            out.append(line([p["name"], "UNREADABLE: " + p["unreadable"]]))
            continue
        tier = tiers.get(p["name"], "none")
        out.append(line(
            [p["name"]] + [stage_cell(p, s) for s in STAGES] +
            [total_cell(p), fmt(p["tables"]),
             ("%s->%s" % (p["cands"], p["accepted"])) if p["cands"] is not None else CELL_ABSENT,
             fmt(p["iters"]), fmt(p["window"]),
             UNMEASURED if p["holes"] is None else str(p["holes"]),
             "none" if tier is None else str(tier)]))
        for key, ms in sorted(p["modules"].items()):
            mt = ms.get("timings")
            mt = mt if isinstance(mt, dict) and mt.get("schema") == 1 else None
            mp = {"name": key, "state": ms, "timings": mt}
            cands = _d(ms.get("deepextract")).get("candidates")
            acc = _d(ms.get("deepextract")).get("accepted")
            out.append(line(
                ["  +" + key] + [stage_cell(mp, s) for s in STAGES] +
                ["", fmt(_d(ms.get("jumptables")).get("tables")),
                 ("%s->%s" % (cands, acc)) if cands is not None else CELL_ABSENT,
                 "", "", "", ""]))
    out.append("")
    out.append("cells:  %s = the stage has no key in this checkpoint (never ran here)" % CELL_ABSENT)
    out.append("        %s = the last run skipped it because it was already checkpointed"
               % CELL_SKIPPED)
    out.append("        %s = it RAN but no duration was recorded -- UNMEASURED, and that is "
               "not zero" % CELL_UNTIMED)
    out.append("        total*  = the run ended partial (a stage raised); the total omits nothing "
               "but stopped early")
    out.append("        rows starting '+' are companion modules, read from "
               ".rexauto_state_<key> beside the port")


def print_timing_detail(ports, out):
    timed = [p for p in ports if p["timings"] and _d(p["timings"].get("stages"))]
    out.append("## Timing detail (cold/warm context -- seconds without it are not comparable)")
    out.append("")
    if not timed:
        out.append("No title carries timing data yet. The instrument writes it on the next")
        out.append("pipeline run; until then every stage duration in this fleet is UNMEASURED.")
        out.append("Per-frame detail lands in <work>/<title>/.rexauto_timings.jsonl.")
        out.append("")
        return
    for p in timed:
        t = p["timings"]
        host = _d(t.get("host"))
        last = _d(t.get("last"))
        out.append("%s   host cpus=%s ram_gb=%s   last run %s (%s)"
                   % (p["name"], host.get("cpus"), host.get("ram_gb"),
                      last.get("run"), last.get("status")))
        cold = _d(last.get("cold"))
        out.append("    run cold/warm: no_ida_cache=%s resumed_checkpoint=%s   skipped: %s"
                   % (cold.get("no_ida_cache"), cold.get("resumed_checkpoint"),
                      ", ".join(last.get("skipped") or []) or "none"))
        for s in STAGES:
            e = _d(t.get("stages")).get(s)
            if not isinstance(e, dict):
                continue
            sub = _d(e.get("sub"))
            subs = " ".join("%s=%s" % (k, v) for k, v in sorted(sub.items())) or "-"
            colds = " ".join("%s=%s" % (k, v) for k, v in sorted(_d(e.get("cold")).items())) or "-"
            out.append("    %-12s wall %8.1fs  self %8.1fs  child %8.1fs  [%s]  sub: %s"
                       % (s, e.get("wall_s", 0.0), e.get("self_s", 0.0), e.get("child_s", 0.0),
                          e.get("status"), subs))
            out.append("    %-12s cold/warm: %s" % ("", colds))
        out.append("")


def print_ratchet(ports, tiers, label, out):
    n = len(ports)
    building = sum(1 for p in ports if p["building"])
    # A tier-0 receipt is minted ONLY on positive evidence (rexauto.py: "if alive and
    # range_exact"), so receipt=True IS a live convergence. But the two verdicts come from
    # MUTUALLY EXCLUSIVE branches of the same mark(): the receipt path writes
    # {receipt, verdict} with no "alive"; the launched path writes {iters, alive,
    # confirmed_seconds} with no "receipt". Counting "alive is True" alone therefore drops
    # every receipt title OUT of the very column zero_launch is announced as a subset of --
    # the ratchet's second row would have reported a phantom multi-title regression with the
    # fleet untouched. Convergence is the union; zero_launch stays the subset.
    converging = sum(1 for p in ports if p["alive"] is True or p["receipt"])
    zero_launch = sum(1 for p in ports if p["receipt"])
    holed = [p for p in ports if p["holes"] is not None]
    zero_holes = sum(1 for p in holed if p["holes"] == 0)
    tier3 = sum(1 for p in ports if tiers.get(p["name"]) == 3)
    timed = [p for p in ports
             if p["timings"] and isinstance(_d(p["timings"].get("last")).get("total_s"),
                                            (int, float))]
    cold = [p for p in timed
            if _d(_d(p["timings"].get("last")).get("cold")).get("no_ida_cache")]
    if cold and len(cold) == n:
        seconds = "%.1f" % sum(_d(p["timings"]["last"])["total_s"] for p in cold)
    else:
        seconds = "%s (%d/%d titles timed, %d of them cold)" % (UNMEASURED, len(timed), n, len(cold))
    holes_cell = ("%d / %d" % (zero_holes, n)) if len(holed) == n \
        else "%s (%d of %d measurable)" % (UNMEASURED, len(holed), n)

    out.append("## Fleet ratchet")
    out.append("")
    out.append("| iter | titles building | titles converging | converging w/ 0 launches "
               "| titles w/ 0 holes | titles at tier 3 | fleet cold seconds |")
    out.append("|---|---|---|---|---|---|---|")
    out.append("| %s | %d / %d | %d / %d | %d / %d | %s | %d / %d | %s |"
               % (label, building, n, converging, n, zero_launch, n, holes_cell, tier3, n, seconds))
    out.append("")
    out.append("counted as (state this the same way next iteration or the row is not comparable):")
    out.append("  building        = build.exe present in .rexauto_state")
    out.append("  converging      = runheal.alive is true OR runheal.receipt is true. Both are positive")
    out.append("                    evidence and they come from mutually exclusive mark() branches: a")
    out.append("                    receipt is only ever minted from a live convergence, so counting")
    out.append("                    alive alone would drop receipt titles out of the column that")
    out.append("                    '0 launches' is a subset of.")
    out.append("  0 launches      = runheal.receipt is true -- the tier-0 receipt path returned "
               "without launching")
    out.append("  0 holes         = a holes count in the checkpoint. No stage computes one today, "
               "so this is UNMEASURED,")
    out.append("                    not 0: closure_cert is not wired into any stage and its cover "
               "predicate is vacuous.")
    out.append("  tier 3          = health_tier 3 in baselines/<title>.runtime.json AND a port "
               "under this root")
    out.append("  fleet cold secs = sum of timings.last.total_s over ALL titles, and only when "
               "every one of them was")
    out.append("                    measured cold (REXAUTO_NO_IDA_CACHE=1). A mixed cold/warm "
               "sum is not a fleet cold time.")


def write_csv(path, ports, tiers):
    """One row per title (and per companion module), so an iteration can diff two
    runs of this tool instead of two screenshots."""
    cols = (["title", "module"] + ["%s_s" % s for s in STAGES] +
            ["total_s", "run_status", "no_ida_cache", "resumed", "tables", "jt_cache",
             "candidates", "accepted", "heal_iters", "alive", "confirmed_seconds", "receipt",
             "building", "holes", "health_tier"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in ports:
            last = _d(p["timings"].get("last")) if p["timings"] else {}
            cold = _d(last.get("cold"))
            w.writerow([p["name"], ""] + [stage_cell(p, s) for s in STAGES] +
                       [last.get("total_s", UNMEASURED), last.get("status", UNMEASURED),
                        cold.get("no_ida_cache", UNMEASURED), cold.get("resumed_checkpoint",
                                                                      UNMEASURED),
                        fmt(p["tables"]), fmt(p["jt_cache"]), fmt(p["cands"]), fmt(p["accepted"]),
                        fmt(p["iters"]), fmt(p["alive"]), fmt(p["window"]), p["receipt"],
                        p["building"], UNMEASURED if p["holes"] is None else p["holes"],
                        fmt(tiers.get(p["name"]), "none")])
            for key, ms in sorted(p["modules"].items()):
                mt = ms.get("timings")
                mp = {"name": key, "state": ms,
                      "timings": mt if isinstance(mt, dict) and mt.get("schema") == 1 else None}
                w.writerow([p["name"], key] + [stage_cell(mp, s) for s in STAGES] +
                           ["", "", "", "", fmt(_d(ms.get("jumptables")).get("tables")), "",
                            fmt(_d(ms.get("deepextract")).get("candidates")),
                            fmt(_d(ms.get("deepextract")).get("accepted")),
                            "", "", "", "", "", "", ""])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=pipeline_default_work(),
                    help="fleet root (default: the same value rexauto.py --work resolves to)")
    ap.add_argument("--iter", default="(unlabelled)", help="ledger row label, e.g. it-001")
    ap.add_argument("--csv", nargs="?", const=os.path.join(HERE, "fleet_metrics.csv"),
                    default=None, help="also write a per-title CSV (default metrics/fleet_metrics.csv)")
    ap.add_argument("titles", nargs="*", help="limit to these titles")
    args = ap.parse_args()

    root = args.work
    names = []
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            if os.path.exists(os.path.join(root, d, ".rexauto_state")):
                names.append(d)
    if args.titles:
        names = [n for n in names if n in args.titles]
    if not names:
        # Loud, not empty. An empty fleet table looks exactly like a healthy fleet
        # with nothing wrong in it, and that is the one failure mode a ratchet
        # report must never have.
        sys.stderr.write(
            "fleet_metrics: no .rexauto_state under %s\n"
            "  This tool deliberately does NOT go hunting for a different fleet: the whole\n"
            "  point is to report on the tree the pipeline writes. Pass --work <root>, or set\n"
            "  REXAUTO_WORK, so the reader and rexauto.py agree. See LOOP.md Q1 -- the\n"
            "  pipeline's default root and the live fleet have diverged.\n" % root)
        return 2

    ports = [read_port(root, n) for n in names]
    tiers = runtime_tiers()
    out = []
    out.append("rexauto fleet metrics -- %s" % root)
    out.append("%d port(s) with a checkpoint, %d companion module(s), read %s"
               % (len(ports), sum(len(p["modules"]) for p in ports),
                  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    out.append("")
    out.append("## Scoreboard")
    out.append("")
    print_scoreboard(ports, tiers, out)
    out.append("")
    print_timing_detail(ports, out)
    print_ratchet(ports, tiers, args.iter, out)
    sys.stdout.write("\n".join(out) + "\n")

    if args.csv:
        write_csv(args.csv, ports, tiers)
        sys.stdout.write("\ncsv: %s\n" % args.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
