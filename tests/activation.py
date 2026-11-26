"""A runner started the way systemd socket activation starts it, shared by
the link tests that run as anyone and the ones that need root."""

import socket
import subprocess
import sys
from pathlib import Path

from persistbot import runner

# Becomes the runner the way systemd socket activation starts it: the
# listening socket on descriptor 3, LISTEN_FDS and LISTEN_PID set.
ACTIVATE = (
    "import os, sys; os.dup2(int(sys.argv[1]), 3); "
    "os.environ.update(LISTEN_FDS='1', LISTEN_PID=str(os.getpid())); "
    "os.execv(sys.executable, [sys.executable, '-I', sys.argv[2]])"
)


class Activated:
    """A runner behind a Unix socket, optionally on another account (root only)."""

    def __init__(self, directory, uid=None, program=runner.__file__):
        self.path = Path(directory) / "runner.sock"
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        listener.listen(4)
        account = {} if uid is None else {"user": uid, "group": uid, "extra_groups": []}
        self.proc = subprocess.Popen(
            [sys.executable, "-c", ACTIVATE, str(listener.fileno()), str(program)],
            pass_fds=(listener.fileno(),), cwd="/", **account,
        )
        listener.close()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
