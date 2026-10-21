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

Agent programs that wake on: a schedule (`every day 08:30`), a file or
folder changing, a systemd unit failing, the disk passing a threshold, a
command's output changing. Each wake is a process with its own quota; its
report lands in an inbox with a desktop notification.

## Capabilities, programs and the governor (phase 20)

- **Capabilities**: `read:<paths>`, `write:<paths>`, `exec`, `sudo`,
  `net`, `spend:<usd>`; checked at every syscall; a child's are a subset of
  its parent's.
- **Programs**: `~/.config/chucknorris/programs/<name>.md` (prompt, tools,
  capabilities, model, quota); `/run reviewer "the diff"`.
- **Governor**: `performance` (strongest model everywhere), `balanced`
  (strong for planning and acting, fast for reading and sub-agents),
  `powersave` (fastest that passes).

## Hibernate, replay, time travel (phase 21)

Every process checkpoints after each step and resumes after a crash. The
journal replays a run step by step; `/fork <pid> <step>` restarts it from
any step with a different instruction. `/strace <pid>` lists every syscall
with its latency and token cost.
