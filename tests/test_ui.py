"""One gone state, one voice: both windows announce it with the
same AdwBanner under their header. The HIG banner pattern is for
persistent view states (its own examples: offline, read-only --
gone is the same species), one short title, an optional button;
events belong to toasts. The ratchet keeps the two windows from
drifting apart one wording at a time."""
import xml.etree.ElementTree as ET
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
UIS = ["io.github.ntman.PerDeviceEQ.ui",
       "io.github.ntman.PerDeviceEQ.Measure.ui"]


def _banners(path):
    root = ET.parse(path).getroot()
    hits = []
    for tv in root.iter("object"):
        if tv.get("class") != "AdwToolbarView":
            continue
        for child in tv.findall("child"):
            if child.get("type") != "top":
                continue
            for o in child.findall("object"):
                if o.get("class") == "AdwBanner":
                    t = o.find("property[@name='title']")
                    hits.append((o.get("id"),
                                 None if t is None else t.text))
    return hits


def test_both_windows_announce_gone_in_one_voice():
    per_ui = {ui: _banners(DATA / ui) for ui in UIS}
    for ui, hits in per_ui.items():
        assert len(hits) == 1, "%s: expected one top banner" % ui
    ids = {hits[0][0] for hits in per_ui.values()}
    assert ids == {"gone_banner"}, ids
    titles = {hits[0][1] for hits in per_ui.values()}
    assert len(titles) == 1 and None not in titles, titles
