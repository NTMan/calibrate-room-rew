"""The device floor: Taste asks, the zone disposes. Pure,
GTK-free -- the LR8 arithmetic, the gate, and the seal."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perdeviceeq import eq


def _floored(lo=38.3):
    return {"preamp": 0.0, "apply_all": True,
            "all": {"bands": []},
            "fit": {"params": {"f_lo": lo}}}


def test_floor_is_four_hp_stages_at_the_zone_edge():
    fb = eq.floor_bands(_floored())
    assert [b["type"] for b in fb] == ["HP"] * 4
    assert all(b["freq"] == 38.3 for b in fb)
    assert [b["q"] for b in fb] == list(eq.FLOOR_QS)


def test_lr8_signature_minus_six_at_the_edge():
    fb = [eq.Band.from_dict(b) for b in eq.floor_bands(_floored())]
    edge = eq.response_db(0.0, fb, [38.3])[0]
    assert -7.0 < edge < -5.0


def test_below_forty_means_nothing():
    fb = [eq.Band.from_dict(b) for b in eq.floor_bands(_floored())]
    assert eq.response_db(0.0, fb, [20.0])[0] < -40.0


def test_the_zone_itself_is_untouched():
    fb = [eq.Band.from_dict(b) for b in eq.floor_bands(_floored())]
    assert abs(eq.response_db(0.0, fb, [383.0])[0]) < 0.2


def test_the_zone_outranks_the_fit_range():
    # the architect's field case: fit made with the 20.0
    # default while the measured band starts at 38.3
    p = {"fit": {"params": {"f_lo": 20.0},
                 "zone": {"lo": 38.3, "hi": 20000.0}}}
    fb = eq.floor_bands(p)
    assert len(fb) == 4 and fb[0]["freq"] == 38.3


def test_a_deep_zone_gates_even_a_high_fit_range():
    p = {"fit": {"params": {"f_lo": 38.3},
                 "zone": {"lo": 20.0, "hi": 20000.0}}}
    assert eq.floor_bands(p) == []


def test_deep_zones_get_no_floor():
    assert eq.floor_bands(_floored(lo=20.0)) == []
    assert eq.floor_bands({}) == []
    assert eq.floor_bands(None) == []


def test_the_graph_wears_the_floor_sealed():
    g = eq.profile_graph(_floored())
    assert g.count("bq_highpass") == 4
    bare = eq.profile_graph(_floored(lo=20.0))
    assert "bq_highpass" not in bare


def test_taste_rides_behind_the_floor():
    taste = [{"type": "PK", "freq": 60.0, "gain": 4.0, "q": 1.0}]
    g = eq.profile_graph(_floored(), extra=taste)
    assert g.count("bq_highpass") == 4
    assert "freq = 60" in g
