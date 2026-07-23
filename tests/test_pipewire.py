"""Unit tests for PipeWire node listing.

No subprocess: synthetic pw-dump dicts are passed straight to the
listing functions, so these test the filtering/shaping logic itself.
"""
from perdeviceeq import pipewire


def _node(nid, name, cls, desc=None, prio=0):
    props = {"node.name": name, "media.class": cls, "priority.session": prio}
    if desc is not None:
        props["node.description"] = desc
    return {"type": "PipeWire:Interface:Node", "id": nid,
            "info": {"props": props}}


def test_list_sources_filters_and_sorts():
    dump = [
        _node(10, "alsa_output.sink_a", "Audio/Sink", "Sink A", 100),
        _node(40, "alsa_input.ears", "Audio/Source", "EARS", 50),
        _node(41, "alsa_input.webcam", "Audio/Source", "Webcam", 200),
        _node(60, "some_stream", "Stream/Input/Audio", "A stream"),
        {"type": "PipeWire:Interface:Node", "id": 42,       # source, no desc
         "info": {"props": {"node.name": "alsa_input.bare",
                            "media.class": "Audio/Source"}}},
        {"type": "PipeWire:Interface:Device", "id": 99},    # not a node
    ]
    got = pipewire.list_sources(dump)
    assert [s["name"] for s in got] == [
        "alsa_input.webcam", "alsa_input.ears", "alsa_input.bare"]
    assert all(s["name"].startswith("alsa_input") for s in got)  # no sink
    bare = next(s for s in got if s["name"] == "alsa_input.bare")
    assert bare["desc"] == "alsa_input.bare"     # desc falls back to name
    ears = next(s for s in got if s["name"] == "alsa_input.ears")
    assert ears["id"] == 40 and ears["desc"] == "EARS" and ears["prio"] == 50


def test_list_sources_empty_without_sources():
    dump = [_node(10, "alsa_output.sink_a", "Audio/Sink", "Sink A")]
    assert pipewire.list_sources(dump) == []


def test_list_sources_skips_nameless_source():
    dump = [{"type": "PipeWire:Interface:Node", "id": 7,
             "info": {"props": {"media.class": "Audio/Source"}}}]
    assert pipewire.list_sources(dump) == []


def test_monitor_capture_pins_the_tap(monkeypatch):
    """The meter tap must die with its pipe, never wander to the
    default (field catch: a card-profile fork died and was reborn
    between two polls; WirePlumber re-parented the orphaned
    capture onto the default sink and the meter danced to another
    device's music)."""
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stdout = None

    monkeypatch.setattr(pipewire.subprocess, "Popen", FakePopen)
    pipewire.monitor_capture("some.sink", 2, 48000)
    cmd = seen["cmd"]
    props = cmd[cmd.index("-P") + 1]
    assert "node.dont-reconnect = true" in props
    assert "stream.capture.sink = true" in props
    assert cmd[cmd.index("--target") + 1] == "some.sink"


def test_hook_protocol_states(monkeypatch):
    """Stamped, legacy (object without the stamp), and no
    metadata object at all."""
    import subprocess as sp

    def fake(out):
        def run(cmd, timeout=2.0):
            return sp.CompletedProcess(cmd, 0, out, "")
        return run

    monkeypatch.setattr(pipewire, "_run", fake(
        'Found "per-device-eq" metadata 99\n'
        "update: id:0 key:'protocol' value:'1' "
        "type:'Spa:String:JSON'\n"))
    assert pipewire.hook_protocol() == (True, "1")

    monkeypatch.setattr(
        pipewire, "_run",
        fake('Found "per-device-eq" metadata 99\n'))
    assert pipewire.hook_protocol() == (True, None)

    monkeypatch.setattr(pipewire, "_run", fake(""))
    assert pipewire.hook_protocol() == (False, None)
