#!/usr/bin/env bash
# Baut lindeck_<version>_all.deb  (Aufruf: ./packaging/build-deb.sh [Ausgabeordner])
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import lindeck;print(lindeck.__version__)")
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
PKG="$STAGE/lindeck_${VERSION}_all"
SD_LIB="$(python3 -c 'import StreamDeck,os;print(os.path.dirname(StreamDeck.__file__))')"
SD_LICENSE="$(python3 -c 'import importlib.metadata as m;d=m.distribution("streamdeck");print([str(d.locate_file(f)) for f in d.files if f.name=="LICENSE"][0])')"

mkdir -p "$PKG/DEBIAN" "$PKG/opt/lindeck/vendor" "$PKG/usr/bin" "$PKG/lib/udev/rules.d" \
         "$PKG/usr/lib/modules-load.d" "$PKG/etc/xdg/autostart" "$PKG/usr/share/applications" \
         "$PKG/usr/share/doc/lindeck"

cp -r "$ROOT/lindeck" "$PKG/opt/lindeck/"
cp -r "$SD_LIB" "$PKG/opt/lindeck/vendor/StreamDeck"
find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
cp "$SD_LICENSE" "$PKG/usr/share/doc/lindeck/LICENSE.python-elgato-streamdeck"
cp "$ROOT/README.md" "$PKG/usr/share/doc/lindeck/README.md"

cat > "$PKG/usr/bin/lindeck" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/lindeck:/opt/lindeck/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m lindeck "$@"
EOF
chmod 755 "$PKG/usr/bin/lindeck"

cat > "$PKG/lib/udev/rules.d/70-lindeck.rules" <<'EOF'
# Elgato Stream Deck: Zugriff für den angemeldeten Benutzer
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
# Virtuelle Tastatur/Maus für Hotkeys (X11 und Wayland)
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF
echo uinput > "$PKG/usr/lib/modules-load.d/lindeck-uinput.conf"

cat > "$PKG/etc/xdg/autostart/lindeck.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LinDeck
Comment=Stream Deck Dienst
Exec=/usr/bin/lindeck run
Icon=input-keyboard
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
EOF
cat > "$PKG/usr/share/applications/lindeck.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LinDeck (Stream Deck)
Comment=Stream Deck starten
Exec=/usr/bin/lindeck run
Icon=input-keyboard
Categories=Utility;
EOF
cat > "$PKG/usr/share/applications/lindeck-editor.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LinDeck Editor
GenericName=Stream-Deck-Profile bearbeiten
Comment=Tasten, Seiten und Ordner der Stream-Deck-Profile bearbeiten
Exec=/usr/bin/lindeck edit
Icon=preferences-desktop-keyboard-shortcuts
Categories=Utility;Settings;
StartupWMClass=Lindeck-editor
EOF

cat > "$PKG/DEBIAN/control" <<EOF
Package: lindeck
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Felix <noreply@localhost>
Depends: python3 (>= 3.10), python3-pil, python3-tk, python3-pil.imagetk, python3-evdev, python3-cairosvg, libhidapi-libusb0, libusb-1.0-0, fonts-dejavu-core, xdg-utils, xdg-user-dirs, libnotify-bin, wl-clipboard, xclip, playerctl, pulseaudio-utils, msitools, udev, kmod
Recommends: chromium | google-chrome-stable | firefox, mpv | ffmpeg | pipewire-bin, fonts-noto-color-emoji, wmctrl, x11-utils, wireplumber, pipewire-pulse, nodejs, swh-plugins
Description: Elgato-kompatible Stream-Deck-Software für Linux
 Steuert alle Elgato Stream Decks (Original, MK.2, Mini, XL, Neo, +, + XL,
 Studio, Pedal) und liest Profile der Elgato-App direkt ein. Profile,
 Tasten, Seiten und Ordner lassen sich mit dem grafischen Editor
 („LinDeck Editor“) direkt unter Linux bearbeiten. Elgato-Plugins (Node.js)
 laufen über den eingebauten Plugin-Host; OBS, Spotify, Discord und Wave Link
 sind für Linux umgesetzt, inklusive PipeWire-Mixer. Profile,
 Backups und der Elgato-Installer werden automatisch aus Downloads,
 Desktop und USB-Sticks übernommen.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    modprobe uinput 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=usb --subsystem-match=hidraw --subsystem-match=misc 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    # Für angemeldete Benutzer sofort starten (sonst ab der nächsten Anmeldung automatisch)
    if command -v loginctl >/dev/null 2>&1; then
        for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
            [ "$uid" -ge 1000 ] 2>/dev/null || continue
            user=$(getent passwd "$uid" | cut -d: -f1)
            [ -n "$user" ] || continue
            pkill -u "$uid" -f "python3 -m lindeck run" 2>/dev/null || true
            runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$(getent passwd "$uid" | cut -d: -f6)" \
                setsid -f /usr/bin/lindeck run >/dev/null 2>&1 < /dev/null || true
        done
    fi
fi
exit 0
EOF
cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "upgrade" ]; then
    pkill -f "python3 -m lindeck run" 2>/dev/null || true
fi
exit 0
EOF
cat > "$PKG/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
udevadm control --reload-rules 2>/dev/null || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/DEBIAN/postrm"

find "$PKG" -type d -exec chmod 755 {} +
find "$PKG/opt" "$PKG/usr/share" "$PKG/lib" "$PKG/etc" "$PKG/usr/lib" -type f -exec chmod 644 {} +
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$PKG" "$OUT/lindeck_${VERSION}_all.deb" >/dev/null
rm -rf "$STAGE"
echo "$OUT/lindeck_${VERSION}_all.deb"
