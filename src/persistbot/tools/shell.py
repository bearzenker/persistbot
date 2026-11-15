"""run_shell: one allowlisted command, no shell, bounded in time and output."""

from __future__ import annotations

import os
import signal
import subprocess
import threading

from persistbot import allowlist, log
from persistbot.tools import Context, Result

OUTPUT_CAP = 4000  # bytes of combined stdout and stderr returned to the model
# The command sees only these, so nothing the harness was started with
# (a key in the environment, a proxy) leaks into a command's output.
ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TZ")


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_shell(ctx: Context, command: str) -> Result:
    try:
        argv = allowlist.check(command)
    except allowlist.Rejected as exc:
        log.action(f"run_shell rejected: {command!r}", level="warning")
        return Result(False, f"error: command not run: {exc}")

    env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
    env["LC_ALL"] = "C"
    ctx.base.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=ctx.base,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # its own process group, so a timeout kills children too
        )
    except FileNotFoundError:
        return Result(False, f"error: command not found on this node: {argv[0]}")
    except OSError as exc:
        return Result(False, f"error: cannot run {argv[0]}: {exc.strerror or exc}")

    output = bytearray()
    overflow = threading.Event()

    def read() -> None:
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > OUTPUT_CAP:
                overflow.set()
                _kill(proc)
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=ctx.shell_timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        proc.wait()
    reader.join(timeout=5)
    proc.stdout.close()

    text = bytes(output[:OUTPUT_CAP]).decode("utf-8", errors="replace").rstrip("\n")
    if overflow.is_set():
        status = f"stopped: output passed {OUTPUT_CAP} bytes and was cut off. Narrow the command."
        ok = False
    elif timed_out:
        status = f"stopped: still running after {ctx.shell_timeout:g} seconds"
        ok = False
    else:
        status = f"exit code {proc.returncode}"
        ok = proc.returncode == 0
    log.action(f"run_shell {argv[0]}: {status.split(':')[0]}")
    return Result(ok, f"{status}\n{text}" if text else f"{status}\n(no output)")
