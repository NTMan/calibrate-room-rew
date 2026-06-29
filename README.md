🇷🇺 Читайте инструкцию на русском: [README.ru.md](README.ru.md)

# Room & headphone correction with REW / AutoEQ and **per-device-eq** (PipeWire, Linux)

Measure your speakers/room with REW (or grab a headphone target from AutoEQ), then apply the result as a **per-output-device parametric EQ** directly inside PipeWire with [`per-device-eq.py`](per-device-eq.py).

`per-device-eq` writes the EQ as an in-node filter-graph directly into the **real sink** — no separate virtual sink, no extra node in the graph — and installs a small WirePlumber hook that re-applies it whenever the device starts playing. The correction lives inside WirePlumber itself, so it survives reboot, hotplug and Bluetooth reconnect **with no background process of your own running**: nothing to autostart, nothing to keep open.

![Per-Device EQ — the GUI](per-device-eq-screenshot.png)

### What per-device-eq gives you

- **Per output device.** Each sink — built-in speakers, HDMI, a specific Bluetooth headset (by MAC) — remembers its own EQ.
- **Reusable profiles.** The EQ is a profile you can switch, duplicate, rename and delete. `Clean (no EQ)` means flat; a device with no profile is Clean.
- **Direct REW/AutoEQ import.** Loads the text REW exports (and AutoEQ `ParametricEQ.txt`) as-is — no conversion script.
- **Live + persistent.** Edits apply instantly and autosave; the WirePlumber hook restores them on every playback.
- **Interactive editor.** Drag bands on the response graph, per-channel EQ, preamp, bypass A/B, and Undo/Redo (`Ctrl+Z` / `Ctrl+Shift+Z`).

### Why this instead of EasyEffects

EasyEffects is great, but it didn't fit how I actually use my machine, which is why this tool exists. Specifically:

- **It doesn't store settings per output device.** I want each sink — speakers, this Bluetooth headset, that one — to keep its own correction automatically. EasyEffects doesn't think in terms of sinks.
- **It has to be running.** The correction only exists while the EasyEffects process is up, so it needs to be autostarted and stay open. `per-device-eq` puts the EQ inside WirePlumber; nothing of mine runs.
- **It breaks the "just pick the sink and it plays" flow.** When you switch the output in your desktop and audio follows, EasyEffects doesn't follow with it — you have to go find and select the right sink inside EasyEffects. With this tool the EQ is attached to the sink, so it follows the audio.
- **It doesn't work for a DAW.** Pro-audio apps that talk to devices directly bypass the EasyEffects sink. An in-node graph on the real device applies regardless.

`per-device-eq` is free of all four — and, most importantly, it does the one job it's for: making the sound correct.

---

## Requirements & install

**PipeWire ≥ 1.6** (the in-node `audioconvert.filter-graph` is required), **WirePlumber**, **GTK 4**, **PyGObject**, **Python 3**.

At runtime the app shells out to the PipeWire command-line tools `pw-metadata` and `pw-dump`. If either is missing it tells you so on launch — install the PipeWire command-line utilities however your distribution ships them (the tool names are the same everywhere; the package that contains them varies by distro). On Fedora, for example:

```
sudo dnf install gtk4 python3-gobject pipewire pipewire-utils wireplumber
```

Clone the repo and make the script executable:

```
git clone https://github.com/NTMan/calibrate-room-rew.git
cd calibrate-room-rew
chmod +x per-device-eq.py
```

The built-in `Clean` profile lives in [`profiles/clean.json`](profiles/) next to the script. Shared/system profiles can go in `/usr/share/per-device-eq/profiles/`.

---

# Part A — Measure (REW + UMIK-1)

> Correcting **headphones or IEMs**, not a room? Skip Part A: get a parametric EQ from [AutoEQ](https://github.com/jaakkopasanen/AutoEq) (or <https://autoeq.app>) and use its **ParametricEQ** export in Part B.

### 1. Preparation

1. Download REW (Room EQ Wizard) from <https://www.roomeqwizard.com/>.
2. Prefer the build with embedded Java — newer system Java versions may make REW misbehave.
3. Connect the UMIK-1 measurement microphone to a USB port.
4. Download the microphone calibration file from miniDSP: <https://www.minidsp.com/products/acoustic-measurement/umik-1>
   - Choose `7163423_90deg.txt` if you measure loudspeakers horizontally at 90°.
   - REW does not recognise `Sens Factor`, but it supports a `Sensitivity: -18.3 dB` line.
   - Add the `Sensitivity: -18.3 dB` line at the very **beginning** of `7163423_90deg.txt`.
   - Remove the `Sens Factor` line entirely to avoid parsing issues in REW.

### 2. Set volume levels

```
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.9
wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.5
```

### 3. Configure REW

1. Open REW.
2. Go to **Preferences → Cal files**.
3. In the **Mic cal files** section:
   - Make sure your active input (e.g. `Default Device Default Input`) is selected.
   - Click the gear icon to the right of the row and ensure `Input device is a C weighted SPL meter` is **unchecked** — it must be off for calibrated microphones.
   - Provide the full path to your `7163423_90deg.txt` calibration file.

### 4. Measuring

1. Open the **Measure** tab.
2. Click **Check Levels** — the level should fall between −12 and −6 dBFS.
3. Click **Start Measuring**.

### 5. Export the filters

1. Open the **EQ** tab after measuring.
2. Set the target device type (usually *Full range speaker*).
3. Click **Match Response to Target**.
4. Export with **Export filter settings as text** and save the file.

This text file is what you import in Part B — no conversion needed.

---

# Part B — Apply with per-device-eq

### 1. Launch the app

```
./per-device-eq.py
```

The first time it starts, it installs its WirePlumber hook automatically and restarts WirePlumber once to activate it — you don't need to run anything by hand. (For a headless setup you can do this without the GUI: `./per-device-eq.py --install-hook`.)

### 2. Pick the output device

Use the **Device** dropdown. `★` marks the current default; the **Follow default** switch auto-selects whatever is playing.

### 3. Import your filters

Click **Import REW/AutoEQ…** and choose the text file exported in Part A (or an AutoEQ `ParametricEQ.txt` for headphones). The filters load, you hear the correction immediately, a profile is created and bound to that device, and it is saved automatically.

🎉 That's it. The EQ is re-applied on every reboot / reconnect — there is no manual "save" step.

### Everyday use

- **Profiles panel (right):** switch the profile bound to the current device; **New**, **Duplicate**, **Rename**, **Delete**. Select **Clean (no EQ)** to remove correction.
- **Bypass** to A/B against the uncorrected sound (runtime only, not saved).
- **Undo / Redo** (`Ctrl+Z` / `Ctrl+Shift+Z`, or the ↶ ↷ buttons) for accidental edits.
- **Tune by hand:** drag a point on the graph to move a band, click empty space to add one, right-click to remove; or edit Type/Freq/Gain/Q in the table. Scrolling over the table scrolls the list (it won't change values).
- **Per-channel EQ:** untick *Apply to all channels* to edit FL/FR separately.

---

## Command line

```
./per-device-eq.py --list            # list sinks (default marked with *)
./per-device-eq.py --list-profiles   # list profiles and their device bindings
./per-device-eq.py --inspect NAME    # dump a sink's params (node.name)
./per-device-eq.py --apply           # apply each bound profile to its sink now
./per-device-eq.py --install-hook    # (re)install the WirePlumber hook + config
```

## Files

| Path | What |
| --- | --- |
| `~/.config/per-device-eq/profiles/*.json` | your profiles |
| `~/.config/per-device-eq/bindings.json` | device (`node.name`) → profile map |
| `~/.local/share/wireplumber/scripts/90-per-device-eq.lua` | the persistence hook (a static script, installed verbatim from the repo) |
| `~/.local/state/wireplumber/per-device-eq` | the hook's saved graphs (written by the hook; restored at startup) |
| `~/.config/wireplumber/wireplumber.conf.d/90-per-device-eq.conf` | loads the hook and creates the `per-device-eq` metadata object |
| `profiles/clean.json`, `/usr/share/per-device-eq/profiles/` | built-in / system profiles |

## Known issues

- **Volume drop after enabling EQ (PipeWire).** On some sinks with hardware volume, the first volume change made *after* an in-node EQ is active can collapse the real output level (while the reported volume looks correct) until PipeWire/WirePlumber is restarted. Tracked upstream: <https://gitlab.freedesktop.org/pipewire/pipewire/-/work_items/5344>. Workaround: set the volume before enabling the EQ, or `systemctl --user restart wireplumber`.

## Legacy: EasyEffects converter

If you still use EasyEffects for some reason, the old [`rew2easyeffects.py`](rew2easyeffects.py) converter remains in the repo — paste the REW text into it to generate an EasyEffects-compatible configuration. For everything else, `per-device-eq` is the path this project recommends (see *Why this instead of EasyEffects* above).
