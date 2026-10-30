#!/usr/bin/env bash
# Baut das Vorschau-Paket der Rust-Version (Aufruf: rust/packaging/build-deb.sh [Ausgabeordner])
#   freedeck-rs_<version>_<arch>.deb – FreeDeck (Rust), lässt sich neben der Python-Version installieren
# Das Programm heißt /usr/bin/freedeck-rs und nutzt dieselben Einstellungen und Profile wie FreeDeck.
set -euo pipefail
RUST="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$RUST/dist}" && cd "${1:-$RUST/dist}" && pwd)"
NAME=freedeck-rs
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' "$RUST/Cargo.toml" | head -1)"
ARCH="$(dpkg --print-architecture)"
BIN="$RUST/target/release/freedeck"

[ -x "$BIN" ] || (cd "$RUST" && cargo build --release -p freedeck)

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/${NAME}_${VERSION}_${ARCH}"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/lib/udev/rules.d" "$PKG/usr/lib/modules-load.d" \
         "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/doc/$NAME"

install -m 755 -s "$BIN" "$PKG/usr/bin/$NAME"
install -m 644 "$RUST/packaging/freedeck.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$NAME.svg"
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

# Laufzeit-Bibliotheken aus dem gebauten Programm ermitteln (GTK 4, libudev …)
mkdir -p "$STAGE/debian"
touch "$STAGE/debian/control"
DEPS="$(cd "$STAGE" && dpkg-shlibdeps -O "$PKG/usr/bin/$NAME" 2>/dev/null | sed -n 's/^shlibs:Depends=//p')"

cat > "$PKG/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: Felix <noreply@localhost>
Depends: ${DEPS:+$DEPS, }fonts-dejavu-core, udev
Recommends: xdg-utils, kmod
Description: FreeDeck for Elgato decks, Rust version (preview)
 Controls Elgato deck hardware (MK.2, XL, Mini, Plus, Neo, Pedal …) on Linux
 without Python. Uses the same settings and profiles as FreeDeck.
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
