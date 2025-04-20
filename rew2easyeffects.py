import gi
import re
import os

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib

class REW2EasyEffects(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.example.REW2EasyEffects")
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = Gtk.ApplicationWindow(application=self)
            self.window.set_title("REW to EasyEffects Converter")
            self.window.set_default_size(700, 250)

            grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            self.window.set_child(grid)

            self.left_entry = Gtk.Entry(hexpand=True)
            self.right_entry = Gtk.Entry(hexpand=True)
            self.output_entry = Gtk.Entry(hexpand=True)

            grid.attach(Gtk.Label(label="Left channel REW .txt"), 0, 0, 1, 1)
            grid.attach(self.left_entry, 1, 0, 1, 1)
            grid.attach(self._browse_button(self.left_entry), 2, 0, 1, 1)

            grid.attach(Gtk.Label(label="Right channel REW .txt"), 0, 1, 1, 1)
            grid.attach(self.right_entry, 1, 1, 1, 1)
            grid.attach(self._browse_button(self.right_entry), 2, 1, 1, 1)

            grid.attach(Gtk.Label(label="Output text file"), 0, 2, 1, 1)
            grid.attach(self.output_entry, 1, 2, 1, 1)
            grid.attach(self._browse_button(self.output_entry, save=True), 2, 2, 1, 1)

            self.mode_group = []
            self.left_radio = Gtk.CheckButton.new_with_label("Use only LEFT")
            self.right_radio = Gtk.CheckButton.new_with_label("Use only RIGHT")
            self.avg_radio = Gtk.CheckButton.new_with_label("Average L + R")

            self.left_radio.set_group(None)
            self.right_radio.set_group(self.left_radio)
            self.avg_radio.set_group(self.left_radio)
            self.avg_radio.set_active(True)

            grid.attach(self.left_radio, 0, 3, 1, 1)
            grid.attach(self.right_radio, 1, 3, 1, 1)
            grid.attach(self.avg_radio, 2, 3, 1, 1)

            generate_button = Gtk.Button(label="Generate TXT")
            generate_button.connect("clicked", self.on_generate_clicked)
            grid.attach(generate_button, 0, 4, 3, 1)

        self.window.present()

    def _browse_button(self, entry, save=False):
        button = Gtk.Button(label="Browse...")

        def on_clicked(_):
            dialog = Gtk.FileDialog.new()
            if save:
                dialog.save(self.window, None, self._on_file_chosen(entry))
            else:
                dialog.open(self.window, None, self._on_file_chosen(entry))

        button.connect("clicked", on_clicked)
        return button

    def _on_file_chosen(self, entry):
        def callback(dialog, result):
            try:
                file = dialog.open_finish(result)
                entry.set_text(file.get_path())
            except GLib.Error:
                pass
        return callback

    def parse_filters(self, txt_path):
        filters = []
        pattern = re.compile(r"Filter\s+\d+: ON +PK +Fc +([\d.]+) Hz +Gain +([\-\d.]+) dB +Q +([\d.]+)")
        with open(txt_path) as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    freq, gain, q = map(float, match.groups())
                    filters.append((freq, gain, q))
        return filters

    def average_filters(self, l_filters, r_filters):
        if len(l_filters) != len(r_filters):
            print("Filter count mismatch, cannot average.")
            return l_filters
        return [
            (
                (l[0] + r[0]) / 2,
                (l[1] + r[1]) / 2,
                (l[2] + r[2]) / 2
            )
            for l, r in zip(l_filters, r_filters)
        ]

    def on_generate_clicked(self, _):
        left_file = self.left_entry.get_text()
        right_file = self.right_entry.get_text()
        output_file = self.output_entry.get_text()

        if not output_file:
            print("Output path missing")
            return

        if self.left_radio.get_active():
            if not os.path.exists(left_file):
                print("Missing left file")
                return
            filters = self.parse_filters(left_file)

        elif self.right_radio.get_active():
            if not os.path.exists(right_file):
                print("Missing right file")
                return
            filters = self.parse_filters(right_file)

        else:
            if not (os.path.exists(left_file) and os.path.exists(right_file)):
                print("Missing one of the input files")
                return
            left_filters = self.parse_filters(left_file)
            right_filters = self.parse_filters(right_file)
            filters = self.average_filters(left_filters, right_filters)

        with open(output_file, "w") as f:
            for i, (freq, gain, q) in enumerate(filters, start=1):
                f.write(f"Filter {i}: ON  PK       Fc   {freq:.2f} Hz  Gain  {gain:.2f} dB  Q  {q:.3f}\n")

        print("TXT saved to:", output_file)

if __name__ == "__main__":
    app = REW2EasyEffects()
    app.run([])
