#!/usr/bin/env python3
"""gamepatches.py -- catalogo de patches da comunidade, casado com um port.

Fonte: github.com/xenia-canary/game-patches (patches para o emulador Xenia).
Cada arquivo `<TITLEID> - <Nome>.patch.toml` traz varios blocos [[patch]], e cada
um lista escritas na imagem do jogo ([[patch.be32]], [[patch.be8]], ...).

O PONTO DELICADO, e a razao deste modulo existir: o rexauto e um recompilador
ESTATICO. Uma escrita que cai em .text altera INSTRUCAO -- ela precisa entrar
antes do codegen (vira codigo nativo compilado) e NAO pode ser ligada/desligada
em runtime. Uma escrita que cai em dados pode, em tese, ser aplicada em runtime.
Prometer um toggle instantaneo para a primeira classe seria mentir para o
usuario, entao cada patch recebe um SELO:

  RECOMPILAR -- tem ao menos uma escrita em .text; exige rebuild do port
  RUNTIME    -- todas as escritas caem fora de .text

O selo do patch e sempre o PIOR selo entre as suas escritas.

Uso:
    python gamepatches.py list <port_dir>            # relatorio
    python gamepatches.py apply <port_dir> <nome>... # gera o _guest_patches.toml
"""
import json
import os
import re
import struct
import tomllib
import sys
import urllib.request

CATALOG_API = "https://api.github.com/repos/xenia-canary/game-patches/contents/patches"
RAW_BASE = "https://raw.githubusercontent.com/xenia-canary/game-patches/main/patches/"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "gamepatches")

# larguras suportadas pelo formato da comunidade -> bytes
WIDTHS = {"be8": 1, "be16": 2, "be32": 4, "be64": 8}


# --------------------------------------------------------------------------
# identificacao do port
# --------------------------------------------------------------------------
def _xex_title_id(head):
    """title_id (8 hex) do header XEX2 -- mesma logica de extract.py:_xex_title_id."""
    if head[:4] != b"XEX2":
        return None
    try:
        cnt = struct.unpack_from(">I", head, 0x14)[0]
        if cnt > 4096:
            return None
        for i in range(cnt):
            key, val = struct.unpack_from(">II", head, 0x18 + i * 8)
            if key == 0x00040006 and val + 0x10 <= len(head):
                return "%08X" % struct.unpack_from(">I", head, val + 0x0C)[0]
    except Exception:
        return None
    return None


def _module_info(port_dir, man_path):
    """Um modulo do port: nome, faixa de codigo e faixa de imagem.

    O out_directory_path vem do PROPRIO manifesto. Assumir "generated/default"
    quebrava calado em todo port multi-modulo: o Spider-Man gera em
    generated/gamelogic e generated/default, e a busca ia procurar o init.h do
    gamelogic dentro de default.
    """
    name = os.path.basename(man_path)[: -len("_manifest.toml")]
    try:
        d = tomllib.load(open(man_path, "rb"))
    except Exception:
        return None
    ep = d.get("entrypoint", {}) or {}
    out_dir = ep.get("out_directory_path") or ("generated/" + name)
    init_h = os.path.join(port_dir, *out_dir.split("/"), name + "_init.h")
    if not os.path.exists(init_h):
        return None
    txt = open(init_h, encoding="utf-8", errors="ignore").read()

    def g(key):
        m = re.search(key + r"\s+0x([0-9A-Fa-f]+)", txt)
        return int(m.group(1), 16) if m else None

    cb, cs, ib = g("REX_CODE_BASE"), g("REX_CODE_SIZE"), g("REX_IMAGE_BASE")
    if None in (cb, cs, ib):
        return None
    img = os.path.join(os.path.dirname(port_dir.rstrip("\\/")), name + "_image.bin")
    size = os.path.getsize(img) if os.path.exists(img) else 0x10000000
    src = ep.get("file_path") or ""
    return {"name": name, "manifest": man_path, "out_dir": out_dir,
            "code_base": cb, "code_size": cs, "code_end": cb + cs,
            "image_base": ib, "image_end": ib + size,
            "source": src, "is_xex": src.lower().endswith(".xex")}


def port_modules(port_dir):
    """Todos os modulos do port, o executavel principal primeiro."""
    mods = []
    for f in sorted(os.listdir(port_dir)):
        if f.endswith("_manifest.toml"):
            mi = _module_info(port_dir, os.path.join(port_dir, f))
            if mi:
                mods.append(mi)
    if not mods:
        raise SystemExit("nenhum modulo utilizavel em %s -- rode o codegen antes" % port_dir)
    mods.sort(key=lambda m: (not m["is_xex"], m["name"]))
    return mods


def owning_module(mods, addr):
    """Qual modulo contem este endereco guest, se algum."""
    for m in mods:
        if m["image_base"] <= addr < m["image_end"]:
            return m
    return None


def port_identity(port_dir):
    """(nome do modulo principal, title_id, [modulos])."""
    mods = port_modules(port_dir)
    main = mods[0]
    xex = os.path.normpath(os.path.join(port_dir, "..", "game", "default.xex"))
    tid = None
    if os.path.exists(xex):
        with open(xex, "rb") as f:
            tid = _xex_title_id(f.read(0x10000))
    return main["name"], tid, mods


# --------------------------------------------------------------------------
# catalogo
# --------------------------------------------------------------------------
def _cached(path, fetch):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="ignore").read()
    body = fetch()
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    return body


def catalog_index():
    """[{name, download_url}] -- baixado sob demanda e cacheado (o repo nao tem
    LICENSE, entao nada e redistribuido junto do rexauto)."""
    def fetch():
        req = urllib.request.Request(CATALOG_API, headers={"User-Agent": "rexauto"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        return json.dumps([{"name": x["name"]} for x in data if x["name"].endswith(".patch.toml")])
    return json.loads(_cached(os.path.join(CACHE, "index.json"), fetch))


def _port_has_tu(port_dir):
    """O port esta em title update? (existe .xexp estagiado no game dir)"""
    game = os.path.normpath(os.path.join(port_dir, "..", "game"))
    for root, _, files in os.walk(game):
        for f in files:
            if f.lower().endswith(".xexp"):
                return True
    return False


def files_for_title(tid, port_dir=None):
    """Arquivos de patch do titulo, JA FILTRADOS pela variante certa.

    O catalogo traz variantes por build -- "<TID> - Nome.patch.toml" (base) e
    "<TID> - Nome (TU4).patch.toml" (title update). Os enderecos sao TOTALMENTE
    diferentes entre elas: no Judgment o "Unlock FPS" e 0x8255DE08 na base e
    0x8255E220 no TU4. Aplicar a variante errada escreve em cima de instrucao
    aleatoria -- quebra silenciosa e dificil de rastrear. Por isso escolhemos UMA.

    Criterio definitivo seria o hash do codigo (o que o Xenia usa); enquanto ele
    nao existe, usamos o sinal disponivel: port sem .xexp -> variante base."""
    idx = catalog_index()
    hits = [x["name"] for x in idx if x["name"].upper().startswith(tid.upper())]
    if len(hits) <= 1 or port_dir is None:
        return hits
    has_tu = _port_has_tu(port_dir)
    tu_like = [h for h in hits if re.search(r"\(TU\d+\)", h)]
    base_like = [h for h in hits if h not in tu_like]
    chosen = (tu_like or hits) if has_tu else (base_like or hits)
    if len(chosen) > 1:
        raise SystemExit(
            "ambiguo: %d variantes casam e nao da para desempatar sem hash do codigo:\n  %s"
            % (len(chosen), "\n  ".join(chosen)))
    return chosen


def fetch_patch_file(fname):
    def fetch():
        url = RAW_BASE + urllib.request.quote(fname)
        req = urllib.request.Request(url, headers={"User-Agent": "rexauto"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
    return _cached(os.path.join(CACHE, safe), fetch)


# --------------------------------------------------------------------------
# parse + classificacao
# --------------------------------------------------------------------------
def parse_patches(text):
    """[{name, desc, author, enabled_upstream, writes:[{width,address,value}]}]"""
    out = []
    cur = None
    pending_width = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "[[patch]]":
            cur = {"name": None, "desc": "", "author": "", "enabled_upstream": False, "writes": []}
            out.append(cur)
            pending_width = None
            continue
        m = re.match(r"\[\[patch\.([a-z0-9]+)\]\]", line)
        if m:
            pending_width = m.group(1)
            continue
        if cur is None:
            continue
        m = re.match(r'(\w+)\s*=\s*(.+)', line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "name":
            cur["name"] = v.strip('"')
        elif k == "desc":
            cur["desc"] = v.strip('"')
        elif k == "author":
            cur["author"] = v.strip('"')
        elif k == "is_enabled":
            cur["enabled_upstream"] = v.lower().startswith("true")
        elif k == "address" and pending_width in WIDTHS:
            cur.setdefault("_addr", []).append(int(v, 0))
        elif k == "value" and pending_width in WIDTHS:
            addrs = cur.get("_addr") or []
            if addrs:
                cur["writes"].append({"width": WIDTHS[pending_width],
                                      "address": addrs.pop(0),
                                      "value": int(v, 0)})
    for p in out:
        p.pop("_addr", None)
    return [p for p in out if p["name"]]


def classify(patch, mods):
    """Selo do patch + motivo. O selo e o PIOR entre as escritas.

    Uma escrita e avaliada contra o modulo que a CONTEM, nao contra o modulo
    principal. Com a checagem feita so no principal, uma escrita no .text de um
    modulo companheiro caia em "fora da imagem" e o patch saia selado RUNTIME --
    prometendo um toggle para algo que na verdade vira codigo nativo compilado.
    """
    if not patch["writes"]:
        return "INVALIDO", "patch sem escritas", []
    reasons, touched, seal = [], [], "RUNTIME"
    for w in patch["writes"]:
        a = w["address"]
        m = owning_module(mods, a)
        if m is None:
            return "INVALIDO", "escrita 0x%08X fora de todas as imagens" % a, []
        if m not in touched:
            touched.append(m)
        if m["code_base"] <= a < m["code_end"]:
            seal = "RECOMPILAR"
            reasons.append("0x%08X em .text de %s" % (a, m["name"]))
    why = "; ".join(reasons[:3]) if reasons else "todas as escritas fora de .text"
    return seal, why, touched


# --------------------------------------------------------------------------
# comandos
# --------------------------------------------------------------------------
def variant_fit(fname, mods):
    """Quantas escritas de um arquivo do catalogo caem no .text DESTE port.

    Duas variantes do mesmo title_id (base, TU, demo) trazem os MESMOS patches em
    enderecos diferentes. Escolher a errada nao falha: ela nopeia instrucao valida
    em outro lugar, que e miscompile silencioso. Entao a ambiguidade reporta o
    ajuste medido e o operador escolhe -- adivinhar aqui seria o pior caminho.
    """
    try:
        patches = parse_patches(fetch_patch_file(fname))
    except Exception:
        return None
    n_in = n_out = n_total = 0
    for p in patches:
        for w in p["writes"]:
            n_total += 1
            m = owning_module(mods, w["address"])
            if m and m["code_base"] <= w["address"] < m["code_end"]:
                n_in += 1
            else:
                n_out += 1
    return {"file": fname, "patches": len(patches), "writes": n_total,
            "in_text": n_in, "outside_text": n_out}


def cmd_list(port_dir, only_file=None):
    name, tid, mods = port_identity(port_dir)
    print("port=%s  title_id=%s  %d modulo(s)" % (name, tid, len(mods)))
    for m in mods:
        print("  %-28s .text=0x%08X..0x%08X  imagem=0x%08X..0x%08X%s"
              % (m["name"], m["code_base"], m["code_end"],
                 m["image_base"], m["image_end"], "  <- xex" if m["is_xex"] else ""))
    if not tid:
        raise SystemExit("sem title_id (nao achei ../game/default.xex)")
    files = [only_file] if only_file else files_for_title(tid, port_dir)
    if not files:
        print("\nnenhum patch da comunidade para %s" % tid)
        return
    for fname in files:
        print("\n== %s" % fname)
        for p in parse_patches(fetch_patch_file(fname)):
            seal, why, touched = classify(p, mods)
            mods_txt = ",".join(m["name"] for m in touched)
            print("  [%-10s] %-34s %s" % (seal, p["name"][:34], why))
            if len(mods) > 1 and touched:
                print("               modulo(s): %s" % mods_txt)
            if p["desc"]:
                print("               %s" % p["desc"][:90])
            if p["author"]:
                print("               autor: %s" % p["author"])


def _write_guest_patches(port_dir, mod, entries):
    """Um arquivo de patches por MODULO, porque e por modulo que a imagem e
    parcheada antes da analise -- escrever tudo no manifesto principal faria as
    escritas do companheiro caírem num espaco de enderecos que nao e o delas."""
    out = os.path.join(port_dir, mod["name"] + "_guest_patches.toml")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Gerado por gamepatches.py -- patches da comunidade\n")
        f.write("# (github.com/xenia-canary/game-patches), aplicados na imagem guest\n")
        f.write("# ANTES da analise, entao viram codigo nativo permanente.\n")
        f.write("# modulo: %s\n" % mod["name"])
        n = 0
        for fname, p, seal, writes in entries:
            f.write("\n# [%s] %s\n" % (seal, p["name"]))
            if p["desc"]:
                f.write("#   %s\n" % p["desc"])
            f.write("#   autor: %s | fonte: %s\n" % (p["author"] or "?", fname))
            for w in writes:
                f.write("[[guest_patches]]\naddress = 0x%08X\nvalue = 0x%X\nwidth = %d\n"
                        % (w["address"], w["value"], w["width"]))
                n += 1
    return out, n


def _add_include(man, inc_name):
    txt = open(man, encoding="utf-8", errors="ignore").read()
    inc = '"%s"' % inc_name
    if inc in txt:
        return False
    m = re.search(r'(includes\s*=\s*\[)([^\]]*)\]', txt)
    if not m:
        raise SystemExit("nao achei 'includes' em %s" % man)
    txt = txt[:m.end(2)] + ", " + inc + txt[m.end(2):]
    open(man, "w", encoding="utf-8", newline="\n").write(txt)
    return True


def cmd_apply(port_dir, wanted, only_file=None):
    name, tid, mods = port_identity(port_dir)
    chosen = []
    for fname in ([only_file] if only_file else files_for_title(tid, port_dir)):
        for p in parse_patches(fetch_patch_file(fname)):
            if p["name"] in wanted:
                seal, why, touched = classify(p, mods)
                if seal == "INVALIDO":
                    raise SystemExit("patch '%s' invalido: %s" % (p["name"], why))
                chosen.append((fname, p, seal))
    missing = set(wanted) - {p["name"] for _, p, _ in chosen}
    if missing:
        raise SystemExit("nao achei no catalogo: %s" % ", ".join(sorted(missing)))

    # agrupa as escritas pelo modulo que as contem
    per_mod = {}
    for fname, p, seal in chosen:
        buckets = {}
        for w in p["writes"]:
            m = owning_module(mods, w["address"])
            buckets.setdefault(m["name"], []).append(w)
        for mname, writes in buckets.items():
            per_mod.setdefault(mname, []).append((fname, p, seal, writes))

    total = 0
    for mod in mods:
        entries = per_mod.get(mod["name"])
        if not entries:
            continue
        out, n = _write_guest_patches(port_dir, mod, entries)
        total += n
        added = _add_include(mod["manifest"], os.path.basename(out))
        print("%s: %d escrita(s) -> %s  (manifesto: %s)"
              % (mod["name"], n, os.path.basename(out),
                 "include adicionado" if added else "include ja presente"))
    print("\ntotal: %d patch(es), %d escrita(s) em %d modulo(s)"
          % (len(chosen), total, len(per_mod)))
    if any(seal == "RECOMPILAR" for _, _, seal in chosen):
        print("RECOMPILAR: rode o codegen + build do port para o patch entrar no codigo nativo.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    argv = sys.argv[1:]
    only = None
    if "--file" in argv:
        i = argv.index("--file")
        only = argv[i + 1]
        del argv[i:i + 2]
    if argv[0] == "list":
        cmd_list(argv[1], only)
    elif argv[0] == "apply":
        cmd_apply(argv[1], set(argv[2:]), only)
    else:
        raise SystemExit(__doc__)
