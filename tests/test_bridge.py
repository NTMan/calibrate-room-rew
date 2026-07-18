import math

import numpy as np
import pytest

from perdeviceeq import bridge
from perdeviceeq import measure_core as mc

GRID = {"f_lo": 20.0, "f_hi": 20000.0, "ppo": 96}
FREQS = mc.log_grid(GRID["f_lo"], GRID["f_hi"], GRID["ppo"])
N = len(FREQS)


def _take(tid, key, mag):
    return {"id": tid, "session": "s1", "channel": key,
            "capture_channel": 0,
            "created_utc": "2026-07-18T00:00:00+00:00",
            "mag_db_uncal": [float(v) for v in mag],
            "delay_ms": 5.0, "snr_db": 45.0, "peak_dbfs": -6.0,
            "noise_dbfs": -80.0, "clipped": 0, "repaired": 0,
            "chan_vol": 0.3, "soft_vol": 0.3}


def _profile(pid, name, takes):
    return {"id": pid, "name": name,
            "measurement": {
                "grid": dict(GRID),
                "source": {"name": name + "-rig", "serial": "",
                           "node_match": "n", "channels": 2,
                           "cal": {}},
                "sessions": {"s1": {"created_utc": "x"}},
                "takes": takes}}


def _base():
    # a smooth, headphone-ish curve: gentle tilt plus one bump
    lf = np.log10(FREQS)
    bump = 4.0 * np.exp(-((lf - math.log10(3000.0)) / 0.15) ** 2)
    return -2.0 * (lf - lf[0]) + bump


def _delta():
    # the injected rig-to-rig truth: a treble shelf and a small dip
    lf = np.log10(FREQS)
    shelf = 2.0 / (1.0 + np.exp(-(lf - math.log10(4000.0)) / 0.08))
    dip = -1.0 * np.exp(-((lf - math.log10(200.0)) / 0.12) ** 2)
    return shelf + dip


def _noise(rng, scale):
    # per-take seating noise, smooth-ish so spread stays realistic
    raw = rng.normal(0.0, scale, N)
    k = np.ones(9) / 9.0
    return np.convolve(raw, k, mode="same")


def _aligned(freqs, vals):
    m = (freqs >= bridge.ALIGN_LO) & (freqs <= bridge.ALIGN_HI)
    return vals - vals[m].mean()


def _make_pair(extra_left_b=0.0, wild_top=True, seed=7):
    rng = np.random.default_rng(seed)
    base, g = _base(), _delta()
    wild = np.zeros(N)
    if wild_top:
        # rig B loses repeatability above 12 kHz: the trusted band
        # must end before it
        wild[FREQS > 12000.0] = 1.0
    ta, tb = [], []
    for i in range(3):
        for ch in ("FL", "FR"):
            ta.append(_take("a%d%s" % (i, ch), ch,
                            base + _noise(rng, 0.15)))
            skew = extra_left_b if ch == "FL" else 0.0
            tb.append(_take("b%d%s" % (i, ch), ch,
                            base + g + skew + _noise(rng, 0.15)
                            + wild * _noise(rng, 8.0)))
    return (_profile("pa", "A", ta), _profile("pb", "B", tb), g)


def test_bridge_recovers_the_injected_delta():
    pa, pb, g = _make_pair()
    res = bridge.compute_bridge(pa, pb)
    assert sorted(res["channels"]) == ["FL", "FR"]
    freqs = res["freq_hz"]
    want = _aligned(freqs, -g)          # D = A - B = -g, shape-aligned
    for ch in ("FL", "FR"):
        e = res["channels"][ch]
        t = e["band_mask"]
        assert t.any()
        assert np.max(np.abs(e["delta_db"][t] - want[t])) < 0.35
        lo, hi = e["trusted_band_hz"]
        assert hi < 12000.0             # the wild top is out of trust
        assert lo < 100.0


def test_channel_skew_warns():
    pa, pb, _ = _make_pair(extra_left_b=1.5, wild_top=False)
    res = bridge.compute_bridge(pa, pb)
    assert res["warnings"]
    assert "seating" in res["warnings"][0]


def test_published_residual_reads_the_sum():
    pa, pb, g = _make_pair()
    # published = the truth of the device (rig A is the anchor here),
    # so B's residual against it must recover +g in shape
    res = bridge.compute_bridge(pa, pb,
                                published=(FREQS, _base()))
    freqs = res["freq_hz"]
    want = _aligned(freqs, g)
    e = res["channels"]["FL"]
    t = e["band_mask"]
    assert np.max(np.abs(e["published_residual_db"][t] - want[t])) \
        < 0.35


def test_outputs_roundtrip(tmp_path):
    pa, pb, _ = _make_pair()
    res = bridge.compute_bridge(pa, pb)
    rp = bridge.write_outputs(res, str(tmp_path))
    assert (tmp_path / "bridge.json").exists()
    txt = (tmp_path / "delta.txt")
    f, v = bridge.parse_curve(str(txt))
    assert len(f) == len(res["freq_hz"])
    mean_d = np.stack([res["channels"][c]["delta_db"]
                       for c in ("FL", "FR")]).mean(axis=0)
    assert np.max(np.abs(v - mean_d)) < 5e-4
    body = open(rp).read()
    assert "trusted" in body and "cannot split" in body


def test_no_shared_channels_is_an_error():
    pa, pb, _ = _make_pair()
    for t in pb["measurement"]["takes"]:
        t["channel"] = "C"
    with pytest.raises(bridge.BridgeError):
        bridge.compute_bridge(pa, pb)
