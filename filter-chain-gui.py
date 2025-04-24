import gi
import os
import subprocess
import json
import tempfile
import re
from uuid import uuid4
from pathlib import Path

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GObject, GLib

CONFIG_PATH = "/tmp/pipewire_eq_gui_config.json"

class Filter:
    def __init__(self, ftype="PK", freq=1000.0, gain=0.0, q=1.0, enabled=True):
        self.ftype = ftype
        self.freq = freq
        self.gain = gain
        self.q = q
        self.enabled = enabled
        self.container = None
        self.bypass_button = None
        self.gain_scale = None
        self.labels = []
        self.freq_label = None
        self.gain_label = None
        self.q_label = None

class EQEditor(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.pipewire.eqeditor")
        self.filters = []
        self.device_name = "Unknown"
        self.debounce_source = None

    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("PipeWire Filter-Chain EQ Editor")
        self.window.set_default_size(900, 500)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.window.set_child(vbox)

        header = Gtk.HeaderBar()
        self.device_label = Gtk.Label(label=f"Active device: {self.device_name}")
        header.set_title_widget(self.device_label)
        vbox.append(header)

        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.band_count = Gtk.SpinButton.new_with_range(0, 100, 1)
        self.band_count.set_value(len(self.filters))
        self.band_count.connect("value-changed", self.on_band_count_changed)
        self.band_label = Gtk.Label(label="Bands")
        control_box.append(self.band_label)
        control_box.append(self.band_count)
        header.pack_start(control_box)

        import_button = Gtk.Button(label="Import REW")
        import_button.connect("clicked", self.import_rew_filters)
        header.pack_start(import_button)

        export_button = Gtk.Button(label="Export .conf")
        export_button.connect("clicked", self.export_as_conf)
        header.pack_start(export_button)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_hexpand(True)
        self.scroll.set_vexpand(True)
        vbox.append(self.scroll)

        self.listbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.scroll.set_child(self.listbox)

        self.window.present()

    def on_band_count_changed(self, spin):
        target = int(spin.get_value())
        while len(self.filters) < target:
            self.add_filter()
        while len(self.filters) > target:
            self.remove_last_filter()

    def add_filter(self):
        filt = Filter()
        self.filters.append(filt)
        self.render_filter(filt)

    def remove_last_filter(self):
        if self.filters:
            self.filters.pop()
            child = self.listbox.get_last_child()
            if child:
                self.listbox.remove(child)

    def load_from_active_filter_chain(self):
        try:
            result = subprocess.run(["pw-dump"], capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            for obj in data:
                if obj.get("type") == "PipeWire:Node" and obj.get("info", {}).get("props", {}).get("media.class") == "Stream/Output/Audio":
                    self.device_name = obj.get("info", {}).get("props", {}).get("node.description", "Unknown")
                if obj.get("type") == "PipeWire:Module" and obj.get("name") == "libpipewire-module-filter-chain":
                    args = obj.get("args", {})
                    graph = args.get("filter.graph", {})
                    nodes = graph.get("nodes", [])
                    for node in nodes:
                        if node.get("type") == "builtin" and node.get("label") == "eq":
                            ctrl = node.get("control", {})
                            for key in ctrl:
                                val = ctrl[key]
                                parts = [float(x) for x in val.split(":")]
                                self.filters.append(Filter(freq=parts[0], gain=parts[1], q=parts[2]))
        except Exception as e:
            print(f"Could not load current filter chain: {e}")

    def generate_filter_chain_config(self):
        lines = []
        lines.append("context.modules = [")
        lines.append("  { name = libpipewire-module-filter-chain")
        lines.append("    args = {")
        lines.append("      node.name = \"eq_gui\"")
        lines.append("      node.description = \"EQ GUI\"")
        lines.append("      media.name = \"EQ GUI\"")
        lines.append("      filter.graph = {")
        lines.append("        nodes = [")

        for i, flt in enumerate(self.filters):
            if not flt.enabled:
                continue
            lines.append(
                f"          {{ type = builtin name = eq{i} label = eq control = {{ {i} = {flt.freq:.1f}:{flt.gain:.1f}:{flt.q:.2f} }} }}"
            )

        lines.append("        ]")
        lines.append("      }")
        lines.append("      capture.props = { node.name = \"eq_capture\" media.class = \"Stream/Output/Audio\" }")
        lines.append("      playback.props = { node.name = \"eq_playback\" media.class = \"Stream/Input/Audio\" }")
        lines.append("    }")
        lines.append("  }")
        lines.append("]")
        return "\n".join(lines)

    def export_as_conf(self, _):
        try:
            with open("exported_eq.conf", "w") as f:
                f.write(self.generate_filter_chain_config())
            print("Exported to exported_eq.conf")
        except Exception as e:
            print(f"Export failed: {e}")

    def schedule_live_apply(self):
        if self.debounce_source is not None and self.debounce_source > 0:
            try:
                GLib.source_remove(self.debounce_source)
            except Exception as e:
                print(f"[!] Failed to remove debounce source: {e}")
        self.debounce_source = GLib.timeout_add_seconds(1, self.apply_filters_live)

    def _apply_filters_wrapper(self):
        self.apply_filters_live()
        self.debounce_source = None
        return False

    def apply_filters_live(self):
        try:
            result = subprocess.run(["pw-dump"], capture_output=True, text=True)
            data = json.loads(result.stdout)

            node_id = None
            for obj in data:
                if obj.get("type") == "PipeWire:Node" and obj.get("info", {}).get("props", {}).get("node.name") == "eq_playback":
                    node_id = obj.get("id")
                    break

            if node_id is None:
                print("[!] Filter chain node not found")
                return False

            control_parts = []
            for i, flt in enumerate(self.filters):
                if flt.enabled:
                    control_parts.append(f"{i} = {flt.freq:.1f}:{flt.gain:.1f}:{flt.q:.3f}")

            arg = "{ control = { " + ", ".join(control_parts) + " } }"
            subprocess.run(["pw-cli", "set-param", str(node_id), "Props", arg])
            print("[✓] Filters applied live")

        except Exception as e:
            print(f"[!] Failed to apply live filters: {e}")
        return False

    def import_rew_filters(self, _):
        dialog = Gtk.FileDialog.new()
        dialog.open(self.window, None, self.on_rew_file_chosen)

    def on_rew_file_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                path = file.get_path()
                self.load_rew_txt_file(path)
        except Exception as e:
            print(f"Failed to import: {e}")

    def load_rew_txt_file(self, path):
        try:
            with open(path) as f:
                lines = f.readlines()

            self.filters.clear()
            for child in list(self.listbox):
                self.listbox.remove(child)

            pattern = re.compile(r"Filter\s+\d+:\s+ON\s+(\w+)\s+Fc\s+([\d.]+)\s+Hz\s+Gain\s+([\-\d.]+)\s+dB(?:\s+Q\s+([\d.]+))?")

            for line in lines:
                line = line.strip()
                if line.startswith("Filter") and ": ON" in line:
                    try:
                        match = pattern.search(line)
                        if match:
                            ftype = match.group(1)
                            freq = float(match.group(2))
                            gain = float(match.group(3))
                            q = float(match.group(4)) if match.group(4) else 1.0
                            self.filters.append(Filter(ftype=ftype, freq=freq, gain=gain, q=q))
                        else:
                            print(f"Skipping invalid line: {line} (no match)")
                    except Exception as e:
                        print(f"Skipping invalid line: {line} ({e})")

            self.band_count.set_value(len(self.filters))
            for f in self.filters:
                self.render_filter(f)

            print(f"Imported {len(self.filters)} filters from REW export")
        except Exception as e:
            print(f"Failed to import REW file: {e}")

    def on_band_count_changed(self, spin):
        target = int(spin.get_value())
        while len(self.filters) < target:
            self.add_filter()
        while len(self.filters) > target:
            self.remove_last_filter()

    def add_filter(self):
        filt = Filter()
        self.filters.append(filt)
        self.render_filter(filt)

    def remove_last_filter(self):
        if self.filters:
            self.filters.pop()
            child = self.listbox.get_last_child()
            if child:
                self.listbox.remove(child)

    def render_filter(self, filt):
        frame = Gtk.Frame()
        filt.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        filt.freq_label = Gtk.Label(label=f"{int(filt.freq)} Hz")
        filt.gain_label = Gtk.Label(label=f"{filt.gain:.1f} dB")
        filt.q_label = Gtk.Label(label=f"Q={filt.q:.2f}")
        filt.labels = [filt.freq_label, filt.gain_label, filt.q_label]

        gain_adjust = Gtk.Adjustment(value=filt.gain, lower=-24, upper=24, step_increment=0.1)
        gain_scale = Gtk.Scale(orientation=Gtk.Orientation.VERTICAL, adjustment=gain_adjust)
        gain_scale.set_digits(1)
        gain_scale.set_vexpand(True)
        gain_scale.set_inverted(True)  # Flip the direction
        filt.gain_scale = gain_scale

        def on_gain_changed(w):
            filt.gain = w.get_value()
            filt.gain_label.set_text(f"{filt.gain:.1f} dB")
            self.schedule_live_apply()

        gain_scale.connect("value-changed", on_gain_changed)

        edit_button = Gtk.Button(label="⚙️")
        edit_button.connect("clicked", lambda b: self.open_filter_editor(filt))

        bypass_button = Gtk.Button(label="B")
        filt.bypass_button = bypass_button
        bypass_button.connect("clicked", lambda b: self.toggle_bypass(filt))

        filt.container.append(edit_button)
        filt.container.append(gain_scale)
        filt.container.append(filt.freq_label)
        filt.container.append(filt.gain_label)
        filt.container.append(filt.q_label)
        filt.container.append(bypass_button)

        frame.set_child(filt.container)
        self.listbox.append(frame)
        self.set_filter_sensitivity(filt)

    def toggle_bypass(self, filt):
        filt.enabled = not filt.enabled
        self.set_filter_sensitivity(filt)
        self.schedule_live_apply()

    def set_filter_sensitivity(self, filt):
        filt.gain_scale.set_sensitive(filt.enabled)
        for label in filt.labels:
            label.set_sensitive(filt.enabled)
        if filt.bypass_button:
            filt.bypass_button.set_sensitive(True)

    def open_filter_editor(self, filt):
        dialog = Gtk.Dialog(title="Edit Filter", transient_for=self.window)
        content = dialog.get_content_area()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

        type_combo = Gtk.ComboBoxText()
        for t in ["PK", "LowShelf", "HighShelf"]:
            type_combo.append_text(t)
        type_combo.set_active(["PK", "LowShelf", "HighShelf"].index(filt.ftype))
        type_combo.connect("changed", lambda c: setattr(filt, "ftype", c.get_active_text()))

        freq_adjust = Gtk.Adjustment(value=filt.freq, lower=20, upper=20000, step_increment=10)
        freq_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=freq_adjust)
        freq_scale.set_digits(0)
        freq_label = Gtk.Label(label=f"{int(filt.freq)} Hz")

        def on_freq_changed(w):
            filt.freq = w.get_value()
            freq_label.set_text(f"{int(filt.freq)} Hz")
            filt.freq_label.set_text(f"{int(filt.freq)} Hz")

        freq_scale.connect("value-changed", on_freq_changed)

        q_adjust = Gtk.Adjustment(value=filt.q, lower=0.1, upper=10.0, step_increment=0.1)
        q_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=q_adjust)
        q_scale.set_digits(2)
        q_value = Gtk.Label(label=f"Q={filt.q:.2f}")

        def on_q_changed(w):
            filt.q = w.get_value()
            q_value.set_text(f"Q={filt.q:.2f}")
            filt.q_label.set_text(f"Q={filt.q:.2f}")

        q_scale.connect("value-changed", on_q_changed)

        bypass_check = Gtk.CheckButton(label="Bypass")
        bypass_check.set_active(not filt.enabled)
        def on_bypass_toggled(w):
            filt.enabled = not w.get_active()
            filt.gain_scale.set_sensitive(filt.enabled)
            for label in filt.labels:
                label.set_sensitive(filt.enabled)

        bypass_check.connect("toggled", on_bypass_toggled)

        box.append(Gtk.Label(label="Type"))
        box.append(type_combo)
        box.append(Gtk.Label(label="Frequency (Hz)"))
        box.append(freq_scale)
        box.append(freq_label)
        box.append(Gtk.Label(label="Q"))
        box.append(q_scale)
        box.append(q_value)
        box.append(bypass_check)

        content.append(box)

        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

app = EQEditor()
app.run()
