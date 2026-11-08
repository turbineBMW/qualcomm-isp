#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Shared camera demand: switching, cancelled startup and graph failures."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ipe_demand import camera_demand, should_release


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


if __name__ == '__main__':
    unittest.main()
