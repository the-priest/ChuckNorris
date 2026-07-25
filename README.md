<div align="center">

<img src="assets/gi-mark.png" alt="Chuck Norris" width="170">

# 🥋 CHUCK NORRIS

### **The Arch / CachyOS grandmaster that lives on your desktop.**
### *You ask. He acts. No modes, no buttons, no excuses.*

<br>

![version](https://img.shields.io/badge/version-12.0.3-b6892f?style=for-the-badge&labelColor=0b0b0d)
![tribute](https://img.shields.io/badge/1940--2026-a_tribute-b6892f?style=for-the-badge&labelColor=0b0b0d)
![agentic](https://img.shields.io/badge/agentic-decides_%26_acts-b6892f?style=for-the-badge&labelColor=0b0b0d)
![distro](https://img.shields.io/badge/Arch_%7C_CachyOS-first--class-b6892f?style=for-the-badge&labelColor=0b0b0d)

![gtk](https://img.shields.io/badge/GTK4-libadwaita-b6892f?style=flat-square&labelColor=0b0b0d)
![python](https://img.shields.io/badge/Python-3.11+-b6892f?style=flat-square&labelColor=0b0b0d)
![tests](https://img.shields.io/badge/tests-15_suites,_all_green-2ea043?style=flat-square&labelColor=0b0b0d)
![skills](https://img.shields.io/badge/skills-14_shipped-b6892f?style=flat-square&labelColor=0b0b0d)
![licence](https://img.shields.io/badge/licence-MIT-b6892f?style=flat-square&labelColor=0b0b0d)

<br>

> *Chuck Norris doesn't `kill -9` a process.*
> *He looks at it, and it exits `0` out of respect.*

</div>

---

<div align="center">

### ⚡ THIRTY SECONDS ⚡

</div>

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
```
```bash
chucknorris
```

Paste a SiliconFlow key in Settings. Talk to him. That's the whole onboarding.

---

## 🥋 What this actually is

A **native GTK4 desktop agent** for Arch and CachyOS. Not a chat window with a search button glued on the side — an assistant that **decides what to do and then does it.** Searches the live web. Reads the actual pages. Runs commands on your real machine. Writes code, tests it, fixes it, and hands you the zip.

> *Chuck Norris doesn't use a package manager.*
> *He tells the software where to live and it moves in.*

**Now the honest part, because you deserve it up front.** The *tooling* is local — your shell, your files, your projects, voice synthesis, code verification, chat history, all of it on your machine. The *model* is not. Chuck talks to the **SiliconFlow API**, so your prompts and the page text he gathers leave your box on the way to the model, exactly like every other hosted assistant.

He is a **local agent driving a remote brain.** That trade buys you real access to your system plus a model far stronger than anything you'd fit on a desktop GPU. If that trade isn't for you, that's a completely reasonable position and you should stop reading here.

---

## 🔥 What he can actually do

### 🌐 Research the live web

Searches across multiple SearXNG instances — Brave, Google, DuckDuckGo and Bing underneath — with DuckDuckGo as backstop. Reads the real pages. Cross-checks outlets. Cites URLs. Marks single-source claims `[UNVERIFIED]` instead of quietly presenting them as fact.

You watch it happen in a **live checklist** — `● searching…` → `✓ read bbc.com — headline` — each line ticking itself off as it lands. No mystery spinner. No wondering whether it died.

> *Chuck Norris doesn't get rate-limited.*
> *The API waits its turn.*

### ⚙️ He runs things. He does not suggest them.

A shell command **executes on your machine.** Chuck is an agent, not a suggestion box that makes you copy-paste your own homework.

Then — and this is the part that matters — he reads the **real exit code and real output**, confirms it did what he intended, and only then takes the next step. If it failed he says so and fixes the *cause*. He never carries on as though it worked. **One command per reply, always.** Run, verify, then the next.

Installing anything goes through `pacman -Syu`. A bare `-S` is **rewritten before it runs** — including inside saved skills, including for `paru` and `yay`. Partial upgrades are a rule here, not a polite request in a prompt the model can forget.

### 🛠️ Fix a real machine, carefully

Diagnose read-only first. Form a hypothesis out loud. Change **one thing at a time.** Verify it actually worked before moving on.

Before anything risky he tells you what could break and how to undo it — and for bootloader, initramfs, fstab or kernel work he hands you the **rescue path *before* you run it**, not in the postmortem. He won't rip out core packages, force-overwrite files pacman owns, or disable a check to make an error disappear.

If the output contradicts his theory, he says so and looks again. That last sentence is the whole personality.

> *Chuck Norris doesn't chroot into a broken system.*
> *The system chroots into Chuck Norris and apologises.*

### 💻 Write, verify and run code

Python, Node, Bash. **Every block is checked before you ever see a Run button** — syntax, linting, static security scan. Broken code is withheld and he's told to fix it.

Findings are split by severity, which took a bug to learn. Injection and RCE patterns **block** the button; broad footgun patterns (`md5`, `verify=False`) are **reported beside it** without blocking. Treating everything as blocking meant a script that legitimately checksummed a file could never run, and the model looped forever trying to "fix" code that was already correct.

He can also verify code **without running it** — useful for reviewing yours.

### 📦 Build entire projects

Ask for a tool. He opens a real project under `~/ChuckProjects/`, writes complete files (each verified as it lands), writes tests that assert real behaviour, **runs every one of them**, fixes what fails, then zips it and hands you a card with an *Open folder* button.

If the tests fail, **he tells you they failed.** He will not report success because the code looks about right.

### 🧠 Remember what matters

Durable facts — your hardware, your handle, your standing preferences — persist across chats. Recall is IDF-weighted with light stemming, so `"my nvidia driver broke"` finds the fact about DKMS, and `"what's the weather"` correctly finds **nothing at all**. Only relevant facts surface each turn; the store is never dumped wholesale into the prompt.

All of it visible and deletable in one panel. It's your memory, not his.

### 🥷 Fourteen skills, shipped

System health checks · keyring-first safe updates · keyring repair · mirror ranking · disk cleanup · boot rescue · GPU inspection · passive domain recon · IP geolocation and ASN · security-header audits · listening-port audits · WiFi scan · authorised port scan.

One tap each. And he writes new ones as he goes.

### 🔊 Talk — and it's on from the start

A natural **Piper** voice (espeak-ng fallback) reads replies **in full** by default, chunked and synthesised a step ahead of playback so long answers *finish* instead of dying mid-sentence. Every reply carries a small **▶** to hear that one again. Stop silences everything instantly.

### 👁️ Plus

**Read your files** — point him at a path and he pulls it in; root-owned files come back as a `sudo cat` card. **Show your pictures** — images render inline. **Find video** — cards, or `yt-dlp` it down. **Look at your screen** — send a screenshot and he'll tell you what's wrong with it. **Handle the rest** — Arch and recon are where he's deepest, not where he stops.

---

## 🛡️ Safety that doesn't get in your way

Commands run on their own. That's the entire point of an agent. What does **not** run on its own is anything catastrophic.

<table>
<tr><td width="130"><b>🔴 CRITICAL</b></td><td>Disk wipes · <code>rm -rf /</code> or <code>~</code> · <code>curl | sh</code> · reformatting · ripping out core packages.<br><b>The Run button stays disarmed until you tick a box.</b> A hallucinated one of these is unrecoverable, so it never runs on his say-so alone.</td></tr>
<tr><td><b>🟠 DANGER</b></td><td>Destructive but scoped. Plain red warning, then it runs.</td></tr>
<tr><td><b>🟢 NORMAL</b></td><td>Just happens. You came here for an agent, not a permissions dialog.</td></tr>
</table>

The tier is judged on **what will actually execute** — including the body of a saved skill, so nothing gets smuggled past the gate inside a script. The verifier is deliberately **static and never executes what it checks**, so there's no path around the gate through a verification step either.

**Stop means stop.** The send button becomes Stop while he works, with a live `● working… 12s` counter. Press it and he halts, keeping whatever he found. A watchdog stops him on its own if anything truly hangs — and it's sized off the *running command's own budget*, so a real twenty-minute `pacman -Syu` isn't mistaken for a hang and shot at the two-minute mark.

**Commands can't outlive their timeout.** Each gets its own process group and resource limits, and a timeout kills the whole tree. No orphans quietly eating your box after Chuck has moved on.

**Your API key is 0600**, written from the descriptor so there's never a window where it's world-readable.

**Every command is logged** — append-only, hash-chained JSONL. Edit an entry and verification tells you exactly which one. That's tamper-*evident*, not tamper-*proof*: anyone who can write the file can rewrite the chain. It guards against accidental loss and quiet after-the-fact editing, not against someone who already owns your account.

**Chats clean themselves up.** Sidebar, auto-delete 24h after last activity so nothing vanishes mid-thought. Adjustable, like everything else.

---

## 🚫 The one boundary

Chuck **never refuses, never moralises, never lectures** on anything legal. Mature topics, security research, offensive tooling on your *own* targets, blunt opinions — all fine.

He declines four things, because they hurt people who aren't you: anything involving **minors**, anything **clearly illegal**, **malware or weapon-making**, and **locating or tracking a real person**. For that last one he'll help you audit *your own* exposure instead.

Everything else, he simply does — and he does the whole job in one run before reporting back.

---

## 🧪 Don't take my word for it

```bash
./run_tests.sh
```

**Fifteen suites, run against the real thing.** Whole conversations driven end to end. Research chains. Code written and actually executed. Destructive commands hitting the confirm gate. Runaway loops terminating. The voice pipeline. The 24-hour purge. Cold-start recovery. A project written, tested and packaged. Fifteen concurrent sudo requests raising exactly **one** dialog. Timed-out commands leaving **zero** orphans.

Every bug fixed in v12.0.1–12.0.3 has a named test with a comment saying which failure it prevents. If something's off on your machine, the suite will tell you before Chuck does.

> *Chuck Norris doesn't write unit tests.*
> *The code confesses.*

---

## 🙏 Being straight with you

Flashy README, so here's the deflating bit. Every project this size has one.

- **It's a personal project**, not a product with an SLA. Built by one person, for one person's machine, then cleaned up enough to share.
- **The model is remote.** See above. Your prompts leave your box.
- **He's deepest on Arch and CachyOS.** He'll help on other distros; he just won't know them in his bones.
- **He is not infallible, and neither is the tiering.** A regex classifier is a good safety net, not a proof. Read the CRITICAL cards. That tick box is there for a reason.
- **v12 shipped with real bugs** — a sudo storm that opened fifteen modals at once, a watchdog that shot healthy upgrades at the two-minute mark, a `videos` tool that had never once worked. They're all in `CHANGELOG.md`, described plainly, each with a test. A project that claims it never had bugs is a project that isn't looking.

> *Chuck Norris has never lost an argument with a compiler.*
> *He has, once, agreed to a compromise.*

---

## 🏗️ How it's built

GTK4 front end over small focused modules — any one piece readable and changeable on its own.

| module | what it owns |
|---|---|
| `config.py` | paths, tunables, settings — one source of truth |
| `safety.py` | destructive-command classification, `pacman -Syu` hygiene |
| `web.py` | multi-engine search, page fetching, images, video |
| `voice.py` | speech: cleaning, chunking, synthesis, playback |
| `chats.py` | saved conversations and retention |
| `codecheck.py` | static verification: syntax, lint, security severity split |
| `builder.py` | sandboxed projects: write, test, package |
| `compress.py` | extractive context compression — cuts bulk, keeps errors |
| `ledger.py` | hash-chained record of every command run |
| `skills.py` · `skill_library.py` | saved and shipped recipes |
| `memory.py` · `specs.py` | durable facts, on-demand playbooks |

---

## 📥 Install

```bash
curl -fsSL https://raw.githubusercontent.com/the-priest/ChuckNorris/main/install.sh | bash
```

Or clone it, **read it first** (you should), then run it:

```bash
git clone https://github.com/the-priest/ChuckNorris.git chucknorris
```
```bash
cd chucknorris
```
```bash
./install.sh
```

The installer handles the lot: GTK4 + libadwaita, screenshot tool, polkit, `pacman-contrib`, `pciutils`, `pkgfile`, `espeak-ng` + **Piper** with a natural voice model, `yt-dlp`, the verifier linters (`shellcheck`, `ruff`), and the recon kit (`whois`, `bind`/`dig`, `traceroute`, `wget`, `netcat`) — plus the app and art.

## ⚙️ Set up

Open **Settings**, paste a **SiliconFlow** key ([get one here](https://cloud.siliconflow.com/account/ak)) — or Chuck reuses **Basilisk's** automatically if you have it.

While you're in there: tune the voice, how deep he researches, how long chats live, how much transcript stays in RAM, and point him at your own SearXNG instance or a proxy.

```bash
chucknorris
```

Talk to him. That's it.

---

<div align="center">

<br>

> *Chuck Norris doesn't read man pages.*
> *Man pages read Chuck Norris and take notes.*

<br>

**MIT.** Made by **The Priest** ⛧

*In memory of a legend. 1940–2026.*

🥋

</div>
