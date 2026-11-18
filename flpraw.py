"""Lector/escritor minimo de eventos .flp (sin dependencias).

FL 2026 escribe el evento 172 con 3 bytes (comprobado en proyectos reales);
las versiones anteriores usan 4. Se prueba primero lo que dicta la version y
si falla se intenta la otra variante.
"""
import struct

BYTE, WORD, DWORD, TEXT, DATA = 0, 64, 128, 192, 208
EV_CONTAINER = 254
EV_FLVERSION, EV_CH_NEW, EV_CH_TYPE, EV_CH_NAME = 199, 64, 21, 231
EV_ARR_NEW, EV_ARR_NAME, EV_PLAYLIST = 99, 241, 233
EV_TRACK_DATA, EV_TRACK_NAME = 238, 239
CT_AUTOMATION = 5
FIXED_SIZES = {172: 3}
NUL = bytes(1)


def _read_varint(b, i):
    n = shift = 0
    while True:
        x = b[i]; i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7


def _varint(n):
    out = bytearray()
    while True:
        x = n & 0x7F; n >>= 7
        out.append(x | (0x80 if n else 0))
        if not n:
            return bytes(out)


def parse_events(raw, i=0, end=None, sizes=None):
    end = len(raw) if end is None else end
    sizes = FIXED_SIZES if sizes is None else sizes
    events = []
    while i < end:
        eid = raw[i]; i += 1
        if eid in sizes: size = sizes[eid]
        elif eid < WORD: size = 1
        elif eid < DWORD: size = 2
        elif eid < TEXT: size = 4
        else: size, i = _read_varint(raw, i)
        if i + size > end:
            raise ValueError("evento truncado")
        val = raw[i:i + size]
        if eid == EV_CONTAINER:
            try:
                val = parse_events(val, sizes=sizes)
            except (ValueError, IndexError):
                pass
        events.append([eid, val])
        i += size
    return events


def dump_events(events):
    body = bytearray()
    for eid, val in events:
        if isinstance(val, list):
            val = dump_events(val)
        body.append(eid)
        if eid >= TEXT:
            body += _varint(len(val))
        body += val
    return bytes(body)


def fl_version(raw):
    """Version de FL leida del primer evento (sin parsear todo)."""
    hlen = struct.unpack_from("<I", raw, 4)[0]
    i = 8 + hlen + 8
    if i < len(raw) and raw[i] == EV_FLVERSION:
        n, j = _read_varint(raw, i + 1)
        return raw[j:j + n].split(NUL)[0].decode("ascii", "replace")
    return ""


def loads(raw):
    if raw[:4] != b"FLhd":
        raise ValueError("No es un proyecto FL Studio (.flp)")
    hlen = struct.unpack_from("<I", raw, 4)[0]
    header = raw[:8 + hlen]
    i = 8 + hlen
    if raw[i:i + 4] != b"FLdt":
        raise ValueError("Bloque de datos no encontrado")
    ver = fl_version(raw)
    major = ver.split(".")[0]
    new = major.isdigit() and int(major) >= 25
    order = [FIXED_SIZES, {}] if new else [{}, FIXED_SIZES]
    err = None
    for sizes in order:
        try:
            return header, parse_events(raw, i + 8, sizes=sizes)
        except (ValueError, IndexError) as e:
            err = e
    raise ValueError("Proyecto danado o formato desconocido: %s" % err)


def load(path):
    with open(path, "rb") as f:
        return loads(f.read())


def save(path, header, events):
    body = dump_events(events)
    with open(path, "wb") as f:
        f.write(header + b"FLdt" + struct.pack("<I", len(body)) + body)


def walk(events):
    """Recorre todos los eventos (incluidos los anidados) en orden: (lista, indice)."""
    for k, (eid, val) in enumerate(events):
        if isinstance(val, list):
            yield from walk(val)
        else:
            yield events, k


def text(val):
    return val.decode("utf-16-le", "replace").rstrip("\0")


def enc_text(s):
    return (s + "\0").encode("utf-16-le")
