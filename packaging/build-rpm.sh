#!/usr/bin/env bash
# Baut RPM-Pakete für Fedora/openSUSE (Aufruf: ./packaging/build-rpm.sh [Ausgabeordner] [Paket …])
#   streamlin-<version>-1.noarch.rpm  – LinDeck + Linewave
#   lindeck-<version>-1.noarch.rpm    – nur LinDeck (Stream Deck)
#   linewave-<version>-1.noarch.rpm   – nur Linewave (Mixer, Wave-Mikrofon)
# Gleicher Inhalt wie die .deb-Pakete (packaging/common.sh, packaging/parts.py); nur die
# Abhängigkeiten tragen Fedora-Namen (mit openSUSE-Alternativen) und die udev-Regeln
# liegen unter /usr/lib/udev. Braucht rpmbuild (Debian/Ubuntu: apt install rpm).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UDEV_DIR="usr/lib/udev/rules.d"
# shellcheck source=packaging/common.sh
source "$ROOT/packaging/common.sh"
OUT="${1:-$ROOT/dist}"
shift || true
PACKAGES=("${@:-streamlin lindeck linewave}")
read -r -a PACKAGES <<< "${PACKAGES[*]}"
command -v rpmbuild >/dev/null || { echo "rpmbuild fehlt (Paket rpm bzw. rpm-build)" >&2; exit 1; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

# Fedora-Name zuerst, sonst openSUSE ((a or b) = boolesche Abhängigkeit, rpm >= 4.13)
COMMON_REQS=("python3 >= 3.10" "(python3-tkinter or python3-tk)" "(python3-pillow or python3-Pillow)"
             "(python3-pillow-tk or python3-Pillow-tk)" "(dejavu-sans-fonts or dejavu-fonts)" "xdg-utils"
             "(libnotify or libnotify-tools)" "(systemd-udev or udev)" "(procps-ng or procps)" "util-linux")
DECK_REQS=("python3-evdev" "(python3-cairosvg or python3-CairoSVG)" "(hidapi or libhidapi-libusb0)"
           "(libusb1 or libusb-1_0-0)" "xdg-user-dirs" "wl-clipboard" "xclip" "playerctl" "pulseaudio-utils"
           "msitools" "kmod")
WAVE_REQS=("pulseaudio-utils" "(libusb1 or libusb-1_0-0)")
COMMON_RECS=("python3-pystray" "(libayatana-appindicator-gtk3 or typelib-1_0-AyatanaAppIndicator3-0_1)")
DECK_RECS=("(chromium or google-chrome-stable or firefox)" "(mpv or ffmpeg or pipewire-utils or pipewire-tools)"
           "(google-noto-color-emoji-fonts or noto-coloremoji-fonts)" "wmctrl" "(xprop or xorg-x11-utils)" "nodejs")
WAVE_RECS=("wireplumber" "pipewire-pulseaudio" "(pipewire-utils or pipewire-tools)" "ladspa-swh-plugins"
           "(lilv or lilv-tools)" "jalv" "(pipewire-jack-audio-connection-kit or pipewire-libjack-0_3)"
           "(lv2-x42-plugins or x42-plugins)")

spec_lines() {  # spec_lines <Feld> <Wert …> -> eine Zeile je Wert (doppelte nur einmal)
    local field="$1"; shift
    printf '%s\n' "$@" | awk -v f="$field" '!seen[$0]++ { print f ": " $0 }'
}

# ------------------------------------------------------------------ ein Paket
build() {
    NAME="$1"
    package_parts "$NAME"
    STAGE="$(mktemp -d)"
    PKG="$STAGE/root"
    mkdir -p "$PKG/opt/$NAME/streamlin" "$PKG/usr/bin" "$PKG/$UDEV_DIR" "$PKG/etc/xdg/autostart" \
             "$PKG/usr/share/applications" "$PKG/usr/share/doc/$NAME"
    [ "$deck" = 1 ] && mkdir -p "$PKG/usr/lib/modules-load.d"
    stage_files "$NAME" "$deck" "$wave" "usr/share/doc/$NAME"
    find "$PKG" -type d -exec chmod 755 {} +
    find "$PKG" -type f -exec chmod 644 {} +
    chmod 755 "$PKG"/usr/bin/*

    local reqs=("${COMMON_REQS[@]}") recs=("${COMMON_RECS[@]}") rel
    if [ "$deck" = 1 ]; then reqs+=("${DECK_REQS[@]}"); recs+=("${DECK_RECS[@]}"); fi
    if [ "$wave" = 1 ]; then reqs+=("${WAVE_REQS[@]}"); recs+=("${WAVE_RECS[@]}"); fi
    # wie Conflicts/Replaces beim .deb: das eine ersetzt die anderen (dnf entfernt sie dabei)
    case "$NAME" in
        streamlin) rel="Conflicts: lindeck
Conflicts: linewave
Obsoletes: lindeck <= %{version}-%{release}
Obsoletes: linewave <= %{version}-%{release}" ;;
        lindeck) rel="Conflicts: streamlin
Obsoletes: streamlin <= %{version}-%{release}
Suggests: linewave" ;;
        linewave) rel="Conflicts: streamlin
Obsoletes: streamlin <= %{version}-%{release}
Suggests: lindeck" ;;
    esac
    describe "$NAME"
    hooks "$deck" "$wave"

    # Dateiliste: eigene Ordner ganz, sonst einzelne Dateien (fremde Ordner nicht beanspruchen)
    local files
    files="$(cd "$PKG" && { echo "/opt/$NAME"; echo "/usr/share/doc/$NAME"
                           find . -type f ! -path "./opt/*" ! -path "./usr/share/doc/*" | sed 's|^\.||' | sort; } \
             | sed 's|.*|"&"|')"

    cat > "$STAGE/$NAME.spec" <<EOF
%define _build_name_fmt %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm
%define _binary_payload w6.xzdio
%define _build_id_links none
%define debug_package %{nil}
%define __os_install_post %{nil}
%define __brp_python_bytecompile %{nil}

Name: $NAME
Version: $VERSION
Release: 1
Summary: $(head -n1 <<< "$DESC")
License: LicenseRef-Unspecified AND MIT
URL: https://github.com/adblocker-1/Elgato-StreamDeck-for-Linux
BuildArch: noarch
AutoReqProv: no
$(spec_lines Requires "${reqs[@]}")
$(spec_lines Recommends "${recs[@]}")
$rel

%description
$(tail -n +2 <<< "$DESC" | sed 's/^ //')

%install
mkdir -p %{buildroot}
cp -a "$PKG/." %{buildroot}/

%files
%defattr(-,root,root,-)
$files

# Erst nach der ganzen Transaktion starten: bei Update/Wechsel (streamlin <-> lindeck + linewave)
# laufen preun/postun des alten Pakets erst nach post des neuen.
%posttrans
$(post_install)
exit 0

%preun
if [ "\$1" -eq 0 ]; then$HOOK_KILL
fi
exit 0

%postun
udevadm control --reload-rules 2>/dev/null || true
# beim Start entstandene __pycache__-Ordner mit entfernen
if [ "\$1" -eq 0 ]; then
    rm -rf "/opt/$NAME"
fi
exit 0
EOF
    rpmbuild -bb --quiet \
        --define "_topdir $STAGE/rpmbuild" \
        --define "_rpmdir $OUT" \
        --define "_tmppath $STAGE/tmp" \
        "$STAGE/$NAME.spec" >/dev/null
    rm -rf "$STAGE"
    echo "$OUT/$NAME-$VERSION-1.noarch.rpm"
}

for p in "${PACKAGES[@]}"; do
    build "$p"
done
