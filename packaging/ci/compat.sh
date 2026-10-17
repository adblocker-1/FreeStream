#!/bin/sh
# Tests und Paket in einem älteren Debian/Ubuntu (CI: debian:12, ubuntu:22.04).
# Aufruf im Container aus dem Quellordner: sh packaging/ci/compat.sh
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-pil python3-pil.imagetk python3-tk python3-evdev \
  python3-cairosvg xvfb xauth fonts-dejavu-core dpkg-dev procps >/dev/null
python3 --version
# pip ab Debian 12 verlangt --break-system-packages, Ubuntu 22.04 kennt die Option noch nicht
pip3 install -q --break-system-packages "streamdeck>=0.10" pyflakes pytest 2>/dev/null \
  || pip3 install -q "streamdeck>=0.10" pyflakes pytest
python3 -m pyflakes freestream tests tools packaging
xvfb-run -a python3 -m pytest -q -p no:cacheprovider tests
./packaging/build-deb.sh /tmp/dist >/dev/null
# lässt sich das Paket mit den Abhängigkeiten dieses Systems installieren?
apt-get install -y -qq /tmp/dist/freestream_*.deb >/dev/null
freedeck --version
freedeck profiles
freewave mixer --help >/dev/null
echo "OK: $(. /etc/os-release; echo "$PRETTY_NAME")"
