<div align="center">

# Chuck

### The operating system for AI agents.

**In the mainframe era you rented the CPU by the second. Now you rent the model by the token.**
Tokens are the new cycles, the context window is the new RAM, tools are the new syscalls, and an agent is the new process.
Every other agent is one program running on bare metal. Chuck is the OS underneath: a kernel that schedules tokens, isolates agents, lets them talk, puts them to sleep, wakes them on events, and rewinds them in time.
On your Linux desktop, today.

![version](https://img.shields.io/badge/version-17_·_Chuck_OS-8b5cf6?style=flat-square&labelColor=0c0c10)
![gtk](https://img.shields.io/badge/GTK4-libadwaita-8b5cf6?style=flat-square&labelColor=0c0c10)
![python](https://img.shields.io/badge/Python-3.11_·_3.12_·_3.13-8b5cf6?style=flat-square&labelColor=0c0c10)
![suites](https://img.shields.io/badge/test_suites-24-2ea043?style=flat-square&labelColor=0c0c10)
![deps](https://img.shields.io/badge/runtime_deps-stdlib_+_GTK-2ea043?style=flat-square&labelColor=0c0c10)
![licence](https://img.shields.io/badge/licence-MIT-8b5cf6?style=flat-square&labelColor=0c0c10)

<img src="assets/screenshots/welcome.png" alt="Chuck's welcome screen: a woven field of tiny code glyphs struck by a click, its uneven wavefront and echo spreading under the welcome card" width="100%">

</div>

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash && chucknorris
```

Paste a SiliconFlow key in Settings. Press **Tab** to pick Agent or Chat. Talk to it. Then type `/top`.

---

## What is this, in plain words?

**Chuck is an AI that works on your computer, not just one that talks.** You type (or say) what you want: *"my disk is full"*, *"add tests to this project"*, *"why won't Bluetooth start?"*. Chuck then actually does it. It runs the commands, reads and edits the files, tests what it changed, and tells you what it did and what it cost. The AI's thinking happens on a model you rent online (SiliconFlow). Everything it *does* happens on your machine, where you can see it.

**Why "an operating system"?** A single AI helper is like a single program. Once you have many of them working at the same time, you need what a computer has always needed to run many programs: someone to decide who goes next, who may touch what, who pays for what, and what happens when something crashes. With AI you pay per *token* (roughly, per word read or written), the way people once paid per second of computer time. So Chuck treats tokens like computer time and AI helpers like programs, and it gives them a real operating system:

- **You can see every helper at once** (`/top`), what it's doing, and what it has cost, and pause or stop any of them.
- **Each helper has a budget.** When it runs out, it is asked to write up what it found and stop. Nothing runs up your bill forever.
- **Helpers can work in the background** on a *copy* of your project, so they can't break your files. When one finishes, you look at what it changed and click **Merge** if you like it.
- **Helpers can talk.** You can send one a message while it works, it can ask you a question without stopping the others, and they share a notice board so they don't get in each other's way.
- **Helpers can wait for something to happen.** "Every morning at 8:30", "when a service fails", "when the disk is 90% full". Waiting costs nothing; they wake up only when it happens, look, and leave you a report.
- **Nothing is lost.** Every step is saved. If the computer crashes, a helper picks up where it was. You can even go back to any step of a helper's work and try a different direction from there.
- **Safety stays on.** A helper can do whatever you allow it to, but anything that could wreck your system (wiping a disk, deleting your home folder) always waits for your click.

**Do I need to understand any of that to use it?** No. Install it, open it, and talk to it like a colleague. The OS part works underneath, the way your computer's OS does. The rest of this page is for when you're curious.

---

## Chuck OS

An agent loop with a prompt and a few tools is a program. Run ten of them and you need what computers needed in 1965: something to decide who runs, who pays, who may touch what, and what happens when one crashes. That layer is Chuck OS. The full design is in [`docs/OS.md`](docs/OS.md).

| a computer has | Chuck OS has |
|---|---|
| CPU time | **tokens and dollars**, metered per process and charged up the tree |
| processes | **agents**: a pid, a parent, a quota, a state, a journal |
| a scheduler | **model-call slots**, granted by priority with ageing, never past a quota |
| RAM and virtual memory | **the context window**, with old tool output *paged out* and paged back exactly |
| syscalls and permissions | **tools**, each checked against the process's **capabilities** |
| fork, copy-on-write | **background agents** in their own copy of your folder, merged three-way |
| signals and IPC | **STOP / CONT / TERM / KILL**, mailboxes, a shared **blackboard** |
| init and systemd | **daemons** that sleep for free and wake on a schedule or an event |
| packages | **agent programs**: a prompt, tools, a model and a quota in one Markdown file |
| a CPU governor | **a model governor**: cheap work on a cheap model |
| `top`, `strace` | **`/top`** and **`/strace`**, live |
| hibernate | **every step checkpointed**: agents survive a crash and resume |
| (nothing) | **time travel**: replay any run and fork it from any step |

### `/top`: every agent is a process

<img src="assets/screenshots/os-top.png" alt="/top: the chat as PID 1 and its sub-agents, with state, priority, tokens, cost, tokens per second, run time, what each is doing, and Pause / Continue / Kill per process" width="100%">

The chat you are talking to is PID 1. Every sub-agent, background agent, program and daemon is a process under it, with a quota of tokens, money and time. A child can never spend more than its parent was allowed. Past its quota, a process is sent **TERM** and gets exactly one more call to write up what it has, then **KILL**. No runaway loop ever eats your balance. `/kill`, `/stop`, `/cont` and `/nice` work on any pid.

### Background agents: fork, then merge

```text
/spawn add tests for the parser and fix what they find
```

A full agent that writes, runs and tests code works **in its own copy of your folder** while you keep talking to Chuck. Its commands are jailed to that copy by bubblewrap. Run five at once; none can trample another. When one finishes, its card shows the diff, and **Merge** brings it home three-way: your edits and its edits to the same file are combined; overlapping edits are never forced, but set aside for you. The merge is checkpointed, so `/rewind` undoes it.

<img src="assets/screenshots/os-background.png" alt="A background agent's card: done, two files changed in its copy, its report, and Show diff / Merge into folder / Discard / Trace / Replay" width="100%">

### Agents that talk, ask, share, and know their budget

What an agent needs from its OS is what any colleague needs:

- **To be told things.** `/send 4 also keep the old port as a fallback` reaches pid 4 with its next results. The main agent does it with ```` ```send ````.
- **To ask without stopping the world.** A background agent's ```` ```ask ```` pops a card and a notification and waits, *WAITING* in `/top`, while every other agent keeps working.
- **Shared memory.** A blackboard every agent reads and writes: *"pid 3 is rewriting auth.py"*, *"the tests need Postgres on :5432"*. ```` ```post ````, ```` ```board ````, `/board`.
- **To see its own budget.** Every result ends with a `[kernel]` line (`step 7 of 60 · tokens 212k of 1.5M · 41 min left`), so an agent paces itself instead of being cut off mid-thought.
- **To wait for its children.** The main agent's ```` ```wait 4 ```` returns a background agent's report as a tool result.

<img src="assets/screenshots/os-ask.png" alt="A running background agent asks a question in a card with an answer box while it waits; the rest of the session carries on" width="100%">

### Time travel: replay and fork any run

Every step of a background agent is checkpointed: its conversation and, in a private git store, its copy of the folder. So:

- **It survives anything.** Close Chuck, crash it, reboot: the next start offers **Resume**, and the agent goes on from the step it was on, in the same copy. Finished work you never merged comes back, ready to merge.
- **`/replay <pid>`** walks through a run step by step: what it did, what it got back, what it had spent.
- **`/fork <pid> <step> [instruction]`** starts a *new* agent from any step, with the conversation up to there and the files **exactly as they were at that step**. *"At step 3 you went for a rewrite. Patch it instead."* The original is untouched; fork the fork as often as you like.

<img src="assets/screenshots/os-replay.png" alt="REPLAY: each step of a background agent's run with the tools it called, what came back, the kernel line, and a Fork here button" width="100%">

### Daemons: agents that sleep for free

An agent that polls *"is the disk full yet?"* burns tokens on every look. A Chuck daemon's trigger is plain code (a clock, a file's mtime, `systemctl --failed`, the disk, a command's output), checked every 30 seconds **for zero tokens**. Only when it fires is an agent woken, as a metered process told what fired and what was seen.

```text
/daemon daily 08:30: summarise new errors in the journal since yesterday
/daemon when a service fails: find out why and propose a fix
/daemon when disk / is above 90%: find what's filling it and what's safe to delete
/daemon when `checkupdates` changes: say which updates are worth taking
/daemon act every day at 3am: update the dependencies and run the tests
```

Unattended means **it looks, it never changes**: no file writes; commands run with the whole system read-only; nothing that restarts a service, powers off or installs a package gets through, even on the system bus. Reports land in `/inbox` with a desktop notification (and quietly, with none, when there's nothing to do). **Act on it** hands one to the agent. A daemon that should change code (`/daemon act …`) is a background agent in a copy you merge.

<table>
<tr>
<td width="50%"><img src="assets/screenshots/os-daemons.png" alt="DAEMONS: four daemons with their triggers, state and run counts, each with Run now / Pause / Delete"></td>
<td width="50%"><img src="assets/screenshots/os-inbox.png" alt="INBOX: daemon reports with their reasons, and Act on it"></td>
</tr>
</table>

### Programs, capabilities, the governor, strace

- **Agent programs** are packages: one Markdown file with a mode, tools, a model, a quota and a prompt. Six ship built in (`reviewer`, `tester`, `auditor`, `checkup`, `researcher`, `explainer`); add your own in `~/.config/chucknorris/programs/` or a project's `.chuck/programs/`. `/run reviewer the parser`, or the agent's ```` ```spawn @reviewer ````.
- **Capabilities.** Every process holds `read`, `write`, `exec`, `net` and `spawn`, clipped to its parent's. Every tool call goes through one guard that checks, times and logs it. A program without `fetch` never reaches the network; an offline-sandboxed folder takes `net` away from everything that works in it.
- **The model governor.** *Performance* runs everything on your chat model. *Balanced* moves read-only sub-agents, daemons, report programs and summaries to the fast model (here about 39% cheaper). *Powersave* runs everything on it. `/governor`, or Settings.
- **`/strace <pid>`**: every syscall a process made, its latency, its size, and what its capabilities refused.

<table>
<tr>
<td width="50%"><img src="assets/screenshots/os-programs.png" alt="PROGRAMS: the six built-in programs with their mode, capabilities, model and quota"></td>
<td width="50%"><img src="assets/screenshots/os-governor.png" alt="GOVERNOR · BALANCED: which model each kind of agent runs on, and the saving"></td>
</tr>
<tr>
<td colspan="2"><img src="assets/screenshots/os-strace.png" alt="STRACE: a program's syscalls with timing, size, and a denied write"></td>
</tr>
</table>

### Virtual memory for the context window

Long sessions don't forget; they page. Once a conversation is big, tool output older than the newest few results is swapped out of what is sent for a one-line page-table entry (`[p17 · npm test · 12k chars · FAILED in auth.test.ts:41]`). The model pages it back **exactly** with ```` ```page p17 ````, a line range, or a search inside it. Lossless comes before lossy: summarising only happens when even the page table no longer fits, and the summary carries the page table with it.

### The one thing it won't do

Inside Chuck OS an agent can do anything you grant it. One gate stays, by design: commands that can destroy the machine (disk wipes, `rm -rf ~`, `curl | sh`, removing core packages) wait for your tick, and agents running unattended never get sudo. That gate is what lets you leave twenty agents working overnight and come back to a machine you still own.

---

## Two modes, one key

**Agent** runs on your machine: shell commands, file edits, tests, packages. **Chat** answers only, and nothing touches your system. **Tab** flips between them from anywhere in the window, and the switch sits in the composer so you always know which one you're in.

<table>
<tr>
<td width="50%"><img src="assets/screenshots/agent-commands.png" alt="Agent mode: a reply, then a command card on the work lane"></td>
<td width="50%"><img src="assets/screenshots/chat-mode.png" alt="Chat mode: a formatted answer with a table and a copyable code block"></td>
</tr>
<tr>
<td><b>Agent.</b> Everything the machine did hangs on the <i>work lane</i>, a rail off Chuck's avatar. What he <i>said</i> and what <i>ran</i> never blur together. Every command shows its real exit code and output.</td>
<td><b>Chat.</b> Markdown, real tables, and code blocks with a copy button that scroll sideways instead of overflowing. Code in Chat mode is shown, never executed.</td>
</tr>
</table>

## A working folder for the session

Click the folder chip (or **Ctrl+O**) to point the session at a project. Commands run there. `read`, `grep`, `glob`, `write` and `edit` resolve relative paths there. Each request carries a one-line map of the folder's top level, so the model starts oriented. Choose **no folder** for questions and system work. Recent folders and Chuck's own `~/ChuckProjects` are one click away.

## Built for real coding work

| tool | what it does |
|---|---|
| `map` | Every file in the repo with the classes and functions it defines (real AST for Python, patterns for JS/TS, Go, Rust, C/C++, Java, Ruby, PHP, Lua, shell). Honours `.gitignore`. |
| `glob` · `grep` | Find files by pattern, search by regex. Uses ripgrep when installed, pure Python when not. Output is capped so a search never floods the context. |
| `read path:120-400` | Reads any size of file in **pages**. Each page ends with the exact `read` for the next one. Nothing is silently cut. |
| `edit` | Aider-style `SEARCH/REPLACE` blocks. Only the changed lines are sent, so it's cheap on output tokens. All-or-nothing, whitespace-tolerant, re-verified after the edit. |
| `write` | New files of any length. A reply that hits the output limit is **continued automatically** and stitched (repeated overlap trimmed) before the file is written. |
| `bash` · `python` · `node` | One step at a time, auto-verified before it runs (real defects only, never style), real exit code back. Output streams live into the card. `cd` carries to the next command, like a terminal. |
| `py` | **One Python that lives as long as the chat**, notebook-style: load data once and keep using it, see the last value, get matplotlib figures back as images. |
| `patch` | A multi-file unified diff, applied all-or-nothing. Hunks are found by their context, so wrong line numbers don't matter. |
| `browse` | A real headless Chromium. Load a page or file, click, type, press keys, run JS. Back come uncaught errors with file and line, failed requests, the page text and a screenshot. |
| `def` · `refs` · `rename` | **By meaning, through a real language server** (pyright, TypeScript 7's `tsc`, typescript-language-server, rust-analyzer, gopls, clangd): where `Store.save` is defined, every call to *that* `save` and not `Cache.save`, and a rename across files that leaves comments and strings alone, checkpointed like any edit. Without a server, `def`/`refs` fall back to syntax and say so. |
| `find` | **Code by what it does:** *"where do we retry failed requests?"* ranks functions, classes and doc sections by their names, docstrings, comments and identifiers (BM25, camelCase/snake_case aware), even when no line uses your words. |
| `diagnose` | LSP-lite: lint and type-check files on disk with whatever is installed (ruff, mypy, pyright, tsc, eslint, shellcheck, cargo, go vet). |
| `changes` | The net diff of everything this task changed, for self-review before it reports. |
| `plan` | A live checklist card that ticks itself off as the work proceeds. |
| `undo` | Every write and edit is a checkpoint with its diff and a **Revert** button. `undo` reverts the last one; `undo all` reverts the whole task. |
| `/rewind` | Before every step that acts, the whole folder is snapshotted into a private shadow repository (your own `.git` is never touched). `/rewind` puts the files **and** the conversation back to before any step, including what a *command* changed. A rewind can itself be undone. |
| `bg` · `logs` · `kill` | Dev servers and watchers run in the background with a live card. Chuck reads their logs and stops them, and they stop when the window closes. |
| `ask` | A question with one-click answers, for decisions that are really yours. The task waits for your answer. |
| `open` | Hands you the result in your browser or default app. |
| `runtests` · `package` · `tree` | Run the project's own tests, zip the result, show what was built. |
| `screenshot` · `look` | Chuck **sees**: the screen, or any image (a page rendered with headless Chromium, a plot, a mockup). Build a UI, look at it, fix it. |
| `task` | **Parallel sub-agents.** Each `task` block is a read-only sub-agent with its own model loop. Several run at once and report back; only their summaries enter the main context. |
| `search` · `fetch` | Live web research across SearXNG instances, with sources cited. A page that blocks a plain fetch (a bot check, or nothing until JavaScript runs) is read through the browser instead. |
| `remember` · `forget` | Long-term memory (below). |
| `mcp` | Any Model Context Protocol server's tools (below). |

<img src="assets/screenshots/agent-subagents.png" alt="Three sub-agents running in parallel, each with a done badge and a folded report" width="100%">

**Sub-agents are read-only by design.** They survey, research and report, many at once, and only their summaries enter the main context. Parallel *writers* exist too: they are [background agents](#background-agents-fork-then-merge), each in its own copy of the folder, so they never trample each other's edits.

## Steer it, plan it, retry it

- **Talk while it works.** Press Enter mid-task and your message is queued and delivered at the agent's next step. A *"not that file"* lands without waiting for the run to finish.
- **Plan mode.** Toggle **Plan** in the composer (or `/plan`). Chuck investigates read-only and proposes a plan card. Nothing changes until you press **Approve & run**, and **Revise** sends it back.
- **Retry and edit.** **Ctrl+R** asks the last question again. **↑** in an empty composer edits your last message and resends from there.

<img src="assets/screenshots/plan-mode.png" alt="Plan mode: Chuck read the game, then proposed a four-step plan card with Approve & run and Revise" width="100%">

## The workflow around the code

- **Git.** `/diff` shows the working tree as a card. `/commit` has Chuck stage the right changes and write the message from the diff. `/review` has him review the uncommitted diff for bugs before you push.
- **Hooks.** Settings → Agent takes a command to run after every file write (a formatter, with `{path}`, optionally only for matching files) and one he must pass before he may finish (`pytest -q`, a linter). Their results go back to the model, so he fixes what they find.
- **A Python session.** ```` ```py ```` blocks share one interpreter per chat. Load a dataset once, then explore it block by block. Stop interrupts a long loop and keeps the variables, and a figure comes back as an image he can look at. ```` ```python ```` stays a fresh one-shot script.

<img src="assets/screenshots/python-session.png" alt="Two PYTHON SESSION cards: the first loads a CSV, the second plots it, with the chart shown in the card" width="100%">

## It plays what it builds

Web pages and games get opened in a real browser and *played*: arrow keys pressed, buttons clicked, state read back with JavaScript. The BROWSER card shows you what it found, so a typo that throws on every frame never ships as "done".

<img src="assets/screenshots/browse.png" alt="A BROWSER card: one uncaught ReferenceError with its line, the key presses and JS result, and a screenshot of the game" width="100%">

**Which browser:** Brave if you have it, otherwise Chromium or Chrome. They all speak the DevTools protocol Chuck drives directly, with no extra dependencies. Or choose **[Camoufox](https://camoufox.com)**, a Firefox build that websites can't fingerprint, in Settings → Agent → Browser. It's for research on sites that block automation. Research also falls back to the browser by itself: a page that refuses a plain fetch is read in it, after its bot check has cleared. Install Camoufox with `CHUCK_CAMOUFOX=1 ./install.sh`, or `pip install --user -U 'camoufox[geoip]' && python3 -m camoufox fetch`.

Long builds stream into their card while they run, with a clock, and a finished log opens on its last lines, where the errors are.

<img src="assets/screenshots/live-output.png" alt="A command card with a LIVE output panel streaming build lines" width="100%">

## It finishes the job

Ask for a game and you get a game, not *"I'll write the game now"* and silence. Before Chuck ends a task he checks three things:

- **Did I promise something I didn't do?** A reply that announces an action without one is sent back to act.
- **Is the plan finished?** Unchecked steps on his plan card mean he isn't done.
- **Did I run what I changed?** Code written since the last run gets run, tested or screenshotted before the final report.

He stops early only to ask you something he shouldn't decide alone. That arrives as a question card with one-click answers. All of it is bounded by the step budget (60 by default) and none of it happens in Chat mode.

## It talks, and it listens

**A natural voice.** Chuck speaks with [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), the most natural of the open local text-to-speech models. It runs on your CPU, faster than real time, and nothing is uploaded.
- Speech starts about half a second after a reply arrives: the first sentence is spoken while the rest is still being rendered.
- The whole reply streams through one player, so there are no gaps or clicks between sentences.
- What people write but never say is spoken the way a person would. `e.g.` becomes "for example", `->` becomes "then", and `v16.0.0` becomes "version 16.0.0". Paths are read by name, code and tables are skipped, and bullets and headings get real pauses.
- Pick a voice in Settings → Voice and hear it at once. The default is a blend of Kokoro's two clearest male voices; *Heart* is rated its most natural overall.

**Talk to it.** Click the mic, or press **Ctrl+M**, and just talk. It stops when you pause and puts what you said in the composer, or sends it straight away if you turn on hands-free. Recognition is [Moonshine](https://github.com/usefulsensors/moonshine) with a voice-activity detector, all on your machine. Talking over Chuck interrupts him.

**If the mic doesn't hear you:** Chuck records with whichever tool your system has (`parecord`, then `pw-record`, then `arecord`), and if one fails it tries the next and remembers the one that works. If none gives any sound you'll see why, not a vague "didn't hear". `/doctor` and **Settings › Voice › Test the microphone** record one second and tell you whether sound is coming in. If the level is near zero, pick the right input in your desktop's sound settings, or give Chuck your own command under **Microphone command**.

The installer sets both up, about 600 MB. `/voice` installs them later if you skipped it (`CHUCK_VOICE=0`). `/doctor` checks both with a real test.

## Context it always has

- **Project rules.** Put conventions in `CHUCK.md`, `AGENTS.md` or `CLAUDE.md` in the project (or `~/.config/chucknorris/CHUCK.md` for everything) and they ride along with every request, in the cached part of the prompt. `/init` has Chuck survey the project and write its `CHUCK.md` for you.
- **`@file`** in a message attaches that file from the working folder.
- **Your own commands.** Put a prompt in `.chuck/commands/<name>.md` (in the project, or `~/.config/chucknorris/commands/` for every project) and it becomes `/name`. `$ARGUMENTS` takes what you type after it, and a `description:` line shows it in `/help`.
- **Slash commands** for everything you'd otherwise click:

<img src="assets/screenshots/commands.png" alt="The /help card listing every slash command" width="100%">

## MCP: every tool the ecosystem ships

Chuck is a [Model Context Protocol](https://modelcontextprotocol.io) client. Servers go in `~/.config/chucknorris/mcp.json`, in the same format Claude Desktop uses, so any server's README snippet pastes straight in:

```json
{"mcpServers": {
  "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
             "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "..."}}
}}
```

They start in the background and their tools are listed to the model, which calls them like any other tool. Settings → Agent shows each server's state; `/mcp reload` picks up edits. With no servers configured it costs nothing.

## Attach anything

Click the paperclip, **drag files onto the window**, or **paste an image** (Ctrl+V straight from your screenshot tool or a browser). Everything lands in a tray above the composer, where you can review it and remove it before sending.

- **Images** go to the model *as images*. With a text-only chat model (DeepSeek), they're routed to the vision model automatically.
- **Text files** go as their contents, up to 400k characters. Larger ones say where the rest is.
- **Other files** go by path, so in Agent mode Chuck can still work with them.

Images are paid for once: they ride with the request that needs them, and history keeps only text.

<img src="assets/screenshots/attachments.png" alt="Composer with an image thumbnail and a file chip in the attachment tray" width="100%">

## Models, pricing, and what you spent

The header meter shows **this chat's cost and tokens**, from the usage the API actually reports: prompt, cache hits, completion and reasoning tokens. A click opens the breakdown, the session total and your live account balance.

| model | input / 1M | output / 1M | sees images |
|---|---|---|---|
| `zai-org/GLM-5.3-Flash` *(default)* | $0.15 | $0.50 | yes |
| `deepseek-ai/DeepSeek-V4.1-Flash` | $0.158 ($0.01 cached) | $0.633 | via vision model |
| `deepseek-ai/DeepSeek-V4-Flash` | $0.13 | $0.28 | via vision model |
| `zai-org/GLM-5.3` | $1.40 ($0.26 cached) | $4.40 | yes |
| **Free:** `Qwen/Qwen3-8B`, `THUDM/GLM-4-9B-0414`, `deepseek-ai/DeepSeek-R1-0528-Qwen3-8B` | $0 | $0 | no |

List prices as of October 2026. Switch models from the composer's model menu. **Settings → Fetch** pulls the live list your key can use; SiliconFlow's free tier rotates. A model the table doesn't know shows tokens without inventing a price, and you can set a custom price in Settings.

<p align="center"><img src="assets/screenshots/usage.png" alt="Usage popover: cost, input with cache hits, output with reasoning, price, session total, balance" width="320"></p>

## Efficient by construction

Every hop of an agent loop re-sends the system prompt, so its size is paid per tool call, not per chat.

- **Prompts:** agent under 4k characters (v13 was 5.7k while documenting far fewer tools); chat 0.7k.
- **Playbooks** (debugging, TDD, review, git, UI, performance, security, Arch, building…) are injected only when the task triggers them, at most two at a time, and never in Chat mode.
- **Cache-friendly:** the volatile part (date, memory, folder) is appended *last*, so the long prefix stays cacheable on the API side.
- **Context control:** older tool output is compressed extractively (head, tail and every error line kept). A long session is summarised by the model instead of forgetting its oldest turns, and `/compact` does it on demand. The context budget is a setting.
- **Batching:** read-only tools batch into one round trip, and four of them cost one model call, not four.

## Memory you can trust

Durable facts persist across chats and only the few relevant ones are sent with a question, never the store. Two upgrades in the spirit of [supermemory](https://github.com/supermemoryai/supermemory):

- **Updates supersede.** "GPU is an RTX 4090" replaces "GPU is an RTX 3080" instead of sitting beside it. The old fact is archived, not destroyed.
- **Project scope.** Facts saved as `project:` belong to the working folder and are recalled only there, so one repo's conventions never leak into another's answers.

All of it is visible, searchable, addable and deletable in the Memory panel.

## When things go wrong

- **A model is down?** After its retries, that request is answered by a fallback model (automatic, or your choice in Settings), and the chat says so.
- **Watching spend?** Set a per-chat spending cap. When a chat reaches it, the task pauses and asks before spending more.
- **Esc** stops the turn *and* the command it was running, whole process tree included.
- **Crashed or lost power mid-task?** The chat is saved as each step starts. The next time Chuck opens, the welcome screen offers to **Resume** it, and the agent checks the current state before carrying on.
- **`/doctor`** checks your setup in one card: key and balance, model, browser, linters, screenshots, voice, MCP servers, disk, and recent errors, each with the fix.
- **A crash log.** Errors on the UI thread, in worker threads and in tools are written to `~/.local/state/chucknorris/chuck.log` with their tracebacks (never keys or chats), so a bug report has something in it. Settings opens it.
- **Updates.** Once a day Chuck checks GitHub for a newer version and offers it. `/update` installs it without sudo. A git clone gets a fast-forward pull, and an installed copy is downloaded, checked and swapped in whole. **Restart now** reopens your chat.

<img src="assets/screenshots/doctor.png" alt="The DOCTOR card: a row per check with a green tick or an amber warning and the fix" width="100%">

## Safety that doesn't get in your way

**Per-folder access** (`/access`, shown on the folder chip): *Full access*; *Sandboxed*, where commands, code, the Python session and background jobs run under [bubblewrap](https://github.com/containers/bubblewrap) with the system read-only, only the project writable, a private `/tmp` and no sudo; *Sandboxed, offline*, the same with no network; or *Read-only*, where nothing runs or changes. Give an unfamiliar repo the sandbox and let it work.

Commands run on their own; that's the point of an agent. **CRITICAL** ones (disk wipes, `rm -rf /`, `curl | sh`, removing core packages) arrive disarmed and wait for your tick, and so do saved skills whose *body* is critical. Commands run in their own process group with resource limits, and a timeout kills the whole tree. `pacman -S` is rewritten to `-Syu`. sudo authenticates once per chat, in memory only. Every command lands in a hash-chained ledger you can re-verify in Settings.

## Keyboard

| key | action |
|---|---|
| **Tab** | switch Agent / Chat |
| **Enter** · **Shift+Enter** | send (or, while it works, steer) · new line |
| **Ctrl+R** · **↑** | retry the last answer · edit your last message |
| **Ctrl+M** | talk to Chuck (local speech recognition) |
| **Ctrl+V** | paste text, an image, or copied files |
| **Ctrl+O** | working folder |
| **Ctrl+N** · **Ctrl+B** · **Ctrl+,** | new chat · chats sidebar · settings |
| **Esc** | stop the current run (and its command) |
| **/** · **@** | commands · attach a file |

## The window

Dark neutral surfaces with one violet accent, used only for edges, focus and what's live. The welcome screen is woven from code glyphs. Move the pointer and a soft lens of that fabric surfaces under it, with an outline that breathes and an edge that dissolves glyph by glyph. Click and the fabric is struck. Each strike rolls out with its own uneven, lobed front plus a slower echo, so no two spread alike. The net really moves: glyphs are pushed out at the crest, pulled back behind it and woven sideways, threads light up between neighbours where it flexes, and a cool white edge gives way to a violet crest and an indigo wake of characters still decoding. It costs nothing at rest, thins itself out on slow machines to hold 60 fps, and respects your desktop's reduce-animation setting. The transcript follows new output only while you're at the bottom: scroll up to read and it stays put, with a *Jump to latest* button to come back. Wide code and tables scroll inside their own block, and the page never scrolls sideways.

<img src="assets/screenshots/settings.png" alt="Settings: account, model, and the models and pricing list" width="100%">

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
```

Or read it first, which you should:

```bash
git clone https://github.com/the-priest/ChuckNorris.git && cd ChuckNorris && ./install.sh
```

Arch / CachyOS first-class; apt and dnf supported. The installer handles GTK4 + libadwaita (with the PyGObject cairo bridge), a screenshot tool, polkit, voice (Piper, espeak-ng fallback), `yt-dlp`, and the verifier linters. Runtime needs nothing beyond the standard library and GTK; everything optional degrades gracefully.

## Tests

```bash
./run_tests.sh
```

Twenty-four suites run headless against a stubbed toolkit and drive the real window end to end. They cover whole conversations, both modes, edits in a real folder, paged reads of a 12,000-line file, a file written across two cut-off replies, parallel sub-agents, images reaching the model as images, cache-hit pricing, memory updates and scopes, an agent that stops early or never stops, late streams from a stopped turn, undo and background jobs, the kernel's scheduler and quotas, background agents merging three-way, paging, daemons that only look, capabilities and programs, a crash mid-run resumed and a run forked from a past step with its files as they were, agents asking and messaging each other, steering mid-task, plan approval, git and hooks, a Python session that survives Stop, bot walls on a local site read through real Chromium, updates that refuse a broken download, renames through real pyright and TypeScript servers, rewinding what commands changed, real bubblewrap sandboxes that block writes outside the project and the network, a README whose example must not run, the critical gate, sudo storms, orphaned processes, and the ledger chain. `test_deep.py` runs whole sessions with real processes and ports, plus 180 seeded random model replies that must never hang the app, run something they shouldn't, or write outside the folder. The UI itself was checked by rendering every screen on real GTK 4.14 / libadwaita 1.5. The screenshots above are those renders.

## Being straight with you

- **The model is remote.** Your prompts, attached files and screenshots go to SiliconFlow.
- **It's a personal project,** cleaned up to share, not a product with an SLA.
- **Prices drift.** The table is a snapshot; set a custom price if yours differs.
- **The `DeepSeek-V4.1-Flash` model ID** follows SiliconFlow's naming convention. If your account lists it differently, **Fetch** in Settings shows the exact ID.
- **A regex risk classifier is a safety net, not a proof.** Read the CRITICAL cards.

---

<div align="center">

**MIT.** Made by **The Priest** · *In memory of a legend, 1940–2026.*

</div>
