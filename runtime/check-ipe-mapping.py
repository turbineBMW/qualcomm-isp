# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Hardware-only mmap bounds, permissions and module lifetime checks.

Called by the saved-frame --readback verify test, after IPE finishes.
"""
import ctypes
import errno
import mmap
import os
from pathlib import Path
import numpy as np
import icplab


def check(buffer):
    lib = ctypes.CDLL(None, use_errno=True)
    lib.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_long]
    lib.mmap.restype = ctypes.c_void_p
    lib.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    lib.munmap.restype = ctypes.c_int
    lib.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.mprotect.restype = ctypes.c_int
    failure = ctypes.c_void_p(-1).value
    results = {}
    fd = os.open(f'{icplab.D}/map{buffer.index}', os.O_RDONLY | os.O_CLOEXEC)
    try:
        for name, size, prot, flags, offset, expected in [
            ('private', 4096, mmap.PROT_READ, mmap.MAP_PRIVATE, 0, {errno.EINVAL}),
            ('write', 4096, mmap.PROT_READ | mmap.PROT_WRITE, mmap.MAP_SHARED, 0, {errno.EACCES}),
            ('execute', 4096, mmap.PROT_READ | mmap.PROT_EXEC, mmap.MAP_SHARED, 0, {errno.EINVAL, errno.EPERM}),
            ('oversize', buffer.size + 4096, mmap.PROT_READ, mmap.MAP_SHARED, 0, {errno.ENXIO}),
            ('past_end', 4096, mmap.PROT_READ, mmap.MAP_SHARED, buffer.size, {errno.ENXIO}),
        ]:
            ptr = lib.mmap(None, size, prot, flags, fd, offset)
            err = ctypes.get_errno()
            if ptr != failure:
                lib.munmap(ptr, size)
                raise AssertionError(f'{name} mapping unexpectedly allowed')
            assert err in expected, (name, err, expected)
            results[name] = err
    finally:
        os.close(fd)
    # No map fd is open (trackfd=False); the VMA alone must retain the module.
    address = np.frombuffer(buffer.mapping, np.uint8).ctypes.data
    assert lib.mprotect(address, buffer.size, mmap.PROT_READ | mmap.PROT_WRITE) == -1
    assert ctypes.get_errno() == errno.EACCES
    results['mprotect_write'] = ctypes.get_errno()
    ref = Path('/sys/module/qcom_camss_icp/refcnt')
    before = int(ref.read_text())
    assert before > 0
    buffer.mapping.close()
    buffer.mapping = None
    after = int(ref.read_text())
    assert after == before - 1, (before, after)
    assert not Path('/sys/bus/platform/drivers/camss-icp/unbind').exists()
    results.update(module_references_with_mapping=before,
                   module_references_after_unmap=after, manual_unbind_disabled=True)
    return results
