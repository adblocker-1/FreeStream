# Elgato Stream Deck for Linux (LinDeck)

Linux-Ersatz für die Elgato-Stream-Deck-Software. LinDeck steuert alle
Stream-Deck-Modelle per USB und liest die Profile der Elgato-App **direkt und
unverändert** ein – Tasten, Icons, Titel, Ordner, Seiten, Multi-Actions, Dials
und Touchstreifen. Ein Neu-Einrichten unter Linux ist nicht nötig.

Installiert wird eine einzige `.deb`-Datei. Danach läuft alles automatisch:
Gerätezugriff, virtuelle Tastatur, Autostart und der Import deiner Profile.

> **Status:** Version 0.1 – getestet mit simulierten Geräten aller Modelle und
> den Elgato-Standardprofilen. Tests auf echter Hardware stehen noch aus.

---

## Inhalt

- [Unterstützte Geräte](#unterstützte-geräte)
- [Installation](#installation)
- [Profile übernehmen](#profile-übernehmen)
- [Unterstützte Aktionen](#unterstützte-aktionen)
- [Befehle](#befehle)
- [Konfiguration](#konfiguration)
- [Fehlersuche](#fehlersuche)
- [Entwicklung](#entwicklung)
- [Aufbau des Projekts](#aufbau-des-projekts)
- [Roadmap](#roadmap)
- [Rechtliches](#rechtliches)

---

## Unterstützte Geräte

| Gerät | Tasten | Besonderheiten |
|---|---|---|
| Stream Deck Original / V2 / MK.2 (inkl. Scissor-Keys, Modul 15) | 15 | |
| Stream Deck Mini / Mini MK.2 (inkl. Discord-Edition, Modul 6) | 6 | |
| Stream Deck XL / XL V2 (inkl. Modul 32) | 32 | |
| Stream Deck Neo | 8 | Infobar (Uhr, Seitenanzeige), 2 Touch-Tasten zum Blättern |
| Stream Deck + | 8 | 4 Dials, Touchstreifen (Tippen, Halten, Wischen) |
| Stream Deck + XL | 36 | 6 Dials, Touchstreifen |
| Stream Deck Studio | 32 | 2 Dials |
| Stream Deck Pedal | 3 Pedale | |

Mehrere Geräte gleichzeitig sowie An- und Abstecken im laufenden Betrieb werden
unterstützt. Profile werden dem passenden Gerät automatisch zugeordnet
(Modellcode, sonst Tastenlayout).

## Installation

**Voraussetzung:** Debian-basierte Distribution mit Python ≥ 3.10
(Debian 12/13, Ubuntu 22.04+, Linux Mint 21+, Pop!_OS, Zorin …), X11 oder Wayland.

1. Die aktuelle `lindeck_<version>_all.deb` unter
   [Releases](../../releases) herunterladen.
2. Doppelklick → **Installieren**, oder im Terminal:

   ```bash
   sudo apt install ./lindeck_0.1.0_all.deb
   ```

Das Paket erledigt automatisch:

- alle Abhängigkeiten über `apt` (die Stream-Deck-Bibliothek ist bereits enthalten)
- udev-Regel für den Gerätezugriff ohne root (`/lib/udev/rules.d/70-lindeck.rules`)
- `uinput`-Modul für Hotkeys unter X11 **und** Wayland
- Autostart bei jeder Anmeldung und sofortiger Start nach der Installation
- Menüeintrag „LinDeck (Stream Deck)“

Falls LinDeck direkt nach der Installation nicht anläuft: einmal ab- und wieder anmelden.

**Deinstallieren:** `sudo apt remove lindeck` – deine Profile unter
`~/.local/share/lindeck` bleiben erhalten.

## Profile übernehmen

### Automatisch (empfohlen)

LinDeck durchsucht alle 15 Sekunden **Downloads**, **Desktop** und
**eingesteckte USB-Sticks** und übernimmt selbstständig:

| Gefunden | Aktion |
|---|---|
| `*.streamDeckProfile` | Profil wird importiert |
| `*.streamDeckProfilesBackup` | alle Profile aus dem Elgato-Backup werden importiert |
| Ordner `ProfilesV2` / `ProfilesV3` (auch als ZIP) | alle Profile werden importiert |
| `Stream_Deck_*.msi` (Elgato-Installer) | Standard-Icons werden übernommen |

Nach jedem Import erscheint eine Desktop-Benachrichtigung. Bereits importierte
Dateien werden kein zweites Mal verarbeitet.

**So kommen alle Profile von Windows rüber:**

1. Unter Windows den Ordner `%APPDATA%\Elgato\StreamDeck\ProfilesV3`
   (ältere Versionen: `ProfilesV2`) auf einen USB-Stick kopieren.
2. Stick unter Linux einstecken – fertig.

Optional zusätzlich den Elgato-Installer (`Stream_Deck_*.msi`) in den
Download-Ordner legen, damit Tasten ohne eigenes Icon die Original-Symbole zeigen.

### Unterstützte Formate

- Profilformat **v1, v2 und v3** (Stream-Deck-App 4.x bis 7.x)
- Einzelne Exporte, komplette Backups und kopierte Profilordner
- Eingebettete Ressourcen (z. B. Soundboard-Dateien aus v3-Exporten)
- Auf **macOS** erstellte Profile (Cmd wird zu Strg umgesetzt)

## Unterstützte Aktionen

Alle eingebauten Aktionen der Elgato-App sind nachgebaut:

| Elgato-Aktion | Umsetzung unter Linux |
|---|---|
| **Hotkey**, **Hotkey Switch** | virtuelle Tastatur über `uinput`; Tastaturlayout (DE/US) wird berücksichtigt; auf Dials: Links-/Rechtsdrehung, Drücken, Tippen |
| **Text** | Einfügen über die Zwischenablage (inkl. Emojis), Tippmodus, „Enter senden“ |
| **Multimedia** | Medientasten bzw. `playerctl`; auf Dials: Systemlautstärke mit Anzeige |
| **Open**, **Open Application**, **Close** | Dateien, Ordner, URLs und Programme; Windows-Programme werden automatisch auf Linux-Gegenstücke abgebildet (Chrome, Firefox, Discord, Spotify, Steam, OBS, VS Code, Teams …); langes Drücken = Programm beenden |
| **Website** | Standardbrowser oder gewählter Browser |
| **Multi Action**, **Multi Action Switch**, **Random Action**, **Delay** | wie im Original |
| **Ordner**, **Übergeordneter Ordner** | beliebig verschachtelt |
| **Seiten** (weiter, zurück, gehe zu, Seitenanzeige) | inkl. Wischen auf dem Touchstreifen und Neo-Touch-Tasten |
| **Profil wechseln** | gezielt oder reihum |
| **Helligkeit**, **Sleep** | Tasten und Dials; erster Tastendruck weckt nur auf; optional automatisches Schlafen |
| **Timer** | Start/Pause, langes Drücken setzt zurück, Alarm |
| **Soundboard** (Play Audio, Stop Audio) | Überlappen, Stopp, Neustart, Schleife, Lautstärke |
| **Key Logic** | Tippen, Doppeltippen, Halten |
| **Action Wheel**, **Dial Stack**, **Action Trigger**, **Mouse Wheel** | Dial-Aktionen von Stream Deck + / + XL / Studio |
| **Digital Time**, **Pagination** | Neo-Infobar |
| **Volume Controller** (Ein-/Ausgabegeräte, App-Lautstärke) | PipeWire/PulseAudio |
| **Smart Profiles** | automatischer Profilwechsel nach aktivem Fenster (X11, KDE mit `kdotool`, Hyprland, Sway) |

Animierte GIFs, SVG-Icons, Titel mit Schriftart, -größe, -farbe, Ausrichtung,
Unterstreichung und Kontur werden wie in der Elgato-App dargestellt.

**Plugins von Drittanbietern** (z. B. Discord, Spotify, OBS, Wave Link) werden
mit ihrem gespeicherten Icon angezeigt. Beim Drücken erscheint ein Warnsymbol,
bis das jeweilige Plugin für Linux umgesetzt ist (siehe [Roadmap](#roadmap)).
`lindeck report` zeigt, welche Plugins in deinen Profilen verwendet werden.

## Befehle

Für den normalen Betrieb ist kein Befehl nötig. Für Sonderfälle:

| Befehl | Zweck |
|---|---|
| `lindeck run` | Dienst starten (läuft normalerweise automatisch) |
| `lindeck import <Datei/Ordner>` | Profile manuell importieren |
| `lindeck profiles` | importierte Profile anzeigen |
| `lindeck devices` | angeschlossene Geräte mit Seriennummer und Firmware |
| `lindeck assign <Seriennr.> <Profil>` | Profil fest einem Gerät zuordnen |
| `lindeck report [Profil]` | Kompatibilitätsbericht: Plugins und fehlende Programmzuordnungen |
| `lindeck preview <Profil> -o <Ordner> [--all]` | Seiten als PNG rendern, ohne Gerät |
| `lindeck extract-assets <Stream_Deck.msi>` | Standard-Icons manuell übernehmen |
| `lindeck remove <Profil>` | Profil löschen |
| `lindeck reload` | laufenden Dienst neu laden |
| `lindeck -v run` | mit ausführlichem Log starten |

Profile und Konfiguration werden bei Änderungen automatisch neu geladen.

## Konfiguration

Optional: `~/.config/lindeck/config.json`

```json
{
  "brightness": 70,
  "sleep_after_minutes": 0,
  "keyboard_layout": "auto",
  "path_map": { "C:/Users/Felix/Music": "/home/felix/Musik" },
  "app_map":  { "obs64.exe": "flatpak run com.obsproject.Studio", "Spotify": "spotify" },
  "smart_profiles": { "obs": "<Profil-ID>" },
  "soundboard_output": "",
  "long_press_ms": 500,
  "double_press_ms": 300,
  "devices": { "<Seriennummer>": { "profile": "<Profil-ID>" } }
}
```

| Schlüssel | Bedeutung |
|---|---|
| `brightness` | Standardhelligkeit in Prozent |
| `sleep_after_minutes` | Gerät nach X Minuten ohne Eingabe abschalten (`0` = nie) |
| `keyboard_layout` | Layout, mit dem die Profile unter Windows erstellt wurden: `auto`, `de`, `us` |
| `path_map` | Windows-Pfade auf Linux-Pfade umbiegen (Präfix-Ersetzung) |
| `app_map` | Windows-Programme oder App-Namen auf Linux-Befehle abbilden |
| `smart_profiles` | Fensterklasse → Profil-ID, zusätzlich zur Automatik |
| `soundboard_output` | PipeWire-Ausgabe für das Soundboard (leer = Standard) |
| `long_press_ms`, `double_press_ms` | Zeitschwellen für Halten und Doppeltippen |
| `devices` | feste Profilzuordnung je Gerät |

**Dateien:**

| Pfad | Inhalt |
|---|---|
| `~/.local/share/lindeck/profiles/` | importierte Profile (Elgato-Format) |
| `~/.local/share/lindeck/resources/` | eingebettete Ressourcen (z. B. Sounds) |
| `~/.local/share/lindeck/elgato-assets/` | Standard-Icons aus dem Elgato-Installer |
| `~/.local/state/lindeck/state.json` | zuletzt aktives Profil, Seite, Helligkeit je Gerät |
| `~/.local/state/lindeck/lindeck.log` | Log |

## Fehlersuche

| Problem | Lösung |
|---|---|
| Gerät wird nicht erkannt | `lindeck devices` ausführen; Gerät ab- und wieder anstecken; nach der Installation einmal neu anmelden |
| Display zeigt „Profil fehlt“ | Es gibt noch kein Profil für dieses Modell – Profil in Downloads legen oder USB-Stick einstecken |
| Hotkeys funktionieren nicht | `ls -l /dev/uinput` prüfen; neu anmelden; im Log nach „uinput“ suchen |
| Programm startet nicht | `lindeck report` zeigt fehlende Zuordnungen → in `app_map` eintragen |
| Falsche Zeichen bei Hotkeys | `keyboard_layout` auf das Windows-Layout setzen (`de` oder `us`) |
| Taste zeigt Warnsymbol | Aktion eines Drittanbieter-Plugins, noch nicht umgesetzt |

Log ansehen: `tail -f ~/.local/state/lindeck/lindeck.log`

## Entwicklung

### Aus dem Quellcode starten

```bash
sudo apt install python3-pil python3-evdev python3-cairosvg libhidapi-libusb0
pip install --user "streamdeck>=0.10"
python3 -m lindeck -v run
```

### `.deb` bauen

```bash
pip install --user "streamdeck>=0.10"
./packaging/build-deb.sh            # Ergebnis: dist/lindeck_<version>_all.deb
```

Das Script bündelt die Bibliothek
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
unter `/opt/lindeck/vendor`, damit das Paket ohne pip auskommt.

**Automatischer Build:** Bei jedem Tag `v*` baut die GitHub-Action
[`build-deb.yml`](.github/workflows/build-deb.yml) das Paket und hängt es an ein
Release an. Ein manueller Build ist über *Actions → Build .deb → Run workflow* möglich.

```bash
git tag v0.1.0 && git push origin v0.1.0
```

### Tests

Die Tests laufen mit simulierten Geräten aller Modelle (Dummy-Transport der
Stream-Deck-Bibliothek) und benötigen Elgato-Profile als Testdaten, z. B. die
Standardprofile aus dem Elgato-Installer:

```bash
msiextract -C /tmp/sd Stream_Deck_*.msi
LINDECK_TEST_PROFILES=/tmp/sd/PFiles64/Elgato/StreamDeck/DefaultProfiles \
  python3 -m pytest -q tests
```

Ohne `LINDECK_TEST_PROFILES` werden die Tests übersprungen.

## Aufbau des Projekts

```
lindeck/
├── __main__.py     Kommandozeile
├── daemon.py       Dienst: Geräteerkennung, Hotplug, Neuladen, Smart Profiles
├── engine.py       Steuerung eines Geräts: Seiten, Ordner, Events, Rendering
├── actions.py      Nachbau aller eingebauten Elgato-Aktionen
├── profiles.py     Elgato-Profilformat v1–v3 lesen und importieren
├── render.py       Tasten, Touchstreifen und Neo-Infobar zeichnen
├── keyboard.py     virtuelle Tastatur/Maus (uinput), Windows-/Qt-Keycodes, Layouts
├── system.py       Programme, Zwischenablage, Audio, Fenster
├── autoimport.py   automatischer Import aus Downloads/Desktop/USB
├── assets.py       Standard-Icons aus dem Elgato-Installer extrahieren
├── models.py       Gerätecodes, USB-IDs, Tastenlayouts
├── plugins.py      Schnittstelle für Plugin-Umsetzungen
├── config.py       Konfiguration und Laufzeitzustand
└── paths.py        XDG-Verzeichnisse
packaging/
└── build-deb.sh    baut das Debian-Paket
tests/
└── test_engine.py  Tests mit simulierten Geräten
```

**Ablauf:** `daemon` erkennt Geräte → je Gerät ein `DeckController` (`engine`)
→ lädt Profilseite aus `profiles` → erzeugt pro Taste/Dial eine Aktion aus
`actions` → `render` zeichnet die Bilder → Tastendrücke werden über eine
Ereigniswarteschlange an die Aktionen weitergereicht.

## Roadmap

- [ ] Grafischer Editor zum Bearbeiten von Profilen direkt unter Linux
- [ ] Plugin-Host für das Elgato-Plugin-SDK (WebSocket-Protokoll)
- [ ] Linux-Umsetzung häufiger Plugins (OBS, Discord, Spotify, Wave Link)
- [ ] Wave-Link-Ersatz auf Basis von PipeWire (virtuelle Kanäle, Monitor-/Stream-Mix)
- [ ] Mikrofon-Einstellungen der Wave-Mikrofone (Low Cut, Clipguard, LED)
- [ ] Tests auf echter Hardware aller Modelle

## Rechtliches

LinDeck ist ein unabhängiges Projekt und steht in keiner Verbindung zu Elgato
oder Corsair. „Elgato“ und „Stream Deck“ sind Marken der Corsair Memory, Inc.

LinDeck enthält **keinen** Code und **keine** Grafiken von Elgato. Die
Standard-Icons werden ausschließlich lokal aus dem Elgato-Installer des Nutzers
übernommen. Das Profilformat wurde zur Herstellung der Interoperabilität
analysiert.

Mitgeliefert wird die Bibliothek
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
(MIT-Lizenz, © Dean Camera); die Lizenz liegt im Paket unter
`/usr/share/doc/lindeck/`.
