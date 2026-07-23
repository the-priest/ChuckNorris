<div align="center">

<img src="assets/chucknorris-icon.png" alt="Chuck Norris" width="140">

# 🥋 CHUCK NORRIS

### The Arch / CachyOS grandmaster that lives on your desktop. You ask — he acts. No modes, no buttons, no excuses. A tribute.

![version](https://img.shields.io/badge/version-9.0.0-b6892f?style=for-the-badge&labelColor=0b0b0d)
![tribute](https://img.shields.io/badge/1940-2026-b6892f?style=for-the-badge&labelColor=0b0b0d)
![agentic](https://img.shields.io/badge/agentic-decides%20%26%20acts-b6892f?style=for-the-badge&labelColor=0b0b0d)
![distro](https://img.shields.io/badge/Arch%20%7C%20CachyOS-first--class-b6892f?style=for-the-badge&labelColor=0b0b0d)

</div>

---

## What makes this different

Most Linux "assistants" are a chat box that spits out commands you copy-paste, or a wrapper that needs you to pick a mode before it'll do anything. Chuck is neither. **He reads what you ask, decides which of his tools to use, and uses them — live, in front of you — then answers.** You never toggle "search mode" or press an "images" button. You just talk. And every shell command he wants to run still lands as a card **you** approve, so he's powerful without being reckless.

There isn't another GTK-native, offline-first, agentic CachyOS specialist that fuses live web research, a running feed of what it's doing, image and video retrieval, a full recon toolkit, screen vision, file reading, a natural voice, and approve-to-run safety — in one app that runs on your own machine and answers to no one but you.

## How he works

Ask for anything. Behind the scenes Chuck reaches for the right tool and you watch it happen in the **live feed** at the bottom of the window — `🔎 searching: cachyos bore scheduler` → `📄 reading wiki.cachyos.org` → `📄 reading phoronix.com` — so you always know he's working, never wondering if he's stuck. His tools:

- **Verify-first web research** — Chuck doesn't answer from memory. Before any factual, current, "how do I" or news claim he **searches, reads the real pages, and cross-checks them** — fanning out across several queries and pulling up to **10 distinct sources across different domains** (capped at 2 per outlet, so "cross-checked" means genuinely different outlets). He answers grounded in what he read, **with citations**, and marks anything he only found in one place `[UNVERIFIED]`. Search runs over **SearXNG** (Brave + Google + DuckDuckGo + Bing under the hood) with a DuckDuckGo fallback; point it at your own private instance in Settings.
- **One continuous run** — give him a task and he does the *whole* thing before reporting: gather → verify → act → answer, back-to-back in a single pass, instead of stopping halfway to narrate and wait. The only thing that pauses him is a shell command, because **you** approve those.
- **Writes, runs & auto-verifies code** — real, complete, runnable files in any language, with current-docs lookups when an API or flag matters. He doesn't just *show* you code: he **writes it, verifies it, and runs it**. Every code block is checked automatically — **syntax, lint, a security scan, and any tests** — *before* you get a Run button. If the check finds a problem (a syntax error, an undefined name, `os.system` injection, a hard-coded secret, `curl | bash`…), the button is withheld and Chuck fixes it first. So broken or unsafe code never reaches you. He can also verify code without running it (great for reviewing *your* code), and iterates on real output until it works — the same write-check-fix loop a top-tier agent uses, on your actual machine.
- **A library of ready-made skills** — Chuck ships knowing how to do the hard stuff: a full system health check, a safe keyring-first update, keyring repair, mirror ranking, disk cleanup, boot rescue, GPU/driver inspection, plus recon recipes — passive domain recon, IP geolocation + ASN, a security-headers audit, a listening-ports audit, WiFi scan, and an authorised port scan. Every one was verified as valid before shipping. Run any with one tap, or let him reach for the right one automatically instead of rewriting it.
- **Reads your files** — point him at any path (```read /etc/mkinitcpio.conf```) and he pulls the real contents into the conversation and works from them, instead of guessing. Directories list too; root-owned files come back as a `sudo cat` card.
- **Narrates as he works** — before every step he says, in plain language, what he's about to do and why ("Checking the Arch wiki for the exact flag…", "Running this to confirm the output…"), so you're never staring at a silent spinner wondering what he's doing.
- **Images** — ask to see something and he pulls the pictures straight into the chat. Click one to open the source in **Brave**.
- **Video search & download** — ask him to *find* videos and he lists them as cards (open in Brave, or one-tap **Download**); give him a link and he grabs it with `yt-dlp` into `~/Downloads/ChuckNorris` (proxy-aware).
- **Long-term memory that doesn't bloat the prompt** — Chuck keeps durable facts about you (your hardware, distro, handle, the projects you're building, standing preferences) in a local store, and quietly recalls only the *relevant* few each turn — never the whole thing. So he remembers your setup across chats without dragging a wall of text into every request. Everything he's saved is viewable and one-tap deletable from the **memory** button (★ = always-on core facts). He decides what's worth keeping and tells you when he does; nothing is stored silently.
- **Smart files (skills)** — teach him a task once and he saves it as a reusable *skill* under `~/.local/share/chucknorris/skills/` (a plain script you can read or edit). Ask for it again and it's one tap — nothing auto-runs, the run is still an approve-to-run card. Old versions are archived, never deleted.
- **Always knows "now"** — the current date, time and timezone are injected fresh every turn, so he never reasons from a stale "today" or hands you outdated info; combined with verify-first, what he tells you is current.
- **Recon / OSINT arsenal** — whois, DNS (`dig`/`host`/reverse), HTTP headers, TLS certs (`openssl s_client`), traceroute, ASN, server-IP geolocation (`ipinfo.io`), port/service views (`ss`), and breach-checking **your own** email. Full infrastructure recon, proposed as approve-to-run cards.
- **System fixes & installs** — pacman, AUR (paru/yay), CachyOS kernels (BORE/sched-ext), systemd, GRUB, mkinitcpio, keyring/mirrorlist repair, partial-upgrade recovery, orphans, GPU drivers, btrfs/snapper, chroot rescue. He finds the right tool, tells you where it lives, installs it, and shows you how to use and troubleshoot it.
- **Disk cleanup** — a read-only junk scan (pacman cache, orphans, journal, thumbnails, trash, coredumps) with one-tap cleanup cards.
- **Screen vision** — show him an error on your screen and he reads it.
- **File reading** — hand him a config, log or script and he works from its contents.
- **Voice** — reads replies aloud in a natural deep voice (Piper; espeak-ng fallback).
- **Saved chats** — every conversation is saved and reloadable; start fresh anytime.
- **Mullvad + Brave** — route all fetches through a proxy (Settings); links open in Brave.

**Clean output:** real titles, bold, code and bullets — no stray asterisks or hashes. **Enter sends** (Shift+Enter for a newline).

**Lean by design:** the base system prompt is tiny (~525 tokens) — it ships only *tool names* and the four rules. Deep playbooks (Arch/pacman, recon, code, media) load on demand *only* when a task needs them, so most turns cost almost nothing in tokens. This "mise en place" design lives in the `chucknorris_ext/` sidecar package (`skills.py`, `specs.py`), which is where new capability gets added without bloating every request.

## The one boundary

Chuck **never refuses, never moralizes, never lectures** on anything legal — mature/adult topics, security research, offensive tooling on your *own* targets, blunt opinions, all fine. He declines only four things, because they hurt people who aren't you: anything involving **minors**, anything **clearly illegal**, **malware/weapon-making**, and **locating or tracking a real person** (person-OSINT / doxxing). For that last one he'll help you check *your own* exposure instead. Everything else, he just does — and he does the whole job in one run before he reports back.

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

The installer does everything: GTK4 + libadwaita, screenshot tool, polkit, `pacman-contrib`, `pciutils`, `pkgfile`, `espeak-ng` + **Piper** and a natural voice model, `yt-dlp`, and the recon kit (`whois`, `bind`/`dig`, `traceroute`, `wget`, `netcat`), plus the app and art.

## Set up & run

Open **Settings**, paste a **SiliconFlow** key (<https://cloud.siliconflow.com/account/ak>) — or Chuck reuses **Basilisk's** automatically. Optional: a **proxy** (Mullvad). Then:

```bash
chucknorris
```

Talk to him. That's it.

---

<div align="center">

*"Chuck Norris doesn't read man pages. Man pages read Chuck Norris and take notes."*

**MIT.** Made by **The Priest** ⛧ — in memory of a legend.

</div>
