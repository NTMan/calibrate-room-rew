%global appid    io.github.ntman.PerDeviceEQ
%global reponame calibrate-room-rew

Name:           per-device-eq
Version:        1.0.1
Release:        %autorelease
Summary:        Per-output-device parametric EQ for PipeWire

# The application code is GPL-3.0-or-later; confirm and ship a LICENSE file.
License:        GPL-3.0-or-later
URL:            https://github.com/NTMan/%{reponame}
Source0:        %{url}/archive/v%{version}/%{reponame}-%{version}.tar.gz

BuildArch:      noarch

# validation only (no compilation: pure Python + data)
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib

Requires:       python3
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       pipewire
Requires:       pipewire-utils
Requires:       wireplumber
Requires:       hicolor-icon-theme

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
# nothing to build

%install
# main executable (installed without the .py extension; Exec=per-device-eq)
install -Dpm0755 per-device-eq.py %{buildroot}%{_bindir}/%{name}

# WirePlumber hook, shipped as data; the app installs it per-user on first run
install -Dpm0644 wireplumber/90-per-device-eq.lua \
        %{buildroot}%{_datadir}/%{name}/wireplumber/90-per-device-eq.lua

# desktop entry + icon + AppStream metadata
install -Dpm0644 data/%{appid}.desktop \
        %{buildroot}%{_datadir}/applications/%{appid}.desktop
install -Dpm0644 data/icons/hicolor/scalable/apps/%{appid}.svg \
        %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
install -Dpm0644 data/%{appid}.metainfo.xml \
        %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml

%files
%license LICENSE
%doc README.md README.ru.md
%{_bindir}/%{name}
%dir %{_datadir}/%{name}
%dir %{_datadir}/%{name}/wireplumber
%{_datadir}/%{name}/wireplumber/90-per-device-eq.lua
%{_datadir}/applications/%{appid}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{appid}.svg
%{_metainfodir}/%{appid}.metainfo.xml

%changelog
%autochangelog