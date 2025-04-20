🇷🇺 Читайте инструкцию на русском: [README.ru.md](https://github.com/NTMan/calibrate-room-rew/blob/main/README.ru.md)

**Guide: Room Correction with REW, UMIK-1 and PipeWire (Linux)**

---

### 1. Preparation

1. Download REW (Room EQ Wizard) from https://www.roomeqwizard.com/
2. Prefer the version with embedded Java, as newer Java versions may cause REW to malfunction.
3. Connect the UMIK-1 measurement microphone to a USB port.
4. Download the microphone calibration file from miniDSP: https://www.minidsp.com/products/acoustic-measurement/umik-1
   - Choose the `7163423_90deg.txt` file if you're measuring loudspeakers horizontally at 90°.
   - REW does not recognize the "Sens Factor" parameter but supports a `Sensitivity: -18.3 dB` line.
   - Manually add the `Sensitivity: -18.3 dB` line at the very beginning of the `7163423_90deg.txt` file.
   - Completely remove the `Sens Factor` line to avoid parsing issues in REW.

---

### 2. Set volume levels

Use `wpctl` to set appropriate microphone and playback levels:
```bash
wpctl set-volume @DEFAULT_AUDIO_SOURCE@ 0.9
wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.5
```

---

### 3. Configure REW

1. Open REW.
2. Go to **Preferences → Cal files**.
3. In the **Mic cal files** section:
   - Ensure your active input (e.g., `Default Device Default Input`) is selected.
   - Click the gear icon to the right of the row and ensure that the checkbox `Input device is a C weighted SPL meter` is **unchecked** — this must be off for calibrated microphones.
   - Provide the full path to your `7163423_90deg.txt` calibration file.

---

### 4. Measuring

1. Open the **Measure** tab.
2. Click **Check Levels** — level should fall between -12 and -6 dBFS.
3. Click **Start Measuring**.

---

### 5. Exporting filters

- Open the **EQ** tab after measuring.
- Set the target device type (usually *Full range speaker*).
- Click **Match Response to Target**.
- Export filters via **Export filter settings as text**.

---

### 6. Convert REW filters to EasyEffects format

Copy the contents of the REW-exported text file and paste it into the [`rew2easyeffects.py`](https://github.com/NTMan/calibrate-room-rew/blob/main/rew2easyeffects.py) script from this repository to generate a configuration file compatible with EasyEffects.

---

### 7. Applying filters in EasyEffects

1. Open the **Equalizer** tab.
2. Enable the desired mode (usually FIR).
3. Load the generated file using **Presets**, or import it manually as `.json` or `.txt`.
4. Make sure the equalizer is enabled and active.

🎉 Congratulations! Your audio system has been room-corrected.
