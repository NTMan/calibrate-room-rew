"""cal_biography: the slot's testimony, tested pure against
the exact field shape that opened the thread -- the liberty
profile with six E.A.R.S takes on one cal. GTK-free."""

import os

from perdeviceeq import measure_build as mb

HERE = os.path.dirname(__file__)

EARS = ("alsa_input.usb-miniDSP_E.A.R.S_Gain__0dB_00002-00"
        ".analog-stereo")
UMIK = "alsa_input.usb-miniDSP_UMIK-1_00001-00.analog-stereo"


def _profile(takes_rigs, name="p"):
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
    return {"name": name,
            "measurement": {"sessions": sessions,
                            "takes": takes,
                            "cal_library": {}}}


def test_single_rig_biography_counts():
    p = _profile([("c1", "E.A.R.S", EARS)] * 6)
    bio = mb.cal_biography([p], "c1")
    assert len(bio) == 1
    assert bio[0]["name"] == "E.A.R.S"
    assert bio[0]["count"] == 6
    assert bio[0]["node_match"] == EARS
    assert bio[0]["profiles"] == {"p": 6}


def test_two_rigs_keep_their_own_counts():
    p = _profile([("c1", "E.A.R.S", EARS)] * 4
                 + [("c1", "Umik-1", UMIK)] * 2)
    bio = mb.cal_biography([p], "c1")
    assert [(e["name"], e["count"]) for e in bio] == [
        ("E.A.R.S", 4), ("Umik-1", 2)]


def test_biography_is_house_wide():
    a = _profile([("c1", "E.A.R.S", EARS)] * 3, name="liberty")
    b = _profile([("c1", "E.A.R.S", EARS)] * 3, name="origin")
    bio = mb.cal_biography([a, b], "c1")
    assert bio[0]["count"] == 6
    assert bio[0]["profiles"] == {"liberty": 3, "origin": 3}


def test_cached_sha_survives_resyncs(tmp_path):
    f = tmp_path / "cal.txt"
    f.write_text("20 0.0\n")
    s1 = mb.cal_sha_cached(str(f))
    s2 = mb.cal_sha_cached(str(f))
    assert s1 == s2
    f.write_text("20 0.0\n20000 -1.0\n")
    assert mb.cal_sha_cached(str(f)) != s1


def test_foreign_sha_has_no_biography():
    p = _profile([("c1", "E.A.R.S", EARS)] * 6)
    assert mb.cal_biography([p], "other") == []


def test_severity_speaks_the_architects_examples():
    # one incident against one other profile: calm green
    assert mb.badge_severity(1, 1) == "green"
    # first-time pairing vs a dominant foreign history: red
    assert mb.badge_severity(0, 6) == "red"
    # mixed history: amber
    assert mb.badge_severity(2, 3) == "amber"
    # a lone foreign profile is green even when unprecedented
    assert mb.badge_severity(0, 1) == "green"
    # no foreign history: no cloud at all
    assert mb.badge_severity(3, 0) is None


def test_empty_house_stays_silent():
    assert mb.cal_biography([], "c1") == []
    assert mb.cal_biography(None, "c1") == []
