# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Desktop demand for publishers sharing the exclusive CAMSS/ICP session lock."""


def camera_demand(objects, node_name):
    nodes = {o['id']: o.get('info', {}).get('props', {}) for o in objects
             if o.get('type') == 'PipeWire:Interface:Node'}
    ours = {key for key, props in nodes.items() if props.get('node.name') == node_name}
    # Every hardware publisher in this runtime uses camss-icp-lab.lock.
    others = {key for key, props in nodes.items() if key not in ours
              and props.get('media.class') == 'Video/Source'
              and props.get('qualcomm.isp.raw-node')}
    peers, competing = set(), False
    for obj in objects:
        if obj.get('type') != 'PipeWire:Interface:Link':
            continue
        info = obj.get('info', {})
        if info.get('state') == 'error':
            continue
        if info.get('output-node-id') in ours:
            peers.add(info['input-node-id'])
        elif info.get('output-node-id') in others:
            competing = True
    return len(peers), competing


def should_release(count, competing, now, last_seen, idle_seconds):
    # A failed graph query is unknown, not evidence the client disconnected.
    # Keep same-camera debounce, but never make a selected peer wait for it.
    return count == 0 and (competing or now - last_seen >= idle_seconds)
