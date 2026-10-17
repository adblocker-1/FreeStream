#!/usr/bin/env bash
# Baut die Pakete (Aufruf: ./packaging/build-deb.sh [Ausgabeordner] [Paket …])
#   freestream_<version>_all.deb  – FreeDeck + FreeWave
#   lindeck_<version>_all.deb    – nur FreeDeck (Stream Deck)
#   linewave_<version>_all.deb   – nur FreeWave (Mixer, Wave-Mikrofon)
# Welche Dateien in welches Paket gehören, steht in packaging/parts.py;
# Dateiinhalte und Installationsskripte stehen in packaging/common.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=packaging/common.sh
source "$ROOT/packaging/common.sh"
OUT="${1:-$ROOT/dist}"
shift || true
# streamlin/lindeck/linewave: dasselbe Programm unter den Namen vor der Umbenennung (0.28) – damit ältere
# Installationen das Update finden; ihr nächstes Update wechselt dann auf freestream/freedeck/freewave
PACKAGES=("${@:-freestream freedeck freewave streamlin lindeck linewave}")
read -r -a PACKAGES <<< "${PACKAGES[*]}"
mkdir -p "$OUT"

COMMON_DEPS="python3 (>= 3.10), python3-pil, python3-tk, python3-pil.imagetk, fonts-dejavu-core, xdg-utils, libnotify-bin, udev"
DECK_DEPS="python3-evdev, python3-cairosvg, libhidapi-libusb0, libusb-1.0-0, xdg-user-dirs, wl-clipboard, xclip, playerctl, pulseaudio-utils, msitools, kmod"
WAVE_DEPS="pulseaudio-utils, libusb-1.0-0"
COMMON_RECS="python3-pystray, gir1.2-ayatanaappindicator3-0.1, zenity | kdialog, libsecret-tools"
DECK_RECS="chromium | google-chrome-stable | firefox, mpv | ffmpeg | pipewire-bin, fonts-noto-color-emoji, wmctrl, x11-utils, nodejs"
WAVE_RECS="wireplumber, pipewire-pulse, pipewire-bin, swh-plugins, tap-plugins, lilv-utils, jalv, pipewire-jack, x42-plugins, libspa-0.2-modules, libpipewire-0.3-modules"

# ------------------------------------------------------------------ Installationsskripte
scripts() {  # scripts <freedeck: 1/0> <freewave: 1/0>
    hooks "$1" "$2"
    {
        echo '#!/bin/sh'
        echo 'set -e'
        echo 'if [ "$1" = "configure" ]; then'
        post_install
        echo 'fi'
        echo 'exit 0'
    } > "$PKG/DEBIAN/postinst"
    cat > "$PKG/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
if [ "\$1" = "remove" ] || [ "\$1" = "upgrade" ]; then$HOOK_KILL
fi
exit 0
EOF
    cat > "$PKG/DEBIAN/postrm" <<EOF
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
# beim Start entstandene __pycache__-Ordner mit entfernen
if [ "\$1" = "remove" ] || [ "\$1" = "purge" ]; then
    rm -rf "/opt/$NAME"
fi
exit 0
EOF
    chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"
}

# ------------------------------------------------------------------ ein Paket
build() {
    NAME="$1"
    package_parts "$NAME"
    local rel
    STAGE="$(mktemp -d)"
    PKG="$STAGE/${NAME}_${VERSION}_all"
    mkdir -p "$PKG/DEBIAN" "$PKG/opt/$NAME/freestream" "$PKG/usr/bin" "$PKG/$UDEV_DIR" "$PKG/etc/xdg/autostart" \
             "$PKG/usr/share/applications" "$PKG/usr/share/doc/$NAME" "$PKG/usr/lib/modules-load.d"
    stage_files "$NAME" "$deck" "$wave" "usr/share/doc/$NAME"

    local deps="$COMMON_DEPS" recs="$COMMON_RECS"
    if [ "$deck" = 1 ]; then
        deps="$deps, $DECK_DEPS"; recs="$recs, $DECK_RECS"
    fi
    if [ "$wave" = 1 ]; then
        deps="$deps, $WAVE_DEPS"; recs="$recs, $WAVE_RECS"
    fi
    [ "$deck" = 1 ] || rmdir "$PKG/usr/lib/modules-load.d"
    case "$NAME" in
        freestream)
            rel="Conflicts: freedeck, freewave, streamlin, lindeck, linewave
Replaces: freedeck, freewave, streamlin, lindeck, linewave
Provides: streamlin"
            ;;
        freedeck)
            rel="Conflicts: freestream, streamlin, lindeck
Replaces: freestream, streamlin, lindeck
Provides: lindeck
Suggests: freewave"
            ;;
        freewave)
            rel="Conflicts: freestream, streamlin, linewave
Replaces: freestream, streamlin, linewave
Provides: linewave
Suggests: freedeck"
            ;;
        streamlin)
            rel="Conflicts: lindeck, linewave
Replaces: lindeck, linewave"
            ;;
        lindeck)
            rel="Conflicts: streamlin
Replaces: streamlin"
            ;;
        linewave)
            rel="Conflicts: streamlin
Replaces: streamlin"
            ;;
    esac
    describe "$NAME"
    cat > "$PKG/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Felix <noreply@localhost>
Depends: $deps
Recommends: $recs
$rel
Description: $DESC
EOF
    scripts "$deck" "$wave"
    find "$PKG" -type d -exec chmod 755 {} +
    find "$PKG/opt" "$PKG/usr/share" "$PKG/lib" "$PKG/etc" -type f -exec chmod 644 {} +
    [ -d "$PKG/usr/lib/modules-load.d" ] && find "$PKG/usr/lib/modules-load.d" -type f -exec chmod 644 {} +
    dpkg-deb --root-owner-group --build "$PKG" "$OUT/${NAME}_${VERSION}_all.deb" >/dev/null
    rm -rf "$STAGE"
    echo "$OUT/${NAME}_${VERSION}_all.deb"
}

for p in "${PACKAGES[@]}"; do
    build "$p"
done
