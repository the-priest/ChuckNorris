# Changelog

## 18.0.0 — Chuck in Rust

Chuck is now a Rust app (`crates/`), and the installer puts it over any earlier Chuck. See
`docs/RELEASE-18.0.0.md` for the full notes and `ARCHITECTURE.md` for the design.

- One Lead and up to eight jailed workers on SiliconFlow. Done is enforced in code: criteria,
  evidence, verification after the last change, and a critic review.
- Long jobs have no step or time limit. A paused job says why, and **Keep going** resumes it.
- Default access: the project folder freely, anything outside it only with your yes.
- Closing the window ends every process Chuck started.
- Chat and the Lead get the earlier conversation as real turns. A search written in a model's own
  tool format (`</parameter>`, `<tool_call>`), or left unclosed, runs or is reported back to it. A
  made-up "user:" turn is cut.
- Once criteria exist, the Lead can't end a job with a plain "done" in prose. A real file in `/tmp`
  counts as outside the folder.
- A new README, and new screenshots of the Rust app.
- Camoufox reads pages that turn bots away. It is set up by the installer, picked automatically,
  and checked request by request like Chromium. A page from a restricted folder is served privately,
  so it sees only that folder's files.
- Three bot-check markers (Cloudflare's `cf-browser-verification` among them) never matched: they
  sat after a line break in a raw string. They match now.
- The Python app stays installed as the fallback. It now offers the switch to the Rust app.
- **The fleet is a team.** Workers run on DeepSeek-V4-Flash (cheaper); the Lead (V4.1-Flash)
  plans, reviews and merges; GLM-5.3-Flash is only the eyes. Up to 8 workers.
  - Workers talk on a shared channel (```` ```team``` ````, to everyone or `@name`). Every message
    is in the new **Team talk** section.
  - The Lead waits while workers build: no turn spent polling. It wakes on a submission, a message
    to it, or your words.
  - The Lead is the boss: it can change any file at any time. A worker on that file is told, and the
    Lead's version stands at the merge. ```` ```cancel tN``` ```` takes a task back.
  - Each agent's line shows what it is typing, live.
  - **Chuck pops up** in the corner with what the Lead has to say: its news, its questions, the
    finished job. His mouth moves while it's read aloud, and Settings › Voice turns him off.
  - The Lead fixes a bug or small change itself instead of handing it out. Anything bigger it plans
    whole and spreads over every worker at once (6 by default).
  - **A supervisor.** A worker 8 turns without changing or verifying anything is warned. At 12 it
    is replaced by a fresh worker told what the last one tried. A worker that ends unfinished
    (stuck, out of steps, crashed) is replaced the same way. A step that fails a second worker comes
    back to the Lead to take over or re-split, never waited on.
  - The plan ticks off a step when its task merges.
- Tool calls in DeepSeek's own format (DSML, `<invoke>`, `<tool_call>`) run as the blocks they
  mean, for workers too. Before this, workers' calls were dropped, so the Lead ended up doing all
  the work. A ```` ```criteria```` opened mid-line counts too.
- The Agents section shows only the agents at work and what each is doing. Finished ones are a
  count.
- A worker whose model call crashes is reported to the Lead, not retried for minutes. A worker
  thread that dies is noticed, not waited on forever.
- Smaller prompts:
  - Every worker gets the same system prompt, so the API caches it once for all of them.
  - A worker's context is capped lower than the Lead's.
  - V4-Flash is priced with its cache discount.
- **Agents call their tools through the API.** Every tool is offered as a function. What a model
  calls comes back parsed and runs as the block it means. Calls written as text still work,
  including DSML, `<read x>` and `<bash>…</bash>`. A server that refuses `tools` gets the request
  without them.
- **Worker copies have what the project needs.** `node_modules`, `.venv`, `venv` and `.git` are
  lent read-only, so a worker's checks run as the Lead's do. Before, tests that needed
  dependencies always failed in a worker's copy.
- **Checks count however they're run.**
  - A criterion's verify command run with `bash` counts as verification. It runs with pipefail.
  - A check wrapped in a pipe or chain is explained, not silently ignored.
  - A large task with a small, complete change can be handed in. The Lead weighs proportion at
    review.
- **Stalls and spend are visible.**
  - A reply stream kept alive with no words is retried, not waited on forever.
  - Each agent's line shows its call in flight.
  - The Lead's row shows its own spend and the team's.
  - A worker that stops says why.
- **Camoufox is set up once.** It is launched with the browser it has, and never downloads at
  launch. The installer leaves a working setup alone.
- **Debug: what gets in the agents' way is written down.**
  - What the runtime sees, always: calls that didn't run, missed edits, refusals, checks that don't
    count, refused hand-ins, stuck and replaced workers, rejections, API trouble, cut replies.
  - Settings › Fleet › Debug mode also asks every agent to note its own (`snag`).
  - Where it shows: the right panel's Debug section, `/snags` for what keeps happening, and
    `~/.local/state/chucknorris/snags.jsonl` to send in.
- Races:
  - The first entry through its gate is reviewed at once; accepting it stops the other entrant.
  - The Lead races only a small, risky piece, never a whole build.
- A job never stalls on a question: with no answer in 3 minutes the Lead makes the most reasonable
  choice, says which, and goes on; an answer typed later still reaches it. (Approval of a critical
  command still waits for you, however long.)
- The independent review can't send a job round forever: after 3 rejections, a `done` the gate
  accepts finishes, with the review's open points in the summary.
- Root is refused where a command starts (`sudo …`, `… && sudo …`, `bash -c "sudo …"`, `env X=1 sudo`,
  `xargs sudo`, `run0`), no longer anywhere in the line: `grep -rn su .` or a commit message
  mentioning sudo run.
- The gate's unfinished-code check reads TODO/FIXME/XXX/HACK in comments only: a Tetris piece drawn
  as `"XXX"` or a to-do app's `<h1>TODO</h1>` no longer blocks a hand-in.
- Finished work said in prose isn't lost: a worker that checks everything and then just says it's
  done is handed in; a Lead that finishes in prose with every criterion verified ends through `done`
  (gate and independent review included) instead of being paused as unfinished.
- A dev server or watcher started in front (`npm run dev`, `python3 -m http.server`, `vite`, `flask
  run`, `tail -f`…) holds its agent 25 seconds, not half an hour: it never ends by itself, and the
  agent is told to start it in the background and test it in the same command.
- Bugs found by new generated tests (tens of thousands of hostile replies: cut and nested fences,
  multi-byte text at every cut, call markup in five dialects, NULs, escape codes):
  - a NUL in a reply, a command's output, a diff, a web page or an MCP result crashed the window
    (GTK refuses it): it is dropped where text comes in, and the window's labels guard too;
  - a web address followed by `code` swallowed the code into the link: the reply showed nothing;
  - the placeholder characters the renderer uses could be faked by a reply.
  The tests check every parser, cutter and cleaner, the Markdown → markup (always valid Pango),
  the safety classifier, and that no file op writes through a link out of the folder.
- **One model does it all, by default.** DeepSeek-V4.1-Flash-1017 builds the whole job itself, the
  fastest way there: criteria (a few quick checks, about exactly what you asked) and every file in its
  first reply, the checks and `done` in the next. Alone, no plan is required, there's no line-count
  check and no second model call for review: its checks passing after the last change is "done". A
  worker count saved before goes back to 0 once; raise Settings › Workers to bring the fleet back.
- With workers on:
  - a task whose criteria check a file another open task is building waits for that one to merge
    (side by side, its checks could never pass in its copy, and its worker went round in circles);
  - what merges reaches the copies of workers still building (their own changes stay theirs);
  - workers run a failing check once, read it, and fix the cause; a check that needs a teammate's
    part is reported, not re-run;
  - workers don't think by default (the Lead does).
- One reply runs at most 15 commands, stops after 3 failing in a row, and never runs the same call
  twice with nothing changed between (a model looping wrote dozens).
- Every worker gets a part, and good work is merged at once: a hand-in that passes its gate and its
  re-run checks goes straight into the project, without waiting for the Lead's turn (Settings ›
  Merge good work at once). The Lead sees each one with its diff and fixes or reassigns what's wrong;
  the independent review still judges the whole job. The Lead lays a build out as separate files,
  hands every part out in one reply, and writes the glue itself meanwhile.
- Faster:
  - workers think lightly (Settings › Reasoning › Workers' reasoning, 1k by default) and never for
    more than 45 s; the Lead's default is 2k;
  - a worker's copy of the project is made on every core, and what it changed is found from file
    times, not by reading every file (a 190 MB project: copy 3.5 s → 0.5 s, changes 245 → 75 ms);
  - the job's snapshot and first checkpoint are taken while the Lead thinks, not before;
  - grep searches on every core.
- The Lead works too: what it reads, writes or runs while workers build comes back at once. It
  watches them (FLEET WATCH: each worker's turns and how many went by without progress); a worker
  4 turns without progress is told to hurry, on the team channel; the Lead is woken to deal with
  workers falling behind.
- Replies render right: tables as tables, **bold** around `code`, numbered lists, quotes, rules,
  nested bullets and checkboxes. Chuck's bubble leaves tables out.
- A merge never takes a worker's version over a change of yours made in the same instant the copy
  was taken (the file clock's coarseness can't hide it).
- The old Python app is gone from the repository (about 40,000 lines with its tests). Chuck is the
  Rust app only: the installer builds it, sets the voice up with `chuck voice install`, and removes
  the old app's code. What is still Python is the three small helpers around Python-only libraries
  (Kokoro's and Moonshine's model runners, the Camoufox bridge), compiled into the binary.
- `chuck voice [install]`, and Settings › Voice › Set up: the voice is set up (or repaired) from
  the app, with checksummed downloads that are never kept half-done.
- While workers build, what the Lead reads or runs comes back to it at once (before, it waited for
  the next hand-in to see it).
- A race never stalls: when one entry is sent back, the other's hand-in is reviewed next.
- A reply caught in a loop (the same text written thousands of times) is cut as soon as it starts
  repeating. What came before is kept, the agent is told, and a snag is noted. Before, one such reply
  ran 8 minutes and 30k tokens.
- Commands can't hang waiting for a person: editors, git prompts, npm/pip questions all answer
  themselves (GIT_EDITOR, GIT_TERMINAL_PROMPT=0, npm_config_yes, PIP_NO_INPUT…).
- Command output reads clean: colour codes, progress-bar redraws and control characters are stripped.
- A long log keeps its end (where the error is), not just its start.
- A minified line (a whole bundle on one line) is shown in part instead of flooding the reply.
- `read file.js:70-90` and its other spellings always find the file.
- A command that never stops (a dev server, a watcher) is explained when it times out, with the
  way to run it in the background.
- `test` as a function call can name the criteria it proves.
- Workers change files with edit/patch instead of rewriting them whole.
- `read .` lists the folder.
- A sed/awk pattern like `'/## 3. Setup/'`, or text a heredoc only writes out, is no longer taken for
  a path outside the folder. Numbered criteria (`1. …`) parse.

## Unreleased (v17, in progress)

The third roadmap (`docs/ROADMAP.md`, phases 10–15), with tests in
`tests/test_v17.py`.

### New

- **Code intelligence.** ```` ```def ```` / ```` ```refs ```` / ```` ```rename ```` through
  real language servers (pyright, TypeScript), falling back to the syntax
  tree; ```` ```find ```` ranks code by what it does.
- **Checkpoints and `/rewind`.** The whole folder is snapshotted (in a
  private git store, never your own `.git`) before every step that acts;
  `/rewind` puts files and the conversation back, and is itself undoable.
- **Per-folder access** (`/access`): Full, Sandboxed (bubblewrap: only the
  folder is writable, credentials hidden, no host sockets), Sandboxed offline,
  and Read-only.
- **Custom slash commands** from `.chuck/commands/*.md`.
- **Resume after a crash**: a task that was running when Chuck closed is
  offered on the welcome screen.
- **A natural voice** (Kokoro, local, about half a second to first word) and
  **voice input** (Moonshine, local; mic button or Ctrl+M, optional
  hands-free sending).

### Chuck OS (`docs/OS.md`, phases 16–24)

- **The agent kernel.** Every agent is a process: a quota of tokens, money
  and time, its spend charged to it and every ancestor, scheduled onto
  model-call slots by priority. Past its quota a process is asked to report
  (TERM), then killed. `/top` shows them live; `/kill`, `/stop`, `/cont`,
  `/nice`.
- **Background agents** (`/spawn`, ```` ```spawn ````): a full agent edits,
  runs and tests in its own copy of the folder while you chat; its diff is
  merged three-way when you say so, conflicts set aside, never forced.
- **Virtual memory for the context**: old tool output is paged out of what
  is sent, exactly recoverable with ```` ```page pN ````, before anything is
  summarised.
- **Capabilities, programs, a governor.** Every agent process holds
  capabilities (read, write, exec, net, spawn) clipped to its parent's and
  checked at every tool call; `/strace <pid>` shows each call it made.
  Agent programs are installable Markdown files (six built in: reviewer,
  tester, auditor, checkup, researcher, explainer): `/run`, `/programs`,
  ```` ```spawn @name ````. `/governor` puts cheap work on a cheap model.
- **Time travel.** Background agents and programs checkpoint after every
  step and survive a crash or a reboot (Resume at the next start);
  `/replay <pid>` shows a run step by step and `/fork <pid> <step>
  [instruction]` starts a new agent from any step, files as they were then.
- **No commands needed.** Say what you want; Chuck runs the OS for you: it
  picks between doing it itself, parallel sub-agents, a background agent, a
  mission, a program, a reminder or a daemon (orchestration rules in its
  prompt), and drives everything else with ```` ```chuck <command>``` ````
  (processes, missions, budget, governor, inbox, watching the editor),
  reading the result. Missions it starts run by themselves under your
  auto-start limit; read-only daemons are created with an Undo; raising a
  spending limit always asks you. In Chat mode, "do it" requests get a
  one-click handoff to Agent mode.
- **A shell for the OS**: with Chuck open, `chucknorris "…"`, `--here`,
  `--mission`, `--spawn`, `--run`, `--remind`, `--daemon`, `--top`,
  `--budget`, `--inbox` reach the running window and answer in your terminal.
- **Reminders** (`/remind in 20m: …`, `at 5pm`, `tomorrow at 9am`, `every
  day at 8:30`, or just ask): a note, a notification and the voice, with no
  agent and no tokens; they fire even over budget.
- **`/context`**: what the next model call sends, by what takes the room.
- **Pick which files to merge** from a background agent (a check per file
  in its diff card); **`/pr`** for a branch, commit, push and pull request;
  a **mission cost estimate** before Start that learns from past missions.
- **`/watch`**: write a comment ending in `AI!` in any editor and save; Chuck
  does it and removes the marker. `AI?` asks a question. Costs nothing while
  it waits.
- **Ctrl+P, the command palette**: every action, running agent, program,
  mission, chat, folder and model in groups, fuzzy-searched; Enter runs it
  or puts what it needs in the composer.
- **The tray**: a header indicator of questions waiting, agents working,
  missions and unread reports (lit when an agent waits for you); a click or
  `/activity` lists them all with a button for each.
- **One budget for everything** (`/budget`, Settings): a daily and monthly
  limit across every chat, agent, mission and daemon, kept on disk.
- **Missions** (`/mission <goal>`): a goal planned as a graph of tasks and
  run by agents in parallel, like `make`; each code task's work is merged
  only once the project's tests pass on its copy, failures go back to the
  agent that made them, and the whole mission is one process group with one
  budget that survives a restart. Any background agent's copy is now tested
  when it finishes, so the card says pass or fail before you merge.
- **Agents that talk.** `/send` a running agent a message; a background
  agent can ```ask``` you and wait while everything else goes on; a shared
  board (```post```, ```board```, `/board`); every step shows an agent its
  budget; the main agent can ```wait``` for a background agent's report.
- **Daemons** (`/daemon <when>: <what>`, `/daemons`, `/inbox`): agents that
  sleep for free until a schedule, a file change, a failed service, a full
  disk or a command's output wakes them; they look, report and propose,
  never change anything unattended.

### Polish and hardening

- **A cleaner agent chat.** What Chuck says after a tool block sits under that
  block's card; pictures it looks at share one labelled gallery card; a new
  file's diff starts folded; placeholders carry no buttons; a stray code fence
  from the model never dumps a file's code into the chat. Mission, daemon and
  inbox cards are tidy lists with inline actions and a progress bar.
- **Restricted folders protect git.** A repo's hooks and config are read-only
  inside the sandbox, a hook planted in a repo created during a run is
  disarmed, and Chuck's own file tools leave `.git` alone there.
- **More secrets hidden** from sandboxed agents: browser profiles, cloud and
  rclone credentials, and token files (`.git-credentials`, `.npmrc`, `.pypirc`,
  `.pgpass`, `.my.cnf`).
- **Merges never follow links.** A link in an agent's copy that points outside
  it is refused at merge; pipes, sockets and devices are ignored everywhere a
  tool reads the folder.
- **No back doors around approvals.** Through the agent's `chuck` tool, a
  daemon that acts is only proposed and a mission over the auto-start limit
  waits for your Start, exactly as through the regular tools.
- **The critical gate** treats more home and system wipes as critical
  (`rm -rf ~/.`, `rm -rf ..`, `mv ~ …`, `rsync --delete` into home or `/`).
- **Damaged files never stop Chuck opening**: chats, missions, memory,
  checkpoints and the ledger are validated as they're read.
- **Privacy.** Keys and tokens are redacted from the crash log, and Chuck's
  data and config folders are private to you.
- **Smaller fixes.** The browser is only handed web addresses; terminal output
  from the shell commands is stripped of escape sequences; `/watch` ignores
  comments Chuck wrote itself; `./file`, quoted and `path=` file names in a
  write block go where the model meant.

### Fixed (bug hunts on this cycle)

- **Voice input said "didn't hear anything" the instant the mic was tapped**
  when the recorder couldn't run (pw-record that can't write raw audio to a
  pipe, parecord without pipewire-pulse): its instant exit read as silence.
  Recorders are now tried in turn (parecord, pw-record --raw, pw-record,
  arecord), the working one is remembered, and if none gives sound the
  error says why. `/doctor` and Settings › Voice record a real second to
  prove the mic; a custom microphone command is in Settings.

- Sandbox escapes: switching folders with ```` ```project ````, a repo's own
  git config (`core.fsmonitor`) run by map/find//diff, `~` as the working
  folder uncovering `~/.ssh`, orphaned sandboxed Python kernels, and Chuck's
  own read/grep/browse reaching credentials in restricted folders.
- Code shown in a ````` ````markdown ````` example or in an edit's REPLACE text
  could be executed; it never is now.
- Edits: CRLF files keep their line endings, `=====` underlines no longer
  split a SEARCH block, `patch` no longer rewrites form feeds or U+2028, and
  undo restores non-UTF-8 files instead of deleting them.
- rename could change the wrong symbol (UTF-16 columns), or edit a file the
  language server had a stale copy of; it refuses now.
- `/rewind` never deletes files that were git-ignored at the snapshot.
- Chats: a stale ↑-edit could truncate another chat; two chats started in
  the same second shared one file.
- Bounded memory: compressed pages, huge command output.
- Background jobs that fork can be stopped; MCP servers with malformed
  metadata or replies no longer break chats.
- Voice: never silent when the chosen engine is down, never reads code, no
  fake "Kokoro failed" on close; the mic can't get stuck on.
- The installer no longer installs a stale clone lying in the current folder.
- The approval gate: `rm -rf ~/`, `"$HOME"`, `/usr`, a command inside
  `bash -c`/`eval`, `curl … | sudo -E bash`, `sh -c "$(curl …)"`, writes to a
  block device, and python/node code that deletes home now all wait for a
  tick; harmless look-alikes (`which shred`, `fdisk -l`) no longer do. Saving
  a skill no longer runs it unchecked, and its card shows the script.
- sudo: the password no longer reaches the script's environment (or the chat
  via `env`), and a sudo fallback no longer runs a script twice.
- Compaction keeps the current request and plan verbatim and never covers a
  retried or edited turn; an old spending-cap card can't start a second
  stream; a settings.json with a stray comma is no longer wiped; deleting a
  running chat really deletes it; a `cd` doesn't leak between chats.
- Rendering: a link to a path with `__` or `*` in it no longer turns the whole
  reply into raw markdown; indented, `~~~` and nested code fences are code;
  reopening a long chat no longer rebuilds every message; Chat-mode code
  examples survive reopening in Agent mode; mid-task messages come back as
  your bubbles.
- runtests really runs pytest-style tests (a failing `assert` used to
  "pass"), from the project root, with a single time budget; ESLint 9
  projects are diagnosed; the verifier compiles, not just parses; the
  ledger redacts secrets; a damaged update download can't jam the updater.

## 16.0.0 — steer it, trust it, keep it running

The second roadmap (`docs/ROADMAP.md`, phases 5–9), shipped item by item with
tests in `tests/test_v16.py`.

### Steer it and trust it

- **Steer mid-task.** Enter while Chuck works queues your message; it reaches
  the agent at its next step and shows as a queued bubble until delivered.
  Undelivered messages go back to the composer on Stop.
- **Plan mode.** The **Plan** toggle in the composer (or `/plan`): Chuck
  investigates read-only and proposes a plan card with **Approve & run** and
  **Revise**. Changing tools are refused until you approve. A proposed plan
  shows no step as under way.
- **Retry and edit.** Ctrl+R (or `/retry`) asks the last question again; ↑ in
  an empty composer edits your last message and resends from there. A retry
  rewinds that turn's tool results too.
- A reopened chat now shows what the machine did (tool results, folded)
  instead of only the conversation.

### The workflow around the code

- **Git:** `/diff` (the working tree as a card, no model call), `/commit`
  (Chuck stages the right changes and writes the message from the diff) and
  `/review` (a bug review of the uncommitted diff that changes nothing).
- **Hooks** in Settings → Agent: a command after every file write (`{path}`,
  optionally filtered by glob) and one Chuck must pass before he may finish.
  Results go to the model; a hook that never passes stops after three rounds.
- **A persistent Python session.** ```` ```py ```` blocks share one
  interpreter per chat: data loaded once stays loaded, the last value is
  shown, matplotlib figures come back as images (to the model too, when it
  can see). Output streams live and tracebacks point at the block. Stop sends
  Ctrl+C and the variables survive; a kernel that ignores it is killed and
  the next block says it started fresh. `input()` fails fast. The verifier
  knows the session's names, so building on an earlier block isn't flagged.
  ```` ```python ```` stays a one-shot script. A new chat, switching chats or
  closing Chuck ends the session.

### The web as it really is

- **Pages that refuse a plain fetch are read through the browser.** A bot
  wall, a 403 for anything that isn't a browser, or a page that is empty
  without JavaScript is fetched again in Brave, Chromium, Chrome or Camoufox,
  which waits up to 10 s for the check to clear and reads the article text.
  A wall that never clears is reported as blocked, not returned as content.
  At most two browsers at once; 404s aren't retried. It can be turned off.
- Headless Chromium no longer announces itself as `HeadlessChrome`.

### Operations

- **`/doctor`**: one card checking the key and balance, model, working folder,
  browser, linters, Node, screenshots, voice, MCP servers, disk and recent
  errors, each problem with its fix. No model call.
- **Crash log**: uncaught errors (UI thread and worker threads) and tool
  failures go to `~/.local/state/chucknorris/chuck.log` with a timestamp,
  version and traceback; rotates at 1 MB. Never keys, settings or chats.
  Settings → Agent → Error log opens it.
- **Updates**: a daily check against GitHub (Settings → General can turn it
  off) and a toast with **Update**. `/update` installs without sudo: a git
  clone gets `git pull --ff-only` (local commits are never overwritten); an
  installed copy downloads one archive, verifies every module, including the
  ones `config` imports, is present and compiles, then swaps it in.
  **Restart now** saves the chat and reopens it in the new Chuck.

### Fixes

- Toasts sit over the transcript only, no longer over the composer.
- "Working on it…" no longer stays on a step that only ran tools.
- Money shows as `$0.50`, not `$0.500`.
- An exported chat shows what you typed, not the internal notes prefix.
- The byline after a fallback names the model that answered.
- A file card for a write that changed nothing says "no change" and has no
  Revert.
- The plan nudge can no longer spend three paid calls on a reply that only
  talked.
- Narrow windows: the sidebar collapses below 900 px and secondary header
  buttons hide below 680 px; the welcome screen reflows instead of clipping.

## 15.1.0 — Brave and Camoufox

- **Brave is the preferred browser.** Automatic mode looks for Brave first
  (native, `/opt`, Flatpak, Snap), then Chromium, then Chrome, all driven over
  the DevTools protocol.
- **Camoufox** — a Firefox build that sites can't fingerprint — through its
  Playwright API, with the same results as the Chromium path: uncaught errors
  with file and line (Firefox stack traces parsed), console errors and
  warnings, failed requests and HTTP errors, every action, the visible text and
  a screenshot.
- **Settings → Agent → Browser** chooses Automatic, Brave, Chromium, Chrome or
  Camoufox and shows which binary is in use. A path to any browser binary
  works too (in `settings.json`), and an unavailable choice says what to
  install. `CHUCK_CAMOUFOX=1 ./install.sh` installs Camoufox.
- The browse report and card name the browser that ran.
- A missing file loaded by a page is reported once, as a failed request,
  not also as a console error.

## 15.0.0 — a modern harness

The plan is in `docs/ROADMAP.md`, ticked item by item as it shipped. Every
item has tests in `tests/test_v15.py` (new) and is exercised end to end in
`tests/test_deep.py`.

### The model always has the right context

- **Project rules.** `CHUCK.md`, `AGENTS.md` or `CLAUDE.md` in the working
  folder, plus `~/.config/chucknorris/CHUCK.md` for everything, ride in the
  cached system prompt. They reload when edited, when the folder changes, and
  the moment the agent writes one. `/init` has the agent survey the project
  and write its `CHUCK.md`.
- **`@file` mentions** attach files from the working folder (images as
  images).
- **The shell remembers `cd`** between commands, like a terminal. The model is
  told when the shell has left the working folder. Error line numbers are
  unchanged.
- **Slash commands:** `/help /init /clear /compact /model /undo /agent /chat
  /folder /cost /memory /export /mcp`.

### New tools

- **`browse`** — a real headless Chromium, driven over the DevTools protocol
  with no dependencies. It loads a URL or file at any size, and can click,
  type, press keys, scroll and evaluate JS. It reports uncaught exceptions and
  console errors with file and line, failed requests, the page text and a
  screenshot, in a BROWSER card you see too. Building a web game and finding
  its bugs is one step.
- **`diagnose`** — LSP-lite. Lints and type-checks files on disk with the
  tools installed: ruff/pyflakes plus mypy/pyright, tsc, eslint, node, and
  shellcheck. In a Cargo or Go project it also runs `cargo check` / `go vet`;
  JSON and TOML are parsed. Missing tools are named, so "no problems" never
  means "nothing checked".
- **`patch`** — multi-file unified diffs, all-or-nothing. Hunks are located by
  context, so wrong line numbers don't matter. A hunk that doesn't match
  changes nothing anywhere.
- **`changes`** — the net diff of everything this task changed, for
  self-review before reporting.
- **MCP client** — Model Context Protocol servers from `mcp.json` (Claude
  Desktop's format). Their tools are listed to the model and called with
  ```mcp server.tool```. Text, images, errors, roots and pagination are
  handled, and a server that fails to start says why. There's a Settings
  section and `/mcp`.
- **Live command output** — long commands stream into a LIVE panel in their
  card with a running clock. Finished logs open on their end, where the
  errors are.

### Reliability and cost

- **Model fallback** — a model that is down past its retries, or no longer
  exists, hands that request to a fallback model, with a note in the chat. A
  dead network or a bad request never triggers it.
- **Spending cap** — an optional per-chat cap pauses the task and asks before
  spending more.
- **Auto-compaction** — long sessions are summarised by the model instead of
  silently losing their oldest turns. The transcript stays complete.
  `/compact` runs it on demand.
- **`/export`** — a chat as readable Markdown.

### Foundations and fixes

- A **tool registry**: a new tool is one handler. Parsing, caps, Chat-mode
  gating, holding a look until an action has run, and step-budget filtering
  are shared.
- Fixed: with no working folder, every command claimed "the shell is now
  in …".
- Fixed: `test_autonomy`'s `-Syu` check only passed because the prompt
  contained "-Syu". It now checks the executed command.
- The prompt stays under 4,000 characters with the new tools documented.
  The MCP tool list costs nothing unless servers are configured.

## 14.1.0 — it finishes the job

The headline bug: asked to write a game, Chuck said *"I'll write the game now"*
and stopped. This release makes the agent work the way a careful engineer does:
it keeps going until every part of the task is done, checks that what it built
actually runs, and stops early only to ask you something it can't decide alone.
Covered by `tests/test_completion.py` and `tests/test_audit.py` (both new).

### Finishing what it starts

- **Announce-then-stop is caught.** A reply that promises an action ("I'll
  write…", "Next, let me…") but carries no tool block is sent back to actually
  do it. Code *printed* in a reply instead of written to a file is caught the
  same way. Bounded: at most two such nudges per task.
- **Open plan steps are carried through.** If the agent tries to finish while
  its ```plan``` still has unchecked steps, it's sent back to do the next one
  (up to three times per task).
- **Changed code is run before "done".** Code files written or edited since the
  last command, test run or screenshot get one "verify it now" round before
  the final report. Docs-only changes don't.
- **```ask```** — a QUESTION card with one-click answers, for decisions that
  really are yours (an unclear requirement, a risky trade-off). The task pauses
  until you answer, by clicking an option or typing your own. A reply that ends
  in a question is never nudged either.
- None of this fires in Chat mode, after you press Stop, or past the step
  budget, now 60 by default (4–200 in Settings).

### More tools

- **Checkpoints.** Every ```write```, ```edit``` and ```rmfile``` is a card with
  its diff and a **Revert** button. ```undo``` reverts the last change and
  ```undo all``` reverts every change in the current task. When you revert
  something, the agent is told.
- **Background jobs.** ```bg <name>``` starts a dev server or watcher detached
  in the working folder. ```logs <name>``` reads its output and ```kill
  <name>``` stops it. A live card shows uptime and has a Stop button.
  CRITICAL commands and password-needing sudo are refused as jobs, and closing
  the window stops them.
- ```open <path|url>``` hands you a result in your browser or default app.
- **Playbooks** for debugging, TDD, review, git, UI, performance, security and
  dependencies, ranked by how well they match the task. **Dev skills**:
  toolchain, project survey, git overview, TODOs, big files, Python env, test
  runner discovery, who-holds-this-port.

### Nothing gets in the model's way

- **Every command failed with `fork: Resource temporarily unavailable`.**
  Commands ran with `RLIMIT_NPROC=512`, but that limit counts every task the
  user owns, the whole desktop included, and a normal Plasma session is already
  past 512. Commands now run with no process, memory or file-size caps of
  Chuck's own, exactly as they would in your terminal. The 4 GiB address-space
  cap had also stopped Chromium, Node and the JVM from starting at all.
- **Stop really stops.** Esc and *New chat* ended the turn but left the
  command running until its timeout. Every command now runs in its own process
  group, and Stop kills the whole tree within a second. This includes
  ```python``` / ```node``` programs and sudo commands, which used to lose their
  child processes on a timeout too.
- **Timeouts fit real work.** `node build.js`, `next build` and `bun test` were
  treated as servers and killed after 25 s. Now only things that serve
  (`node server.js`, `vite`, `npm run dev`) count as servers. Unrecognized
  commands get 10 minutes instead of 2, and quick ones 2 minutes instead of 30 s.
- **Code is never withheld over style.** Python verification now blocks only on
  what fails at runtime: syntax errors, undefined names, `return` outside a
  function. Unused imports and unused variables no longer block. The security
  scan stopped blocking `model.eval()`, a class's own `def eval(self)`,
  `os.system("clear")`, `token = "STRING"` in a tokenizer and
  `eval "$(ssh-agent -s)"`; it flags them instead. Real injection (`os.system`
  on a built string), `eval()` on input, and `rm -rf /` or `rm -rf ~` inside a
  program still block. `curl | sh` installers wait for your approval as
  CRITICAL commands instead of being refused outright.
- **"couldn't tell where tetris.html ends"** was a false alarm. The check for
  stray fences after a doc write counted the closing fence of the *next* tool
  block (the ```bash screenshot command). It now tracks depth there as well.
- **An absolute path inside the working folder was refused** ("give a path
  inside the project"). It's now accepted. Paths outside the folder are still
  refused.
- **`look` ran before the command that made the image.** A screenshot or look
  in the same reply as an action is now held back, and the model is told to
  look again once the action has run.
- **Shell style warnings blocked commands.** An unused loop variable was enough.
  Shellcheck now blocks on errors only.
- **Icons drew as blank squares** under themes that lack Adwaita's symbolic
  icons (Breeze, for one). Chuck uses the Adwaita icon set for its own window
  when it's installed, and the installer now installs it.

### Tests

- **`tests/test_deep.py`**: whole sessions with real commands, files,
  processes and ports:
  - a library built and debugged against real unittest output;
  - a web app served from a background job and fetched over HTTP;
  - 400-process and 800-thread commands;
  - Stop killing a running command's tree;
  - a 2,000-line file across three cut-off replies;
  - whole-repo reading;
  - parallel read-only sub-agents;
  - a 40-turn session that saves and reloads;
  - 180 seeded random model replies in both modes. Every run must finish
    cleanly; nothing CRITICAL or embedded in a document may run; nothing may be
    written outside the folder.
- Every suite now runs with its own throwaway HOME. They used to write the real
  `~/.config/chucknorris/settings.json`, and Agent mode leaked from one suite
  into the next.

### Audit fixes

- A stream from a stopped turn, or one from before *New chat*, could finish late
  and land in the next turn. Every turn now carries a generation number, and
  stale output is dropped.
- A README with its own ```bash example inside a ```write block was cut short,
  and the example ran. Writes now track nested fences. A doc block whose end is
  genuinely ambiguous writes nothing and runs nothing, and the model is told to
  resend it in four backticks.
- With the folder chip on *No folder*, files went into the project from a
  *previous* session. They now go to a fresh scratch folder, which the chip
  then shows.
- Trimming context could start the conversation with an assistant turn, which
  some APIs reject. Fixed.
- Tab flipped Agent/Chat while you were inside Settings or the chat search.
  It now only does so from the main window.
- A connection that dropped while the model was still only *reasoning* (no
  visible text yet) was treated as a finished reply and never retried. It now
  retries like any other transient failure.
- Long turns may run up to an hour, and sub-agent progress keeps the watchdog
  fed.
- Diff and output panels were collapsing to one line. They show at full height
  again.

### Look

- The glyph net is now **invisible until you click**: a ripple lights it up in
  waves, then it fades back to black. Glyphs are smaller and there are about
  twice as many, at a typical frame cost of ~3 ms.
- The QUESTION card gets a quiet accent edge, since it's the one card that's
  waiting on you.

### Install

- **Fixed: `ImportError: cannot import name 'pricing'`** right after an update.
  Fetching files one at a time from a CDN could mix versions. The installer
  now downloads one archive of the branch, checks that every module the app
  imports is present, and compiles them. Optional modules are imported one by
  one, so a missing one disables only its own feature.

## 14.0.0 — the harness

A rebuild of the window and a much larger toolset, aimed at real coding work.
Everything below is covered by `tests/test_v14.py` (new) or verified by
rendering the real window on GTK 4.14 / libadwaita 1.5.

### Modes and the working folder

- **Agent / Chat**, toggled with **Tab** from anywhere in the window, shown as a
  switch in the composer. Chat mode has its own 0.7k-character prompt and never
  touches the machine: ```bash``` fences render as copyable code instead of
  running, and agent-only tools are refused with a note.
- **Working folder per session** (folder chip or Ctrl+O): commands run there,
  file tools resolve relative paths there, and each request carries a capped
  one-line map of its top level. ```project``` now sets the working folder too,
  so there is one notion of "where we are". Saved chats remember their folder.

### Tools

- ```edit``` — Aider-style SEARCH/REPLACE, all-or-nothing, whitespace-tolerant,
  re-verified after applying. Changing three lines no longer costs a whole file
  of output tokens.
- ```map``` (repo map with definitions; honours .gitignore), ```glob```,
  ```grep``` (ripgrep when present). Read-only tools batch into one round trip.
- ```read path:start-end``` — big files arrive in pages with an exact pointer to
  the next page. **Fixed:** reads were silently cut at 24,000 characters.
- ```plan``` — a live checklist card updated in place.
- ```screenshot``` / ```look``` — the agent sees the screen or any image file.
- ```task``` — **parallel read-only sub-agents** (`chucknorris_ext/agents.py`),
  each with its own model loop, live progress card and folded report.

### Long work no longer breaks

- **Fixed: files longer than one reply were never written.** A reply cut at
  `max_tokens` left its ```write``` block unclosed. Replies that hit the limit
  are now continued automatically and stitched (with repeated overlap trimmed)
  before any tool is parsed. Default output ceiling 1600 → 8192.
- **Fixed: an agent stopped at its fifth step.** Agent steps shared the
  research-round budget (4). They now have their own (25, settable) and a spent
  budget asks for a final report instead of going silent.
- Tool results per round 12k → 60k characters, command output 6k → 20k with
  head+tail clipping (the error at the end of a build log is kept), context
  budget 60k → 160k characters and settable.

### Vision and attachments

- Attachment tray: multiple files, drag-and-drop onto the window, **Ctrl+V
  pastes images** and copied files. Images go to the model as images (resized to
  1568px), text files as contents, anything else by path. History keeps text
  only, so an image is billed once.
- Images are routed to the vision model when the chat model can't see.

### Models and money

- **Real usage**: the meter books prompt, cache-hit, completion and reasoning
  tokens as the API reports them (`stream_options.include_usage`, with a
  fallback that triggers only on a 400 that actually names it), priced per model
  in `chucknorris_ext/pricing.py`. Popover breakdown, session total, live
  account balance (`/user/info`).
- Model menu in the composer: GLM-5.3-Flash (default, multimodal),
  DeepSeek-V4.1-Flash, DeepSeek-V4-Flash, GLM-5.3, and the free tier. Settings
  can fetch the live model list for your key; custom prices supported.

### Memory

- Newer facts about the same subject **supersede** older ones (archived to
  `superseded.jsonl`, not deleted). Facts saved as `project:` are scoped to the
  working folder and recalled only there.

### Prompt and efficiency

- Agent prompt 5,708 → ~3,500 characters while documenting more tools; at most
  two playbooks per request and none in Chat mode; the volatile note stays last
  so the prefix caches.

### The window

- New dark palette with a single violet accent used as an overtone. The
  background image is gone.
- The **work lane**: commands, outputs, notes, plans and sub-agents hang on a
  rail off Chuck's avatar, inset from his prose. One byline per turn.
- Live markdown while streaming, with real tables and code blocks that scroll
  sideways (they used to overflow). Reasoning is folded under the reply.
- Welcome screen with mode-aware suggestions over an interactive net of code
  glyphs that ripples where you click (zero idle cost; honours reduce-motion).
- Native libadwaita Settings (live-apply, no Save button), redesigned memory and
  working-folder dialogs, chat search and delete in the sidebar, toasts,
  keyboard shortcuts (Ctrl+N/O/B/, , Esc).
- **Scrolling fixed properly.** The view follows new output only while you are
  at the bottom (scroll up and it stays put; *Jump to latest* brings you back).
  Three real bugs behind the old jumps: the bottom was set before layout so it
  landed short; GTK's scroll-to-focus yanked the view back to any selected
  label on every streamed repaint; and every new message tore down and rebuilt
  the whole transcript. The transcript is now reconciled in place, the pin is
  applied after layout, scroll-to-focus is off, and wheel events over code and
  output panels chain to the page.
- Fixed a CSS specificity bug that forced code headers and table cells to body
  text size; fixed libadwaita group titles with "&" failing to render.

### Other fixes

- **The JS verifier blocked every clean file on ESLint 9+** (`--no-eslintrc` and
  the `compact` formatter were removed) and, once that was fixed, linted
  nothing (ESLint 9 skips files outside its base path). It now detects the
  version and lints over stdin with a defect-only rule set.
- The curl installer now fetches `pricing.py` and `agents.py`.

## 13.1.0 — a face worth looking at (properly this time), and a cheaper mouth

### The look

The previous pass fixed the line spacing and the palette. It did not fix the
thing that actually made the window hard to read: **terminal output and Chuck's
prose were the same material.** Real stdout was appended to the transcript as a
bare monospace label, a sibling of the message bubbles, with nothing marking it
as machine output and no height limit — so a 4,000-line build log buried the
answer that followed it.

- **Output is now a terminal panel.** Black ground, a status strip (`OUTPUT`,
  true line count, copy, hide/show), height-capped at 260px and scrollable.
  Lines are deliberately NOT wrapped, so `ls -l`, `df` and `systemctl` keep
  their columns and you scroll sideways instead of watching them fold into mush.
  Output over 12,000 characters is clipped, and the header reports the real
  total rather than the post-clip count.
- **Everything the machine did is a card; nothing Chuck says is.** Commands,
  code, videos and packages share one shell: a titled bar (`COMMAND`, `BASH`,
  `PYTHON`, `PACKAGE`) with a copy button, the payload, then the result. The
  output panel now lives INSIDE its card, so a command and what it printed are
  one object instead of two floating widgets.
- **Exit codes are status pills** — `exit 0` green, `exit 1` red, `running`
  clay, `needs approval` on a gated card — instead of a grey word at the end of
  a row.
- **Replies carry a byline.** Chuck answers as open text rather than in a bubble
  (better for long answers), which previously left nothing marking where his
  voice began. Plus a copy button beside read-aloud.
- **System asides sit in a gutter** — "verifying bash", "added --noconfirm" —
  legible as machine chatter at a glance.
- **A Claude-family palette**: layered warm-neutral surfaces and ONE clay accent
  reserved for things you can press. Gold was spent on decoration before, which
  left nothing to signal an action; it is now the wordmark and nothing else.
- **Headings carry hierarchy by size, not colour.** H1/H2/H3 are distinct sizes
  in the same ink — every heading used to be the same large gold, so a
  three-level answer read as three unrelated shouts. Adds `---` rules, clickable
  `[links](url)`, strikethrough and nested-bullet indentation, and fixes inline
  code containing `*` being re-read as italics.
- **Malformed markup can no longer blank a reply.** `set_markup` does not raise
  on bad markup: it warns and leaves the label showing the PREVIOUS message.
  Verified against real GTK 4.14. Markup is now validated with Pango first and
  falls back to plain text, with `<a>` stripped for the probe because GtkLabel
  handles links itself while bare Pango rejects them.

### The bill

Measured, not guessed: a real three-hop turn was captured through the test
harness. One question cost **3 API calls and ~6,255 input tokens, of which 87%
was the system prompt re-sent on every hop.**

- **`SYSTEM_PROMPT` was declared `r"""` but written with `\` line
  continuations.** In a raw string those are literal backslashes: **44 stray `\`
  characters were shipped to the model on every single call.**
- **Prompt rewritten: 7,161 → 5,708 characters (~20%)** with no instruction
  dropped — backslashes gone, the humour section's four restatements of one idea
  merged, tool list compressed while keeping the newline form for `check`,
  `write` and `skill` where the parser requires it. Same question now costs
  ~4,959 input tokens.
- **`max_tokens` is set (default 1600, settable, clamped 256–8192).** The
  request had no output cap at all, and output tokens cost several times input.
- **A live meter in the header subtitle** — `≈ 4.9k in · 1.2k out · this chat`,
  reset on New chat. Token counts are honest estimates (chars/4); the real
  tokeniser is server-side.

Still on the table: prompt caching. The prefix is already stable and the
volatile date/skills note is correctly appended last, but `_trim_for_send` drops
messages from the FRONT when the conversation passes 60k characters, which
shifts the prefix and invalidates the cache from that point on.

All sixteen suites pass. The stylesheet was parsed by a real GTK 4.14 CSS
engine (including the font slider at both extremes) and the markdown renderer
was fuzzed against real Pango.

## 13.0.0 — a face worth looking at, and a sharper brain

### The model

- **Default backend is now `zai-org/GLM-5.3-Flash` (Z.ai), for both chat and
  vision.** It replaces `deepseek-ai/DeepSeek-V4-Flash`, which had been treated
  as a fixed invariant. The swap is a straight upgrade: GLM-5.3-Flash tops
  SiliconFlow's intelligence board, is built for coding and long-horizon agent
  work, and is *natively multimodal* — so it also retires the separate
  `Qwen2.5-VL` vision model. One model, one bill. Blended cost with prompt
  caching sits at or below V4-Flash, which is what a tight agent loop actually
  pays, so it is both stronger and cheaper. It needed no wiring change: the API
  is OpenAI-compatible and the stream reader takes only `delta.content`, so the
  model's reasoning never leaks into a reply. Anyone who wants the old model
  types it back into the Chat-model field in Settings. `tests/test_startup.py`
  now pins the new default.

### The look

The report was blunt and correct: it looked bad and the text was a wall. Root
cause was mechanical, not taste — GTK labels have no line spacing by default, so
every reply rendered as cramped lines with no air, over a flat near-black theme
with a muddy mustard accent and harsh pure-white body text.

- **Real line spacing** on every message, via a font-size-independent Pango
  line-height attribute, so the Settings font slider still works. This is the
  single biggest change to how a reply reads.
- **A proper palette**, defined once as named colours so surfaces stay in tune:
  layered card backgrounds that actually separate from the page, a brighter
  gold accent with subtle gradients on the user chip and the Run/Send buttons,
  a gold accent bar down the side of command cards, and softened body ink.
- **Typography** — Inter/system UI for chrome, JetBrains Mono for code,
  letter-spaced title and section labels, gold headings.
- **A rewritten markdown → Pango renderer.** Parsed a line at a time, so a lone
  `#` or `-` mid-sentence is no longer mistaken for a heading or bullet. Adds
  numbered lists and block-quotes, renders inline code as a subtle chip instead
  of bare backticks, and HTML-escapes first so `<`, `>` and `&` are always safe.

All sixteen suites pass.

## 12.1.0 — the gaps, and the handshakes

Every item below was reproduced first and is locked shut by a named check in
`tests/test_v13.py`. The 12.0.3 suite passed throughout — none of these were
covered by it.

### Things that were shipped broken

- **The code verifier did nothing without `ruff`.** `_lint_python`'s stdlib
  fallback called `compile()`, which only repeats the syntax check that already
  ran a step earlier — so on a machine with no linter installed the entire lint
  stage was a no-op that could never return a finding, and `x = undefined_name`
  reached a Run button. The README's "no runtime dependency beyond the stdlib"
  was true of the code and false of the guarantee. There is now a real stdlib
  AST analyser behind it (undefined names, unused imports), deliberately
  conservative: it flattens scopes rather than modelling them, and stands down
  entirely on a star-import or a visible `exec`/`globals()`. It can under-report;
  it is not allowed to false-positive, because a false finding withholds working
  code and loops the model over nothing to fix — the md5 failure of 12.0.1 in a
  different coat. 26 valid constructs are in the suite as proof.

- **The evidence ledger could not be read.** `ledger.summary()` carried a
  docstring promising a ` ```ledger ` request that did not exist anywhere — no
  tool tag, no parser branch, no button. Every command since 12.0.3 was recorded
  into a file nothing could display, and `verify()` was equally unreachable. A
  record you cannot inspect is not evidence. There is now a ` ```ledger ` tool
  and a Settings panel with Re-verify.

- **The proxy did not cover the model API.** `Backend.stream` built its request
  through `urllib.request.urlopen` directly, bypassing the opener where the
  proxy lives. Page fetches, images and `yt-dlp` all went through the tunnel;
  every prompt the user typed went out over the bare connection. That is exactly
  backwards, and the settings panel said "Proxy for web, images and video"
  without ever saying so. The API is now proxied by default whenever a proxy is
  set, with an explicit opt-out, and warm-up targets the proxy rather than
  opening a direct connection to the API host.

- **The one-line installer had been fetching an incomplete app since 12.0.3.**
  The curl path fetches a hand-written list of modules and `ledger.py` and
  `compress.py` were never added to it — so every `curl | bash` install ran with
  no evidence ledger and no context compression, while a git clone had both, and
  nothing anywhere reported the difference. Both added; a suite check now
  compares the list against what is actually on disk so it cannot drift again.

- **Memory could not answer the most obvious question you can ask it.** Facts
  are stored as instances ("User runs CachyOS on a ThinkPad X395") and people
  ask by category ("what distro am I on"), which shares not one token with it —
  so the store held the answer and recalled nothing. A small hand-written
  category map now bridges the two at half weight, so a real token match always
  beats a bridged one. Coverage is also measured against the *answerable* part
  of a question: a word appearing in no fact and bridging to nothing was
  diluting the score of the words that did match.

### Speed

Numbers are from the suite, old path against new.

- **New `net.py`: connections are pooled and kept alive.** urllib has no
  connection reuse, so every request paid a fresh TCP and TLS handshake. A
  research round probing four SearXNG instances and reading three pages paid
  seven of them. Measured: six requests to one host now open **one** connection
  instead of six. Redirects are followed, 4xx still raises `HTTPError` so every
  existing `except` behaves identically, the http/https allowlist is enforced in
  the new path too, and any trouble inside it falls back to plain urllib rather
  than surfacing an error the old code would not have produced.

- **Page reads are cached for 90s** (settable, 0 disables). Re-reading the same
  URL across hops of one turn now costs nothing. POSTs are never cached.

- **Searches within a round now run concurrently.** The page fetches were
  already parallel; the searches feeding them ran strictly one after another, so
  two queries against a slow instance cost the sum of both before the first page
  was fetched. Measured: two 0.6s searches complete in **0.60s**, not 1.2s. The
  ordered repeat-query check stays serial (it has to — it compares against
  queries already accepted this round) and results are merged in ask-order, so a
  fast follow-up cannot crowd out the main question.

- **The ledger no longer re-reads itself to append.** `_last_hash()` walked
  every line of the file on every single command, so the cost of running a
  command grew with the number of commands ever run. Now cached against the
  file's identity with a tail-seek on a cold read: measured **zero** reads per
  append. It also rotates at 8MB — via an anchor, so the chain still verifies
  across the rotation rather than silently restarting — and `record()` is
  serialised, so two commands finishing together can no longer write two entries
  claiming the same predecessor and break the chain by accident.

- **Memory stopped rewriting the whole store every turn.** `recall()` re-read,
  re-parsed and re-scored the file on each message, then rewrote it end to end
  just to increment a hit counter nothing reads until the store overflows 400
  facts. Now cached against mtime with stems and IDF memoised, and counters
  batched with a flush on close. Measured: ten recalls now perform **zero**
  re-parses and **zero** writes.

- **Machine facts are probed once, in parallel, at startup.** `gather_context()`
  fired five subprocesses on the GTK main thread — one of them a full
  `pacman -Qq` walk of the local package database — every time a chat was
  created. That is a visible freeze on every New chat for an answer that was
  already known. Now primed in the background at launch and cached for the
  session.

- Smaller: `shutil.which` memoised in the verifier and the voice pipeline (a
  long reply was doing dozens of PATH walks per spoken answer), the Piper model
  resolved once rather than per chunk, the security patterns compiled at import
  rather than resolved per line, and TTS lookahead raised from one chunk to two
  — at a depth of one the producer could not start chunk N+2 until chunk N had
  finished *playing*, so every synthesis after the first was serialised behind
  real-time audio and any slow chunk was audible as a gap.

### Also

- Settings gained **temperature**, **proxy-covers-API**, **page-cache seconds**
  and an **Evidence ledger** section with Re-verify and Open folder. Changing
  the proxy now drops every pooled socket, because a connection opened before
  the change is still a direct connection and reusing it would quietly defeat
  the setting that was just changed.
- Closing the window now stops speech (a daemon thread kept feeding `paplay`,
  so closing mid-answer left a disembodied voice finishing the reply) and
  flushes the batched memory counters.

## 12.0.1 — audit fixes

Every item below was reproduced against `tests/gtkstub.py` before it was fixed,
and is locked shut by `tests/test_v12_fixes.py`. The v12.0.0 suite passed
throughout — none of these were covered.

### Broke outright

- **`videos` tool was 100% dead.** `web.video_search()` returns 3-tuples
  `(title, url, snippet)`; both call sites in `_do_video_search` unpacked two, so
  every ` ```videos ` block died with `ValueError` before drawing a card and the
  model was fed the exception back as its tool result. Call sites corrected and
  the 3-tuple contract written into the function's docstring.

- **Synchronous tools ended the turn mid-dispatch.** `_finalise` incremented
  `_pending_tools` one at a time *inside* the dispatch loop. `_do_read` (project
  file / image path) and `_do_runtests` (no project) call `_tool_done()` inline,
  dropping the counter to zero while later tools were still queued — firing
  `_continue_or_finish` early. Two ` ```read ` blocks produced **three** model
  round-trips and two "final" answers. The counter is now reset first, totalled
  for the whole turn, then dispatched, with a `_dispatching` guard holding the
  turn open for the duration.

- **`_pending_tools = 0` wiped the skill cards it had just launched.** The reset
  sat *below* the skill loops, so a ` ```runskill ` card that had already taken a
  slot had it zeroed underneath, along with its "waiting for the user to confirm"
  feedback. Reset moved above the loops.

- **Any command over 2 minutes was shot as "stuck".** `STUCK_AFTER = 120` with
  nothing pinging `_note_progress()` while a command blocks its worker, so
  `pacman -Syu`, `makepkg` and large installs were cancelled — and their real
  output then discarded on arrival, because `_tool_done` early-returns on
  `_cancelled`. Meanwhile `estimate_runtime` had granted the same command 1800s.
  The watchdog now uses the running command's own budget (`_arm_run_budget`), and
  `RUN_HARD_CAP` stretches to cover it.

- **New chat / open chat mid-answer poisoned the destination.** Neither called
  `stop_run()`, so the in-flight stream's `_finalise` appended the old question's
  answer to the new history — a chat beginning `[system, assistant]` with no user
  turn, which was then sent to the API and written to disk.

### Wrong behaviour

- **`pacman -Rs` and `-Rsn` hung and timed out.** The `_NEEDS_NOCONFIRM`
  lookahead listed a fixed set of spellings that happened to contain `Rns` but
  not `Rs` or `Rsn` — the two forms people actually type. With `stdin=DEVNULL`,
  pacman hit `[Y/n]`, got EOF and aborted. Now matched as a flag cluster.

- **`enforce_syu` never saw `paru`/`yay`, or any skill body.** It was anchored on
  `\bpacman\b`, so every AUR install slipped through un-rewritten; and
  `_command_card` ran it on the `bash /path/skill.sh` wrapper rather than the
  script. Both fixed — a skill body is now rewritten on disk so what runs is what
  was checked.

- **`hashlib.md5()` permanently blocked the Run button.** `res["ok"]` required an
  empty security list, and the patterns include `\bmd5\b` and `verify=False`. A
  checksum script could never reach a card, and the model was told "fix every
  issue" with nothing to fix — a loop until the hop budget ran out. Patterns are
  now tagged `BLOCK` or `ADVISE`: injection/RCE still withholds the card,
  broad-but-real footguns are reported beside it and in the report.

- **Startup purge ignored the TTL setting.** `purge_old_chats()` with no argument
  fell back to the shipped 24h, so raising "auto-delete after" to a week still
  lost everything over 24h on the next launch. The timed sweep already used
  `cfg()`; the launch purge now does too.

- **`run_tests` ran one test file.** Without pytest, builder returned
  `["python3", <first test>]` and reported "tests passed". Now every discovered
  command runs and all output is kept.

- **Sidebar refresh could crash on a race.** `chat_files()` sorted by
  `p.stat().st_mtime` *outside* its `try`; the retention sweep unlinking a file
  between listing and sort raised `FileNotFoundError` into the GTK callback.

- **`install.sh` used `-Sy`** — the exact partial-upgrade footgun the app exists
  to rewrite away. Also fixed in the shipped skill library, the `junk_scan`
  cleanup hints and the Arch playbook in `specs.py`.

### Smaller

- Removed dead `_opener()` / `_get()` from `chucknorris.py` (all network goes
  through `config.get`, where the http/https allowlist lives) and 5 unused
  imports.
- Screenshots of the desktop, interpreter temp files and fetched images no longer
  accumulate in `~/.config/chucknorris`; `_sweep_scratch()` clears any backlog at
  startup.
- Voice wav filenames use a counter, not `hash(chunk)` — a repeated sentence made
  two chunks collide and the consumer's unlink silenced the second.
- `skill_run_cmd` shell-quotes its path (`$HOME` can contain a space); re-saving a
  skill in another language archives the stale body instead of leaving it
  runnable.
- Memory writes are serialised behind a lock (`recall()` rewrites the whole store
  every turn while `remember()` may be doing the same), and fact ids no longer
  collide on the ~11.6-day wrap of `int(time.time()*1000) % 10**9`.
- `Enter` while a turn is running inserts a newline instead of vanishing.
- `download_image` reads are capped at 12 MB.

## 12.0.2 — sudo storm

**Reported symptom: ~15 sudo dialogs at once, none of them typeable.**
Reproduced against the old code: 15 concurrent requests raised 15 dialogs.

Three compounding causes, all fixed:

- **No single-flight on the prompt.** `_get_sudo_pw()` was a bare
  check-then-prompt. Every worker that needed root checked the empty cache at the
  same moment, all saw it empty, and each called `GLib.idle_add` on its own
  `Adw.Window(modal=True, transient_for=self)`. N commands meant N stacked modals
  contending for one keyboard grab — which is exactly why none of them accepted
  typing. Now one caller opens the dialog and every other blocks on a condition
  variable and takes that answer. A cancel is remembered for the rest of the
  turn, so refusing once doesn't produce a second dialog.

- **Nothing serialised execution.** `_execute_shell` had no lock, so cards ran
  concurrently — interleaved output, and a second prompt arriving mid-run. One
  command now runs at a time, process-wide; a queued command picks up the
  password the one ahead of it already collected.

- **The one-command cap didn't cover skills.** `codes = codes[:1]` capped
  ```` ```bash ```` blocks only. ```` ```runskill ```` and ```` ```skill ````
  bypassed it, so a reply with five skill blocks launched five commands at once.
  The cap now pools every runnable source and keeps one, handing the rest back to
  the model for the next turn.

Also: `_awaiting_input` is depth-counted (a plain bool let the first waiter to
finish un-pause the watchdog while others were still blocked); the password entry
explicitly grabs focus; and cancelling now returns a clear "cancelled — nothing
was changed" rather than running the command password-less and failing obscurely.

`test_safety.py` check 11 was asserting on the literal source string
`codes = codes[:1]`; it now asserts the cap covers every runnable source.

## 12.0.3 — audit round two, plus the upgrade list

### Two more constant-vs-setting inconsistencies

- **Stop didn't stop the research loop.** `stop_run` set `self._hops =
  MAX_TOOL_HOPS` with a comment claiming it blocked further hops — but the cap
  it's compared against is `cfg('research_hops')`, settable up to 8. With the
  constant at 4 and the setting at 8, Stop set the counter to 4 < 8 and blocked
  nothing. Swept every other constant/setting pair; the rest were legitimate
  defaults.
- **The sidebar lied about retention**, hardcoding `auto-delete after 24h` from
  the constant while the real TTL is a setting. Now reads the setting and
  refreshes when it changes.

### From the upgrade list

- **Config permissions (#6).** `save_settings` used `write_text()` — 0644 on a
  normal box, so the SiliconFlow key was readable by any account on the machine.
  Now opened 0600 from the descriptor (no chmod window), written to a temp file
  and renamed; `CONFIG_DIR` forced to 0700; existing installs hardened at launch.
- **Evidence ledger (#5).** New `ledger.py`: append-only JSONL at 0600, each
  entry carrying the SHA-256 of the previous one, hooked into `_execute_shell` —
  the single choke point, so nothing runs unlogged. `verify()` reports the first
  index where the chain parts. Tamper-EVIDENT, not tamper-proof: anyone who can
  write the file can rewrite the chain. The guarantee is against accidental loss
  and casual after-the-fact editing.
- **Context compression (#4).** New `compress.py`, applied before the fit-or-drop
  pass so bulky blobs are squeezed rather than discarded whole. Extractive only —
  head, tail and any error/warning lines kept verbatim, nothing paraphrased, no
  second model call. The two newest tool blobs are never touched. Measured
  17,968 -> 1,247 chars with the error line intact.
- **Memory recall (#2).** IDF-weighted scoring with conservative stemming,
  recency and hit-count damping, replacing raw token overlap. Coverage gates,
  IDF ranks — gating on IDF collapses in a small store, which is what broke
  `test_sim` S10 on the first attempt. Stopword list extended: with a few hundred
  short facts, a word appearing once looks maximally distinctive, so filler like
  "like" scored as strongly as "nvidia".
- **Process containment (#3, corrected).** Chuck's note said skills "run raw
  in-process" and wanted restricted builtins. They don't — they already execute
  as `bash /path/skill.sh` via subprocess, so a buggy skill can't crash the app
  and restricted builtins would achieve nothing. The actual gap was that children
  inherited no limits: commands now get their own process group plus RLIMIT_NPROC
  / RLIMIT_AS / RLIMIT_FSIZE / RLIMIT_CORE, and a timeout kills the whole tree.
  `subprocess.run`'s timeout only kills the direct child — measured, a
  backgrounding command left 2 orphans before this change and 0 after.
- **Humour.** The old instruction supplied one template and one example, so every
  fact came out the same shape. Rewritten: roughly one reply in four, must be
  specific to what just happened, several forms to rotate between, never used to
  soften bad news, and an explicit instruction to write nothing rather than
  reach for a stock line.

### Not done

- **Autonomous mode (#1)** — dropped at the user's request. It was being built
  SAFE-tier-only regardless; an unrestricted auto-run flag on a tool with an
  `rm -rf` classifier is not compatible with "no holes where destructive commands
  can run".
- **Desktop automation (#7)** — screenshots already work (grim / spectacle /
  gnome-screenshot). Only synthetic input is missing, and ydotool needs uinput
  access. Left alone pending an explicit decision.
