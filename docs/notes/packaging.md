# packaging – RPM packages and Flatpak

## 1. README section (English)

### Fedora, openSUSE (RPM)

Every release also has RPM packages with the same three variants as the .deb
packages:

- `streamlin-<version>-1.noarch.rpm` – both programs (recommended)
- `lindeck-<version>-1.noarch.rpm` – only LinDeck (Stream Deck)
- `linewave-<version>-1.noarch.rpm` – only Linewave (mixer and Wave microphone)

Install with `sudo dnf install ./streamlin-<version>-1.noarch.rpm` (Fedora) or
`sudo zypper install --allow-unsigned-rpm ./streamlin-<version>-1.noarch.rpm`
(openSUSE). Like with the .deb packages, the package sets up device access
(udev rules), hotkeys (uinput), autostart and menu entries and starts the
programs for logged-in users. Installing `lindeck` + `linewave` replaces
`streamlin` and the other way round. Uninstall with `sudo dnf remove streamlin`
(or `lindeck` / `linewave`); your profiles stay in `~/.local/share/streamlin`.

### Flatpak

`streamlin-<version>.flatpak` contains both programs (menu entries “LinDeck” and
“Linewave”). It needs the Flathub remote for the runtime:

```sh
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user ./streamlin-<version>.flatpak
```

A Flatpak cannot install udev rules. Run this **once** so that LinDeck may use
the Stream Deck, the Wave microphone and the virtual keyboard for hotkeys:

```sh
flatpak run --command=cat io.github.adblocker1.Streamlin /app/share/streamlin/70-streamlin.rules \
  | sudo tee /etc/udev/rules.d/70-streamlin.rules
echo uinput | sudo tee /etc/modules-load.d/streamlin-uinput.conf
sudo modprobe uinput && sudo udevadm control --reload-rules && sudo udevadm trigger
```

Good to know:

- The Flatpak starts audio tools (`pactl`, `pw-cli`, `pw-dump`, `parec`,
  `pipewire` …) and the programs your keys open **on your system**
  (`flatpak-spawn --host`). So the sandbox does not limit Streamlin; the
  audio tools must be installed on the system (`pulseaudio-utils`,
  `pipewire-utils`/`pipewire-bin`).
- At the first start with a window, an autostart entry is created
  (`~/.config/autostart/io.github.adblocker1.Streamlin.desktop`); it starts
  LinDeck and Linewave in the background. Delete it to turn autostart off.
- Settings and profiles of the Flatpak live in
  `~/.var/app/io.github.adblocker1.Streamlin/` (not shared with the .deb/.rpm).
- The built-in effects work; LADSPA plugins of the system are not listed in the
  Flatpak. For all effect plugins, use the .deb or .rpm package.
- The Flatpak does not update itself: install the new `.flatpak` file over it.

Build it yourself with `./packaging/flatpak/build-flatpak.sh` (needs `flatpak`,
`flatpak-builder` and the runtime `org.freedesktop.Sdk//25.08`), the RPMs with
`./packaging/build-rpm.sh` (needs `rpmbuild`).

## 2. Release notes (English)

- New: RPM packages for Fedora and openSUSE (`streamlin`, `lindeck`, `linewave`),
  with the same contents and setup as the .deb packages.
- New: Flatpak bundle `streamlin-<version>.flatpak` with LinDeck and Linewave;
  audio tools and opened programs run on the system, autostart is set up at the
  first start (udev rules: see README).
- Every package is built and test-installed automatically (Ubuntu, Fedora,
  Flatpak) before it is attached to the release.

## 3. Release notes (Deutsch)

- Neu: RPM-Pakete für Fedora und openSUSE (`streamlin`, `lindeck`, `linewave`)
  mit demselben Inhalt und derselben Einrichtung wie die .deb-Pakete.
- Neu: Flatpak-Bündel `streamlin-<version>.flatpak` mit LinDeck und Linewave;
  Audio-Werkzeuge und geöffnete Programme laufen auf dem System, der Autostart
  wird beim ersten Start eingerichtet (udev-Regeln: siehe README).
- Jedes Paket wird automatisch gebaut und probeweise installiert (Ubuntu,
  Fedora, Flatpak), bevor es ans Release kommt.
