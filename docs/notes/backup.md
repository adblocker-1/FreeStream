# Backup and restore (group `backup`)

## README section (English)

### Backup and restore

All your LinDeck and Linewave data fits into one file: profiles with their key images and
backgrounds, resources, installed plugins and their settings, and `config.json` (appearance,
language, updates, Linewave mixer channels, routes and effects, Wave microphone settings).
Logs, caches, running-process data and downloaded updates are left out.

- **In the app:** open *Settings* (gear icon) → *Backup* → **Create backup…** and choose where to
  save `streamlin-YYYY-MM-DD.streamlin-backup`. Untick *Include installed plugins* for a smaller file.
- **Restore:** *Settings* → *Backup* → **Restore…**, pick the file, check what it contains and
  confirm. Your current data is saved first (the last three safety copies are kept in
  `~/.local/state/streamlin/backups/`), then the data is replaced and LinDeck and Linewave restart.
  A backup made without plugins keeps the plugins you have installed.
- **Command line:**

  ```sh
  lindeck backup [FILE|FOLDER] [--no-plugins]   # same as: linewave backup …
  lindeck restore FILE [--yes]                  # same as: linewave restore …
  ```

  `restore` shows the contents and asks before replacing anything (`--yes` skips the question).
  Running programs are stopped for the restore and started again afterwards.

Backups are checked before anything is changed: unknown formats, absolute paths, `..` entries,
links pointing outside the data folders and oversized archives are rejected.

## Release notes (English)

- New: back up all profiles, images, plugins and settings of LinDeck and Linewave into one
  `.streamlin-backup` file – in *Settings → Backup* or with `lindeck backup` / `linewave backup`.
- New: restore a backup with a preview of its contents; your current data is saved automatically
  first, and LinDeck and Linewave restart with the restored data.
- The Settings window now scrolls when it does not fit on small screens.

## Release notes (Deutsch)

- Neu: Alle Profile, Bilder, Plugins und Einstellungen von LinDeck und Linewave in einer
  `.streamlin-backup`-Datei sichern – unter *Einstellungen → Sicherung* oder mit `lindeck backup` /
  `linewave backup`.
- Neu: Sicherung wiederherstellen mit Vorschau des Inhalts; dein aktueller Stand wird vorher
  automatisch gesichert, LinDeck und Linewave starten mit den wiederhergestellten Daten neu.
- Das Einstellungsfenster lässt sich jetzt scrollen, wenn es auf kleinen Bildschirmen nicht ganz passt.
