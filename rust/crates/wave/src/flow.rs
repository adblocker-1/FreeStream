//! Ansicht „Audio Flow“: der Signalweg als Graph – Eingänge (Programme und Geräte) → Kanäle → Mixe →
//! Ausgabegeräte. Reines Modell ohne GTK und ohne pactl: [`build`] liest nur einen fertigen [`Snapshot`],
//! [`Graph::path`] sucht zu einem angeklickten Element alles, was davor und dahinter liegt.

use std::collections::BTreeSet;

use crate::control::{ChannelView, Snapshot};

/// Spalte eines Elements.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Col {
    Input,
    Channel,
    Mix,
    Output,
}

/// Unterzeile eines Elements (übersetzt wird erst im Fenster).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Sub {
    None,
    /// Programm
    Software,
    /// Eingangsgerät
    Hardware,
    /// Kanal ohne Eingang
    Empty,
    /// Pegel eines Kanals in Prozent
    Percent(i64),
    /// Lautstärke eines Mixes oder Geräts in Prozent
    Volume(i64),
    Muted,
}

/// Art einer Verbindung (Farbe in der Legende).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Link {
    InputChannel,
    ChannelMix,
    MixOutput,
}

/// Ein Element (Kachel) im Graphen.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Node {
    /// eindeutig: "in:…", "ch:<Kanal>", "mix:<Mix>", "out:<Gerät>"
    pub id: String,
    pub col: Col,
    pub title: String,
    pub sub: Sub,
    /// Symbolname (iconlib) – leer: Standard des Fensters
    pub icon: String,
    /// Farbe der Kachel (Kanäle) – leer: ohne Kachel
    pub color: String,
    /// Programmschlüssel bei Programmen (für das Programmsymbol)
    pub app: String,
}

/// Verbindung zwischen zwei Elementen (Indizes in [`Graph::nodes`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge {
    pub from: usize,
    pub to: usize,
    pub link: Link,
    /// stumm (gestrichelt)
    pub muted: bool,
}

/// Elemente und Verbindungen; innerhalb einer Spalte in Anzeigereihenfolge.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Graph {
    pub nodes: Vec<Node>,
    pub edges: Vec<Edge>,
}

/// Hervorgehobener Weg: Elemente und Verbindungen.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Path {
    pub nodes: BTreeSet<usize>,
    pub edges: BTreeSet<usize>,
}

impl Graph {
    /// Elemente einer Spalte (Indizes, in Reihenfolge).
    pub fn column(&self, col: Col) -> Vec<usize> {
        (0..self.nodes.len()).filter(|&i| self.nodes[i].col == col).collect()
    }

    pub fn find(&self, id: &str) -> Option<usize> {
        self.nodes.iter().position(|n| n.id == id)
    }

    /// Alles, was in das Element `sel` hineinfließt (stromaufwärts) und was es erreicht (stromabwärts).
    /// Andere Eingänge eines Kanals gehören nicht zum Weg eines einzelnen Eingangs.
    pub fn path(&self, sel: usize) -> Path {
        let mut p = Path::default();
        if sel >= self.nodes.len() {
            return p;
        }
        p.nodes.insert(sel);
        for down in [true, false] {
            let mut todo = vec![sel];
            let mut seen = BTreeSet::from([sel]);
            while let Some(n) = todo.pop() {
                for (i, e) in self.edges.iter().enumerate() {
                    let next = match (down, e.from == n, e.to == n) {
                        (true, true, _) => e.to,
                        (false, _, true) => e.from,
                        _ => continue,
                    };
                    p.edges.insert(i);
                    p.nodes.insert(next);
                    if seen.insert(next) {
                        todo.push(next);
                    }
                }
            }
        }
        p
    }
}

/// Kanäle in Anzeigereihenfolge wie im Mixer (Mikrofon zuerst).
fn ordered(s: &Snapshot) -> Vec<&ChannelView> {
    let mut v: Vec<&ChannelView> = s.channels.iter().collect();
    v.sort_by_key(|c| c.channel.id != "mic");
    v
}

/// Graph aus dem Zustand des Mixers bauen (leer, solange der Mixer nicht läuft).
pub fn build(s: &Snapshot) -> Graph {
    let mut g = Graph::default();
    if !s.active {
        return g;
    }
    let chans = ordered(s);
    let node = |id: String, col: Col, title: &str, sub: Sub| Node {
        id, col, title: title.to_string(), sub, icon: String::new(), color: String::new(), app: String::new(),
    };
    // Eingänge
    let mut inputs: Vec<(Node, String)> = Vec::new();
    for c in &chans {
        let ch = &c.channel;
        let hardware = ch.id == "mic" || !ch.is_apps();
        if hardware && !c.input.name.is_empty() {
            let mut n = node(format!("in:dev:{}", ch.id), Col::Input, &c.input.description, Sub::Hardware);
            n.icon = if ch.id == "mic" { "mic" } else { "input" }.into();
            inputs.push((n, ch.id.clone()));
        } else if !hardware && !c.streams.is_empty() {
            for st in &c.streams {
                let mut n = node(format!("in:app:{}", st.index), Col::Input, &st.app, Sub::Software);
                n.app = st.key.clone();
                inputs.push((n, ch.id.clone()));
            }
        } else {
            let mut n = node(format!("in:empty:{}", ch.id), Col::Input, "", Sub::Empty);
            n.icon = "speaker".into();
            inputs.push((n, ch.id.clone()));
        }
    }
    let first_ch = inputs.len();
    for (n, _) in &inputs {
        g.nodes.push(n.clone());
    }
    // Kanäle
    for c in &chans {
        let ch = &c.channel;
        let sub = match c.master {
            Some(l) if l.mute => Sub::Muted,
            Some(l) => Sub::Percent(l.volume),
            None => Sub::None,
        };
        let mut n = node(format!("ch:{}", ch.id), Col::Channel, &ch.name, sub);
        n.icon = ch.icon.clone();
        n.color = ch.color.clone();
        g.nodes.push(n);
    }
    for (i, (_, cid)) in inputs.iter().enumerate() {
        if let Some(j) = chans.iter().position(|c| &c.channel.id == cid) {
            g.edges.push(Edge { from: i, to: first_ch + j, link: Link::InputChannel, muted: false });
        }
    }
    // Mixe
    let first_mix = g.nodes.len();
    for m in &s.mixes {
        let sub = match m.volume {
            Some(l) if l.mute => Sub::Muted,
            Some(l) => Sub::Volume(l.volume),
            None => Sub::None,
        };
        let mut n = node(format!("mix:{}", m.mix.id), Col::Mix, &m.mix.name, sub);
        n.icon = m.mix.icon.clone();
        n.color = m.mix.color.clone();
        g.nodes.push(n);
    }
    for (j, c) in chans.iter().enumerate() {
        let master_mute = c.master.is_some_and(|l| l.mute);
        for (k, m) in s.mixes.iter().enumerate() {
            if !c.mixes.contains(&m.mix.id) {
                continue;
            }
            let muted = master_mute || c.levels.get(&m.mix.id).is_some_and(|l| l.mute);
            g.edges.push(Edge { from: first_ch + j, to: first_mix + k, link: Link::ChannelMix, muted });
        }
    }
    // Ausgänge (jedes Gerät einmal, auch wenn mehrere Mixe darauf gehen)
    let mut outs: Vec<String> = Vec::new();
    for (k, m) in s.mixes.iter().enumerate() {
        let mix_mute = m.volume.is_some_and(|l| l.mute);
        for d in &m.devices {
            let pos = match outs.iter().position(|o| o == d) {
                Some(p) => p,
                None => {
                    let title = s.outputs.iter().find(|o| &o.name == d).map(|o| o.description.clone())
                        .filter(|t| !t.is_empty()).unwrap_or_else(|| d.clone());
                    let sub = match m.device_levels.get(d) {
                        Some(l) if l.mute => Sub::Muted,
                        Some(l) => Sub::Volume(l.volume),
                        None => Sub::None,
                    };
                    let mut n = node(format!("out:{d}"), Col::Output, &title, sub);
                    n.icon = "speaker".into();
                    g.nodes.push(n);
                    outs.push(d.clone());
                    outs.len() - 1
                }
            };
            let to = g.nodes.iter().position(|n| n.id == format!("out:{}", outs[pos])).unwrap_or(0);
            let muted = mix_mute || m.device_levels.get(d).is_some_and(|l| l.mute);
            g.edges.push(Edge { from: first_mix + k, to, link: Link::MixOutput, muted });
        }
    }
    g
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::control::MixView;
    use crate::mixer::{Channel, Device, Level, Mix, Stream};

    fn ch(id: &str, kind: &str, mixes: &[&str]) -> ChannelView {
        ChannelView {
            channel: Channel { id: id.into(), name: id.to_uppercase(), kind: kind.into(), ..Default::default() },
            master: Some(Level { volume: 100, mute: false }),
            mixes: mixes.iter().map(|m| m.to_string()).collect(),
            ..Default::default()
        }
    }

    fn stream(index: &str, app: &str, cid: &str) -> Stream {
        Stream { index: index.into(), app: app.into(), key: app.to_lowercase(), channel: Some(cid.into()), ..Default::default() }
    }

    fn snap() -> Snapshot {
        let mut music = ch("music", "apps", &["monitor", "stream"]);
        music.streams = vec![stream("10", "Spotify", "music"), stream("11", "VLC", "music")];
        music.master = Some(Level { volume: 10, mute: false });
        music.levels.insert("stream".into(), Level { volume: 50, mute: true });
        let system = ch("system", "apps", &["monitor"]);
        let mut mic = ch("mic", "input", &["stream"]);
        mic.input = Device { name: "alsa_input.usb".into(), description: "USB Mic".into() };
        let mut game = ch("game", "apps", &["monitor"]);
        game.master = Some(Level { volume: 45, mute: true });
        let mon = MixView {
            mix: Mix { id: "monitor".into(), name: "Personal Mix".into(), icon: "headphones".into(), ..Default::default() },
            devices: vec!["alsa_output.a".into(), "alsa_output.b".into()],
            volume: Some(Level { volume: 100, mute: false }),
            device_levels: [("alsa_output.a".to_string(), Level { volume: 80, mute: false }),
                            ("alsa_output.b".to_string(), Level { volume: 100, mute: true })].into_iter().collect(),
            ..Default::default()
        };
        let stream_mix = MixView {
            mix: Mix { id: "stream".into(), name: "Stream Mix".into(), virtual_: true, ..Default::default() },
            devices: vec!["alsa_output.a".into()],
            ..Default::default()
        };
        Snapshot {
            active: true,
            channels: vec![system, music, mic, game],
            mixes: vec![mon, stream_mix],
            outputs: vec![Device { name: "alsa_output.a".into(), description: "Speakers".into() }],
            ..Default::default()
        }
    }

    fn ids(g: &Graph, set: &BTreeSet<usize>) -> Vec<String> {
        set.iter().map(|&i| g.nodes[i].id.clone()).collect()
    }

    #[test]
    fn builds_columns() {
        let g = build(&snap());
        let col = |c| g.column(c).into_iter().map(|i| g.nodes[i].id.clone()).collect::<Vec<_>>();
        // Mikrofon zuerst wie im Mixer; leere Kanäle haben einen Platzhalter
        assert_eq!(col(Col::Input), ["in:dev:mic", "in:empty:system", "in:app:10", "in:app:11", "in:empty:game"]);
        assert_eq!(col(Col::Channel), ["ch:mic", "ch:system", "ch:music", "ch:game"]);
        assert_eq!(col(Col::Mix), ["mix:monitor", "mix:stream"]);
        assert_eq!(col(Col::Output), ["out:alsa_output.a", "out:alsa_output.b"]);
        let n = |id: &str| &g.nodes[g.find(id).unwrap()];
        assert_eq!(n("in:dev:mic").sub, Sub::Hardware);
        assert_eq!(n("in:dev:mic").title, "USB Mic");
        assert_eq!(n("in:app:10").sub, Sub::Software);
        assert_eq!(n("in:app:10").app, "spotify");
        assert_eq!(n("in:empty:game").sub, Sub::Empty);
        assert_eq!(n("ch:music").sub, Sub::Percent(10));
        assert_eq!(n("ch:game").sub, Sub::Muted);
        assert_eq!(n("mix:monitor").sub, Sub::Volume(100));
        assert_eq!(n("mix:stream").sub, Sub::None);
        assert_eq!(n("out:alsa_output.a").title, "Speakers");
        assert_eq!(n("out:alsa_output.a").sub, Sub::Volume(80));
        assert_eq!(n("out:alsa_output.b").title, "alsa_output.b");
        assert_eq!(n("out:alsa_output.b").sub, Sub::Muted);
        assert!(build(&Snapshot::default()).nodes.is_empty());
    }

    #[test]
    fn edges_and_mutes() {
        let g = build(&snap());
        let e = |a: &str, b: &str| g.edges.iter().find(|e| g.nodes[e.from].id == a && g.nodes[e.to].id == b).cloned();
        assert_eq!(e("in:app:10", "ch:music").unwrap().link, Link::InputChannel);
        assert!(!e("ch:music", "mix:monitor").unwrap().muted);
        assert!(e("ch:music", "mix:stream").unwrap().muted); // im Stream-Mix stumm
        assert!(e("ch:game", "mix:monitor").unwrap().muted); // Kanal stumm
        assert!(e("ch:system", "mix:stream").is_none()); // nicht im Mix
        assert_eq!(e("mix:monitor", "out:alsa_output.a").unwrap().link, Link::MixOutput);
        assert!(e("mix:monitor", "out:alsa_output.b").unwrap().muted); // Gerät stumm
        assert!(e("mix:stream", "out:alsa_output.a").is_some()); // Gerät nur einmal
        assert_eq!(g.edges.len(), 5 + 5 + 3);
    }

    #[test]
    fn follows_paths() {
        let g = build(&snap());
        // Kanal: seine Eingänge und alles dahinter, aber kein anderer Kanal
        let p = g.path(g.find("ch:music").unwrap());
        assert_eq!(ids(&g, &p.nodes), ["in:app:10", "in:app:11", "ch:music", "mix:monitor", "mix:stream",
                                       "out:alsa_output.a", "out:alsa_output.b"]);
        assert_eq!(p.edges.len(), 2 + 2 + 3);
        // ein Programm: nur sein eigener Eingang
        let p = g.path(g.find("in:app:11").unwrap());
        assert!(!p.nodes.contains(&g.find("in:app:10").unwrap()));
        assert!(p.nodes.contains(&g.find("out:alsa_output.b").unwrap()));
        // Mix: alle Kanäle davor samt Eingängen, nur seine Ausgänge
        let p = g.path(g.find("mix:stream").unwrap());
        assert_eq!(ids(&g, &p.nodes), ["in:dev:mic", "in:app:10", "in:app:11", "ch:mic", "ch:music", "mix:stream",
                                       "out:alsa_output.a"]);
        // Ausgang: alles, was darauf spielt
        let p = g.path(g.find("out:alsa_output.b").unwrap());
        assert!(p.nodes.contains(&g.find("in:empty:game").unwrap()));
        assert!(!p.nodes.contains(&g.find("mix:stream").unwrap()));
        assert_eq!(g.path(999), Path::default());
    }
}
