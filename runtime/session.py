#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Own the camera lock and one packaged ICP lifecycle; keep CAMSS loaded."""
import fcntl
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import isp_config


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run through qualcomm-isp-worker or sudo -A')
    with open('/run/lock/camss-icp-lab.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if Path('/sys/module/qcom_camss_icp').exists():
            raise RuntimeError('ICP already loaded; another session may own it')
        isp_config.identity()
        print(isp_config.preflight(), flush=True)
        _, profile = isp_config.load()
        child = None
        status = 1
        ipp_parameter = Path('/sys/module/qcom_camss/parameters/ipp_pokes')
        saved_ipp = None
        def stop(sig, frame):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
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
            child = subprocess.Popen([sys.executable, '-I', str(HERE/'ipe-live.py'), *sys.argv[1:]], close_fds=False)
            status = child.wait()
        finally:
            try:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=60)
                if Path('/sys/module/qcom_camss_icp').exists():
                    subprocess.run(['rmmod', 'qcom_camss_icp'], check=True)
                usage = Path('/sys/bus/platform/devices') / profile['icp_device'] / 'power/runtime_usage'
                if int(usage.read_text()) != 0:
                    raise RuntimeError('ICP runtime usage did not return to zero')
            finally:
                if saved_ipp is not None:
                    ipp_parameter.write_text(saved_ipp + '\n')
        return status


if __name__ == '__main__':
    sys.exit(main())
