# Persist Bot

A Python agent harness for edge nodes: woken by a timer, runs one bounded
tool loop against a small LAN model, keeps goals and memory in SQLite, and
resumes unfinished work on the next wake. Nobody chats with it.

## Working the plan

1. Read `docs/build-plan.md`. The status table shows the next phase that is
   not started. Work one phase per session and per pull request.
2. Read the "Core model" section of the plan before any phase from 3
   onward, and `docs/persona-pattern.md` before phase 2 or 5.
3. Branch from the current `main`. Do not start a phase before the previous
   one has merged; if it has not, say so and stop.
4. Build the phase's deliverables, then its acceptance tests. A phase is
   finished when `pytest` passes and the acceptance list is covered.
5. In the same pull request, update the status table, and correct the plan
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
- `conf/keys.json` is never committed.
- The harness runs as an unprivileged user. Nothing may require root before
  phase 8, and then only through the sudo allowlist.

## Commands

```bash
pip install -e '.[dev]'
```

```bash
pytest
```

```bash
python bot.py conf/init.yaml status
```
