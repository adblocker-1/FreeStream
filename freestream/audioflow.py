"""Audio Flow: der Signalweg von FreeWave als Graph (ohne Tk, damit er sich testen lässt).

Vier Spalten wie in der Ansicht „Audio Flow“ von Wave Link:

    Eingänge  →  Kanäle  →  Mixe  →  Ausgänge

Eingänge sind Programme, die gerade spielen (Software), sowie Mikrofon und eigene Eingangs-Kanäle
(Hardware). Hat ein Kanal keinen Eingang, steht dort eine leere Zeile („Empty“), damit jede Kanalzeile
links einen Partner hat. Kanten: Eingang → Kanal, Kanal → Mix (wenn der Kanal im Mix ist) und
Mix → Ausgabegerät. Stumm geschaltete Wege sind als „muted“ markiert (gestrichelt gezeichnet).

build(st) baut den Graphen aus dem Zustand, den das FreeWave-Fenster ohnehin einliest (WaveWindow.collect);
path(graph, node) liefert alle Knoten und Kanten, die mit einem angeklickten Element verbunden sind
(stromaufwärts und stromabwärts); layout(graph, width) ordnet die Karten in Spalten an.
"""
from .i18n import _

COLUMNS = ("input", "channel", "mix", "output")
EDGE_KIND = {"input": "input", "channel": "channel", "mix": "mix"}  # Spalte der Quelle -> Kantenart


def _mic_title(st):
    mic = st.get("mic") or {}
    src = mic.get("source") or ""
    names = st.get("source_names") or {}
    if mic.get("hardware"):
        return names.get(src) or "Elgato Wave:3"
    if src:
        return names.get(src) or src
    return ""


def _mix_sub(lv, direct):
    if lv is None:
        return _("Direct to output") if direct else ""
    vol, mute = lv
    return _("Muted") if mute else _("Volume {value} %").format(value=vol)


def build(st):
    """Graph aus dem Fensterzustand: {"nodes": {id: Knoten}, "columns": {Spalte: [ids]}, "edges": [Kante]}.

    Knoten: {"id", "col", "kind", "title", "sub", "muted", "ref"}; kind ist bei Eingängen "stream",
    "hardware" oder "empty", sonst "channel", "mix", "output" bzw. "virtual" (Mix ohne Gerät, z. B. OBS).
    Kante: {"src", "dst", "kind" ("input"|"channel"|"mix"), "muted"}."""
    nodes, columns, edges = {}, {c: [] for c in COLUMNS}, []

    def node(nid, col, kind, title, sub="", muted=False, ref=None, **extra):
        if nid not in nodes:
            nodes[nid] = dict(id=nid, col=col, kind=kind, title=title, sub=sub, muted=bool(muted), ref=ref, **extra)
            columns[col].append(nid)
        return nid

    def edge(src, dst, muted=False):
        edges.append({"src": src, "dst": dst, "kind": EDGE_KIND[nodes[src]["col"]], "muted": bool(muted)})

    channels = st.get("channels") or []
    mixes = st.get("mixes") or []
    streams = st.get("streams") or []
    levels = st.get("levels") or {}
    masters = st.get("masters") or {}
    sinks = st.get("sink_levels") or {}
    names = st.get("source_names") or {}
    inputs = st.get("inputs") or {}
    outputs = dict(st.get("outputs") or [])
    known = {c["id"] for c in channels}

    # Kanäle mit ihren Eingängen
    for c in channels:
        cid = c["id"]
        vol, mute = masters.get(cid, (None, False))
        sub = _("Muted") if mute else (f"{vol} %" if vol is not None else "")
        ch = node("ch:" + cid, "channel", "channel", c.get("name") or cid, sub, mute, ref=c)
        ins = []
        if cid == "mic":
            title = _mic_title(st)
            if title:
                ins.append(node("in:mic", "input", "hardware", title, _("Hardware")))
        elif c.get("kind", "apps") == "input":
            src = c.get("source") or ""
            if src:
                ins.append(node("in:src:" + src, "input", "hardware", inputs.get(src) or names.get(src) or src,
                                _("Hardware")))
        else:
            for s in streams:
                if s.get("channel") == cid:
                    ins.append(node(f"in:stream:{s['index']}", "input", "stream", s.get("app") or "?",
                                    _("Software"), app=s.get("key") or "", ref=s))
        if not ins:
            ins.append(node("in:empty:" + cid, "input", "empty", _("Empty")))
        for i in ins:
            edge(i, ch)
    # Programme, die in keinem Kanal spielen: ohne Weiter (zur Übersicht)
    for s in streams:
        if not s.get("channel") or s["channel"] not in known:
            node(f"in:stream:{s['index']}", "input", "stream", s.get("app") or "?", _("Not in mixer"),
                 app=s.get("key") or "", ref=s)

    # Mixe und Ausgänge
    for m in mixes:
        mid = m["id"]
        devices = list(m.get("devices_eff") or [])
        direct = not m.get("virtual") and len(devices) == 1
        lv = sinks.get(st.get("mix_sinks", {}).get(mid, "")) if not direct else None
        mix_muted = bool(lv and lv[1])
        mx = node("mix:" + mid, "mix", "mix", m.get("name") or mid, _mix_sub(lv, direct), mix_muted, ref=m)
        for c in channels:
            cid = c["id"]
            if cid in (m.get("exclude") or []):
                continue
            lvl = levels.get(f"{cid}_{mid}")
            muted = bool(masters.get(cid, (0, False))[1]) or bool(lvl and lvl[1])
            edge("ch:" + cid, mx, muted)
        for dev in devices:
            olv = sinks.get(dev)
            out_muted = bool(olv and olv[1])
            sub = _("Muted") if out_muted else (_("Volume {value} %").format(value=olv[0]) if olv else "")
            o = node("out:" + dev, "output", "output", outputs.get(dev) or dev, sub, out_muted, ref=dev)
            edge(mx, o, mix_muted or out_muted)
        if m.get("virtual"):
            o = node("out:virtual:" + mid, "output", "virtual", "FreeWave " + (m.get("name") or mid),
                     _("Virtual output") + (" · OBS" if mid == "stream" else ""), ref=mid)
            edge(mx, o, mix_muted)
    return {"nodes": nodes, "columns": columns, "edges": edges}


def path(graph, nid):
    """(Knoten, Kanten-Indizes), die mit nid verbunden sind: alles stromaufwärts und stromabwärts.

    Wie in Wave Link: Von einem Kanal aus leuchten seine Eingänge, seine Mixe und deren Ausgänge –
    nicht aber andere Kanäle derselben Mixe (die Suche läuft je Richtung nur in diese Richtung)."""
    if nid not in graph["nodes"]:
        return set(), set()
    edges = graph["edges"]
    seen, used = {nid}, set()
    for forward in (True, False):
        todo = [nid]
        done = {nid}
        while todo:
            cur = todo.pop()
            for i, e in enumerate(edges):
                a, b = (e["src"], e["dst"]) if forward else (e["dst"], e["src"])
                if a == cur:
                    used.add(i)
                    seen.add(b)
                    if b not in done:
                        done.add(b)
                        todo.append(b)
    return seen, used


def layout(graph, width, top=48, row_h=52, gap=11, pad=0, min_col=160, max_col=232, min_gap=72):
    """Lage der Karten: {"boxes": {id: (x, y, w, h)}, "cols": {Spalte: (x, w)}, "height": Gesamthöhe}.

    Die Spalten teilen sich die Breite; jede Spalte stapelt ihre Karten von oben."""
    width = max(width, 4 * min_col + 3 * min_gap + 2 * pad)
    col_w = max(min_col, min(max_col, (width - 2 * pad - 3 * min_gap) / 4))
    spacing = (width - 2 * pad - 4 * col_w) / 3
    cols, boxes, bottom = {}, {}, top
    for k, col in enumerate(COLUMNS):
        x = pad + k * (col_w + spacing)
        cols[col] = (x, col_w)
        for r, nid in enumerate(graph["columns"][col]):
            y = top + r * (row_h + gap)
            boxes[nid] = (x, y, col_w, row_h)
            bottom = max(bottom, y + row_h)
    return {"boxes": boxes, "cols": cols, "height": bottom, "width": width}


def curve(a, b):
    """Kubische Bézierkurve von Punkt a (rechter Rand der Quelle) nach b (linker Rand des Ziels):
    die vier Kontrollpunkte für Canvas.create_line(..., smooth="raw")."""
    (x0, y0), (x1, y1) = a, b
    dx = max(20.0, (x1 - x0) * 0.5)
    return [x0, y0, x0 + dx, y0, x1 - dx, y1, x1, y1]


def signature(graph):
    """Kurzer Vergleichswert: neu zeichnen nur, wenn sich am Graphen etwas geändert hat."""
    return (tuple((n["id"], n["title"], n["sub"], n["muted"]) for n in graph["nodes"].values()),
            tuple((e["src"], e["dst"], e["muted"]) for e in graph["edges"]))
