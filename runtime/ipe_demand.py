# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Desktop demand for publishers sharing the exclusive CAMSS/ICP session lock."""
import codecs
import json
import os
import subprocess
import threading

MONITOR_COMMAND = ('pw-dump', '--monitor', '--no-colors')
# A graph dump is a few hundred kilobytes. Anything far beyond that is not one.
MAX_PENDING = 32 << 20
DEMAND_TYPES = ('PipeWire:Interface:Node', 'PipeWire:Interface:Link')


class GraphState:
    """The PipeWire graph, kept current from `pw-dump --monitor` output.

    The monitor prints one JSON array with every object, then one array per
    change. A changed object is printed whole; a removed one has no info.
    """

    def __init__(self):
        self.objects = {}
        self.batches = 0
        self._pending = ''
        self._decoder = json.JSONDecoder()

    def feed(self, text):
        """Apply monitor output. True if a node or link was added, changed or removed."""
        self._pending += text
        relevant = False
        while True:
            self._pending = self._pending.lstrip()
            if not self._pending:
                break
            try:
                batch, end = self._decoder.raw_decode(self._pending)
            except ValueError:
                if len(self._pending) > MAX_PENDING:
                    raise
                break   # the array is not complete yet
            self._pending = self._pending[end:]
            self.batches += 1
            for obj in batch:
                key = obj.get('id')
                old = self.objects.get(key)
                if obj.get('info') is None:
                    self.objects.pop(key, None)
                    current = None
                else:
                    self.objects[key] = current = obj
                if any(o is not None and o.get('type') in DEMAND_TYPES for o in (old, current)):
                    relevant = True
        return relevant


class GraphMonitor:
    """Follow the graph through one long-lived monitor process.

    `changed` is set whenever a node or link changes and whenever the monitor
    stops being usable, so that a waiter can sleep on it without polling.
    """

    def __init__(self, command=MONITOR_COMMAND, restart_seconds=5):
        self.changed = threading.Event()
        self._command = command
        self._restart_seconds = restart_seconds
        self._lock = threading.Lock()
        self._state = None
        self._stopping = threading.Event()
        self._process = None
        self._thread = threading.Thread(target=self._run, name='graph-monitor', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stopping.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
        self._thread.join(5)

    def snapshot(self):
        """Every graph object, or None while the monitor has no complete graph."""
        with self._lock:
            if self._state is None or not self._state.batches:
                return None
            return list(self._state.objects.values())

    def _run(self):
        while not self._stopping.is_set():
            state = GraphState()
            process = None
            try:
                process = self._process = subprocess.Popen(
                    self._command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                decode = codecs.getincrementaldecoder('utf-8')().decode
                descriptor = process.stdout.fileno()
                while chunk := os.read(descriptor, 1 << 16):
                    with self._lock:
                        first = not state.batches
                        relevant = state.feed(decode(chunk))
                        self._state = state
                    if relevant or (first and state.batches):
                        self.changed.set()
            except (OSError, ValueError):
                pass
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    process.stdout.close()
            with self._lock:
                self._state = None
            self.changed.set()   # the waiter falls back to polling
            self._stopping.wait(self._restart_seconds)


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
