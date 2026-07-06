"""Shared state for the PipeWire CLI shims (tests/shims/).

The shims let tools/measure_run.py run end-to-end in CI with no audio
hardware: put this directory first on PATH and the runner's pw-play /
pw-record / pw-dump / pw-metadata / pw-cli / wpctl calls hit these fakes
instead. State lives as small JSON files in $PDE_SHIM_DIR (default
/tmp/pde-shim) so the shims -- separate short-lived processes -- can
coordinate:

  metadata.json        the per-device-eq metadata object {key: graph}
  volume.json          {"cubic": v} sink volume (wpctl view)
  playing.json         present while the fake pw-play "plays"
  played.json          last played wav {"wav", "counter", "volume"}
  meta_at_play_N.json  metadata.json snapshot taken by pw-play run N
                       (proves the profile was bypassed DURING the sound)
  muted_log.json       every pw-cli mute write, in order
  volume_log.json      every wpctl set-volume, in order

Env knobs: PDE_SHIM_FOREIGN=1 adds a foreign stream on the sink,
PDE_SHIM_PLAY_FAIL=1 makes pw-play exit 1 (mid-measure failure),
PDE_SHIM_PLAY_SECONDS holds the fake stream alive for path verification,
PDE_SHIM_NOISE / PDE_SHIM_DELAY_MS shape the fake capture, PDE_SHIM_REPO
points at the checkout so pw-record imports perdeviceeq.pde_audit.
"""
import json
import os

SINK_ID, SOURCE_ID, PLAY_ID, FOREIGN_ID = 50, 40, 60, 70
CAPTURE_ID, WRONG_SOURCE_ID, OTHER_SINK_ID = 80, 41, 90
SINK_NAME, SOURCE_NAME = "test_sink", "test_source"


def state_dir():
    d = os.environ.get("PDE_SHIM_DIR", "/tmp/pde-shim")
    os.makedirs(d, exist_ok=True)
    return d


def path(name):
    return os.path.join(state_dir(), name)


def read_json(name, default=None):
    try:
        with open(path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, obj):
    tmp = path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path(name))


def append_json(name, entry):
    log = read_json(name, [])
    log.append(entry)
    write_json(name, log)


def bump(name):
    n = int(read_json(name, 0)) + 1
    write_json(name, n)
    return n
