#!/bin/sh -e
# Local AppImage build, container-shaped exactly like CI: the
# harvest is dnf on fedora:42, so the build runs INSIDE that
# image regardless of the host. Needs podman or docker.
#   packaging/appimage/build.sh [VERSION]
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VER="${1:-$(cd "$ROOT" && git describe --tags --always | sed 's/^v//')}"
RUN="$(command -v podman || command -v docker)"
exec "$RUN" run --rm -it \
    -v "$ROOT:/src" -w /src/packaging/appimage \
    -e APP_VERSION="$VER" -e APPIMAGE_EXTRACT_AND_RUN=1 \
    fedora:43 sh -ec '
        dnf -y install glib2 gdk-pixbuf2 file binutils wget \
            zsync git
        ./build-appdir.sh
        wget -q -O appimagetool \
            https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
        chmod +x appimagetool
        ARCH=x86_64 ./appimagetool \
            -u "gh-releases-zsync|NTMan|PerDeviceEQ|latest|PerDeviceEQ-*x86_64.AppImage.zsync" \
            AppDir "PerDeviceEQ-${APP_VERSION}-x86_64.AppImage"
        ./PerDeviceEQ-*.AppImage --list-profiles
        ls -l PerDeviceEQ-*'
