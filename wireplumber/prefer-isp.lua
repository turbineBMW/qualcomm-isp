-- SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
-- SPDX-License-Identifier: MIT
-- Hide only the libcamera route replaced by a live ISP publisher. Keep the
-- SpaDevice and creation properties so fallback can be restored on disconnect.
local log = Log.open_topic ("qualcomm-isp")
local routes = {}
local providers = {}
local watched = setmetatable ({}, { __mode = "k" })

local function available (name)
  for _, raw in pairs (providers) do
    if raw == name then return true end
  end
  return false
end

local function reconcile (name)
  local route = routes[name]
  if not route then return end
  if available (name) then
    -- Also called after the asynchronous stock create hook finishes, so an
    -- ISP arriving during node activation cannot leave a duplicate behind.
    route.parent:store_managed_object (route.id, nil)
    route.hidden = true
  elseif route.hidden then
    route.hidden = false
    local event = route.source:call ("create-event",
        "create-libcamera-device-node", route.parent, nil)
    event:set_data ("node-sub-id", route.id)
    event:set_data ("factory", route.factory)
    event:set_data ("node-properties", Properties (route.properties))
    EventDispatcher.push_event (event)
    log:notice ("Restoring " .. name .. " (ISP publisher unavailable)")
  end
end

SimpleEventHook {
  name = "qualcomm-isp/remember-route",
  after = "monitor/libcamera/name-node",
  before = "monitor/libcamera/create-node",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "create-libcamera-device-node" },
    },
  },
  execute = function (event)
    local properties = event:get_data ("node-properties")
    local name = properties["node.name"]
    local parent = event:get_subject ()
    local id = event:get_data ("node-sub-id")
    routes[name] = {
      parent = parent, id = id, factory = event:get_data ("factory"),
      source = event:get_source (), properties = Properties (properties),
      hidden = available (name),
    }
    if not watched[parent] then
      watched[parent] = true
      parent:connect ("object-removed", function (device, removed_id)
        for key, route in pairs (routes) do
          if route.parent == device and route.id == removed_id then
            routes[key] = nil
          end
        end
      end)
    end
    if available (name) then
      properties["node.disabled"] = true
      event:set_data ("node-properties", properties)
    end
  end,
}:register ()

SimpleEventHook {
  name = "qualcomm-isp/reconcile-route",
  after = "monitor/libcamera/create-node",
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "create-libcamera-device-node" },
    },
  },
  execute = function (event)
    reconcile (event:get_data ("node-properties")["node.name"])
  end,
}:register ()

-- Keep these managers in the script environment for the plugin lifetime.
-- Local-only managers are garbage collected after the script loads.
nodes = ObjectManager {
  Interest {
    type = "node",
    Constraint { "media.class", "=", "Video/Source", type = "pw-global" },
  },
}
nodes:connect ("object-added", function (_, node)
  local raw = node.properties["qualcomm.isp.raw-node"]
  if raw and raw ~= "" then
    providers[node["bound-id"]] = raw
    reconcile (raw)
  end
end)
nodes:connect ("object-removed", function (_, node)
  local id = node["bound-id"]
  local raw = providers[id]
  providers[id] = nil
  if raw then reconcile (raw) end
end)
nodes:activate ()

-- Release cached SpaDevices on unplug; never restore a vanished sensor.
devices = ObjectManager {
  Interest {
    type = "device",
    Constraint { "device.api", "=", "libcamera", type = "pw-global" },
  },
}
devices:connect ("object-removed", function (_, device)
  for name, route in pairs (routes) do
    if route.properties["device.id"] == tostring (device["bound-id"]) then
      routes[name] = nil
    end
  end
end)
devices:activate ()
