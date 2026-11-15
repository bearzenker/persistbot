# Persist Bot build plan

Source design: Google Doc "Persistent Bot" (main body, Appendix A agent
comparison, Appendix B persona pattern). This plan narrows that mid-state
design to an MVP and orders the rest into phases, one branch and one pull
request per phase.

How to work this plan is in `CLAUDE.md` at the repo root. The persona,
provider and keys contract is in `docs/persona-pattern.md`.

The Google Doc is not needed to build any phase. Every detail the phases
depend on is in this file or in `docs/persona-pattern.md`; the owner chose
that over committing a copy of the doc. If a session finds it needs
something that is only in the doc, ask the owner and write the answer here.

## Status

Update this table in the same pull request that finishes a phase. Status is
`not started`, `in progress` or `done`. A phase is finished for the purpose
of starting the next one only when its row says `done` on `origin/main`.

| Phase | Scope | Branch | Status |
|---|---|---|---|
| 0 | This plan | `claude/persistbot-mvp-plan-5bdmdy` | done |
| 1 | Skeleton: package, config, logging, state store, lock | `claude/persistbot-mvp-plan-5bdmdy` | done |
| 2 | Driver: persona resolution, OpenAI client, error classes | `claude/awesome-maxwell-8j67y6` | done |
| 3 | Tools: allowlisted shell, files, memory, notes | `claude/youthful-pasteur-jy7xzm` | in progress |
| 4 | Tick loop: goal persistence, recovery, timer units. **MVP, tag `v0.1.0`** | `phase-4-tick` | not started |
| 5 | Phone a friend | `phase-5-friend` | not started |
| 6 | Planning mode | `phase-6-plan` | not started |
| 7 | To Do list | `phase-7-todo` | not started |
| 8 | Reporting, rsyslog, sudo list, custom tools, install | `phase-8-ops` | not started |
| 9 | Backlog (unscheduled) | n/a | n/a |

Branch names are suggestions. If the session assigns a branch name, use that
and put the phase number in the pull request title. Each phase branches from
`main` after the previous phase has merged.

## Handoff

This section is what a new session reads to continue. Rewrite it, do not
append to it, in every push that changes the state of the work. Sessions can
end without warning, so it must be true at every pushed commit.

- **State:** phases 0, 1 and 2 are merged. Phase 3 (tools) is in progress
  on `claude/youthful-pasteur-jy7xzm`.
- **Finished in phase 3:** `scripts/cloud-conf.sh` and the "Cloud build
  sessions" section of `CLAUDE.md`; `allowlist.py`; `tools/` (registry,
  `schemas()`, `call()` and the six tools); `memory.py`; tests for all of
  it. A live tool call as persona `Raven` through OpenRouter worked.
- **Next:** `evals/` (script and task file, with a test against the fake
  server), then the phase 3 "As built" notes, then set the row to `done`.
- **Waiting on the owner:** nothing. Open questions use their defaults.
- **Dead branch:** `claude/persistbot-mvp-plan-5bdmdy` holds one unmerged
  commit, `a77e334`, that deletes the phase 1 package. The owner decided to
  keep phase 1. Never merge or build on that branch.
- **Environment:** run tests with `python -m pytest`. A bare `pytest` on the
  path can be a separate tool install that cannot import `yaml`. In the
  owner's cloud environment, run `scripts/cloud-conf.sh` first for a live
  persona; see "Cloud build sessions" in `CLAUDE.md`.

## What the product is

A Python (3.10 or later) agent harness for edge nodes. It is started by a
timer or by an error-handling script, never by a person, and has no chat
mode. Each start is one *tick*: take the lock, find work, run a bounded tool
loop against a small model on the trusted LAN, record what happened, exit.

The job it is built for: a small, cheap model on an edge node looks at a
failure, learns facts about its node, and writes small repair scripts. It
must stay light enough for micro VMs and single-board computers.

Two properties define success:

- **Persistence.** A tick never assumes the task finished. An upstream
  failure (HTTP 429, 5xx, timeout, connection refused), a crash, or a spent
  turn budget leaves the goal open, and the next tick picks it up with the
  notes the earlier ticks left behind.
- **Phone a friend.** When the small model fails a task three times, the
  harness asks a stronger named persona for guidance. This is the critical
  success factor. It lands in phase 5, but the MVP builds the attempt
  counter and the stuck state it depends on.

## Decisions taken for the MVP

These follow the design doc and Appendix A unless marked *open*; open items
are collected at the end with the default this plan assumes.

| Topic | Decision |
|---|---|
| Runtime dependencies | `openai`, `PyYAML`. Everything else is the standard library. No MCP, LiteLLM or agent framework. |
| State | One SQLite file per bot, `<base>/state.db`, WAL mode, standard `sqlite3` module. Schema versioned with `PRAGMA user_version` and numbered migration functions. |
| Concurrency | One tick per bot at a time, enforced with `flock` on `<base>/bot.lock`. A second start exits 0 quietly. |
| Model protocol | OpenAI chat completions with native tool calling, for both the edge model and the friends. |
| SDK retries | `max_retries=0`, `timeout=300`. The harness owns retry policy: it defers to the next tick. |
| Tool budget | At most six tool schemas in any one request, each description one or two lines. Later phases swap tools by mode instead of adding to the list. |
| Prompt budget | System prompt plus tool schemas under about 1,500 tokens. Memory injected into the prompt is capped by character count. |
| Shell safety | The allowlist is a guardrail against a confused model, not a sandbox. The security boundary is the unprivileged account. |
| Layout | `src/persistbot/` package, `bot.py` launcher at the repo root so `/opt/persistbot/bot.py ./conf/raven.yaml` works as the design doc shows. |
| Not adopted | Goose and Hermes: their system prompt and tool schemas are too large for a 3B model, and their schedulers are unreliable unattended. LiteLLM: a large dependency for endpoints that already speak the OpenAI protocol. `pydantic-ai-slim` was the fallback had we not wanted to own the loop. Do not reopen these without the owner. |

## Core model: goals and attempts

A **goal** is one unit of work: the prompt from the command line, a prompt
file, or (later) a plan or a To Do item. An **attempt** is one tick spent on
a goal.

Goal states:

```
queued -> active -> done
                 -> stuck      (failure limit reached; phase 5 escalates from here)
                 -> abandoned  (operator action or hard error)
```

Attempt outcomes and whether they count toward the failure limit of three:

| Outcome | Meaning | Counts as a failure |
|---|---|---|
| `completed` | The model called `goal_done` | no |
| `deferred` | Upstream error; nothing wrong with the model's work | no, tracked separately with backoff |
| `budget` | Turn limit reached without `goal_done` | yes |
| `invalid` | Malformed tool call, or text with no tool call, repeated past a small limit | yes |
| `crashed` | Attempt row has a start but no end, found by the next tick | yes |

Rules that carry the persistence requirement:

1. A goal becomes `done` only through an explicit `goal_done(summary)` tool
   call. Any other way of ending a tick leaves it `active`.
2. Every tool call is journalled in two writes: a row before execution, an
   update with the result after. If a tick dies between them, the next
   attempt is told that the command was started and its outcome is unknown,
   and that it should inspect before repeating.
3. A new attempt does not replay the previous transcript. It is given the
   goal, the work notes, and a short harness-written summary of each earlier
   attempt (outcome, last few tool calls). This keeps the prompt small for a
   3B model and matches the "fresh start" reasoning in the design doc.
4. Three counted failures move the goal to `stuck`, write a report line, and
   stop work on it. In the MVP a stuck goal waits for an operator. Phase 5
   replaces the wait with a call to a friend.
5. After a `deferred` outcome the goal records `not_before`, with backoff
   doubling from 5 minutes to a 1 hour cap, honouring `Retry-After` when the
   server sends it. Ticks before that time exit without calling the model.

Work selection order in a tick: active goal, then oldest queued goal, then
(later phases) active plan step, then To Do.

Exit codes: `0` goal done or nothing to do, `75` (`EX_TEMPFAIL`) deferred on
an upstream error, `1` attempt failed and the goal remains open, `2`
configuration or startup error.

## Phase 1: skeleton

No model calls in this phase.

Deliverables:

- `pyproject.toml` (Python 3.10 or later, dependencies above, `pytest` as a
  development extra), `bot.py` launcher, `src/persistbot/__main__.py`.
- `config.py`: load `conf/<bot>.yaml`, or `conf/init.yaml` when no path is
  given. Resolve `venv` (absolute), `base` (directory, defaults to `venv`),
  `system`, `name`, `persona`, `friends`, `logging`, `reporting`, `skills`,
  `incoming`. Validate `name` against `[A-Za-z0-9_-]+`. Unknown keys are an
  error. The sample YAML in the design doc is not valid as written
  (`reporting` lacks a colon, `logging` mixes list and map); use plain maps:

  ```yaml
  bot:
    venv: /opt/persistbot
    base: raven
    system: edge_bot
    name: Raven
    persona: Bichon
    friends:
      Antelope: python
    logging:   { mode: local, dir: ./raven/logs }
    reporting: { mode: local, dir: ./raven/logs }
    skills: [bash]
    incoming: ./raven/incoming
    limits: { max_turns: 12, max_failures: 3 }
  ```

- `log.py`: one function family the rest of the code calls, never
  `logging.getLogger` inline. Three sinks under the log directory:
  `action.log` (operations), `chat.log` (append-only transcript of every
  request, response and tool call), `report.log`. `mode: local` only; the
  function signature must allow an rsyslog mode later without touching
  callers.
- `store.py`: open the database, apply migrations, typed helpers. MVP
  tables: `goal`, `attempt`, `tool_call`, `note`, `memory`, `meta`.
- `lock.py`: non-blocking `flock`.
- CLI: `bot.py [conf] [-f prompt_file] [prompt words...]`, plus
  `bot.py [conf] status` to print goals and attempts for an operator.

Acceptance: `pytest` passes; a tick with no work creates the database and
logs, and exits 0; a second concurrent start exits 0 without touching state.

As built:

- A prompt given on the command line or with `-f` is queued as a goal
  already in this phase, so input is never dropped. Nothing runs a goal
  until phase 4. Phase 4 adds the `incoming/` directory and the runner.
- `Store.add_goal` refuses a duplicate only while a goal with the same text
  is still `queued` or `active`. A recurring timer prompt therefore cannot
  pile up behind itself, and it is accepted again once the earlier goal has
  closed.
- The config file is the first argument when it ends in `.yaml` or `.yml`;
  otherwise `conf/init.yaml` beside `bot.py` is used.
- The config accepts `friends` and `logging` either as maps or as the
  design doc's lists of single-key maps.
- `log.error` writes to stderr as well as `action.log`, so a calling script
  or cron mail sees startup failures. `log.out` is the one path to stdout,
  used by `status`.

## Phase 2: driver

Read `docs/persona-pattern.md` first. It is the contract.

Deliverables:

- `persona.py`: discover and merge `conf/persona*.json`, load
  `conf/provider.json` and the keys file, and `resolve(name, persona,
  character=None, parameters=None) -> ResolvedModel`. Startup resolves the
  bot's own persona and fails with exit 2 on any missing piece. When a live
  file is absent the error names the example to copy.
- The repo carries examples only. The live `conf/persona*.json`,
  `conf/provider.json` and `conf/keys.json` belong to each installation and
  are ignored by git. The examples live in `conf/examples/` as
  `persona.json`, `provider.json` and `keys.json`; the operator copies them
  into `conf/` and edits them. They sit in their own directory so that the
  `conf/persona*.json` discovery cannot load an example as live config.
- The examples must resolve the personas named in `conf/init.yaml`:
  - `Bichon`, the bot's own persona: a self-hosted model on the LAN,
    provider `bichon`, model
    `ggml-org/Ministral-3-3B-Instruct-2512-GGUF:Q8_0`, with a placeholder
    URL. That ID is a Hugging Face GGUF repo and quantisation tag, the form
    a llama.cpp server takes.
  - `Antelope`, the friend, from the design doc:

    ```json
    "Antelope": {
      "model": "REPLACE-WITH-OPENROUTER-MODEL-ID",
      "provider": "openrouter",
      "character": "You are an AI Linux administrator.",
      "parameters": {
        "temperature": 1.1, "top_p": 0.96,
        "presence_penalty": 0.5, "frequency_penalty": 0.5
      }
    }
    ```

    The design doc's example gave `Antelope` the GGUF ID above. OpenRouter
    does not accept that form, so the example moves it to `Bichon` and
    leaves the friend's model for the operator to choose. It should be a
    stronger model than the bot's own.
  - `provider.json` carries `openrouter` with driver `openai`, URL
    `https://openrouter.ai/api/v1`, and the same four optional parameters,
    and `bichon` with driver `openai` and a placeholder LAN URL.
  - `keys.json` carries empty `key` entries for `bichon`, `openrouter`,
    `openai`, `xai` and `aws`.
- Parameters: `timeout` is taken by the client. Every other parameter is
  passed through to the chat completion request unchanged.
- `driver.py`: `call(model, messages, tools) -> Reply`, with a `DRIVERS` map
  holding only `openai`. `Reply` carries text, tool calls, token usage.
- Error classification, the piece recovery depends on:
  - `UpstreamRetryable`: 408, 409, 429, 5xx, timeouts, connection errors.
    Carries `retry_after` when present.
  - `UpstreamFatal`: 400, 401, 403, 404 and anything else. Reported, and the
    goal is not burned for it: the attempt is `deferred` with a long backoff
    and a report line, because a bad key is an operator problem.
- `tests/fakellm.py`: a standard-library HTTP server that plays a scripted
  list of chat completion responses, including status codes, delays and
  dropped connections. All later phases test against it. No test needs a
  real model or network.

Acceptance: tests cover unknown persona, missing provider entry, missing key
entry, parameter override order, character fallback, duplicate persona name,
and each error class against the fake server.

As built:

- The conf directory is the directory the bot's YAML file is in. The
  persona, provider and keys files are read from there.
- `persona.load(conf_dir)` returns a `Registry`; `resolve` is its method.
  Phase 5 resolves each friend with the same registry. Friends are not
  resolved at startup yet.
- A persona that does not resolve ends the start with exit 2, but only
  after the prompt has been queued, so input is not dropped. `status`
  still runs and prints why the persona does not resolve.
- Parameters travel in the request body as written, so names the OpenAI
  SDK does not know (`top_k` on llama.cpp) still reach the server.
- A parameter is rejected when it is a number written as a string
  (`"0.9"`). Other strings pass, because some parameters are words
  (`reasoning_effort`). The contract said every string was an error; it is
  corrected.
- `character` and `vendor` must be text. Anything else in a persona file,
  or a non-text `character` from the caller, is a `ConfigurationError`.
- A 200 reply that is not a readable chat completion (an HTML page from a
  proxy, an empty `choices`) is `UpstreamFatal`.
- `Reply.message()` gives the assistant message to append to the
  conversation. `ToolCall.arguments` is the text the model wrote, unparsed;
  phase 4 parses it and treats bad JSON as `invalid`.
- The driver writes one action-log line per call (persona, outcome, tokens,
  duration). It does not write the transcript; that is the tick's job in
  phase 4.
- `tests/fakellm.py`: steps are built with `text`, `tool_call`,
  `tool_calls`, `error`, `drop`, `slow` and `raw`, queued with `add`, and
  played one per request. `requests` records what was sent; `unscripted`
  counts requests that arrived after the script ran out. The `fakellm`
  fixture starts one, and `write_conf_dir(dir, url=fakellm.url)` in
  `tests/conftest.py` writes persona files that point at it.

## Phase 3: tools

Deliverables:

- `tools/` with a small registry: name, one-line description, JSON schema,
  handler. The MVP set is exactly six:

  | Tool | Purpose |
  |---|---|
  | `run_shell(command)` | Run one allowlisted command |
  | `read_file(path)` | Read a text file, size capped |
  | `write_file(path, content)` | Write a file under the bot's base directory |
  | `remember(text)` | Store an operational fact about this node |
  | `note(text)` | Work note on the current goal, shown to later attempts |
  | `goal_done(summary)` | Declare the goal complete |

- `allowlist.py`: the unprivileged list from the design doc, which is
  exactly:

  ```
  awk, cat, chmod, cp, diff, file, find, free, grep, head, host,
  hostname, kill, killall, ln, logger, ls, mkdir, mv, ps, pwd, rm, rmdir,
  sort, tail, tar, umask, uniq, unzip, which, zcat, zgrep, zip
  ```

  plus three wildcard names: `ls*`, `?zip`, `?unzip`. Commands are matched
  left anchored as `^<command>(\s|$)`, with the wildcard names
  expanded so the wildcard part matches only `[A-Za-z0-9_-]`. The
  command is split with `shlex` and run without a shell, with a timeout and
  an output cap. Any of `; & | $ < >`, a backtick, a newline or another
  control character rejects the command before matching. `sudo` is rejected
  until phase 8.
- Tool errors are returned to the model as tool results, never raised.
- Memory: `remember` writes to `memory` with a kind column (`memory` now;
  `gossip` and `personality` later use the same table). Rows are injected
  into the system prompt newest first up to the character cap.
- `evals/`: a script that runs about twenty small tasks against the six real
  schemas and prints the act-when-it-should rate per persona. Appendix A
  asks for this before trusting any 3B model. It needs the LAN model, so the
  operator runs it; the phase ships the script and the task file. Appendix A
  names the models to compare: Ministral 3B, Qwen3.5-4B, Gemma 4 small and
  Granite 4.2 3B. Keep whichever acts most often when it should. The
  benchmark it cites had Ministral 3B avoiding wrong calls reliably but
  acting only half the time it should have, so the tasks must include cases
  where doing nothing is the wrong answer.
- `umask` is a shell builtin. Run without a shell it is either missing or
  has no effect. It stays on the list as the design doc has it, and a
  missing command comes back to the model as a tool error.

Acceptance: allowlist tests include the design doc's own counterexample
(`ls\nrm\x20/tmp/...`), pipes, substitutions, absolute-path commands and
each wildcard; file tools refuse paths that escape the base directory for
writes.

## Phase 4: tick loop (MVP)

Deliverables:

- `tick.py`:
  1. Take the lock.
  2. Close any attempt left open by a dead tick as `crashed`.
  3. Ingest input: command-line prompt, `-f` file, files in `incoming/`.
     Each becomes a queued goal through `Store.add_goal`, then the file
     moves to `incoming/done/`. `add_goal` returns the existing goal when
     one with the same text is still open, which makes a crash between the
     insert and the move harmless.
  4. Select work, honouring `not_before`.
  5. Build the prompt: `<venv>/system.txt`, `<base>/system.txt`, tool list,
     the identity line (`Your name for this session is <name>.` plus the
     persona character), memory, then the goal, notes and attempt summaries
     as the user message.
  6. Run the tool loop up to `max_turns`, journalling every call. The tick
     writes each new message and tool result to `chat.log`; the driver
     does not.
  7. Record the outcome, apply the failure rules above, write the report
     line, set the exit code.
- `system.txt` at the repo root: short, uniform instructions. It must tell
  the model that work continues across runs, to leave notes, and to call
  `goal_done` only after checking the result.
- `deploy/persistbot@.service` and `deploy/persistbot@.timer` examples
  running as an unprivileged user, plus a short `README.md` covering
  install, config and the status command. The README must warn that some
  model servers default to a small context window and truncate silently
  (Ollama's default is 4096 tokens), so the operator sets the context size
  on the server.
- `skills` in the config is a list of names under the bot's base directory.
  The design doc puts "skills and tools available" in the system prompt but
  does not define a skill beyond that. In the MVP the names are loaded and
  validated only; phase 9 gives them content.
- Fault-injection tests against the fake server, which are the acceptance
  test for the MVP:
  - 429 on the first call: goal stays active, exit 75, `not_before` set,
    next tick after the backoff completes the goal.
  - 429 in the middle of a tool loop: completed tool calls are in the
    journal and summarised to the next attempt.
  - Process killed mid-tool: next tick records `crashed`, tells the model
    the outcome is unknown, and continues.
  - Three `budget` outcomes: goal goes `stuck`, report line written, later
    ticks do not call the model for it.
  - Deferred outcomes alone never make a goal `stuck`.
  - Memory written in one tick appears in the next tick's prompt.
  - The same incoming file delivered twice produces one goal.

Done when: all of the above pass, and the operator has run one real goal on
a VM against a LAN model through the systemd timer. Tag `v0.1.0`.

## Phase 5: phone a friend

First after the MVP because it is the critical success factor.

- Resolve every persona in `friends` at startup; a missing friend is exit 2.
- Deterministic triggers in the harness, since a small model seldom notices
  it is stuck: the failure limit of three, the same tool and arguments
  repeated, and `goal_done` contradicted by a check. These call the friend
  on the model's behalf. Appendix A's full trigger list, for reference: N
  consecutive tool errors, a malformed tool call, the turn budget spent, the
  same tool and arguments repeated, and "done" without the check passing.
  The second and third are already counted failures in the core model;
  decide at the start of the phase whether consecutive tool errors needs
  its own trigger.
- `phone_a_friend(persona, question)` tool as the optional model-initiated
  path. The system prompt lists friends with their topic, as in "for help
  with python, phone Antelope". It replaces `write_file` or `read_file` in
  the tool list only if the six-tool cap needs it; decide with eval data.
- The call sends the friend the goal, notes, attempt summaries, the last
  errors and any gossip rows about that friend, with a friend-side system
  prompt asking for concrete next steps a
  small model can follow. The reply is stored as a note marked as advice,
  the failure counter resets once, and the goal returns to `active`.
- Limits: escalations per goal (default two), then `stuck` for an operator.
  A friend's upstream error defers the escalation, it does not consume it.
- `usage` table: tokens per persona per attempt, so cost is visible.
- Gossip: `memory` rows of kind `gossip` recording what each friend was
  asked and whether the advice worked.

## Phase 6: planning mode

Two-pass planning from the design doc, each pass its own tick:

1. Review pass: goal, pass criteria and planning notes are written; the
   tick ends.
2. Step pass: a fresh tick reads those and writes the steps, marks the plan
   ready.
3. One step per tick afterwards. Steps are checked off, never deleted, each
   with work notes.
4. When all steps are checked, a verify tick runs the pass criteria. Only
   that closes the plan. A failed verify reopens it and counts as a failure.

Tables `plan` and `plan_step`. Planning tools (`plan_set`, `step_done`)
replace the general goal tools during planning ticks to hold the six-tool
cap. On completion the plan is dumped to a file under the log directory.
A goal becomes a plan when its prompt file asks for one or a friend's advice
says to; the trigger rule is settled at the start of the phase.

## Phase 7: To Do

- `todo` table: title, description.
- `defer_todo(title, description)` tool so the model can park unrelated
  findings instead of chasing them.
- A tick with no goal and no plan step takes one To Do item and runs it as a
  goal. The item is removed when taken; the goal's own record is the audit
  trail. Optional off-hours window in config.
- A To Do item may turn into a plan (phase 6). It is still removed from the
  list when taken; the plan carries it from there.

## Phase 8: operations

- Reporting through the logging function with an rsyslog mode.
- Sudo allowlist (`sudo wc -l`, `sudo cat`, `sudo grep`, `sudo tail`) with a
  matching `/etc/sudoers.d/99-bot` template.
- `<base>/tools/` directory: executables there are approved automatically.
- Install script, multiple bots per host as separate accounts.
- Markdown export and import of memory for hand editing.

## Phase 9: backlog

Personality store, skills documents attached to custom tools, sending
finished plans and repair scripts upstream to share between nodes, chat
notifications (Mattermost or Slack webhook), pipelines in `run_shell`,
drivers other than `openai`, a model running on the node itself, shipping
logs to an ELK stack.

Keys from somewhere other than `keys.json`: environment variables, a secrets
manager, or no key at all on a cloud VM whose role authorises the model
service. The design doc keeps `keys.json` separate from `provider.json` for
this reason. Bedrock's OpenAI-compatible endpoint is untested for tool
calling and must be tested before any plan depends on it.

## Open questions

Each has a default that the plan assumes. An answer changes only the phase
named.

1. **Keyless local provider** (phase 2). Appendix B makes the keys join
   strict; the design doc says a key may be unnecessary. Default: the
   provider entry must exist in `keys.json`, and an empty `key` is allowed
   and sent as a placeholder.
2. **Where `driver` and `url` live** (phase 2). CandidSail keeps them on the
   persona; the design doc adds `provider.json`. Default: `provider.json`
   supplies them and a persona may override either.
3. **Pipes in `run_shell`** (phase 3). Default: rejected in the MVP, so the
   model runs one command per call. `grep`, `awk` and `sort` lose some value
   without pipes; allowing pipelines of allowlisted commands is a contained
   addition if wanted.
4. **`write_file` scope** (phase 3). Default: writes only under the bot's
   base directory; reads anywhere the account can read.
5. **SQLite versus the design doc's "existing MCP module"** (phase 1).
   Default: standard `sqlite3`, following Appendix A.
6. **Failure limit and stuck goals in the MVP** (phase 4). Default: three
   failures, then the goal waits for an operator until phase 5 exists.
7. **Model eval** (phase 3). Needs a LAN model the build sessions cannot
   reach. Default: the script ships and the operator runs it.

## Owner decisions

Answers the owner has given. They are settled; do not ask again.

- Phase 1 as merged in pull request 2 stays. The later commit on the old
  branch that removed it is not wanted.
- The design doc is not copied into the repo. The details the phases need
  are written into this plan instead.
- Live persona, provider and keys files are not added to the repo. It
  carries examples only (phase 2).

- **Cloud build sessions** (2026-11-15). The owner's cloud environment
  supplies live config as the variables `KEY`, `PROVIDER` and `PERSONA`,
  with persona `Raven` (`cohere/north-mini-code:free` on OpenRouter) and a
  disposable pre-paid key. Sessions use them for live checks through
  `scripts/cloud-conf.sh`. The key must not leave that environment.
