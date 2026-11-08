#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Own the camera lock and one packaged ICP lifecycle; keep CAMSS loaded.

Stop order: SIGTERM reaches only this process (the worker daemon signals it,
not the group); it forwards SIGTERM to ipe-live and allows CHILD_GRACE
seconds. After that it dumps ipe-live's stacks, kills the whole session tree
(including the camera, which runs in its own session) and still unloads ICP.
"""
import faulthandler
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import isp_config
import proctree

CHILD_GRACE = 20   # ipe-live's own stop watchdog fires at 15 s


def log(*parts):
    print('session:', *parts, file=sys.stderr, flush=True)


def stop_child(child, deadline=None):
    """Stop ipe-live by `deadline` (monotonic); whatever happens, leave no descendant running."""
    if child.poll() is None:
        if deadline is None:
            deadline = time.monotonic() + CHILD_GRACE
        child.terminate()
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log(f'ipe-live did not stop within {CHILD_GRACE} s of the stop; dumping stacks and killing the session tree')
            try:
                os.kill(child.pid, signal.SIGUSR1)
                time.sleep(.5)
            except ProcessLookupError:
                pass
            proctree.kill_tree(child.pid)
            child.wait(timeout=10)
    # Orphans (cam, IPA proxy) were adopted by this subreaper.
    left = proctree.kill_tree(os.getpid(), include_root=False)
    if left:
        log(f'killed leftover session processes {left}')
        deadline = time.monotonic() + 5
        while proctree.descendants(os.getpid()) and time.monotonic() < deadline:
            proctree.reap()
            time.sleep(.05)
    proctree.reap()


def unload_icp(profile):
    if Path('/sys/module/qcom_camss_icp').exists():
        subprocess.run(['rmmod', 'qcom_camss_icp'], check=True, timeout=30)
    usage = Path('/sys/bus/platform/devices') / profile['icp_device'] / 'power/runtime_usage'
    if int(usage.read_text()) != 0:
        raise RuntimeError('ICP runtime usage did not return to zero')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run through qualcomm-isp-worker or sudo -A')
    with open('/run/lock/camss-icp-lab.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log('camera hardware is busy')
            return os.EX_TEMPFAIL
        if Path('/sys/module/qcom_camss_icp').exists():
            raise RuntimeError('ICP already loaded; another session may own it')
        isp_config.identity()
        print(isp_config.preflight(), flush=True)
        _, profile = isp_config.load()
        proctree.become_subreaper()
        child = None
        status = 1
        ipp_parameter = Path('/sys/module/qcom_camss/parameters/ipp_pokes')
        saved_ipp = None
        stopping = []
        def stop(sig, frame):
            stopping.append(time.monotonic())
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        faulthandler.register(signal.SIGUSR1, all_threads=True)
        try:
            if profile.get('experimental_native_grbg'):
                if profile.get('sensor_module') != 'ov13858':
                    raise ValueError('The GRBG lab override is qualified only for OV13858')
                saved_ipp = ipp_parameter.read_text().strip()
                if saved_ipp:
                    saved_ipp = None
                    raise RuntimeError('Existing IPP overrides must not be replaced')
                ipp_parameter.write_text('0x330=0x3000000\n')
            subprocess.run(['modprobe', 'qcom_camss_icp'], check=True)
            if not Path('/sys/kernel/debug/camss-icp/alloc').exists():
                raise RuntimeError('ICP firmware did not initialize')
            if stopping:
                raise RuntimeError('Stopped before the camera started')
            child = subprocess.Popen([sys.executable, '-I', str(HERE/'ipe-live.py'), *sys.argv[1:]], close_fds=False)
            if stopping:   # a stop that landed while Popen ran had no child to forward to
                child.terminate()
            # A stop starts the grace period; a child that ignores it is killed in finally.
            while child.poll() is None and not (stopping and time.monotonic() > stopping[0] + CHILD_GRACE):
                time.sleep(.1)
            status = child.returncode if child.returncode is not None else 1
        finally:
            # Every step runs even when an earlier one fails; the first error wins.
            errors = []
            deadline = stopping[0] + CHILD_GRACE if stopping else None
            steps = [lambda: stop_child(child, deadline)] if child is not None else []
            steps.append(lambda: unload_icp(profile))
            if saved_ipp is not None:
                steps.append(lambda: ipp_parameter.write_text(saved_ipp + '\n'))
            for step in steps:
                try:
                    step()
                except BaseException as exc:
                    log(f'cleanup step failed: {exc!r}')
                    errors.append(exc)
            if errors and sys.exc_info()[0] is None:
                raise errors[0]
        return status


if __name__ == '__main__':
    sys.exit(main())
