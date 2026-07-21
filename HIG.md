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
- Device card / Taste card shells: hand-built Gtk.Box cards.
  [Mikhail] compare against Adw.PreferencesGroup with boxed
  lists -- do we adopt the boxed-list look, or is the graph too
  wide for it?
- FL / FR selector: linked ToggleButtons of equal size --
  borrowed (segmented control; rule 1 satisfied, uniform grid).
- Band table: dense Gtk.Grid of DropDown + SpinButtons.
  [Mikhail] the HIG answer is a boxed list of Adw.SpinRow, one
  band per row -- taller, calmer, but the density is the point
  of this table. Decide: keep the dense grid as a deliberate
  exception, or go rows.
- Band actions row (Add band / Replace bands from file): raised
  icon+label buttons, left in one box -- closed by rules 2 and
  3 this sprint.
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
- Measurement window: [Mikhail] full separate walk -- it grew
  fast and was never audited.
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

Rules on the floor today: H1 linked means facets of one control;
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
