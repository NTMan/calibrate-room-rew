# Local captures (never committed)

Everything in this directory except this README is gitignored.

This is the home for **real** audio captures used during audits and debugging — including recordings of copyrighted material, which must never enter the public repository. Synthetic, redistributable fixtures are a different thing: they are generated on the fly by `tools/make_fixtures.py` into `tests/fixtures/` and are not stored in git either.

## What belongs here

- pre-EQ sink monitor captures used with `tools/audit_headroom.py`
- evidence captures for upstream bug reports (see ROADMAP.md, Task 6: the DeaDBeeF SRC-plugin + pipewire-alsa double-resampling artifacts)
- any measurement takes you want to keep across reboots (`/tmp` is tmpfs)

## Capture recipe

```
pw-record -P '{ stream.capture.sink = true }' \
          --target <sink-name> --format f32 capture.wav
```

`--format f32` is mandatory: the graph runs in float32 and over-full-scale peaks are the whole point of the audit — integer formats clip them at write time. Note the sink monitor taps **pre-EQ** in the in-node topology; post-EQ numbers are computed by `tools/audit_headroom.py`.

Suggested naming: `<device>_<what>_<yyyymmdd>.wav`, plus a note of the active profile state (see ROADMAP.md, Task 4) — a capture without the EQ state it was taken under is archaeology.
