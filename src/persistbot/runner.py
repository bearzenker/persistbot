"""The runner: does what the harness asks, as the bot's own Linux account.

The harness holds the keys, the state and the allowlist, and runs as one
account. This process runs as another and is the only thing that executes
a command or touches a file for the model. It does three things: run a
pipeline the harness has already parsed and approved, read a file, write a
file under the working directory.

It is standalone on purpose: standard library only, nothing imported from
the rest of the package, started with `python -I`. It keeps no log, because
it cannot reach the bot's log directory; everything it has to say goes back
to the harness in the reply.

Protocol: one JSON object per line each way, a reply for every request.
  {"op": "hello", "work": DIR, "deny_write": [...], "deny_read": [...]}
  {"op": "run", "commands": [{"argv": [...], "stderr": "show"}], "stdout": null,
   "append": false, "timeout": 60}
  {"op": "read", "path": PATH}
  {"op": "write", "path": PATH, "content": TEXT}

Started by systemd socket activation it serves one connection and exits.
Started any other way it serves its standard input and output.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROTOCOL = 1
OUTPUT_CAP = 4000        # bytes of command output returned to the model
FILE_CAP = 1_000_000     # bytes one command may write through `> file`
READ_CAP = 8000          # characters of a file returned to the model
WRITE_CAP = 100_000      # characters accepted in one write
MAX_LINE = 4_000_000     # bytes in one request line
CHECK_BUDGET = 100_000   # files and directories looked at by the separation check
CHECK_REPORT = 5         # reachable paths named in the reply
ACCEPT_WAIT = 30.0       # seconds a socket-activated runner waits for its caller
# Commands see only these, so nothing the runner was started with leaks
# into a command's output.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TZ")

_UMASK = os.umask(0)
os.umask(_UMASK)


def _pipe() -> tuple[int, int]:
    return os.pipe()


def _kill(procs: list[subprocess.Popen]) -> None:
    for proc in list(procs):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _why(exc: BaseException) -> str:
    return str(getattr(exc, "strerror", None) or exc)


def _fail(text: str, **extra) -> dict:
    return {"ok": False, "text": text, **extra}


def _reachable_for_write(tops: list[str], skip: Path | None) -> tuple[list[str], int, bool]:
    """Paths under `tops` this account can write: (found, looked at, finished)."""
    found: list[str] = []
    seen = 0

    def check(path: str) -> bool:
        """True when the search should stop."""
        nonlocal seen
        seen += 1
        # A link is judged by what it points at only if that is listed too.
        if not os.path.islink(path) and os.access(path, os.W_OK):
            found.append(path)
        return len(found) >= CHECK_REPORT or seen >= CHECK_BUDGET

    for top in tops:
        if skip is not None and Path(top) == skip:
            continue
        if check(top):
            return found, seen, len(found) >= CHECK_REPORT
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        for root, dirs, names in os.walk(top):
            if skip is not None:
                dirs[:] = [name for name in dirs if Path(root, name) != skip]
            for name in dirs + names:
                if check(os.path.join(root, name)):
                    return found, seen, len(found) >= CHECK_REPORT
    return found, seen, True


def _reachable_for_read(paths: list[str]) -> list[str]:
    found = []
    for path in paths:
        mode = os.R_OK
        if os.access(path, mode) or (os.path.isdir(path) and os.access(path, os.X_OK)):
            found.append(path)
    return found


class Runner:
    """The state of one connection: the working directory, and what is running."""

    def __init__(self, watch: socket.socket | None = None) -> None:
        self.work: Path | None = None
        self.watch = watch          # the caller's connection, to notice it going away
        self.procs: list[subprocess.Popen] = []

    # dispatch ------------------------------------------------------------

    def handle(self, request: object) -> dict:
        if not isinstance(request, dict):
            return _fail("error: the request is not a JSON object")
        op = request.get("op")
        try:
            if op == "hello":
                return self.hello(request)
            if self.work is None:
                return _fail("error: the runner has not been told its working directory")
            if op == "run":
                return self.run(request)
            if op == "read":
                return self.read(request)
            if op == "write":
                return self.write(request)
        except Exception as exc:  # a request must always get a reply
            return _fail(f"error: {op} failed: {type(exc).__name__}: {exc}")
        return _fail(f"error: the runner has no operation '{op}'")

    # hello ---------------------------------------------------------------

    def hello(self, request: dict) -> dict:
        work = Path(str(request.get("work") or ""))
        if not work.is_absolute():
            return _fail("error: the working directory must be an absolute path")
        try:
            work.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _fail(f"error: the working directory {work} does not exist and this "
                         f"account cannot create it: {_why(exc)}")
        if not work.is_dir() or not os.access(work, os.R_OK | os.W_OK | os.X_OK):
            return _fail(f"error: this account cannot write in the working directory {work}")
        self.work = work.resolve()
        os.chdir(self.work)
        writable, seen, complete = _reachable_for_write(
            [str(p) for p in request.get("deny_write") or []], self.work)
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "uid": os.getuid(),
            "work": str(self.work),
            "writable": writable,
            "readable": _reachable_for_read([str(p) for p in request.get("deny_read") or []]),
            "checked": seen,
            "complete": complete,
        }

    # files ---------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        if not path.strip() or "\x00" in path:
            raise ValueError("the path is empty or not usable")
        target = Path(path.strip()).expanduser()
        if not target.is_absolute():
            target = self.work / target
        return target.resolve()  # follows symlinks, so a link cannot lead a write outside

    def writable(self, path: str) -> Path:
        """The resolved path if a file may be written there, else ValueError for the model."""
        target = self._resolve(path)
        if target != self.work and self.work not in target.parents:
            raise ValueError(f"files can be written only under {self.work}. Use a relative path.")
        if target == self.work or target.is_dir():
            raise ValueError(f"{target} is a directory")
        return target

    def read(self, request: dict) -> dict:
        path = str(request.get("path", ""))
        try:
            target = self._resolve(path)
            if target.is_dir():
                return _fail(f"error: {target} is a directory; use run_shell with ls")
            if not target.is_file():
                # Also refuses devices and pipes, which could block or never end.
                return _fail(f"error: no such file: {target}")
            with open(target, "rb") as handle:
                data = handle.read(READ_CAP * 4 + 1)
        except (OSError, ValueError) as exc:
            return _fail(f"error: cannot read {path}: {_why(exc)}")
        if b"\x00" in data:
            return _fail(f"error: {target} is not a text file")
        text = data.decode("utf-8", errors="replace")
        if len(text) > READ_CAP or len(data) > READ_CAP * 4:
            text = text[:READ_CAP] + f"\n[cut off after {READ_CAP} characters; the file is longer]"
        return {"ok": True, "text": text if text else "(the file is empty)"}

    def write(self, request: dict) -> dict:
        path, content = str(request.get("path", "")), request.get("content")
        if not isinstance(content, str):
            return _fail("error: the content must be text")
        if len(content) > WRITE_CAP:
            return _fail(f"error: content is over {WRITE_CAP} characters; write a smaller file")
        try:
            target = self.writable(path)
        except ValueError as exc:
            return _fail(f"error: {exc}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace(target, content)
        except OSError as exc:
            return _fail(f"error: cannot write {path}: {_why(exc)}")
        return {"ok": True, "text": f"wrote {len(content)} characters to {target}"}

    # pipelines -----------------------------------------------------------

    def _open_target(self, stdout: str | None, append: bool) -> int | None:
        """The descriptor for `> file`, or None when output goes to the model."""
        if stdout is None:
            return None
        if stdout == "/dev/null":
            return os.open(os.devnull, os.O_WRONLY)
        target = self.writable(stdout)
        if target.exists() and not target.is_file():
            # A FIFO would block the open until something read it.
            raise ValueError(f"{target} is not a regular file")
        if target.exists() and target.stat().st_nlink > 1:
            # Writing in place would change the other names of the same file.
            raise ValueError(f"{target} is a hard link; write to a different name")
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        return os.open(target, mode, 0o666)

    def _watch_caller(self, stop: threading.Event) -> None:
        """Stop the commands if the harness goes away while they run."""
        while not stop.is_set():
            try:
                ready, _, _ = select.select([self.watch], [], [], 0.2)
                if not ready:
                    continue
                if self.watch.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                    _kill(self.procs)
                    return
            except (BlockingIOError, InterruptedError):
                pass
            except (OSError, ValueError):
                return
            stop.wait(0.2)

    def run(self, request: dict) -> dict:
        commands = request.get("commands")
        stdout, append = request.get("stdout"), bool(request.get("append"))
        timeout = request.get("timeout")
        if (
            not isinstance(commands, list) or not commands
            or not all(
                isinstance(c, dict) and isinstance(c.get("argv"), list) and c["argv"]
                and all(isinstance(word, str) for word in c["argv"])
                and c.get("stderr", "show") in ("show", "null", "stdout")
                for c in commands
            )
            or not (stdout is None or isinstance(stdout, str))
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            return _fail("error: the run request is malformed")

        env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
        env["LC_ALL"] = "C"
        # Find every command before starting any, so a pipeline with a missing
        # command runs nothing at all.
        for item in commands:
            if shutil.which(item["argv"][0], path=env.get("PATH")) is None:
                return _fail(f"error: command not found on this node: {item['argv'][0]}")
        try:
            target = self._open_target(stdout, append)
        except (OSError, ValueError) as exc:
            return _fail(f"error: command not run: {_why(exc)}")

        # One pipe carries everything the model is shown: the last command's
        # output and every command's errors, in the order they are written.
        shown_read, shown_write = _pipe()
        # Output for `> file` passes through the runner, so it can be bounded:
        # written straight to the file, `cat /dev/zero > x` would fill the disk.
        copy_read = copy_write = None
        if target is not None and stdout != "/dev/null":
            copy_read, copy_write = _pipe()
        procs = self.procs = []
        feed = subprocess.DEVNULL  # stdin of the next command
        failure = None
        try:
            for number, item in enumerate(commands):
                last = number == len(commands) - 1
                if last:
                    out = shown_write if target is None else (copy_write or target)
                    next_feed = None
                else:
                    try:
                        next_feed, out = _pipe()
                    except OSError as exc:  # out of descriptors: stop what has started
                        failure = f"error: cannot start the pipeline: {_why(exc)}"
                        if feed != subprocess.DEVNULL:
                            os.close(feed)
                        break
                err = {"show": shown_write, "null": subprocess.DEVNULL, "stdout": out}[
                    item.get("stderr", "show")]
                try:
                    procs.append(subprocess.Popen(
                        item["argv"], cwd=self.work, env=env, stdin=feed, stdout=out, stderr=err,
                        start_new_session=True,  # own process group, so a kill reaches children
                    ))
                except FileNotFoundError:
                    failure = f"error: command not found on this node: {item['argv'][0]}"
                except OSError as exc:
                    failure = f"error: cannot run {item['argv'][0]}: {_why(exc)}"
                finally:
                    if isinstance(feed, int) and feed != subprocess.DEVNULL:
                        os.close(feed)
                    if not last:
                        os.close(out)
                feed = next_feed
                if failure:
                    if feed is not None:
                        os.close(feed)
                    break
        finally:
            os.close(shown_write)
            if copy_write is not None:
                os.close(copy_write)
            elif target is not None:
                os.close(target)
        if failure:
            _kill(procs)
            for proc in procs:
                proc.wait()
            for fd in (shown_read, copy_read, target if copy_read is not None else None):
                if fd is not None:
                    os.close(fd)
            return _fail(failure)

        output = bytearray()
        overflow = threading.Event()
        file_overflow = threading.Event()

        def copy() -> None:
            written = 0
            with os.fdopen(copy_read, "rb", buffering=0) as source, \
                    os.fdopen(target, "wb", buffering=0) as sink:
                while True:
                    chunk = source.read(65536)
                    if not chunk:
                        return
                    room = FILE_CAP - written
                    sink.write(chunk[:room])
                    written += len(chunk)
                    if written > FILE_CAP:
                        file_overflow.set()
                        _kill(procs)
                        return

        copier = None
        if copy_read is not None:
            copier = threading.Thread(target=copy, daemon=True)
            copier.start()

        def read() -> None:
            with os.fdopen(shown_read, "rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        return
                    output.extend(chunk)
                    if len(output) > OUTPUT_CAP:
                        overflow.set()
                        _kill(procs)
                        return

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        finished = threading.Event()
        if self.watch is not None:
            threading.Thread(target=self._watch_caller, args=(finished,), daemon=True).start()
        deadline = time.monotonic() + timeout
        timed_out = False
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill(procs)
                proc.wait()
        finished.set()
        reader.join(timeout=5)
        if copier is not None:
            copier.join(timeout=5)

        text = bytes(output[:OUTPUT_CAP]).decode("utf-8", errors="replace").rstrip("\n")
        code = procs[-1].returncode
        if overflow.is_set():
            status = f"stopped: output passed {OUTPUT_CAP} bytes and was cut off. Narrow the command."
            ok = False
        elif file_overflow.is_set():
            status = f"stopped: more than {FILE_CAP} bytes went to the file, which was cut off there"
            ok = False
        elif timed_out:
            status = f"stopped: still running after {timeout:g} seconds"
            ok = False
        else:
            status = f"exit code {code}"
            ok = code == 0
        return {
            "ok": ok,
            "text": f"{status}\n{text}" if text else f"{status}\n(no output)",
            "status": status.split(":")[0],
        }


def _replace(target: Path, content: str) -> None:
    """Write a new file and move it into place.

    Writing through the existing name would also change every hard link to
    it, and a link to a file outside the working directory would be a way to
    write there. Replacing the name leaves the other links alone.
    """
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=".write-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(content)
            if mode is not None:
                os.fchmod(out.fileno(), mode)  # a script stays executable
            else:
                os.fchmod(out.fileno(), 0o666 & ~_UMASK)
        os.replace(name, target)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def serve(read_fd: int, write_fd: int) -> None:
    """Answer requests until the caller closes the connection."""
    watch = None
    if stat.S_ISSOCK(os.fstat(read_fd).st_mode):
        watch = socket.socket(fileno=os.dup(read_fd))
    runner = Runner(watch)
    with os.fdopen(read_fd, "rb") as requests, os.fdopen(write_fd, "wb") as replies:
        while True:
            line = requests.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                reply = _fail("error: the request is too long or was cut off")
            else:
                try:
                    reply = runner.handle(json.loads(line))
                except ValueError:
                    reply = _fail("error: the request is not valid JSON")
            try:
                replies.write(json.dumps(reply).encode("ascii") + b"\n")
                replies.flush()
            except OSError:
                return  # the caller has gone
            if len(line) > MAX_LINE:
                return  # the rest of an overlong line cannot be told from a request


def main() -> int:
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        # systemd socket activation: descriptor 3 is the listening socket.
        listener = socket.socket(fileno=3)
        listener.settimeout(ACCEPT_WAIT)
        try:
            connection, _ = listener.accept()
        except OSError:
            return 0  # woken, but the caller has already gone
        listener.close()
        connection.setblocking(True)
        fd = connection.detach()
        serve(fd, os.dup(fd))
    else:
        serve(0, 1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
