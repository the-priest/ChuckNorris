<div align="center">

<img src="assets/chucknorris-icon.png" alt="Chuck Norris" width="140">

# 🥋 CHUCK NORRIS

### An Arch / CachyOS grandmaster on your desktop. A tribute. He fixes anything, researches and verifies, shows pictures, downloads video, speaks — and you approve every step.

![version](https://img.shields.io/badge/version-4.0.0-b6892f?style=for-the-badge&labelColor=0b0b0d)
![tribute](https://img.shields.io/badge/1940-2026-b6892f?style=for-the-badge&labelColor=0b0b0d)
![distro](https://img.shields.io/badge/Arch%20%7C%20CachyOS-grandmaster-b6892f?style=for-the-badge&labelColor=0b0b0d)
![mode](https://img.shields.io/badge/non--autonomous-you%20approve%20every%20step-b6892f?style=for-the-badge&labelColor=0b0b0d)

</div>

---

## What this is

A native GTK4/libadwaita desktop assistant built as a tribute to **Chuck Norris (1940–2026)** — reborn as an extreme **Arch Linux / CachyOS** expert. Deadpan, unflappable, and very good at his job. He fixes and installs anything, finds the right tool for any task, researches the live web with citations, verifies the news, looks at your screen, reads your files, pulls up images, downloads video, talks in a gruff voice, and drops a Chuck Norris fact when the moment's right. Every fix is a command **you** approve — he never runs off on his own.

Chuck was famously amused by his own legend. This keeps it going.

## What he does

- **CachyOS/Arch grandmaster** — pacman, AUR (paru/yay), CachyOS kernels (BORE/sched-ext), systemd, GRUB, mkinitcpio, keyring/mirrorlist repair, partial-upgrade recovery, orphans, GPU drivers, btrfs/snapper, chroot rescue. **Tool finder:** names the tool, where it lives, the command to get it (`pkgfile`/`pacman -F` for what provides a command), and how to use + troubleshoot it.
- **🔎 Web research** — multi-source, grounded, cited. **📰 News** — reports only what 2+ sources corroborate, flags `[UNVERIFIED]`.
- **🖼 Images** — type a query, hit Images: pulls pictures into the chat (unfiltered — legal content, your machine). Click one to open the source in **Brave**.
- **⬇ Video** — paste a URL, hit Video: downloads via `yt-dlp` to `~/Downloads/ChuckNorris` (proxy-aware).
- **🔊 Voice** — reads replies aloud in a gruff character voice (`espeak-ng`). *(A synthetic tough-guy voice — not a clone of the real man's.)*
- **Eyes & files** — screenshots your screen and reads errors; paperclip a text file and he works from it.
- **Junk scanner + one-tap actions** — Update · GPU drivers · Fix keyring · Clean junk · Failed services.
- **Clean rendering** — proper titles, bold and code, bullet points. No stray asterisks or hashes.
- **Saved chats** — every conversation is saved to `~/.local/share/chucknorris/chats/`; the recent-chats button reloads any of them, and **New chat** starts fresh.
- **Mullvad + Brave** — set a proxy in Settings (e.g. Mullvad's SOCKS/HTTP) to route web/image/video fetches through it; links open in Brave. For a full tunnel, just connect Mullvad system-wide.

**Hands on a leash:** commands are approve-to-run cards, sudo via graphical polkit, destructive ones flagged red. He's a system + knowledge helper — **not a hacking tool**, and he **won't track or geolocate people**.

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

Installs the lot: GTK4 + libadwaita, screenshot tool, polkit, `pacman-contrib`, `pciutils`, `pkgfile`, `espeak-ng` (voice), `yt-dlp` (video), plus the app and art.

## Set up & run

Open **Settings**, paste a **SiliconFlow** key (<https://cloud.siliconflow.com/account/ak>) — or Chuck reuses **Basilisk's** automatically. Optional: set a **proxy** (Mullvad). Then:

```bash
chucknorris
```

---

<div align="center">

*"Chuck Norris doesn't kill zombie processes. He stares at them until they apologise and exit 0."*

**MIT.** Made by **The Priest** ⛧ — in memory of a legend.

</div>
