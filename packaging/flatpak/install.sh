#!/usr/bin/env bash
# Installiert Streamlin im Flatpak-Build nach $FLATPAK_DEST (/app); Aufruf aus dem Manifest.
# Programmdateien wie im Paket streamlin, Starter (launcher.py), Desktop-Dateien und Symbole
# (aus packaging/common.sh) sowie Weiterleitungen für Programme des Systems.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=packaging/common.sh
source "$ROOT/packaging/common.sh"
APP_ID="${FLATPAK_ID:-io.github.adblocker1.Streamlin}"
DEST="${FLATPAK_DEST:-/app}"

# Diese Programme gibt es in der Sandbox nicht – sie laufen auf dem System (flatpak-spawn --host).
# --watch-bus: endet der Aufruf in der Sandbox, endet auch das Programm auf dem System.
HOST_TOOLS="pactl parec paplay pacat pw-cli pw-dump pw-link pw-jack pw-play pw-cat pipewire wpctl
jalv lv2ls lv2info playerctl wmctrl kdotool xdotool xprop swaymsg hyprctl xclip xsel wl-copy wl-paste
mpv ffplay node nodejs msiextract 7z notify-send pkill pgrep gsettings localectl xdg-user-dir"

install -d "$DEST/bin" "$DEST/lib/streamlin/streamlin" "$DEST/share/applications" "$DEST/share/streamlin"
for f in $(python3 "$ROOT/packaging/parts.py" streamlin); do
    cp -r "$ROOT/streamlin/$f" "$DEST/lib/streamlin/streamlin/"
done
find "$DEST/lib/streamlin" -name "__pycache__" -prune -exec rm -rf {} +

install -m755 "$ROOT/packaging/flatpak/launcher.py" "$DEST/bin/streamlin-launch"
for prog in lindeck linewave; do
    printf '#!/bin/sh\nexec /app/bin/streamlin-launch %s "$@"\n' "$prog" > "$DEST/bin/$prog"
    chmod 755 "$DEST/bin/$prog"
done
for tool in $HOST_TOOLS; do
    printf '#!/bin/sh\nexec flatpak-spawn --host --watch-bus %s "$@"\n' "$tool" > "$DEST/bin/$tool"
    chmod 755 "$DEST/bin/$tool"
done

lindeck_desktop lindeck "$APP_ID" > "$DEST/share/applications/$APP_ID.desktop"
linewave_desktop linewave "$APP_ID.Linewave" > "$DEST/share/applications/$APP_ID.Linewave.desktop"
PKG="$(mktemp -d)"
icons "$APP_ID" app
icons "$APP_ID.Linewave" wavehost
cp -r "$PKG/usr/share/icons" "$DEST/share/"
rm -rf "$PKG"

# udev-Regeln kann ein Flatpak nicht installieren: liegen zum Kopieren bereit (siehe README)
udev_rules_text 1 > "$DEST/share/streamlin/70-streamlin.rules"
cp "$ROOT/README.md" "$DEST/share/streamlin/README.md"
