import json
from pathlib import Path

import pytest

from conftest import PERSONAS, write_conf_dir
from persistbot import config, persona
from persistbot.persona import (
    AuthenticationError,
    ConfigurationError,
    PersonaNotFoundError,
    UnknownDriverError,
)

REPO = Path(__file__).parent.parent


def test_resolve_builds_a_self_contained_model(conf_dir):
    model = persona.load(conf_dir).resolve("Raven", "Antelope")
    assert model.name == "Raven"
    assert model.model == "big-model"
    assert model.provider == "openrouter"
    assert model.driver == "openai"
    assert model.url == "http://127.0.0.1:9/v1"
    assert model.character == "You are an AI Linux administrator."
    assert model.parameters == {"temperature": 0.7}
    assert model.auth == {"key": "sk-test"}
    assert "sk-test" not in repr(model)
    assert model.identity() == (
        "Your name for this session is Raven. You are an AI Linux administrator."
    )


def test_provider_is_lowercased_and_empty_key_is_accepted(conf_dir):
    model = persona.load(conf_dir).resolve("Raven", "Bichon")  # provider written "Bichon"
    assert model.provider == "bichon"
    assert model.auth == {"key": ""}
    assert model.identity() == "Your name for this session is Raven."


def test_two_agents_may_share_a_persona(conf_dir):
    registry = persona.load(conf_dir)
    pro, con = registry.resolve("Pro", "Bichon"), registry.resolve("Con", "Bichon")
    assert (pro.name, con.name) == ("Pro", "Con")
    assert pro.model == con.model


def test_unknown_persona(conf_dir):
    with pytest.raises(PersonaNotFoundError, match="'Heron' is not defined.*Antelope, Bichon"):
        persona.load(conf_dir).resolve("Raven", "Heron")


def test_missing_provider_entry(tmp_path):
    write_conf_dir(tmp_path, providers={"openrouter": {"driver": "openai"}})
    with pytest.raises(ConfigurationError, match="provider 'bichon' has no entry.*provider.json"):
        persona.load(tmp_path).resolve("Raven", "Bichon")


def test_missing_keys_entry(tmp_path):
    write_conf_dir(tmp_path, keys={"openrouter": {"key": "x"}})
    with pytest.raises(AuthenticationError, match="provider 'bichon' has no entry.*keys.json"):
        persona.load(tmp_path).resolve("Raven", "Bichon")


@pytest.mark.parametrize(
    "missing, error, example",
    [
        ("personas", ConfigurationError, "persona.json"),
        ("providers", ConfigurationError, "provider.json"),
        ("keys", AuthenticationError, "keys.json"),
    ],
)
def test_absent_live_file_names_the_example_to_copy(tmp_path, missing, error, example):
    write_conf_dir(tmp_path, **{missing: False})
    with pytest.raises(error) as caught:
        persona.load(tmp_path)
    assert str(tmp_path / "examples" / example) in str(caught.value)


def test_parameter_order_is_provider_then_persona_then_caller(tmp_path):
    write_conf_dir(
        tmp_path,
        personas={
            "P": {
                "model": "m",
                "provider": "x",
                "parameters": {"top_p": 0.5, "presence_penalty": 0.2, "seed": 7},
            }
        },
        providers={
            "x": {
                "driver": "openai",
                "parameters": {"temperature": 0.1, "top_p": 0.9, "presence_penalty": 0.9},
            }
        },
        keys={"x": {"key": ""}},
    )
    registry = persona.load(tmp_path)
    assert registry.resolve("A", "P").parameters == {
        "temperature": 0.1, "top_p": 0.5, "presence_penalty": 0.2, "seed": 7,
    }
    tuned = registry.resolve("A", "P", parameters={"presence_penalty": 0.0, "seed": None})
    assert tuned.parameters == {
        "temperature": 0.1, "top_p": 0.5, "presence_penalty": 0.0, "seed": 7,
    }


def test_character_fallback(conf_dir):
    registry = persona.load(conf_dir)
    assert registry.resolve("A", "Antelope").character == "You are an AI Linux administrator."
    assert registry.resolve("A", "Antelope", character="  ").character == (
        "You are an AI Linux administrator."
    )
    assert registry.resolve("A", "Antelope", character="Be brief.").character == "Be brief."
    assert registry.resolve("A", "Bichon").character == ""


def test_driver_and_url_come_from_the_persona_when_set(tmp_path):
    personas = {
        "Own": {"model": "m", "provider": "x", "url": "http://own/v1", "driver": "openai"},
        "Plain": {"model": "m", "provider": "x"},
    }
    write_conf_dir(tmp_path, personas=personas, providers={"x": {"url": "http://prov/v1"}},
                   keys={"x": {}})
    registry = persona.load(tmp_path)
    assert registry.resolve("A", "Own").url == "http://own/v1"
    with pytest.raises(ConfigurationError, match="names a driver"):
        registry.resolve("A", "Plain")


def test_unknown_driver_is_caught_at_resolution(tmp_path):
    write_conf_dir(tmp_path, providers={"bichon": {"driver": "anthropic"}})
    with pytest.raises(ValueError, match="unknown driver 'anthropic'") as caught:
        persona.load(tmp_path).resolve("Raven", "Bichon")
    assert isinstance(caught.value, UnknownDriverError)


def test_duplicate_persona_name_across_two_files(conf_dir):
    (conf_dir / "persona_aws.json").write_text(json.dumps({"Bichon": PERSONAS["Bichon"]}))
    with pytest.raises(ConfigurationError, match="both persona.json and persona_aws.json"):
        persona.load(conf_dir)


def test_files_merge_with_persona_json_first_and_wrapped_form(conf_dir):
    (conf_dir / "persona-a.json").write_text(
        json.dumps({"persona": {"Heron": {"model": "h", "provider": "openrouter"}}})
    )
    (conf_dir / "persona_z.json").write_text(
        json.dumps({"Stork": {"model": "s", "provider": "openrouter"}})
    )
    (conf_dir / "other.json").write_text("not loaded")
    assert [p.name for p in persona.discover_persona_files(conf_dir)] == [
        "persona.json", "persona-a.json", "persona_z.json",
    ]
    registry = persona.load(conf_dir)
    assert sorted(registry.personas) == ["Antelope", "Bichon", "Heron", "Stork"]
    assert registry.resolve("A", "Heron").model == "h"


@pytest.mark.parametrize(
    "definition, message",
    [
        ({"provider": "x"}, "'model' is required"),
        ({"model": "m"}, "'provider' is required"),
        ({"model": "m", "provider": ""}, "provider: expected a non-empty string"),
        ({"model": "m", "provider": "x", "colour": "blue"}, "unknown field"),
        ({"model": "m", "provider": "x", "parameters": {"model": "other"}}, "belongs on the persona"),
        ({"model": "m", "provider": "x", "parameters": {"temperature": "0.9"}}, "without quotes"),
        ({"model": "m", "provider": "x", "parameters": {"timeout": 0}}, "positive number"),
        ({"model": "m", "provider": "x", "parameters": [1]}, "expected a map"),
        ("just text", "expected a map"),
        ({"model": "m", "provider": "x", "character": 5}, "character: expected text"),
        ({"model": "m", "provider": "x", "character": ["Be brief."]}, "character: expected text"),
        ({"model": "m", "provider": "x", "vendor": 7}, "vendor: expected text"),
    ],
)
def test_bad_persona_definitions(tmp_path, definition, message):
    write_conf_dir(tmp_path, personas={"P": definition})
    with pytest.raises(ConfigurationError, match=message):
        persona.load(tmp_path)


def test_non_numeric_string_parameters_pass_through(tmp_path):
    personas = {"P": {"model": "m", "provider": "bichon",
                      "parameters": {"reasoning_effort": "low", "stop": ["END"]}}}
    write_conf_dir(tmp_path, personas=personas)
    assert persona.load(tmp_path).resolve("A", "P").parameters == {
        "reasoning_effort": "low", "stop": ["END"],
    }


def test_caller_parameters_are_validated_too(conf_dir):
    registry = persona.load(conf_dir)
    with pytest.raises(ConfigurationError, match="belongs on the persona"):
        registry.resolve("A", "Bichon", parameters={"url": "http://elsewhere"})
    with pytest.raises(ConfigurationError, match="without quotes"):
        registry.resolve("A", "Bichon", parameters={"temperature": "0.9"})


def test_caller_character_must_be_text(conf_dir):
    with pytest.raises(ConfigurationError, match="caller character: expected text"):
        persona.load(conf_dir).resolve("A", "Bichon", character=5)


def test_agent_name_is_validated(conf_dir):
    with pytest.raises(ConfigurationError, match="agent name"):
        persona.load(conf_dir).resolve("bad/name", "Bichon")


def test_malformed_files(tmp_path):
    write_conf_dir(tmp_path)
    (tmp_path / "keys.json").write_text("{not json")
    with pytest.raises(AuthenticationError, match="not valid JSON"):
        persona.load(tmp_path)
    write_conf_dir(tmp_path, providers={"X": {"driver": "openai"}, "x": {"driver": "openai"}})
    with pytest.raises(ConfigurationError, match="given twice"):
        persona.load(tmp_path)
    write_conf_dir(tmp_path, providers={"bichon": {"driver": "openai", "key": "secret"}})
    with pytest.raises(ConfigurationError, match="unknown field"):
        persona.load(tmp_path)


def test_shipped_config_resolves_against_the_shipped_examples():
    cfg = config.load(REPO / "conf" / "init.yaml")
    registry = persona.load(REPO / "conf" / "examples")
    own = registry.resolve(cfg.name, cfg.persona)
    assert own.driver == "openai" and own.url
    for friend in cfg.friends:
        assert registry.resolve(friend, friend).driver == "openai"


def test_only_examples_are_shipped_and_live_files_are_ignored():
    conf = REPO / "conf"
    assert {p.name for p in (conf / "examples").iterdir()} == {
        "persona.json", "provider.json", "keys.json",
    }
    ignore = (REPO / ".gitignore").read_text().splitlines()
    for pattern in ("conf/keys.json", "conf/provider.json", "conf/persona*.json"):
        assert pattern in ignore
