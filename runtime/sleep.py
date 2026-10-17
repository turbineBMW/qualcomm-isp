#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Quiesce the on-demand IPE worker before system sleep; restore after wake."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess

SOCKET = 'qualcomm-isp-worker.socket'
WORKER = 'qualcomm-isp-worker.service'
STATE = Path('/run/qualcomm-isp/sleep-state.json')
MODULE = Path('/sys/module/qcom_camss_icp')
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import isp_config
_, BOARD = isp_config.load()
POWER = Path('/sys/bus/platform/devices') / BOARD['icp_device'] / 'power'
LOCK = Path('/run/lock/camss-icp-lab.lock')


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def control(action, *units):
    subprocess.run(['systemctl', action, *units], check=True, timeout=75)


def restore():
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    units = [unit for unit in (SOCKET, WORKER) if state[unit]]
    if units:
        control('start', *units)
    STATE.unlink()
    print(f'IPE sleep: restored {units}', flush=True)


def prepare():
    if STATE.exists():
        raise RuntimeError('A previous sleep transition has not been restored')
    state = {unit: active(unit) for unit in (SOCKET, WORKER)}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Save before stopping so ExecStopPost can also recover an interrupted pre-hook.
    with STATE.open('x') as stream:
        json.dump(state, stream)
    try:
        # Stop activation too: an open PipeWire consumer keeps asking for frames.
        control('stop', SOCKET, WORKER)
        with LOCK.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if MODULE.exists():
                raise RuntimeError('ICP still loaded after worker shutdown')
            if POWER.exists() and int((POWER / 'runtime_usage').read_text()) != 0:
                raise RuntimeError('ICP still holds runtime-PM references')
        print('IPE sleep: camera stopped, ICP unloaded, runtime usage zero', flush=True)
    except BaseException:
        restore()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'restore'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run through the system sleep service')
    (prepare if args.action == 'prepare' else restore)()


if __name__ == '__main__':
    main()
