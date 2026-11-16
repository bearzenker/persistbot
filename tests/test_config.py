import pytest

from persistbot.config import ConfigError, load


def test_defaults(make_conf, tmp_path):
    cfg = load(make_conf())
    base = tmp_path / "opt" / "raven"
    assert cfg.base == base
    assert cfg.system == base / "system.txt"
    assert cfg.logging.dir == base / "logs"
    assert cfg.reporting.dir == base / "logs"
    assert cfg.incoming == base / "incoming"
    assert cfg.db_path == base / "state.db"
    assert cfg.friends == {}
    assert (cfg.limits.max_turns, cfg.limits.max_failures) == (12, 3)


def test_full_file(make_conf, tmp_path):
    cfg = load(
        make_conf(
            """
            system: edge_bot
            friends:
              Antelope: python
            logging: { mode: local, dir: ./raven/l }
            reporting: { mode: local, dir: /var/tmp/r }
            skills: [bash]
            incoming: ./raven/in
            limits: { max_turns: 5, max_failures: 2 }
            """
        )
    )
    venv = tmp_path / "opt"
    assert cfg.system == venv / "raven" / "edge_bot.txt"
    assert cfg.friends == {"Antelope": "python"}
    assert cfg.logging.dir == venv / "raven" / "l"
    assert str(cfg.reporting.dir) == "/var/tmp/r"
    assert cfg.skills == ("bash",)
    assert cfg.incoming == venv / "raven" / "in"
    assert cfg.limits.max_turns == 5


def test_design_doc_list_forms_are_accepted(make_conf):
    cfg = load(
        make_conf(
            """
            friends:
              - Antelope: python
            logging:
              - mode: local
              - dir: ./raven/logs
            """
        )
    )
    assert cfg.friends == {"Antelope": "python"}
    assert cfg.logging.dir.name == "logs"


def test_base_defaults_to_venv(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text(f"bot:\n  venv: {tmp_path}\n  name: A\n  persona: P\n")
    assert load(path).base == tmp_path


@pytest.mark.parametrize(
    "body, message",
    [
        ("colour: blue", "unknown key"),
        ("logging: { mode: local, level: debug }", "unknown key"),
        ("limits: { max_turn: 3 }", "unknown key"),
        ("logging: { mode: rsyslog }", "not supported"),
        ("limits: { max_turns: 0 }", "positive integer"),
        ("limits: { max_turns: true }", "positive integer"),
        ("skills: bash", "expected a list"),
        ("friends: Antelope", "expected a map"),
    ],
)
def test_rejects(make_conf, body, message):
    with pytest.raises(ConfigError, match=message):
        load(make_conf(body))


@pytest.mark.parametrize("name", ["a/b", "two words", "../x", "dot.name"])
def test_rejects_bad_names(make_conf, name):
    with pytest.raises(ConfigError, match="bot.name"):
        load(make_conf(name=f"'{name}'"))


def test_missing_required_and_relative_venv(tmp_path):
    path = tmp_path / "b.yaml"
    path.write_text("bot:\n  venv: /opt/x\n  name: A\n")
    with pytest.raises(ConfigError, match="bot.persona is required"):
        load(path)
    path.write_text("bot:\n  venv: opt/x\n  name: A\n  persona: P\n")
    with pytest.raises(ConfigError, match="absolute"):
        load(path)


def test_unreadable_and_invalid_yaml(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load(tmp_path / "nope.yaml")
    path = tmp_path / "b.yaml"
    # The design doc's sample, with `reporting` missing its colon.
    path.write_text("bot:\n  name: A\n  reporting\n    - mode: local\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(path)
    path.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError, match="top-level 'bot'"):
        load(path)


def test_shipped_init_yaml_loads():
    from pathlib import Path

    cfg = load(Path(__file__).parent.parent / "conf" / "init.yaml")
    assert cfg.name == "Raven"
    assert str(cfg.base) == "/opt/persistbot/raven"


# the tools account and the working directory ------------------------------


def test_work_is_beside_the_state_when_there_is_no_user(make_conf, tmp_path):
    cfg = load(make_conf())
    assert cfg.user is None and cfg.runner is None
    assert cfg.work == tmp_path / "opt" / "raven" / "work"


def test_user_puts_work_beside_the_install_and_names_the_socket(make_conf, tmp_path):
    cfg = load(make_conf("user: raven\n"))
    assert cfg.user == "raven"
    assert cfg.work == tmp_path / "bot-raven"  # /opt/bot-raven for an install in /opt/persistbot
    assert str(cfg.runner) == "/run/persistbot/raven.sock"
    assert cfg.base == tmp_path / "opt" / "raven"  # state stays where it was


def test_work_and_runner_can_be_set(make_conf, tmp_path):
    cfg = load(make_conf("user: raven\nwork: /srv/raven\nrunner: /run/elsewhere.sock\n"))
    assert str(cfg.work) == "/srv/raven" and str(cfg.runner) == "/run/elsewhere.sock"
    assert load(make_conf("work: scratch\n")).work == tmp_path / "opt" / "scratch"


@pytest.mark.parametrize(
    "body, message",
    [
        ("runner: /run/x.sock", "bot.runner needs bot.user"),
        ("user: Raven", "not a Linux account name"),
        ("user: 'ra ven'", "not a Linux account name"),
        ("user: ''", "non-empty"),
        ("user: raven\nwork: raven/work", "inside bot.base"),
        ("user: raven\nwork: raven", "inside bot.base"),
    ],
)
def test_rejects_bad_separation_settings(make_conf, body, message):
    with pytest.raises(ConfigError, match=message):
        load(make_conf(body))
