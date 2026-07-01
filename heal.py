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


def load_overrides_full(toml_path):
    """addr -> {"end", "parent", "size", "name"} (each None if absent). Lossless for the
    [functions] entries -- preserves chunk `parent` links and custom names that the
    end-only loader used to silently drop (which would split those functions on rewrite)."""
    ov = {}
    if os.path.exists(toml_path):
        txt = _read_text(toml_path)
        fm = re.search(r'\[functions\](.*)', txt, re.S)   # ignore [meta] etc.
        body_all = fm.group(1) if fm else txt
        for m in re.finditer(r'"0x([0-9A-Fa-f]+)"\s*=\s*\{([^}]*)\}', body_all):
            a = int(m.group(1), 16)
            b = m.group(2)

            def _hex(key, body=b):
                mm = re.search(key + r'\s*=\s*0x([0-9A-Fa-f]+)', body)
                return int(mm.group(1), 16) if mm else None
            nm = re.search(r'name\s*=\s*"([^"]*)"', b)
            ov[a] = {"end": _hex("end"), "parent": _hex("parent"),
                     "size": _hex("size"), "name": nm.group(1) if nm else None}
    return ov


def _fmt_entry(attrs):
    parts = []
    if attrs.get("size"):
        parts.append("size = 0x%X" % attrs["size"])
    if attrs.get("end"):
        parts.append("end = 0x%X" % attrs["end"])
    if attrs.get("parent"):
        parts.append("parent = 0x%X" % attrs["parent"])
    if attrs.get("name"):
        parts.append('name = "%s"' % attrs["name"])
    return "{ %s }" % ", ".join(parts) if parts else "{}"


def write_overrides_full(toml_path, ov):
    """Write addr -> {end,parent,size,name} losslessly. Preserves any [meta] block."""
    meta = ""
    if os.path.exists(toml_path):
        mm = re.search(r'(\[meta\].*?)\n\[functions\]', _read_text(toml_path), re.S)
        if mm:
            # _read_text reads binary, so a CRLF source file keeps its \r here; the
            # text-mode write below would then turn each \r\n into \r\r\n and break the
            # TOML parse. Normalise the carried-over [meta] block to \n first.
            meta = mm.group(1).rstrip().replace("\r\n", "\n").replace("\r", "\n") + "\n\n"
    header = ("# Boundary/function overrides auto-healed by rexauto.\n"
              "# `end` = extend a function the recompiler split mid-flow;\n"
              "# `parent` = a chunk (address-taken sub-entry) of a parent function;\n"
              "# `{}`  = a function discovered at runtime by the heal loop.\n\n")
    out = header + meta + "[functions]\n"
    for a in sorted(ov):
        out += '"0x%08X" = %s\n' % (a, _fmt_entry(ov[a]))
    open(toml_path, "w").write(out)


def load_overrides(toml_path):
    """Back-compat: addr -> end (or None). `parent`/`name`/`size` stay on disk and are
    preserved across writes -- see write_overrides."""
    return {a: v["end"] for a, v in load_overrides_full(toml_path).items()}


def write_overrides(toml_path, ov):
    """Back-compat for end-only callers. Merges the given {addr: end_or_None} onto the
    on-disk full set so chunk `parent` links (and names) are never dropped."""
    full = load_overrides_full(toml_path)
    for a, end in ov.items():
        attrs = full.get(a) or {"end": None, "parent": None, "size": None, "name": None}
        attrs["end"] = end
        full[a] = attrs
    write_overrides_full(toml_path, full)


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


def forced_landings_from_log(build_log):
    """Landing addresses from every "use of undeclared label 'loc_T'" compile error.
    A dangling goto is, by definition, an in-function jump-table landing the SDK's
    heuristic detectJumpTable under-recovered (an InternalLabel target with no block) --
    never a separate function -- so forcing the SDK to recover it as an in-function block
    is the safe, function-preserving fix (keeps a decompressor loop's back-edge intact)."""
    txt = _read_text(build_log)
    return sorted(set(int(m.group(3), 16) for m in UNDECL.finditer(txt)))


def load_forced(path):
    """Set of addresses in a `forced_landings = [..]` TOML (empty if absent)."""
    if not os.path.exists(path):
        return set()
    m = re.search(r"forced_landings\s*=\s*\[([^\]]*)\]", _read_text(path))
    return set(int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(1))) if m else set()


def write_forced(path, addrs):
    """Merge addrs into the forced_landings TOML. Returns count newly added (0 => no
    change, so the file stays byte-identical on disk)."""
    cur = load_forced(path)
    merged = cur | set(addrs)
    if merged == cur and os.path.exists(path):
        return 0
    body = ", ".join("0x%08X" % a for a in sorted(merged))
    open(path, "w").write(
        "# Jump-table landings the heuristic detectJumpTable under-recovered -- forced to\n"
        "# be recovered as in-function blocks so build_bctr's `goto loc_T` resolves and the\n"
        "# enclosing routine stays whole. Auto-written by rexauto's undeclared-label heal.\n"
        "forced_landings = [%s]\n" % body)
    return len(merged) - len(cur)


def ensure_manifest_include(manifest_path, include_name):
    """Add include_name to the manifest's `includes = [..]` array if missing (idempotent)."""
    if not os.path.exists(manifest_path):
        return
    txt = _read_text(manifest_path)
    if include_name in txt:
        return
    m = re.search(r"(includes\s*=\s*\[)([^\]]*)(\])", txt)
    if not m:
        return
    items = m.group(2).rstrip()
    sep = ", " if items.strip() else ""
    new = m.group(1) + items + '%s"%s"' % (sep, include_name) + m.group(3)
    open(manifest_path, "w").write(txt[:m.start()] + new + txt[m.end():])


def extend_switch_table(addrs, switch_path, spans):
    """For each addr inside a function `end`-span that also contains a bctr switch table,
    add the addr to THAT table's labels. A runtime "invalid function" for an in-routine
    address is a jump-table landing the heuristic under-recovered: it hit the switch's
    `default: REX_CALL_INDIRECT_FUNC` because it was never a `case`. Adding it as a case
    makes build_bctr lower `case 0xA: goto loc_A;` (paired with a forced_landings loc_).
    `spans` = [(start,end)] of end-override routines. Returns count of labels added."""
    if not addrs or not os.path.exists(switch_path):
        return 0
    txt = _read_text(switch_path)
    added = 0
    # walk each [[switch_tables]] block: capture its bctr `address` and `labels` array
    blocks = list(re.finditer(
        r'(\[\[switch_tables\]\].*?address\s*=\s*0x([0-9A-Fa-f]+).*?labels\s*=\s*)\[([^\]]*)\]',
        txt, re.S))
    for m in reversed(blocks):                       # reversed so earlier spans stay valid
        bctr = int(m.group(2), 16)
        routine = next(((s, e) for (s, e) in spans if s <= bctr < e), None)
        if not routine:
            continue
        s, e = routine
        cur = [int(x, 16) for x in re.findall(r'0x[0-9A-Fa-f]+', m.group(3))]
        want = [a for a in addrs if s <= a < e and a not in cur]
        if not want:
            continue
        merged = sorted(set(cur) | set(want))
        added += len(merged) - len(cur)
        body = ", ".join("0x%08X" % a for a in merged)
        txt = txt[:m.start()] + m.group(1) + "[" + body + "]" + txt[m.end():]
    if added:
        open(switch_path, "w").write(txt)
    return added


def register_or_seed(addrs, toml_path, forced_path, switch_path=None):
    """Partition unregistered-function addresses. Any addr that falls INSIDE an existing
    function's `end`-override span is a computed-goto/jump-table LANDING of that routine
    (an indirect target the runtime reached but the static scan left uncovered) -- route
    it to forced_landings so it becomes an in-function block, keeping the routine WHOLE.
    Registering it as a standalone {} instead would SPLIT the routine, and any internal
    loop-back branch into the parent (e.g. a decompressor's `blt -> loc_head`) then
    lowers to REX_FATAL("Unresolved branch") -- a crash the play-and-heal loop can never
    fix (it only heals "invalid function", not "unresolved branch"). Everything else is a
    genuine new function -> {} registration. Returns (n_registered, n_seeded)."""
    full = load_overrides_full(toml_path)
    spans = [(a, v["end"]) for a, v in full.items() if v.get("end")]

    def in_routine(x):
        return any(s <= x < e for s, e in spans)

    landings = sorted(a for a in addrs if in_routine(a))
    funcs = [a for a in addrs if not in_routine(a)]
    n_reg = register_functions(funcs, toml_path)
    # A landing needs BOTH the switch `case` (so the routine's bctr dispatches to it
    # instead of falling to `default: REX_CALL_INDIRECT_FUNC`) and the `loc_` block
    # (so `case 0xA: goto loc_A;` has a target). Add both; either being new is progress.
    n_case = extend_switch_table(landings, switch_path, spans) if (landings and switch_path) else 0
    n_seed = write_forced(forced_path, landings) if landings else 0
    return n_reg, n_seed + n_case


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
