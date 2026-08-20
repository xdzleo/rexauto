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


def port_identity(port_dir):
    """(name, title_id, code_base, code_size, image_base) do port."""
    name = None
    for f in os.listdir(port_dir):
        if f.endswith("_manifest.toml"):
            name = f[: -len("_manifest.toml")]
            break
    if not name:
        raise SystemExit("nao achei <nome>_manifest.toml em %s" % port_dir)

    init_h = os.path.join(port_dir, "generated", "default", name + "_init.h")
    if not os.path.exists(init_h):
        raise SystemExit("sem generated/default/%s_init.h -- rode o codegen antes" % name)
    txt = open(init_h, encoding="utf-8", errors="ignore").read()

    def g(key):
        m = re.search(key + r"\s+0x([0-9A-Fa-f]+)", txt)
        return int(m.group(1), 16) if m else None

    xex = None
    for cand in (os.path.join(port_dir, "..", "game", "default.xex"),):
        cand = os.path.normpath(cand)
        if os.path.exists(cand):
            xex = cand
            break
    tid = None
    if xex:
        with open(xex, "rb") as f:
            tid = _xex_title_id(f.read(0x10000))
    return name, tid, g("REX_CODE_BASE"), g("REX_CODE_SIZE"), g("REX_IMAGE_BASE")


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


def classify(patch, code_base, code_size, image_base, image_end):
    """Selo do patch + motivo. O selo e o PIOR entre as escritas."""
    code_end = code_base + code_size
    reasons = []
    seal = "RUNTIME"
    for w in patch["writes"]:
        a = w["address"]
        if not (image_base <= a < image_end):
            return "INVALIDO", "escrita 0x%08X fora da imagem" % a
        if code_base <= a < code_end:
            seal = "RECOMPILAR"
            reasons.append("0x%08X em .text" % a)
    if not patch["writes"]:
        return "INVALIDO", "patch sem escritas"
    return seal, ("; ".join(reasons[:3]) if reasons else "todas as escritas fora de .text")


# --------------------------------------------------------------------------
# comandos
# --------------------------------------------------------------------------
def cmd_list(port_dir):
    name, tid, cb, cs, ib = port_identity(port_dir)
    print("port=%s  title_id=%s  .text=0x%08X..0x%08X" % (name, tid, cb, cb + cs))
    if not tid:
        raise SystemExit("sem title_id (nao achei ../game/default.xex)")
    files = files_for_title(tid, port_dir)
    if not files:
        print("nenhum patch da comunidade para %s" % tid)
        return
    img = os.path.join(os.path.dirname(port_dir.rstrip("\\/")), name + "_image.bin")
    image_end = ib + (os.path.getsize(img) if os.path.exists(img) else 0x10000000)
    for fname in files:
        print("\n== %s" % fname)
        for p in parse_patches(fetch_patch_file(fname)):
            seal, why = classify(p, cb, cs, ib, image_end)
            print("  [%-10s] %-34s %s" % (seal, p["name"][:34], why))
            if p["desc"]:
                print("               %s" % p["desc"][:90])
            if p["author"]:
                print("               autor: %s" % p["author"])


def cmd_apply(port_dir, wanted):
    name, tid, cb, cs, ib = port_identity(port_dir)
    img = os.path.join(os.path.dirname(port_dir.rstrip("\\/")), name + "_image.bin")
    image_end = ib + (os.path.getsize(img) if os.path.exists(img) else 0x10000000)
    chosen = []
    for fname in files_for_title(tid, port_dir):
        for p in parse_patches(fetch_patch_file(fname)):
            if p["name"] in wanted:
                seal, why = classify(p, cb, cs, ib, image_end)
                if seal == "INVALIDO":
                    raise SystemExit("patch '%s' invalido: %s" % (p["name"], why))
                chosen.append((fname, p, seal))
    missing = set(wanted) - {p["name"] for _, p, _ in chosen}
    if missing:
        raise SystemExit("nao achei no catalogo: %s" % ", ".join(sorted(missing)))

    out = os.path.join(port_dir, name + "_guest_patches.toml")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Gerado por gamepatches.py -- patches da comunidade\n")
        f.write("# (github.com/xenia-canary/game-patches), aplicados na imagem guest\n")
        f.write("# ANTES da analise, entao viram codigo nativo permanente.\n")
        for fname, p, seal in chosen:
            f.write("\n# [%s] %s\n" % (seal, p["name"]))
            if p["desc"]:
                f.write("#   %s\n" % p["desc"])
            f.write("#   autor: %s | fonte: %s\n" % (p["author"] or "?", fname))
            for w in p["writes"]:
                f.write("[[guest_patches]]\naddress = 0x%08X\nvalue = 0x%X\nwidth = %d\n"
                        % (w["address"], w["value"], w["width"]))
    print("escrito: %s (%d patch(es), %d escrita(s))"
          % (out, len(chosen), sum(len(p["writes"]) for _, p, _ in chosen)))

    man = os.path.join(port_dir, name + "_manifest.toml")
    txt = open(man, encoding="utf-8", errors="ignore").read()
    inc = '"%s_guest_patches.toml"' % name
    if inc not in txt:
        m = re.search(r'(includes\s*=\s*\[)([^\]]*)\]', txt)
        if not m:
            raise SystemExit("nao achei 'includes' no manifesto")
        txt = txt[:m.end(2)] + ", " + inc + txt[m.end(2):]
        open(man, "w", encoding="utf-8", newline="\n").write(txt)
        print("manifesto: include adicionado")
    else:
        print("manifesto: include ja presente")
    print("\nRECOMPILAR: rode o codegen + build do port para o patch entrar no codigo nativo.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    if sys.argv[1] == "list":
        cmd_list(sys.argv[2])
    elif sys.argv[1] == "apply":
        cmd_apply(sys.argv[2], set(sys.argv[3:]))
    else:
        raise SystemExit(__doc__)
