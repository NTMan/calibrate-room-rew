# per-device-eq Roadmap

Goal: any output device (speakers, wired headphones, TWS) corrected through per-device-eq should sound the same to *this* listener. Formalized as a two-layer profile model (Task 1). Derived from the July 2026 measurement/clipping investigation. Tools in `tools/`, synthetic fixtures via `tools/make_fixtures.py`.

---

## Task 1 — Two-layer profiles: measured device correction + user preference

**Model.**
- **Device layer** — measured, per device, by protocol ("по науке"): sweep on the rig, seatings averaged, constrained fit. It corrects the device toward the coordinate system it was measured in.
- **User preference layer** — chosen once per *listener*, shared by all devices: the target curve the listener wants. Can be a published curve (Harman) or a personally tuned one (e.g. "flat on my EARS", which by-ear testing on music and colleagues' voices validated for this listener). Taste adjustments (the +3 dB sub-bass shelf) live here too, as a listener-global setting with an on/off toggle, not inside any device profile.

**Composition mechanics (target provenance).** A device fit only makes sense relative to the target it was fitted to, so each device layer stores `fit_target` metadata (`EARS-flat-mikhail`, `Harman-2018-GRAS` for AutoEq imports, ...). Applied curve = `device_fit + (user_target − fit_target) + taste`. For locally measured profiles the bracket is zero (no double counting); for AutoEq imports it automatically converts Harman-referenced fits to the listener's preference.

**Deciding experiment (free, do first).** Denon PerL Pro measured almost flat on the EARS rig, i.e. it already sits at the "flat on my EARS" target with zero filters. Listen to it in Bypass on voice calls:
- sounds right (no cardboard, no shout) → "flat on my EARS" *is* the personal target; user layer = that target choice.
- still shouty → preference lives at a different point; tune the personal curve by ear starting from the validated ~2.3 kHz cut.

**Acceptance.** Switching the user preference (Harman ↔ personal) changes all devices by exactly the same dB curve; migrating an AutoEq import requires no manual band surgery.

## Task 2 — Headroom / clip meter  ← START HERE

**Rationale.** In the in-node (software-dsp) topology the post-EQ signal is unobservable in the graph: the sink monitor taps **pre-EQ** (verified: Bypass toggled mid-recording is inaudible in the capture; GNOME's output meter reads the same blind spot). Post-EQ level must be *computed*.

**Spec.**
- Passive observer only: read-only capture stream on the device monitor (`stream.capture.sink = true`, f32). No node inserted into the playback path — tooling, not a processing hop.
- Three tiers, implementation order 1 → 3 → 2:
  1. Instant upper bound (no capture): `monitor_peak + max(total EQ curve)` per applied chain. The preamp is ONE shared per-profile value (profile schema v2, `version: 2`; v1 files convert once via `tools/migrate_profiles_v1_to_v2.py`): the field reads out the worst chain's estimate, colors past 0 dBFS, and every over-0 channel is flagged on its tab. Auto = −(worst channel's curve max); unequal preamps would shift the balance the curves encode.
  2. Live meter (device window open): capture → profile's biquads (RBJ, `tools/pde_audit.py`) → count `|x| >= 1.0` → clip lamp + post-EQ peak.
  3. "Check headroom" button: 15–30 s capture, report pre/post peak, clip count, `recommended preamp = -(post-EQ peak dBFS)`. Prototype: `tools/audit_headroom.py` (reads saved v2 app profiles: `--profile NAME`; the suggestion is one shared value set by the worst channel).
- Lamp works in Bypass too: input alone can exceed FS (hot lossy masters overshoot after any honest resampler — see hot_master fixture).
- Tooltip distinguishes "clipping at input" (pre-EQ peak >= FS) from "clipping caused by profile" (appears only after biquads).

**Reference numbers (deterministic fixtures, seed 20260704).**
| fixture × demo profile | pre-EQ peak | post-EQ peak | clipped | verdict |
|---|---|---|---|---|
| clean_master (L/R) | −4.28 / −5.67 | −2.82 / −2.76 | 0 | preamp 0.0 OK |
| hot_master (L/R) | +1.72 / +1.35 | +7.10 / +7.49 | 9.8% / 8.4% | preamp −7.5 |

*Re-derived 2026-07-05: `pde_audit` shelves used the RBJ slope form while
PipeWire's param_eq uses plain Q — `alpha = sin(w0)/(2Q)` in
`spa/plugins/audioconvert/biquad.c`, linked into filter-graph (verified on the
1.6.2 tag and master; up to ~2 dB apart on the demo chains). The audit now
matches PipeWire and `perdeviceeq.eq` to 1e-10; the −7.5 verdict is unchanged.
Tier-1 numbers: max(total curve) = FL +9.57 dB @ 200 Hz, FR +8.48 dB @ 196 Hz.
The local-capture calibration below predates the fix — re-run `audit_headroom`
before comparing against old notes.*

Real-world calibration from the investigation (captures kept local-only, copyrighted material): own masters (−4.3 ceiling) → post ≈ −1 dBFS, fine at preamp 0; hot commercial master → post +5.5 dBFS, 2.3% clipped, preamp ≈ −6. Static worst-case auto (≈ −16) is 2–3× overkill — content-aware recommendation is the point.

**Acceptance.** `audit_headroom.py` on the fixtures reproduces the table within ±0.1 dB; the GTK meter agrees with the CLI on the same capture.

## Task 3 — Measurement wizard (REW vertical slice)

**Rationale.** Every footgun of this investigation (single-seating optimization, full-range match, wrong target level, dueling narrow filters) is a REW knob set wrong. A built-in wizard encodes the protocol so the mistakes have no place to happen.

**Spec (pipeline).**
- Farina log sweep (<= 256k samples) out through the selected sink; capture from the measurement mic (UMIK-1 / EARS / 71x coupler, USB).
- Deconvolution with inverse sweep → impulse → magnitude response; apply mic calibration file. Window out pre-impulse distortion harmonics (Farina puts them before the linear IR).
- Seating protocol: prompt reinsert × 5, align each take by its own impulse peak, average by **magnitude (RMS)** only — never vector/time-domain: playback (BT sink) and capture (USB mic) run on independent clocks.
- No acoustic timing reference — meaningless and harmful over BT.
- Psychoacoustic smoothing → constrained biquad fit to target: max Q = 2, max boost +6 dB, <= 8 filters, match range 20 Hz – 2 kHz for coupler rigs; above that, hand over to by-ear tools.
- BT sanity check: inter-take delay jitter > ~2 ms ⇒ warn "wireless link unstable, HF may be unreliable". (REW can't know the link is wireless; we do.)

**Acceptance.** Full cycle (sweep → averaged curve → filters → applied profile → verification sweep matches prediction) without REW.

## Task 4 — Profile state journal

**Rationale.** During the investigation the active band set changed between captures and could not be reconstructed afterwards. Measurements are only interpretable against a known EQ state.

**Spec.** Log every band edit / toggle / bypass / preamp change with a timestamp (journald via `logging` is enough). `--dump-state` prints the full current profile for embedding into measurement notes.

## Task 5 — Hardware: 711-clone coupler (parallel track)

- Buy an IEC 60318-4 clone (~$100–250): for IEMs it is closer to standard than EARS' straight tubes; no pinna needed.
- Verification ritual before trusting it: measure 3–4 IEMs with published curves (Denon PerL Pro is in AutoEq/Rtings and on hand), build this unit's correction curve, record its trusted range (expect <= 4–5 kHz).
- EARS remains for over-ears and same-rig deltas; EARS Pro when it ships to the region.

## Task 6 — Upstream bug report: DeaDBeeF SRC plugin + pipewire-alsa

**Finding.** DeaDBeeF's Resampler (Secret Rabbit Code) DSP combined with the pipewire-alsa output produces periodic micro-discontinuities: +3.5 dB phantom peaks, over-FS clusters every ~0.4 s, an 18-sample lag jump, time-warped correlation vs source (0.65). libsamplerate itself is innocent (sinc_fastest on the same segment → +0.68 dBFS). Root cause: two rate converters with conflicting ratio views.

**Action.** File against deadbeef with a *synthetic* repro (clipped sweep through the SRC plugin + pipewire-alsa) — cleaner legally and clearer for the maintainer; local real captures back it up if asked. Workaround already applied: no resampler in the player, rate conversion belongs to PipeWire.

---

## Repo layout for this work

```
tools/pde_audit.py        shared RBJ biquads, clip stats, demo profile
tools/audit_peaks.py      peak/clip counter for f32 captures
tools/audit_headroom.py   capture x profile -> post-EQ verdict (Task 2 t3)
tools/make_fixtures.py    deterministic clean/hot master generator
tests/fixtures/           generated: clean_master.wav, hot_master.wav
tests/fixtures-local/     .gitignore'd: real captures (copyrighted audio)
```

Capture recipe (the point being audited is pre-EQ by design): `pw-record -P '{ stream.capture.sink = true }' --target <sink> --format f32 out.wav`

## Established facts worth not re-deriving

- Sink monitor in in-node topology = **pre-EQ** (Bypass-toggle experiment).
- BT absolute volume does not protect against quantization clipping; software stream gain does.
- Constant BT latency is harmless to sweep FR; clock drift and timing references are the actual hazards (hence Task 3 rules).
- EARS: trust < ~2 kHz absolute, full range for same-rig deltas; the ~2.3 kHz cut is listener-validated by ear, the 14.4 kHz filter trio was rig resonance.
