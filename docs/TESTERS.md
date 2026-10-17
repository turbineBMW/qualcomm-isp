# First report from another X1E machine

Start with inventory on the machine's working kernel:

```sh
python tools/collect-platform.py > camera-platform.json
```

This reads sysfs/device-tree metadata. It does not load ICP, open a camera,
change the boot setup, or include the full kernel command line or machine
serial numbers. Include the machine model, current kernel, and camera-platform
JSON when reporting a new board.

Bring-up stages:

1. Confirm the board DT describes the actual camera sensor, CCI bus, supplies,
   clocks, CSI lanes and endpoints. Confirm the correct sensor driver binds.
2. Establish RAW capture with that board's native sensor configuration.
3. Qualify the IFE path and matching libcamera controls/metadata.
4. Qualify ICP firmware initialization, shutdown and runtime-PM cleanup.
5. Only then attempt IPE using compatible, sensor-appropriate program data.

The checked-in Denali OLED profile, DTB and program-data expectations are for
the Surface Pro 11 test device. Other machines must use their own DTB and
profile. The complete kernel recipe includes 66 Surface prerequisite patches
as a separate ordered group alongside the ISP work; it is not yet a minimal
generic X1E series.

For each qualified profile, record the kernel/source identity, sensor and
firmware versions, matching libcamera/bridge packages, successful capture,
buffer cleanup, repeated open/close, application behavior and idle/active
suspend results. A compiled DTB or enumerated node is not a camera test.

## Current evidence

- Kernel: Omarchy ARM 7.2.8-13 base, ISP candidate 7.2.8-13.1.
- Board: Surface Pro 11, Denali OLED, IMX681 front camera.
- Original runtime: 4K processed capture, Snapshot, Chromium Mozilla
  getUserMedia, and idle/active-camera system suspend passed.
- Packaged runtime: 120-frame installed capture with 137/137 leases returned;
  420-frame PipeWire consumer survived explicit sleep-hook stop/restore.
- Packaged revision 0.1-3: user confirmed Snapshot, Chromium getUserMedia and
  idle/active-camera actual system suspend. Logs confirm two clean deep-sleep
  cycles and first-frame recovery about five seconds after service restoration.
- No second X1E machine or NVIDIA N1x hardware has been tested.
