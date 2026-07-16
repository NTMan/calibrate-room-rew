# per-device-eq Roadmap

Goal: any output device corrected through per-device-eq should sound the
same to *this* listener — measured correction per device, taste per
listener, headroom that never lies.

---

## Shipped

- **Measurement.** The app is the instrument: Farina sweep out through the
  selected sink, capture from the rig, per-capsule calibration, impulse
  windowing, magnitude-RMS averaging across seatings, SNR-targeted
  auto-leveling, per-take quality gating, and a constrained biquad fit
  with an EQ range that follows the take-to-take spread. Incremental core:
  every take persists the moment it lands; sessions resume; delete + refit
  are first-class. (Roadmap Tasks 2 and 3 of the July 2026 plan, absorbed
  and exceeded.)
- **Headroom.** Tier-1 worst-chain estimate with a Safe suggestion, tier-2
  live post-EQ meter, fit lands gain-staged, and every hint composes the
  active taste layer.
- **Taste layer.** Named preference EQs over any profile on any device,
  with a settings dialog, a Taste switcher above Profile, and honest
  interaction with headroom. This absorbed the useful half of the old
  "two-layer profiles" Task 1; the target-provenance half is parked below.
- **Sink honesty.** Edit aims at the profile's own sink; only New picks up
  the current output; a session belongs to its sink — alive or
  Unavailable, no retargeting, no selection stealing.
- **Adaptive UI.** Adw.MultiLayoutView two-column measurement window,
  client-side modality, draw budgets, and a long tail of layout fixes.

## Next

1. **EQ editor as a component.** Extract the graph + bands editor into one
   reusable widget so device correction and Taste render and edit
   identically everywhere.
2. **Collapsible EQ sections.** The main window carries two equalizers
   (device correction and Taste) without overload: collapsed shows just
   the profile/layer choice, expanded shows the graph and bands, editable
   in place.
3. **Main window layout.** Device first, then Taste, then a closing block
   with the preamp and per-channel level meters; Bypass likely moves into
   the header bar.
4. **Auto-preamp instead of a suggestion.** When the live meter detects
   the composition going over 0 dBFS, apply the recommended preamp
   immediately rather than offering a Session button — nobody should sit
   through audible distortion to click a hint.

## Parked

- **Targets and measurement frames** (Harman and friends): `fit.target` as
  a magnitude curve with a frame tag and provenance, frame-compatibility
  gating against the takes' calibration, AutoEq CSV import, and the
  per-unit D-bridge via a reference measurement. Revisit after the Next
  list lands — and maybe not at all, if a well-tuned Taste layer keeps
  covering the need.
- **Profile state journal** (old Task 4): log band edits / bypass / preamp
  changes with timestamps; `--dump-state` for measurement notes.
- **Hardware: 711-clone coupler** (old Task 5): buy an IEC 60318-4 clone,
  verify against 3–4 IEMs with published curves, record its trusted
  range. EARS remains for over-ears and same-rig deltas.
- **Upstream: DeaDBeeF SRC + pipewire-alsa** (old Task 6): file with a
  synthetic repro; two rate converters with conflicting ratio views.

---

## Established facts worth not re-deriving

- Sink monitor in in-node topology = **pre-EQ** (Bypass-toggle experiment);
  post-EQ level must be computed, which the meter does.
- BT absolute volume does not protect against quantization clipping;
  software stream gain does.
- Constant BT latency is harmless to sweep FR; clock drift and timing
  references are the actual hazards — average by magnitude only, never
  time-domain, and never use an acoustic timing reference over BT.
- EARS: trust < ~2 kHz absolute, full range for same-rig deltas; the
  ~2.3 kHz cut is listener-validated by ear, the 14.4 kHz filter trio was
  rig resonance.
- PipeWire's param_eq shelves use plain Q (`alpha = sin(w0)/(2Q)`), not the
  RBJ slope form; `perdeviceeq.eq` and the audit match it to 1e-10.

## Upstream notes

* WirePlumber: a fresh `stream.capture.sink` stream against a settled BT
  sink with an in-node filter-graph deterministically comes up with one
  monitor port unlinked; only a node reconfigure (graph republish)
  completes the links. Repro + workaround live in per-device-eq (the
  400 ms republish nudge and the dead-channel watchdog).
* gnome-shell (observed once, repro unknown): the quick-settings output
  picker's checkmark desynced from the actual default sink while
  per-device-eq was active. Capture kit for the next occurrence:
  `pw-metadata 0 default.audio.sink` vs `pactl get-default-sink` vs the
  picker's checkmark — whichever disagrees is the stale layer; run
  `pactl subscribe` during EQ edits to see sink remove/add storms.
