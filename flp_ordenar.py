"""
Ordena un proyecto de FL Studio (.flp) SIN abrir FL:

  - Junta todos los clips de cada automatizacion en una sola pista propia.
  - Ordena esas pistas por categoria (VOL, FLT, PAN, FX, PIT, OTR) y nombre,
    con el nombre y color de la categoria (tambien colorea el clip).
  - Elimina las pistas que quedan vacias y sube las pistas con clips
    (manteniendo su orden), dejando las automatizaciones debajo.

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
DIVIDER = "-- AUTOMATIZACIONES --"
DIVIDER_COLOR = 0x202020
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
            chans[cur] = {"type": None, "name": "", "lst": lst, "idx": k}
        elif cur is None:
            continue
        elif eid == F.EV_CH_TYPE:
            chans[cur]["type"] = val[0]
        elif eid == EV_CH_TEXTNAME and not chans[cur]["name"]:
            chans[cur]["name"] = F.text(val)
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


# ---------------------------------------------------------------- arreglos
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


def organize_arrangement(lst, pl_idx, blocks, chans, log):
    pl = bytearray(lst[pl_idx][1])
    sz = item_size(pl)
    items = []
    for o in range(0, len(pl), sz):
        base, ii = struct.unpack_from("<HH", pl, o + 4)
        track = MAX_IDX - struct.unpack_from("<H", pl, o + 12)[0]
        is_auto = ii < base and chans.get(ii, {}).get("type") == F.CT_AUTOMATION
        items.append({"o": o, "track": track, "auto": ii if is_auto else None})

    ntracks = len(blocks)
    old = [[copy.deepcopy(e) for e in lst[a:b]] for a, b in blocks]

    keep = sorted({it["track"] for it in items if it["auto"] is None})
    autos = sorted({it["auto"] for it in items if it["auto"] is not None},
                   key=lambda c: (classify(chans[c]["name"]), strip_prefix(chans[c]["name"]).lower()))
    empty = [t for t in range(ntracks) if t not in keep]
    need = len(keep) + (1 + len(autos) if autos else 0)
    if need > ntracks:
        raise ValueError("No caben las pistas (%d > %d)" % (need, ntracks))

    new_blocks, mapping, auto_track = [], {}, {}
    for t in keep:
        mapping[t] = len(new_blocks)
        new_blocks.append(old[t])
    template = old[empty[0]] if empty else old[-1]
    if autos:
        div = copy.deepcopy(template)
        block_set_name(div, DIVIDER)
        new_blocks.append(div)
        div_color = DIVIDER_COLOR
        div[0][1] = div[0][1][:4] + struct.pack("<I", bgr(div_color)) + div[0][1][8:]
        for c in autos:
            label, color, _ = CATEGORIES[classify(chans[c]["name"])]
            b = copy.deepcopy(template)
            block_set_name(b, "[%s] %s" % (label, strip_prefix(chans[c]["name"])))
            b[0][1] = b[0][1][:4] + struct.pack("<I", bgr(color)) + b[0][1][8:]
            auto_track[c] = len(new_blocks)
            new_blocks.append(b)
            set_channel_color(chans[c], color)
    while len(new_blocks) < ntracks:  # rellenar con pistas vacias limpias
        b = copy.deepcopy(template)
        b[:] = [e for e in b if e[0] != F.EV_TRACK_NAME]
        new_blocks.append(b)

    for i, b in enumerate(new_blocks):  # renumerar iid (1-based)
        b[0][1] = struct.pack("<I", i + 1) + b[0][1][4:]

    for it in items:
        nt = auto_track[it["auto"]] if it["auto"] is not None else mapping[it["track"]]
        struct.pack_into("<H", pl, it["o"] + 12, MAX_IDX - nt)

    removed = [t for t in empty if block_name(old[t]) or any(
        it["track"] == t for it in items)]
    log.append("  pistas con clips: %d (subidas arriba, mismo orden)" % len(keep))
    log.append("  automatizaciones juntadas: %d -> %d pistas" % (
        sum(1 for it in items if it["auto"] is not None), len(autos)))
    for c in autos:
        log.append("     pista %3d  %s" % (auto_track[c] + 1, block_name(new_blocks[auto_track[c]])))
    log.append("  pistas eliminadas (quedaban vacias o solo con nombre): %d" % len(removed))

    flat = [e for b in new_blocks for e in b]
    a0, b_last = blocks[0][0], blocks[-1][1]
    lst[a0:b_last] = flat
    lst[pl_idx][1] = bytes(pl)


def ordenar(src, dry=False, dst=None):
    """Ordena el proyecto. Nunca toca el original. -> {"log": [...], "dst": ruta o ""}"""
    with open(src, "rb") as f:
        raw = f.read()
    header, events = F.loads(raw)
    body = F.dump_events(events)
    if header + b"FLdt" + struct.pack("<I", len(body)) + body != raw:
        raise ValueError("Este proyecto no se puede editar de forma segura (formato inesperado)")
    chans = read_channels(events)
    arrs = find_arrangements(events)
    if not arrs:
        raise ValueError("No se encontro ningun arreglo en el proyecto")
    log = []
    for n, (lst, pl_idx, blocks) in enumerate(reversed(arrs)):  # de atras hacia delante
        log.append("Arreglo %d:" % (len(arrs) - n))
        organize_arrangement(lst, pl_idx, blocks, chans, log)
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
