# name=Auto Organizer
# url=
"""
Auto Organizer - script de FL Studio para ordenar automatizaciones.

Que hace:
  1. Busca todos los clips de automatizacion (canales tipo Automation Clip).
  2. Los clasifica por categoria segun su nombre (Volumen, Filtro, Pan, FX, Pitch, Otros).
  3. Los colorea por categoria (el color se ve en el Playlist y en el Channel Rack).
  4. Les pone un prefijo opcional: "[VOL] Master Volume".
  5. Crea un bloque de pistas en el Playlist (desde AUTO_TRACK_START), una por clip,
     con el mismo nombre y color, ordenadas por categoria y nombre.
     -> Solo tienes que soltar cada clip en su pista correspondiente.

Como usarlo:
  - Options > MIDI Settings > elige un puerto de entrada y en "Controller type"
    selecciona "Auto Organizer".
  - Desde el puerto MIDI:  nota C5 (60) = organizar todo
                           nota C#5 (61) = solo colorear
                           nota D5 (62) = limpiar las pistas AUTO
  - O en View > Script output, pestana "Auto Organizer", escribe: organizar()
"""

import channels
import playlist
import midi
import ui
import general

# ------------------------------------------------------------------ CONFIG
AUTO_TRACK_START = 400      # primera pista del Playlist reservada para automatizaciones
ADD_PREFIX = True           # anadir "[VOL] ", "[FLT] "... al nombre del clip
RUN_ON_INIT = False         # organizar automaticamente al cargar el script
NOTE_ORGANIZE, NOTE_COLOR, NOTE_CLEAR = 60, 61, 62
LANE_COLOR_DIVIDER = 0x202020

# (etiqueta, color 0xRRGGBB, palabras clave) - el orden define el orden en el Playlist
CATEGORIES = [
    ("VOL", 0x3FA9F5, ("vol", "gain", "level", "fader", "master")),
    ("FLT", 0xF5A623, ("cutoff", "filter", "freq", "reso", "q ", "eq", "lp", "hp")),
    ("PAN", 0x7ED321, ("pan", "stereo", "width")),
    ("FX",  0xBD10E0, ("reverb", "delay", "wet", "dry", "mix", "send", "dist", "chorus")),
    ("PIT", 0xD0021B, ("pitch", "tune", "detune", "fine")),
    ("OTR", 0x9B9B9B, ()),
]
# -------------------------------------------------------------------------

_PREFIXES = tuple("[%s] " % c[0] for c in CATEGORIES)


def _ch(fn, *args):
    """Llama funciones de channels con indice global (con fallback a APIs viejas)."""
    try:
        return fn(*args, True)
    except TypeError:
        return fn(*args)


def _strip_prefix(name):
    for p in _PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def _classify(name):
    low = " " + _strip_prefix(name).lower() + " "
    for idx, (_, _, keys) in enumerate(CATEGORIES):
        if any(k in low for k in keys):
            return idx
    return len(CATEGORIES) - 1


def _auto_clips():
    try:
        total = channels.channelCount(True)
    except TypeError:
        total = channels.channelCount()
    clips = []
    for i in range(total):
        if _ch(channels.getChannelType, i) == midi.CT_AutoClip:
            name = _ch(channels.getChannelName, i)
            clips.append((i, name, _classify(name)))
    clips.sort(key=lambda c: (c[2], _strip_prefix(c[1]).lower()))
    return clips


def _undo(label):
    try:
        general.saveUndo(label, midi.UF_PL | midi.UF_KNOB)
    except Exception:
        pass


def _hint(msg):
    print(msg)
    try:
        ui.setHintMsg(msg)
    except Exception:
        pass


def colorear():
    clips = _auto_clips()
    _undo("Auto Organizer: colorear")
    for idx, name, cat in clips:
        label, color, _ = CATEGORIES[cat]
        _ch(channels.setChannelColor, idx, color)
        clean = _strip_prefix(name)
        new = ("[%s] %s" % (label, clean)) if ADD_PREFIX else clean
        if new != name:
            _ch(channels.setChannelName, idx, new)
    _hint("Auto Organizer: %d automatizaciones coloreadas" % len(clips))
    return clips


def limpiar():
    _undo("Auto Organizer: limpiar pistas")
    last = playlist.trackCount()
    for t in range(AUTO_TRACK_START, last + 1):
        if playlist.getTrackName(t).startswith(("AUTO", "--")) or t == AUTO_TRACK_START:
            playlist.setTrackName(t, "")
            playlist.setTrackColor(t, 0x565148)
    _hint("Auto Organizer: pistas AUTO limpiadas")


def organizar():
    clips = colorear()
    if not clips:
        _hint("Auto Organizer: no hay clips de automatizacion en el proyecto")
        return
    _undo("Auto Organizer: ordenar playlist")
    last = playlist.trackCount()
    t = AUTO_TRACK_START
    playlist.setTrackName(t, "-- AUTOMATIZACIONES --")
    playlist.setTrackColor(t, LANE_COLOR_DIVIDER)
    t += 1
    prev_cat = None
    for idx, _, cat in clips:
        if t > last:
            _hint("Auto Organizer: no caben todas, baja AUTO_TRACK_START")
            break
        label, color, _ = CATEGORIES[cat]
        if prev_cat is not None and cat != prev_cat and t < last:
            playlist.setTrackName(t, "-- %s --" % label)
            playlist.setTrackColor(t, LANE_COLOR_DIVIDER)
            t += 1
        prev_cat = cat
        playlist.setTrackName(t, "AUTO " + _strip_prefix(_ch(channels.getChannelName, idx)))
        playlist.setTrackColor(t, color)
        t += 1
    _hint("Auto Organizer: %d automatizaciones ordenadas desde la pista %d"
          % (len(clips), AUTO_TRACK_START))


# ------------------------------------------------------------ eventos de FL
def OnInit():
    print("Auto Organizer cargado. Escribe organizar(), colorear() o limpiar().")
    if RUN_ON_INIT:
        organizar()


def OnNoteOn(event):
    if event.data2 == 0:
        return
    action = {NOTE_ORGANIZE: organizar, NOTE_COLOR: colorear, NOTE_CLEAR: limpiar}.get(event.data1)
    if action:
        action()
        event.handled = True
