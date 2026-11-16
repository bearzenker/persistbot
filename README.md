# Persist Bot

A small agent harness for edge nodes. A timer wakes it, it runs one bounded
attempt at its current goal against a small model on the LAN, records what
happened in SQLite, and exits. The next wake-up carries on from the notes
the last one left. Nobody chats with it.

It is built so that a cheap model of about 3B parameters can look at a
failure on its node, learn facts about that node, and write small repair
scripts, and so that an interrupted or failed run never loses the goal.

Status: MVP (`v0.1.0`). The roadmap is in `docs/build-plan.md`.

## What a wake-up does

One wake-up is a *tick*:

1. Take the bot's lock. If another tick holds it, exit quietly.
2. Close any attempt a dead tick left open, as `crashed`.
3. Queue new goals from the command line and from files in `incoming/`.
4. Pick the active goal, or else the oldest queued one that is due.
5. Run the model in a tool loop, at most `max_turns` turns.
6. Record the outcome and exit.

A goal is closed only when the model calls `goal_done`. Everything else
leaves it open for the next tick:

| Attempt outcome | Meaning | Counts as a failure |
|---|---|---|
| `completed` | the model called `goal_done` | no |
| `deferred` | the model server was unreachable, busy or refused the request | no; the goal waits, 5 minutes doubling to 1 hour |
| `budget` | the turn limit ran out | yes |
| `invalid` | three replies with no usable tool call | yes |
| `crashed` | the tick died part-way | yes |

After `max_failures` counted failures (three by default) the goal is
`stuck` and waits for an operator.

The model has six tools: `run_shell`, `read_file`, `write_file`, `remember`,
`note` and `goal_done`. `run_shell` runs allowlisted commands only, alone or
in a pipeline, without a shell. `write_file` writes only under the bot's own
directory.

## Install

Python 3.10 or later. As the unprivileged account that will run the bot:

```bash
git clone https://github.com/bearzenker/persistbot.git /opt/persistbot
```

```bash
cd /opt/persistbot && python3 -m venv .venv && .venv/bin/pip install .
```

The only runtime dependencies are `openai` and `PyYAML`.

## Configure

Three files say how to reach a model. Copy the examples and edit them:

```bash
cp conf/examples/*.json conf/
```

- `conf/persona.json`: named recipes for reaching a model (model ID,
  provider, optional character text and parameters).
- `conf/provider.json`: each provider's driver and base URL.
- `conf/keys.json`: each provider's API key. A self-hosted server needs an
  entry too; its `key` may be empty.

These three are ignored by git and must never be committed. The details
are in `docs/persona-pattern.md`.

One YAML file describes each bot. `conf/init.yaml` is the default:

```yaml
bot:
  venv: /opt/persistbot     # install directory
  base: raven               # this bot's directory: state.db, logs, incoming
  name: Raven               # the name the model is told it has
  persona: Bichon           # which persona in conf/persona*.json it runs as
  logging:   { mode: local, dir: ./raven/logs }
  reporting: { mode: local, dir: ./raven/logs }
  incoming: ./raven/incoming
  limits: { max_turns: 12, max_failures: 3 }
```

Relative paths are under `venv`. For more than one bot, copy it to
`conf/<name>.yaml` and give each its own `base`.

The model's instructions are `system.txt` in the install directory. A bot
may add its own in `<base>/system.txt`.

### Set the context window on the model server

Some model servers default to a small context window and cut the prompt
off without saying so. Ollama's default is 4096 tokens. A truncated prompt
loses the goal or the tool list, and the model then looks confused for no
visible reason. Set the context size on the server (for Ollama, `num_ctx`)
to at least 8192.

## Run

Check that the persona resolves:

```bash
.venv/bin/python bot.py conf/init.yaml status
```

Give the bot a goal and run one tick:

```bash
.venv/bin/python bot.py conf/init.yaml "Find out why /var/log/app.log is growing."
```

Other ways to give it work:

- `bot.py conf/init.yaml -f prompt.txt` queues the text of a file.
- Any file dropped into the bot's `incoming/` directory becomes a goal at
  the next tick and is moved to `incoming/done/`.

A tick with no arguments only continues existing work. Exit codes: `0`
goal done or nothing to do, `75` deferred on an upstream error, `1` the
attempt failed and the goal is still open, `2` configuration error.

## Run on a timer

```bash
sudo cp deploy/persistbot@.service deploy/persistbot@.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now persistbot@init.timer
```

The instance name is the config file: `persistbot@raven` runs
`conf/raven.yaml`. The units expect an account named `persistbot` that owns
`/opt/persistbot`; edit `User=` if yours differs. Nothing in the bot needs
root.

## See what it is doing

```bash
.venv/bin/python bot.py conf/init.yaml status
```

prints every goal, its state, and each attempt. Under the bot's log
directory:

- `report.log`: one line each time a goal is finished, an attempt fails, a
  goal becomes stuck, or the model server refuses the bot outright (a bad
  key or model name). This is the file to watch.
- `action.log`: what each tick did, including ordinary deferrals when the
  model server was busy or unreachable.
- `chat.log`: the full transcript of every request, reply and tool call.

A `stuck` goal stays stuck until an operator deals with it. Queueing the
same prompt again starts a fresh goal.

## Choosing a model

`evals/run.py` sends about twenty small tasks to a persona and reports how
often it calls a sensible tool when it should. Run it against each
candidate before trusting one:

```bash
.venv/bin/python evals/run.py conf/init.yaml -p Bichon -v
```

## Known limits

- The command allowlist is a guardrail against a confused model, not a
  sandbox. The unprivileged account is the security boundary. Options such
  as `find -exec` can run commands that are not on the list.
- The model can read any file the account can read, including
  `conf/keys.json`.
- No shell means no variables, wildcards or `cd` in `run_shell`.

## Development

```bash
pip install -e '.[dev]'
```

```bash
python -m pytest
```

No test needs a real model or the network.
