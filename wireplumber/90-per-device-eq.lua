-- 90-per-device-eq.lua — per-output-device EQ for PipeWire.
--
-- Part of per-device-eq (https://github.com/NTMan/calibrate-room-rew).
-- This script is STATIC: it ships in the repository and is installed verbatim;
-- per-device-eq.py never generates it. It is the single writer of the in-node
-- EQ filter-graph on each sink, and the sole owner of the persisted state.
--
-- How it works:
--   * graphs are kept in an in-memory table `graphs` (node.name -> graph string),
--     which is the runtime source of truth;
--   * on startup the table is seeded from WpState (~/.local/state/wireplumber/),
--     because the metadata object is empty after a PipeWire restart;
--   * the GUI/CLI push live edits into the "per-device-eq" metadata object; we
--     subscribe to its "changed" signal, update the table, apply to the live
--     node, and persist the table back to WpState;
--   * each sink gets its graph (re)applied when it reaches the "running" state,
--     which also covers hotplug / Bluetooth reconnect.
-- No background process of the user's is involved: the EQ lives in WirePlumber.

local log   = Log.open_topic("pde")
local META  = "per-device-eq"   -- metadata object name (live edits from the app)
local STATE = "per-device-eq"   -- WpState name -> ~/.local/state/wireplumber/per-device-eq

-- identity / flat graph: a single 0 dB filter. Applied to strip EQ when a device
-- is set to Clean. Must stay in sync with build_graph(0.0, []) in per-device-eq.py.
local FLAT = "{ nodes = [ { type = builtin name = eq label = param_eq config = "
          .. "{ filters = [ { type = bq_peaking, freq = 1000, gain = 0.0, q = 1.0 } ] } } ] }"

local graphs = {}            -- node.name -> graph string (runtime source of truth)
local nodes  = {}            -- node.name -> live Audio/Sink node proxy
local md     = nil           -- activated metadata proxy
local state  = State(STATE)  -- WpState handle (GKeyFile under ~/.local/state)

-- seed the table from persisted state (cold start: metadata is empty)
do
  local ok, p = pcall(function() return state:load() end)
  if ok and p ~= nil then
    pcall(function()
      for k, v in pairs(p) do graphs[k] = v end
    end)
  end
end

local function persist()
  pcall(function() state:save(graphs) end)
end

local function set_graph(node, graph)
  local ok, err = pcall(function()
    node:set_param("Props", Pod.Object {
      "Spa:Pod:Object:Param:Props", "Props",
      params = Pod.Struct { "audioconvert.filter-graph.0", graph },
    })
  end)
  if not ok then log.warning("set_param failed: " .. tostring(err)) end
end

local function apply(node, name)
  local g = graphs[name]
  if g then set_graph(node, g) end   -- no entry => Clean / unbound => leave alone
end

-- ---- metadata: the live channel from the GUI/CLI ----
md_om = ObjectManager {
  Interest { type = "metadata",
    Constraint { "metadata.name", "equals", META, type = "pw-global" } }
}
md_om:connect("object-added", function(_, m)
  if md then return end
  local feat = (type(Feature) == "table" and Feature.Metadata) and Feature.Metadata.DATA or nil
  m:activate(feat, function(_, err)
    if err then log.warning("metadata activate: " .. tostring(err)); return end
    md = m
    m:connect("changed", function(_, subject, key, typ, value)
      if value ~= nil and value ~= "" then
        graphs[key] = value
        local n = nodes[key]; if n then set_graph(n, value) end
      else
        graphs[key] = nil                  -- key cleared (Clean) -> strip EQ
        local n = nodes[key]; if n then set_graph(n, FLAT) end
      end
      persist()
    end)
  end)
end)
md_om:activate()

-- ---- sinks: (re)apply when a sink reaches running (ports negotiated) ----
sink_om = ObjectManager {
  Interest { type = "node",
    Constraint { "media.class", "equals", "Audio/Sink", type = "pw-global" } }
}
sink_om:connect("object-added", function(_, node)
  local name
  if not pcall(function() name = tostring(node.properties["node.name"]) end) or not name then
    return
  end
  nodes[name] = node
  pcall(function()
    node:connect("state-changed", function(n, _old, new)
      if new == "running" then apply(n, name) end
    end)
  end)
  local st; pcall(function() st = node:get_state() end)
  if st == "running" then apply(node, name) end
end)
sink_om:connect("object-removed", function(_, node)
  local name
  if pcall(function() name = tostring(node.properties["node.name"]) end) and name then
    nodes[name] = nil
  end
end)
sink_om:activate()

do
  local n = 0; for _ in pairs(graphs) do n = n + 1 end
  log.info("per-device-eq hook loaded; " .. n .. " persisted graph(s)")
end
