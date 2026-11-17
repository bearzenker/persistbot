"""The harness's side of the runner: start or reach it, check it, call it.

Every command and every file access for the model goes through a `Link` to
a runner process (`runner.py`). There are two ways to get one:

  no `user` in the config   a child process on the harness's own account.
                            Development, tests and cloud sessions. The bot
                            is NOT separated from the harness.
  `user: raven`             a Unix socket that systemd answers by starting
                            the runner as that account.

With `user` set the link fails closed. The kernel says which account each
reply was sent by, and the runner is asked whether it can reach the
harness's code, configuration, keys or state. If the account is wrong, or
anything is reachable, no tool runs. There is no fallback to the harness's
account.
"""

from __future__ import annotations

import json
import os
import pwd
import select
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from persistbot import log, runner
from persistbot.config import BotConfig

CONNECT_WAIT = 10.0      # seconds to reach the socket
HELLO_WAIT = 60.0        # the first reply includes the separation check
FILE_WAIT = 30.0         # a file read or write
RUN_GRACE = 20.0         # added to a command's own timeout
MAX_REPLY = 1_000_000    # bytes in one reply line
_UCRED = struct.Struct("3i")  # pid, uid, gid


class RunnerError(Exception):
    """The runner cannot be used. No tool may run until an operator acts."""


class Unavailable(RunnerError):
    """The runner could not be started or reached."""


class Refused(RunnerError):
    """The runner is there, but is not the account or the separation asked for."""


class Lost(Exception):
    """The runner went away, or answered nonsense, during one call."""


@dataclass
class Link:
    work: Path                              # the model's working directory
    user: str | None = None                 # account the runner must be; None: our own
    socket_path: Path | None = None
    deny_write: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=list)
    uid: int | None = None                  # the runner's account, once open
    _sock: socket.socket | None = None
    _child: subprocess.Popen | None = None
    _expected: int | None = None
    _pending: bytes = b""

    @classmethod
    def for_config(cls, config: BotConfig) -> "Link":
        if config.user is None:
            return cls(work=config.work)
        package = Path(runner.__file__).resolve().parent
        # Everything the harness runs or reads: its install, its configuration,
        # its state, and every directory Python loads its code from.
        code = [Path(entry) for entry in sys.path if entry and Path(entry).is_dir()]
        write = [config.venv, config.path.parent, config.path, config.base, config.logging.dir,
                 config.reporting.dir, config.incoming, package, *code]
        read = [config.path.parent / "keys.json", config.base, config.db_path,
                config.logging.dir, config.reporting.dir, config.incoming]
        return cls(
            work=config.work,
            user=config.user,
            socket_path=config.runner,
            deny_write=_outermost(write),
            deny_read=_unique(read),
        )

    @property
    def separated(self) -> bool:
        return self.user is not None

    def describe(self) -> str:
        if self.separated:
            return f"separated as {self.user} (uid {self.uid}), working directory {self.work}"
        return f"not separated: tools run on the harness's own account, working directory {self.work}"

    # opening -------------------------------------------------------------

    def open(self) -> None:
        """Start or reach the runner and check it. Raises RunnerError."""
        if self._sock is not None:
            return
        try:
            if self.separated:
                self._connect()
            else:
                self._spawn()
            self._hello()
        except BaseException:
            self.close()
            raise

    def _spawn(self) -> None:
        ours, theirs = socket.socketpair()
        env = {key: os.environ[key] for key in runner.ENV_KEEP if key in os.environ}
        try:
            self._child = subprocess.Popen(
                [sys.executable, "-I", runner.__file__], stdin=theirs, stdout=theirs, env=env, cwd="/",
            )
        except OSError as exc:
            ours.close()
            raise Unavailable(f"cannot start the runner: {exc.strerror or exc}") from exc
        finally:
            theirs.close()
        self._expected = os.getuid()
        self._adopt(ours)

    def _connect(self) -> None:
        expected = _account(self.user)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_WAIT)
        try:
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise Unavailable(
                f"cannot reach the runner at {self.socket_path}: {exc.strerror or exc}. "
                f"Is persistbot-runner@{self.user}.socket enabled?"
            ) from exc
        self._expected = expected
        self._adopt(sock)

    def _adopt(self, sock: socket.socket) -> None:
        # Ask the kernel to stamp everything received with its sender's
        # account. The stamp cannot be forged by an unprivileged sender.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        sock.setblocking(False)
        self._sock, self._pending = sock, b""

    def _hello(self) -> None:
        request = {"op": "hello", "work": str(self.work),
                   "deny_write": self.deny_write, "deny_read": self.deny_read}
        try:
            reply = self._exchange(request, HELLO_WAIT)
        except Lost as exc:
            raise Unavailable(f"the runner did not answer: {exc}") from exc
        if not reply.get("ok"):
            raise Refused(str(reply.get("text") or "the runner refused to start").removeprefix("error: "))
        if reply.get("uid") != self._expected:
            raise Refused(f"the runner says it is uid {reply.get('uid')}, not {self._expected}")
        self.uid = self._expected
        if not self.separated:
            return
        if reply.get("writable"):
            raise Refused(
                f"the tools account '{self.user}' can write {_names(reply['writable'])}; "
                "it must not be able to change the harness's code, configuration or state"
            )
        if reply.get("readable"):
            raise Refused(
                f"the tools account '{self.user}' can read {_names(reply['readable'])}; "
                "the keys, queue, logs and state must be closed to it"
            )
        if reply.get("complete") is not True:
            # What was not looked at cannot be vouched for.
            raise Refused(
                f"the separation check stopped after {reply.get('checked')} files without "
                "reaching the end of the harness's install and Python directories, so it "
                f"cannot say that '{self.user}' is kept out of them; move what does not "
                "belong there out of the install directory"
            )

    # calling -------------------------------------------------------------

    def call(self, request: dict, wait: float) -> dict:
        """One request, one reply.

        Raises Lost when the runner goes away during the call (the next call
        reconnects), and RunnerError when it cannot be reached or is not what
        it should be.
        """
        self.open()
        try:
            return self._exchange(request, wait)
        except (Lost, RunnerError):
            self.close()
            raise

    def _exchange(self, request: dict, wait: float) -> dict:
        deadline = time.monotonic() + wait
        self._pending = b""  # nothing left over from an earlier call may pass as this reply
        data = json.dumps(request).encode("ascii") + b"\n"
        try:
            while data:
                self._wait(deadline, writing=True)
                data = data[self._sock.send(data):]
        except OSError as exc:
            raise Lost(f"the runner went away: {exc.strerror or exc}") from exc
        line = self._line(deadline)
        try:
            reply = json.loads(line)
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            raise Lost("the runner's reply was not a JSON object")
        return reply

    def _wait(self, deadline: float, writing: bool = False) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Lost("the runner did not answer in time")
        socks = [self._sock]
        ready = select.select([], socks, [], remaining) if writing else select.select(socks, [], [], remaining)
        if not any(ready):
            raise Lost("the runner did not answer in time")

    def _line(self, deadline: float) -> bytes:
        space = socket.CMSG_SPACE(_UCRED.size)
        while b"\n" not in self._pending:
            if len(self._pending) > MAX_REPLY:
                raise Lost(f"the runner's reply passed {MAX_REPLY} bytes")
            self._wait(deadline)
            try:
                chunk, ancillary, _flags, _ = self._sock.recvmsg(65536, space)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise Lost(f"the runner went away: {exc.strerror or exc}") from exc
            if not chunk:
                raise Lost("the runner closed the connection")
            self._check_sender(ancillary)
            self._pending += chunk
        line, _, self._pending = self._pending.partition(b"\n")
        return line

    def _check_sender(self, ancillary: list) -> None:
        sender = None
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(data) >= _UCRED.size:
                sender = _UCRED.unpack(data[: _UCRED.size])[1]
        if sender != self._expected:
            who = "an unidentified sender" if sender is None else f"uid {sender}"
            want = f"'{self.user}' (uid {self._expected})" if self.separated else f"uid {self._expected}"
            raise Refused(f"the runner's reply came from {who}, not from {want}")

    # closing -------------------------------------------------------------

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._pending = b""
        if self._child is not None:
            # Closing the connection is the runner's signal to stop.
            try:
                self._child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._child.kill()
                self._child.wait()
            self._child = None


def _account(user: str) -> int:
    """The uid the runner must have. It may be neither root nor our own."""
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise Refused(f"there is no Linux account named '{user}' (bot.user)") from None
    if uid == 0:
        raise Refused("bot.user is root; the tools must run as an unprivileged account")
    if uid == os.geteuid():
        raise Refused(
            f"bot.user '{user}' is the account the harness itself runs as; "
            "the tools need an account of their own"
        )
    return uid


def _names(paths: list) -> str:
    return ", ".join(str(path) for path in paths)


def _unique(paths: list[Path]) -> list[str]:
    seen: list[str] = []
    for path in paths:
        if str(path) not in seen:
            seen.append(str(path))
    return seen


def _outermost(paths: list[Path]) -> list[str]:
    """Drop every path that lies inside another on the list: the check walks down."""
    kept: list[Path] = []
    for path in paths:
        if any(other == path or other in path.parents for other in kept):
            continue
        kept = [other for other in kept if path not in other.parents] + [path]
    return [str(path) for path in kept]
