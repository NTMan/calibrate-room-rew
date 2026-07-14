"""The open-time trust report (perdeviceeq/trust.py).

Synthetic canvases with known statistics pin the controlled band
(spread bound + 1/6-octave edge scan + sweep coverage) and every
score ingredient: clean-take base, spread, SNR, age and fit-range
coverage. The shared statistics themselves (spread_trust_bound,
trusted_band_hz) are pinned here at the module level and guarded
against the live session by test_measure_session.
"""
from datetime import datetime, timedelta

import numpy as np

from perdeviceeq import trust
from perdeviceeq import measure_core as mc
from perdeviceeq import measure_session as ms

GRID = {"f_lo": 20.0, "f_hi": 20000.0, "ppo": 96}
FREQS = mc.log_grid()
N = len(FREQS)
T0 = "2026-07-10T12:00:00+00:00"
NOW = datetime.fromisoformat(T0) + timedelta(days=1)


def _take(tid, key, mag, snr=45.0, peak=-6.0, clipped=0,
          created=T0, col=0):
    return {"id": tid, "session": "s1", "channel": key,
            "capture_channel": col, "created_utc": created,
            "mag_db_uncal": [float(v) for v in mag],
            "delay_ms": 5.0, "snr_db": snr, "peak_dbfs": peak,
            "noise_dbfs": -80.0, "clipped": clipped, "repaired": 0,
            "chan_vol": 0.3, "soft_vol": 0.3}


def _meas(takes, f_start=40.0, f_end=18000.0):
    return {"grid": dict(GRID),
            "source": {"name": "rig", "serial": "", "node_match": "n",
                       "channels": 2, "cal": {}},
            "sessions": {"s1": {"created_utc": T0,
                                "sweep": {"f_start": f_start,
                                          "f_end": f_end}}},
            "takes": takes}


def _hf_break(db, f_edge=8000.0):
    m = np.zeros(N)
    m[FREQS > f_edge] = db
    return m


def _clean_trio(key="FL", **kw):
    return [_take("%s%d" % (key, i), key, np.zeros(N), **kw)
            for i in range(3)]


def test_statistics_helpers_are_pinned():
    # the confidence multipliers quoted everywhere: x2.42 / x1.61
    two = ms.spread_trust_bound(np.ones(4), 2)
    three = ms.spread_trust_bound(np.ones(4), 3)
    assert abs(two[0] - 2.42) < 0.01
    assert abs(three[0] - 1.61) < 0.01
    # a trusted island shorter than 1/6 octave never becomes the edge
    ok = np.zeros(N, bool)
    ok[:600] = True          # the real band
    ok[610:612] = True       # a two-point perch above a gap
    floor, ceiling = ms.trusted_band_hz(FREQS, ok)
    assert floor == FREQS[0]
    assert ceiling == float(FREQS[599])
    # no qualifying run degenerates to floor >= ceiling
    floor, ceiling = ms.trusted_band_hz(FREQS, np.zeros(N, bool))
    assert floor >= ceiling


def test_three_clean_tight_takes_score_100():
    rep = trust.assess({"measurement": _meas(_clean_trio())},
                       now=NOW)
    assert rep["score"] == 100
    ch = rep["channels"]["FL"]
    assert ch["score"] == 100 and ch["reasons"] == []
    # the band is the sweep coverage: the statistics trust everything
    assert ch["band"] == (40.0, 18000.0)
    assert rep["band"] == (40.0, 18000.0)
    assert ch["coverage"] == (40.0, 18000.0)
    assert rep["newest_utc"] is not None


def test_hf_disagreement_lowers_the_ceiling_not_the_score():
    takes = [_take("a", "FL", _hf_break(0.0)),
             _take("b", "FL", _hf_break(+4.0)),
             _take("c", "FL", _hf_break(-4.0))]
    rep = trust.assess({"measurement": _meas(takes)}, now=NOW)
    ch = rep["channels"]["FL"]
    assert 7000.0 < ch["band"][1] <= 8000.0
    assert ch["band"][0] == 40.0
    # in-band the takes agree perfectly: the score stays clean
    assert ch["score"] == 100


def test_take_count_sets_the_base():
    one = trust.assess({"measurement": _meas(_clean_trio()[:1])},
                       now=NOW)
    two = trust.assess({"measurement": _meas(_clean_trio()[:2])},
                       now=NOW)
    assert one["score"] == 45 and two["score"] == 70
    ch = one["channels"]["FL"]
    assert ch["band"] is None
    assert any("fewer than two takes" in r for r in ch["reasons"])
    assert two["channels"]["FL"]["band"] == (40.0, 18000.0)


def test_clipped_take_drops_the_clean_count():
    takes = _clean_trio()
    takes[2] = _take("FL2", "FL", np.zeros(N), clipped=3)
    rep = trust.assess({"measurement": _meas(takes)}, now=NOW)
    ch = rep["channels"]["FL"]
    assert ch["n_clean"] == 2 and ch["n_clipped"] == 1
    assert ch["score"] == 70
    assert any("clipped" in r for r in ch["reasons"])


def test_low_snr_flags_and_penalizes():
    takes = _clean_trio()
    takes[2]["snr_db"] = 25.0            # flagged AND the worst SNR
    rep = trust.assess({"measurement": _meas(takes)}, now=NOW)
    ch = rep["channels"]["FL"]
    assert ch["n_clean"] == 2 and ch["n_flagged"] == 1
    f = trust._linear_factor(25.0, mc.SNR_WARN_DB,
                             mc.SNR_WARN_DB - trust.SNR_SPAN_DB,
                             trust.SNR_MIN_FACTOR)
    assert ch["score"] == int(round(70 * f))
    assert any("SNR" in r for r in ch["reasons"])


def test_age_decays_gently():
    old = (datetime.fromisoformat(T0)
           - timedelta(days=400)).isoformat(timespec="seconds")
    rep = trust.assess(
        {"measurement": _meas(_clean_trio(created=old))}, now=NOW)
    ch = rep["channels"]["FL"]
    assert 80 <= ch["score"] < 100
    assert any("days old" in r for r in ch["reasons"])
    assert ch["age_days"] > 400


def test_fit_reaching_past_the_band_costs():
    takes = [_take("a", "FL", _hf_break(0.0)),
             _take("b", "FL", _hf_break(+4.0)),
             _take("c", "FL", _hf_break(-4.0))]
    prof = {"measurement": _meas(takes),
            "fit": {"params": {"bands": 10, "f_lo": 20.0,
                               "f_hi": 12000.0, "max_boost": 6.0,
                               "smoothing": 6, "mono": False}}}
    rep = trust.assess(prof, now=NOW)
    ch = rep["channels"]["FL"]
    assert ch["score"] < 100
    assert any("reaches past" in r for r in ch["reasons"])
    # trimming the fit to the certified band restores the score
    prof["fit"]["params"]["f_hi"] = 8000.0
    prof["fit"]["params"]["f_lo"] = 40.0
    assert trust.assess(prof, now=NOW)["channels"]["FL"][
        "score"] == 100


def test_profile_band_is_the_worst_channel():
    fl = _clean_trio("FL")
    fr = [_take("FR0", "FR", _hf_break(0.0), col=1),
          _take("FR1", "FR", _hf_break(4.0), col=1),
          _take("FR2", "FR", _hf_break(-4.0), col=1)]
    rep = trust.assess({"measurement": _meas(fl + fr)}, now=NOW)
    assert rep["channels"]["FL"]["band"] == (40.0, 18000.0)
    assert rep["channels"]["FR"]["band"][1] <= 8000.0
    assert rep["band"][1] <= 8000.0     # combined by max, like live
    assert rep["score"] == 100          # both channels clean in-band
    # profile reasons carry the channel prefix
    fr[0]["snr_db"] = 25.0
    rep = trust.assess({"measurement": _meas(fl + fr)}, now=NOW)
    assert any(r.startswith("FR: ") for r in rep["reasons"])
    assert rep["score"] == rep["channels"]["FR"]["score"]


def test_degenerate_canvases():
    assert trust.assess({"id": "x"}) is None
    rep = trust.assess({"measurement": _meas([])})
    assert rep["score"] == 0
    assert rep["reasons"] == ["the canvas has no takes"]
    bad = _meas([_take("a", "FL", np.zeros(N - 1))])
    rep = trust.assess({"measurement": bad})
    assert rep["score"] == 0
    assert any("profile grid" in r for r in rep["reasons"])


def test_spread_median_costs_inside_the_band():
    rng = np.random.default_rng(7)
    base = np.zeros(N)
    takes = [_take("a", "FL", base + rng.normal(0, 1.0, N)),
             _take("b", "FL", base + rng.normal(0, 1.0, N)),
             _take("c", "FL", base + rng.normal(0, 1.0, N))]
    rep = trust.assess({"measurement": _meas(takes)}, now=NOW)
    ch = rep["channels"]["FL"]
    assert ch["spread_median_db"] is not None
    assert ch["spread_median_db"] > trust.SPREAD_GOOD_DB
    assert ch["score"] < 100
    assert any("median in-band spread" in r for r in ch["reasons"])
