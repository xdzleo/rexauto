"""
heal.py — the two auto-heal mechanisms, parameterised (no hard-coded game).

boundary heal (build-time): the recompiler sometimes splits a function where the
compiler let control fall into the next one. The generated C++ then has
`goto loc_T` with loc_T declared in a different function -> "use of undeclared
label". Fix: extend the function F that owns the goto to swallow T
(F.end = smallest function start strictly > T). Same class the Skate 3 team fixed
by hand ~3500 times. Forced by a real compile error, so no guessing.

runtime heal (run-time): the dispatcher aborts with "invalid or unregistered
function at guest address 0xADDR" when execution reaches a function the recompiler
never discovered. Fix: register 0xADDR and rebuild. The play-and-heal loop.

Both write into one `[functions]` TOML (`{ end = .. }` overrides and `{}`
registrations coexist). Functions here are pure parsing/derivation; the driver
runs the build/run cycles.
"""
import bisect
import glob
import os
import re

DEFRE = re.compile(r"DEFINE_REX_FUNC\(sub_([0-9A-Fa-f]{8})\)")
UNDECL = re.compile(
    r"([^\s:]+\.cpp):(\d+):\d+: error: use of undeclared label 'loc_([0-9A-Fa-f]{8})'")
INVALID = re.compile(
    r"invalid or unregistered function at guest address 0x([0-9A-Fa-f]+)")
# codegen Validate: "0xTARGET from 0xCALLER: ... target not in any function"
UNRESOLVED = re.compile(
    r"0x([0-9A-Fa-f]+) from 0x[0-9A-Fa-f]+.*?target not in any function")


def _read_text(path):
    data = open(path, "rb").read()
    if b"\x00" in data[:64]:
        try:
            return data.decode("utf-16")
        except Exception:
            pass
    return data.decode("utf-8", "ignore")


def func_grid(gen_dir):
    """Per-file [(start_line, addr)] and the global sorted function starts."""
    per_file, starts = {}, set()
    for fp in glob.glob(os.path.join(gen_dir, "*.cpp")):
        rows = []
        with open(fp, "r", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                m = DEFRE.search(line)
                if m:
                    a = int(m.group(1), 16)
                    rows.append((i, a))
                    starts.add(a)
        per_file[os.path.basename(fp)] = rows
    return per_file, sorted(starts)


def _func_at(rows, line):
    best = None
    for ln, a in rows:
        if ln <= line:
            best = a
        else:
            break
    return best


def load_overrides(toml_path):
    """addr -> end (or None for a bare {} registration)."""
    ov = {}
    if os.path.exists(toml_path):
        for m in re.finditer(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{([^}]*)\}', open(toml_path).read()):
            a = int(m.group(1), 16)
            ee = re.search(r'end\s*=\s*0x([0-9A-Fa-f]+)', m.group(2))
            ov[a] = int(ee.group(1), 16) if ee else None
    return ov


def write_overrides(toml_path, ov):
    lines = ["# Boundary/function overrides auto-healed by rexauto.",
             "# `end` = extend a function the recompiler split mid-flow;",
             "# `{}`  = a function discovered at runtime by the heal loop.",
             "", "[functions]"]
    for a in sorted(ov):
        lines.append('"0x%08X" = { end = 0x%08X }' % (a, ov[a]) if ov[a]
                     else '"0x%08X" = {}' % a)
    open(toml_path, "w").write("\n".join(lines) + "\n")


def heal_boundaries(build_log, gen_dir, toml_path):
    """Add `end` extensions for every undeclared-label error. Returns count added."""
    txt = _read_text(build_log)
    errs = [(os.path.basename(m.group(1)), int(m.group(2)), int(m.group(3), 16))
            for m in UNDECL.finditer(txt)]
    if not errs:
        return 0
    per_file, starts = func_grid(gen_dir)
    ov = load_overrides(toml_path)
    added = 0
    for fname, line, T in errs:
        rows = per_file.get(fname)
        if not rows:
            continue
        F = _func_at(rows, line)
        i = bisect.bisect_right(starts, T)
        if F is None or i >= len(starts):
            continue
        nextStart = starts[i]
        if ov.get(F) is None or (ov.get(F) or 0) < nextStart:
            if F not in ov or ov[F] != nextStart:
                added += 1
            ov[F] = nextStart
    write_overrides(toml_path, ov)
    return added


def invalid_functions_from_text(txt):
    """Distinct guest addresses the dispatcher flagged as unregistered."""
    return sorted(set(int(m.group(1), 16) for m in INVALID.finditer(txt)))


def unresolved_calls_from_text(txt):
    """Tail-call targets codegen's Validate phase couldn't place in a function."""
    return sorted(set(int(m.group(1), 16) for m in UNRESOLVED.finditer(txt)))


def invalid_functions(run_log):
    return invalid_functions_from_text(_read_text(run_log))


def register_functions(addrs, toml_path):
    """Add bare `{}` registrations for addrs not already present. Returns count."""
    ov = load_overrides(toml_path)
    added = 0
    for a in addrs:
        if a not in ov:
            ov[a] = None
            added += 1
    if added:
        write_overrides(toml_path, ov)
    return added
