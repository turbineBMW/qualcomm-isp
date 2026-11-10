#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Shared camera demand: switching, cancelled startup and graph failures."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import json
import time
from ipe_demand import GraphMonitor, GraphState, camera_demand, should_release


def node(n, name, isp=True):
    return dict(id=n, type='PipeWire:Interface:Node', info=dict(props={
        'node.name': name, 'media.class': 'Video/Source',
        'qualcomm.isp.raw-node': 'sensor-' + name if isp else ''}))


def link(source, sink, state='active'):
    return dict(type='PipeWire:Interface:Link', info={
        'output-node-id': source, 'input-node-id': sink, 'state': state})


class DemandTests(unittest.TestCase):
    def setUp(self):
        self.graph = [node(1, 'front'), node(2, 'rear'), node(3, 'usb', False)]

    def test_selected_camera_keeps_hardware(self):
        count, peer = camera_demand(self.graph + [link(1, 9), link(2, 10)], 'front')
        self.assertEqual((count, peer), (1, True))
        self.assertFalse(should_release(count, peer, 100, 0, 5))

    def test_switch_releases_even_during_start_grace(self):
        count, peer = camera_demand(self.graph + [link(2, 9, 'paused')], 'front')
        self.assertEqual((count, peer), (0, True))
        self.assertTrue(should_release(count, peer, 100, 110, 5))

    def test_cancelled_rear_start_yields_to_front(self):
        count, peer = camera_demand(self.graph + [link(1, 9)], 'rear')
        self.assertTrue(should_release(count, peer, 100, 100, 5))

    def test_unrelated_camera_does_not_bypass_idle_grace(self):
        count, peer = camera_demand(self.graph + [link(3, 9)], 'front')
        self.assertEqual((count, peer), (0, False))
        self.assertFalse(should_release(count, peer, 104, 100, 5))
        self.assertTrue(should_release(count, peer, 105, 100, 5))

    def test_failed_graph_query_keeps_session(self):
        self.assertFalse(should_release(None, False, 100, 0, 5))

    def test_failed_peer_link_does_not_request_hardware(self):
        self.assertEqual(camera_demand(self.graph + [link(2, 9, 'error')], 'front'), (0, False))

    def test_multiple_links_count_one_client(self):
        self.assertEqual(camera_demand(self.graph + [link(1, 9), link(1, 9)], 'front'), (1, False))



def dumped(*batches):
    return ''.join(json.dumps(batch, indent=2) + '\n' for batch in batches)


class GraphStateTests(unittest.TestCase):
    def setUp(self):
        self.state = GraphState()
        self.first = [node(1, 'front'), node(2, 'rear'), dict(id=5, type='PipeWire:Interface:Client', info={})]

    def test_first_dump_split_across_reads(self):
        text = dumped(self.first)
        self.assertFalse(self.state.feed(text[:40]))
        self.assertEqual(self.state.batches, 0)
        self.assertTrue(self.state.feed(text[40:]))
        self.assertEqual(camera_demand(self.state.objects.values(), 'front'), (0, False))

    def test_link_arrives_and_leaves(self):
        self.state.feed(dumped(self.first))
        self.assertTrue(self.state.feed(dumped([dict(link(1, 9), id=7)])))
        self.assertEqual(camera_demand(self.state.objects.values(), 'front'), (1, False))
        self.assertTrue(self.state.feed(dumped([dict(id=7, info=None)])))
        self.assertEqual(camera_demand(self.state.objects.values(), 'front'), (0, False))

    def test_link_state_change_replaces_the_object(self):
        self.state.feed(dumped(self.first, [dict(link(2, 9, 'error'), id=7)]))
        self.assertEqual(camera_demand(self.state.objects.values(), 'front'), (0, False))
        self.assertTrue(self.state.feed(dumped([dict(link(2, 9), id=7)])))
        self.assertEqual(camera_demand(self.state.objects.values(), 'front'), (0, True))

    def test_other_objects_do_not_wake_the_publisher(self):
        self.state.feed(dumped(self.first))
        self.assertFalse(self.state.feed(dumped(
            [dict(id=6, type='PipeWire:Interface:Client', info={})], [dict(id=6, info=None)], [dict(id=5, info=None)])))
        self.assertEqual(len(self.state.objects), 2)

    def test_runaway_output_is_an_error(self):
        with self.assertRaises(ValueError):
            self.state.feed('[' + ' ' * (33 << 20))


class GraphMonitorTests(unittest.TestCase):
    def monitor(self, script):
        monitor = GraphMonitor(command=(sys.executable, '-c', script), restart_seconds=60)
        self.addCleanup(monitor.stop)
        monitor.start()
        return monitor

    def test_follows_a_running_monitor(self):
        first, change = dumped([node(1, 'front')]), dumped([dict(link(1, 9), id=7)])
        monitor = self.monitor(
            'import sys, time\n'
            f'sys.stdout.write({first!r}); sys.stdout.flush(); time.sleep(0.3)\n'
            f'sys.stdout.write({change!r}); sys.stdout.flush(); time.sleep(60)\n')
        self.assertTrue(monitor.changed.wait(10))
        monitor.changed.clear()
        self.assertEqual(camera_demand(monitor.snapshot(), 'front'), (0, False))
        self.assertTrue(monitor.changed.wait(10))
        self.assertEqual(camera_demand(monitor.snapshot(), 'front'), (1, False))

    def test_dead_monitor_wakes_the_waiter_and_reports_no_graph(self):
        monitor = self.monitor('print("[]")')
        deadline = time.monotonic() + 10
        while monitor.snapshot() is not None or not monitor.changed.is_set():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertIsNone(monitor.snapshot())

    def test_missing_program_reports_no_graph(self):
        monitor = GraphMonitor(command=('/nonexistent/pw-dump',), restart_seconds=60)
        self.addCleanup(monitor.stop)
        monitor.start()
        self.assertTrue(monitor.changed.wait(10))
        self.assertIsNone(monitor.snapshot())


if __name__ == '__main__':
    unittest.main()
