import email.utils
import time

import pytest

import fakellm as fake
from conftest import write_conf_dir
from persistbot import driver, persona
from persistbot.driver import UpstreamFatal, UpstreamRetryable

MESSAGES = [{"role": "user", "content": "Check the disk."}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run one allowlisted command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


@pytest.fixture
def model(fakellm, tmp_path):
    """Resolve a persona whose provider points at the fake server."""

    def make(name="Bichon", **parameters):
        write_conf_dir(tmp_path / "conf", url=fakellm.url)
        return persona.load(tmp_path / "conf").resolve("Raven", name, parameters=parameters)

    return make


def test_text_reply(fakellm, model):
    fakellm.add(fake.text("The disk is fine.", usage=(42, 7)))
    reply = driver.call(model(), MESSAGES)
    assert reply.text == "The disk is fine."
    assert reply.tool_calls == ()
    assert (reply.prompt_tokens, reply.completion_tokens) == (42, 7)
    assert reply.finish_reason == "stop"
    assert reply.message() == {"role": "assistant", "content": "The disk is fine."}


def test_request_carries_model_messages_tools_parameters_and_key(fakellm, model):
    fakellm.add(fake.text("ok"))
    driver.call(model("Antelope", top_k=40, timeout=5), MESSAGES, TOOLS)
    sent = fakellm.requests[0]
    assert sent["path"] == "/v1/chat/completions"
    assert sent["headers"]["authorization"] == "Bearer sk-test"
    body = sent["body"]
    assert body["model"] == "big-model"
    assert body["messages"] == MESSAGES
    assert body["tools"] == TOOLS
    assert body["temperature"] == 0.7      # from the provider
    assert body["top_k"] == 40             # not an OpenAI parameter: still passed through
    assert "timeout" not in body           # consumed by the client


def test_empty_key_sends_a_placeholder_and_no_tools_key(fakellm, model):
    fakellm.add(fake.text("ok"))
    driver.call(model(), MESSAGES)
    sent = fakellm.requests[0]
    assert sent["headers"]["authorization"] == f"Bearer {driver.NO_KEY}"
    assert "tools" not in sent["body"]


def test_tool_calls_are_returned_as_written(fakellm, model):
    fakellm.add(
        fake.tool_calls(
            [("run_shell", {"command": "df -h"}, "call_a"), ("note", "{broken json", "call_b")],
            usage=(30, 12),
        )
    )
    reply = driver.call(model(), MESSAGES, TOOLS)
    assert reply.text == ""
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("call_a", "run_shell", '{"command": "df -h"}'),
        ("call_b", "note", "{broken json"),
    ]
    assert reply.finish_reason == "tool_calls"
    message = reply.message()
    assert message["tool_calls"][0] == {
        "id": "call_a",
        "type": "function",
        "function": {"name": "run_shell", "arguments": '{"command": "df -h"}'},
    }


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_retryable_statuses(fakellm, model, status):
    fakellm.add(fake.error(status, "try later"))
    with pytest.raises(UpstreamRetryable) as caught:
        driver.call(model(), MESSAGES)
    assert caught.value.status == status
    assert caught.value.retry_after is None
    assert "try later" in str(caught.value)
    assert len(fakellm.requests) == 1  # the SDK did not retry on its own


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_fatal_statuses(fakellm, model, status):
    fakellm.add(fake.error(status, "no"))
    with pytest.raises(UpstreamFatal) as caught:
        driver.call(model(), MESSAGES)
    assert caught.value.status == status
    assert not isinstance(caught.value, UpstreamRetryable)
    assert len(fakellm.requests) == 1


def test_retry_after_seconds(fakellm, model):
    fakellm.add(fake.error(429, retry_after=120))
    with pytest.raises(UpstreamRetryable) as caught:
        driver.call(model(), MESSAGES)
    assert caught.value.retry_after == 120.0


def test_retry_after_http_date(fakellm, model):
    fakellm.add(fake.error(503, retry_after=email.utils.formatdate(time.time() + 90, usegmt=True)))
    with pytest.raises(UpstreamRetryable) as caught:
        driver.call(model(), MESSAGES)
    assert 80 < caught.value.retry_after <= 90


def test_unreadable_retry_after_is_ignored(fakellm, model):
    fakellm.add(fake.error(429, retry_after="soon"))
    with pytest.raises(UpstreamRetryable) as caught:
        driver.call(model(), MESSAGES)
    assert caught.value.retry_after is None


def test_timeout_is_retryable(fakellm, model):
    fakellm.add(fake.slow(fake.text("too late"), 1.5))
    started = time.monotonic()
    with pytest.raises(UpstreamRetryable, match="timed out after 0.3s") as caught:
        driver.call(model(timeout=0.3), MESSAGES)
    assert time.monotonic() - started < 1.4
    assert caught.value.status is None


def test_dropped_connection_is_retryable(fakellm, model):
    fakellm.add(fake.drop())
    with pytest.raises(UpstreamRetryable, match="connection failed"):
        driver.call(model(), MESSAGES)


def test_connection_refused_is_retryable(fakellm, model):
    resolved = model()
    fakellm.stop()  # nothing listens on that port now
    with pytest.raises(UpstreamRetryable, match="connection failed"):
        driver.call(resolved, MESSAGES)


def test_reply_that_is_not_a_completion_is_fatal(fakellm, model):
    fakellm.add(fake.raw(200, "<html>captive portal</html>", "text/html"))
    with pytest.raises(UpstreamFatal):
        driver.call(model(), MESSAGES)
    fakellm.add({"status": 200, "body": {"choices": []}, "headers": {}})
    with pytest.raises(UpstreamFatal, match="unreadable reply"):
        driver.call(model(), MESSAGES)


def test_script_plays_in_order_and_records_overrun(fakellm, model):
    fakellm.add(fake.error(429), fake.text("second"))
    resolved = model()
    with pytest.raises(UpstreamRetryable):
        driver.call(resolved, MESSAGES)
    assert driver.call(resolved, MESSAGES).text == "second"
    assert fakellm.remaining == 0 and fakellm.unscripted == 0
    with pytest.raises(UpstreamRetryable):  # nothing scripted: the fake answers 500
        driver.call(resolved, MESSAGES)
    assert fakellm.unscripted == 1


def test_calls_are_written_to_the_action_log(fakellm, model, make_conf, tmp_path):
    from persistbot import config, log

    cfg = config.load(make_conf())
    log.setup(cfg.name, "local", cfg.logging.dir, "local", cfg.reporting.dir)
    fakellm.add(fake.text("ok", usage=(11, 3)), fake.error(429, "slow down"))
    resolved = model()
    driver.call(resolved, MESSAGES)
    with pytest.raises(UpstreamRetryable):
        driver.call(resolved, MESSAGES)
    log.close()
    text = (cfg.logging.dir / "action.log").read_text()
    assert "model call as Raven (bichon) ok" in text and "11 in, 3 out" in text
    assert "failed, retryable: HTTP 429" in text and "slow down" in text
    assert "sk-test" not in text
