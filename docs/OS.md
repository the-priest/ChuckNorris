# Chuck OS — an operating system for AI agents

In the mainframe era you rented slices of a CPU by the second. Today we rent
slices of a model by the token. Tokens are the new cycles, the context window
is the new RAM, tools are the new syscalls, and an agent is the new process.
Every agent product so far is a single program running on bare metal: one
loop, one context, one budget, no isolation, no scheduler. Chuck OS is the
layer underneath that they are all missing.

This document is the design. `docs/ROADMAP.md` (phases 16–21) tracks what is
built.

## The mapping

| Computer        | Chuck OS                                                        |
| --------------- | --------------------------------------------------------------- |
| CPU time        | model tokens (and dollars), metered per process                 |
| process         | an agent: a conversation, a program, a quota, a state           |
| scheduler       | grants model-call slots by priority, fairly, within quotas      |
| RAM             | the context window                                              |
| virtual memory  | paging: old tool output swapped to disk, paged back on demand   |
| syscalls        | tools (`read`, `edit`, `bash`, `search`, MCP, …)                |
| permissions     | capabilities: what a process may read, write, run, reach, spend |
| fork / exec     | spawn a child agent with a brief, a program and a slice of quota |
| copy-on-write   | each writing agent works in its own worktree; join merges it    |
| signals         | STOP, CONT, TERM (wrap up and report), KILL, INFO (what now?)   |
| IPC             | mailboxes between processes; a shared blackboard of facts       |
| init / systemd  | daemons that wake on a schedule or an event                     |
| packages        | agent programs: prompt + capabilities + model + quota           |
| `top`, `ps`     | `/top`: every agent, its state, tokens/s, spend, what it does   |
| `strace`        | every syscall a process made, with latency and token cost       |
| hibernate       | every process checkpoints and survives a crash or reboot        |
| CPU governor    | routes each call to the cheapest model that can do it           |

## Principles

1. **Account for every token.** A token spent is charged to the process that
   spent it, and to its parents. Budgets nest: a child can never spend more
   than its parent allotted it.
2. **Nothing runs unbounded.** Every process has a token, money and wall-clock
   quota; the kernel stops it at the limit and asks it to report (TERM),
   then kills it if it won't (KILL).
3. **Isolation by default.** A child gets a subset of its parent's
   capabilities. Writers never share a working tree: copy-on-write, then an
   explicit, checked merge.
4. **Lossless before lossy.** When context fills up, page out (exact text,
   recoverable) before summarising (lossy).
5. **Everything observable, everything replayable.** The journal records
   every model exchange and syscall, so a run can be inspected, replayed and
   forked from any step.
6. **The UI is a window onto the kernel**, not the other way round: the
   kernel has no GTK in it and is tested on its own.

## The kernel (phase 16)

`chucknorris_ext/kernel.py`, pure Python, no GTK, no network.

- **Process**: pid, parent, name, program, brief, state (`ready`,
  `running`, `waiting`, `stopped`, `done`, `failed`, `killed`), priority
  (nice −10…10), quota `{tokens, usd, seconds}`, usage, exit status and
  result, mailbox, event log.
- **Scheduler**: a fixed number of model-call slots (concurrency). A process
  asks for a slot before each model call; slots go to the highest priority,
  then the one that has waited longest (no starvation). A stopped process
  never gets one. Quota is checked at the slot: past it the process is sent
  TERM (it gets one last call to report), then KILL.
- **Accounting**: `charge(pid, usage, usd)` books tokens and dollars to the
  process and every ancestor. Live rates (tokens per second) for `/top`.
- **Signals**: `STOP`/`CONT` pause and resume at the next slot; `TERM` asks
  for a final report; `KILL` ends it at once; `INFO` returns what it is
  doing without spending a token.
- **IPC**: `send(pid, msg)` / `recv(pid)` mailboxes; a blackboard
  (`post(key, value, by=pid)` / `read(key)`) that every process sees.
- **Journal**: an append-only event log per process (spawned, slot granted,
  syscall, charged, signalled, exited), the basis for `/top`, `strace` and
  replay.
- **wait / join**: a parent waits for children and gets their exit status
  and result.

PID 1 is the chat you are talking to. Sub-agents (```task```) become
processes with their own quota, visible in `/top`, stoppable one by one.

## Background agents and copy-on-write (phase 17)

`/spawn <brief>` (or ```spawn``` from the agent) starts a full agent in the
background while you keep chatting. A writer gets its own git worktree of the
working folder (a plain copy when the folder isn't a repo), so two agents can
change code at once. When it exits, `/join <pid>` shows its diff and merges
it three-way into the folder; a conflict is reported, never forced.

## Virtual memory for context (phase 18)

Long tool output is stored in a page store and replaced in the conversation
by a one-line page-table entry: `[p17 · npm test · 12k chars · FAILED in
auth.test.ts:41]`. The model pages it back with ```page p17``` (or a line
range, or a grep within it). Paging is exact; summarising (compaction) only
happens when even the page table no longer fits.

## Daemons (phase 19)

Agent programs that wake on: a schedule (`daily 08:30`, `every 2h`), a file
or folder changing, a systemd unit failing, the disk passing a threshold, a
command's output changing. Each wake is a process with its own quota; its
report lands in an inbox with a desktop notification.

The point is what a daemon costs while it sleeps: nothing. An agent that
polls ("is the disk full yet?") spends tokens on every look. Here the
trigger is plain code (`chucknorris_ext/daemons.py`, no model): the clock, a
tree's newest mtime, `systemctl --failed`, `statvfs`, a command's output
hash, every 30 seconds (a trigger's command every 5 minutes). Watches are
edge-triggered: the first check records the state, only a change fires, and
the same failed unit never fires twice. The woken agent is told what fired
and what the trigger saw, so it doesn't spend a call looking again.

Unattended means it may look, never change. A daemon's syscall table has no
`write` or `edit`; its commands run in bubblewrap with the whole system
read-only, its folder included, the journal and the system bus visible (for
`systemctl status`), and `daemons.read_only_problem` refuses any verb that
would change something through the bus (`systemctl restart`, `loginctl
poweroff`, `busctl call`), sudo, `git push`, `pacman -S`. Without bubblewrap
only commands that are read-only by nature run. What it would change, it
proposes; the user decides (**Act on it**). A daemon that should change
code (`/daemon act …`) is a background agent in a copy of the folder,
merged by the user.

| You write                                  | It wakes                                 |
| ------------------------------------------ | ---------------------------------------- |
| `/daemon daily 08:30: summarise the journal` | at 08:30, or at the next start if missed |
| `/daemon when a service fails: why?`         | when a unit newly fails                  |
| `/daemon when disk / is above 90%: what?`    | on crossing, then every 6 h while above  |
| `/daemon when ~/proj/src changes: review`    | when a file there changes                |
| `/daemon when \`checkupdates\` changes: …`   | when the command prints something new    |
| `/daemon act every day at 3am: update deps`  | as a background agent in a copy          |

## Capabilities, programs and the governor (phase 20)

- **Capabilities**: `read`, `write`, `exec`, `net`, `spawn`, held by each
  process (PID 1, your chat, holds them all) and clipped at spawn to the
  parent's. Every syscall of an agent process goes through one guard
  (`_guard`): the capability it needs is checked (`kernel.SYSCALL_CAP`),
  the call is timed, and both land in the process's syscall log. Money is
  the `usd` quota; sudo is never a capability of an unattended process.
- **Programs** (`chucknorris_ext/programs.py`): a Markdown file whose
  header gives a mode (`report`: looks, read-only; `act`: works in a
  copy-on-write copy you merge), its tools, a model (`chat`, `fast`, or an
  id) and a quota, and whose body is the prompt. Its capabilities follow
  from its mode and tools. Built in: reviewer, tester, auditor, checkup,
  researcher, explainer; `~/.config/chucknorris/programs/` and a project's
  `.chuck/programs/` add or replace them. `/run reviewer the parser`; the
  agent uses ```spawn @reviewer the parser```.
- **Governor** (`chucknorris_ext/governor.py`): `performance` (the chat
  model everywhere), `balanced` (the chat and writers on the chat model;
  read-only sub-agents, daemons, report programs and summaries on the fast
  model), `powersave` (the fast model everywhere). The fast model is
  Settings' choice or the cheapest suggested model, weighted for agents
  (input counts three times); `/governor` shows who runs on what and what it
  saves.

## Hibernate, replay, time travel (phase 21)

`chucknorris_ext/hibernate.py`. After every completed step a background
agent or program is written to disk: its conversation, its step list and,
for a writer, a snapshot of its copy of the folder in a private git store
(`checkpoint.Shadow`, one per copy). A step cut short is never a
checkpoint: its tools didn't all run, so resuming from it would resume
from a lie.

- **Hibernate.** Chuck closing, crashing or the machine rebooting leaves
  the record at its last step; the next start shows INTERRUPTED AGENTS
  with Resume / Drop. Resume reattaches the same copy and goes on.
  Finished work that was never merged comes back as its card, ready to
  merge.
- **Replay.** `/replay <pid>` (or the card's Replay button): every step,
  the tools it called, what came back, and the tokens spent so far.
- **Fork.** `/fork <pid> <step> [instruction]` (or Fork here on a step):
  a new process with the conversation up to that step, the instruction
  where it picks up, and its copy of the folder as it was at that step:
  a copy of the base with exactly the changes between the base snapshot
  and that step's replayed onto it (`Shadow.replay_onto`), so files the
  snapshots ignore, like `.env`, are kept. Every run's store starts at
  its base, so a fork of a fork replays from the same place.
- **strace.** `/strace <pid>` lists every syscall with its latency, its
  size and whether the capability check refused it.
