#!/usr/bin/env bash
# Baut das Vorschau-Paket der Rust-Version (Aufruf: rust/packaging/build-deb.sh [Ausgabeordner])
#   freedeck-rs_<version>_<arch>.deb – FreeDeck und FreeWave (Rust), lässt sich neben der Python-Version installieren
# Die Programme heißen /usr/bin/freedeck-rs und /usr/bin/freewave-rs und nutzen dieselben Einstellungen,
# Profile und Mixer-Einstellungen wie die Python-Version.
set -euo pipefail
RUST="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$RUST/dist}" && cd "${1:-$RUST/dist}" && pwd)"
NAME=freedeck-rs
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$RUST/Cargo.toml" | head -1)"
ARCH="$(dpkg --print-architecture)"
BIN="$RUST/target/release/freedeck"
WAVE_BIN="$RUST/target/release/freewave"
WAVE=freewave-rs

[ -x "$BIN" ] || (cd "$RUST" && cargo build --release -p freedeck)
[ -x "$WAVE_BIN" ] || (cd "$RUST" && cargo build --release -p freewave)

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/${NAME}_${VERSION}_${ARCH}"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/lib/udev/rules.d" "$PKG/usr/lib/modules-load.d" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/doc/$NAME"

install -m 755 -s "$BIN" "$PKG/usr/bin/$NAME"
install -m 755 -s "$WAVE_BIN" "$PKG/usr/bin/$WAVE"
install -m 644 "$RUST/packaging/freedeck.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$NAME.svg"
install -m 644 "$RUST/packaging/freewave.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$WAVE.svg"
install -m 644 "$RUST/README.md" "$PKG/usr/share/doc/$NAME/README.md"
[ -f "$RUST/../LICENSE" ] && install -m 644 "$RUST/../LICENSE" "$PKG/usr/share/doc/$NAME/copyright"

# Zugriff auf die Geräte ohne root (wie die Python-Pakete, eigene Datei, damit sich beide nicht stören)
cat > "$PKG/lib/udev/rules.d/70-$NAME.rules" <<'EOF'
# Elgato-Geräte: Zugriff für den angemeldeten Benutzer
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
# Virtuelle Tastatur/Maus für Hotkeys (X11 und Wayland)
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF
echo uinput > "$PKG/usr/lib/modules-load.d/$NAME-uinput.conf"

cat > "$PKG/usr/share/applications/$NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FreeDeck (Rust)
GenericName=Deck Control
GenericName[de]=Deck-Steuerung
Comment=Preview of FreeDeck written in Rust: keys, profiles
Comment[de]=Vorschau von FreeDeck in Rust: Tasten, Profile
Exec=$NAME
Icon=$NAME
Terminal=false
Categories=Utility;AudioVideo;
Keywords=Stream Deck;Elgato;Streamdeck;Macro;
StartupWMClass=io.github.freestream.FreeDeck
StartupNotify=true
EOF

cat > "$PKG/usr/share/applications/$WAVE.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=FreeWave (Rust)
GenericName=Audio Mixer
GenericName[de]=Audio-Mixer
Comment=Preview of FreeWave written in Rust: mixer, channels and microphone
Comment[de]=Vorschau von FreeWave in Rust: Mixer, Kanäle und Mikrofon
Exec=$WAVE
Icon=$WAVE
Terminal=false
Categories=AudioVideo;Audio;Mixer;
Keywords=Wave Link;Elgato;Mixer;PipeWire;Microphone;
StartupWMClass=io.github.freestream.FreeWave
StartupNotify=true
EOF

# Laufzeit-Bibliotheken aus den gebauten Programmen ermitteln (GTK 4, libudev …)
mkdir -p "$STAGE/debian"
touch "$STAGE/debian/control"
DEPS="$(cd "$STAGE" && dpkg-shlibdeps -O "$PKG/usr/bin/$NAME" "$PKG/usr/bin/$WAVE" 2>/dev/null | sed -n 's/^shlibs:Depends=//p')"
# FreeWave braucht pactl/parec (pulseaudio-utils) und PipeWire; Effekte aus den Plugin-Paketen – wie
# WAVE_DEPS/WAVE_RECS der Python-Pakete, hier nur empfohlen (FreeDeck läuft auch ohne)
WAVE_RECS="pulseaudio-utils | pipewire-bin, wireplumber, pipewire-pulse, pipewire-bin, swh-plugins, tap-plugins, lilv-utils, x42-plugins, libspa-0.2-modules, libpipewire-0.3-modules, libnotify-bin"

cat > "$PKG/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: Felix <noreply@localhost>
Depends: ${DEPS:+$DEPS, }fonts-dejavu-core, udev
Recommends: xdg-utils, kmod, $WAVE_RECS
Description: FreeDeck and FreeWave for Elgato hardware, Rust version (preview)
 FreeDeck controls Elgato deck hardware (MK.2, XL, Mini, Plus, Neo, Pedal …)
 on Linux without Python. FreeWave (freewave-rs) is the audio mixer on
 PipeWire: channels, mixes, effects and Wave microphones. Both use the same
 settings and profiles as the Python version.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    modprobe uinput 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=hidraw --subsystem-match=usb --subsystem-match=misc 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
EOF
cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/postrm"
find "$PKG" -type d -exec chmod 755 {} +

dpkg-deb --root-owner-group --build "$PKG" "$OUT/${NAME}_${VERSION}_${ARCH}.deb" >/dev/null
echo "$OUT/${NAME}_${VERSION}_${ARCH}.deb"
