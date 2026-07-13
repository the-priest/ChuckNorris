<div align="center">

<img src="assets/chucknorris-icon.png" alt="Chuck Norris" width="150">

# 🥋 CHUCK NORRIS

### An Arch / CachyOS grandmaster on your desktop. Fixes, installs, researches, verifies the news, reads your files — and you approve every step.

![version](https://img.shields.io/badge/version-3.0.0-14b8a6?style=for-the-badge&labelColor=0b0e10)
![license](https://img.shields.io/badge/license-MIT-14b8a6?style=for-the-badge&labelColor=0b0e10)
![distro](https://img.shields.io/badge/Arch%20%7C%20CachyOS-grandmaster-14b8a6?style=for-the-badge&labelColor=0b0e10)
![web](https://img.shields.io/badge/web%20%2B%20news-multi--source%20%2B%20cited-14b8a6?style=for-the-badge&labelColor=0b0e10)
![mode](https://img.shields.io/badge/non--autonomous-you%20approve%20every%20step-14b8a6?style=for-the-badge&labelColor=0b0e10)

</div>

---

## What Chuck is

Chuck Norris is a native GTK4/libadwaita desktop assistant built to be an **extreme Arch Linux / CachyOS expert** and an all-round smart helper. It fixes problems, installs anything, finds the right tool for any job and shows you how to use it, researches the live web with citations, verifies the news across multiple sources, looks at your screen, and reads your files. It shares Basilisk's button art and bones — with a friendly Tux where the serpent used to be — but it's a **builder and healer, not a hacker.**

Everything it can do:

- **CachyOS/Arch grandmaster** — pacman, AUR (paru/yay), CachyOS repos + kernels (BORE/sched-ext), systemd, GRUB/systemd-boot, mkinitcpio, keyring/mirrorlist repair, partial-upgrade recovery, orphans, GPU drivers, btrfs/snapper, chroot rescue. Ask it to fix or install *anything* and it hands you the exact commands.
- **Tool finder** — for any task it names the right tool, tells you where it lives (repo / AUR / flatpak) and the command to get it, uses `pkgfile`/`pacman -F` to find which package provides a missing command, and shows how to use and troubleshoot it.
- **Live-web research** — toggle **🔎 Web**: it searches multiple sources, reads them, and answers **grounded in what it found, with `[n]` citations** and a Sources list. Told to cross-check and prefer official docs.
- **Verified news** — hit **📰 News**, type a topic: it cross-checks multiple outlets and reports **only what's corroborated by 2+ sources**, labels single-source claims `[UNVERIFIED]`, flags spin and staleness, and cites everything.
- **Eyes** — screenshots your screen and reads the error with a vision model.
- **Reads files** — paperclip a text file (config, log, script) and Chuck works from its contents.
- **Junk scanner** + **one-tap actions** — Update · GPU drivers · Fix keyring · Clean junk · Orphans · Failed services.
- **Hands** — every fix is an approve-to-run **command card**; sudo via graphical polkit; destructive commands flagged red.

**Non-autonomous by design** — it proposes, you decide, scans are read-only, and it never claims to have run something it hasn't.

### What Chuck won't do

Chuck is a system + knowledge assistant, **not** a hacking or surveillance tool. It won't run offensive/attack tooling, and it **won't locate, de-anonymise, track or geolocate real people** (no OSINT person-hunting, no doxxing). Ask it to find or geolocate someone and it declines — it'll only help you check *your own* exposure. That line's deliberate.

---

## Install

One line (Arch/CachyOS first-class; also apt/dnf):

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

The installer does everything: GTK4 + libadwaita, screenshot tool, polkit, `pacman-contrib` (cleanup), `pciutils` (GPU) and `pkgfile` (tool lookup), plus the app, art and background.

---

## Set it up & use it

Open **Settings**, paste a **SiliconFlow** key (<https://cloud.siliconflow.com/account/ak>) — or Chuck reuses **Basilisk's key** automatically if it's installed. Then:

```bash
chucknorris
```

Ask it to fix or install anything, toggle **🔎 Web** for cited research, **📰 News** for verified headlines, the camera to show an error, or the paperclip to hand it a file. Approve the command cards it hands back. On first launch it reads safe, read-only facts about your box so its advice fits *your* machine.

---

## Accuracy

No model is incapable of being wrong, so Chuck is built to *check itself*: web and news answers are grounded in fetched sources and cited so you can verify; news needs 2+ corroborating sources; it proposes a command that **checks** rather than guessing when unsure; and it never claims to have run something it hasn't.

---

<div align="center">

## License

**MIT.** Yours to fork.

Made by **The Priest** ⛧

*grandmaster of your machine — answers to no one but you*

</div>
# ChuckNorrs
