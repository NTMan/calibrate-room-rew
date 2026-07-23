# HIG pass -- charter and worklist

## Charter

The reference is not taste, it is inventory: the libadwaita
widget gallery (Adwaita Demo) and the GNOME core apps --
Settings, Calendar, Files, Calculator. The method is to borrow,
never to invent: every control in this app gets a verdict,
either `borrowed(<source>)` naming the pattern it copies, or
`self-made -> replace` naming the gallery widget that should
stand there instead. When the two of us disagree, a screenshot
of ours next to the reference decides.

## Rules earned in the field (design rounds of the .pdeq sprint)

1. `linked` means facets of ONE control -- undo/redo, spin
   plus/minus. Two independent verbs never share a pill.
2. An action group under a list sits LEFT, in one box, flat or
   raised as rule 3 says; grid cells are not alignment.
3. `flat` lives where a structured container teaches the
   pattern -- header bars, toolbars, grids of uniform targets.
   A lone button with a long label needs its background;
   Calculator is the precedent that dropping flat is not a sin.

## Worklist

Verdicts below are provisional, read from the code by someone
who cannot run the GUI; [Mikhail] marks the items where the
deciding eye must be yours.

- Header bar: undo/redo linked pair -- borrowed (HeaderBar
  history pattern). Sink dropdown + pin toggle: [Mikhail]
  compare against Adw.SplitButton and the Settings device
  pickers; is the pin discoverable?
- Device card / Taste card shells: SETTLED (the pickers-vs-
  Sound round). Settings' row is flat because the row IS the
  control -- one action per strip; our header multiplexes two
  (collapse + pick), and the GNOME grammar for a multiplexed
  header is the expander row: arrow at the end, the whole strip
  toggles, suffix controls keep their own clicks.
  CollapsibleCard already carried that grammar; the one delta
  was chrome, and the header pickers now wear flat in-row style
  (the suffix idiom). Raised was never a sin -- Sound's own
  Test... button is raised -- it was compensating for the wrong
  container. Ratchet: the card header is a structured container
  for rule H3 (css card-header -> in_bar). The graph stays in a
  card, not a boxed list -- too wide, deliberately.
  [Mikhail] confirm by screenshot.
- FL / FR selector: linked ToggleButtons of equal size --
  borrowed (segmented control; rule 1 satisfied, uniform grid).
- Band table: SETTLED (the table-of-controls round). The
  dense grid stays, a deliberate exception -- density is the
  point, and EasyEffects is the libadwaita-world precedent for
  exactly this widget. Two clarifications made it easy: HIG
  flatness is about buttons and pickers, never about entry-like
  controls (spins carry chrome by nature, like Sound's own
  sliders), and Settings' Search list IS a table of controls --
  its grammar, state then a vertical separator then the action,
  is borrowed verbatim: the band row now draws a separator
  between On and the delete blade.
  [Mikhail] confirm by screenshot.
- Band actions row (Add band / Replace bands from file): raised
  icon+label buttons, left in one box -- closed by rules 2 and
  3 this sprint. Reopened round four on new field evidence: once
  the pickers went flat everywhere, the pair stood as the last
  chrome on the card. Verdict: not naked flat (rule 3 holds) --
  the box declares the toolbar class and the buttons declare
  flat themselves (round five: the stylesheet's own flattening
  was not trusted alone). The auditor's ratchet: the toolbar
  class counts as a bar.
- Eye toggle (measured curves): icon-only flat in a card corner.
  [Mikhail] against Calendar's icon buttons -- fine as is?
- Preamp spin + Auto: linked pair -- borrowed (facets of one
  value; rule 1).
- Profile picker popover: list rows + Import + pin.
  [Mikhail] against Files' popover lists and Settings' boxed
  lists; row affordances (hover chrome, chevrons) to check.
- Export wizard: Adw.NavigationView, PreferencesGroup rows,
  SwitchRow / ComboRow / SpinRow -- borrowed throughout; the
  one-page picker rule (no scrolling on defaults) holds as of
  this sprint.
- Dialogs: Adw.AlertDialog everywhere -- borrowed. Audit the
  button labels against HIG verb rules (no Yes/No, name the
  action).
- Measurement window, round one (three field findings):
  the Takes fold snapped while the main cards breathe -- a
  self-made per-row visibility toggle where the borrowed
  grammar is the Revealer; it now folds through the same
  200 ms slide as CollapsibleCard, and its face declares
  card-header for rule H3. The calibration buttons read set
  and unset apart at a glance: a chosen file wears its capsule
  letter, a check mark and its name, with the path and the cal
  sha16 in the tooltip -- the same fingerprint the rig block
  records. The mic pickers stay Gtk.DropDown with native
  chrome: this is a form body, not a row-as-control, and flat
  here is exactly what rule H3 exists to catch; the settled
  TARGET is row grammar -- Profile name, Measurement mic and
  Calibration as one boxed group -- in a dedicated round.
  Round one confirmed in the field (fold, walls, cal marks,
  EQ range in sight).
- Measurement window, round two: the mic and calibration
  blocks join the Profile name row's grammar -- AdwActionRow
  with title and subtitle, the dropdowns and cal buttons as
  flat suffixes, exactly the Sound-settings shape the
  round-one verdict named. The auditor's ratchet: Adw rows
  join _BARS as the second legal home for flat chrome (a
  suffix inside a boxed row IS the row's own control).
  Field verdict: revised -- the wide suffixes crushed the
  title column into hyphenation (Calibrat-ion is the tell),
  and GtkDropDown ignores the flat class, so the pickers
  kept their chrome. The _BARS ratchet stands.
- Measurement window, round three: Sound's actual grammar is
  the ComboRow -- the row IS the picker, flat by birth, the
  selected value ellipsized by the row itself. Measurement
  mic and Capsules become AdwComboRows; Calibration becomes
  one ActionRow per capsule with the state in the subtitle
  (check mark and file name, or "runs raw"), a flat
  Choose/Change button as the suffix, path and cal sha16 in
  the row tooltip. No suffix is ever wider than its title
  again. Confirmed in the field on both rig shapes: the
  stereo E.A.R.S renders L and R calibration rows, the mono
  UMIK-1 a single Mono calibration row; the vertical cost was
  accepted, the column had the headroom. The bench's answer
  to "may one row hold several picker controls": the HIG has
  no such ban. The rules that actually bit were narrower --
  a ComboRow IS one choice by construction (the row is the
  control), and a suffix must never out-grow its title. Two
  modest suffix controls in one ActionRow stay legal (Sound's
  own rows carry a control beside a button); our split was
  width plus the Capsules warning earning a subtitle of its
  own, not a prohibition.
- Measurement window, rounds four and five (field review): the
  ring's transport hung centered BETWEEN the column axes -- the
  center now lives on a real Gtk.Grid, two column axes shared
  by mic icons, capsule pickers, play and stop (mono keeps the
  centered pair). The card's macro layout follows the field
  spec: the volume fader holds the card's left edge at ring
  height, the auto-level button joins the status line (its lead
  bin size-grouped with the fader), and ring plus status center
  together in the space right of the fader -- one axis, no
  card-versus-ring drift. And an empty fold promises nothing:
  the chevron is born hidden, arrives with the first take and
  leaves with the last (round five caught the fresh-session
  gap the refresh path missed).
- Toasts vs status labels: the wizard uses both. [Mikhail]
  decide the rule: toasts for outcomes of actions, labels for
  standing state?

## The auditor

Three tiers, honestly delimited. Tier one is the GATE:
perdeviceeq/hig.py holds mechanical rules that run
deterministically over a widget tree -- each finding names the
rule, the path, and a suggested replacement.
tools/hig_audit.py walks the LIVE widgets under a display
(xvfb in CI); the rules themselves are pure dicts-in,
findings-out and stay under test with no GTK in sight. Tier two
is EVIDENCE: the CI screenshot rig (lands with the CI day)
renders each surface to PNG artifacts so every merge request
carries pictures, and a pixel diff flags unintended drift. Tier
three is JUDGMENT: taste calls like dense-grid-vs-rows have no
mechanical judge -- GNOME reviews Circle apps with human eyes,
and so do we. The tiers connect by a RATCHET: every judgment
call this file settles gets minted into a tier-one rule, so the
gate grows stricter with every dispute instead of re-arguing it.

Rules on the floor today: H1 linked means one instrument --
facets of one control, or one value control with its
ToggleButton modifiers (the --window round's verdict; a spin
with its Auto, a picker with its follow pin) -- and, the letter
of the libadwaita style-classes doc, a linked box carries no
spacing;
H2 an action group anchors to an edge; H3 flat needs a
structured container; H4 an icon-only button describes itself;
H5 spacing sits on the 6px grid; H6 dialog buttons name the
action. H1-H3 are the .pdeq sprint's earned rules; H4-H6 are the
HIG's own letter. Run: `xvfb-run -a python3 tools/hig_audit.py
--peq-view`.

## Process

Top to bottom, one item per sitting; each contested item gets a
screenshot of ours against the reference before any code moves.
The charter and rules above are amended in this file as rounds
settle them.
