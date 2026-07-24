#!/bin/sh -e
# Local AppImage build, container-shaped exactly like CI: the
# recipe harvests plucky debs, so the build must run INSIDE
# ubuntu:25.04 regardless of the host. Needs podman or docker.
#   packaging/appimage/build.sh [VERSION]
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VER="${1:-$(cd "$ROOT" && git describe --tags --always | sed 's/^v//')}"
RUN="$(command -v podman || command -v docker)"
exec "$RUN" run --rm -it \
    -v "$ROOT:/src" -w /src/packaging/appimage \
    -e APP_VERSION="$VER" -e APPIMAGE_EXTRACT_AND_RUN=1 \
    -e DEBIAN_FRONTEND=noninteractive \
    ubuntu:25.04 sh -ec '
        apt-get update
        apt-get install -y --no-install-recommends \
            python3-pip python3-setuptools binutils fakeroot \
            patchelf file wget zsync squashfs-tools \
            desktop-file-utils gtk-update-icon-cache git \
            ca-certificates
        pip3 install --break-system-packages appimage-builder
        appimage-builder --recipe AppImageBuilder.yml --skip-test
        ./Per*.AppImage --list-profiles
        ls -l *.AppImage*'
