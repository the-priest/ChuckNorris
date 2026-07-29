# Changelog

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
