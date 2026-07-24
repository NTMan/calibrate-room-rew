#!/bin/sh -e
# Assemble the AppDir by hand on Fedora -- no appimage-builder.
# Needs: dnf, glib2, gdk-pixbuf2 in the build container.
# (soundfile is an rpm from Fedora 43 on; if a future base
# ever lacks a python dep, the fallback road is a pip --target
# wheel into the AppDir site-packages -- see git history.)
#
# The old recipe leaned on appimage-builder, whose package
# harvest exists only in apt shape and whose apt shape requires
# the long-removed apt-key: the tool chose Ubuntu for us and
# then died on modern apt. This script is the replacement: a
# plain dnf --installroot harvest on the OLDEST SUPPORTED
# Fedora, run INSIDE that same container, from
# packaging/appimage/.
#
# BASE POLICY (the architect's lifecycle point): Fedora lives
# on a six-month cadence and each release EOLs ~13 months in,
# so no pinned number survives -- the base tracks the oldest
# supported release, bumped once per cycle when N-2 retires.
# Oldest-supported rather than latest because the base IS the
# glibc floor: latest would shut out every host still on the
# previous release. Today that is 43 (libadwaita 1.7, floor
# glibc 2.42: Fedora 43+, Ubuntu 25.10+). Bump HERE, in
# build.sh and in the workflow container together.
#
# The floor is the HOST loader's: the glibc family is pruned
# from the AppDir after the harvest, so the artifact runs on
# any distro whose glibc >= the build's. PipeWire stays host
# territory by design.

APPDIR="$PWD/AppDir"
RELEASEVER="${RELEASEVER:-43}"

rm -rf "$APPDIR"
mkdir -p "$APPDIR"

# gobject-introspection is load-bearing, not tooling: since
# glib2 >= 2.80 the GLib/GObject/Gio typelibs live in glib2
# itself, but cairo-1.0.typelib and the freedesktop base set
# stayed in gobject-introspection -- without it the image
# leans on the HOST's copy through GI_TYPELIB_PATH's default
# fallback, which every developer machine has and a bare
# container does not (the GUI smoke's first catch).
dnf -y install --installroot="$APPDIR" \
    --releasever="$RELEASEVER" \
    --setopt=install_weak_deps=False \
    --setopt=reposdir=/etc/yum.repos.d \
    python3 python3-gobject python3-cairo \
    python3-numpy python3-scipy python3-soundfile \
    gtk4 libadwaita librsvg2 \
    adwaita-icon-theme shared-mime-info glib2 \
    gobject-introspection

# the app, in the installed layout the launcher already
# searches: <prefix>/share/per-device-eq, data checkout-shaped
mkdir -p "$APPDIR/usr/share/per-device-eq"
cp -r ../../perdeviceeq "$APPDIR/usr/share/per-device-eq/"
cp -r ../../data "$APPDIR/usr/share/per-device-eq/"
install -m 755 ../../per-device-eq.py \
    "$APPDIR/usr/share/per-device-eq/per-device-eq.py"
mkdir -p "$APPDIR/usr/share/applications"
cp ../../data/io.github.ntman.PerDeviceEQ.desktop \
    "$APPDIR/usr/share/applications/"
mkdir -p "$APPDIR/usr/share/icons"
cp -r ../../data/icons/hicolor "$APPDIR/usr/share/icons/"

# caches the libraries expect, regenerated against the AppDir
if [ -x "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" ]; then
    LOADERS="$(ls -d "$APPDIR"/usr/lib64/gdk-pixbuf-2.0/*/loaders)"
    GDK_PIXBUF_MODULEDIR="$LOADERS" \
        "$APPDIR/usr/bin/gdk-pixbuf-query-loaders" \
        > "$LOADERS.cache"
fi
if [ -d "$APPDIR/usr/share/glib-2.0/schemas" ]; then
    glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
fi

# THE HOST-FAMILIES LAW (field lesson: a bundled F43
# libfontconfig parsing a newer host /etc/fonts spewed
# warnings, and the mixed GL stack -- host mesa dlopened
# against our older libdrm/glvnd underneath -- segfaulted the
# window). Some families MUST come from the host because they
# are joined at the hip to host state: the dynamic loader and
# glibc (the floor), the whole GPU platform (mesa follows the
# host's kernel and hardware), and the font platform
# (fontconfig follows the host's /etc/fonts syntax). Pruning
# them here is what makes LD_LIBRARY_PATH safe: whatever
# remains in usr/lib64 shadows nothing it cannot honor.

# the glibc family: the floor is the host loader's
rm -f "$APPDIR"/usr/lib64/ld-linux-* \
      "$APPDIR"/usr/lib64/libc.so* \
      "$APPDIR"/usr/lib64/libm.so* \
      "$APPDIR"/usr/lib64/libdl.so* \
      "$APPDIR"/usr/lib64/libpthread.so* \
      "$APPDIR"/usr/lib64/librt.so* \
      "$APPDIR"/usr/lib64/libresolv.so* \
      "$APPDIR"/usr/lib64/libnss_*
rm -rf "$APPDIR/lib" "$APPDIR/lib64" 2>/dev/null || true

# the C++ runtime rides with the GPU family: the host's mesa,
# once dlopened, resolves ITS OWN needs through our
# LD_LIBRARY_PATH first -- an old bundled libstdc++ lacks the
# GLIBCXX symbols radeonsi asks for, and that is the half of
# the segfault the GPU pruning alone does not kill
rm -f "$APPDIR"/usr/lib64/libstdc++.so* \
      "$APPDIR"/usr/lib64/libgcc_s.so*

# the GPU platform: DRIVERS GO, DISPATCH STAYS. What faces
# the kernel and the hardware -- dri drivers, gbm, libdrm,
# mesa's glapi -- belongs to the host that owns them (that
# pairing was the original segfault). The neutral dispatch
# loaders -- the glvnd quartet and the vulkan loader -- stay
# BUNDLED: they are built to tolerate version skew, they find
# the host's ICDs through XDG_DATA_DIRS, and our own
# libgtk-4.so.1 DT_NEEDs libvulkan outright, so pruning the
# loader killed GTK on any host without its own copy
# (field-caught by the GUI smoke in the bare container).
rm -rf "$APPDIR"/usr/lib64/dri "$APPDIR"/usr/lib64/gbm
rm -f "$APPDIR"/usr/lib64/libgbm.so* \
      "$APPDIR"/usr/lib64/libdrm*.so* \
      "$APPDIR"/usr/lib64/libglapi.so*

# the font and display-client platform: fontconfig reads the
# HOST /etc/fonts, so the host's own library must be the one
# reading it; freetype and the X/Wayland client libs travel
# with it
rm -f "$APPDIR"/usr/lib64/libfontconfig.so* \
      "$APPDIR"/usr/lib64/libfreetype.so* \
      "$APPDIR"/usr/lib64/libX11*.so* \
      "$APPDIR"/usr/lib64/libxcb*.so* \
      "$APPDIR"/usr/lib64/libwayland-*.so*
rm -rf "$APPDIR"/usr/etc/fonts "$APPDIR"/etc/fonts \
       "$APPDIR"/usr/share/fontconfig "$APPDIR"/usr/share/fonts

# weight nobody runs
rm -rf "$APPDIR"/usr/share/man "$APPDIR"/usr/share/doc \
       "$APPDIR"/usr/share/info "$APPDIR"/usr/share/locale \
       "$APPDIR"/var/cache "$APPDIR"/var/log \
       "$APPDIR"/var/lib/dnf "$APPDIR"/usr/lib/sysimage

# AppImage front matter: AppRun + desktop + icon at the root
install -m 755 AppRun "$APPDIR/AppRun"
cp ../../data/io.github.ntman.PerDeviceEQ.desktop "$APPDIR/"
ICON="$(find "$APPDIR/usr/share/icons/hicolor" \
        -name 'io.github.ntman.PerDeviceEQ.*' | head -1)"
cp "$ICON" "$APPDIR/"

echo "AppDir ready: $APPDIR"
