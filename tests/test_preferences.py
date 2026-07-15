"""User preference layers (perdeviceeq/preferences.py): the taste EQ
store and its composition into profile graphs. A layer is playback-
side taste, device-independent, appended after EVERY chain of the
active profile -- and it never touches the profile itself.
"""
from perdeviceeq import eq, preferences


LSC50 = {"type": "LSC", "freq": 50, "gain": 8.0, "q": 1.0,
         "enabled": True}


def _store(tmp_path):
    return preferences.PreferenceLayers(
        path=str(tmp_path / "layers.json"))


def test_store_roundtrip_and_active(tmp_path):
    st = _store(tmp_path)
    assert st.layers == [] and st.active() is None
    lid = st.upsert({"name": "Basshead", "bands": [LSC50, "junk"]})
    assert st.get(lid)["bands"] == [LSC50]      # non-dicts dropped
    st.set_active(lid)
    st2 = _store(tmp_path)                      # re-read from disk
    assert st2.active_id == lid
    assert st2.active_bands() == [LSC50]
    # replace by id keeps identity; unknown active reads as off
    st2.upsert({"id": lid, "name": "Basshead v2", "bands": []})
    assert st2.get(lid)["name"] == "Basshead v2"
    assert st2.active_bands() == []
    st2.set_active("nope")
    assert st2.active() is None
    assert st2.delete(lid) and not st2.delete(lid)
    assert _store(tmp_path).layers == []


def test_deleting_the_active_layer_turns_it_off(tmp_path):
    st = _store(tmp_path)
    lid = st.upsert({"name": "A", "bands": [LSC50]})
    st.set_active(lid)
    st.delete(lid)
    st3 = _store(tmp_path)
    assert st3.active() is None and st3.layers == []


def test_layer_composes_after_every_chain():
    prof = {"preamp": -3.0, "apply_all": False,
            "ch_keys": ["FL", "FR"],
            "channels": {
                "FL": {"bands": [{"type": "PK", "freq": 200,
                                  "gain": -2.0, "q": 1.0,
                                  "enabled": True}]},
                "FR": {"bands": []}},
            "all": {"bands": []}}
    g = eq.profile_graph(prof, extra=[LSC50])
    assert g.count("freq = 50") == 2            # one per channel
    assert g.count("freq = 200") == 1           # the profile's own
    flat = {"preamp": 0.0, "apply_all": True, "ch_keys": [],
            "all": {"bands": []}, "channels": {}}
    g = eq.profile_graph(flat, extra=[LSC50])
    assert g.count("freq = 50") == 1            # taste over Clean
    # no layer: byte-identical to the plain call
    assert eq.profile_graph(prof, extra=[]) == eq.profile_graph(prof)


def test_disabled_layer_bands_stay_out_of_the_graph():
    off = dict(LSC50, enabled=False)
    flat = {"preamp": 0.0, "apply_all": True, "ch_keys": [],
            "all": {"bands": []}, "channels": {}}
    assert "freq = 50" not in eq.profile_graph(flat, extra=[off])
