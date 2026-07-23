# -*- coding: utf-8 -*-
"""per-device-eq -- per-output-device parametric EQ for PipeWire.

Package layout:
  config       constants, paths, the WirePlumber component config
  eq           EQ model, filter-graph builders, biquad response, REW text
  profiles     ProfileStore (system/user profiles + bindings), Clean profile
  pipewire     runtime bridge to PipeWire and the WP Lua hook (via metadata)
  integration  install/uninstall of the hook, .desktop entry and icon
  cli          CLI command implementations
  gui          the GTK4 / libadwaita GUI (loads the .ui design)
"""

__version__ = "4.0.0"
