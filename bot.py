#!/usr/bin/env python3
"""Launcher: /opt/persistbot/bot.py [conf.yaml] [-f prompt_file] [prompt...]"""

import sys
from pathlib import Path

HOME = Path(__file__).resolve().parent
SRC = HOME / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from persistbot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(home=HOME))
