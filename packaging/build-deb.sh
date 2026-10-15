#!/usr/bin/env bash
# Baut streamlin_<version>_all.deb (LinDeck + Linewave)  (Aufruf: ./packaging/build-deb.sh [Ausgabeordner])
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import streamlin;print(streamlin.__version__)")
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
PKG="$STAGE/streamlin_${VERSION}_all"
SD_LIB="$(python3 -c 'import StreamDeck,os;print(os.path.dirname(StreamDeck.__file__))')"
SD_LICENSE="$(python3 -c 'import importlib.metadata as m;d=m.distribution("streamdeck");print([str(d.locate_file(f)) for f in d.files if f.name=="LICENSE"][0])')"

mkdir -p "$PKG/DEBIAN" "$PKG/opt/streamlin/vendor" "$PKG/usr/bin" "$PKG/lib/udev/rules.d" \
         "$PKG/usr/lib/modules-load.d" "$PKG/etc/xdg/autostart" "$PKG/usr/share/applications" \
         "$PKG/usr/share/doc/streamlin"

cp -r "$ROOT/streamlin" "$PKG/opt/streamlin/"
cp -r "$SD_LIB" "$PKG/opt/streamlin/vendor/StreamDeck"
find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
cp "$SD_LICENSE" "$PKG/usr/share/doc/streamlin/LICENSE.python-elgato-streamdeck"
cp "$ROOT/README.md" "$PKG/usr/share/doc/streamlin/README.md"

cat > "$PKG/usr/bin/lindeck" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/streamlin:/opt/streamlin/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m streamlin "$@"
EOF
cat > "$PKG/usr/bin/linewave" <<'EOF'
#!/bin/sh
export PYTHONPATH="/opt/streamlin:/opt/streamlin/vendor${PYTHONPATH:+:$PYTHONPATH}"
exec /usr/bin/python3 -m streamlin.linewave "$@"
EOF
chmod 755 "$PKG/usr/bin/lindeck" "$PKG/usr/bin/linewave"

cat > "$PKG/lib/udev/rules.d/70-streamlin.rules" <<'EOF'
# Elgato Stream Deck: Zugriff für den angemeldeten Benutzer
SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"
# Virtuelle Tastatur/Maus für Hotkeys (X11 und Wayland)
KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"
EOF
echo uinput > "$PKG/usr/lib/modules-load.d/streamlin-uinput.conf"

cat > "$PKG/etc/xdg/autostart/lindeck.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LinDeck
Comment=Start the Stream Deck in the background
Comment[de]=Stream Deck im Hintergrund starten
Comment[es]=Iniciar el Stream Deck en segundo plano
Comment[fr]=Démarrer le Stream Deck en arrière-plan
Comment[pt_BR]=Iniciar o Stream Deck em segundo plano
Exec=/usr/bin/lindeck run
Icon=lindeck
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
EOF
cat > "$PKG/usr/share/applications/lindeck.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=LinDeck
GenericName=Stream Deck
Comment=Set up your Stream Deck: keys, profiles, plugins
Comment[de]=Stream Deck einrichten: Tasten, Profile, Plugins
Comment[es]=Configura tu Stream Deck: teclas, perfiles, plugins
Comment[fr]=Configurer votre Stream Deck : touches, profils, plugins
Comment[pt_BR]=Configure seu Stream Deck: teclas, perfis, plugins
Exec=/usr/bin/lindeck
Icon=lindeck
Terminal=false
Categories=Utility;AudioVideo;
Keywords=Stream Deck;Elgato;Streamdeck;Macro;
Keywords[de]=Stream Deck;Elgato;Streamdeck;Makro;
StartupWMClass=Lindeck
StartupNotify=true
EOF
# Zweites Programm: Linewave (Mixer + Mikrofon), wie Elgato Wave Link
cat > "$PKG/etc/xdg/autostart/linewave.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Linewave
Comment=Start the audio mixer and Wave microphone in the background
Comment[de]=Audio-Mixer und Wave-Mikrofon im Hintergrund starten
Comment[es]=Iniciar el mezclador de audio y el micrófono Wave en segundo plano
Comment[fr]=Démarrer le mixeur audio et le micro Wave en arrière-plan
Comment[pt_BR]=Iniciar o mixer de áudio e o microfone Wave em segundo plano
Exec=/usr/bin/linewave --hidden
Icon=linewave
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
EOF
cat > "$PKG/usr/share/applications/linewave.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Linewave
GenericName=Audio Mixer
GenericName[de]=Audio-Mixer
GenericName[es]=Mezclador de audio
GenericName[fr]=Mixeur audio
GenericName[pt_BR]=Mixer de áudio
Comment=Wave Link replacement: mixer (Monitor/Stream Mix) and Elgato Wave microphone
Comment[de]=Wave-Link-Ersatz: Mixer (Monitor-/Stream-Mix) und Elgato-Wave-Mikrofon
Comment[es]=Sustituto de Wave Link: mezclador (mezcla de monitor/stream) y micrófono Elgato Wave
Comment[fr]=Remplaçant de Wave Link : mixeur (mix moniteur/stream) et micro Elgato Wave
Comment[pt_BR]=Substituto do Wave Link: mixer (mix de monitor/stream) e microfone Elgato Wave
Exec=/usr/bin/linewave
Icon=linewave
Terminal=false
Categories=AudioVideo;Audio;Mixer;
Keywords=Wave Link;Elgato;Wave:3;Mixer;Microphone;PipeWire;
Keywords[de]=Wave Link;Elgato;Wave:3;Mixer;Mikrofon;PipeWire;
StartupWMClass=Linewave
StartupNotify=true
EOF
# App-Symbole in mehreren Größen
for sz in 48 64 128 256; do
    mkdir -p "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps"
    python3 -c "import sys;sys.path.insert(0,'$ROOT');from streamlin.app import make_icon;make_icon($sz).save(sys.argv[1])" \
        "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps/lindeck.png"
    python3 -c "import sys;sys.path.insert(0,'$ROOT');from streamlin.wavehost import make_icon;make_icon($sz).save(sys.argv[1])" \
        "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps/linewave.png"
done

cat > "$PKG/DEBIAN/control" <<EOF
Package: streamlin
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Maintainer: Felix <noreply@localhost>
Depends: python3 (>= 3.10), python3-pil, python3-tk, python3-pil.imagetk, python3-evdev, python3-cairosvg, libhidapi-libusb0, libusb-1.0-0, fonts-dejavu-core, xdg-utils, xdg-user-dirs, libnotify-bin, wl-clipboard, xclip, playerctl, pulseaudio-utils, msitools, udev, kmod
Recommends: python3-pystray, gir1.2-ayatanaappindicator3-0.1, chromium | google-chrome-stable | firefox, mpv | ffmpeg | pipewire-bin, fonts-noto-color-emoji, wmctrl, x11-utils, wireplumber, pipewire-pulse, nodejs, swh-plugins, tap-plugins, pipewire-bin, lilv-utils, jalv, pipewire-jack, x42-plugins
Conflicts: lindeck
Replaces: lindeck
Provides: lindeck
Description: Stream Deck and Wave Link replacement for Linux (LinDeck + Linewave)
 Drives every Elgato Stream Deck (Original, MK.2, Mini, XL, Neo, +, + XL,
 Studio, Pedal) and reads the profiles of the Elgato app directly. Two
 programs like Elgato's: "LinDeck" for the Stream Deck (edit profiles,
 keys, plugins) and "Linewave" as a Wave Link replacement (PipeWire mixer
 with Monitor/Stream Mix, effect plugins, Elgato Wave microphones).
 Elgato plugins (Node.js) run in the built-in plugin host; OBS, Spotify,
 Discord and Wave Link are implemented for Linux. Profiles, backups and
 the Elgato installer are taken over automatically from Downloads,
 Desktop and USB sticks. Available in English, German, Spanish, French
 and Portuguese (Brazil).
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    modprobe uinput 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
    udevadm trigger --subsystem-match=usb --subsystem-match=hidraw --subsystem-match=misc 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    # Für angemeldete Benutzer sofort starten (sonst ab der nächsten Anmeldung automatisch)
    if command -v loginctl >/dev/null 2>&1; then
        for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
            [ "$uid" -ge 1000 ] 2>/dev/null || continue
            user=$(getent passwd "$uid" | cut -d: -f1)
            [ -n "$user" ] || continue
            pkill -u "$uid" -f "python3 -m (streamlin|lindeck)" 2>/dev/null || true
            sleep 1
            runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$(getent passwd "$uid" | cut -d: -f6)" \
                setsid -f /usr/bin/lindeck run >/dev/null 2>&1 < /dev/null || true
            runuser -u "$user" -- env XDG_RUNTIME_DIR="/run/user/$uid" HOME="$(getent passwd "$uid" | cut -d: -f6)" \
                setsid -f /usr/bin/linewave --hidden >/dev/null 2>&1 < /dev/null || true
        done
    fi
fi
exit 0
EOF
cat > "$PKG/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "upgrade" ]; then
    pkill -f "python3 -m (streamlin|lindeck)" 2>/dev/null || true
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
dpkg-deb --root-owner-group --build "$PKG" "$OUT/streamlin_${VERSION}_all.deb" >/dev/null
rm -rf "$STAGE"
echo "$OUT/streamlin_${VERSION}_all.deb"
