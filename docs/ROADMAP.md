# Chuck roadmap: from v14.1 to a modern agent harness

A living plan. Each item ships on `main` with tests when it's done, and is
ticked here in the same commit. The guiding rule is the one learned the hard way
in 14.1: **the harness serves the model, it never gets in its way.** Every new
tool has to be cheap in prompt characters, safe by construction, testable
without a network, and visible to the user as a card.

## Phase 0 — foundations

- [x] **Tool registry.** New tools declare a tag, a handler, and whether they
      are read-only or agent-only. The reply parser dispatches them, counts
      them, gates them in Chat mode and batches read-only ones automatically.
      Adding a tool stops meaning edits in eight places.

## Phase 1 — the model always has the right context

- [x] **Project instructions.** `CHUCK.md`, `AGENTS.md` or `CLAUDE.md` in the
      working folder (and `~/.config/chucknorris/CHUCK.md` for global rules)
      are loaded into every request, capped and cache-friendly.
- [x] **`@file` mentions.** `@src/app.py` in your message attaches that file;
      Tab-free, works with paths relative to the working folder.
- [x] **Persistent shell directory.** `cd` in one command carries to the next,
      the way a terminal does, and resets when the working folder changes.
- [x] **Slash commands.** `/help /clear /compact /model /undo /agent /chat
      /folder /cost /memory /export` in the composer.

## Phase 2 — tools a modern coding agent needs

- [x] **`browse`**: render a page or local HTML file in headless Chromium.
      Returns a screenshot (seen by the model), JavaScript console errors and
      uncaught exceptions, and the page title and text. Building a web game
      and checking it for errors becomes one step.
- [ ] **`diagnose`**: LSP-lite. Lint and type-check files on disk with
      whatever is installed (ruff, pyflakes, mypy, pyright, tsc, eslint,
      shellcheck, cargo check, go vet). Real errors only, with file:line.
- [x] **`changes`**: every file this task changed, with a compact diff, so
      the agent can review its own work before calling it done.
- [ ] **`patch`**: apply a multi-file unified diff atomically, with
      checkpoints. Big refactors in one block.
- [ ] **Live command output.** Long builds stream into their card as they
      run, instead of appearing at the end.
- [ ] **MCP client.** Connect Model Context Protocol servers (stdio) from
      Settings. Their tools are listed to the model and called with
      ```mcp server.tool {json}```. Every tool the ecosystem builds becomes
      Chuck's.

## Phase 3 — reliability and cost

- [ ] **Model fallback.** Repeated 429/5xx on the chat model fall back to
      another configured model for that request, with a note.
- [ ] **Cost guard.** An optional per-chat spending cap. Reaching it pauses
      the task and asks before spending more.
- [ ] **Auto-compaction.** When a long session outgrows the context budget,
      older turns are summarized by the model into one dense note instead of
      silently dropped. `/compact` does it on demand.

## Phase 4 — polish and proof

- [ ] **Export** a chat as Markdown.
- [ ] Deep tests for every item above, in `tests/test_deep.py` or a new suite.
- [ ] README, CHANGELOG, screenshots of the new cards on real GTK.
- [ ] Prompt budget re-checked: every new tool costs characters on every hop.
