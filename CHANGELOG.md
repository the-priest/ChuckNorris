# Changelog

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
