"""The mechanical HIG floor: every rule gets a violating tree
and a conforming one, plus the field regressions that minted the
rules in the first place -- the band-actions row as built after
round three must pass clean."""

from perdeviceeq import hig


def _btn(label=None, css=(), icon_only=False, tooltip=None,
         in_bar=False):
    return {"class": "GtkButton",
            "props": {"label": label, "css": list(css),
                      "icon_only": icon_only, "tooltip": tooltip,
                      "in_bar": in_bar},
            "children": []}


def _box(children, css=(), halign="fill", spacing=6,
         in_bar=False):
    return {"class": "GtkBox",
            "props": {"css": list(css), "halign": halign,
                      "spacing": spacing, "in_bar": in_bar},
            "children": list(children)}


def _rules(findings):
    return sorted({f["rule"] for f in findings})


def test_h1_linked_holds_facets_of_one_control():
    mixed = _box([_btn("Undo"), {"class": "GtkToggleButton",
                                 "props": {}, "children": []}],
                 css=["linked"])
    assert _rules(hig.lint(mixed)) == ["H1"]
    lone = _box([_btn("Undo")], css=["linked"])
    assert _rules(hig.lint(lone)) == ["H1"]
    pair = _box([_btn("Undo"), _btn("Redo")], css=["linked"])
    assert hig.lint(pair) == []


def test_h2_action_row_anchors_to_an_edge():
    floats = _box([_btn("Add"), _btn("Replace")], halign="center")
    got = hig.lint(floats)
    assert _rules(got) == ["H2"]
    assert "anchor" in got[0]["fix"]
    anchored = _box([_btn("Add"), _btn("Replace")],
                    halign="start")
    assert hig.lint(anchored) == []
    # a bar supplies its own geometry -- no finding there
    in_bar = _box([_btn("A", in_bar=True),
                   _btn("B", in_bar=True)],
                  halign="center", in_bar=True)
    assert hig.lint(in_bar) == []


def test_h3_flat_needs_a_structured_container():
    long_flat = _btn("Replace bands from file\u2026",
                     css=["flat"])
    got = hig.lint(long_flat)
    assert _rules(got) == ["H3"]
    assert "Calculator" in got[0]["fix"]
    short_flat = _btn("Add band", css=["flat"])
    assert hig.lint(short_flat) == []
    in_bar = _btn("Replace bands from file\u2026", css=["flat"],
                  in_bar=True)
    assert hig.lint(in_bar) == []
    raised = _btn("Replace bands from file\u2026")
    assert hig.lint(raised) == []


def test_h4_icon_only_button_describes_itself():
    mute = _btn(icon_only=True)
    assert _rules(hig.lint(mute)) == ["H4"]
    spoken = _btn(icon_only=True, tooltip="Export this profile")
    assert hig.lint(spoken) == []


def test_h5_spacing_sits_on_the_grid():
    off = _box([], spacing=7)
    got = hig.lint(off)
    assert _rules(got) == ["H5"]
    on = _box([], spacing=12)
    assert hig.lint(on) == []
    margins = {"class": "GtkBox",
               "props": {"margins": [6, 6, 5, 6]},
               "children": []}
    assert _rules(hig.lint(margins)) == ["H5"]


def test_h6_dialog_buttons_name_the_action():
    dlg = {"class": "AdwAlertDialog",
           "props": {"responses": [{"id": "y", "label": "Yes"},
                                   {"id": "n", "label": "No"}]},
           "children": []}
    got = hig.lint(dlg)
    assert len(got) == 2 and _rules(got) == ["H6"]
    verbs = {"class": "AdwAlertDialog",
             "props": {"responses": [
                 {"id": "cancel", "label": "Cancel"},
                 {"id": "import", "label": "Import"}]},
             "children": []}
    assert hig.lint(verbs) == []


def test_field_regression_band_actions_row_passes():
    """The row as built after design round three: raised
    icon+label buttons, one box, halign start, spacing 6 --
    the shape that minted H2 and H3 must itself pass clean."""
    row = _box([_btn("Add band"),
                _btn("Replace bands from file\u2026",
                     tooltip="Replace this channel's bands "
                             "from a parametric-EQ text file")],
               halign="start", spacing=6)
    assert hig.lint(row) == []


def test_field_regression_round_one_would_have_fired():
    """Round one as it shipped: two long flat labels in a
    grid-strand box mid-card. The floor must catch exactly the
    two mistakes the human eye caught."""
    row = _box([_btn("Add band", css=["flat"]),
                _btn("Replace bands from file\u2026",
                     css=["flat"])],
               halign="center", spacing=6)
    assert _rules(hig.lint(row)) == ["H2", "H3"]


def test_unknown_props_never_accuse():
    bare = {"class": "GtkButton", "props": {}, "children": []}
    assert hig.lint(bare) == []


def test_report_carries_rule_path_and_fix():
    got = hig.lint(_btn(icon_only=True))
    lines = hig.report(got)
    assert lines[0].startswith("H4 GtkButton")
    assert lines[1].startswith("   fix: ")


def _label(text, css=()):
    return {"class": "GtkLabel",
            "props": {"label": text, "css": list(css)},
            "children": []}


def test_h7_prose_needs_a_house():
    """Minted in the gone-state round: a free paragraph under
    the measuring card lost to the banner; the rule keeps loose
    prose from creeping back into bare columns."""
    prose = ("Its channel configuration changed, or it was "
             "unplugged.")
    loose = _box([_label(prose)])
    assert _rules(hig.lint(loose)) == ["H7"]
    housed = _box([_label(prose)], css=["card"])
    assert hig.lint(housed) == []
    listed = _box([_label(prose)], css=["boxed-list"])
    assert hig.lint(listed) == []
    caption = _box([_label("SNR 43.1 dB")])
    assert hig.lint(caption) == []
    heading = _box([_label(prose, css=["heading"])])
    assert hig.lint(heading) == []
    in_bar = _box([_label(prose)], in_bar=True)
    assert hig.lint(in_bar) == []
