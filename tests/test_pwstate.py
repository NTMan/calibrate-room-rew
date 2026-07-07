"""PWState: one snapshot (sinks/sources/default) with change detection and
subscriptions. GTK-free core, driven by update() against synthetic dumps."""
from perdeviceeq import pipewire as pw


def _node(nid, name, cls, prio=0):
    return {"id": nid, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name, "media.class": cls,
                               "node.description": name,
                               "priority.session": prio}}}


def _default_meta(sink_name, as_string=False):
    val = ('{"name": "%s"}' % sink_name) if as_string else {"name": sink_name}
    return {"id": 30, "type": "PipeWire:Interface:Metadata",
            "props": {"metadata.name": "default"},
            "metadata": [{"key": "default.audio.sink", "value": val}]}


def test_default_sink_from_dump_dict_value():
    d = [_default_meta("spk"), _node(1, "spk", "Audio/Sink")]
    assert pw.default_sink_from_dump(d) == "spk"


def test_default_sink_from_dump_json_string_value():
    assert pw.default_sink_from_dump([_default_meta("spk", True)]) == "spk"


def test_default_sink_from_dump_absent():
    assert pw.default_sink_from_dump([_node(1, "spk", "Audio/Sink")]) is None


def test_pwstate_update_fills_and_detects_change():
    st = pw.PWState()
    d1 = [_default_meta("spk"), _node(1, "spk", "Audio/Sink"),
          _node(2, "mic", "Audio/Source")]
    assert st.update(d1) is True             # first snapshot -> changed
    assert [s["name"] for s in st.sinks] == ["spk"]
    assert [s["name"] for s in st.sources] == ["mic"]
    assert st.default_sink == "spk"
    assert st.update(d1) is False            # identical -> no change
    d2 = d1 + [_node(3, "hp", "Audio/Sink")]
    assert st.update(d2) is True             # a sink appeared -> changed


def test_pwstate_default_change_is_a_change():
    st = pw.PWState()
    nodes = [_node(1, "spk", "Audio/Sink"), _node(2, "hp", "Audio/Sink")]
    st.update([_default_meta("spk")] + nodes)
    assert st.update([_default_meta("hp")] + nodes) is True
    assert st.default_sink == "hp"


def test_pwstate_subscribe_notify_unsubscribe():
    st = pw.PWState()
    seen = []
    off = st.subscribe(lambda s: seen.append(s.default_sink))
    st.default_sink = "x"
    st._notify()
    assert seen == ["x"]
    off()
    st._notify()
    assert seen == ["x"]                     # unsubscribed -> no more calls


def test_app_state_is_singleton():
    assert pw.app_state() is pw.app_state()
