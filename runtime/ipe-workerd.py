#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Brandon Williams <turbine.bmw@gmail.com>
# SPDX-License-Identifier: MIT
"""Privileged IPE worker daemon for the persistent publisher.

Listens on a SOCK_SEQPACKET unix socket (or a systemd-passed listening socket).
One client at a time. Messages are JSON packets:
  {"type":"start"}  + SCM_RIGHTS [memfd, control socket]  -> started | failed | busy
  {"type":"stop"}                                          -> stopped
  {"type":"status"}                                        -> status
Closing the connection stops a running session. Each session is one
run-ipe-session.sh invocation (ICP module load, ipe-live.py with
--handoff-fds, module unload and camera restoration on exit).
"""
import argparse
import array
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import copy
import struct
import sys
import threading
import time

HERE = Path(__file__).resolve().parent


def log(*parts):
    print(time.strftime('%H:%M:%S'), 'workerd', *parts, flush=True)


class Session:
    def __init__(self, args, memfd, control_fd):
        self.started = time.monotonic()
        self.dir = Path(args.sessions) / time.strftime('%Y%m%d-%H%M%S')
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        self.log = (self.dir.parent / (self.dir.name + '.log')).open('w')
        cmd = ['python3', '-I', str(HERE / 'session.py'),
               '--publish', '--handoff-fds', f'{memfd},{control_fd}',
               '--seconds', str(args.max_seconds), '--out', str(self.dir),
               '--state-file', str(Path(args.sessions).parent / 'last-controls.json'),
               *args.session_args]
        env = dict(os.environ, QUALCOMM_ISP_CAPTURE_UID=str(args.capture_uid))
        self.process = subprocess.Popen(cmd, pass_fds=(memfd, control_fd), stdout=self.log,
                                        stderr=subprocess.STDOUT, start_new_session=True, cwd=str(HERE), env=env)
        self.log_path = Path(self.log.name)

    def wait_started(self, timeout, stopping=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stopping is not None and stopping.is_set():
                return False
            if self.process.poll() is not None:
                return False
            text = self.log_path.read_text(errors='replace')
            if 'PUBLISHER READY' in text or 'LIVE frame=' in text:
                return True
            time.sleep(.2)
        return False

    def alive(self):
        return self.process.poll() is None

    def stop(self, timeout=45):
        if self.alive():
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                log('session did not stop; killing')
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(10)
        self.log.close()
        return self.process.returncode

    def tail(self, n=12):
        try:
            return self.log_path.read_text(errors='replace').splitlines()[-n:]
        except OSError:
            return []


def recv(conn):
    data, anc, flags, _ = conn.recvmsg(65536, socket.CMSG_SPACE(2 * 4), socket.MSG_CMSG_CLOEXEC)
    fds = []
    for level, kind, payload in anc:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array('i')
            values.frombytes(payload[:len(payload) // values.itemsize * values.itemsize])
            fds.extend(values)
    if not data:
        for fd in fds:
            os.close(fd)
        return None, []
    try:
        return json.loads(data), fds
    except ValueError:
        for fd in fds:
            os.close(fd)
        return dict(type='invalid'), []


def reply(conn, **msg):
    conn.send(json.dumps(msg).encode())


def serve(conn, args, state, stopping):
    args = copy.copy(args)
    _, args.capture_uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    session = None
    conn.settimeout(.5)
    try:
        while not stopping.is_set():
            try:
                msg, fds = recv(conn)
            except socket.timeout:
                continue
            if msg is None:
                log('client disconnected')
                break
            kind = msg.get('type')
            if kind == 'start':
                if len(fds) != 2:
                    for fd in fds:
                        os.close(fd)
                    reply(conn, type='failed', error='start needs memfd and control descriptors')
                    continue
                if session is not None and session.alive():
                    for fd in fds:
                        os.close(fd)
                    reply(conn, type='busy', session=str(session.dir))
                    continue
                session = Session(args, *fds)
                for fd in fds:
                    os.close(fd)   # the session process holds its own copies
                ok = session.wait_started(args.start_timeout, stopping)
                if ok:
                    state['sessions'] += 1
                    log(f'session {session.dir.name} started in {time.monotonic()-session.started:.1f}s')
                    reply(conn, type='started', session=str(session.dir))
                else:
                    rc = session.stop()
                    log(f'session {session.dir.name} failed to start (exit {rc})')
                    reply(conn, type='failed', error=f'session exited {rc}' if rc is not None else 'start timeout',
                          log=session.tail())
                    session = None
            elif kind == 'stop':
                if session is None:
                    reply(conn, type='stopped', exit=None)
                else:
                    rc = session.stop()
                    log(f'session {session.dir.name} stopped (exit {rc}) after {time.monotonic()-session.started:.0f}s')
                    reply(conn, type='stopped', exit=rc, session=str(session.dir))
                    session = None
            elif kind == 'status':
                reply(conn, type='status', running=bool(session and session.alive()),
                      session=str(session.dir) if session else None, sessions=state['sessions'])
            else:
                reply(conn, type='failed', error=f'unknown message {kind!r}')
    except OSError as exc:
        log(f'client error: {exc!r}')
    finally:
        if session is not None and session.alive():
            rc = session.stop()
            log(f'session {session.dir.name} stopped with the client (exit {rc})')
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--socket', type=Path, default=Path('/run/qualcomm-isp/control'))
    p.add_argument('--owner', type=int, help='uid allowed to connect when binding the socket here (mode 0600)')
    p.add_argument('--group', type=int, help='gid allowed to connect (mode 0660)')
    p.add_argument('--sessions', default='/var/lib/qualcomm-isp/sessions')
    p.add_argument('--max-seconds', type=int, default=0, help='Hard session limit passed to ipe-live (0 = none)')
    p.add_argument('--start-timeout', type=float, default=60)
    p.add_argument('--session-args', nargs=argparse.REMAINDER,
                   default=['--adaptive', '--centre-priority', '--quiet', '--retain-frames', '3000'])
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('The worker daemon needs root for the ICP lab interface')
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    serve_forever(args, stopping)


def serve_forever(args, stopping):
    if os.environ.get('LISTEN_FDS') == '1' and os.environ.get('LISTEN_PID') == str(os.getpid()):
        listener = socket.socket(fileno=3)
        log('using the systemd-provided socket')
    else:
        args.socket.parent.mkdir(parents=True, exist_ok=True)
        if args.socket.exists():
            args.socket.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(str(args.socket))
        if args.group is not None:
            os.chown(args.socket, 0, args.group)
            os.chmod(args.socket, 0o660)
        elif args.owner is not None:
            os.chown(args.socket, args.owner, -1)
            os.chmod(args.socket, 0o600)
        else:
            os.chmod(args.socket, 0o600)
        listener.listen(1)
        log(f'listening on {args.socket}')
    listener.settimeout(.5)
    state = dict(sessions=0)
    while not stopping.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        pid, uid, gid = __import__('struct').unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        log(f'client pid {pid} uid {uid}')
        serve(conn, args, state, stopping)
    listener.close()
    log('exiting')


if __name__ == '__main__':
    main()
