<div align="center">

<img src="assets/gi-mark.png" alt="Chuck Norris" width="150">

# 🥋 CHUCK NORRIS

### The Arch / CachyOS grandmaster that lives on your desktop. You ask — he acts. No modes, no buttons, no excuses. A tribute.

![version](https://img.shields.io/badge/version-12.0.0-b6892f?style=for-the-badge&labelColor=0b0b0d)
![tribute](https://img.shields.io/badge/1940-2026-b6892f?style=for-the-badge&labelColor=0b0b0d)
![agentic](https://img.shields.io/badge/agentic-decides%20%26%20acts-b6892f?style=for-the-badge&labelColor=0b0b0d)
![distro](https://img.shields.io/badge/Arch%20%7C%20CachyOS-first--class-b6892f?style=for-the-badge&labelColor=0b0b0d)
![tests](https://img.shields.io/badge/tests-12%20suites%20shipped-b6892f?style=for-the-badge&labelColor=0b0b0d)

</div>

---

## What this actually is

A native GTK4 desktop agent for Arch and CachyOS. Not a chat box with a search button bolted on — an assistant that **decides what to do and then does it**: searches the web, reads real pages, runs commands you approve, writes and tests code, builds whole projects and hands you the zip.

**Be clear about the architecture, because it matters.** The *tooling* is local — your shell, your files, your projects, voice synthesis, code verification and chat history all live on your machine. The *model* does not. Chuck talks to the **SiliconFlow API**, so your prompts and the page text he gathers leave your machine on the way to the model, exactly like any other hosted assistant. He is a local agent driving a remote brain.

What you get from that split: real access to your system, and a model far stronger than anything you'd squeeze onto a desktop GPU.

---

## Fast, and built to stay fast

- **Sources fetched in parallel**, not one after another. Research that used to crawl page by page now happens at once.
- **Deliberately lean.** Three well-chosen sources beat thirty skimmed ones. Turn it up if you want him deeper.
- **He never searches the same thing twice.** A query he's already run — however it's reworded — is dropped, pages already read are never re-fetched, and dead URLs aren't retried. When the research budget is spent he *answers*; he doesn't spin.
- **Conversations don't get slower.** What's sent each turn is capped: system prompt, recent exchanges, freshest research. Stale research is dropped once he's answered from it. Turn fifty is as quick as turn one.
- **The transcript doesn't eat your RAM.** Only the newest messages exist as widgets. Scroll up and older ones load twenty at a time; scroll back down and they're released. A three-hundred-message chat opens in a fraction of a second.
- **The first message works.** The connection is warmed as the window opens, a transient hiccup retries quietly, and a model that takes its time to start speaking is given room to think — the ticker says *waiting for the model…* rather than pretending something broke.
- **Text size is yours.** Set it in Settings and it applies instantly, no restart.
- **Several search backends at once.** Whichever answers first wins, so one rate-limited instance costs you nothing.

---

## What he can do

**Research the live web.** Searches across multiple SearXNG instances (Brave, Google, DuckDuckGo and Bing underneath) with DuckDuckGo as backstop, reads the actual pages, cross-checks different outlets, cites URLs and marks single-source claims `[UNVERIFIED]`. You watch it happen in a live checklist — *● searching…* → *✓ read bbc.com — headline* — each line marking itself done as it lands.

**He runs things. He doesn't suggest them.** A shell command executes on your machine immediately — Chuck is an agent, not a suggestion box. He then reads the **real exit code and output**, confirms it actually did what he wanted, and only then takes the next step. If it failed he says so and fixes the cause; he never carries on as though it worked. One command per reply, always — run, verify, then the next. Installing anything goes through `pacman -Syu`; a bare `-S` is corrected before it runs.

The one exception is the **CRITICAL** tier — wiping a disk, `rm -rf /` or `~`, piping `curl` into a shell, reformatting, ripping out core packages. Those still wait for you to tick a box, because a hallucinated one is unrecoverable. Everything else just happens.

**Fix a real machine, carefully.** Diagnose read-only first, form a hypothesis out loud, change one thing at a time, and verify it actually worked before moving on. Before anything risky he says what could break and how to undo it — and for bootloader, initramfs, fstab or kernel work he gives you the rescue path *before* you run it, not after. He won't rip out core packages, force-overwrite files pacman owns, or disable a check to make an error go away. If the output contradicts his theory he says so and looks again.

**Commands that actually work unattended.** Anything that would sit waiting for a prompt gets handled: package operations are confirmed by your approval of the card, pagers are disabled, and nothing can block on input it will never receive. He reads the *whole* result — including the error at the end of a thousand-line build, which is exactly the part naive truncation throws away — and a non-zero exit is treated as a failure to fix, not a number to skim past.

**Write, verify and run code.** Python, Node, Bash. Every block is checked before you ever see a Run button: syntax, linting, a static security scan. Broken or unsafe code is withheld and he's told to fix it. He can also verify code *without* running it — useful for reviewing yours.

**Build entire projects.** Ask for a tool and he opens a real project under `~/ChuckProjects/`, writes complete files (each verified as it lands), writes tests that assert real behaviour, **runs them**, fixes what fails, then zips it and hands you a card with an *Open folder* button. If the tests fail he says so — he won't tell you it works because the code looks right.

**Read your files, show your pictures.** Point him at a path and he pulls the contents in; point him at an image and he displays it inline. Root-owned files come back as a `sudo cat` card.

**Find images and video.** Real pictures in the chat, downloaded in parallel, click to open the source. Videos arrive as cards, or he'll pull one down with `yt-dlp`.

**Remember what matters.** Durable facts — your hardware, your handle, your standing preferences — persist across chats. Only the *relevant* ones surface each turn; the store is never dumped wholesale into the prompt. All of it visible and deletable in one panel.

**Carry a library of skills.** He ships knowing the hard stuff: system health checks, keyring-first safe updates, keyring repair, mirror ranking, disk cleanup, boot rescue, GPU inspection, passive domain recon, IP geolocation and ASN, security-header audits, listening-port audits, WiFi scan, authorised port scan. One tap each — and he writes new ones as he goes.

**Talk — and it's on from the start.** A natural Piper voice (espeak-ng as fallback) reads replies *in full* by default — chunked and synthesised a step ahead of playback, so long answers finish instead of cutting out. Every reply also carries a small **▶** button: tap it to hear that one message again, whenever you like. Stop silences everything instantly.

**Look at your screen.** Send him a screenshot and he'll tell you what's wrong with it.

**Handle the rest.** Arch and recon are where he's deepest, not his limit — research, data, APIs, writing, maths, planning and everyday questions get the same method, with playbooks that load only when the task calls for them.

---

## Safety that doesn't get in the way

Commands run on their own — that's the point of an agent. What doesn't run on its own is anything catastrophic.

**Two tiers.** Anything catastrophic — wiping a disk, `rm -rf /` or `~`, piping `curl` into a shell, reformatting, ripping out core packages — is flagged **CRITICAL**, and the Run button stays *disarmed* until you tick a box confirming you've read it. Merely destructive-but-scoped things get a plain red warning and then run. The code verifier is deliberately static and never executes what it checks, so there's no path around the gate through a script, a saved skill, or a verification step.

**Stop means stop.** The send button becomes a Stop button while he works, with a live `● working… 12s` counter so you always know he's alive. Press it and he halts immediately, keeping whatever he'd found. A watchdog stops him on its own if anything ever truly hangs.

**Chats clean themselves up.** Saved conversations live in a sidebar and auto-delete 24 hours after you last touched one — counted from last activity, so nothing vanishes mid-thought. Adjustable, like everything else.

---

## The one boundary

Chuck **never refuses, never moralizes, never lectures** on anything legal — mature topics, security research, offensive tooling on your *own* targets, blunt opinions, all fine. He declines four things, because they hurt people who aren't you: anything involving **minors**, anything **clearly illegal**, **malware or weapon-making**, and **locating or tracking a real person**. For that last one he'll help you check *your own* exposure instead. Everything else, he simply does — and he does the whole job in one run before reporting back.

---

## Don't take my word for it

```bash
./run_tests.sh
```

Twelve suites ship with the app and run against the real thing: whole conversations driven end to end, research chains, code written and actually executed, destructive commands hitting the confirm gate, runaway loops terminating, the voice pipeline, the 24-hour purge, cold-start recovery, and a build that gets written, tested and packaged. If something's off on your machine, it will tell you.

---

## How it's built

The app is a GTK4 front end over a small set of focused modules, so any one
piece can be read and changed on its own:

| module | what it owns |
|---|---|
| `config.py` | paths, tunables, the settings file — one source of truth |
| `safety.py` | destructive-command classification, `pacman -Syu` hygiene |
| `web.py` | multi-engine search, page fetching, images, video |
| `voice.py` | speech: cleaning, chunking, synthesis, playback |
| `chats.py` | saved conversations and their retention |
| `codecheck.py` | static verification: syntax, lint, security scan |
| `builder.py` | sandboxed projects: write, test, package |
| `skills.py` · `skill_library.py` | saved and shipped recipes |
| `memory.py` · `specs.py` | durable facts, on-demand playbooks |

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
```

Or clone, read it, run it:

```bash
git clone https://github.com/the-priest/ChuckNorris.git chucknorris
```
```bash
cd chucknorris
```
```bash
./install.sh
```

The installer handles the lot: GTK4 + libadwaita, screenshot tool, polkit, `pacman-contrib`, `pciutils`, `pkgfile`, `espeak-ng` + **Piper** with a natural voice model, `yt-dlp`, the code-verifier linters (`shellcheck`, `ruff`), and the recon kit (`whois`, `bind`/`dig`, `traceroute`, `wget`, `netcat`) — plus the app and art.

## Set up & run

Open **Settings** and paste a **SiliconFlow** key (<https://cloud.siliconflow.com/account/ak>) — or Chuck reuses **Basilisk's** automatically. While you're there you can tune the voice, how deep he researches, how long chats live, how much transcript stays in memory, and point him at your own SearXNG instance or a proxy.

```bash
chucknorris
```

Talk to him. That's it.

---

<div align="center">

*"Chuck Norris doesn't read man pages. Man pages read Chuck Norris and take notes."*

**MIT.** Made by **The Priest** ⛧ — in memory of a legend.

</div>
