# Group "support" – notes for the coordinator

## 1. README section (English)

### Getting help: support report

Something not working? Open **Settings → Help → Create support report…** (or run
`lindeck support-report [FILE]` / `linewave support-report [FILE]`). Streamlin writes one text file
(default: `~/streamlin-support-<date>-<time>.txt`) that you can attach to a
[GitHub issue](https://github.com/adblocker-1/Streamlin/issues/new). It contains the Streamlin version and
installed packages, Python/Tk, distribution, desktop and session type, kernel, connected Stream Decks with their
hidraw/uinput permissions and udev rules, PipeWire/PulseAudio information, the LinDeck/Linewave state,
your `config.json` and the last 300 log lines. Passwords, tokens and keys (OBS, Home Assistant, Discord …) are
replaced by `<redacted>` – have a quick look before you upload it anyway.

### Updates: beta versions, earlier versions, RPM and Flatpak

- **Include beta versions** (Settings → Updates, config key `update_channel`: `"stable"` or `"beta"`) also
  offers pre-releases. Drafts are never offered.
- **Install an earlier version…** lists the published releases (newest first, with dates). Pick one to go back;
  the download is checked (SHA-256) and installed like an update. The automatic update check will offer the newest
  version again later – choose **Skip this version** to stay on the older one.
- Updates work for the `.deb` packages (apt) and the `.rpm` packages (dnf, or `rpm -U` without dnf).
- In the **Flatpak** version Streamlin never updates itself; updates come through your software center or
  `flatpak update`.

## 2. Release notes (English)

- New: support report – Settings → Help → “Create support report…” or `lindeck support-report` /
  `linewave support-report` writes one file with everything needed for a GitHub issue; passwords and tokens are
  removed.
- New: “Install an earlier version…” in Settings → Updates lets you go back to an older release (with SHA-256
  check).
- New: “Include beta versions” switch for the update channel (`update_channel`: stable/beta).
- The updater now also handles the RPM packages (dnf) and stays out of the way in Flatpak.

## 3. Release notes (Deutsch)

- Neu: Support-Bericht – Einstellungen → Hilfe → „Support-Bericht erstellen …“ oder `lindeck support-report` /
  `linewave support-report` schreibt eine Datei mit allem, was für ein GitHub-Issue nötig ist; Passwörter und
  Tokens werden entfernt.
- Neu: „Ältere Version installieren …“ unter Einstellungen → Updates bringt dich zu einer früheren Version zurück
  (mit SHA-256-Prüfung).
- Neu: Schalter „Beta-Versionen einbeziehen“ für den Update-Kanal (`update_channel`: stable/beta).
- Der Updater kann jetzt auch die RPM-Pakete (dnf) aktualisieren und hält sich in Flatpak heraus.
