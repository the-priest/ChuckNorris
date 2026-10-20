<div align="center">

# Chuck

**A desktop coding and system agent for Linux, on SiliconFlow models.**
Runs commands, edits real code, reads whole repos, sees screenshots, runs sub-agents in parallel, and tells you what it all cost.

![version](https://img.shields.io/badge/version-14.1.0-8b5cf6?style=flat-square&labelColor=0c0c10)
![gtk](https://img.shields.io/badge/GTK4-libadwaita-8b5cf6?style=flat-square&labelColor=0c0c10)
![python](https://img.shields.io/badge/Python-3.11_·_3.12_·_3.13-8b5cf6?style=flat-square&labelColor=0c0c10)
![suites](https://img.shields.io/badge/test_suites-20-2ea043?style=flat-square&labelColor=0c0c10)
![deps](https://img.shields.io/badge/runtime_deps-stdlib_+_GTK-2ea043?style=flat-square&labelColor=0c0c10)
![licence](https://img.shields.io/badge/licence-MIT-8b5cf6?style=flat-square&labelColor=0c0c10)

<img src="assets/screenshots/welcome.png" alt="Chuck's welcome screen: a dark window with a field of tiny code glyphs rippling from a click" width="100%">

</div>

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash && chucknorris
```

Paste a SiliconFlow key in Settings. Press **Tab** to pick Agent or Chat. Talk to it.

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
| `bash` · `python` · `node` | One step at a time, auto-verified (syntax, lint, security scan) before it runs, real exit code back. |
| `plan` | A live checklist card that ticks itself off as the work proceeds. |
| `undo` | Every write and edit is a checkpoint with its diff and a **Revert** button. `undo` reverts the last one; `undo all` reverts the whole task. |
| `bg` · `logs` · `kill` | Dev servers and watchers run in the background with a live card. Chuck reads their logs and stops them, and they stop when the window closes. |
| `ask` | A question with one-click answers, for decisions that are really yours. The task waits for your answer. |
| `open` | Hands you the result in your browser or default app. |
| `runtests` · `package` · `tree` | Run the project's own tests, zip the result, show what was built. |
| `screenshot` · `look` | Chuck **sees**: the screen, or any image (a page rendered with headless Chromium, a plot, a mockup). Build a UI, look at it, fix it. |
| `task` | **Parallel sub-agents.** Each `task` block is a read-only sub-agent with its own model loop. Several run at once and report back; only their summaries enter the main context. |
| `search` · `fetch` | Live web research across SearXNG instances, with sources cited. |
| `remember` · `forget` | Long-term memory (below). |

<img src="assets/screenshots/agent-subagents.png" alt="Three sub-agents running in parallel, each with a done badge and a folded report" width="100%">

**Sub-agents are read-only by design.** Parallel writers would trample each other's edits and race each other's sudo prompts. Parallel readers can't. They survey, research and report; the main agent acts, one verified step at a time.

## It finishes the job

Ask for a game and you get a game, not *"I'll write the game now"* and silence. Before Chuck ends a task he checks three things:

- **Did I promise something I didn't do?** A reply that announces an action without one is sent back to act.
- **Is the plan finished?** Unchecked steps on his plan card mean he isn't done.
- **Did I run what I changed?** Code written since the last run gets run, tested or screenshotted before the final report.

He stops early only to ask you something he shouldn't decide alone. That arrives as a question card with one-click answers. All of it is bounded by the step budget (60 by default) and none of it happens in Chat mode.

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

- **Prompts:** agent 3.9k characters (v13 was 5.7k while documenting a dozen fewer tools); chat 0.7k.
- **Playbooks** (debugging, TDD, review, git, UI, performance, security, Arch, building…) are injected only when the task triggers them, at most two at a time, and never in Chat mode.
- **Cache-friendly:** the volatile part (date, memory, folder) is appended *last*, so the long prefix stays cacheable on the API side.
- **Context control:** older tool output is compressed extractively (head, tail and every error line kept) and the context budget is a setting.
- **Batching:** read-only tools batch into one round trip, and four of them cost one model call, not four.

## Memory you can trust

Durable facts persist across chats and only the few relevant ones are sent with a question, never the store. Two upgrades in the spirit of [supermemory](https://github.com/supermemoryai/supermemory):

- **Updates supersede.** "GPU is an RTX 4090" replaces "GPU is an RTX 3080" instead of sitting beside it. The old fact is archived, not destroyed.
- **Project scope.** Facts saved as `project:` belong to the working folder and are recalled only there, so one repo's conventions never leak into another's answers.

All of it is visible, searchable, addable and deletable in the Memory panel.

## Safety that doesn't get in your way

Commands run on their own; that's the point of an agent. **CRITICAL** ones (disk wipes, `rm -rf /`, `curl | sh`, removing core packages) arrive disarmed and wait for your tick, and so do saved skills whose *body* is critical. Commands run in their own process group with resource limits, and a timeout kills the whole tree. `pacman -S` is rewritten to `-Syu`. sudo authenticates once per chat, in memory only. Every command lands in a hash-chained ledger you can re-verify in Settings.

## Keyboard

| key | action |
|---|---|
| **Tab** | switch Agent / Chat |
| **Enter** · **Shift+Enter** | send · new line |
| **Ctrl+V** | paste text, an image, or copied files |
| **Ctrl+O** | working folder |
| **Ctrl+N** · **Ctrl+B** · **Ctrl+,** | new chat · chats sidebar · settings |
| **Esc** | stop the current run |

## The window

Dark neutral surfaces with one violet accent, used only for edges, focus and what's live. On the welcome screen, a fine net of code glyphs ripples like water wherever you click. It costs nothing at rest, animates only while a ripple is alive, and respects your desktop's reduce-animation setting. The transcript follows new output only while you're at the bottom: scroll up to read and it stays put, with a *Jump to latest* button to come back. Wide code and tables scroll inside their own block, and the page never scrolls sideways.

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

Twenty suites run headless against a stubbed toolkit and drive the real window end to end. They cover whole conversations, both modes, edits in a real folder, paged reads of a 12,000-line file, a file written across two cut-off replies, parallel sub-agents, images reaching the model as images, cache-hit pricing, memory updates and scopes, an agent that stops early or never stops, late streams from a stopped turn, undo and background jobs, a README whose example must not run, the critical gate, sudo storms, orphaned processes, and the ledger chain. `test_deep.py` runs whole sessions with real processes and ports, plus 180 seeded random model replies that must never hang the app, run something they shouldn't, or write outside the folder. The UI itself was checked by rendering every screen on real GTK 4.14 / libadwaita 1.5. The screenshots above are those renders.

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
