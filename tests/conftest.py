import textwrap

import pytest

from persistbot import log


@pytest.fixture(autouse=True)
def _close_logs():
    yield
    log.close()


@pytest.fixture
def make_conf(tmp_path):
    """Write a bot config under tmp_path and return its path."""

    def make(body: str = "", name: str = "Raven") -> "Path":
        venv = tmp_path / "opt"
        venv.mkdir(exist_ok=True)
        text = textwrap.dedent(
            f"""\
            bot:
              venv: {venv}
              base: raven
              name: {name}
              persona: Bichon
            """
        ) + textwrap.indent(textwrap.dedent(body), "  ")
        path = tmp_path / "bot.yaml"
        path.write_text(text)
        return path

    return make
