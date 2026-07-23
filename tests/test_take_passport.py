"""The take passport's identity law, run against the REAL
function (unbound, a stub for self) over the exact data shape
the field shipped: the liberty profile, E.A.R.S-stamped
sessions, a poisoned mic-store serial. Needs gi (the module
imports GTK at top), so the GTK-less sandbox skips and CI with
xvfb judges."""

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as e:      # gi without the typelibs
    pytest.skip(str(e), allow_module_level=True)

from perdeviceeq import measure_window as mw  # noqa: E402

EARS = ("alsa_input.usb-miniDSP_E.A.R.S_Gain__0dB_00002-00"
        ".analog-stereo")
EARS2 = ("alsa_input.usb-miniDSP_E.A.R.S_Gain__0dB_00003-00"
         ".analog-stereo")
UMIK = "alsa_input.usb-miniDSP_UMIK-1_00001-00.analog-stereo"

PROFILE = {
    "id": "p1",
    "measurement": {
        "sessions": {"s1": {"source": {
            "name": "E.A.R.S Gain   0dB   Analog Stereo",
            "serial": "8603052", "node_match": EARS,
            "channels": 2}}},
        "takes": [{"id": "t1", "channel": "FL", "session": "s1",
                   "cal_sha": None}],
        "cal_library": {},
    },
}


class _Store:
    def get(self, _pid):
        return PROFILE


class _Obj:
    pass


def _stub(serial, node):
    s = _Obj()
    s.edit_pid = "p1"
    s.parent = _Obj()
    s.parent.store = _Store()
    s._canvas_ids = {}
    s.mic_picker = _Obj()
    s.mic_picker.core = _Obj()
    s.mic_picker.core.node = node
    s._source_info = lambda: {"name": "rig", "serial": serial}
    return s


class _Rec:
    id = "t1"


def _group(serial, node):
    g, _tip = mw.MeasureWindow._take_passport(
        _stub(serial, node), "FL", _Rec())
    return g


def test_poisoned_store_serial_cannot_veto_the_node():
    # the field bug: the Umik's store entry carried the E.A.R.S
    # serial; equal serials, different instruments
    assert _group("8603052", UMIK) is not None


def test_replugged_rig_stays_itself():
    # same instrument, another usb port: node tail differs,
    # serials agree -- the home team wears no header
    assert _group("8603052", EARS2) is None


def test_twin_models_are_told_apart_by_serial():
    # same node identity, different serials: two units of one
    # model -- foreign
    assert _group("9999999", EARS2) is not None


def test_the_plain_native_case_stays_headerless():
    assert _group("8603052", EARS) is None
