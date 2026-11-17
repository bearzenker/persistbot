# Privilege separation: keeping the bot out of its own code and config

Status: **built as phase 4a.** The owner accepted this on 2026-11-16. What
was built, and where it differs from the proposal, is under "Phase 4a",
"As built", in `docs/build-plan.md`. This document keeps the evaluation
and the reasons.

## The problem as it stood before phase 4a

The harness and the model's tool calls ran as one Linux account, and that
account owned `/opt/persistbot`. The plan calls the account "the security
boundary", but the harness sits inside that boundary together with the
model. Reproduced on 2026-11-16 against a throwaway install, using only the
six tools and allowlisted commands:

| What the model did | Tool | Result |
|---|---|---|
| Replaced `src/.../allowlist.py` | `write_file` then `run_shell cp` | allowed |
| Replaced its own `conf/<bot>.yaml` | `write_file` then `run_shell cp` | allowed |
| Read `conf/keys.json` (issue 7) | `run_shell cat` | allowed |
| Wrote `<base>/system.txt`, its own instance prompt | `write_file` | allowed |
| Dropped a file in `<base>/incoming/`, a goal it gave itself | `write_file` | allowed |
| Emptied `logs/report.log` | `write_file` | allowed |
| Deleted `state.db-wal` | `run_shell rm` | allowed |

`write_file` refuses a path outside the base directory, but `cp`, `mv`,
`rm`, `ln`, `tar` and `unzip` do not go through that check, and the last
four rows need no shell at all because the harness keeps its own files in
the directory it gives the model to work in.

So there are two separate faults:

1. **One account.** Anything the harness can write, a tool call can write.
2. **One directory.** State, logs, the instance prompt and the inbox live in
   the model's working directory.

## What has to be protected

| Asset | Why it matters | Needed |
|---|---|---|
| Code, `.venv` | The allowlist and every limit are code. A `.pth` file in the venv is code too. | no write |
| `conf/*.yaml`, `persona*.json`, `provider.json` | A changed provider URL sends the key to another host. | no write |
| `conf/keys.json` | Issue 7. | no read |
| `system.txt` (both) | It is the model's instructions. | no write |
| `state.db`, `bot.lock` | Goals, failure counts, memory. "Never lose a goal" depends on it. | no access |
| `logs/` | The audit trail of what the model did. | no access |
| `incoming/` | Writing here creates a goal. A stuck goal could requeue itself forever. | no write |
| Phase 8 `<base>/tools/` | Executables there are approved automatically. | no write |

The model keeps one writable place, its working directory, and two
intended channels into state: `remember` and `note`, which the harness
writes for it, capped.

## Evaluation of the two-account proposal

The proposal: the harness runs as `persistbot`; each bot's actions run as
its own account (`raven`), named by a `user` directive in the bot's YAML.

**Verdict: it is the right foundation and should be built.** File
ownership is enforced by the kernel, not by string checks in Python, and
nothing else on the list of alternatives below is as cheap or as widely
available. It also closes issue 7, takes the sting out of issue 8
(`find -exec` then runs as an account that cannot reach anything that
matters), isolates bots from each other, and is where phase 8 was already
heading ("multiple bots per host as separate accounts").

As stated it leaves five gaps. Each has a fix, and the fixes are what turn
the idea into a design:

1. **Only the shell would change account.** `read_file` and `write_file`
   run inside the harness process. `read_file` would still read the keys
   as `persistbot`. `write_file` would become a confused deputy: it checks
   the path, then writes as `persistbot` into a directory `raven` owns, and
   `raven` can swap a directory for a symlink between the check and the
   write. *All three file-touching tools must run as the bot's account.*
2. **An unprivileged process cannot change account.** `persistbot` needs
   something root-owned to let it become `raven`. The choice of mechanism
   is the main design decision; see "How the runner gets its account".
3. **The YAML must not be the authority.** If `user: raven` alone decided
   the account, editing the YAML would be a way to pick any account. The
   authority has to be something only root can change. The YAML line then
   states what is expected, and the harness checks it.
4. **The directory has to be split** as well as the account. Otherwise the
   harness's state is still in the directory `raven` must be able to write.
5. **The harness cannot signal another account's processes.** `_kill` in
   `tools/shell.py` already swallows `PermissionError`, so timeouts and
   output caps would stop working without any error. The kill logic has to
   live on the bot-account side.

## Recommended design

### A runner process

Add one small module, `persistbot/runner.py`, that runs as the bot's
account and does only this: execute an already-parsed pipeline, read a
file, write a file. The existing code in `tools/shell.py` and
`tools/files.py` moves there largely unchanged.

```
persistbot (harness)                     raven (runner)
--------------------                     --------------
model calls, keys, state.db, logs        run a pipeline (argv lists)
allowlist.parse()  ── request ──────►    read a file
remember / note / goal_done              write a file under the work dir
journal, limits    ◄── result ───────    timeouts, output caps, kills
```

- The allowlist stays in the harness. The runner receives argv lists and a
  redirect target, never a command line, so policy is on the trusted side.
- The protocol is one JSON line per request and one per reply, standard
  library only. The harness enforces a deadline and a size cap on every
  reply and treats the content as untrusted: the model can `kill` its own
  runner, since they share an account.
- A runner that goes away during a call makes that call a tool error
  ("outcome unknown"), and the next call reconnects. A runner that cannot
  be reached at all is an operator problem, and never counts toward the
  failure limit. At the start of a tick, before the attempt is recorded,
  it is exit 2 with a report line and the goal untouched. In the middle of
  an attempt, the attempt ends `deferred` with a report line, like a bad
  key.
- The runner imports nothing but the standard library and is started with
  `python -I`.

### How the runner gets its account

| | A. systemd socket unit | B. `sudo -u raven` | C. `CAP_SETUID` on the harness |
|---|---|---|---|
| Root-owned authority | `User=` in the runner unit | one sudoers line | none; the harness can become any account, root included |
| Setuid program in the path | no | yes | no |
| `NoNewPrivileges=yes` on the harness | kept | must be removed | kept |
| Harness and tools sandboxed differently | yes, two units | no, one unit | no |
| Works without systemd | no | yes | yes |
| Known risk | two more unit files | PAM session handling differs by distribution and can move the command out of the service's control group | a harness bug is a root bug |

**Recommendation: A, with B as the documented alternative for a node
without systemd.** C is rejected.

With A, `persistbot-runner@raven.socket` listens on
`/run/persistbot/raven.sock`, mode 0600, owned by `persistbot`. The first
tool call of a tick connects; systemd starts `persistbot-runner@raven.service`
as `raven`; the runner serves that one connection and exits. systemd then
removes anything the tick's commands left running, which the current
process-group kill cannot guarantee. Root is needed once, to install the
units and create the accounts, as it is today for the timer.

The runner core takes a pair of streams, so the transport is about twenty
lines each way. This phase builds two transports: the socket, and a local
child process on the harness's own account for development, tests and
cloud sessions. B is not built unless the owner asks.

### Layout

The owner chose to keep the application in one directory:

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/persistbot/` | `root:root` 0755 | code, `.venv`, common `system.txt` |
| `/opt/persistbot/conf/` | `root:persistbot` 0750, files 0640 | `<bot>.yaml`, `persona*.json`, `provider.json`, `keys.json` |
| `/opt/persistbot/<bot>/` | `persistbot:persistbot` 0700 | the base: `state.db`, `bot.lock`, `logs/`, `incoming/`, instance `system.txt` |
| `/opt/bot-<account>/` | `<account>:<account>` 0750 | the model's working directory |

- The harness can read its code and config and cannot write them either.
- The bot's account cannot enter the base directory at all, so it cannot
  read its own queue, logs or state, and cannot queue work for itself.
  Scripts that submit work run as `persistbot` or root.
- What the bot's account may read on the node is decided the ordinary
  way, by group membership (`adm`, `systemd-journal`), and in phase 8 by
  sudo.
- Creating the bot's account is the system administrator's job.

Config gains three keys, all optional:

```yaml
bot:
  user: raven                          # account the tools must run as
  work: /opt/bot-raven                 # default with `user`; <base>/work without
  runner: /run/persistbot/raven.sock   # default with `user`
```

Without `user` the runner is a child process on the harness's own account
and `work` is `<base>/work`, so the model's files and the harness's state
are never in one directory, even on a developer's machine.

### Fail closed, and prove it every tick

- `user` set: the harness asks the kernel which account each reply was
  sent by. If that is not the uid of `user`, no tool runs and the tick
  exits 2 before the goal is touched. `user` may be neither root nor the
  harness's own account. There is no fallback to the harness account.

  The proposal said `SO_PEERCRED`. That does not work here: it reports who
  created the listening socket, and under socket activation that is
  systemd, as root. The harness instead sets `SO_PASSCRED` and reads the
  sender's credentials that the kernel attaches to the data it receives.
  An unprivileged sender cannot forge them.
- On connect the harness sends the runner the paths it must not reach.
  The runner answers, as the bot's account, whether it can write any of
  the code, configuration or state, and whether it can read the keys or
  enter the base directory. Any "yes" is exit 2 with a report line naming
  the path. A later `chmod -R 777` cannot go unnoticed.
- `user` unset: tools run on the harness account. `status` and the action
  log say `not separated` in so many words.

### Units

The units are in `deploy/`: `persistbot@.service` (harness),
`persistbot-runner@.socket` and `persistbot-runner@.service` (runner).
The runner unit's `User=` is the root-owned authority for the account;
`user:` in the YAML is what the harness expects to find.

The mount rules in both units (`ProtectSystem=strict`, `ReadWritePaths`,
`InaccessiblePaths`) repeat what ownership already enforces. That is on
purpose: two independent mechanisms, so one mistake does not open the
door.

## Alternatives considered

- **Same account, root-owned code, read-only mounts.** Protects code and
  config from writes with no code change, and is the stopgap below. It
  cannot protect the keys, the state or the logs, because the harness must
  reach those and shares the account. Kept as the second layer.
- **Containers or `bwrap` per command.** The bot's job is to inspect the
  real node; a container hides it. Another dependency on small machines.
- **Landlock.** Unprivileged and well suited, but there is no standard
  library binding and vendor kernels on single-board computers often lack
  it. Worth a look later as a third layer, not as the base.
- **SELinux or AppArmor profile.** Strong, but a policy per distribution.
- **Checksums or signatures on the code at start-up.** Detects, does not
  prevent, and tampered code can skip the check.
- **More deny rules in the allowlist.** The plan already says the list is
  a guardrail. Every rule has a way round it (issue 8).

## An install that is not separated

A bot with no `user` in its config still runs on one account. Root-owned
code and a read-only mount protect its code and configuration from
writes, and nothing else:

1. Make `/opt/persistbot` and everything in it `root:root`, except the
   bot's base directory. Make `conf/keys.json` `root:persistbot` 0640.
2. Keep `ProtectSystem=strict` and `ReadWritePaths=` as shipped in
   `persistbot@.service`.

The keys, the state, the logs, the inbox and the instance prompt stay
within the model's reach. Set `user`.

## Effect on the rest of the plan

- **Issue 7** is closed by this on a separated install. **Issue 8** stays
  open but drops to a tidiness matter: `find -exec` runs as the bot's
  account.
- **Phase 5.** Friend calls use keys and stay in the harness. No change.
- **Phase 8.** The sudo allowlist is granted to the bot account, never to
  `persistbot`. The runner unit sets `NoNewPrivileges=yes`, under which
  sudo cannot work at all; phase 8 lifts it on that unit only, and the
  harness keeps it. `<base>/tools/` must not be writable by the bot's
  account, or automatic approval lets the model approve itself.
- **CLAUDE.md** "nothing may require root before phase 8" still holds at
  run time. Creating the accounts and installing the units needs root
  once, and is the system administrator's work.

## The owner's decisions

Answered 2026-11-16; recorded in full under "Owner decisions" in the plan.

1. **Build it, and when.** Yes, as phase 4a, before phase 5.
2. **Mechanism.** A, the systemd socket unit.
3. **Install layout.** Everything under `/opt/persistbot`; the working
   directory is `/opt/bot-<account>`. Not the `/etc`, `/var/lib`, `/srv`
   split this proposal defaulted to.
4. **May a bot queue work for itself?** No. It has no access to its queue,
   logs or state.
5. **Network for tool commands.** Not asked for; unchanged.
