Chuck 18 is Chuck rebuilt in Rust: one Lead and eight workers on SiliconFlow, and work that counts
as done only when it has been verified.

## Install or update

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash && chucknorris
```

The installer builds Chuck from source and installs it over any Chuck you had before. Afterwards the
menu entry and `chucknorris` open the Rust app. Your settings, API key, chats, memory, daemons and
spending history carry over.

An older Chuck offers the switch itself: its update installs the new files, and on the next start
a toast offers **Install it**.

The `chuck-18.0.0-linux-x86_64.tar.gz` attached to this release is a prebuilt binary. It needs GTK
4.14+ and libadwaita 1.5+. Unpack it and run `./chuck`, or put it in `~/.local/bin`. The installer
is still the recommended way, because it also sets up the menu entry, bubblewrap and voice.

## What's new

- **A fleet, not a chatbot.**
  - One Lead (DeepSeek-V4.1-Flash) writes the acceptance criteria, plans, reviews and owns "done".
  - Up to four DeepSeek and four GLM-5.3-Flash workers build in parallel, each jailed in its own copy
    of your project.
- **Genuine Done, enforced in code.**
  - Every criterion needs real evidence.
  - Verification must pass after the last change.
  - A worker's work is re-checked before it is merged.
  - An independent critic reviews the result before the job can finish.
- **Long jobs run until they're done.**
  - There is no step or time limit, and API outages are waited out.
  - Only your budget limits, or the Lead really being stuck, pause a job.
  - A paused job says why. **Keep going** (also `/continue` or `chuck continue`) carries on where it
    stopped, even after Chuck was closed.
- **Safe without getting in the way.**
  - The project folder is fully writable.
  - Anything outside it waits for your yes: **Allow once**, **Allow for this job**, or **Refuse**.
  - Credentials stay hidden.
  - Critical commands need you to type `yes`.
- **Closing the window ends everything Chuck started**, including background and detached processes.
- **The window:**
  - Recent chats (including the Python app's).
  - The code-wave welcome screen.
  - A live right panel: plan, tasks, agents, commands, the Done gate, spend and checkpoints.
  - Ctrl+P command palette.
  - Chat mode, with pictures and videos.
- **Built in:**
  - Web research and a real browser for checking UIs.
  - MCP servers and language servers.
  - Daemons and reminders, local voice in and out, memory, checkpoints and `/undo`, and `/doctor`.

## Not ported yet

Camoufox (a Firefox build) as the browser. The Rust app drives Brave, Chromium or Chrome.
