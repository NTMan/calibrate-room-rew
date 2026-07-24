"""cal_biography: the slot's testimony, tested pure against
the exact field shape that opened the thread -- the liberty
profile with six E.A.R.S takes on one cal. GTK-free."""

import os

from perdeviceeq import measure_build as mb

HERE = os.path.dirname(__file__)

EARS = ("alsa_input.usb-miniDSP_E.A.R.S_Gain__0dB_00002-00"
        ".analog-stereo")
UMIK = "alsa_input.usb-miniDSP_UMIK-1_00001-00.analog-stereo"


def _profile(takes_rigs):
    """A minimal profile: one session per distinct rig, takes
    spread over them, all on one cal sha."""
    sessions = {}
    takes = []
    for i, (sha, rig_name, node) in enumerate(takes_rigs):
        sid = "s-" + rig_name
        sessions.setdefault(sid, {"source": {
            "name": rig_name, "node_match": node,
            "serial": "", "channels": 2}})
        takes.append({"id": "t%d" % i, "channel": "FL",
                      "session": sid, "cal_sha": sha})
    return {"measurement": {"sessions": sessions,
                            "takes": takes,
                            "cal_library": {}}}


def test_single_rig_biography_counts():
    p = _profile([("c1", "E.A.R.S", EARS)] * 6)
    bio = mb.cal_biography([p], "c1")
    assert len(bio) == 1
    assert bio[0]["name"] == "E.A.R.S"
    assert bio[0]["count"] == 6
    assert bio[0]["node_match"] == EARS


def test_two_rigs_keep_their_own_counts():
    p = _profile([("c1", "E.A.R.S", EARS)] * 4
                 + [("c1", "Umik-1", UMIK)] * 2)
    bio = mb.cal_biography([p], "c1")
    assert [(e["name"], e["count"]) for e in bio] == [
        ("E.A.R.S", 4), ("Umik-1", 2)]


def test_biography_is_house_wide():
    a = _profile([("c1", "E.A.R.S", EARS)] * 3)
    b = _profile([("c1", "E.A.R.S", EARS)] * 3)
    bio = mb.cal_biography([a, b], "c1")
    assert bio[0]["count"] == 6


def test_foreign_sha_has_no_biography():
    p = _profile([("c1", "E.A.R.S", EARS)] * 6)
    assert mb.cal_biography([p], "other") == []


def test_empty_house_stays_silent():
    assert mb.cal_biography([], "c1") == []
    assert mb.cal_biography(None, "c1") == []
