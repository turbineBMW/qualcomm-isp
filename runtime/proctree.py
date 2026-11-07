#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Process-tree helpers for the camera session stop path.

The camera runs in its own session (runuser -> cam -> IPA proxy), so a
process-group signal never reaches it. These helpers follow parent links in
/proc instead, and let a session supervisor adopt orphans as a subreaper.
"""
import ctypes
import os
import signal
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36


def become_subreaper():
    """Orphaned descendants reparent to this process instead of init."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER failed')


def _parents(proc='/proc'):
    parents = {}
    for entry in Path(proc).iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / 'stat').read_text()
        except OSError:
            continue
        # comm may contain spaces and parentheses: fields follow the last ')'.
        fields = stat[stat.rindex(')') + 2:].split()
        if fields[0] == 'Z':
            continue   # already dead, only waiting to be reaped
        parents[int(entry.name)] = int(fields[1])
    return parents


def descendants(pid, proc='/proc'):
    """Every live descendant of pid (not pid itself), parents before children."""
    children = {}
    for child, parent in _parents(proc).items():
        children.setdefault(parent, []).append(child)
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            found.append(child)
            queue.append(child)
    return found


def runs_script(pid, directory):
    """True if pid runs a Python script from directory (these handle SIGUSR1)."""
    try:
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False
    prefix = str(directory).rstrip('/') + '/'
    return any(arg.decode(errors='replace').startswith(prefix) and arg.endswith(b'.py') for arg in argv[1:3])


def signal_tree(pid, sig, include_root=True):
    """Send sig to pid's descendants (snapshot first) and optionally pid. Returns the pids signalled."""
    targets = descendants(pid) + ([pid] if include_root else [])
    sent = []
    for target in targets:
        try:
            os.kill(target, sig)
            sent.append(target)
        except ProcessLookupError:
            pass
    return sent


def reap():
    """Collect exited adopted children without blocking."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def kill_tree(pid, include_root=True):
    return signal_tree(pid, signal.SIGKILL, include_root)
