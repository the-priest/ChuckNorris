<div align="center">

<img src="assets/chucknorris-icon.png" alt="Chuck Norris" width="140">

# 🥋 CHUCK NORRIS

### The Arch / CachyOS grandmaster that lives on your desktop. You ask — he acts. No modes, no buttons, no excuses. A tribute.

![version](https://img.shields.io/badge/version-4.2.0-b6892f?style=for-the-badge&labelColor=0b0b0d)
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

- **Web search & browsing** — he searches multiple sources, opens and reads the actual pages, and answers grounded in them **with citations**. For news he cross-checks 2+ outlets and flags single-source claims `[UNVERIFIED]`.
- **Images** — ask to see something and he pulls the pictures straight into the chat. Click one to open the source in **Brave**.
- **Video/audio download** — give him a link, he grabs it with `yt-dlp` into `~/Downloads/ChuckNorris` (proxy-aware).
- **Recon / OSINT arsenal** — whois, DNS (`dig`/`host`/reverse), HTTP headers, TLS certs (`openssl s_client`), traceroute, ASN, server-IP geolocation (`ipinfo.io`), port/service views (`ss`), and breach-checking **your own** email. Full infrastructure recon, proposed as approve-to-run cards.
- **System fixes & installs** — pacman, AUR (paru/yay), CachyOS kernels (BORE/sched-ext), systemd, GRUB, mkinitcpio, keyring/mirrorlist repair, partial-upgrade recovery, orphans, GPU drivers, btrfs/snapper, chroot rescue. He finds the right tool, tells you where it lives, installs it, and shows you how to use and troubleshoot it.
- **Disk cleanup** — a read-only junk scan (pacman cache, orphans, journal, thumbnails, trash, coredumps) with one-tap cleanup cards.
- **Screen vision** — show him an error on your screen and he reads it.
- **File reading** — hand him a config, log or script and he works from its contents.
- **Voice** — reads replies aloud in a natural deep voice (Piper; espeak-ng fallback).
- **Saved chats** — every conversation is saved and reloadable; start fresh anytime.
- **Mullvad + Brave** — route all fetches through a proxy (Settings); links open in Brave.

**Clean output:** real titles, bold, code and bullets — no stray asterisks or hashes. **Enter sends** (Shift+Enter for a newline).

## The one boundary

Chuck helps with anything legal and doesn't moralize — mature/adult content included. He refuses only four things, because they hurt people who aren't you: anything involving **minors**, anything **clearly illegal**, **malware/weapon-making**, and **locating or tracking a real person** (person-OSINT / doxxing). For that last one he'll help you check *your own* exposure instead. Everything else, he just does.

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
