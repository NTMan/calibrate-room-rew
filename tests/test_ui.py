"""One state, one voice, per state: both windows announce a
gone OUTPUT with the same AdwBanner, and the Measure window
announces a gone RIG with its own -- the HIG banner pattern is
for persistent view states (offline, read-only -- and gone),
one short factual title each; two independent states may hold
at once and their banners come and go independently. The
ratchet keeps the wordings from drifting apart."""
import xml.etree.ElementTree as ET
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
MAIN = "io.github.ntman.PerDeviceEQ.ui"
MEASURE = "io.github.ntman.PerDeviceEQ.Measure.ui"


def _banners(path):
    root = ET.parse(path).getroot()
    hits = {}
    for tv in root.iter("object"):
        if tv.get("class") != "AdwToolbarView":
            continue
        for child in tv.findall("child"):
            if child.get("type") != "top":
                continue
            for o in child.findall("object"):
                if o.get("class") == "AdwBanner":
                    t = o.find("property[@name='title']")
                    hits[o.get("id")] = (None if t is None
                                         else t.text)
    return hits


def test_gone_banners_speak_one_language_per_state():
    main = _banners(DATA / MAIN)
    measure = _banners(DATA / MEASURE)
    assert set(main) == {"gone_banner"}, main
    assert set(measure) == {"gone_banner", "mic_banner"}, measure
    assert main["gone_banner"] == measure["gone_banner"]
    assert main["gone_banner"]
    assert measure["mic_banner"]
    assert measure["mic_banner"] != measure["gone_banner"]
