#!/usr/bin/env bash
# Baut das Flatpak und das Bündel streamlin-<version>.flatpak
# (Aufruf: ./packaging/flatpak/build-flatpak.sh [Ausgabeordner]).
# Braucht flatpak und flatpak-builder sowie die Laufzeit von Flathub:
#   flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
#   flatpak install --user flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08
# Zwischenstände liegen in .flatpak-build/ (FLATPAK_BUILD_DIR), der Download-Zwischenspeicher
# in .flatpak-build/state wird beim nächsten Bau wiederverwendet.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP_ID=io.github.adblocker1.Streamlin
VERSION=$(python3 -c "import sys;sys.path.insert(0,'$ROOT');import streamlin;print(streamlin.__version__)")
OUT="${1:-$ROOT/dist}"
BUILD="${FLATPAK_BUILD_DIR:-$ROOT/.flatpak-build}"
mkdir -p "$OUT" "$BUILD"

cd "$ROOT"
flatpak-builder --user --install-deps-from=flathub --force-clean --disable-rofiles-fuse \
    --state-dir="$BUILD/state" --repo="$BUILD/repo" "$BUILD/app" "packaging/flatpak/$APP_ID.yml"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
    "$BUILD/repo" "$OUT/streamlin-$VERSION.flatpak" "$APP_ID"
echo "$OUT/streamlin-$VERSION.flatpak"
