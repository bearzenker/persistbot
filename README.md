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
in a pipeline, without a shell. `write_file` writes only under the bot's
working directory.

## Two accounts

The bot is two processes on two Linux accounts:

| | Account | Does | Can reach |
|---|---|---|---|
| Harness | `persistbot` | talks to the model, holds the keys, keeps the goals, logs and queue | its own files; nothing is run here for the model |
| Runner | the bot's own, for example `raven` | runs the model's commands, reads and writes its files | what that account can reach, and its working directory `/opt/bot-raven` |

So the model cannot change the bot's code, configuration, instructions,
queue, logs or state, and cannot read the keys, whatever command it finds
a way to run. The kernel enforces this, not the allowlist.

The harness checks the arrangement at every tick, before any tool runs:
the kernel tells it which account the runner's answers come from, and the
runner reports whether it can write the code, configuration or state or
read the keys. If the account is wrong, or anything is reachable, the tick
stops with exit 2 and a line in `report.log`, and the goal is left as it
was. It never falls back to running tools on its own account.

A bot with no `user:` in its config runs both parts on one account. That
is for development. `status` and the action log say `not separated`.

## Install

Python 3.10 or later, and systemd. As root:

```bash
useradd --system --home-dir /opt/persistbot --shell /usr/sbin/nologin persistbot
```

```bash
git clone https://github.com/bearzenker/persistbot.git /opt/persistbot
```

```bash
cd /opt/persistbot && python3 -m venv .venv && .venv/bin/pip install .
```

Installed this way `/opt/persistbot` belongs to root, which is what is
wanted: neither account can change the code. The only runtime dependencies
are `openai` and `PyYAML`.

For each bot, create its account, its working directory and its base
directory. Creating the account is the system administrator's job; the
harness never does it. For a bot named `raven`:

```bash
useradd --system --home-dir /opt/bot-raven --shell /usr/sbin/nologin raven
```

```bash
install -d -o raven -g raven -m 0750 /opt/bot-raven
```

```bash
install -d -o persistbot -g persistbot -m 0700 /opt/persistbot/raven
```

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/persistbot/` | `root:root` 0755 | code, `.venv`, `system.txt` |
| `/opt/persistbot/conf/` | `root:persistbot` 0750, files 0640 | bot YAML, persona, provider and keys files |
| `/opt/persistbot/raven/` | `persistbot:persistbot` 0700 | the bot's base: `state.db`, `logs/`, `incoming/` |
| `/opt/bot-raven/` | `raven:raven` 0750 | the model's working directory |

What the bot may read on the node is decided the ordinary way: add `raven`
to a group such as `adm` if it should read `/var/log`.

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
are in `docs/persona-pattern.md`. Close the directory to everyone but the
harness:

```bash
chgrp -R persistbot conf && chmod 0750 conf && chmod 0640 conf/*.json conf/*.yaml
```

One YAML file describes each bot. `conf/init.yaml` is the default:

```yaml
bot:
  venv: /opt/persistbot     # install directory
  base: raven               # the harness's directory for this bot: state.db, logs, incoming
  user: raven               # Linux account the tools run as
  name: Raven               # the name the model is told it has
  persona: Bichon           # which persona in conf/persona*.json it runs as
  logging:   { mode: local, dir: ./raven/logs }
  reporting: { mode: local, dir: ./raven/logs }
  incoming: ./raven/incoming
  limits: { max_turns: 12, max_failures: 3 }
```

Relative paths are under `venv`. For more than one bot, copy it to
`conf/<name>.yaml` and give each its own `base` and `user`. Name the file
after the account (`conf/raven.yaml` for `raven`): the systemd units use
one name for both.

With `user: raven` the model's working directory is `/opt/bot-raven`, a
directory named `bot-<user>` beside the install directory. `work:` sets a
different one. It may not be inside `base` or the install directory. If
you set it, also change `ReadWritePaths=` in
`persistbot-runner@.service` to the same path: that unit makes everything
else read-only to the tools, and `status` will say the working directory
cannot be written until the two agree. The shipped `conf/init.yaml`
leaves `user` out so that it runs anywhere; set it on a real install.

The model's instructions are `system.txt` in the install directory. A bot
may add its own in `<base>/system.txt`.

### Set the context window on the model server

Some model servers default to a small context window and cut the prompt
off without saying so. Ollama's default is 4096 tokens. A truncated prompt
loses the goal or the tool list, and the model then looks confused for no
visible reason. Set the context size on the server (for Ollama, `num_ctx`)
to at least 8192.

## Run

Install the units first ("Run on a timer"): the runner is started by
systemd. Then check that the persona resolves and the accounts are right:

```bash
sudo -u persistbot .venv/bin/python bot.py conf/raven.yaml status
```

The third line should read `tools separated as raven (uid ...), working
directory /opt/bot-raven, checked`. Anything else names what to fix: the
account does not exist, the socket is not enabled, or `raven` can write or
read something it must not.

Give the bot a goal and run one tick:

```bash
sudo -u persistbot .venv/bin/python bot.py conf/raven.yaml "Find out why /var/log/app.log is growing."
```

Other ways to give it work:

- `bot.py conf/raven.yaml -f prompt.txt` queues the text of a file.
- Any file dropped into the bot's `incoming/` directory becomes a goal at
  the next tick and is moved to `incoming/done/`. Only `persistbot` and
  root can write there. The bot's own account cannot, so a bot cannot give
  itself work.

A tick with no arguments only continues existing work. Exit codes: `0`
goal done or nothing to do, `75` deferred on an upstream error, `1` the
attempt failed and the goal is still open, `2` configuration error.

## Run on a timer

```bash
sudo cp deploy/persistbot@.service deploy/persistbot@.timer deploy/persistbot-runner@.socket deploy/persistbot-runner@.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now persistbot-runner@raven.socket persistbot@raven.timer
```

The instance name is both the config file and the account:
`persistbot@raven` runs `conf/raven.yaml`, and `persistbot-runner@raven`
runs the tools as `raven`.

- `persistbot@.service` is the harness. It runs as `persistbot` with the
  whole file system read-only except `/opt/persistbot/<name>`.
- `persistbot-runner@.socket` listens on `/run/persistbot/<name>.sock`,
  which only `persistbot` may open.
- `persistbot-runner@.service` is started by that socket, as the bot's
  account, for one tick. Its `User=` line is the authority for which
  account the tools run as; `user:` in the YAML states what the harness
  expects, and the harness refuses to run tools if they differ. When the
  tick ends systemd stops anything its commands left running.

If your paths or names differ from `/opt/persistbot/<name>` and
`/opt/bot-<name>`, edit the units to match. Nothing in the bot needs root
once it is installed.

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
  sandbox. Options such as `find -exec` can run commands that are not on
  the list. Whatever runs, runs as the bot's account: that account is the
  security boundary, and the harness is outside it.
- The model can read any file the bot's account can read, and can use the
  network as that account can.
- Without `user:` in the config there is one account and none of this
  holds: the model can change the bot's code and read its keys.
- The runner unit sets `NoNewPrivileges`, so `sudo` cannot work from a
  tool call.
- No shell means no variables, wildcards or `cd` in `run_shell`.

## Development

```bash
pip install -e '.[dev]'
```

```bash
python -m pytest
```

No test needs a real model or the network.
