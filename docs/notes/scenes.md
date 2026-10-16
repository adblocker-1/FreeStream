# Notes: Mixer scenes, auto-ducking, global shortcuts (group `scenes`)

## 1. README section (English)

### Mixer scenes

A scene remembers the volume and mute state of every Linewave channel in both mixes (Monitor and Stream).

- **Linewave window:** in the Mixer view, click **Scenes** in the header (the button shows the active scene).
  Pick a scene to apply it, choose **Save current mix as scene …**, or **Manage scenes …** to rename,
  delete or give a scene a keyboard shortcut.
- **Command line** (also as `lindeck mixer scene …`):

  ```
  linewave mixer scene list               # * marks the active scene
  linewave mixer scene save "Just Chatting"
  linewave mixer scene apply "just chatting"
  linewave mixer scene rename gaming "Late Night"
  linewave mixer scene delete "Late Night"
  ```

- **Stream Deck:** LinDeck has a new key action **Linewave → Mixer Scene**. It applies the chosen scene and
  lights up while the mixer matches that scene.

Scenes are stored in `config.json` under `mixer.scenes`.

### Auto-ducking

Linewave can turn channels down while you speak – e.g. your music drops by 12 dB as soon as you talk and comes
back when you stop. Open **Microphone** in Linewave and switch on **Auto-ducking**, then choose:

- **Channels** that are lowered (default: Music) – always in both mixes,
- **Lower by** (−6 to −24 dB),
- **Sensitivity**: the marker on the level meter is the threshold; speak and move it so that your voice
  reaches it but background noise does not.

Ducking fades in quickly, holds through short pauses and fades out smoothly. It works relative to your
fader: if you move a fader (in Linewave, on the Stream Deck or via the command line) while ducking is active,
that becomes your new level, and afterwards every channel returns exactly to it. Fine-tuning in
`config.json` → `mixer.ducking`: `attack_ms`, `hold_ms`, `release_ms`, `threshold_db`, `amount_db`.

### Global keyboard shortcuts (also on Wayland)

Linewave adds shortcuts to your desktop's own keyboard settings, so they work in every app – also on Wayland:

- **Mute microphone:** Linewave → Microphone → *Keyboard shortcut* → **Set**, press the keys.
- **Apply a scene:** Scenes → *Manage scenes …* → *Keyboard shortcut* next to the scene.

Supported: GNOME (Ubuntu, Pop!_OS, Budgie …), Cinnamon, MATE, XFCE and KDE Plasma 5/6 (KDE applies new
shortcuts after you log in again). On other desktops Linewave shows the command so you can add it as a custom
shortcut yourself, e.g. `linewave mic mute toggle` or `linewave mixer scene apply gaming`.
`linewave mic mute` also accepts `on`, `off` and `toggle`; without a Wave microphone it mutes the default
microphone. Linewave only ever changes the shortcuts it created itself.

## 2. Release notes (English)

- Mixer scenes: save the whole mix under a name and switch with one click, from the command line
  (`linewave mixer scene …`), with a keyboard shortcut or with the new Stream Deck key “Mixer Scene”.
- Auto-ducking: Linewave lowers music (or any channels you pick) while you speak and restores your exact
  levels afterwards – faders keep working while ducking is active.
- Global keyboard shortcuts that also work on Wayland (GNOME, Cinnamon, MATE, XFCE, KDE Plasma) for muting the
  microphone and applying scenes.
- `linewave mic mute` now takes `on`, `off` or `toggle`.

## 3. Release notes (Deutsch)

- Mixer-Szenen: den ganzen Mix unter einem Namen speichern und mit einem Klick wechseln – auch über die
  Kommandozeile (`linewave mixer scene …`), per Tastenkürzel oder mit der neuen Stream-Deck-Taste „Mixer-Szene“.
- Auto-Ducking: Linewave macht Musik (oder die Kanäle deiner Wahl) leiser, solange du sprichst, und stellt danach
  deine Lautstärken exakt wieder her – die Regler funktionieren dabei weiter.
- Globale Tastenkürzel, die auch unter Wayland funktionieren (GNOME, Cinnamon, MATE, XFCE, KDE Plasma), zum
  Stummschalten des Mikrofons und zum Anwenden von Szenen.
- `linewave mic mute` versteht jetzt `on`, `off` und `toggle`.
