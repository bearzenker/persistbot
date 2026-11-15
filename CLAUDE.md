# Persist Bot

A Python agent harness for edge nodes: woken by a timer, runs one bounded
tool loop against a small LAN model, keeps goals and memory in SQLite, and
resumes unfinished work on the next wake. Nobody chats with it.

## Continuing ("continue where you left off")

The repo is the only memory between sessions. Before anything else:

1. `git fetch origin`, then read the Status table and the Handoff section
   of `docs/build-plan.md` as they are on `origin/main`.
2. Look for unfinished work: open pull requests, and remote branches that
   are ahead of main (`git branch -r --no-merged origin/main`). Skip any
   branch the Handoff section calls dead.
   - A branch with a phase marked `in progress`: that is the work to
     continue. Merge it into this session's branch, read its Handoff
     section, run the tests, and carry on from "Next".
   - An open pull request for a finished phase: do not start the next
     phase. Deal with any review comments, tell the owner it is waiting to
     be merged, and stop.
3. If nothing is unfinished, start the first phase that is `not started`,
   following "Working the plan".
4. If the tree, the tests and the Handoff section disagree, trust the tree
   and the tests, say what differs, and correct the Handoff section.

## Checkpoints

A session can end without warning, so unpushed work is lost work.

- The first commit of a phase sets its row to `in progress`, names the
  branch in the table, and rewrites the Handoff section.
- Commit and push whenever the tests pass and something works that did not
  before. Each push keeps the Handoff section true: what is finished, what
  is next, anything a new session would otherwise have to rediscover.
- Open the pull request at the first push, so unfinished work is visible.
  Say in its description that the phase is still in progress until it is
  finished.
- Decisions the owner gives in conversation go into the plan ("Open
  questions" or "Owner decisions") in the same push.

## Working the plan

1. Read `docs/build-plan.md`. The status table shows the next phase that is
   not started. Work one phase per session and per pull request. A change
   to the plan or these instructions alone may have its own pull request.
2. Read the "Core model" section of the plan before any phase from 3
   onward, and `docs/persona-pattern.md` before phase 2 or 5.
3. Branch from the current `main`. Do not start a phase before the previous
   one has merged; if it has not, say so and stop.
4. Build the phase's deliverables, then its acceptance tests. A phase is
   finished when the tests pass and the acceptance list is covered.
5. In the same pull request, set the phase to `done` in the status table,
   rewrite the Handoff section to name the next phase, and correct the plan
   if the work showed it was wrong. Say what changed in the PR description.
6. The plan's "Open questions" list has a default for each item. Build to
   the default unless the owner has answered; record answers in the plan.

## Rules that hold in every phase

- Never assume a goal finished. Only an explicit `goal_done` call closes
  one. Any code path that ends a tick must leave state the next tick can
  resume from.
- An upstream error (429, 5xx, timeout, connection failure) is a deferral,
  not a failure. It must not count toward the failure limit.
- Runtime dependencies are `openai` and `PyYAML`. Adding one needs the
  owner's agreement.
- At most six tool schemas per request; one or two lines of description
  each. The target model is about 3B parameters.
- All logging goes through `persistbot.log`. No direct `logging` or `print`
  calls elsewhere.
- No test may need a real model or the network. Use `tests/fakellm.py`.
- Live `conf/keys.json`, `conf/provider.json` and `conf/persona*.json` are
  never committed. The repo carries examples only, in `conf/examples/`.
- The harness runs as an unprivileged user. Nothing may require root before
  phase 8, and then only through the sudo allowlist.

## Cloud build sessions

The owner's cloud environment ("PersistBot") carries three variables, each
the JSON text of one live config file: `KEY` (keys), `PROVIDER` (provider)
and `PERSONA` (persona, currently `Raven` on OpenRouter). Its network
allows `openrouter.ai`. At the start of a session, when they are set:

```bash
scripts/cloud-conf.sh
```

```bash
python bot.py conf/cloud.yaml status
```

- The script writes `conf/keys.json`, `conf/provider.json`,
  `conf/persona_oprtr.json` and `conf/cloud.yaml`. The last is
  `conf/init.yaml` with `venv` set to the checkout and `persona` set to the
  one in `PERSONA`. All four are untracked. Use `conf/cloud.yaml` for live
  runs; do not edit `conf/init.yaml` for this, because the tests resolve
  the shipped file against `conf/examples/`.
- The key is disposable and pre-paid. Never print it, log it, commit it or
  put it in a pull request, and never copy it out of this environment. A
  401, 402 or 403 from OpenRouter means it has expired or run out: tell the
  owner, who will supply a new one, and carry on with the fake server.
- Live calls are for checking that a phase works against a real model, and
  cost the owner money: keep them few and small. They are never part of
  `python -m pytest`.
- If the variables are absent, skip all of this. Every phase can be built
  and tested without them.

## Commands

```bash
pip install -e '.[dev]'
```

```bash
python -m pytest
```

Use `python -m pytest`, not a bare `pytest`: the one on the path may be a
separate install that cannot see the project's dependencies.

```bash
python bot.py conf/init.yaml status
```
