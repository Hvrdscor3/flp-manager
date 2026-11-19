"""Prescindibles: que plugins de terceros sobran o conviene convertir para mandar el proyecto.

Solo lee el .flp. Para cada plugin externo decide:
  borrar     -> no suena (canal sin notas en el playlist, insert sin senal, medidores)
  directwave -> instrumento de pocas notas: se puede samplear con DirectWave
  nativo     -> efecto con equivalente de FL (se puede cambiar sin renderizar)
  render     -> lo demas: renderizar a audio (stem / consolidar)
  gratis     -> plugin gratuito: quien reciba el proyecto lo puede instalar

Formato (FL 20+):
  canal: 64 nuevo, 21 tipo (2 = plugin), 201 nombre interno ("Fruity Wrapper" = externo),
         203 nombre, 22 insert del mixer, 213 datos del plugin (registros: 54 nombre, 55 ruta...)
  notas: evento 224 dentro de un pattern (65), registros de 24 bytes:
         pos u32, flags u16, canal u16, largo u32, nota u16 ...
  mixer: 236 empieza un insert; 212 del slot (u32 insert, u32 slot); 213 datos; 235 ruteo
         (un byte por insert destino); 204 nombre del insert (va antes de su 236)
"""
import os
import struct

import flpraw as F
import flp_ordenar as O
from plugins import _records, _txt

EV_INSERT, EV_ROUTE, EV_INS_NAME, EV_CH_MIX = 236, 235, 204, 22
EV_PLUG_NAME, EV_PLUG_INFO, EV_PLUG_DATA = 201, 212, 213
WRAPPER = "fruity wrapper"

METERS = ("span", "loudness", "youlean", "minimeter", "insight", "tonal balance", "levels", "metric ab",
          "analyzer", "analyser", "oscilloscope", "correlometer", "spectrum", "vu meter", "vumt", "s(m)exoscope",
          "voxengo span", "expose", "reference")
FREE = ("ott", "valhalla supermassive", "supermassive", "vital", "tdr nova", "spitfire labs", "labs", "surge",
        "dexed", "khs ", "kilohearts essentials", "camelcrusher", "tal-noisemaker", "tal noisemaker", "tyrell",
        "decent sampler", "ozone imager", "youlean", "span", "frontier", "classic compressor", "kotelnikov ge",
        "fresh air", "tal-chorus", "tal-reverb", "dragonfly", "vladg", "molot", "smartelectronix", "ambience")
NATIVE = [  # (palabras clave, equivalente de FL)
    (("pro-q", " eq", "eq ", "equaliz", "puigtec", "pultec", "eqp", "kirchhoff", "nova"), "Fruity Parametric EQ 2"),
    (("clip", "kclip", "gclip", "clipper"), "Fruity Soft Clipper"),
    (("pro-l", "limit", "ozone", "l1", "l2", "l3", "elevate"), "Fruity Limiter / Maximus"),
    (("pro-mb", "multiband", "ott", "multipressor"), "Multiband Compressor / Maximus"),
    (("comp", "cla-2a", "cla-76", "1176", "la-2a", "ssl", "pro-c", "glue", "bus"), "Fruity Limiter (modo compresor)"),
    (("reverb", "valhalla", "pro-r", "verb", "room", "plate", "hall"), "Fruity Reeverb 2 / Fruity Convolver"),
    (("delay", "echo", "h-delay", "timeless", "echoboy", "comeback"), "Fruity Delay 3"),
    (("satur", "decapitator", "saturn", "trash", "distort", "tube", "tape", "fuzz", "crush"),
     "Fruity Blood Overdrive / Distructor"),
    (("chorus", "flanger", "phaser", "dimension", "doubler"), "Fruity Chorus / Flanger / Phaser"),
    (("deess", "de-ess", "sibil"), "Maximus (banda alta) / Fruity Limiter"),
    (("gate", "transient", "kickstart", "duck", "lfotool", "volumeshaper", "shaper", "trance"),
     "Fruity Peak Controller / Volume shaper (Patcher)"),
    (("width", "imager", "haas", "pancake", "autopan", " pan"), "Fruity Stereo Shaper / Stereo Enhancer"),
    (("pitch", "tune", "autotune", "melodyne"), "Pitcher / NewTone"),
    (("filter", "volcano", "pro-r", "sweep"), "Fruity Filter / Fruity Love Philter"),
]


def _has(name, words):
    n = " " + name.lower() + " "
    return any(w in n for w in words)


def _plugin(val):
    rec = _records(val)
    if not rec:
        return None
    path = _txt(rec.get(55, b""))
    fmt = "VST3" if path.lower().endswith(".vst3") or (52 in rec and 51 not in rec) else \
        "CLAP" if path.lower().endswith(".clap") else "VST2"
    name = _txt(rec[54])
    return {"plugin": name, "shell": name.lower().startswith("waveshell"), "vendor": _txt(rec.get(56, b"")), "path": path, "fmt": fmt}


def _read(events):
    chans, inserts, notes = {}, {}, {}
    cur = pat = None
    ins, ins_name, slot_ins = -1, "", None
    in_mixer = False
    for lst, k in F.walk(events):
        eid, val = lst[k]
        if eid == F.EV_CH_NEW:
            cur = struct.unpack("<H", val)[0]
            chans[cur] = {"type": None, "name": "", "internal": "", "mix": 0, "plug": None}
            in_mixer = False
            continue
        if eid == O.EV_PATTERN_NEW:
            pat = struct.unpack("<H", val[:2])[0]
            cur = None
        elif eid == O.EV_PAT_NOTES and pat is not None:
            for o in range(0, len(val) - 23, 24):
                _, _, ch, ln, key = struct.unpack_from("<IHHIH", val, o)
                notes.setdefault(ch, []).append((pat, key, ln))
        elif eid == EV_INS_NAME:
            ins_name = F.text(val)
        elif eid == EV_INSERT:
            in_mixer, cur = True, None
            ins += 1
            inserts[ins] = {"name": ins_name, "route": [], "slots": []}
            ins_name = ""
        elif in_mixer and ins >= 0:
            if eid == EV_PLUG_NAME:
                inserts[ins]["slots"].append({"internal": F.text(val), "plug": None, "slot": None})
            elif eid == EV_PLUG_INFO and len(val) >= 8 and inserts[ins]["slots"]:
                slot_ins, sl = struct.unpack_from("<II", val, 0)
                inserts[ins]["slots"][-1]["slot"] = sl
            elif eid == EV_PLUG_DATA and inserts[ins]["slots"]:
                inserts[ins]["slots"][-1]["plug"] = _plugin(val)
            elif eid == 203 and inserts[ins]["slots"] and not inserts[ins]["slots"][-1].get("label"):
                inserts[ins]["slots"][-1]["label"] = F.text(val)
            elif eid == EV_ROUTE:
                inserts[ins]["route"] = [j for j, b in enumerate(val) if b]
        elif cur is not None:
            c = chans[cur]
            if eid == F.EV_CH_TYPE:
                c["type"] = val[0]
            elif eid == EV_PLUG_NAME and not c["internal"]:
                c["internal"] = F.text(val)
            elif eid == 203 and not c["name"]:
                c["name"] = F.text(val)
            elif eid == EV_CH_MIX:
                c["mix"] = val[0]
            elif eid in (EV_PLUG_INFO, EV_PLUG_DATA) and c["plug"] is None:
                c["plug"] = _plugin(val)
            elif eid == F.EV_ARR_NEW:
                cur = None
    return chans, inserts, notes


def _placed(events, chans):
    """Patterns y canales que aparecen en algun arreglo del playlist."""
    pats, chs = set(), set()
    for lst, k, _ in O.find_arrangements(events):
        pl = lst[k][1]
        if not pl:
            continue
        try:
            sz = O.item_size(pl)
        except ValueError:
            continue
        for it in O.classify_items(pl, sz, chans, set()):
            (pats if it["src"][0] == "pat" else chs).add(it["src"][1])
    return pats, chs


def _fx_advice(name):
    for words, nat in NATIVE:
        if _has(name, words):
            return nat
    return ""


def analizar(path):
    with open(path, "rb") as f:
        raw = f.read()
    _, events = F.loads(raw)
    chans, inserts, notes = _read(events)
    placed_pats, placed_chs = _placed(events, chans)
    autos = [c["name"].lower() for c in chans.values() if c["type"] == F.CT_AUTOMATION]

    # canales que suenan
    sounding = set()
    for i, c in chans.items():
        if c["type"] == F.CT_AUTOMATION:
            continue
        ns = [n for n in notes.get(i, []) if n[0] in placed_pats]
        if ns or i in placed_chs:
            sounding.add(i)

    # inserts con senal: los que reciben de un canal que suena, y lo que sigue por el ruteo
    live, todo = {0}, [chans[i]["mix"] for i in sounding]
    while todo:
        x = todo.pop()
        if x in live or x not in inserts:
            live.add(x)
            continue
        live.add(x)
        todo += inserts[x]["route"]

    items = []
    for i, c in chans.items():
        p = c["plug"]
        if c["type"] == F.CT_AUTOMATION or not p or c["internal"].lower() != WRAPPER:
            continue
        label = c["name"] or p["plugin"]
        if p["shell"]:
            p = dict(p, plugin=label, vendor=p["vendor"] or "Waves")
        ns = [n for n in notes.get(i, []) if n[0] in placed_pats]
        automated = any(a.startswith(label.lower() + " - ") for a in autos)
        keys = sorted({n[1] for n in ns})
        base = {"kind": "inst", "label": label, **p, "where": "Canal · Mixer %d" % c["mix"],
                "notes": len(ns), "keys": len(keys), "automated": automated}
        if not ns:
            allns = notes.get(i, [])
            why = ("Tiene notas pero en patterns que no están en el playlist" if allns
                   else "Canal sin notas: no suena")
            items.append(dict(base, action="borrar", why=why))
        elif len(keys) <= 3 and not automated:
            items.append(dict(base, action="directwave",
                              why="%d nota(s) distinta(s): se puede samplear en DirectWave y queda tocable" % len(keys)))
        else:
            why = ("%d notas distintas, con automatizaciones: renderízalo a audio" % len(keys) if automated else
                   "%d notas distintas: renderízalo, o sampléalo en DirectWave si no usa LFO ni glide" % len(keys))
            items.append(dict(base, action="render", why=why))

    for x, ins in inserts.items():
        for s in ins["slots"]:
            p = s["plug"]
            if not p or s["internal"].lower() != WRAPPER:
                continue
            label = s.get("label") or p["plugin"]
            if p["shell"]:
                p = dict(p, plugin=label, vendor=p["vendor"] or "Waves")
            where = "Master" if x == 0 else "Insert %d%s" % (x, " · " + ins["name"] if ins["name"] else "")
            base = {"kind": "fx", "label": label, **p, "where": where, "slot": (s["slot"] or 0) + 1}
            if _has(p["plugin"], METERS) or _has(label, METERS):
                items.append(dict(base, action="borrar", why="Medidor/analizador: no cambia el sonido"))
            elif x not in live:
                items.append(dict(base, action="borrar", why="Insert sin señal: ningún canal que suene llega aquí"))
            else:
                nat = _fx_advice(p["plugin"] + " " + label)
                if nat:
                    items.append(dict(base, action="nativo", why="Equivalente de FL: " + nat, native=nat))
                else:
                    items.append(dict(base, action="render", why="Renderiza el insert (stem) para mandarlo"))

    for it in items:
        it["free"] = _has(it["plugin"], FREE)
        if it["free"] and it["action"] in ("render", "nativo"):
            it["action"], it["why"] = "gratis", "Plugin gratuito: quien lo reciba lo puede instalar"
    order = {"borrar": 0, "directwave": 1, "nativo": 2, "render": 3, "gratis": 4}
    items.sort(key=lambda x: (order[x["action"]], x["kind"], x["label"].lower()))
    count = {k: sum(1 for x in items if x["action"] == k) for k in order}
    return {"items": items, "count": count, "total": len(items),
            "name": os.path.splitext(os.path.basename(path))[0]}


if __name__ == "__main__":
    import json
    import sys
    r = analizar(sys.argv[1])
    print(json.dumps(r["count"], ensure_ascii=False))
    for x in r["items"]:
        print("%-10s %-4s %-28s %-30s %s" % (x["action"], x["kind"], x["label"][:28], x["where"][:30], x["why"]))
