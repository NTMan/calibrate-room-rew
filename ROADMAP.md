# per-device-eq Roadmap

Goal: any output device corrected through per-device-eq should sound the
same to *this* listener — measured correction per device, taste per
listener, headroom that never lies.

---

## Shipped

- **The cal wears its provenance.** Closed as testimony, not
  verdict: the architect's analog doctrine rules the domain --
  a cal belongs to the analog layer in front of the hole, a
  hole has no serial, and even usb node grammar cannot split
  an integrated mic from an interface input, so "the wrong
  cal" is machine-undecidable and the machine testifies while
  the one who sees the analog layer judges. The slot wears the
  assigned file's biography (content-addressed by sha,
  house-wide across every profile's takes, silent when the
  story is all native); the noise lives in the cloud's shadow
  -- one number, the count of foreign profiles, in a colored
  pill whose color weighs the statistical anomaly, with every
  word in the tooltip; the inventory dialog counts per rig and
  its Reassign remains the bulk re-cal door. The round also
  minted H10 (the list dresses evenly, jurisdiction inside the
  row's own population) which caught the wizard's band budget
  on its first sighted run and the CI-only two-line target
  rows the workstation could never see, and the takes list now
  speaks the architect's three-rule grammar: a group opens
  with its capsule line right-aligned, takes are separated by
  their own signal lines, a gray rule closes every group --
  chronology sacred throughout.

- **AppImage.** Released with 4.0.1 and field-clean on the
  architect's station: a single-file door for everyone not on
  Fedora, attached to every release with a zsync auto-update
  channel, and built on every push under the same continuous
  discipline as the code -- a CLI smoke for the Python spine
  and a GUI smoke that opens a real window. The harvest is a
  plain dnf --installroot on the oldest supported Fedora (a
  policy, not a number: the base is the glibc floor, bumped
  once per cycle), and the bundle obeys the host-families
  law the field taught one catch at a time: what couples to
  host state leaves (the loader and glibc, the GPU drivers
  and their kernel spine, the C++ runtime mesa resolves
  through us, fontconfig with its /etc/fonts grammar), while
  skew-stable cargo stays (dispatch loaders, the protocol
  client libs, freetype). Debian 13 and Mint 22 sit below the
  floor until a source-built base earns its keep.

- **GNOME HIG pass.** Closed in both halves. The mechanical
  floor: nine rules (H1-H9, the last one dynamic -- the audit
  presses Tab itself and proves the walk rounds the room),
  zero findings across the whole house -- both windows in both
  channel costumes, the command dialog, About, the export
  wizard with every preview, the cal history -- under the CI
  ratchet, cairo-rendered so headless containers judge too.
  The manual half lives as the charter's checklist in HIG.md,
  every line a check with evidence; the sittings it settled
  became rules (the wardrobe, the focus grammar) or design
  (one rig, one header). Two field bugs became permanent
  machine rules on the way.

- **Profile package.** The architect's ruling shaped it: the exchange
  artifact is the store's own canonical body, bare -- no envelope, no
  wrapper version, the profile's own schema version is the one
  contract (design review peeled off the embedded sha and the wrapper
  before a line shipped). pack() writes deterministic bytes and the
  file's sha256 IS its address; unpack() refuses directionally (newer
  body -> newer build, older -> the migration tool); absorb() never
  destroys -- byte-identical keep, spoken no-op on the same address,
  remint with provenance on collision; the import dialog shows the
  package_report passport. Byte-stable roundtrip is pinned by test.
- **CI that sees the GUI.** Two Actions lanes. Tests: Fedora container
  with the real toolkit -- pyflakes over the whole tree, every `.ui`
  loaded by GtkBuilder after `Adw.init()` (a stricter judge than
  `gtk4-builder-tool`, which cannot see libadwaita types), a headless
  import smoke of the Gtk modules, and the full suite under Xvfb.
  Flatpak: manifest built for both arches on every push, bundles
  attached to releases. Acceptance met in the field: the lane turned
  red on a real import-time error (a missing typelib) before it ever
  went green.
- **Export wizard.** One window-level action (the primary menu is its
  home) that bakes the COMPOSED chain -- device + active taste, preamp
  included -- and asks one question: where is this going? Shipped in
  3.2.0 with the writer classes and the null-test acceptance the sprint
  wrote for it.
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

1. **Verified .pdeq exchange, v1.** The architect's words: a
   download service of verified EQ profiles. Promoted from
   Direction at its smallest honest size -- a static,
   sha-addressed index of .pdeq packages (a git repo can be
   the whole backend) plus in-app browse and import through
   the existing unpack door. Verification per the Direction
   entry: the maintainer's cross-check against published
   curves marks a package verified; unverified stays visible
   and labeled. Accept when a profile published in the index
   installs from inside the app with its provenance shown.

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

- **Flathub, parked at the door.** Submission flathub/flathub#9484
  was closed under Flathub's Generative AI policy, which covers
  AI-assisted code and content -- a line this project's development
  openly falls under, so the closure is not contested. The manifest
  is real, CI-built for both arches, and ships as bundles on every
  release already; what a store would add is auto-updates and
  discovery. Re-engage if the policy matures the way its own
  "mature, well-maintained projects" exception hints, or ship an
  own OSTree repository instead and owe nobody a door.
- **The foreign hand.** Gated on PipeWire's filter-graph read-back
  shipping in a release and reaching Fedora (master already exposes
  applied graphs on the properties -- pipewire#5345 carries our
  use case). Then: read `filter-graph.0` before writing; non-empty
  and not what we last wrote means another writer -- surface a
  takeover-or-leave choice, per device, remembered. Ownership of a
  sink's EQ belongs to the user; the tool's job is to make the
  contest visible and the choice durable. Rejected in advance:
  silent clobber (a war nobody sees), silent yield (breaks the
  persistence promise silently), and chaining into `.1` (two
  competing corrections are acoustic garbage; chaining is for
  complementary filters).
- **Measure-window Undo, sitting-scoped.** One undo stack for the whole
  window, alive from open to close: take deletion, cal reassignment,
  re-fits -- every destructive gesture joins it, and the stack dies with
  the window (the profile on disk stays the artifact). The cal-reassign
  dialog deliberately shipped without a toast-Undo because a single
  orphan Undo raises worse questions than it answers; once the stack
  exists, per-action Undo toasts become legitimate and can return.
  Ctrl+Z binding rides along.
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
* xdg-desktop-portal, ready to file: no portal exists for restarting a
  user service, and the only mechanism today -- flatpak-spawn --host
  via org.freedesktop.Flatpak -- is arbitrary-code-on-host by design,
  so every app that installs host-daemon integration ships an
  instruction sentence instead (ours: the WirePlumber hook needs one
  `systemctl --user restart wireplumber`). Any proposal must dodge the
  trap our own case demonstrates: restarting a unit whose inputs the
  sandbox writes IS executing what you wrote, so the design has to be
  consent-shaped -- units declared statically in the manifest (the
  Background/autostart pattern), a dialog naming the unit, remembered
  per-unit grants, restart-only (no start/stop/enable of arbitrary
  units), user units only. Precedent for lifecycle portals: the
  restart-self portal, Flatpak 1.0. The filing-ready issue text is
  drafted (see the sprint notes); target: flatpak/xdg-desktop-portal.
* gnome-shell (observed once, repro unknown): the quick-settings output
  picker's checkmark desynced from the actual default sink while
  per-device-eq was active. Capture kit for the next occurrence:
  `pw-metadata 0 default.audio.sink` vs `pactl get-default-sink` vs the
  picker's checkmark — whichever disagrees is the stale layer; run
  `pactl subscribe` during EQ edits to see sink remove/add storms.
