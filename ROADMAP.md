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
  are first-class.
- **Headroom.** Tier-1 worst-chain estimate composing the taste layer,
  tier-2 live post-EQ meters in the preamp card, an Auto preamp MODE that
  lands the composed Safe (device + taste) and a session clamp that
  absorbs real peaks mid-playback. The fit lands bands only -- the mode
  owns the gain.
- **Taste layer.** Named preference EQs over any profile on any device,
  a Taste card with an in-place editor, picker with rename/delete, and
  honest interaction with headroom.
- **One undo timeline.** Device, taste and profile switches share a
  single history: selection is not an edit, steps are single, births and
  deletions are revocable within the session (the graveyard), and the
  arrows never lie about whether anything would change.
- **Sink honesty.** Edit aims at the profile's own sink; only New picks up
  the current output; a session belongs to its sink — alive or
  Unavailable, no retargeting, no selection stealing.
- **Adaptive UI.** Adw.MultiLayoutView two-column measurement window,
  client-side modality, draw budgets, the PeqView component shared by
  device and taste, cards everywhere (device, taste, preamp, ring,
  takes), and a long tail of layout fixes.
- **Integration and CLI.** `install_full` as the one source behind both
  `--install` and the GUI dialog; `--uninstall`; symmetric
  `--list-sinks` / `--list-sources`; the launcher's tools gate.

## Next (the sprint)

1. **Export wizard.** One window-level action (the primary menu is its
   home) that bakes the COMPOSED chain -- device + active taste, preamp
   included -- and asks one question: where is this going? A registry of
   known external equalizers drives the rest, three writer classes:
   (a) native import files for targets that have import (REW /
   EqualizerAPO-style parametric text, Wavelet GraphicEQ, RootlessJamesDSP);
   (b) a constrained fit for fixed-band vendor graphics (Soundcore-class
   companion apps) -- fixed frequencies, gains solved by least squares,
   against a MEASURED basis where we have one, with the residual curve
   shown BEFORE export so nobody carries a surprise; (c) a hand-transfer
   sheet for targets with no import at all: a readable table, values
   rounded to the target's step, one screen. Accept when parametric
   exports null-test against the in-app chain within 0.1 dB across the
   fit band with the per-channel collapse policy stated in the header,
   and the fixed-band path never exports without having shown its
   residual.
2. **Profile package.** One-file `.pdeq` bundle: profile + measurement +
   fit provenance + rig fingerprint + content sha. Import validates and
   shows provenance. Accept when export→import roundtrips byte-stable.
   This is the foundation the exchange service stands on.
3. **GNOME HIG pass.** A written checklist in-repo (spacing scale, focus
   order, keyboard mnemonics, symbolic icons, header-bar patterns,
   dialog vs popover usage, About window), every deviation fixed or
   argued in place. Accept when each line carries a check or a commit.
4. **CI that sees the GUI.** Actions running pytest + pyflakes +
   `gtk4-builder-tool validate` over every `.ui`, plus a headless import
   smoke of the Gtk modules. Accept when the classes of breakage we have
   actually shipped past the sandbox (bad `.ui`, import-time errors) turn
   the build red.
5. **Flathub.** Manifest, metainfo, screenshots, release notes.
   Popularity starts with installability.

## Direction

- **Frames and targets (architecture settled; the debate narrowed to
  scope).** Two domains that never mix. Playback stays device + taste
  + preamp, forever; everything frame-related lives on the MEASUREMENT
  side. A canvas carries frame provenance (the rig, or
  bridged-via-<bridge.json sha>); the measurement card shows it and
  the fit consumes it silently, translating canvas or target between
  rig coordinates BEFORE fitting. A rig delta must never enter the
  PipeWire chain: applying it in playback would "fix" the listener's
  ear for the difference between stands.
  The listener's anchor is itself the target: "EARS-flat plus the
  taste layer" IS the perceptual goal expressed in EARS coordinates,
  and a target in any other frame is anchor + D from the bridge. The
  bridge is the old Contra -- frame-compatibility machinery -- made
  empirical: D is measured, not read from a PDF. The canonical bridge
  should average 2-3 reference IEMs (seat-rig interaction is
  per-device); a single ORIGIN is the first approximation.
  Amp inserts (the UTWS case): a combo is just another device with its
  own sink and its own measured profile -- the amp's delta rides
  inside for free, no fourth playback layer. A same-rig differential
  delta (wired vs through-the-amp, the best case for EARS: same-rig,
  full-band trust) is a SYNTHESIS aid only -- predict a starting
  profile without remeasuring -- and is never transferable across
  IEMs: output impedance interacts with the specific IEM's impedance
  curve.
  Next concrete steps: bridge ORIGIN across EARS and the 711 clone
  when it arrives; add the frame property to the canvas. Deciding
  experiment for full arbitrary-curve targets stays: an EARS-flat
  profile plus a taste layer on one IEM should match an
  EARS-to-Harman profile without taste, by ear and by curve; if it
  does not, targets earn their complexity.
- **Profile exchange service.** Static, sha-addressed index of `.pdeq`
  packages first (a git repo can be the backend); accounts, ratings and
  comments only if the static thing proves too small.
- **Verified measurements.** Opt-in device-fingerprint sharing once the
  service exists, a popularity list, and the maintainer verifying the
  top of it -- by buying and measuring, or by cross-checking submitted
  packages against published curves.
- **Hardware PEQ, the full story.** Per-device capability tables (band
  counts, Q ranges, shelf types, preamp granularity), bank naming and
  multi-bank export where the hardware has them.
- **Mobile (Android and iOS): the bud-side doctrine.** The platform
  fact first: iOS has no system PEQ and cannot have one -- third-party
  apps cannot touch each other's audio, Headphone Accommodations serves
  AirPods/Beats only. So the one common denominator for a
  give-and-forget solution is DSP inside the earbuds via the vendor's
  companion app, and that path is primary, not a fallback: it survives
  phone swaps, adds no phone battery cost or latency, and covers every
  source. Vendor graphic EQs hide their band shapes, but the DSP sits
  in the bud, so the shapes are MEASURABLE: one slider at a time to max
  on the rig, subtract the zero run, and the fit degenerates into
  least squares over the measured basis -- exact gains for hand entry,
  the missing import stops hurting. Measure in the ANC mode the person
  actually wears, with HearID/adaptive presets off. Target curves for a
  dead donor device (the Falcon case): take the DELTA of the two
  models from ONE published measurement base -- same-rig differences
  transfer across frames far better than absolute curves. On Android,
  Wavelet is the optional upgrade (session-effect global EQ, zero
  hassle, imports GraphicEQ); RootlessJamesDSP is a true parametric
  but lives on stream capture -- latency, DRM apps silently escaping,
  permission rituals -- fine for an enthusiast, wrong for a gift. The
  export wizard's registry (sprint item 1) is where all of these live
  as targets.
- **Advocacy.** Write-ups of the established facts below (the BT
  loudness/limiter findings deserve their own post), short demo videos,
  and a comparison page against static AutoEq presets.

## Parked

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
- Mic calibration files are per-incidence-angle: 0° aimed at the active
  speaker for per-speaker sweeps, 90° up for speakers all around; below
  the room transition they coincide.
- Mic cal promises a flat pressure sensor at the capsule and nothing
  more: after honest cals it is the SIMULATED EARS that differ (pinna
  vs coupler), so a nonzero bridge D between two calibrated rigs is a
  legitimate frame difference, not evidence against anyone's file.
- When one coupler serves both channels in turn (EARS with earbuds
  seated one at a time), take-to-take spread reads SEATING, not
  hardware -- the FL/FR trust-band asymmetry on the first live bridge
  was left-bud-in-right-ear repeatability. One coupler + one cal also
  keeps the fit's cross-channel balance trims valid by construction.

## Upstream notes

* **Resolved:** PipeWire filter-graph ate softVolume/softMute so channel
  volumes applied twice and the level collapsed after enabling EQ --
  fixed in 1.6.8 (work item 5344).
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
