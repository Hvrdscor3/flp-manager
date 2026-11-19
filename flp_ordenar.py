"""
Ordena un proyecto de FL Studio (.flp) SIN abrir FL:

  - Junta todos los clips de cada automatizacion en una sola pista propia.
  - Ordena esas pistas por categoria (VOL, FLT, PAN, FX, PIT, OTR) y nombre,
    con el nombre y color de la categoria (tambien colorea el clip).
  - Quita los clips de patterns vacios (sin notas ni controladores).
  - Una pista por sonido: todos los clips de un mismo audio o pattern van a la misma pista
    (si se superponen, a una pista extra del mismo sonido), con su nombre.
  - Agrupa por tipo, en el orden en que aparecian: patterns arriba, luego audio y al final
    las automatizaciones. Elimina las pistas vacias.

Nunca sobrescribe el original: guarda "<nombre> - ordenado.flp".

Uso:  python flp_ordenar.py "C:\\ruta\\proyecto.flp" [--probar]
      (--probar solo muestra lo que haria, sin guardar)
"""
import copy
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flpraw as F  # noqa: E402

MAX_IDX = 499            # FL guarda la pista como 499 - indice


def max_idx(sz):
    """FL 12 y anteriores (clips de 32 bytes) tienen 199 pistas: la pista se guarda como 198 - indice."""
    return 198 if sz == 32 else MAX_IDX
DIVIDER = "-- AUTOMATIZACIONES --"
DIV_AUDIO = "-- AUDIO --"
DIVIDER_COLOR = 0x202020
EV_PATTERN_NEW, EV_PAT_NOTES, EV_PAT_CTRL, EV_PAT_NAME = 65, 224, 223, 193
EV_SAMPLE = 196
CATEGORIES = [  # (etiqueta, color 0xRRGGBB, palabras clave)
    ("VOL", 0x3FA9F5, ("vol", "gain", "level", "fader", "master", "balance")),
    ("FLT", 0xF5A623, ("cutoff", "filter", "freq", "reso", "eq", "lp", "hp")),
    ("PAN", 0x7ED321, ("pan", "stereo", "width")),
    ("FX",  0xBD10E0, ("reverb", "delay", "wet", "dry", "mix", "send", "dist", "chorus")),
    ("PIT", 0xD0021B, ("pitch", "tune", "detune", "fine", "bend")),
    ("OTR", 0x9B9B9B, ()),
]
EV_CH_TEXTNAME = 203
EV_CH_COLOR = 128


def bgr(rgb):  # FL guarda colores como 0x00BBGGRR
    return ((rgb & 0xFF) << 16) | (rgb & 0xFF00) | ((rgb >> 16) & 0xFF)


def classify(name):
    low = name.lower()
    for i, (_, _, keys) in enumerate(CATEGORIES):
        if any(k in low for k in keys):
            return i
    return len(CATEGORIES) - 1


def strip_prefix(name):
    if name.startswith("[") and "] " in name[:7]:
        return name.split("] ", 1)[1]
    return name


# ---------------------------------------------------------------- canales
def read_channels(events):
    """{iid: {"type", "name", "lst", "idx" (indice del evento New)}}"""
    chans, cur = {}, None
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == F.EV_CH_NEW:
            cur = struct.unpack("<H", val)[0]
            chans[cur] = {"type": None, "name": "", "sample": "", "lst": lst, "idx": k}
        elif cur is None:
            continue
        elif eid == F.EV_CH_TYPE:
            chans[cur]["type"] = val[0]
        elif eid == EV_CH_TEXTNAME and not chans[cur]["name"]:
            chans[cur]["name"] = F.text(val)
        elif eid == EV_SAMPLE and not chans[cur]["sample"]:
            chans[cur]["sample"] = os.path.splitext(os.path.basename(F.text(val).replace("/", "\\")))[0]
        elif eid == F.EV_ARR_NEW:
            cur = None
    return chans


def set_channel_color(ch, rgb):
    lst, k = ch["lst"], ch["idx"] + 1
    while k < len(lst) and lst[k][0] != F.EV_CH_NEW:
        if lst[k][0] == EV_CH_COLOR:
            lst[k][1] = struct.pack("<I", bgr(rgb))
            return
        k += 1


def read_patterns(events):
    """Ids de los patterns que tienen contenido (notas o controladores)."""
    full, cur = set(), None
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == EV_PATTERN_NEW:
            cur = struct.unpack("<H", val[:2])[0]
        elif eid in (F.EV_CH_NEW, F.EV_ARR_NEW):
            cur = None
        elif cur is not None and eid in (EV_PAT_NOTES, EV_PAT_CTRL) and len(val):
            full.add(cur)
    return full


def read_pattern_names(events):
    names, cur = {}, None
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == EV_PATTERN_NEW:
            cur = struct.unpack("<H", val[:2])[0]
        elif eid in (F.EV_CH_NEW, F.EV_ARR_NEW):
            cur = None
        elif cur is not None and eid == EV_PAT_NAME and cur not in names:
            names[cur] = F.text(val)
    return names


def source_name(src, chans, pnames):
    kind, i = src
    if kind == "pat":
        return pnames.get(i) or "Pattern %d" % i
    c = chans.get(i, {})
    return c.get("name") or c.get("sample") or "Audio %d" % i


def classify_items(pl, sz, chans, pats):
    """Clips del playlist: kind = "pat" | "audio" | "auto"; empty = pattern sin contenido."""
    items = []
    for o in range(0, len(pl), sz):
        pos, base, ii, length = struct.unpack_from("<IHHI", pl, o)
        track = max_idx(sz) - struct.unpack_from("<H", pl, o + 12)[0]
        if ii < base:
            auto = chans.get(ii, {}).get("type") == F.CT_AUTOMATION
            items.append({"o": o, "track": track, "kind": "auto" if auto else "audio", "src": ("ch", ii),
                          "auto": ii if auto else None, "empty": False, "pos": pos, "len": length})
        else:
            items.append({"o": o, "track": track, "kind": "pat", "auto": None, "src": ("pat", ii - base),
                          "empty": (ii - base) not in pats, "pos": pos, "len": length})
    return items


# ---------------------------------------------------------------- arreglos
def lanes(clips):
    """Reparte los clips de un sonido en el minimo de pistas sin que se superpongan.
    -> [[clip, ...], ...] (la primera pista recibe todo lo que cabe)."""
    out, ends = [], []
    for it in sorted(clips, key=lambda x: (x["pos"], x["track"])):
        for i, e in enumerate(ends):
            if it["pos"] >= e:
                out[i].append(it)
                ends[i] = it["pos"] + it["len"]
                break
        else:
            out.append([it])
            ends.append(it["pos"] + it["len"])
    return out


def find_arrangements(events):
    """Devuelve [(lista, idx_playlist, [bloques de pista])]; bloque = (ini, fin)."""
    out = []
    for lst, k in list(F.walk(events)):
        if lst[k][0] != F.EV_PLAYLIST:
            continue
        starts, j = [], k + 1
        while j < len(lst) and lst[j][0] != F.EV_ARR_NEW:
            if lst[j][0] == F.EV_TRACK_DATA:
                starts.append(j)
            elif starts and j - starts[-1] > 4:
                break
            j += 1
        if not starts:
            continue
        followers = {lst[x][0] for a, b in zip(starts, starts[1:]) for x in range(a + 1, b)}
        end = starts[-1] + 1
        while end < len(lst) and lst[end][0] in followers:
            end += 1
        blocks = [(a, b) for a, b in zip(starts, starts[1:] + [end])]
        out.append((lst, k, blocks))
    return out


def item_size(pl):
    for sz in (88, 80, 60, 32):     # 80: FL 25.2.5+
        if len(pl) % sz == 0 and all(
                struct.unpack_from("<H", pl, o + 4)[0] == 20480 for o in range(0, len(pl), sz)):
            return sz
    raise ValueError("Formato de playlist desconocido (%d bytes)" % len(pl))


def block_set_name(block, name):
    for ev in block:
        if ev[0] == F.EV_TRACK_NAME:
            ev[1] = F.enc_text(name)
            return
    block.append([F.EV_TRACK_NAME, F.enc_text(name)])


def block_name(block):
    for ev in block:
        if ev[0] == F.EV_TRACK_NAME:
            return F.text(ev[1])
    return ""


def organize_arrangement(lst, pl_idx, blocks, chans, log, pats=None, pnames=None):
    pl = bytes(lst[pl_idx][1])
    sz = item_size(pl)
    pnames = pnames or {}
    items = classify_items(pl, sz, chans, pats if pats is not None else set())
    n_empty = sum(it["empty"] for it in items)
    items = [it for it in items if not it["empty"]]          # clips de patterns vacios: fuera

    ntracks = len(blocks)
    if any(not 0 <= it["track"] < ntracks for it in items):
        raise ValueError("Proyecto de una version antigua de FL: abrelo y guardalo con tu FL actual para poder ordenarlo"
                         if sz == 32 else "Hay clips en pistas que no existen en el archivo (formato inesperado)")
    old = [[copy.deepcopy(e) for e in lst[a:b]] for a, b in blocks]
    old_names = [block_name(b) for b in old]

    # sonidos (audio o pattern) en el orden en que aparecian: pista original y luego posicion
    by_src, srcs_on_track = {}, {}
    for it in items:
        by_src.setdefault(it["src"], []).append(it)
        srcs_on_track.setdefault(it["track"], set()).add(it["src"])
    first = {k: min((x["track"], x["pos"]) for x in v) for k, v in by_src.items()}
    pat_srcs = sorted((k for k, v in by_src.items() if v[0]["kind"] == "pat"), key=first.get)
    aud_srcs = sorted((k for k, v in by_src.items() if v[0]["kind"] == "audio"), key=first.get)
    autos = sorted((k for k, v in by_src.items() if v[0]["kind"] == "auto"),
                   key=lambda k: (classify(chans[k[1]]["name"]), strip_prefix(chans[k[1]]["name"]).lower()))
    plan = {k: lanes(by_src[k]) for k in by_src}
    n_pat = sum(len(plan[k]) for k in pat_srcs)
    n_aud = sum(len(plan[k]) for k in aud_srcs)
    n_aut = sum(len(plan[k]) for k in autos)
    audio_div = bool(pat_srcs and aud_srcs)
    need = n_pat + n_aud + n_aut + audio_div + (1 if autos else 0)
    if need > ntracks:
        raise ValueError("Proyecto de una version antigua de FL: abrelo y guardalo con tu FL actual para poder ordenarlo"
                         if sz == 32 else "No caben las pistas (%d > %d)" % (need, ntracks))

    used_tracks = set(srcs_on_track)
    empty = [t for t in range(ntracks) if t not in used_tracks]
    template = old[empty[0]] if empty else old[-1]
    new_blocks, dest = [], {}            # dest: offset del clip -> pista nueva

    def add_block(src_block, name, color=None):
        b = copy.deepcopy(src_block)
        if name is not None:
            block_set_name(b, name)
        if color is not None:
            b[0][1] = b[0][1][:4] + struct.pack("<I", bgr(color)) + b[0][1][8:]
        new_blocks.append(b)
        return len(new_blocks) - 1

    def place(src, name_for):
        t0 = first[src][0]
        # si la pista original era solo de este sonido y tenia nombre, se respeta
        base_name = old_names[t0] if old_names[t0].strip() and srcs_on_track[t0] == {src} else name_for(src)
        for n, lane in enumerate(plan[src]):
            i = add_block(old[t0], base_name if n == 0 else "%s (%d)" % (base_name, n + 1))
            for it in lane:
                dest[it["o"]] = i

    for src in pat_srcs:
        place(src, lambda k: source_name(k, chans, pnames))
    if audio_div:
        add_block(template, DIV_AUDIO, DIVIDER_COLOR)
    for src in aud_srcs:
        place(src, lambda k: source_name(k, chans, pnames))
    if autos:
        add_block(template, DIVIDER, DIVIDER_COLOR)
        for src in autos:
            c = src[1]
            label, color, _ = CATEGORIES[classify(chans[c]["name"])]
            nm = "[%s] %s" % (label, strip_prefix(chans[c]["name"]))
            for n, lane in enumerate(plan[src]):
                i = add_block(template, nm if n == 0 else "%s (%d)" % (nm, n + 1), color)
                for it in lane:
                    dest[it["o"]] = i
            set_channel_color(chans[c], color)
    while len(new_blocks) < ntracks:  # rellenar con pistas vacias limpias
        b = copy.deepcopy(template)
        b[:] = [e for e in b if e[0] != F.EV_TRACK_NAME]
        new_blocks.append(b)

    for i, b in enumerate(new_blocks):  # renumerar iid (1-based)
        b[0][1] = struct.pack("<I", i + 1) + b[0][1][4:]

    out = bytearray()
    for it in items:
        rec = bytearray(pl[it["o"]:it["o"] + sz])
        struct.pack_into("<H", rec, 12, max_idx(sz) - dest[it["o"]])
        out += rec

    joined = sum(1 for k in pat_srcs + aud_srcs if len({x["track"] for x in by_src[k]}) > len(plan[k]))
    last_used = max(used_tracks) if used_tracks else -1
    removed = sum(1 for t in range(last_used + 1) if t not in used_tracks)
    log.append("  patterns: %d en %d pista(s) · audios: %d en %d pista(s) (una pista por sonido)" % (
        len(pat_srcs), n_pat, len(aud_srcs), n_aud))
    if joined:
        log.append("  sonidos que estaban repartidos en varias pistas y se juntaron: %d" % joined)
    if n_empty:
        log.append("  clips vacios quitados (patterns sin notas): %d" % n_empty)
    log.append("  automatizaciones juntadas: %d -> %d pistas" % (
        sum(1 for it in items if it["auto"] is not None), n_aut))
    for i, b in enumerate(new_blocks):
        if block_name(b).startswith("["):
            log.append("     pista %3d  %s" % (i + 1, block_name(b)))
    log.append("  pistas vacias eliminadas: %d" % removed)

    flat = [e for b in new_blocks for e in b]
    a0, b_last = blocks[0][0], blocks[-1][1]
    lst[a0:b_last] = flat
    lst[pl_idx][1] = bytes(out)


def _arr_stats(lst, pl_idx, blocks, chans, pats=None):
    pl = lst[pl_idx][1]
    sz = item_size(pl)
    by_track, srcs = {}, {}
    empty_clips = 0
    for it in classify_items(pl, sz, chans, pats if pats is not None else set()):
        if it["empty"]:
            empty_clips += 1
            continue
        if it["auto"] is None:
            srcs.setdefault(it["src"], []).append(it)
        d = by_track.setdefault(it["track"], {"auto": set(), "other": 0, "kinds": set(), "srcs": set()})
        if it["auto"] is not None:
            d["auto"].add(it["auto"])
        else:
            d["other"] += 1
            d["kinds"].add(it["kind"])
            d["srcs"].add(it["src"])
    used = sorted(by_track)
    names = {t: block_name(lst[a:b]) for t, (a, b) in enumerate(blocks)}
    auto_ch = {c for d in by_track.values() for c in d["auto"]}
    last = used[-1] if used else -1
    gaps = sum(1 for t in range(last + 1) if t not in by_track and not names.get(t, "").startswith("-- "))  # separadores no cuentan
    unnamed = [t for t in used if not names.get(t, "").strip()]
    mixed = [t for t in used if by_track[t]["auto"] and by_track[t]["other"]]
    # automatizaciones repartidas: un mismo canal de automatizacion en varias pistas
    spread = sum(1 for c in auto_ch if sum(1 for d in by_track.values() if c in d["auto"]) > 1)
    # automatizaciones intercaladas entre pistas de audio/patterns
    first_other_after = 0
    seen_auto = False
    for t in used:
        if by_track[t]["auto"] and not by_track[t]["other"]:
            seen_auto = True
        elif seen_auto and by_track[t]["other"]:
            first_other_after += 1
    # audio intercalado entre pistas de patterns
    seen_audio = audio_between = 0
    for t in used:
        k = by_track[t]["kinds"]
        if k == {"audio"}:
            seen_audio = 1
        elif seen_audio and "pat" in k:
            audio_between += 1
    return {"used": len(used), "last": last + 1, "gaps": gaps, "unnamed": len(unnamed), "mixed": len(mixed),
            "autos": len(auto_ch), "spread": spread, "interleaved": first_other_after,
            "emptyClips": empty_clips, "audioMixed": audio_between,
            "srcSpread": sum(1 for v in srcs.values() if len({x["track"] for x in v}) > len(lanes(v))),
            "multiSrc": sum(1 for d in by_track.values() if len(d["srcs"]) > 1)}


def analizar(src):
    """Revisa el orden de las pistas SIN modificar nada. -> {"score", "level", "issues", "stats"}"""
    with open(src, "rb") as f:
        raw = f.read()
    header, events = F.loads(raw)
    chans = read_channels(events)
    pats = read_patterns(events)
    arrs = find_arrangements(events)
    if not arrs:
        raise ValueError("No se encontro ningun arreglo en el proyecto")
    st = None
    for lst, k, blocks in arrs:      # el arreglo con mas pistas usadas es el principal
        try:
            s = _arr_stats(lst, k, blocks, chans, pats)
        except ValueError:
            continue
        if st is None or s["used"] > st["used"]:
            st = s
    if st is None:
        raise ValueError("Formato de playlist desconocido")
    used = max(1, st["used"])
    small = st["used"] <= 4          # bocetos cortos: se juzgan con menos rigor
    issues, pts = [], 0
    if st["gaps"] > 1:
        pts += min(25, st["gaps"] / max(1, st["last"]) * 40)
        issues.append("%d pista(s) vacía(s) entre las que tienen clips" % st["gaps"])
    if st["unnamed"] and not small:
        pts += min(20, st["unnamed"] / used * 20)
        issues.append("%d de %d pistas con clips no tienen nombre" % (st["unnamed"], st["used"]))
    if st["mixed"]:
        pts += min(20, st["mixed"] * 6)
        issues.append("%d pista(s) mezclan automatizaciones con audio o patterns" % st["mixed"])
    if st["spread"]:
        pts += min(15, st["spread"] * 3)
        issues.append("%d automatización(es) repartidas en varias pistas" % st["spread"])
    if st["interleaved"]:
        pts += min(15, st["interleaved"] * 3)
        issues.append("Automatizaciones intercaladas entre pistas de audio/patterns")
    if st["srcSpread"]:
        pts += min(20, st["srcSpread"] * 2)
        issues.append("%d sonido(s) repartidos en varias pistas (podrían ir en una sola)" % st["srcSpread"])
    if st["multiSrc"] and not small:
        pts += min(15, st["multiSrc"] * 2)
        issues.append("%d pista(s) mezclan sonidos distintos" % st["multiSrc"])
    if st["emptyClips"]:
        pts += min(10, st["emptyClips"])
        issues.append("%d clip(s) vacío(s) en el Playlist (patterns sin notas)" % st["emptyClips"])
    if st["audioMixed"]:
        pts += min(10, st["audioMixed"] * 2)
        issues.append("Pistas de audio intercaladas entre pistas de patterns")
    if small:
        pts /= 2
    score = max(0, round(100 - pts))
    level = "ordenado" if score >= 80 else "regular" if score >= 55 else "desordenado"
    return {"score": score, "level": level, "issues": issues, "stats": st}


def ordenar(src, dry=False, dst=None):
    """Ordena el proyecto. Nunca toca el original. -> {"log": [...], "dst": ruta o ""}"""
    with open(src, "rb") as f:
        raw = f.read()
    header, events = F.loads(raw)
    body = F.dump_events(events)
    if header + b"FLdt" + struct.pack("<I", len(body)) + body != raw:
        raise ValueError("Este proyecto no se puede editar de forma segura (formato inesperado)")
    chans = read_channels(events)
    pats = read_patterns(events)
    pnames = read_pattern_names(events)
    arrs = find_arrangements(events)
    if not arrs:
        raise ValueError("No se encontro ningun arreglo en el proyecto")
    log = []
    for n, (lst, pl_idx, blocks) in enumerate(reversed(arrs)):  # de atras hacia delante
        log.append("Arreglo %d:" % (len(arrs) - n))
        organize_arrangement(lst, pl_idx, blocks, chans, log, pats, pnames)
    if dry:
        return {"log": log, "dst": ""}
    if not dst:
        root, ext = os.path.splitext(src)
        dst, n = root + " - ordenado" + ext, 2
        while os.path.exists(dst):
            dst, n = "%s - ordenado (%d)%s" % (root, n, ext), n + 1
    tmp = dst + ".tmp"
    F.save(tmp, header, events)
    F.load(tmp)  # verificacion: el archivo resultante se puede leer
    os.replace(tmp, dst)
    return {"log": log, "dst": dst}


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    dry = "--probar" in argv
    try:
        r = ordenar(argv[1], dry)
    except ValueError as e:
        print(e)
        return 1
    print("\n".join(r["log"]))
    print("\n(--probar: no se guardo nada)" if dry else "\nGuardado: " + r["dst"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
