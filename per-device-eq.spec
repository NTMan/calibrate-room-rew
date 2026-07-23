%global appid    io.github.ntman.PerDeviceEQ
%global reponame PerDeviceEQ

Name:           per-device-eq
Version:        4.0.0
Release:        %autorelease
Summary:        Per-output-device parametric EQ for PipeWire

License:        GPL-3.0-or-later
URL:            https://github.com/NTMan/%{reponame}
Source0:        %{url}/archive/v%{version}/%{reponame}-%{version}.tar.gz

BuildArch:      noarch

# python3-devel: interpreter for the import check + python-rpm-macros
# (provides the py_byte_compile macro used at install time)
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

Requires:       python3
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       libadwaita
Requires:       pipewire
Requires:       pipewire-utils
Requires:       wireplumber
Requires:       hicolor-icon-theme
# measurement wizard math (perdeviceeq.measure_core/measure_session,
# fit_peq, pde_audit); the core EQ app runs without them
Recommends:     python3-numpy
Recommends:     python3-scipy
Recommends:     python3-soundfile

%description
Per-Device EQ applies a parametric equaliser directly inside the real PipeWire
sink, so every output device keeps its own correction. The EQ is written as an
in-node filter graph (no extra virtual sink) and is restored automatically by a
WirePlumber hook, surviving reboot and device reconnect with no background
process running. It applies to every application, including pro-audio tools
that open the device directly.

%prep
%autosetup -n %{reponame}-%{version}

%build
# nothing to build (pure Python + data)

%install
# thin launcher (installed without the .py extension; Exec=per-device-eq).
# It locates the perdeviceeq package below at /usr/share/per-device-eq.
install -Dpm0755 per-device-eq.py %{buildroot}%{_bindir}/%{name}

# Python implementation package, imported by the launcher
install -d %{buildroot}%{_datadir}/%{name}/perdeviceeq
install -pm0644 -t %{buildroot}%{_datadir}/%{name}/perdeviceeq perdeviceeq/*.py
# byte-compile so .pyc ship next to the .py (Fedora-proper)
%py_byte_compile %{python3} %{buildroot}%{_datadir}/%{name}/perdeviceeq

# GtkBuilder design for the GUI (resolved at runtime via the package data root)
install -Dpm0644 data/%{appid}.ui \
        %{buildroot}%{_datadir}/%{name}/data/%{appid}.ui
install -Dpm0644 data/%{appid}.Measure.ui \
        %{buildroot}%{_datadir}/%{name}/data/%{appid}.Measure.ui

# WirePlumber hook, shipped as data; the app installs it per-user on first run
# The hook SOURCE only: a package must not touch user homes.
# Activation is per-user, done by the app as the user (first-run
# dialog, the menu switch, or --install) into ~/.local/share and
# ~/.config -- deliberately not system-activated.
install -Dpm0644 wireplumber/90-per-device-eq.lua \
        %{buildroot}%{_datadir}/%{name}/wireplumber/90-per-device-eq.lua

# desktop entry + icon + AppStream metadata
install -Dpm0644 data/%{appid}.desktop \
        %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -Dpm0644 data/icons/hicolor/scalable/apps/%{appid}.svg \
        %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
install -Dpm0644 data/icons/pde-level-symbolic.svg \
        %{buildroot}%{_datadir}/icons/hicolor/scalable/actions/pde-level-symbolic.svg
install -Dpm0644 data/%{appid}.metainfo.xml \
        %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml
# import check: the package must import cleanly (no GTK needed here);
# the .pyc were produced by the byte-compile step above and are packaged.
%{python3} -c "import sys; sys.path.insert(0, '%{buildroot}%{_datadir}/%{name}'); \
import perdeviceeq.config, perdeviceeq.eq, perdeviceeq.profiles, \
perdeviceeq.pipewire, perdeviceeq.integration, perdeviceeq.cli, \
perdeviceeq.measure_prefs"

# On full removal (not upgrade), tell the user what the package
# cannot do for them: the per-user integration, if installed, is
# outside any package's reach. Knowledge at the moment it is
# needed most; printing is all a scriptlet may do here.
%preun
if [ "$1" -eq 0 ]; then
    cat << 'MSG'
per-device-eq: the per-user WirePlumber integration (if you
installed it) is NOT removed with the package. To remove it:
  rm ~/.local/share/wireplumber/scripts/90-per-device-eq.lua
  rm ~/.config/wireplumber/wireplumber.conf.d/90-per-device-eq.conf
  systemctl --user restart wireplumber
MSG
fi

%files
%license LICENSE
%doc README.md README.ru.md
%{_bindir}/%{name}
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/perdeviceeq/
%dir %{_datadir}/%{name}/data
%{_datadir}/%{name}/data/%{appid}.ui
%{_datadir}/%{name}/data/%{appid}.Measure.ui
%dir %{_datadir}/%{name}/wireplumber
%{_datadir}/%{name}/wireplumber/90-per-device-eq.lua
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_datadir}/icons/hicolor/scalable/actions/pde-level-symbolic.svg
%{_metainfodir}/%{appid}.metainfo.xml

%changelog
%autochangelog
