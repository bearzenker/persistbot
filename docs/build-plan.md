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
A row marked `proposed` waits for the owner's decision: do not start it,
and do not let it hold up the phases after it.

| Phase | Scope | Branch | Status |
|---|---|---|---|
| 0 | This plan | `claude/persistbot-mvp-plan-5bdmdy` | done |
| 1 | Skeleton: package, config, logging, state store, lock | `claude/persistbot-mvp-plan-5bdmdy` | done |
| 2 | Driver: persona resolution, OpenAI client, error classes | `claude/awesome-maxwell-8j67y6` | done |
| 3 | Tools: allowlisted shell, files, memory, notes | `claude/youthful-pasteur-jy7xzm` | done |
| 4 | Tick loop: goal persistence, recovery, timer units. **MVP, tag `v0.1.0`** | `claude/youthful-pasteur-jy7xzm` | done |
| 4a | Privilege separation: tools run as a second account (`docs/privilege-separation.md`) | `ccr-7e74e664-33adcf` | done |
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

- **State:** phases 0 to 4 are done and on `main`. Phase 4a (privilege
  separation) is finished on `ccr-7e74e664-33adcf` and waits for the owner
  to merge pull request 13. No phase is in progress.
- **Next:** phase 5 (phone a friend), once 4a is on `main`. Read "Core
  model", `docs/persona-pattern.md`, and the "As built" notes of phases 4
  and 4a first. Friend calls use keys and stay in the harness; nothing in
  phase 5 goes through the runner. The stuck state and the failure counter
  it escalates from are in `tick._fail`.
- **Waiting on the owner:** merge of 4a; then one real goal on a VM against
  the LAN model through the systemd timer, with the two accounts and the
  four units, and the tag `v0.1.0`. Build sessions cannot do either: no
  session has run the units under a real systemd. Issue 8 (`find -exec`)
  is backlog by the owner's decision; do not fix it unasked. Issue 7 (keys
  file readable) is closed by 4a on a separated install. Issue 12 (where
  processed prompt files go) is open and not scheduled.
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
| Shell safety | The allowlist is a guardrail against a confused model, not a sandbox. The security boundary is the bot's own unprivileged account, and since phase 4a the harness is outside it, on a second account. No shell is used: the harness reads the command line itself, and every command in a pipeline must be on the list. |
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
  *Superseded by issue 9: see "Pipes and redirection" below.*
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

As built:

- `tools.schemas()` is the request's tool list. `tools.call(ctx, name,
  arguments)` takes the argument text as the model wrote it and always
  returns a `Result(ok, text, done, malformed)`; it never raises.
  `malformed` marks an unknown tool, bad JSON or a missing argument, which
  phase 4 counts toward `invalid`. `tools.parse` does the same check
  without running anything.
- `goal_done` does not touch the store. It returns `done=<summary>`, and
  the tick closes the goal. An empty summary is a tool error and the goal
  stays open.
- `Context` carries the store, the base directory, the goal and attempt
  ids, and the shell timeout (60 seconds by default).
- `run_shell` runs in the base directory with stdout and stderr merged.
  The model is told `exit code N` and the output. Past 4,000 bytes of
  output the command is killed and the result says it was cut off. A
  non-zero exit is `ok=False` but is not malformed.
- The command gets a reduced environment (`PATH`, `HOME`, `USER`,
  `LOGNAME`, `LANG`, `TMPDIR`, `TZ`, and `LC_ALL=C`), so a key or proxy
  setting in the harness's environment does not reach command output.
- No shell means no wildcard or `~` expansion: `ls *.log` looks for a
  file with that literal name.
- `umask` behaves as the plan predicted, by system: where there is no
  such executable the model gets "command not found"; Fedora ships
  `/usr/bin/umask`, which exits 0 and changes nothing.
- `ls*` allows any command whose name starts with `ls` (`lsblk`, `lscpu`,
  `lsof`); `?zip` and `?unzip` allow one leading character (`gzip`,
  `gunzip`).
- `read_file` returns at most 8,000 characters and refuses directories,
  devices and files containing a NUL byte. A relative path is under the
  base directory.
- `write_file` resolves symbolic links before the base-directory check,
  accepts at most 100,000 characters, creates parent directories, and
  refuses `state.db`, its WAL files and `bot.lock`. It writes a temporary
  file and renames it into place, so writing through a hard link (`ln
  state.db alias`) replaces that name only and cannot change the linked
  file. A replaced file keeps its mode. `rm`, `mv` and `cp` through
  `run_shell` can still damage the bot's own state; that is the guardrail's
  limit.
- `read_file` and `cat` can read `conf/keys.json`, because reads go
  anywhere the account can (open question 4) and the bot's account owns
  that file. The content would then be sent to the model provider. Not
  changed here; a deny rule for the keys file is a small addition if the
  owner wants it.
- `remember` collapses whitespace, caps a fact at 300 characters and skips
  an exact duplicate. `note` caps a note at 1,000 characters.
- `memory.prompt_block(db)` is the text for the system prompt: kind
  `memory` only, newest first, 1,500 characters by default including the
  heading and bullets.
- The six schemas are about 1,800 characters of JSON, roughly 450 tokens.
- `evals/run.py [conf.yaml] [-p PERSONA ...] [-v] [--delay S]` sends each
  task in `evals/tasks.json` (21 tasks) as one request and scores the first
  tool call as `acted`, `silent`, `wrong` or `invalid`. No tool is
  executed. It carries a stand-in system prompt until phase 4 ships
  `system.txt`; switch it to that file then.
- One live run, 2026-11-15, persona `Raven` (`cohere/north-mini-code:free`
  on OpenRouter): acted on 18 of 21, never silent, no invalid calls. That
  model is not one of the 3B candidates; the comparison Appendix A asks
  for is still the operator's to run on the LAN.
- In that run the model reached for pipes and redirection unprompted
  (`ps aux | grep nginx`, `find ... 2>/dev/null`) and for commands off the
  list (`df`, `netstat`). That led to issue 9.

Pipes and redirection (issue 9, after phase 3):

- `allowlist.parse(command)` replaces `check`. It reads the command line
  itself, with shell quoting rules, into a `Pipeline` of `Command`s. There
  is still no shell.
- `a | b | c` is allowed, up to six commands, and every one must be on the
  list. `ls | python3` is refused.
- Redirection is a fixed set: `2>/dev/null` and `2>&1` on any command;
  `>/dev/null`, `> file` and `>> file` on the last. The file follows the
  `write_file` rules (under the base directory, not harness state) and
  may not be a hard link or a FIFO. At most 1,000,000 bytes go to the
  file in one call; past that the pipeline is killed.
- Refused, each with its own message: `;`, `&&`, `||`, `&`, `<`,
  backticks, `$(...)`, and `$NAME`, `${NAME}`, `$1`, `$?` outside single
  quotes. They are refused rather than passed as text because without a
  shell they would silently do something other than what was meant.
- Inside single quotes everything is text, so `awk '{print $1}'` and
  `grep 'a|b'` work. A `$` that does not start a variable (`grep "end$"`)
  is text too.
- Line breaks and control characters are refused anywhere, quoted or not.
- The model is shown the last command's output and every command's
  errors. The exit code is the last command's. The time and output limits
  cover the whole pipeline; all its processes are killed together.
- Not done, on purpose: variables, wildcards, `cd`, command sequences.
  Each call still starts in the base directory.

Allowlist additions (owner's standing permission, 2026-11-16):

- `cut`: named by the owner.
- `df`, `du`, `wc`: refused in the live runs of 2026-11-16.
- `netstat`: refused in the live run of 2026-11-15.
- Seen refused and not added, because it runs other commands: `xargs`.
- Since phase 4a "the base directory" in the notes above means the
  model's working directory.

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

As built:

- `tick.run(config, model, prompts)` is the whole wake-up; `cli.main`
  resolves the persona and calls it. With `model=None` (the persona did
  not resolve) it still takes the lock, closes crashed attempts and queues
  input, then returns 2.
- One goal per tick, one attempt per tick.
- `<venv>/system.txt` is required. If it cannot be read the tick exits 2
  before the goal is touched. `<base>/system.txt` is optional. The system
  prompt is those two, the identity line, then the memory block. The tool
  list travels as the request's `tools`, not as prompt text.
- The user message is `Goal: ...`, then the goal's notes (newest 2,000
  characters, shown oldest first), then a summary of the last three
  earlier attempts: outcome, detail, and the last four tool calls with
  arguments and results cut to 160 characters. A journalled call with no
  result is shown as `STARTED, OUTCOME UNKNOWN`.
- A reply with no tool call gets one fixed user message asking for a tool
  call. Three unusable replies in an attempt (no tool call, or a malformed
  one) end it as `invalid`. The plan said "a small limit"; it is three.
- When one reply holds several tool calls they run in order, at most
  four. Calls past the fourth, and any call after `goal_done`, are
  answered with an error and not run or journalled. If one of them is
  `goal_done` the attempt ends after that reply.
- A crashed attempt is closed by the next tick, before input is ingested,
  and counts as a failure then. A third failure found this way makes the
  goal `stuck` without a model call.
- Deferral: `Retry-After` when the server sent it, else 300 seconds
  doubling per consecutive deferral to a 3,600 second cap. A fatal
  upstream error (401, 404 and the like) waits 3,600 seconds and writes a
  report line. Any attempt that is not a deferral resets the deferral
  count. Failures are never reset in the MVP.
- An attempt's end and its effect on the goal (done, failure count,
  stuck, `not_before`) are one transaction, `Store.close_attempt`. A tick
  killed at that point leaves either both or neither, and "neither" is an
  open attempt the next tick closes as `crashed`.
- Report lines: goal done, each counted failure with its number, goal
  stuck, and a fatal upstream deferral. An ordinary deferral goes to the
  action log only.
- `incoming/`: regular files, sorted by name, dot-files skipped. Each is
  queued, then moved to `incoming/done/`; a name already there gets a
  timestamp suffix. An empty file is moved without becoming a goal.
- A blank prompt on the command line is ignored.
- `skills` entries must be plain names (letters, digits, `_`, `-`). They
  are not required to exist and are not used yet.
- Exit 1 means "attempt failed, goal still open", including the tick in
  which a goal becomes stuck.
- `deploy/persistbot@.service` treats exit 75 and 1 as success, so only a
  configuration error (2) marks the unit failed. The timer fires every
  five minutes. The units pass `systemd-analyze verify`; they have not
  been run under systemd by a build session.
- Version is 0.1.0. The tag is the owner's, after the VM run.
- One live goal, 2026-11-16, persona `Raven` on OpenRouter, through
  `bot.py`: `free`, `remember`, `write_file`, `read_file`, `goal_done` in
  one attempt, exit 0.

## Phase 4a: privilege separation

Read `docs/privilege-separation.md` first. It holds the evaluation, the
design and the reasons; this section is the build list.

The fault: the harness and the model's tool calls ran as one account, and
the harness kept its state in the model's working directory, so a tool
call could rewrite the allowlist, the bot's YAML, the instance prompt, the
inbox, the logs and the database, and read the keys.

Deliverables:

- `runner.py`: a standalone process, standard library only, that runs an
  already-parsed pipeline, reads a file and writes a file. It runs as the
  bot's Linux account. The pipeline and file code moves there from
  `tools/shell.py` and `tools/files.py`.
- `link.py`: the harness's side. One JSON line per request and per reply,
  a deadline and a size cap on every reply. Two transports: a child
  process on the harness's own account (development, tests, cloud
  sessions), and a Unix socket to a systemd socket-activated unit.
- The allowlist stays in the harness. The runner is sent argv lists.
- Config keys `user`, `work` and `runner`. The model's working directory
  is separate from the bot's base directory in every mode.
- With `user` set: the harness checks the kernel's record of which account
  the runner's replies come from, and asks the runner whether it can reach
  the code, config, keys or state. A wrong answer to either means no tool
  runs. No fallback to the harness account.
- `deploy/`: the harness unit hardened; `persistbot-runner@.socket` and
  `persistbot-runner@.service`.
- `status` reports whether the bot is separated.
- README install rewritten for two accounts.

Acceptance:

- Every existing tool and tick test passes through the runner.
- No tool can name the instance prompt, the inbox, the logs or the
  database: each is a test.
- Wrong runner account, root, or the harness's own account: exit 2, goal
  untouched.
- The runner reports it can write a protected path or read the keys:
  exit 2, report line.
- Runner killed during a call: tool error, the next call reconnects.
- Runner unreachable at the start of a tick: exit 2, goal untouched.
  Unreachable in the middle of an attempt: `deferred`, failure count
  unchanged.
- A reply over the size cap or past the deadline is a tool error.

Done when those pass and the owner has run one goal on the VM with two
accounts.

As built:

- Two directories per bot. `base` is the harness's (state, lock, logs,
  incoming, instance prompt). `work` is the model's: `<base>/work` without
  `user`, and `bot-<user>` beside the install directory with it
  (`/opt/bot-raven`). With `user` set, `work` may not be, hold or lie
  inside `base` or the install directory: that is a configuration error.
  The separation check exempts nothing, so a working directory that
  reaches into a protected tree some other way (a symbolic link, a
  directory on Python's path) is refused at the start of the tick. The `PROTECTED` list of file names is gone: the files it
  guarded are no longer where a tool can name them.
- `user` must look like a Linux account name. `runner` is the socket path,
  `/run/persistbot/<user>.sock` unless set, and is an error without `user`.
- `runner.py` holds the pipeline, read and write code unchanged in
  behaviour. It is one file with no imports from the package, so it can be
  copied and run alone. Started by socket activation (`LISTEN_FDS`) it
  serves one connection and exits; otherwise it serves standard input and
  output.
- While a pipeline runs, the runner watches its caller's connection. If
  the harness goes away, the commands are killed and the runner exits.
- `link.py`: `Link.for_config(config)`, `open()`, `call(request, wait)`,
  `close()`. Errors: `Lost` (this call: the runner went away, answered
  late, answered nonsense, or passed 1 MB; the tool returns an error and
  the next call reconnects) and `RunnerError` with `Unavailable` and
  `Refused` (no tool can run).
- The identity check is not `SO_PEERCRED`. Under socket activation the
  listening socket was made by systemd, so `SO_PEERCRED` names root. The
  link sets `SO_PASSCRED` and reads the sender's credentials that the
  kernel attaches to each piece of data received (`SCM_CREDENTIALS`).
  Every reply is checked, not only the first. The uid the runner reports
  in its hello must match as well.
- The separation check runs in the runner at hello. Not writable: the
  install directory, the conf directory, the bot's YAML, the base, log and
  incoming directories, the instance prompt file wherever `system` puts
  it, and every directory on the harness's `sys.path`,
  each walked to the bottom, up to 250,000 entries. Not readable or
  enterable: `keys.json`, the base directory, `state.db`, the log and
  incoming directories. The first five offenders are named. A symbolic
  link is judged by where it leads: its target is checked, walked if it
  is a directory, and reported if the account could create a target that
  is missing. That includes a listed path that is itself a link, such as
  an install directory pointing at a versioned one. Running out of entries before the end is a
  refusal like any other: what was not looked at cannot be vouched for. A
  real install is about 20,000 entries.
- "Can write" means the kernel says so or the mode bits say so. The
  runner unit mounts the file system read-only, which would otherwise
  hide a mode that has drifted. A directory above a protected path that
  the account could rename entries in (writable, and not protected by the
  sticky bit) is reported too.
- The check is answered by a process on the bot's own account. It can be
  believed because that process is root-owned code that systemd has just
  started, before any command of the tick has run, and systemd stopped
  whatever the previous tick left behind. It is there to catch an install
  that has drifted, not to out-argue an intruder already on the account.
- At the start of a tick the link is opened after the prompt files are
  read and before the attempt row is written. A `RunnerError` there is
  exit 2, a report line, and a goal left exactly as it was. A
  `RunnerError` during an attempt ends it `deferred` (one hour, a report
  line with its own hint), and the journal row says the call was not run.
- `tools.call` lets `RunnerError` through to the tick. Everything else is
  still a `Result`. What the runner returns is clipped again in the
  harness: the runner shares an account with the model's commands and is
  not trusted to keep its own limits.
- `status` prints one line about the tools. With `user` set it connects,
  runs both checks and prints `separated as raven (uid N), working
  directory ..., checked`, or `tools cannot run:` and the reason. Without
  `user` it prints `not separated` and starts nothing. Each tick writes
  the same line to the action log, as a warning when not separated.
- Units: `persistbot@.service` now wants the runner socket of the same
  instance name, and is read-only everywhere but `/opt/persistbot/%i`.
  `persistbot-runner@.socket` (mode 0600, owner `persistbot`) and
  `persistbot-runner@.service` (`User=%i`) are new. One instance name
  serves as config file name and account name. The harness waits 20
  seconds longer for a command than the command is allowed to run, so the
  worst-case tick grew from 108 to 124 minutes; `TimeoutStartSec` is now
  4 hours and the runner's `RuntimeMaxSec` 5. All four pass
  `systemd-analyze verify`. They have not been run under systemd.
- `conf/init.yaml` leaves `user` commented out, so the shipped file and
  `conf/cloud.yaml` run on one account. The README tells an installer to
  set it.
- Checked by hand, 2026-11-16, in a cloud session: accounts `persistbot`
  and `raven`, the README layout under `/opt`, and a small stand-in for
  the socket unit. A scripted model tried the seven attacks that started
  this phase; each got `Permission denied` or the working-directory rule,
  and `find -exec id` ran as `raven`. Then, each in turn: a code file made
  writable, the keys made readable, the base directory made enterable, the
  runner started as `persistbot` and as root, nothing listening, the
  working directory missing. Each stopped the tick with the reason, and a
  queued goal stayed queued.
- One live goal the same day, persona `Raven` on OpenRouter, separated:
  the model's commands ran as `raven`. It did not finish in 12 turns
  (`budget`); that is the model, not the separation.
- Not built: the `sudo -u` transport, an install script, network limits on
  the runner unit.

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
  matching `/etc/sudoers.d/99-bot` template. The rule names the bot's
  account, never `persistbot`. `persistbot-runner@.service` sets
  `NoNewPrivileges=yes`, under which sudo cannot work; this phase lifts it
  on that unit only, and `ProtectSystem=strict` there needs the same look.
- `<base>/tools/` directory: executables there are approved automatically.
  It must not be writable by the bot's account, or the model approves its
  own tools. Since phase 4a `<base>` is closed to that account, so decide
  here how the runner is to read and run them.
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
3. **Pipes in `run_shell`** (phase 3). *Answered 2026-11-15, issue 9:*
   pipelines are allowed, with every command checked against the
   allowlist, and a fixed set of redirections. No shell is used. See
   "Pipes and redirection" under phase 3.
4. **`write_file` scope** (phase 3). Default: writes only under the bot's
   working directory (the base directory before phase 4a); reads anywhere
   the bot's account can read.
5. **SQLite versus the design doc's "existing MCP module"** (phase 1).
   Default: standard `sqlite3`, following Appendix A.
6. **Failure limit and stuck goals in the MVP** (phase 4). Default: three
   failures, then the goal waits for an operator until phase 5 exists.
7. **Model eval** (phase 3). Needs a LAN model the build sessions cannot
   reach. Default: the script ships and the operator runs it.
8. **Privilege separation** (phase 4a). Raised by the owner 2026-11-16:
   the bot can change its own configuration and code. *Answered
   2026-11-16:* build it now as phase 4a. See "Owner decisions".

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
- **Pipes** (2026-11-15, issue 9). The owner chose pipelines built by the
  harness, each command checked against the allowlist, over running
  commands through `bash -c`.
- **Backlog by decision** (2026-11-15). Issue 7 (`read_file` and `cat` can
  read `conf/keys.json`) and issue 8 (`find -exec` and similar options run
  commands that are not on the list) are accepted development risk for
  now. They stay open as issues and are not to be fixed unasked.
- **Privilege separation** (2026-11-16, phase 4a). Approved for build
  before phase 5:
  - Mechanism: the runner process behind a systemd socket unit, not
    `sudo -u`.
  - Accounts: the harness runs as `persistbot`, the tools as the bot's own
    account (`raven`). Creating the bot's account is the sysadmin's job,
    not the harness's or an install script's.
  - Layout: the application stays in one directory, `/opt/persistbot`,
    including `conf/` and each bot's base directory
    (`/opt/persistbot/raven`: queue, logs, state). The bot's account has
    no access to its base directory. Its writable working directory is
    `/opt/bot-<account>` (`/opt/bot-raven`).
  - A bot may not queue work for itself: `incoming/` is closed to it.
  - The owner accepts that the phase 8 sudo allowlist may not work under
    the runner unit as shipped (`NoNewPrivileges=yes`). Phase 8 revisits
    the unit.
  - Network limits on tool commands: not asked for; unchanged.
- **Allowlist additions** (2026-11-16). When a build session sees the
  model fail on a basic, passive Linux command that is not on the list
  (`cut` was the example), it may add the command without asking. Record
  each addition under phase 3 "As built".
