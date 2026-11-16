"""run_shell: allowlisted commands, alone or in a pipeline, with no shell.

The harness connects the processes itself, so every command in a pipeline
has passed the allowlist. Time and output are bounded.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time

from persistbot import allowlist, log
from persistbot.tools import Context, Result, files

OUTPUT_CAP = 4000  # bytes of output returned to the model
FILE_CAP = 1_000_000  # bytes one command may write through `> file`
# The commands see only these, so nothing the harness was started with
# (a key in the environment, a proxy) leaks into a command's output.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TZ")


def _pipe() -> tuple[int, int]:
    return os.pipe()


def _kill(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _open_target(ctx: Context, pipeline: allowlist.Pipeline) -> int | None:
    """The descriptor for `> file`, or None when output goes to the model."""
    if pipeline.stdout is None:
        return None
    if pipeline.stdout == "/dev/null":
        return os.open(os.devnull, os.O_WRONLY)
    target = files.writable(ctx, pipeline.stdout)
    if target.exists() and not target.is_file():
        # A FIFO would block the open until something read it.
        raise ValueError(f"{target} is not a regular file")
    if target.exists() and target.stat().st_nlink > 1:
        # Writing in place would change the other names of the same file.
        raise ValueError(f"{target} is a hard link; write to a different name")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if pipeline.append else os.O_TRUNC)
    return os.open(target, mode, 0o666)


def run_shell(ctx: Context, command: str) -> Result:
    try:
        pipeline = allowlist.parse(command)
    except allowlist.Rejected as exc:
        log.action(f"run_shell rejected: {command!r}", level="warning")
        return Result(False, f"error: command not run: {exc}")

    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env["LC_ALL"] = "C"
    ctx.base.mkdir(parents=True, exist_ok=True)
    # Find every command before starting any, so a pipeline with a missing
    # command runs nothing at all.
    for item in pipeline.commands:
        if shutil.which(item.argv[0], path=env.get("PATH")) is None:
            return Result(False, f"error: command not found on this node: {item.argv[0]}")
    try:
        target = _open_target(ctx, pipeline)
    except (OSError, ValueError) as exc:
        return Result(False, f"error: command not run: {getattr(exc, 'strerror', None) or exc}")

    # One pipe carries everything the model is shown: the last command's
    # output and every command's errors, in the order they are written.
    shown_read, shown_write = _pipe()
    # Output for `> file` passes through the harness, so it can be bounded:
    # written straight to the file, `cat /dev/zero > x` would fill the disk.
    copy_read = copy_write = None
    if target is not None and pipeline.stdout != "/dev/null":
        copy_read, copy_write = _pipe()
    procs: list[subprocess.Popen] = []
    names = " | ".join(item.argv[0] for item in pipeline.commands)
    feed = subprocess.DEVNULL  # stdin of the next command
    failure = None
    try:
        for number, item in enumerate(pipeline.commands):
            last = number == len(pipeline.commands) - 1
            if last:
                out = shown_write if target is None else (copy_write or target)
                next_feed = None
            else:
                try:
                    next_feed, out = _pipe()
                except OSError as exc:  # out of descriptors: stop what has started
                    failure = f"error: cannot start the pipeline: {exc.strerror or exc}"
                    if feed != subprocess.DEVNULL:
                        os.close(feed)
                    break
            err = {"show": shown_write, "null": subprocess.DEVNULL, "stdout": out}[item.stderr]
            try:
                procs.append(subprocess.Popen(
                    item.argv, cwd=ctx.base, env=env, stdin=feed, stdout=out, stderr=err,
                    start_new_session=True,  # own process group, so a kill reaches children
                ))
            except FileNotFoundError:
                failure = f"error: command not found on this node: {item.argv[0]}"
            except OSError as exc:
                failure = f"error: cannot run {item.argv[0]}: {exc.strerror or exc}"
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
        return Result(False, failure)

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
    deadline = time.monotonic() + ctx.shell_timeout
    timed_out = False
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(procs)
            proc.wait()
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
        status = f"stopped: still running after {ctx.shell_timeout:g} seconds"
        ok = False
    else:
        status = f"exit code {code}"
        ok = code == 0
    log.action(f"run_shell {names}: {status.split(':')[0]}")
    return Result(ok, f"{status}\n{text}" if text else f"{status}\n(no output)")
