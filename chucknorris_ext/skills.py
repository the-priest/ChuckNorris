"""skills.py — Chuck's "smart files": reusable scripts he writes once and runs again.

A skill is a small, named, self-contained shell OR python snippet Chuck has
learned/authored (e.g. "rate the CachyOS mirrors", "show my top 10 biggest
packages"). They live as plain files under ~/.local/share/chucknorris/skills/
with a JSON sidecar of metadata, so they survive restarts and the user can read
or edit them. Running a skill still produces an approve-to-run command card in
the UI — nothing auto-executes.

Design mirrors Basilisk's skills.py (write / run / list, archived not deleted)
but is stdlib-only and desktop-appropriate. No network here; the app owns that.
"""
import os
import re
import json
import time
from pathlib import Path

SKILLS_DIR = Path.home() / ".local" / "share" / "chucknorris" / "skills"
ARCHIVE_DIR = SKILLS_DIR / ".archive"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,48}$")
_LANG_EXT = {"bash": "sh", "sh": "sh", "python": "py", "py": "py"}


def _slug(name):
    name = (name or "").strip().lower().replace(" ", "-")
    name = re.sub(r"[^a-z0-9._-]", "", name)
    return name[:48] or "skill"


def _meta_path(slug):
    return SKILLS_DIR / (slug + ".json")


def _body_path(slug, lang):
    return SKILLS_DIR / (slug + "." + _LANG_EXT.get(lang, "sh"))


def skill_write(name, lang, body, description=""):
    """Create/overwrite a skill. Returns (ok, message, run_command).

    run_command is what the app should turn into an approve-to-run card — we do
    NOT execute anything here.
    """
    slug = _slug(name)
    if not _NAME_RE.match(slug):
        return False, f"bad skill name {name!r}", None
    lang = (lang or "bash").strip().lower()
    if lang not in _LANG_EXT:
        return False, f"unsupported lang {lang!r} (use bash or python)", None
    body = (body or "").strip()
    if not body:
        return False, "empty skill body", None
    bpath = _body_path(slug, lang)
    # archive any prior version rather than clobbering silently
    if bpath.exists():
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            (ARCHIVE_DIR / f"{slug}.{ts}.{_LANG_EXT[lang]}").write_text(bpath.read_text())
        except Exception:
            pass
    bpath.write_text(body)
    try:
        os.chmod(bpath, 0o755)
    except Exception:
        pass
    meta = {"name": slug, "lang": lang, "description": description.strip()[:200],
            "file": bpath.name, "created": time.time()}
    _meta_path(slug).write_text(json.dumps(meta, indent=2))
    return True, f"saved skill '{slug}' ({lang})", skill_run_cmd(slug)


def skill_run_cmd(name):
    """The shell command that runs a saved skill (for an approve-to-run card)."""
    slug = _slug(name)
    for lang, ext in (("python", "py"), ("bash", "sh")):
        p = SKILLS_DIR / (slug + "." + ext)
        if p.exists():
            if ext == "py":
                return f"python3 {p}"
            return f"bash {p}"
    return None


def skill_list():
    """Return a compact list of saved skills: [(name, lang, description)]."""
    out = []
    for mp in sorted(SKILLS_DIR.glob("*.json")):
        try:
            m = json.loads(mp.read_text())
            out.append((m.get("name", mp.stem), m.get("lang", "?"),
                        m.get("description", "")))
        except Exception:
            continue
    return out


def skill_read(name):
    """Return (lang, body) for a saved skill, or (None, None)."""
    slug = _slug(name)
    for lang, ext in (("python", "py"), ("bash", "sh")):
        p = SKILLS_DIR / (slug + "." + ext)
        if p.exists():
            try:
                return lang, p.read_text()
            except Exception:
                return lang, None
    return None, None


def skill_forget(name):
    """Archive a skill (never hard-deletes). Returns (ok, message)."""
    slug = _slug(name)
    moved = False
    for ext in ("py", "sh", "json"):
        p = SKILLS_DIR / (slug + "." + ext)
        if p.exists():
            try:
                ts = time.strftime("%Y%m%d-%H%M%S")
                p.rename(ARCHIVE_DIR / f"{slug}.{ts}.{ext}")
                moved = True
            except Exception:
                pass
    return (moved, f"archived '{slug}'" if moved else f"no skill '{slug}'")


def skills_index_line():
    """One short line naming saved skills — cheap enough to include per turn."""
    names = [n for (n, _l, _d) in skill_list()]
    if not names:
        return ""
    return "Saved skills you can run: " + ", ".join(names[:20])
