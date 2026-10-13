# Elgato Stream Deck for Linux (LinDeck)

Linux-Ersatz für die Elgato-Stream-Deck-Software. LinDeck steuert alle
Stream-Deck-Modelle per USB und liest die Profile der Elgato-App **direkt und
unverändert** ein – Tasten, Icons, Titel, Ordner, Seiten, Multi-Actions, Dials
und Touchstreifen. Ein Neu-Einrichten unter Linux ist nicht nötig – und mit dem
eingebauten **grafischen Editor** lassen sich Profile direkt unter Linux
bearbeiten oder neu anlegen.

Installiert wird eine einzige `.deb`-Datei. Danach läuft alles automatisch:
Gerätezugriff, virtuelle Tastatur, Autostart und der Import deiner Profile.

> **Status:** Version 0.4 – getestet mit simulierten Geräten aller Modelle und
> den Elgato-Standardprofilen. Tests auf echter Hardware stehen noch aus.

---

## Inhalt

- [Unterstützte Geräte](#unterstützte-geräte)
- [Installation](#installation)
- [Profile übernehmen](#profile-übernehmen)
- [Profile bearbeiten (Editor)](#profile-bearbeiten-editor)
- [Unterstützte Aktionen](#unterstützte-aktionen)
- [Elgato-Plugins](#elgato-plugins)
- [OBS, Spotify, Discord](#obs-spotify-discord)
- [Audio-Mixer (Wave-Link-Ersatz)](#audio-mixer-wave-link-ersatz)
- [Wave-Mikrofon](#wave-mikrofon)
- [Hardware-Selbsttest](#hardware-selbsttest)
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
   sudo apt install ./lindeck_0.4.0_all.deb
   ```

Das Paket erledigt automatisch:

- alle Abhängigkeiten über `apt` (die Stream-Deck-Bibliothek ist bereits enthalten)
- udev-Regel für den Gerätezugriff ohne root (`/lib/udev/rules.d/70-lindeck.rules`)
- `uinput`-Modul für Hotkeys unter X11 **und** Wayland
- Autostart bei jeder Anmeldung und sofortiger Start nach der Installation
- Menüeinträge „LinDeck (Stream Deck)“ und „LinDeck Editor“

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

## Profile bearbeiten (Editor)

Start über den Menüeintrag **LinDeck Editor** oder im Terminal mit
`lindeck edit` (optional mit Profilname: `lindeck edit "Streaming"`).

Aufbau wie in der Elgato-App:

- **Mitte:** das Gerät mit allen Tasten, Dials (Stream Deck +, + XL, Studio) und
  der Seitenleiste – die Tasten werden genau so gezeichnet wie auf dem Gerät.
- **Rechts:** alle eingebauten Aktionen nach Kategorien, mit Suchfeld.
- **Unten:** die Eigenschaften der gewählten Taste.

| Was | So geht's |
|---|---|
| Aktion zuweisen | aus der Liste rechts auf eine Taste **ziehen** oder doppelklicken |
| Taste verschieben/tauschen | Taste auf einen anderen Platz ziehen – oder auf eine Seitennummer, um sie auf eine andere Seite zu verschieben |
| Kopieren, Einfügen, Löschen | Rechtsklick auf die Taste oder `Strg+C`, `Strg+X`, `Strg+V`, `Entf` |
| Titel, Schrift, Farbe, Position | im Bereich **Darstellung** (pro Zustand, z. B. bei Hotkey Switch) |
| Eigenes Icon | **Bild wählen…** (PNG, JPG, SVG, animiertes GIF) – **Standard** stellt das Original wieder her |
| Hotkey aufnehmen | ins Hotkey-Feld klicken und die Kombination drücken, oder **Eingeben…** (z. B. `Strg+Umschalt+T`) |
| Ordner | Aktion „Ordner“ ablegen, per Doppelklick öffnen, mit **↩ Zurück** wieder verlassen |
| Multi Action, Key Logic, Dial Stack … | Schritte unter **Enthaltene Aktionen** hinzufügen, bearbeiten, sortieren; Verzögerungen einfügen |
| Seiten | **+** / **−** fügt Seiten hinzu bzw. entfernt sie, **◀ ▶** ändert die Reihenfolge |
| Rückgängig | `Strg+Z` / `Strg+Y` oder **↶ ↷** |
| Profile | **Neu…** (mit Gerätemodell), Duplizieren, Umbenennen, Löschen, Importieren, Exportieren |
| Plugin-Aktionen | bleiben erhalten; Titel, Icon und Rohdaten (**Einstellungen (JSON)…**) sind bearbeitbar |

Jede Änderung wird **sofort gespeichert**; der laufende LinDeck-Dienst lädt das
Profil automatisch neu, sodass das Stream Deck die Änderung nach ein bis zwei
Sekunden zeigt. Gespeichert wird im Originalformat der Elgato-App – unbekannte
Einstellungen (z. B. von Plugins) bleiben unverändert erhalten. **Exportieren…**
erzeugt eine `.streamDeckProfile`-Datei, z. B. als Sicherung oder für einen
anderen Rechner.

Tasten ohne eigenes Icon zeigen die Elgato-Standardsymbole, sobald der
Elgato-Installer übernommen wurde (siehe oben); sonst erscheint der Aktionsname.

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

**Plugins von Drittanbietern:** OBS, Spotify, Discord und Wave Link setzt LinDeck
selbst um (siehe unten), andere Plugins laufen über den eingebauten
[Plugin-Host](#elgato-plugins). Aktionen von Plugins, die unter Linux nicht laufen,
werden mit ihrem gespeicherten Icon angezeigt; beim Drücken erscheint ein
Warnsymbol. `lindeck report` zeigt, welche Plugins deine Profile verwenden und
welche davon unterstützt sind.

## Elgato-Plugins

LinDeck enthält einen Plugin-Host für das offizielle Stream-Deck-SDK
(WebSocket-Protokoll). Plugins installieren:

- `.streamDeckPlugin`-Datei in den Download-Ordner legen (wird automatisch
  installiert) oder
- `lindeck plugin install <Datei.streamDeckPlugin>`

| Plugin-Art | unter Linux |
|---|---|
| Node.js-Plugins (alle mit dem offiziellen SDK ab Stream Deck 6.4 gebauten) | laufen mit dem System-Node.js (Paket `nodejs`, Version 20 empfohlen) |
| Plugins mit `CodePathLin` im Manifest (Linux-Programm) | laufen direkt |
| reine Windows-/macOS-Programme (`.exe`, macOS-Binärdateien) | nicht lauffähig – `lindeck plugin list` zeigt das an |
| ältere HTML/JavaScript-Plugins | nicht lauffähig |

Unterstützt werden Tasten, Dials und Touchstreifen: `keyDown`/`keyUp`,
`willAppear`/`willDisappear`, `dialRotate`/`dialDown`/`dialUp`, `touchTap`,
`setTitle`, `setImage` (PNG/JPG/SVG), `setState`, `showAlert`/`showOk`,
`setSettings`/`getSettings` (wird im Profil gespeichert), globale Einstellungen,
`setFeedback`, `openUrl`, `switchToProfile`, `logMessage`. Die Aktionen
installierter Plugins erscheinen im Editor in der Aktionsliste. Plugin-Logs
liegen unter `~/.local/state/lindeck/plugins/`.

**Einstellungsseiten (Property Inspector):** Im Editor öffnet
**Einstellungsseite…** die HTML-Einstellungen des Plugins wie in der
Elgato-App – in einem schlanken Chromium-/Chrome-Fenster, sonst im
Standardbrowser. Änderungen speichert das Plugin sofort im Profil; der Editor
übernimmt sie automatisch. Dafür muss der LinDeck-Dienst laufen. Die Seite wird
nur lokal (127.0.0.1) und mit einem zufälligen Zugangsschlüssel ausgeliefert;
fremde Webseiten können sich nicht mit dem Plugin-Host verbinden. Alternativ
lassen sich die Einstellungen weiterhin als JSON bearbeiten.

## OBS, Spotify, Discord

Diese Plugins sind in LinDeck nachgebaut. Tasten aus importierten Elgato-Profilen
werden automatisch zugeordnet; neue legst du im Editor an (Kategorien
„OBS Studio“, „Spotify & Medien“, „Discord“).

**OBS Studio** (über obs-websocket, in OBS ab Version 28 enthalten): Szene
wechseln, Aufnahme (auch Pause), Stream, Quelle ein/aus, Audio-Eingang
stumm/Lautstärke per Dial, Replay-Puffer, virtuelle Kamera, Studio-Modus,
Übergang, Screenshot. Tasten zeigen den Zustand live (z. B. Aufnahme läuft).

1. In OBS: *Werkzeuge → WebSocket-Server-Einstellungen → WebSocket-Server aktivieren*
2. Passwort in `config.json` eintragen: `"obs": {"password": "…"}`
3. `lindeck obs` prüft die Verbindung.

Bei importierten Tasten des Elgato-OBS-Plugins nutzt LinDeck die gespeicherten
Einstellungen und – falls dort kein Name steht – den Tastentitel als Szenen-
bzw. Quellennamen.

**Spotify und andere Player** (über MPRIS/`playerctl`): Wiedergabe/Pause,
vor/zurück, aktueller Titel, Zufall, Wiederholen, Lautstärke (auch per Dial).
Funktioniert mit jedem MPRIS-Player (Spotify, VLC, Firefox, Rhythmbox …).

**Discord** (über die lokale RPC-Schnittstelle der Desktop-App): Mikrofon stumm,
Ton aus, Push to Talk/Mute, Sprachkanal betreten/verlassen, Textkanal öffnen.
Discord erlaubt das nur angemeldeten Apps – einmalig einrichten:

1. Auf https://discord.com/developers/applications eine App anlegen („New Application“).
2. Unter *OAuth2* die Redirect-URL `http://localhost` eintragen.
3. Client-ID und Client-Secret in `config.json`:
   `"discord": {"client_id": "…", "client_secret": "…"}`
4. `lindeck discord` ausführen und die Anfrage in Discord bestätigen.

## Audio-Mixer (Wave-Link-Ersatz)

Wie Elgato Wave Link, auf Basis von PipeWire: Programme laufen in virtuellen
Kanälen (System, Musik, Browser, Voice Chat, Spiel, Effekte, Mikrofon), und jeder
Kanal hat zwei unabhängige Regler:

- **Monitor-Mix** – was du auf Kopfhörer/Lautsprechern hörst
- **Stream-Mix** – was dein Publikum hört; in OBS als Audioquelle
  „Monitor of LinDeck Stream-Mix“ wählen

```bash
lindeck mixer setup                    # einrichten (bleibt nach Neustart aktiv)
lindeck mixer                          # Übersicht aller Kanäle
lindeck mixer set music stream 40      # Musik im Stream auf 40 %
lindeck mixer assign spotify music     # Programm dauerhaft einem Kanal zuordnen
lindeck mixer output                   # Monitor-Mix auf das nächste Ausgabegerät
lindeck mixer remove                   # alles entfernen
```

Bekannte Programme (Spotify, Browser, Discord, Steam …) werden automatisch dem
passenden Kanal zugeordnet, alles andere landet in „System“. Auf dem Stream Deck:
Aktion **Mixer-Kanal** (Taste: stumm/lauter/leiser, Dial: Lautstärke, Drücken:
stumm) und **Monitor-Ausgang**. Tasten des Elgato-Wave-Link-Plugins aus
importierten Profilen werden auf den Mixer abgebildet. Voraussetzung: PipeWire
mit `pipewire-pulse` (Standard bei aktuellen Distributionen).

## Wave-Mikrofon

**Elgato Wave:3** – alle Einstellungen direkt im Mikrofon, wie mit Wave Link:

| Funktion | Befehl |
|---|---|
| Gain (0–40 dB), Stumm | `lindeck wave gain 70` (Prozent), `lindeck wave mute` |
| Low Cut, Clipguard (im Mikrofon) | `lindeck wave lowcut on`, `lindeck wave clipguard on` |
| LEDs ein/aus | `lindeck wave leds off` |
| LEDs umkehren (Mikrofon hängend am Arm) | `lindeck wave flip on` |
| Kopfhörer-Lautstärke (−60 … 0 dB) | `lindeck wave headphone -20` |
| Mithören Mikrofon/PC (0–100 %) | `lindeck wave monitor 50` |

Das Wave:3 hat keine einstellbare LED-Farbe oder -Helligkeit, nur „LEDs aus“ und
„umkehren“. Grundlage ist die öffentliche Protokoll-Analyse von
[elgato-wave3-ubuntu](https://github.com/KailasMahavarkar/elgato-wave3-ubuntu)
(MIT-Lizenz). LinDeck liest nach jeder Änderung zurück und nimmt sie
zurück, falls das Mikrofon unerwartet reagiert. Der Ton läuft dabei ungestört
weiter. Zugriff erhält der angemeldete Benutzer über die mitgelieferte udev-Regel.

**Wave:1, Wave Neo, Wave XLR** – Gain und Stumm über die USB-Audio-Steuerung;
Low Cut (80/120 Hz) und Clipguard per PipeWire-Filter (Clipguard braucht
`swh-plugins`). Das bearbeitete Signal erscheint als Mikrofon **„LinDeck Wave“**.
LED-Steuerung ist für diese Modelle nicht möglich (Protokoll nicht dokumentiert).

Auf dem Stream Deck: Aktion **Wave-Mikrofon** (Stumm, Gain, Low Cut, Clipguard,
LEDs; Dial: Gain). `lindeck wave` zeigt den aktuellen Zustand.

## Hardware-Selbsttest

`lindeck selftest --stop-daemon` prüft jedes angeschlossene Stream Deck Schritt
für Schritt: Display und Helligkeit, jede Taste, jeden Dial (links, rechts,
drücken), jedes Feld des Touchstreifens samt Wischen und die Neo-Touch-Tasten.
Der Bericht landet in `~/.local/state/lindeck/selftest-*.txt` – bitte bei
Problemen (oder als Erfolgsmeldung für ein noch ungetestetes Modell) an ein
Issue anhängen.

## Befehle

Für den normalen Betrieb ist kein Befehl nötig. Für Sonderfälle:

| Befehl | Zweck |
|---|---|
| `lindeck run` | Dienst starten (läuft normalerweise automatisch) |
| `lindeck edit [Profil]` | grafischen Editor öffnen |
| `lindeck import <Datei/Ordner>` | Profile manuell importieren |
| `lindeck profiles` | importierte Profile anzeigen |
| `lindeck devices` | angeschlossene Geräte mit Seriennummer und Firmware |
| `lindeck assign <Seriennr.> <Profil>` | Profil fest einem Gerät zuordnen |
| `lindeck report [Profil]` | Kompatibilitätsbericht: Plugins und fehlende Programmzuordnungen |
| `lindeck preview <Profil> -o <Ordner> [--all]` | Seiten als PNG rendern, ohne Gerät |
| `lindeck extract-assets <Stream_Deck.msi>` | Standard-Icons manuell übernehmen |
| `lindeck remove <Profil>` | Profil löschen |
| `lindeck plugin [list\|install\|remove]` | Elgato-Plugins verwalten |
| `lindeck mixer [status\|setup\|remove\|set\|mute\|assign\|output]` | Audio-Mixer (Wave-Link-Ersatz) |
| `lindeck wave [status\|gain\|mute\|lowcut\|clipguard\|leds\|flip\|headphone\|monitor]` | Wave-Mikrofon |
| `lindeck obs` | Verbindung zu OBS prüfen |
| `lindeck discord` | Discord verbinden (einmalige Freigabe) |
| `lindeck selftest [--stop-daemon]` | Hardware-Selbsttest |
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
  "devices": { "<Seriennummer>": { "profile": "<Profil-ID>" } },
  "obs": { "host": "127.0.0.1", "port": 4455, "password": "" },
  "discord": { "client_id": "", "client_secret": "" },
  "mixer": { "enabled": false, "monitor_output": "", "mic": "", "routes": { "spotify": "music" } },
  "wave_mic": { "lowcut": false, "lowcut_freq": 80, "clipguard": false, "set_default": true },
  "node_path": ""
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
| `obs` | Verbindung zu obs-websocket |
| `discord` | eigene Discord-App für die RPC-Steuerung |
| `mixer` | Audio-Mixer: aktiv, Monitor-Ausgang, Mikrofon, Programm → Kanal (`system`, `music`, `browser`, `voice`, `game`, `sfx`), eigene Kanäle über `channels` |
| `wave_mic` | Startwerte für Low Cut/Clipguard |
| `node_path` | Node.js für Plugins (leer = `node` aus dem Pfad) |

**Dateien:**

| Pfad | Inhalt |
|---|---|
| `~/.local/share/lindeck/profiles/` | importierte Profile (Elgato-Format) |
| `~/.local/share/lindeck/resources/` | eingebettete Ressourcen (z. B. Sounds) |
| `~/.local/share/lindeck/elgato-assets/` | Standard-Icons aus dem Elgato-Installer |
| `~/.local/share/lindeck/plugins/` | installierte Elgato-Plugins |
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
| Taste zeigt Warnsymbol | Plugin nicht installiert oder nur für Windows/macOS (`lindeck report`, `lindeck plugin list`); bei OBS/Discord: Verbindung prüfen (`lindeck obs`, `lindeck discord`) |
| OBS-Tasten zeigen „OBS offline“ | WebSocket-Server in OBS aktivieren, Passwort in `config.json` |
| Kein Ton nach `mixer setup` | `lindeck mixer` prüfen; Monitor-Ausgang mit `lindeck mixer output` wählen; `lindeck mixer remove` stellt alles zurück |

Log ansehen: `tail -f ~/.local/state/lindeck/lindeck.log`

## Entwicklung

### Aus dem Quellcode starten

```bash
sudo apt install python3-pil python3-pil.imagetk python3-tk python3-evdev python3-cairosvg libhidapi-libusb0
pip install --user "streamdeck>=0.10"
python3 -m lindeck -v run
python3 -m lindeck edit          # Editor
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
Release an. Ein manueller Build ist über *Actions → Build .deb → Run workflow* möglich;
mit der Option „Release anlegen“ entsteht dabei direkt der Release `v<Version>`
(Version aus `lindeck/__init__.py`).

```bash
git tag v0.4.0 && git push origin v0.4.0
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

Ohne `LINDECK_TEST_PROFILES` werden diese Tests übersprungen.

Die Editor-Tests (`tests/test_editor.py`) brauchen keine Elgato-Profile; die
Oberflächentests laufen mit einem Display, z. B. virtuell:

```bash
sudo apt install xvfb
xvfb-run -a python3 -m pytest -q tests/test_editor.py
```

`tests/test_integrations.py` prüft Plugin-Host, Einstellungsseiten, OBS, Discord,
Mixer, Wave-Mikrofon und Selbsttest gegen simulierte Gegenstellen. Mit
`LINDECK_BROWSER_TEST=1` (und global installiertem Playwright) läuft zusätzlich
eine Plugin-Einstellungsseite in einem echten Chromium.

## Aufbau des Projekts

```
lindeck/
├── __main__.py     Kommandozeile
├── editor.py       grafischer Profil-Editor (Tk)
├── editdoc.py      Profile bearbeiten und speichern (Logik des Editors, Undo, Export)
├── catalog.py      Aktionskatalog des Editors, Hotkey-Umwandlung
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
├── plugins.py      Plugin-Host für das Elgato-SDK (WebSocket), Plugin-Installation
├── ws.py           WebSocket-Server/-Client (Standardbibliothek)
├── native.py       Linux-Umsetzung von OBS, Spotify/MPRIS, Discord, Wave Link
├── obs.py          obs-websocket-Client (v5)
├── discord.py      Discord-RPC über den lokalen IPC-Socket
├── mixer.py        Audio-Mixer mit PipeWire (Wave-Link-Ersatz)
├── wavemic.py      Wave-Mikrofon: Gain, Mute, Low Cut, Clipguard (Hardware oder PipeWire)
├── wave3hw.py      Wave:3-Steuerprotokoll (USB-Control-Transfers über libusb)
├── services.py     Hintergrunddienste (Mixer, Wave-Filter)
├── selftest.py     Hardware-Selbsttest
├── config.py       Konfiguration und Laufzeitzustand
└── paths.py        XDG-Verzeichnisse
packaging/
└── build-deb.sh    baut das Debian-Paket
tests/
├── test_engine.py  Tests mit simulierten Geräten
├── test_editor.py  Tests für Editor und Profilbearbeitung
└── test_integrations.py  Plugin-Host, OBS, Discord, Mixer, Wave, Selbsttest (simulierte Gegenstellen)
```

**Ablauf:** `daemon` erkennt Geräte → je Gerät ein `DeckController` (`engine`)
→ lädt Profilseite aus `profiles` → erzeugt pro Taste/Dial eine Aktion aus
`actions` → `render` zeichnet die Bilder → Tastendrücke werden über eine
Ereigniswarteschlange an die Aktionen weitergereicht.

## Roadmap

- [x] Grafischer Editor zum Bearbeiten von Profilen direkt unter Linux (seit 0.2)
- [x] Plugin-Host für das Elgato-Plugin-SDK (WebSocket-Protokoll) (seit 0.3; Node.js- und Linux-Plugins)
- [x] Linux-Umsetzung häufiger Plugins: OBS, Discord, Spotify/MPRIS, Wave Link (seit 0.3)
- [x] Wave-Link-Ersatz auf Basis von PipeWire: virtuelle Kanäle, Monitor-/Stream-Mix (seit 0.3)
- [x] Wave-Mikrofon: Gain, Stumm, Low Cut, Clipguard (seit 0.3)
- [x] Wave:3 direkt per USB: Low Cut/Clipguard in Hardware, LEDs aus/umkehren, Kopfhörer, Mithören (seit 0.4)
- [x] Property Inspector (Einstellungsseiten) für Plugins (seit 0.4)
- [ ] LED-Steuerung für Wave:1, Wave Neo, Wave XLR (Protokoll nicht dokumentiert)
- [ ] Tests auf echter Hardware aller Modelle – Werkzeug vorhanden (`lindeck selftest`), Berichte willkommen

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
