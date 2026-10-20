# Chuck roadmap

## v15 — from v14.1 to a modern agent harness (done)

A living plan. Each item ships on `main` with tests when it's done, and is
ticked here in the same commit. The guiding rule is the one learned the hard way
in 14.1: **the harness serves the model, it never gets in its way.** Every new
tool has to be cheap in prompt characters, safe by construction, testable
without a network, and visible to the user as a card.

### Phase 0 — foundations

- [x] **Tool registry.** New tools declare a tag, a handler, and whether they
      are read-only or agent-only. The reply parser dispatches them, counts
      them, gates them in Chat mode and batches read-only ones automatically.
      Adding a tool stops meaning edits in eight places.

### Phase 1 — the model always has the right context

- [x] **Project instructions.** `CHUCK.md`, `AGENTS.md` or `CLAUDE.md` in the
      working folder (and `~/.config/chucknorris/CHUCK.md` for global rules)
      are loaded into every request, capped and cache-friendly.
- [x] **`@file` mentions.** `@src/app.py` in your message attaches that file;
      Tab-free, works with paths relative to the working folder.
- [x] **Persistent shell directory.** `cd` in one command carries to the next,
      the way a terminal does, and resets when the working folder changes.
- [x] **Slash commands.** `/help /clear /compact /model /undo /agent /chat
      /folder /cost /memory /export` in the composer.

### Phase 2 — tools a modern coding agent needs

- [x] **`browse`**: render a page or local HTML file in headless Chromium.
      Returns a screenshot (seen by the model), JavaScript console errors and
      uncaught exceptions, and the page title and text. Building a web game
      and checking it for errors becomes one step.
- [x] **`diagnose`**: LSP-lite. Lint and type-check files on disk with
      whatever is installed (ruff, pyflakes, mypy, pyright, tsc, eslint,
      shellcheck, cargo check, go vet). Real errors only, with file:line.
- [x] **`changes`**: every file this task changed, with a compact diff, so
      the agent can review its own work before calling it done.
- [x] **`patch`**: apply a multi-file unified diff atomically, with
      checkpoints. Big refactors in one block.
- [x] **Live command output.** Long builds stream into their card as they
      run, instead of appearing at the end.
- [x] **MCP client.** Connect Model Context Protocol servers (stdio) from
      Settings. Their tools are listed to the model and called with
      ```mcp server.tool {json}```. Every tool the ecosystem builds becomes
      Chuck's.

### Phase 3 — reliability and cost

- [x] **Model fallback.** Repeated 429/5xx on the chat model fall back to
      another configured model for that request, with a note.
- [x] **Cost guard.** An optional per-chat spending cap. Reaching it pauses
      the task and asks before spending more.
- [x] **Auto-compaction.** When a long session outgrows the context budget,
      older turns are summarized by the model into one dense note instead of
      silently dropped. `/compact` does it on demand.

### Phase 4 — polish and proof

- [x] **Export** a chat as Markdown.
- [x] Deep tests for every item above, in `tests/test_deep.py` or a new suite.
- [x] README, CHANGELOG, screenshots of the new cards on real GTK.
- [x] Prompt budget re-checked: every new tool costs characters on every hop.

## v16 — fill the gaps

The v15 work made Chuck capable. v16 is about what still makes it worse to
work with than it should be: you can't steer it while it works, it acts
before you've agreed on a plan, git and formatting are manual, research hits
walls a real browser wouldn't, and when something breaks on your machine
there's nothing to look at.

### Phase 5 — steer it and trust it

- [x] **Steer mid-task.** Enter while it's working queues your message; it
      reaches the agent at its next step ("from the user, mid-task") and shows
      as a queued bubble until delivered. No more waiting for a run to finish
      to say "not that file".
- [x] **Plan mode.** A toggle in the composer (and `/plan`): the agent
      investigates read-only and proposes a plan card with Approve and Revise.
      Nothing is changed until you approve; approving runs it.
- [x] **Retry and edit.** Ctrl+R retries the last answer. ↑ in an empty
      composer edits your last message and resends from there.

### Phase 6 — the workflow around the code

- [x] **Git.** `/diff` shows the working tree as a diff card; `/commit` has the
      agent write the message from the diff and commit; `/review` has it review
      the diff for bugs before you push.
- [x] **Hooks.** Settings: a command after every file write (a formatter,
      with `{path}`) and one before the agent may finish (tests, lint). Their
      results go to the model, so it fixes what they find.
- [x] **Python session.** ```py``` blocks run in one persistent interpreter
      per chat: data loaded once stays loaded, plots are saved and shown.

### Phase 7 — the web as it really is

- [x] **Stealth fetch.** When a page refuses a plain fetch (403/429, a bot
      challenge, an empty JavaScript shell), it's fetched again through the
      chosen browser (Brave or Camoufox) and its readable text extracted.

### Phase 8 — operations

- [x] **`/doctor`.** One card checking the API key and balance, the model,
      browsers, linters, MCP servers, voice and disk, with the fix for each
      problem.
- [x] **Crash log.** Exceptions on the UI thread and in tools are written to
      `~/.local/state/chucknorris/chuck.log`; Settings opens it; `/doctor`
      shows the latest.
- [x] **Update check.** Once a day, compare with the version on GitHub; a
      toast when there's a new one; `/update` runs the installer.

### Phase 9 — proof

- [x] Tests for every item, deep tests where it touches real processes.
- [x] README, CHANGELOG, screenshots on real GTK.
- [x] Prompt budget re-checked.

## v17 — the next gaps

v16 made Chuck steerable and dependable. What still separates it from the
best agents: it navigates code by text, not by meaning or by symbol; undo
covers files it wrote, not everything a command changed; only one task can
run at a time; desktop apps it builds can be seen but not driven; and MCP
reaches only local servers.

### Phase 10 — code intelligence

- [x] **Symbols.** ```def <name>``` and ```refs <name>``` answer "where is this
      defined / used" through a real language server when one is installed
      (pyright, typescript-language-server, rust-analyzer, gopls), with the
      AST/regex map as the fallback.
- [x] **Rename.** ```rename <old> <new>``` applies a language server's rename
      across the project as one checkpointed, revertible change.
- [x] **Search by meaning.** ```find <description>``` ranks files and
      functions by relevance (BM25 over identifiers, docstrings and comments,
      stdlib only), for "where do we handle retries?" in an unfamiliar repo.

### Phase 11 — safer autonomy

- [x] **Checkpoints of everything.** Before each step that acts, the working
      folder is snapshotted into a shadow git repository (yours is never
      touched). `/rewind` restores files AND conversation to any earlier step,
      including what a command (a code generator, `npm install`) changed.
- [x] **Sandbox mode.** An option to run commands and ```py``` inside
      bubblewrap: writes only to the project, no network unless allowed, the
      rest of the system read-only.
- [x] **Permission profiles.** Per-folder rules (allow all / ask before writes
      outside the folder / ask before network / read-only), shown as a chip in
      the composer.

### Phase 12 — more than one thing at once

- [ ] **Background tasks.** Send a task to the background and keep chatting;
      it runs in its own chat with its own folder, and a notification says when
      it's done or needs an answer.
- [x] **Resume after a crash.** An in-flight turn's state is saved as it goes;
      reopening Chuck after a crash or power cut offers to continue it.
- [ ] **Scheduled tasks.** "Every morning, update the system and tell me what
      changed": a schedule in Settings, results waiting as a chat.

### Phase 13 — desktop apps it can drive

- [ ] **Accessibility driver.** ```app``` launches a GTK/Qt/Electron app and
      acts on it through AT-SPI: list the controls, click a button by its
      name, type into a field, read a label. Desktop apps get the same
      build–play–fix loop web pages have.
- [ ] **Voice input.** Push-to-talk with a local whisper.cpp model; nothing
      leaves the machine.

### Phase 14 — ecosystem

- [ ] **Remote MCP.** Streamable-HTTP MCP servers (with OAuth), MCP resources
      and prompts, alongside the stdio servers.
- [x] **Custom commands.** `.chuck/commands/<name>.md` in a project (or the
      config folder) becomes `/name`, with `$ARGUMENTS`.

### Phase 15 — proof

- [ ] Tests for every item, real language servers and real AT-SPI where
      they're installed.
- [ ] README, CHANGELOG, screenshots on real GTK.
- [ ] Prompt budget re-checked.
