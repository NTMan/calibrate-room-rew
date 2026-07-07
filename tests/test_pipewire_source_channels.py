"""source_channels/sink_channels read a node's Format position from a dump."""
from perdeviceeq import pipewire as pw


def _dump(name, position=None, channels=None):
    fmt = {}
    if position is not None:
        fmt["position"] = position
    if channels is not None:
        fmt["channels"] = channels
    return [{"type": "PipeWire:Interface:Node", "id": 42,
             "info": {"props": {"node.name": name,
                                "media.class": "Audio/Source"},
                      "params": {"Format": [fmt]}}}]


def test_source_channels_stereo_position():
    d = _dump("mic", position=["FL", "FR"])
    assert pw.source_channels("mic", d) == ["FL", "FR"]


def test_source_channels_mono_position():
    d = _dump("mic", position=["MONO"])
    assert pw.source_channels("mic", d) == ["MONO"]


def test_source_channels_count_fallback():
    d = _dump("mic", channels=2)          # no position -> POS fallback
    assert pw.source_channels("mic", d) == ["FL", "FR"]


def test_source_channels_unknown_node_defaults_stereo():
    assert pw.source_channels("nope", _dump("mic", ["FL", "FR"])) == \
        ["FL", "FR"]


def test_sink_channels_still_delegates():
    d = _dump("spk", position=["FL", "FR", "FC", "LFE", "RL", "RR"])
    assert pw.sink_channels("spk", d) == ["FL", "FR", "FC", "LFE",
                                          "RL", "RR"]
