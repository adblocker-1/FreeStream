#!/usr/bin/env bash
# Testet die RPM-Pakete in einem Fedora- oder openSUSE-Container (Aufruf aus der CI):
#   docker run --rm -v "$PWD/dist:/dist:ro" -v "$PWD/packaging:/p:ro" quay.io/fedora/fedora:latest bash /p/test-rpm.sh /dist
set -euxo pipefail
D="${1:-/dist}"
if command -v dnf >/dev/null; then
    inst() { dnf install -y --setopt=install_weak_deps=False "$@"; }
    uninst() { dnf remove -y "$@"; }
else
    inst() { zypper --non-interactive install --allow-unsigned-rpm --no-recommends "$@"; }
    uninst() { zypper --non-interactive remove "$@"; }
fi
not_installed() { if rpm -q "$1"; then echo "$1 ist noch installiert" >&2; exit 1; fi; }

# beide Programme in einem Paket
inst "$D"/streamlin-*.noarch.rpm
lindeck --version
lindeck profiles
linewave mixer --help
test -f /usr/lib/udev/rules.d/70-streamlin.rules
test -f /usr/share/applications/lindeck.desktop -a -f /usr/share/applications/linewave.desktop
# Container-Images installieren keine Dokumentation (tsflags=nodocs): im Paketinhalt prüfen
rpm -ql streamlin | grep /usr/share/doc/streamlin/LICENSE.python-elgato-streamdeck >/dev/null

# einzeln: ersetzt das Paket streamlin, beide arbeiten zusammen
inst "$D"/lindeck-*.noarch.rpm "$D"/linewave-*.noarch.rpm
not_installed streamlin
test ! -e /opt/streamlin
lindeck --version
lindeck mixer --help
linewave mixer --help

# nur LinDeck: Linewave-Befehle melden sich als nicht installiert
uninst linewave
test ! -e /opt/linewave
lindeck profiles
if lindeck mixer; then echo "lindeck mixer geht ohne Linewave" >&2; exit 1; fi

# und zurück zum Paket streamlin
inst "$D"/streamlin-*.noarch.rpm
not_installed lindeck
lindeck --version
uninst streamlin
test ! -e /opt/streamlin
echo "RPM-Test bestanden"
