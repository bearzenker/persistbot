# Privilege separation: keeping the bot out of its own code and config

Status: **proposal, waiting for the owner's decision.** Nothing here is
built. If accepted it becomes phase 4a in `docs/build-plan.md`.

## The problem as it stands in the tree

The harness and the model's tool calls run as one Linux account, and that
account owns `/opt/persistbot`. The plan calls the account "the security
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
  be reached at all is an operator problem: the attempt ends `deferred`
  with a report line, like a bad key. It never counts toward the failure
  limit.
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

Paths are configuration, so the code does not care, but the recommended
install is:

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/persistbot/` | `root:root` 0755 | code, `.venv`, common `system.txt` |
| `/etc/persistbot/` | `root:persistbot` 0750 | `<bot>.yaml`, `persona*.json`, `provider.json` |
| `/etc/persistbot/keys.json` | `root:persistbot` 0640 | keys |
| `/var/lib/persistbot/<bot>/` | `persistbot:persistbot` 0700 | `state.db`, `bot.lock`, `logs/`, `incoming/`, instance `system.txt` |
| `/srv/persistbot/<bot>/` | `<bot>:<bot>` 0750 | the model's working directory |

- The harness can read its code and config and cannot write them either.
- `incoming/` gets a group for the scripts that submit work
  (`persistbot-submit`, mode 0730). Bot accounts are not in it.
- What `raven` may read on the node is decided the ordinary way, by group
  membership (`adm`, `systemd-journal`), and in phase 8 by sudo.

Config gains three keys, all optional:

```yaml
bot:
  user: raven                          # account the tools must run as
  work: /srv/persistbot/raven          # default: <base>/work
  runner: /run/persistbot/raven.sock   # default: local child process
```

`work` defaults to `<base>/work` in every mode, so the model's files and
the harness's state are never in one directory, even on a developer's
machine. The `PROTECTED` list in `tools/files.py` then has nothing left to
guard and goes.

### Fail closed, and prove it every tick

- `user` set: the harness reads the kernel's record of who is on the other
  end of the socket (`SO_PEERCRED`). If that is not the uid of `user`, or
  is 0, or is the harness's own uid, no tool runs and the tick exits 2
  before the goal is touched. There is no fallback to the harness account.
- On connect the harness sends the runner the paths it must not reach
  (code, config, keys, state directory). The runner answers, as `raven`,
  whether it can write each and whether it can read the keys. Any "yes"
  is exit 2 with a report line naming the path. A later `chmod -R 777`
  cannot go unnoticed.
- `user` unset: tools run on the harness account, as now. `status` and
  the action log say `not separated` in so many words.

### Unit sketch

To be checked with `systemd-analyze verify` and on the owner's VM.

```ini
# persistbot@.service  (harness; changes only)
User=persistbot
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/persistbot/%i
ProtectHome=yes
PrivateTmp=yes

# persistbot-runner@.socket
[Socket]
ListenStream=/run/persistbot/%i.sock
SocketUser=persistbot
SocketMode=0600

# persistbot-runner@.service
[Service]
User=%i
ExecStart=/opt/persistbot/.venv/bin/python -I -m persistbot.runner
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/srv/persistbot/%i
InaccessiblePaths=/etc/persistbot /var/lib/persistbot
MemoryMax=256M
TasksMax=64
```

The mount rules repeat what ownership already enforces. That is on
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

## What can be done today, with no code

On any install before this is built, as root:

1. Make `/opt/persistbot` and everything in it `root:root`, except the
   bot's base directory, which stays with `persistbot`. Make
   `conf/keys.json` `root:persistbot` 0640.
2. In `persistbot@.service` set `ProtectSystem=strict` and
   `ReadWritePaths=` to the bot's base directory.

That stops the first two rows of the table at the top. It does not stop
the other five. It is worth doing before the first VM run, and it remains
in place underneath the full design.

## Phase 4a, if accepted

Deliverables:

- `runner.py`: protocol, pipeline execution, file read and write, moved
  from `tools/shell.py` and `tools/files.py`.
- `tools/shell.py`, `tools/files.py`: parse and check in the harness,
  send to the runner. `Context` carries the runner connection.
- Two transports: local child process, Unix socket with the peer check.
- `config.py`: `user`, `work`, `runner`.
- The start-up separation check, and `status` output for it.
- `deploy/`: the harness unit hardened, the two runner units.
- `README.md`: install rewritten for the layout above; "Known limits"
  updated. `CLAUDE.md` and the plan's "Shell safety" row updated.

Acceptance, all with the local transport and no root:

- Every existing tool test passes through the runner.
- The four in-base rows of the table at the top (instance prompt, inbox,
  report log, database) are each a test: with state outside the work
  directory, no tool can name those files.
- The other three rows depend on the second account, which no test can
  create without root. They are covered by the start-up check tests below
  and by the VM run.
- Peer uid wrong, 0, or equal to the harness: exit 2, goal untouched.
- Runner reports it can write a protected path: exit 2, report line.
- Runner killed during a call: tool error, next call reconnects.
- Runner unreachable: attempt `deferred`, failure count unchanged.
- A reply over the size cap or past the deadline is a tool error.

Done when those pass and the owner has run one goal on the VM with two
accounts and seen `status` report `separated as raven`.

Out of scope: the install script (phase 8), transport B, Landlock,
network limits on the runner unit, migrating an existing base directory
(there is no deployed bot yet).

## Effect on the rest of the plan

- **Order.** Before phase 5, and before the VM run and the `v0.1.0` tag.
  The layout changes, and it is free to change now and awkward once a bot
  is deployed. The owner then validates one layout, once.
- **Issue 7** is closed by this. **Issue 8** stays open but drops to a
  tidiness matter.
- **Phase 5.** Friend calls use keys and stay in the harness. No change.
- **Phase 8.** The sudo allowlist is granted to the bot account, never to
  `persistbot`. The shipped unit sets `NoNewPrivileges=yes`, under which
  sudo cannot work at all; with two units that setting is lifted on the
  runner only, and the harness keeps it. `<base>/tools/` must be owned by
  the operator, or automatic approval lets the model approve itself.
- **CLAUDE.md** "nothing may require root before phase 8" still holds at
  run time. Installing the accounts and units needs root once.

## Decisions for the owner

Each has the default this proposal assumes.

1. **Build it, and when.** Default: yes, as phase 4a, before phase 5 and
   the tag.
2. **Mechanism.** Default: A, the systemd socket unit. B (`sudo -u`) is
   closer to "shells out as raven" and simpler to picture, at the cost of
   `NoNewPrivileges` on the harness and per-distribution PAM checking.
3. **Install layout.** Default: `/etc`, `/var/lib` and `/srv` as above.
   Keeping everything under `/opt/persistbot` with the same owners and
   modes works equally well.
4. **May a bot queue work for itself?** Default: no. `incoming/` is closed
   to bot accounts; phase 7's `defer_todo` is the intended route.
5. **Network for tool commands.** Default: unchanged. The runner unit can
   later deny outbound traffic if the owner wants it.
