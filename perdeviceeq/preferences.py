# -*- coding: utf-8 -*-
"""User preference layers: taste, not correction.

A layer is a plain parametric EQ the user dials by hand -- 'Basshead',
'Alina fav' -- device-independent, composed into the PipeWire graph
AFTER the active device profile's filters and applied whatever sink
and whatever profile is selected. Correction answers 'what does this
device do wrong'; a layer answers 'what does this listener like', so
a layer never touches a device profile and can never flip its
`edited` mark.

Targets (Harman and friends) are NOT layers: a target is a magnitude
curve in the measurement frame of a specific rig, consumed by the
fit (fit.target); a layer is playback-side and frame-agnostic.

One JSON file ({"active": id | null, "layers": [...]}) with atomic
rewrites. No GTK; filesystem + JSON only, like the profile store.
"""
import json
import os
import uuid

from .config import PREF_LAYERS_FILE


def _sane_layer(d):
    lid = str(d.get("id") or uuid.uuid4().hex[:12])
    bands = [b for b in (d.get("bands") or []) if isinstance(b, dict)]
    return {"id": lid,
            "name": str(d.get("name") or "Preference"),
            "bands": bands}


class PreferenceLayers:
    """Load, edit and persist the taste layers + which one is on."""

    def __init__(self, path=None):
        self.path = path or PREF_LAYERS_FILE
        self.layers = []
        self.active_id = None
        self.reload()

    def reload(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        raw = data.get("layers")
        self.layers = [_sane_layer(d) for d in raw
                       if isinstance(d, dict)] if isinstance(
                           raw, list) else []
        act = data.get("active")
        self.active_id = act if any(l["id"] == act
                                    for l in self.layers) else None

    def _write(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"active": self.active_id,
                       "layers": self.layers},
                      f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, lid):
        return next((l for l in self.layers if l["id"] == lid), None)

    def upsert(self, layer):
        """Add or replace a layer (matched by id); returns the id."""
        lay = _sane_layer(layer)
        for i, l in enumerate(self.layers):
            if l["id"] == lay["id"]:
                self.layers[i] = lay
                break
        else:
            self.layers.append(lay)
        self._write()
        return lay["id"]

    def delete(self, lid):
        before = len(self.layers)
        self.layers = [l for l in self.layers if l["id"] != lid]
        if self.active_id == lid:
            self.active_id = None
        if len(self.layers) != before:
            self._write()
        return len(self.layers) != before

    def set_active(self, lid):
        """Turn a layer on (or off with None). Unknown ids read as
        off rather than raising: a deleted layer must not wedge the
        apply path."""
        self.active_id = lid if lid and self.get(lid) else None
        self._write()

    def active(self):
        return self.get(self.active_id) if self.active_id else None

    def active_bands(self):
        """The bands to compose into every graph, or []. Disabled
        bands ride along; the graph builder skips them like any
        profile band."""
        lay = self.active()
        return list(lay["bands"]) if lay else []
