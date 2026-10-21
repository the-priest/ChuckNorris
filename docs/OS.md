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

## What an agent needs from its OS (phase 22)

Written from the agent's side: what an agent working inside Chuck lacks
when it has only a prompt and a loop.

- **To be told things while it works.** `/send <pid> <message>` (the main
  agent: ```send```) lands in the process's mailbox; the worker drains it
  after each step's tools ran and hands it over with the results. A
  message that arrives as the agent is about to report is heard first.
- **To ask without stopping everything.** ```ask <question>``` in a
  background agent blocks only that process (WAITING, no slot held) and
  shows a card with an answer box and a desktop notification; an
  unanswered question times out (`ask_minutes`, 30) and the agent is told
  to decide and say what it assumed.
- **Shared memory between agents.** The kernel's blackboard as tools:
  ```post <key>: <fact>``` and ```board```, for every process including
  the chat; `/board` shows it. Two agents changing code at once tell each
  other what they are touching.
- **To know its own budget.** Every result ends with a `[kernel]` line:
  `step 7 of 60 · tokens 212k of 1.5M · 41 min left · 3 facts on the
  board`. An agent that can see its budget plans for it, instead of
  being cut off mid-thought.
- **To wait for its children.** ```wait <pid> [minutes]``` returns a
  background agent's report as a tool result (and not again as a note).

## Missions (phase 23)

`chucknorris_ext/missions.py` (pure) and the orchestrator in the app.

A mission is a goal too big for one agent. Process control was the
missing piece of an agent OS: one agent per request works for small jobs,
and big ones need what `make` and a CI system give a team of engineers.

1. **Plan.** One model call with the project's map and rules returns a JSON
   graph: tasks with an id, a kind (`code` changes files; `research` and
   `check` only look), a brief, and dependencies. `parse_plan` refuses
   unknown dependencies and cycles (Kahn's algorithm) and asks again once
   with the reason. The plan waits for Start.
2. **Run.** The mission is a kernel process (kind `mission`) with its own
   token and time quota; every task's agent is spawned under it, so its
   quota is clipped to what the mission has left and `/kill` on the
   mission ends them all. Ready tasks (pending, every dependency merged or
   done) start up to `mission_parallel` at a time. A code task is a
   background agent in a fresh copy of the folder, taken when it starts,
   so it sees the earlier tasks' merged work. Research runs the
   `investigator` program and checks the `reviewer`. Each agent's brief carries
   the mission, its task, and what the tasks before it reported.
3. **Verify.** When a code task finishes with changes, the project's tests
   run on its copy (`.chuck/test`, else `builder.test_command`). Pass →
   three-way merge. Fail → the same agent goes on in the same copy with the
   failing output appended to its conversation (it's not a fresh start), up to
   `mission_attempts`. Still failing, or a merge conflict → the task
   stops, and the mission pauses with Retry / Skip once nothing else can run.
4. **Survive.** Missions are saved after every change. At startup, a
   running mission gets a new process group; a task whose agent was cut off
   resumes from its hibernated step, and one with no checkpoint starts over.
   Missions started in this session are never taken over by that startup
   pass.

The mission card is the whole view: each task's state, pid, test result,
and its Report, Trace (strace) and Replay. When it completes, the chat and
the agent get a summary with the spend.

## One budget for everything (phase 24)

A per-chat cap stops one conversation; it doesn't stop twenty background
agents, a mission and three daemons each spending a little. `budget.py`
keeps a ledger of real cost per day on disk (every call goes through
`_account`, which books it), so a daily and a monthly limit hold across
everything the OS runs and across restarts. Over a limit the chat pauses
before its next call (Allow more raises today's or this month's limit by
the base amount); every agent process is refused its next model call at
`_sub_call`, so it ends with a report saying why; daemons don't wake.

## The OS you can see and reach (phase 25)

An OS isn't only its kernel; it's also the parts you touch.

- **The command palette** (`palette.py`, Ctrl+P) is the launcher: one
  ranked list of every action, built fresh each time it opens, so it holds
  the processes running now, the missions, your programs and chats.
  Ranking: a title starting with the query, then a word starting with it,
  then a substring, keywords, all words, then the letters in order.
- **The tray** is the status bar: `? 1  ▶ 2  ◎ 1  ✉ 1` (a question waiting,
  agents working, missions, unread reports), lit when an agent waits for
  you, hidden when nothing is going on; `/activity` is its window.
- **The shell**: `ChuckApp` handles the command line, so a second
  `chucknorris …` is forwarded by Gio to the running instance's
  `remote_command`, and its answer is printed in the caller's terminal.
  Scripts, cron, git hooks and editor tasks can drive the OS.
- **`/context`** is `free` for the context window: the real trimming and
  paging run as a dry run (no compaction, no notes), and the result is
  broken down by what takes the room.

## Coding and everyday help (phase 26)

- **`/watch`** (`aiwatch.py`) is an editor bridge that needs no plugin:
  mtimes are polled cheaply; only changed files are read; a comment ending
  (or starting) with `AI!`/`AI?` in any common comment syntax becomes a
  turn with the code around it. Each comment fires once (keyed with its
  surroundings, forgotten when removed); a draft in the composer is never
  overwritten; long or minified lines are skipped.
- **Selective merge**: `Workspace.merge(only=…)` takes the files you tick;
  the rest stays in the copy for later.
- **Reminders** are daemons of kind `remind` with one-time `{"at": …}` or
  repeating triggers: when due, a note, the inbox, a notification and the
  voice, and no process. They fire even past the budget.

