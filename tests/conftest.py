import json
import textwrap

import pytest

from fakellm import FakeLLM
from persistbot import log, tools
from persistbot.link import Link
from persistbot.store import Store


@pytest.fixture(autouse=True)
def _close_logs():
    yield
    log.close()


PERSONAS = {
    "Bichon": {"model": "edge-3b", "provider": "Bichon", "parameters": {"timeout": 5}},
    "Antelope": {
        "model": "big-model",
        "provider": "openrouter",
        "character": "You are an AI Linux administrator.",
    },
}


def write_conf_dir(conf_dir, url="http://127.0.0.1:9/v1", personas=None, providers=None, keys=None):
    """Write persona.json, provider.json and keys.json into `conf_dir`.

    Pass False for any of the three to leave that file out.
    """
    conf_dir.mkdir(parents=True, exist_ok=True)
    if providers is None:
        providers = {
            "bichon": {"driver": "openai", "url": url},
            "openrouter": {"driver": "openai", "url": url, "parameters": {"temperature": 0.7}},
        }
    if keys is None:
        keys = {"bichon": {"key": ""}, "openrouter": {"key": "sk-test"}}
    files = {
        "persona.json": PERSONAS if personas is None else personas,
        "provider.json": providers,
        "keys.json": keys,
    }
    for name, content in files.items():
        if content is not False:
            (conf_dir / name).write_text(json.dumps(content))
    return conf_dir


@pytest.fixture
def conf_dir(tmp_path):
    """A conf directory holding only the persona, provider and keys files."""
    return write_conf_dir(tmp_path / "conf")


@pytest.fixture
def fakellm():
    with FakeLLM() as server:
        yield server


@pytest.fixture
def make_conf(tmp_path):
    """Write a bot config under tmp_path, with persona files beside it."""

    def make(body: str = "", name: str = "Raven", url: str | None = None) -> "Path":
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
        (venv / "system.txt").write_text("You are a test agent. Use tools.")
        if url is not None:
            write_conf_dir(tmp_path, url=url)
        elif not (tmp_path / "persona.json").exists():
            write_conf_dir(tmp_path)
        return path

    return make


@pytest.fixture
def ctx(tmp_path):
    """Tools wired to a real runner process. The harness's state is in
    `raven/`; the model's working directory is `raven/work/`."""
    base = tmp_path / "raven"
    base.mkdir()
    link = Link(work=base / "work")
    link.open()
    with Store(base / "state.db") as db:
        goal_id, _ = db.add_goal("fix the thing", "cli")
        attempt_id = db.start_attempt(goal_id)
        yield tools.Context(db=db, link=link, goal_id=goal_id, attempt_id=attempt_id, shell_timeout=5)
    link.close()
