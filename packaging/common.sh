# shellcheck shell=bash disable=SC2034
# Bausteine für build-deb.sh: Dateien, Starter, udev-Regeln, Desktop-Dateien, Installationsskripte (per „source“ geladen).
# Erwartet: ROOT (Projektordner). Die Funktionen schreiben in den Paketbaum "$PKG"
# und nutzen NAME (Paketname) sowie UDEV_DIR (Ordner der udev-Regeln im Paketbaum).
VERSION=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import streamlin;print(streamlin.__version__)")
SD_LIB="$(python3 -c 'import StreamDeck,os;print(os.path.dirname(StreamDeck.__file__))')"
SD_LICENSE="$(python3 -c 'import importlib.metadata as m;d=m.distribution("streamdeck");print([str(d.locate_file(f)) for f in d.files if f.name=="LICENSE"][0])')"
UDEV_DIR="${UDEV_DIR:-lib/udev/rules.d}"

package_parts() {  # package_parts <Paket> -> setzt deck/wave (1/0)
    deck=0 wave=0
    case "$1" in
        streamlin) deck=1; wave=1 ;;
        lindeck) deck=1 ;;
        linewave) wave=1 ;;
        *) echo "unbekanntes Paket: $1" >&2; exit 1 ;;
    esac
}

# ------------------------------------------------------------------ Dateien
launcher() {  # launcher <Paket> <Befehl> <Modul>
    cat > "$PKG/usr/bin/$2" <<EOF
#!/bin/sh
export PYTHONPATH="/opt/$1:/opt/$1/vendor\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m $3 "\$@"
EOF
    chmod 755 "$PKG/usr/bin/$2"
}

udev_rules_text() {  # udev_rules_text <mit uinput: 1/0>
    echo '# Elgato-Geräte (Stream Deck, Wave): Zugriff für den angemeldeten Benutzer'
    echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
    echo 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
    echo 'KERNEL=="hidraw*", ATTRS{idVendor}=="0fd9", TAG+="uaccess"'
    if [ "$1" = 1 ]; then
        echo '# Virtuelle Tastatur/Maus für Hotkeys (X11 und Wayland)'
        echo 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"'
    fi
}

udev_rules() {  # udev_rules <Datei> <mit uinput: 1/0>
    mkdir -p "$PKG/$UDEV_DIR"
    udev_rules_text "$2" > "$PKG/$UDEV_DIR/$1"
}

icons() {  # icons <Symbolname> <Modul mit make_icon>
    for sz in 48 64 128 256; do
        mkdir -p "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps"
        python3 -c "import sys;sys.path.insert(0,'$ROOT');from streamlin.$2 import make_icon;make_icon($sz).save(sys.argv[1])" \
            "$PKG/usr/share/icons/hicolor/${sz}x${sz}/apps/$1.png"
    done
}

lindeck_desktop() {  # lindeck_desktop <Exec-Befehl> <Symbol>
    cat <<EOF
[Desktop Entry]
Type=Application
Name=LinDeck
GenericName=Deck Control
GenericName[de]=Deck-Steuerung
GenericName[es]=Control del deck
GenericName[fr]=Contrôle du deck
GenericName[pt_BR]=Controle do deck
Comment=Set up LinDeck: keys, profiles, plugins
Comment[de]=LinDeck einrichten: Tasten, Profile, Plugins
Comment[es]=Configura LinDeck: teclas, perfiles, plugins
Comment[fr]=Configurer LinDeck : touches, profils, plugins
Comment[pt_BR]=Configure o LinDeck: teclas, perfis, plugins
Exec=$1
Icon=$2
Terminal=false
Categories=Utility;AudioVideo;
Keywords=Stream Deck;Elgato;Streamdeck;Macro;
Keywords[de]=Stream Deck;Elgato;Streamdeck;Makro;
StartupWMClass=Lindeck
StartupNotify=true
EOF
}

lindeck_autostart() {  # lindeck_autostart <Exec-Befehl> <Symbol>
    cat <<EOF
[Desktop Entry]
Type=Application
Name=LinDeck
Comment=Start LinDeck in the background
Comment[de]=LinDeck im Hintergrund starten
Comment[es]=Iniciar LinDeck en segundo plano
Comment[fr]=Démarrer LinDeck en arrière-plan
Comment[pt_BR]=Iniciar o LinDeck em segundo plano
Exec=$1
Icon=$2
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
EOF
}

linewave_desktop() {  # linewave_desktop <Exec-Befehl> <Symbol>
    cat <<EOF
[Desktop Entry]
Type=Application
Name=Linewave
GenericName=Audio Mixer
GenericName[de]=Audio-Mixer
GenericName[es]=Mezclador de audio
GenericName[fr]=Mixeur audio
GenericName[pt_BR]=Mixer de áudio
Comment=Wave Link replacement: mixer with your own mixes and Elgato Wave microphone
Comment[de]=Wave-Link-Ersatz: Mixer mit eigenen Mixen und Elgato-Wave-Mikrofon
Comment[es]=Sustituto de Wave Link: mezclador con tus propias mezclas y micrófono Elgato Wave
Comment[fr]=Remplaçant de Wave Link : mixeur avec vos propres mix et micro Elgato Wave
Comment[pt_BR]=Substituto do Wave Link: mixer com seus próprios mixes e microfone Elgato Wave
Exec=$1
Icon=$2
Terminal=false
Categories=AudioVideo;Audio;Mixer;
Keywords=Wave Link;Elgato;Wave:3;Mixer;Microphone;PipeWire;
Keywords[de]=Wave Link;Elgato;Wave:3;Mixer;Mikrofon;PipeWire;
StartupWMClass=Linewave
StartupNotify=true
EOF
}

linewave_autostart() {  # linewave_autostart <Exec-Befehl> <Symbol>
    cat <<EOF
[Desktop Entry]
Type=Application
Name=Linewave
Comment=Start the audio mixer and Wave microphone in the background
Comment[de]=Audio-Mixer und Wave-Mikrofon im Hintergrund starten
Comment[es]=Iniciar el mezclador de audio y el micrófono Wave en segundo plano
Comment[fr]=Démarrer le mixeur audio et le micro Wave en arrière-plan
Comment[pt_BR]=Iniciar o mixer de áudio e o microfone Wave em segundo plano
Exec=$1
Icon=$2
NoDisplay=true
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
EOF
}

lindeck_files() {
    lindeck_autostart "/usr/bin/lindeck run" lindeck > "$PKG/etc/xdg/autostart/lindeck.desktop"
    lindeck_desktop /usr/bin/lindeck lindeck > "$PKG/usr/share/applications/lindeck.desktop"
    echo uinput > "$PKG/usr/lib/modules-load.d/$NAME-uinput.conf"
    icons lindeck app
}

linewave_files() {
    linewave_autostart "/usr/bin/linewave --hidden" linewave > "$PKG/etc/xdg/autostart/linewave.desktop"
    linewave_desktop /usr/bin/linewave linewave > "$PKG/usr/share/applications/linewave.desktop"
    icons linewave wavehost
}

# Programmdateien, Bibliothek und Starter in den Paketbaum (Ordner müssen existieren)
stage_files() {  # stage_files <Paket> <deck> <wave> <Doku-Ordner im Paketbaum>
    for f in $(python3 "$ROOT/packaging/parts.py" "$1"); do
        cp -r "$ROOT/streamlin/$f" "$PKG/opt/$1/streamlin/"
    done
    find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
    cp "$ROOT/README.md" "$PKG/$4/README.md"
    udev_rules "70-$1.rules" "$2"
    if [ "$2" = 1 ]; then
        mkdir -p "$PKG/opt/$1/vendor"
        cp -r "$SD_LIB" "$PKG/opt/$1/vendor/StreamDeck"
        find "$PKG/opt" -name "__pycache__" -prune -exec rm -rf {} +
        cp "$SD_LICENSE" "$PKG/$4/LICENSE.python-elgato-streamdeck"
        launcher "$1" lindeck streamlin
        lindeck_files
    fi
    if [ "$3" = 1 ]; then
        launcher "$1" linewave streamlin.linewave
        linewave_files
    fi
}

# ------------------------------------------------------------------ Installationsskripte
# hooks <lindeck: 1/0> <linewave: 1/0> -> setzt HOOK_START (für angemeldete Benutzer starten)
# und HOOK_KILL (laufende Programme beenden), jeweils als Shell-Zeilen
hooks() {
    HOOK_START="" HOOK_KILL=""
    if [ "$1" = 1 ]; then
        HOOK_START="$HOOK_START
            runuser -u \"\$user\" -- env XDG_RUNTIME_DIR=\"/run/user/\$uid\" HOME=\"\$home\" \\
                setsid -f /usr/bin/lindeck run >/dev/null 2>&1 < /dev/null || true"
        HOOK_KILL="$HOOK_KILL
    pkill -f 'python3 -m (streamlin|lindeck)( |\$)' 2>/dev/null || true"
    fi
    if [ "$2" = 1 ]; then
        HOOK_START="$HOOK_START
            runuser -u \"\$user\" -- env XDG_RUNTIME_DIR=\"/run/user/\$uid\" HOME=\"\$home\" \\
                setsid -f /usr/bin/linewave --hidden >/dev/null 2>&1 < /dev/null || true"
        HOOK_KILL="$HOOK_KILL
    pkill -f 'python3 -m (streamlin|lindeck)\\.linewave' 2>/dev/null || true"
    fi
}

# Nach der Installation: udev neu laden, alte Prozesse beenden, für angemeldete Benutzer starten
post_install() {  # (nach hooks)
    cat <<EOF
    modprobe uinput 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
    udevadm trigger --subsystem-match=usb --subsystem-match=hidraw --subsystem-match=misc 2>/dev/null || true
    udevadm settle 2>/dev/null || true
    # alte Prozesse beenden und für angemeldete Benutzer sofort starten (sonst ab der nächsten Anmeldung)
$HOOK_KILL
    sleep 1
    if command -v loginctl >/dev/null 2>&1; then
        for uid in \$(loginctl list-users --no-legend 2>/dev/null | awk '{print \$1}'); do
            [ "\$uid" -ge 1000 ] 2>/dev/null || continue
            user=\$(getent passwd "\$uid" | cut -d: -f1)
            [ -n "\$user" ] || continue
            home=\$(getent passwd "\$uid" | cut -d: -f6)$HOOK_START
        done
    fi
EOF
}

# ------------------------------------------------------------------ Beschreibungen
# describe <Paket> -> setzt DESC (erste Zeile: Kurzbeschreibung, danach mit einem Leerzeichen eingerückt)
describe() {
    case "$1" in
        streamlin)
            DESC="LinDeck and Linewave for Linux (Elgato decks and Wave Link)
 Both programs of Streamlin in one package: \"LinDeck\" drives every Elgato
 deck model (Original, MK.2, Mini, XL, Neo, +, + XL, Studio, Pedal), reads
 the profiles of the Elgato app directly and runs Elgato plugins; OBS,
 Spotify and Discord are built in. \"Linewave\" replaces Wave Link: PipeWire
 mixer with Personal Mix, Stream Mix and your own mixes, effect plugins and
 Elgato Wave microphones.
 For only one of them, install the package lindeck or linewave instead."
            ;;
        lindeck)
            DESC="LinDeck - software for Elgato decks on Linux (part of Streamlin)
 LinDeck drives every Elgato deck model (Original, MK.2, Mini, XL, Neo, +,
 + XL, Studio, Pedal), reads the profiles of the Elgato app directly and
 runs Elgato plugins; OBS, Spotify and Discord are built in. Install the
 package linewave as well for the audio mixer and Wave microphones – both
 programs then work together like the package streamlin."
            ;;
        linewave)
            DESC="Wave Link replacement for Linux (part of Streamlin)
 Linewave is a PipeWire audio mixer with Personal Mix, Stream Mix and your
 own mixes, channels per app, effect plugins (LADSPA, LV2) and control of
 Elgato Wave microphones. Install the package lindeck as well for LinDeck –
 both programs then work together like the package streamlin."
            ;;
    esac
    DESC="$DESC
 Available in English, German, Spanish, French and Portuguese (Brazil)."
}
