#!/usr/bin/env bash
# Baut die Pakete (Aufruf: ./packaging/build-deb.sh [Ausgabeordner] [Paket …])
#   streamlin_<version>_all.deb  – LinDeck + Linewave
#   lindeck_<version>_all.deb    – nur LinDeck (Stream Deck)
#   linewave_<version>_all.deb   – nur Linewave (Mixer, Wave-Mikrofon)
# Welche Dateien in welches Paket gehören, steht in packaging/parts.py.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import streamlin;print(streamlin.__version__)")
OUT="${1:-$ROOT/dist}"
shift || true
PACKAGES=("${@:-streamlin lindeck linewave}")
read -r -a PACKAGES <<< "${PACKAGES[*]}"
SD_LIB="$(python3 -c 'import StreamDeck,os;print(os.path.dirname(StreamDeck.__file__))')"
SD_LICENSE="$(python3 -c 'import importlib.metadata as m;d=m.distribution("streamdeck");print([str(d.locate_file(f)) for f in d.files if f.name=="LICENSE"][0])')"
mkdir -p "$OUT"

COMMON_DEPS="python3 (>= 3.10), python3-pil, python3-tk, python3-pil.imagetk, fonts-dejavu-core, xdg-utils, libnotify-bin, udev"
DECK_DEPS="python3-evdev, python3-cairosvg, libhidapi-libusb0, libusb-1.0-0, xdg-user-dirs, wl-clipboard, xclip, playerctl, pulseaudio-utils, msitools, kmod"
WAVE_DEPS="pulseaudio-utils, libusb-1.0-0"
COMMON_RECS="python3-pystray, gir1.2-ayatanaappindicator3-0.1"
DECK_RECS="chromium | google-chrome-stable | firefox, mpv | ffmpeg | pipewire-bin, fonts-noto-color-emoji, wmctrl, x11-utils, nodejs"
WAVE_RECS="wireplumber, pipewire-pulse, pipewire-bin, swh-plugins, tap-plugins, lilv-utils, jalv, pipewire-jack, x42-plugins, libspa-0.2-modules, libpipewire-0.3-modules"

# ------------------------------------------------------------------ Bausteine
launcher() {  # launcher <Paket> <Befehl> <Modul>
    cat > "$PKG/usr/bin/$2" <<EOF
#!/bin/sh
export PYTHONPATH="/opt/$1:/opt/$1/vendor\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m $3 "\$@"
EOF
    chmod 755 "$PKG/usr/bin/$2"
}

udev_rules() {  # udev_rules <Datei> <mit uinput: 1/0>
    {
        echo '# Elgato-Geräte (Stream Deck, Wave): Zugriff für den angemeldeten Benutzer'
        echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
        echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
        echo 'KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
        if [ "$2" = 1 ]; then
            echo '# Virtuelle Tastatur/Maus für Hotkeys (X11 und Wayland)'
            echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"'
        fi
    } > "$PKG/lib/udev/rules.d/$1"
}

lindeck_files() {
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
    echo uinput > "$PKG/usr/lib/modules-load.d/$NAME-uinput.conf"
    for sz in 48 64 128 256; do
        mkdir -p "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps"
        python3 -c "import sys;sys.path.insert(0,'$ROOT');from streamlin.app import make_icon;make_icon($sz).save(sys.argv[1])" \
            "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps/lindeck.png"
    done
}

linewave_files() {
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
    for sz in 48 64 128 256; do
        mkdir -p "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps"
        python3 -c "import sys;sys.path.insert(0,'$ROOT');from streamlin.wavehost import make_icon;make_icon($sz).save(sys.argv[1])" \
            "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps/linewave.png"
    done
}

scripts() {  # scripts <lindeck: 1/0> <linewave: 1/0>
    local start="" kill=""
    if [ "$1" = 1 ]; then
        start="$start
            runuser -u \"\$user\" -- env XDG_RUNTIME_DIR=\"/run/user/\$uid\" HOME=\"\$home\" \\
                setsid -f /usr/bin/lindeck run >/dev/null 2>&1 < /dev/null || true"
        kill="$kill
    pkill -f 'python3 -m (streamlin|lindeck)( |\$)' 2>/dev/null || true"
    fi
    if [ "$2" = 1 ]; then
        start="$start
            runuser -u \"\$user\" -- env XDG_RUNTIME_DIR=\"/run/user/\$uid\" HOME=\"\$home\" \\
                setsid -f /usr/bin/linewave --hidden >/dev/null 2>&1 < /dev/null || true"
        kill="$kill
    pkill -f 'python3 -m (streamlin|lindeck)\\.linewave' 2>/dev/null || true"
    fi
    cat > "$PKG/DEBIAN/postinst" <<EOF
#!/bin/sh
set -e
if [ "\$1" = "configure" ]; then
    modprobe uinput 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
    udevadm trigger --subsystem-match=usb --subsystem-match=hidraw --subsystem-match=misc 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    # alte Prozesse beenden und für angemeldete Benutzer sofort starten (sonst ab der nächsten Anmeldung)
$kill
    sleep 1
    if command -v loginctl >/dev/null 2>&1; then
        for uid in \$(loginctl list-users --no-legend 2>/dev/null | awk '{print \$1}'); do
            [ "\$uid" -ge 1000 ] 2>/dev/null || continue
            user=\$(getent passwd "\$uid" | cut -d: -f1)
            [ -n "\$user" ] || continue
            home=\$(getent passwd "\$uid" | cut -d: -f6)$start
        done
    fi
fi
exit 0
EOF
    cat > "$PKG/DEBIAN/prerm" <<EOF
#!/bin/sh
set -e
if [ "\$1" = "remove" ] || [ "\$1" = "upgrade" ]; then$kill
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
    local deck=0 wave=0
    case "$NAME" in
        streamlin) deck=1; wave=1 ;;
        lindeck) deck=1 ;;
        linewave) wave=1 ;;
        *) echo "unbekanntes Paket: $NAME" >&2; exit 1 ;;
    esac
    STAGE="$(mktemp -d)"
    PKG="$STAGE/${NAME}_${VERSION}_all"
    mkdir -p "$PKG/DEBIAN" "$PKG/opt/$NAME/streamlin" "$PKG/usr/bin" "$PKG/lib/udev/rules.d" "$PKG/etc/xdg/autostart" \
             "$PKG/usr/share/applications" "$PKG/usr/share/doc/$NAME" "$PKG/usr/lib/modules-load.d"
    for f in $(python3 "$ROOT/packaging/parts.py" "$NAME"); do
        cp -r "$ROOT/streamlin/$f" "$PKG/opt/$NAME/streamlin/"
    done
    find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
    cp "$ROOT/README.md" "$PKG/usr/share/doc/$NAME/README.md"
    udev_rules "70-$NAME.rules" "$deck"

    local deps="$COMMON_DEPS" recs="$COMMON_RECS" rel desc
    if [ "$deck" = 1 ]; then
        mkdir -p "$PKG/opt/$NAME/vendor"
        cp -r "$SD_LIB" "$PKG/opt/$NAME/vendor/StreamDeck"
        find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
        cp "$SD_LICENSE" "$PKG/usr/share/doc/$NAME/LICENSE.python-elgato-streamdeck"
        launcher "$NAME" lindeck streamlin
        lindeck_files
        deps="$deps, $DECK_DEPS"; recs="$recs, $DECK_RECS"
    fi
    if [ "$wave" = 1 ]; then
        launcher "$NAME" linewave streamlin.linewave
        linewave_files
        deps="$deps, $WAVE_DEPS"; recs="$recs, $WAVE_RECS"
    fi
    [ "$deck" = 1 ] || rmdir "$PKG/usr/lib/modules-load.d"
    case "$NAME" in
        streamlin)
            rel="Conflicts: lindeck, linewave
Replaces: lindeck, linewave"
            desc="Stream Deck and Wave Link replacement for Linux (LinDeck + Linewave)
 Both programs of Streamlin in one package: \"LinDeck\" drives every Elgato
 Stream Deck (Original, MK.2, Mini, XL, Neo, +, + XL, Studio, Pedal), reads
 the profiles of the Elgato app directly and runs Elgato plugins; OBS,
 Spotify and Discord are built in. \"Linewave\" replaces Wave Link: PipeWire
 mixer with Monitor/Stream Mix, effect plugins and Elgato Wave microphones.
 For only one of them, install the package lindeck or linewave instead."
            ;;
        lindeck)
            rel="Conflicts: streamlin
Replaces: streamlin
Suggests: linewave"
            desc="Stream Deck software for Linux (part of Streamlin)
 LinDeck drives every Elgato Stream Deck (Original, MK.2, Mini, XL, Neo, +,
 + XL, Studio, Pedal), reads the profiles of the Elgato app directly and
 runs Elgato plugins; OBS, Spotify and Discord are built in. Install the
 package linewave as well for the audio mixer and Wave microphones – both
 programs then work together like the package streamlin."
            ;;
        linewave)
            rel="Conflicts: streamlin
Replaces: streamlin
Suggests: lindeck"
            desc="Wave Link replacement for Linux (part of Streamlin)
 Linewave is a PipeWire audio mixer with Monitor and Stream Mix, channels
 per app, effect plugins (LADSPA, LV2) and control of Elgato Wave
 microphones. Install the package lindeck as well for the Stream Deck –
 both programs then work together like the package streamlin."
            ;;
    esac
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
Description: $desc
 Available in English, German, Spanish, French and Portuguese (Brazil).
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
