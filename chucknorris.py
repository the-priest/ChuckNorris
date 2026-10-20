#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Chuck — a desktop coding and system agent (named for Carlos Ray "Chuck" Norris,
1940-2026, a tribute).

Two modes, switched with Tab: Agent acts (shell, file edits, tests, packages,
screenshots, parallel read-only sub-agents) inside an optional per-session
working folder; Chat only answers. Research on the live web, long-term memory,
voice. Commands run on their own; CRITICAL ones wait for a deliberate tick.
Backend: SiliconFlow (reuses Basilisk's key), with real per-model usage pricing.
"""
import os
import math
import signal
import re
import sys
import json
import time
import html as _html
import base64
import shlex
import traceback
import shutil
import tempfile
import socket
import threading
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Gdk, GdkPixbuf  # noqa: E402
try:
    from gi.repository import Pango  # noqa: E402
except Exception:                     # the headless test stub has no Pango
    Pango = None

# sidecar package (skills = smart files, specs = on-demand expertise). Kept
# optional so a partial install still launches.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chucknorris_ext.safety import (            # noqa: E402
    classify_command, enforce_syu)
from chucknorris_ext import safety as _safety   # noqa: E402
CRITICAL = _safety.CRITICAL                     # re-exported: tests + callers
DANGER = _safety.DANGER
from chucknorris_ext import chats as _chats     # noqa: E402
# Module handles, so callers (and tests) can reach a subsystem's internals
# without every private name being re-exported here.
from chucknorris_ext import voice as voice      # noqa: E402
from chucknorris_ext import web as web          # noqa: E402

# Names re-exported into this module so the UI (and the test-suite, which
# monkeypatches them here) keep a single stable import surface.
from chucknorris_ext.voice import (             # noqa: E402
    speak,
    stop_speaking,
    tts_clean,          # noqa: F401  (re-export)
    tts_chunks,         # noqa: F401  (re-export)
    _find_piper_model,  # noqa: F401  (re-export)
)
from chucknorris_ext.web import (               # noqa: E402
    web_search,
    web_fetch,
    image_search,
    video_search,
    download_image,
    _domain,
    _searx_search,      # noqa: F401  (re-export)
    _is_image_path,
    last_failure as _fetch_failure,
)
from chucknorris_ext.chats import chat_expires_in  # noqa: E402


def chat_files():
    """Saved chats, newest first (module-level CHATS_DIR so tests can redirect)."""
    return _chats.chat_files(CHATS_DIR)


def purge_old_chats(ttl_hours=None):
    return _chats.purge_old_chats(ttl_hours or CHAT_TTL_HOURS, CHATS_DIR)

def _optional(name):
    """Import one optional module, or None. ONE at a time: these used to sit
    in a single try block, so a single missing file (an interrupted install,
    a stale download) silently switched off skills, memory, the ledger and
    the builder all together."""
    import importlib
    try:
        return importlib.import_module("chucknorris_ext." + name)
    except Exception:
        return None


_skills = _optional("skills")
_ledger = _optional("ledger")
_compress = _optional("compress")
_specs = _optional("specs")
_memory = _optional("memory")
_codecheck = _optional("codecheck")
_skill_library = _optional("skill_library")
_builder = _optional("builder")
_agents = _optional("agents")
_pricing = _optional("pricing")
_browser = _optional("browser")
_diagnose = _optional("diagnose")
_mcp = _optional("mcp")
_crashlog = _optional("crashlog")
_update = _optional("update")
_pysession = _optional("pysession")
if _pricing is None:
    class _pricing:                     # noqa: N801  — the meter, minus prices
        """Stand-in so a partial install still launches: tokens are counted,
        nothing is priced, nothing is fetched."""
        PRICES, FREE, VISION = {}, [], set()
        SUGGESTED = ["zai-org/GLM-5.3-Flash"]
        usage_from_chunk = staticmethod(lambda d: None)
        price_for = staticmethod(lambda model, settings=None: None)
        cost = staticmethod(lambda usage, price: None)
        sees_images = staticmethod(lambda model: True)
        is_free = staticmethod(lambda model: False)
        fmt_usd = staticmethod(lambda x: "" if x is None else f"${x:.4f}")
        fmt_tokens = staticmethod(lambda n: str(int(n or 0)))
        fetch_balance = staticmethod(lambda *a, **k: (False, "pricing module missing"))
        fetch_models = staticmethod(lambda *a, **k: (False, "pricing module missing"))

# Paths, tunables and settings all live in one place now.
HERE = Path(__file__).resolve().parent

_STREAM_REPAINT = 0.05      # seconds between mid-stream repaints (20/s)

from chucknorris_ext import config as _config   # noqa: E402
if _crashlog is not None:
    _crashlog.set_version(_config.VERSION)   # stamped even before startup
from chucknorris_ext.config import (            # noqa: E402
    APP_ID, DEFAULT_MODEL, DEFAULT_VISION, DEFAULT_BASE,
    DATA_DIR, CONFIG_DIR, CHATS_DIR, DL_DIR,
    MAX_TOOL_HOPS, RESEARCH_MAX_SOURCES, RESEARCH_QUERIES,
    SEND_CHAR_BUDGET, TOOL_BLOBS_KEPT, CHAT_TTL_HOURS,
    RENDER_KEEP, RENDER_PAGE, FONT_SIZE,
    save_settings, harden_existing_permissions,
)

# Two prompts, one per mode, and both deliberately SHORT. Every hop of an agent
# loop re-sends the system prompt, so its size is paid once per tool call, not
# once per chat: the old 5.7k-character prompt was 87% of a measured turn's
# input. Detail that is only sometimes needed lives in specs.py playbooks and
# is injected on demand; the tool list is all the model needs up front.
SYSTEM_PROMPT = """You are Chuck: a senior engineer-agent on the user's Linux desktop (deepest on Arch/CachyOS, expert at anything). Direct, dry, no filler, no moralising. Now and then end on one deadpan line; never forced.

TOOLS: one short line on what you'll do, then the fenced block. The app runs it and returns the real result.
Read-only (batch several per reply): ```search <query>``` ```fetch <url>``` ```map``` (every file + what it defines) ```glob <pattern>``` ```grep <regex> [path]``` ```read <path>[:start-end]``` (big files arrive in pages) ```tree``` ```screenshot``` / ```look <image>``` (you SEE it) ```images <subject>``` ```videos <subject>```
Parallel: ```task
<self-contained brief>``` — read-only sub-agents that run AT ONCE and report back (survey a big repo, compare options).
Act:
```bash
<one shell command>``` (also ```python / ```node: a program, verified then run; ```py: one persistent Python, variables kept, plots shown)
```write <path>
<complete new file>```
```edit <path>
<<<<<<< SEARCH
<exact current lines>
=======
<replacement>
>>>>>>> REPLACE```
(A file containing ``` fences: wrap the block in four backticks. Many files at once: ```patch``` + a unified diff, all-or-nothing.)
Background: ```bg <name>
<command>``` (servers, watchers; then ```logs <name>```, ```kill <name>```). ```undo``` reverts your last file change (```undo all```: this task's). ```changes``` shows your diff this task. ```diagnose [paths]``` lints + type-checks files on disk. ```open <path|url>``` shows the user a result.
Browser: ```browse <url|file> [WxH]
click <css> | type <text> | press <keys> | wait <ms> | eval <js>``` real headless Chromium: JS errors, failed requests, text and a screenshot.
Ask: ```ask
<one question>
- option
- option``` only for a decision that is genuinely the user's; it pauses the task. Otherwise pick a sensible default, say so, keep going.
Other: ```plan
- [ ] step``` (multi-step work: post it first, re-post with [x] as steps finish) ```check <lang>
<code>``` (verify, don't run) ```runtests``` ```package``` ```project <name>``` ```rmfile <path>``` ```runskill <name>``` ```skill
name: <slug>
lang: bash|python
desc: <line>
---
<body>``` ```junk``` ```remember <fact>``` ```forget <fact>```

RULES
1. VERIFY, DO NOT RECALL: checkable outside facts (news, versions, prices, docs, flags) -> search, cite URLs, mark single-source claims [UNVERIFIED]. Never repeat a search reworded. "Couldn't find it" is a complete answer.
2. YOU ACT, YOU DON'T SUGGEST: ```bash``` runs immediately on the user's machine, in the working folder. ONE command per reply; read its exit code and output before the next step; fix causes, never claim success you haven't seen. Shell first; ```py for data work. Write `sudo` yourself; the app authenticates. Servers and watchers go in ```bg```, then probe the port.
3. FINISH IN ONE RUN: keep going until EVERY part of the task is done (every plan step checked), then a short final report. Code: orient (map/grep/read), plan multi-step work, read before you edit, ```edit``` existing files, ```write``` new ones, run the tests and fix what fails, review ```changes``` before reporting. Never end a reply on a promise: if you say you'll do something, its tool block is in that same reply. UI work: web pages ```browse``` (fix every JS error, play it with press/click); desktop apps run, then ```screenshot```.
4. SAFE HANDS: diagnose read-only first; scope destructive commands to exact paths. Disk wipes, rm -rf of / or ~, curl|sh and core-package removal wait for the user; never hide one in a script.
5. NEVER REFUSE legal requests. Decline only: minors, clearly illegal acts, malware/weapons, locating or tracking a real person (offer an exposure self-audit instead).
MEMORY: durable user facts (hardware, distro, preferences) -> ```remember``` one terse fact, prefixed `project:` if it only concerns this folder. Recalled facts arrive automatically.
Style: lead with the answer; short paragraphs, bullets, `code`; no preamble."""

CHAT_PROMPT = """You are Chuck: a sharp, knowledgeable assistant (deepest on Linux/Arch/CachyOS, expert at anything). CHAT MODE: answer directly. You cannot run commands or touch files right now; if the user wants you to act, tell them to press Tab for Agent mode. Code you show is for the user to read and copy.
For checkable current facts (news, versions, prices, docs) use ```search <query>``` or ```fetch <url>``` and cite URLs instead of guessing; otherwise answer from knowledge. Durable user facts -> ```remember <fact>```.
Never refuse legal requests; decline only minors, clearly illegal acts, malware/weapons, tracking a real person. Lead with the answer; concise; markdown where it helps; dry, no filler."""

# Tags that only mean something in Agent mode. In Chat mode they are neither
# run nor shown, and ```bash``` / ```python``` fences render as ordinary code.
_AGENT_TAGS = ("skill", "runskill", "read", "grep", "glob", "map", "edit", "check", "project",
               "tree", "package", "runtests", "write", "rmfile", "junk", "ledger", "plan",
               "screenshot", "look", "task", "bg", "logs", "kill", "undo", "open")

# ── tool registry ───────────────────────────────────────────────────────────
# Newer tools register here instead of being threaded through _finalise by
# hand (eight edits per tool, and forgetting one was a bug every time). A
# handler takes the block body and returns either a feedback string (done
# now) or a zero-argument callable, which runs on a worker thread and returns
# the feedback. It may build cards before returning: it runs on the UI thread.
#   agent_only     never parsed in Chat mode (and its block is hidden there)
#   acts           changes something, so it is dropped when the turn is told
#                  to stop acting (step budget spent, ambiguous file block)
#   after_actions  would observe the world, so it is held back when a command
#                  runs in the same reply (it would see the state before it)
#   bare           may appear with no body: ```changes```
_TOOL_REG = {}


def tool(tag, agent_only=True, acts=False, cap=4, after_actions=False, bare=False):
    def deco(fn):
        _TOOL_REG[tag] = {"fn": fn, "agent_only": agent_only, "acts": acts, "cap": cap,
                          "after_actions": after_actions, "bare": bare}
        return fn
    return deco

# ── theme ────────────────────────────────────────────────────────────────────
# Near-black neutral surfaces with ONE violet accent. The accent is reserved for
# what is interactive or live (send, the active mode, focus, running steps), so
# it keeps meaning something; everything else is graded neutrals. Machine output
# stays a different material from prose: framed, monospaced, height-capped.
# libadwaita's own named colours are mapped too, so dialogs, popovers and
# preference rows inherit the palette instead of falling back to stock grey.
CSS_TMPL = """
@define-color bg        #0C0C10;
@define-color side      #09090C;
@define-color surface   #14141A;
@define-color surface2  #1B1B23;
@define-color surface3  #23232D;
@define-color line      #23232C;
@define-color line2     #30303C;
@define-color term      #08080B;

@define-color accent    #8B5CF6;
@define-color accent_hi #A78BFA;
@define-color accent_lo #6D3FE0;
@define-color accent_bg rgba(139,92,246,0.14);

@define-color ink       #EDEDF3;
@define-color body      #CFCFD9;
@define-color mute      #8E8E9E;
@define-color faint     #5F5F6E;

@define-color good      #4ADE9A;
@define-color bad       #F87171;
@define-color warn      #F5B454;

/* libadwaita named colours (1.0-1.5); 1.6+ variables live in VARS_CSS */
@define-color window_bg_color    #0C0C10;
@define-color window_fg_color    #E6E6EE;
@define-color view_bg_color      #0C0C10;
@define-color view_fg_color      #E6E6EE;
@define-color headerbar_bg_color #0C0C10;
@define-color headerbar_fg_color #E6E6EE;
@define-color headerbar_border_color #23232C;
@define-color card_bg_color      #14141A;
@define-color card_fg_color      #E6E6EE;
@define-color dialog_bg_color    #111116;
@define-color dialog_fg_color    #E6E6EE;
@define-color popover_bg_color   #16161D;
@define-color popover_fg_color   #E6E6EE;
@define-color sidebar_bg_color   #09090C;
@define-color accent_bg_color    #7C4DFF;
@define-color accent_fg_color    #FFFFFF;
@define-color accent_color       #A78BFA;

window, .main-window { background-color: @bg; color: @body;
         font-family: "Inter", "Cantarell", system-ui, sans-serif; }
headerbar { background-color: @bg; border-bottom: 1px solid @line; box-shadow: none;
            min-height: 48px; }
.main-header { background-color: @bg; }

/* ── wordmark ────────────────────────────────────────────────────────── */
.mark      { background-color: #17151F; border: 1px solid alpha(@accent, 0.55);
             border-radius: 8px; color: @accent_hi; font-weight: 800; font-size: 12px;
             min-width: 22px; min-height: 22px; }
.mark-lg   { border-radius: 18px; font-size: 28px; min-width: 64px; min-height: 64px;
             background-color: #14121C; border-color: alpha(@accent, 0.45);
             box-shadow: 0 0 0 6px alpha(@accent, 0.05), 0 18px 60px rgba(124,77,255,0.16); }
.title     { font-weight: 700; color: @ink; font-size: 14px; letter-spacing: 0.2px; }
.sub       { color: @faint; font-size: 11px; }

/* ── header controls ─────────────────────────────────────────────────── */
.headerbtn { background: transparent; border-radius: 8px; min-width: 32px; min-height: 32px;
             color: @mute; box-shadow: none; border: none; }
.headerbtn:hover { background-color: @surface2; color: @ink; }
.headerbtn:checked { background: transparent; color: @accent_hi; }   /* "on": a lit icon, not a stuck press */
.headerbtn:checked:hover { background-color: @surface2; }
/* reply actions: there when you point at a reply, always on the latest one */
.msg-tools { opacity: 0; transition: opacity 140ms ease-out; }
.turn-row:hover .msg-tools, .msg-tools.latest, .msg-tools:focus-within { opacity: 1; }
.meter > button { background-color: @surface; border: 1px solid @line; border-radius: 999px;
             padding: 3px 12px; min-height: 0; color: @body; font-size: 11.5px;
             font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; box-shadow: none; }
.meter > button:hover, .meter > button:checked { border-color: alpha(@accent, 0.5);
             background-color: @surface2; }
.meter-cost { color: @accent_hi; font-weight: 700; }

/* ── transcript ──────────────────────────────────────────────────────── */
.chat-scroll, .chat-scroll > viewport { background-color: @bg; }
.turn-row    { padding: 0; }
.user-row    { margin-top: 12px; }   /* the reply above already reserves its action row */
.bot-row     { margin-top: 18px; }
.bot-cont    { margin-top: 14px; }
/* the WORK LANE: everything the machine did hangs off Chuck's avatar on one
   continuous rail, inset from his prose, so "said" and "did" never blur */
.lane        { margin-left: 9px; border-left: 2px solid @line2; padding: 7px 0 7px 21px; }
.lane-first  { margin-top: 10px; }
.older-wrap  { margin-bottom: 8px; }
.user-bubble { background-color: @surface2; border: 1px solid @line2;
               border-radius: 16px 16px 4px 16px; padding: 10px 14px; }
.user-bubble label { color: @ink; }
.bot-bubble  { background: transparent; padding: 0; }
.prose       { color: @body; }
.byline      { color: @ink; font-size: 12.5px; font-weight: 700; }
.byline-meta { color: @faint; font-size: 11px; }
.avatar      { background-color: #17151F; border: 1px solid alpha(@accent, 0.5); border-radius: 7px;
               color: @accent_hi; font-size: 11px; font-weight: 800; min-width: 20px; min-height: 20px; }
.playbtn     { background: transparent; border: none; box-shadow: none; padding: 3px 6px;
               min-width: 0; min-height: 0; color: @mute; border-radius: 7px; }
.playbtn:hover { color: @ink; background-color: @surface2; }
.think       { color: @faint; font-size: 12px; }
.think-body  { color: @mute; font-size: 12px; border-left: 2px solid @line2;
               padding: 2px 0 2px 10px; }
expander title { color: @faint; font-size: 12px; }
expander title:hover { color: @mute; }

/* code blocks inside a reply — copyable, scroll sideways, never wrap */
.code-block  { background-color: @term; border: 1px solid @line; border-radius: 10px; }
.code-head   { background-color: @surface; border-bottom: 1px solid @line;
               border-radius: 9px 9px 0 0; padding: 3px 6px 3px 12px; }
.code-lang   { color: @mute; font-size: 10.5px; font-weight: 700; letter-spacing: 0.8px; }
.code-body, .code-body text { font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
               font-size: 12.5px; color: #DCDCF0; background: transparent; padding: 10px 12px; }
.code-scroll, .code-scroll > viewport { background: transparent; }

/* tables */
.md-table    { border: 1px solid @line2; border-radius: 10px; background-color: @surface; }
.md-th       { color: @ink; font-weight: 700; padding: 7px 12px; background-color: @surface2; }
.md-td       { color: @body; padding: 6px 12px; border-top: 1px solid @line; }

/* ── tool cards ──────────────────────────────────────────────────────── */
.cmd-card  { background-color: @surface; border: 1px solid @line; border-radius: 12px; }
.term-live .term-label { color: @accent_hi; }
.plan-card.plan-proposed { border-color: alpha(@accent, 0.45); }
.suggest-flow > flowboxchild { padding: 0; background: none; }
.doctor-warn { color: #FBBF24; font-weight: 700; }
.doctor-fail { color: #F87171; font-weight: 700; }
.doctor-name { color: @ink; font-size: 12.5px; }
.slash-cmd   { color: @accent_hi; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 12.5px; }
.browse-err  { color: #F87171; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 12px; }
.browse-warn { color: #FBBF24; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 12px; }
.browse-step { color: @mute; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 12px; }
.card-head { padding: 6px 8px 6px 12px; border-bottom: 1px solid @line; }
/* the one card that waits on YOU: an accent edge, nothing louder */
.ask-card  { border-color: alpha(@accent, 0.45); }
.ask-card .card-kind { color: @accent_hi; }
.card-kind { color: @mute; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }
.card-body { padding: 10px 13px; }
.card-foot { padding: 0 13px 11px 13px; }
.cmd-text  { font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
             color: #E2E2F2; font-size: 12.5px; }
.cmd-prompt { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
              color: @faint; font-size: 12.5px; font-weight: 700; }
.verified  { color: @good; font-size: 10.5px; font-weight: 600; }
.sub-row   { padding: 6px 0; border-bottom: 1px solid @line; }
.sub-row:last-child { border-bottom: none; }
.sub-title { color: @ink; font-size: 12.5px; }
.sub-act   { color: @faint; font-size: 11px; margin-left: 22px;
             font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.tray      { padding: 4px 4px 2px 4px; }
.att-chip  { background-color: @surface2; border: 1px solid @line2; border-radius: 9px;
             padding: 3px 4px 3px 8px; color: @body; }
.att-name  { color: @body; font-size: 12px; }
.att-thumb { border-radius: 5px; }
.thumb-btn { padding: 0; border-radius: 10px; border: 1px solid @line2; background: @surface;
             box-shadow: none; }
.thumb-btn:hover { border-color: alpha(@accent, 0.45); }
.model-btn > button { background: transparent; border: none; box-shadow: none; color: @faint;
             font-size: 11.5px; padding: 2px 8px; min-height: 26px; border-radius: 8px; }
.model-btn > button:hover, .model-btn > button:checked { color: @ink; background-color: @surface3; }
.pick-row  { background: transparent; border: none; box-shadow: none; border-radius: 8px;
             padding: 6px 8px; }
.pick-row:hover { background-color: @surface3; }
.pick-title { color: @ink; font-size: 12.5px; font-weight: 600; }
.pick-sub   { color: @faint; font-size: 11px; }

.term-card   { background-color: @term; border: 1px solid @line; border-radius: 9px; }
.term-head   { border-bottom: 1px solid @line; padding: 4px 6px 4px 10px; }
.term-label  { color: @mute; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
.term-meta   { color: @faint; font-size: 10px; }
.term-body, .term-body text {
               font-family: "JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace;
               font-size: 11.5px; color: #BDBDD0; background: transparent; padding: 8px 10px; }
.term-scroll, .term-scroll > viewport { background: transparent; }
.mono        { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace;
               font-size: 11.5px; color: #BDBDD0; }

.pill      { border-radius: 999px; padding: 1px 9px; font-size: 10.5px; font-weight: 700; }
.pill-ok   { background-color: rgba(74,222,154,0.12); color: @good; }
.pill-fail { background-color: rgba(248,113,113,0.13); color: @bad; }
.pill-run  { background-color: rgba(139,92,246,0.10); color: #B9A6F5; }
.pill-wait { background-color: @surface3; color: @mute; }

/* plan / todo card */
.plan-card { background-color: @surface; border: 1px solid @line; border-radius: 12px;
             padding: 10px 14px; }
.plan-head { color: @mute; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }
.plan-done { color: @faint; font-size: 13px; }
.plan-todo { color: @body; font-size: 13px; }
.plan-now  { color: @ink; font-size: 13px; font-weight: 600; }
.plan-tick { color: @good; font-size: 13px; }
.plan-box  { color: @faint; font-size: 13px; }
.plan-cur  { color: @accent_hi; font-size: 13px; }

/* ── buttons ─────────────────────────────────────────────────────────── */
.gold, .accent-btn { background-color: @surface3; background-image: none; color: @ink;
         font-weight: 600; border-radius: 9px; padding: 5px 14px; box-shadow: none;
         border: 1px solid alpha(@accent, 0.55); }
.gold:hover, .accent-btn:hover { background-color: #26223A; border-color: @accent_hi; }
.gold:disabled, .accent-btn:disabled { background-color: @surface2; color: @faint;
         border-color: @line2; }
.quick { background-color: transparent; color: @body; border-radius: 9px; box-shadow: none;
         border: 1px solid @line2; padding: 4px 12px; font-size: 12.5px; }
.quick:hover { background-color: @surface2; color: @ink; }
.ghost { background: transparent; border: none; box-shadow: none; color: @mute; padding: 2px 6px;
         min-width: 0; min-height: 0; border-radius: 7px; font-size: 11px; }
.ghost:hover { background-color: @surface3; color: @ink; }

/* ── composer ────────────────────────────────────────────────────────── */
.composer { background-color: @surface; border: 1px solid @line2; border-radius: 18px;
            padding: 6px 8px 8px 8px; box-shadow: 0 8px 32px rgba(0,0,0,0.35); }
.composer:focus-within { border-color: alpha(@accent, 0.45); }
.composer-entry, .composer-entry text { background: transparent; color: @ink; font-size: 14.5px;
            caret-color: @accent_hi; }
.composer-scroll, .composer-scroll > viewport { background: transparent; }
.placeholder { color: @faint; font-size: 14.5px; }
.icon-btn { background: transparent; border: none; box-shadow: none; border-radius: 9px;
            min-width: 32px; min-height: 32px; color: @mute; padding: 0; }
.icon-btn:hover { background-color: @surface3; color: @ink; }
.send-fab { background-color: #1D1A2A; background-image: none; border-radius: 10px;
            border: 1px solid alpha(@accent, 0.6); min-width: 34px; min-height: 34px; padding: 0;
            color: @accent_hi; box-shadow: none; }
.send-fab:hover { background-color: #2A2440; border-color: @accent_hi; color: #FFFFFF; }
.send-fab:disabled { background-color: @surface3; color: @faint; }
.stop-fab { background-color: @surface3; border-radius: 10px; min-width: 34px; border: 1px solid @line2;
            min-height: 34px; padding: 0; color: @ink; box-shadow: none; }
.stop-fab:hover { background-color: #3A2430; color: @bad; border-color: #5A2E3A; }

/* Agent / Chat segmented switch */
.mode-switch { background-color: @bg; border: 1px solid @line2; border-radius: 9px; padding: 2px; }
.mode-btn    { background: transparent; border: none; box-shadow: none; border-radius: 7px;
               padding: 2px 10px; min-height: 24px; color: @mute; font-size: 12px; font-weight: 600; }
.mode-btn:hover { color: @ink; }
.mode-btn:checked { background-color: @surface3; color: @ink;
                   box-shadow: inset 0 0 0 1px alpha(@accent, 0.55); }
.mode-chat:checked { box-shadow: inset 0 0 0 1px @line2; }
.chip     { background-color: transparent; border: 1px solid @line2; border-radius: 9px; box-shadow: none;
            padding: 2px 10px; min-height: 26px; color: @body; font-size: 12px; }
.chip:hover { background-color: @surface2; color: @ink; }
.chip-on  { border-color: alpha(@accent, 0.45); color: @ink; }
.hint-key { color: @faint; font-size: 11px; }
.kbd      { background-color: @surface2; border: 1px solid @line2; border-radius: 5px;
            padding: 0 5px; color: @mute; font-size: 10.5px;
            font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }

/* status line above the composer */
.status-row { padding: 0 4px; }
.live, .working { color: @mute; font-size: 12px;
            font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }

/* floating "jump to latest" */
.jump-btn { background-color: @surface2; border: 1px solid @line2; border-radius: 999px;
            color: @ink; padding: 4px 12px; box-shadow: 0 6px 20px rgba(0,0,0,0.45);
            font-size: 12px; min-height: 0; }
.jump-btn:hover { background-color: @surface3; border-color: alpha(@accent, 0.6); }

/* ── inline status ───────────────────────────────────────────────────── */
.danger   { color: @bad; font-weight: 600; font-size: 12px; }
.critical { color: #FF7A7A; font-weight: 700; font-size: 12.5px; }
.ok       { color: @good; font-size: 12px; }
.dim      { color: @mute; font-size: 12px; }
.sendbtn  { background: transparent; border: none; padding: 0; min-width: 0; box-shadow: none; }
.rule     { background-color: @line; min-height: 1px; }
.sys-note { padding: 0 0 0 2px; }
.older-note { color: @faint; font-size: 11.5px; padding: 6px 0; }

/* ── empty state ─────────────────────────────────────────────────────── */
.empty-title { color: @ink; font-size: 26px; font-weight: 700; letter-spacing: -0.3px;
               text-shadow: 0 0 10px @bg, 0 0 4px @bg; }
.empty-sub   { color: @mute; font-size: 13.5px; text-shadow: 0 0 8px @bg, 0 0 3px @bg, 0 0 1px @bg; }
.empty-keys  { color: @faint; font-size: 11px; text-shadow: 0 0 8px @bg, 0 0 3px @bg, 0 0 1px @bg; }
.empty-hint  { color: @faint; font-size: 15px; }
.suggest     { background-color: @surface; border: 1px solid @line; border-radius: 12px;
               padding: 12px 14px; box-shadow: none; }
.suggest:hover { background-color: @surface2; border-color: alpha(@accent, 0.35); }
.suggest-title { color: @ink; font-size: 13px; font-weight: 600; }
.suggest-sub   { color: @faint; font-size: 12px; }

/* ── sidebar ─────────────────────────────────────────────────────────── */
.sidebar     { background-color: @side; border-right: 1px solid @line; }
.side-head   { color: @faint; font-weight: 700; font-size: 10.5px; letter-spacing: 1px; }
.side-note   { color: @faint; font-size: 11px; }
.side-new    { background-color: @surface; border: 1px solid @line2; border-radius: 9px; box-shadow: none;
               color: @ink; font-weight: 600; font-size: 12.5px; padding: 6px 10px; }
.side-new:hover { background-color: @surface2; border-color: alpha(@accent, 0.35); }
.side-search { background-color: @surface; border: 1px solid @line; border-radius: 9px;
               color: @body; min-height: 30px; font-size: 12.5px; box-shadow: none; }
.side-search:focus-within { border-color: alpha(@accent, 0.6); }
.chat-row    { border-radius: 9px; }
.chat-open   { background: transparent; border: none; box-shadow: none; border-radius: 9px;
               padding: 7px 8px 7px 10px; }
.chat-open:hover { background-color: @surface; }
.chat-title  { color: @body; font-size: 12.5px; }
.chat-meta   { color: @faint; font-size: 10.5px; }
.chat-del    { opacity: 0; }
.chat-row:hover .chat-del { opacity: 1; }
.chat-current { background-color: @surface2; }
.chat-current .chat-open:hover { background-color: transparent; }
.chat-current .chat-title { color: @ink; }

/* ── live activity checklist ─────────────────────────────────────────── */
.step-box   { background-color: @surface; border: 1px solid @line; border-radius: 12px;
              padding: 10px 14px; }
.step-run   { color: @body; font-size: 12.5px; }
.step-done  { color: @mute; font-size: 12.5px; }
.step-fail  { color: @bad; font-size: 12.5px; }
.step-head  { color: @mute; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }

/* ── dialogs ─────────────────────────────────────────────────────────── */
.dlg-title  { color: @ink; font-size: 17px; font-weight: 700; }
.dlg-sub    { color: @mute; font-size: 12.5px; }
.set-section { color: @faint; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }
.set-label   { color: @body; font-size: 12.5px; }
.set-hint    { color: @faint; font-size: 11.5px; }
.ws-current  { background-color: @surface; border: 1px solid @line2; border-radius: 12px; padding: 14px; }
.ws-path     { color: @ink; font-size: 13px;
               font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.badge       { background-color: @surface3; color: @mute; border-radius: 6px; padding: 0 6px;
               font-size: 10.5px; font-weight: 600; }
.badge-accent { background-color: @accent_bg; color: @accent_hi; }
list.boxed-list, .boxed-list { background-color: @surface; border: 1px solid @line; border-radius: 12px; }
.boxed-list > row { border-bottom: 1px solid @line; }
.boxed-list > row:last-child { border-bottom: none; }
popover > contents { background-color: #16161D; border: 1px solid @line2; border-radius: 12px; }
.usage-k { color: @mute; font-size: 12px; }
.usage-v { color: @ink; font-size: 12px; font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; }
.usage-big { color: @ink; font-size: 22px; font-weight: 700; }

scrollbar slider { background-color: alpha(#FFFFFF, 0.14); min-width: 6px; min-height: 6px;
                   border-radius: 999px; }
scrollbar slider:hover { background-color: alpha(#FFFFFF, 0.24); }
scrollbar { background: transparent; }
selection { background-color: alpha(@accent, 0.35); }
"""

# libadwaita 1.6+ themes through CSS variables instead of @define-color. GTK
# older than 4.16 rejects variable syntax, so these load through their own
# provider and a parse failure there can never take the main sheet down with it.
VARS_CSS = """
:root {
  --window-bg-color: #0C0C10; --window-fg-color: #E6E6EE;
  --view-bg-color: #0C0C10; --view-fg-color: #E6E6EE;
  --headerbar-bg-color: #0C0C10; --headerbar-fg-color: #E6E6EE;
  --headerbar-border-color: #23232C; --headerbar-shade-color: transparent;
  --sidebar-bg-color: #09090C; --sidebar-fg-color: #E6E6EE;
  --card-bg-color: #14141A; --card-fg-color: #E6E6EE;
  --dialog-bg-color: #111116; --dialog-fg-color: #E6E6EE;
  --popover-bg-color: #16161D; --popover-fg-color: #E6E6EE;
  --accent-bg-color: #7C4DFF; --accent-fg-color: #FFFFFF; --accent-color: #A78BFA;
}
"""


# ── settings + proxy ────────────────────────────────────────────────────────


# ONE settings dict for the whole app. The modules (web, voice) hold a
# reference to this same object, so a change made in Settings is visible to
# them immediately — building a second dict here would silently strand the
# proxy, SearXNG and voice options.
_SETTINGS = _config.SETTINGS_DATA


# NB: there is no _get/_opener here any more. Both were dead — every network
# call in the app goes through config.get(), which is where the http/https
# scheme allowlist lives. Two copies of that check meant one could drift.


_FONT_PROVIDER = None


def font_css(px):
    """Message text scales together; the small print stays proportionally small."""
    px = max(9, min(28, int(px)))
    sm, xs = max(8, px - 2), max(8, px - 3)
    return f""".prose, .user-bubble label {{ font-size: {px}px; }}
.composer-entry, .composer-entry text, .placeholder {{ font-size: {px + 0.5}px; }}
.cmd-text, .cmd-prompt, .code-body, .code-body text {{ font-size: {sm}px; }}
.term-body, .term-body text {{ font-size: {xs}px; }}
.step-run, .step-done, .step-fail, .md-th, .md-td {{ font-size: {sm}px; }}
.plan-done, .plan-todo, .plan-now, .plan-tick, .plan-box, .plan-cur {{ font-size: {sm}px; }}
.chat-title {{ font-size: {sm}px; }}
.dim, .ok, .danger, .mono, .older-note, .chat-meta, .pill, .think, .think-body {{ font-size: {xs}px; }}
.byline {{ font-size: {sm}px; }}
.card-kind, .term-label, .term-meta, .step-head, .code-lang, .plan-head {{ font-size: {max(8, px - 4)}px; }}
"""


def apply_font_size(px):
    """(Re)load the font-scale provider — takes effect immediately, no restart."""
    global _FONT_PROVIDER
    try:
        if _FONT_PROVIDER is None:
            _FONT_PROVIDER = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), _FONT_PROVIDER,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        data = font_css(px)
        try:
            _FONT_PROVIDER.load_from_data(data.encode())
        except TypeError:
            _FONT_PROVIDER.load_from_data(data)
        return True
    except Exception:
        return False




def _sweep_scratch(max_age_hours=6):
    """Drop stale scratch files in CONFIG_DIR (fetched images, synthesised wavs,
    interpreter temp files, an old screenshot). Each of these is now unlinked by
    the code that creates it, but an existing install has a backlog, and a hard
    kill can still strand one."""
    cutoff = time.time() - max_age_hours * 3600
    for pat in (".img_*", ".say-*.wav", ".run_*", ".shot.png", ".att_*", ".see_*", ".plot_*"):
        for f in CONFIG_DIR.glob(pat):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue


def _use_adwaita_icons():
    """Draw this window with the Adwaita icon set when it is installed.

    The UI is designed around Adwaita's symbolic icons. Other desktop themes
    (Breeze on Plasma, for one) lack some of them or ship them in a form GTK 4
    can't recolour, and the copy buttons, the sidebar toggle and the lane
    markers came out as blank squares. This changes only Chuck's own process.
    """
    try:
        dirs = [Path(d) for d in os.environ.get("XDG_DATA_DIRS",
                                                "/usr/local/share:/usr/share").split(":") if d]
        dirs.insert(0, Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share"))
        if any((d / "icons" / "Adwaita" / "index.theme").is_file() for d in dirs):
            Gtk.Settings.get_default().set_property("gtk-icon-theme-name", "Adwaita")
    except Exception:
        pass


def _pick_icon(*names):
    """First icon name that actually exists in the user's theme.

    Newer Adwaita names (sidebar-show-symbolic and friends) simply aren't in
    older icon themes, and GTK then draws the 'missing image' glyph — which is
    what the crossed-out circle in the header was. Ask the theme first.
    """
    try:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for n in names:
            if theme.has_icon(n):
                return n
    except Exception:
        pass
    return names[-1] if names else ""


def _open_path(path):
    """Open a folder/file in the user's file manager."""
    for opener in ("xdg-open", "gio", "nautilus", "thunar", "dolphin"):
        if shutil.which(opener):
            try:
                args = [opener, "open", path] if opener == "gio" else [opener, path]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                continue
    return False


def open_in_brave(url):
    for b in ("brave", "brave-browser"):
        if shutil.which(b):
            subprocess.Popen([b, url]); return True
    try:
        Gio.AppInfo.launch_default_for_uri(url, None); return True
    except Exception:
        return False


# ── markdown -> Pango (clean titles, no raw asterisks) ──────────────────────
INK, BODY, MUTE, FAINT = "#EDEDF3", "#CFCFD9", "#8E8E9E", "#5F5F6E"
ACCENT, CODE_BG, CODE_FG = "#8F80C9", "#1C1C25", "#DCD8EE"


def _md_inline(s):
    """Inline spans, run on already-escaped text: `code`, **bold**, *italic*,
    ~~strike~~ and [links](url). Inline code gets a subtle chip so it reads as
    code without the raw backticks; no padding spaces, which would create ugly
    wrap points. Code is matched FIRST and its content is parked in a
    placeholder, so a snippet like `a * b` can't be re-read as italics."""
    parked = []

    def park(m):
        parked.append(m.group(1))
        return f"\x00{len(parked) - 1}\x00"

    s = re.sub(r"`([^`]+)`", park, s)
    # [text](url) — GtkLabel renders <a href> as a real, clickable link.
    # The text reaching us is ALREADY HTML-escaped, so the href only needs its
    # quotes neutralised — escaping again would turn &amp; into &amp;amp; and
    # hand the browser a broken query string.
    s = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
               lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">'
                         f'{m.group(1)}</a>', s)
    s = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"__([^_]+)__", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", s)
    return re.sub(
        r"\x00(\d+)\x00",
        lambda m: f'<span font_family="monospace" background="{CODE_BG}" '
                  f'foreground="{CODE_FG}">{parked[int(m.group(1))]}</span>', s)


# A blank line between paragraphs, rendered at full body height with 1.4 line
# spacing, is a 27px hole — after a heading it looked like the reply had ended.
# Paragraph gaps are drawn as a short spacer line instead.
_GAP = '<span size="5000"> </span>'


def md_to_pango(text):
    """Markdown-ish → Pango markup, processed a line at a time so a lone '#' or
    '-' mid-sentence is never mistaken for a heading or a bullet.

    Hierarchy is carried by SIZE and WEIGHT rather than by colour. Bullets keep
    an accent marker (so the eye can find the list) and indent to their nesting
    depth, block-quotes keep a bar, and `---` becomes an actual rule. Runs of
    blank lines collapse to one short spacer.

    Everything is HTML-escaped first, so user/model text can contain <, > and &
    safely. Fenced code and tables are split out by md_segments() before this
    runs, so there is no code-fence case to handle here."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    esc = lambda x: _html.escape(x, quote=False)
    # Heading level → (Pango size keyword, colour, weight)
    hsize = {1: ("x-large", INK, "bold"), 2: ("large", INK, "bold"),
             3: ("medium", INK, "bold")}
    out = []
    lines = text.strip("\n").split("\n")
    for raw in lines:
        if not raw.strip():
            if out and out[-1] != _GAP:
                out.append(_GAP)
            continue
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", raw)
        if m:
            size, col, wt = hsize.get(len(m.group(1)), ("medium", BODY, "bold"))
            out.append(f'<span size="{size}" foreground="{col}" weight="{wt}">'
                       f'{_md_inline(esc(m.group(2)))}</span>')
            continue
        if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", raw):
            out.append(f'<span foreground="{FAINT}">'
                       + "─" * 28 + '</span>')
            continue
        m = re.match(r"^\s{0,3}>\s?(.*)$", raw)
        if m:
            out.append(f'<span foreground="{MUTE}"><i>▎  '
                       f'{_md_inline(esc(m.group(1)))}</i></span>')
            continue
        m = re.match(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            done = m.group(2).lower() == "x"
            box = ('<span foreground="#4ADE9A">☑</span>' if done
                   else f'<span foreground="{FAINT}">☐</span>')
            body = _md_inline(esc(m.group(3)))
            out.append("    " * depth + box + "  "
                       + (f'<span foreground="{MUTE}">{body}</span>' if done else body))
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            mark = ("•", "◦", "‣", "•")[depth]
            out.append("    " * depth
                       + f'<span foreground="{ACCENT}">{mark}</span>  '
                       + _md_inline(esc(m.group(2))))
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            out.append("    " * depth
                       + f'<span foreground="{ACCENT}" weight="bold">{m.group(2)}.</span>  '
                       + _md_inline(esc(m.group(3))))
            continue
        out.append(_md_inline(esc(raw)))
    while out and out[-1] == _GAP:
        out.pop()
    return "\n".join(out)


_FENCE_RE = re.compile(r"^```([\w+#.-]*)[^\n]*\n(.*?)(?:\n```[ \t]*$|\Z)", re.M | re.S)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def md_segments(text):
    """Split a reply into ("md", text) / ("code", lang, body) / ("table", rows)
    segments, so code renders as a copyable block that scrolls sideways
    instead of overflowing the column, and tables render as a real grid.
    An unclosed fence (a reply cut off mid-block) still renders as code."""
    text = text or ""
    segs = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            segs.extend(_table_split(text[pos:m.start()]))
        segs.append(("code", m.group(1).lower(), m.group(2).rstrip("\n")))
        pos = m.end()
    if pos < len(text):
        segs.extend(_table_split(text[pos:]))
    return [sg for sg in segs if not (sg[0] == "md" and not sg[1].strip())]


def _table_split(text):
    lines = text.split("\n")
    out, buf, i = [], [], 0
    while i < len(lines):
        if (i + 1 < len(lines) and "|" in lines[i] and _TABLE_SEP.match(lines[i + 1])
                and len(_split_row(lines[i])) >= 2):
            if buf:
                out.append(("md", "\n".join(buf))); buf = []
            rows = [_split_row(lines[i])]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i])); i += 1
            out.append(("table", rows))
            continue
        buf.append(lines[i]); i += 1
    if buf:
        out.append(("md", "\n".join(buf)))
    return out


def _apply_line_height(label, mult=1.45):
    """Give a label real line spacing. GTK labels have none by default, which is
    why long replies used to look like a cramped wall; Pango's line-height attr
    is font-size independent, so the Settings font slider still works. Guarded:
    older Pango or the test stub simply skip it."""
    try:
        from gi.repository import Pango
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_line_height_new(mult))
        label.set_attributes(attrs)
    except Exception:
        pass


def _markup_ok(markup):
    """Would GtkLabel accept this?

    set_markup does NOT raise on malformed markup — it logs a warning and leaves
    the label showing whatever it held before, so a single stray '<' in a reply
    would silently redisplay the PREVIOUS message. Verified against real GTK 4.14.
    So we ask Pango first and fall back to plain text if the answer is no.

    The <a> tags are stripped for the probe because GtkLabel handles links itself
    (confirmed working) while bare Pango rejects them as an unknown element. With
    no Pango present — the test stub — we assume yes rather than degrade every
    reply to plain text.
    """
    try:
        from gi.repository import Pango
    except Exception:
        return True
    try:
        Pango.parse_markup(re.sub(r"</?a(?:\s[^>]*)?>", "", markup), -1, "\x00")
        return True
    except Exception:
        return False


def set_rich(label, text):
    try:
        markup = md_to_pango(text)
        if _markup_ok(markup):
            label.set_markup(markup)
        else:
            label.set_text(text)
    except Exception:
        try:
            label.set_text(text)
        except Exception:
            pass
    _apply_line_height(label)


def _pic_from_file(path, w=-1, h=-1):
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, w, h, True)
    tex = Gdk.Texture.new_for_pixbuf(pb)
    pic = Gtk.Picture.new_for_paintable(tex)
    pic.set_can_shrink(False)
    return pic, pb


# ── system helpers ──────────────────────────────────────────────────────────
def _run_ro(cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _now_line():
    """Current local date/time + timezone — injected fresh each turn so Chuck
    never reasons from a stale 'today' and knows how current his own knowledge is."""
    now = datetime.now().astimezone()
    tz = now.tzname() or ""
    return now.strftime("Today is %A, %d %B %Y, %H:%M ") + tz


# The machine's distro, kernel and GPU do not change while the app is open, but
# this was re-probed from scratch every time a chat was created — five
# subprocesses ON THE GTK MAIN THREAD, one of them a full `pacman -Qq` walk of
# the local package database. On a large Arch install that is a visible freeze
# every single time you click New chat, for an answer that was already known.
_CTX = {"text": None}
_CTX_LOCK = threading.Lock()


def prime_context():
    """Work the machine facts out in the background, before anything wants them."""
    threading.Thread(target=gather_context, daemon=True).start()


_INSTRUCTION_FILES = ("CHUCK.md", "AGENTS.md", "CLAUDE.md", ".chuck/instructions.md")
_INSTRUCTION_CAP = 12000


def _read_capped(path, cap=_INSTRUCTION_CAP):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > cap:
        text = text[:cap] + f"\n… (cut at {cap} characters — read {path} for the rest)"
    return text


def project_instructions(root):
    """The user's standing rules: ~/.config/chucknorris/CHUCK.md for everything,
    plus the first of CHUCK.md / AGENTS.md / CLAUDE.md in the working folder.
    Returned as one block for the system prompt ('' when there are none)."""
    out = []
    g = _read_capped(CONFIG_DIR / "CHUCK.md")
    if g:
        out.append("USER INSTRUCTIONS (~/.config/chucknorris/CHUCK.md) — always follow:\n" + g)
    if root:
        for name in _INSTRUCTION_FILES:
            f = Path(root) / name
            if f.is_file():
                t = _read_capped(f)
                if t:
                    out.append(f"PROJECT INSTRUCTIONS ({name} in the working folder) — "
                               f"follow them for work here:\n{t}")
                break
    return "\n\n".join(out)


def gather_context(refresh=False):
    with _CTX_LOCK:
        if not refresh and _CTX["text"] is not None:
            return _CTX["text"]

        # The probes are independent, so they run CONCURRENTLY: the cost is the
        # slowest one rather than the sum of all of them.
        jobs = {
            "distro": ["sh", "-c",
                       ". /etc/os-release 2>/dev/null; echo \"$PRETTY_NAME\""],
            "kernel": ["uname", "-r"],
            "gpu": ["sh", "-c",
                    "lspci | grep -iE 'vga|3d|display' | sed 's/.*: //'"],
        }
        if shutil.which("pacman"):
            jobs["packages"] = ["sh", "-c", "pacman -Qq | wc -l"]
        ex = ThreadPoolExecutor(max_workers=len(jobs))
        try:
            futs = {k: ex.submit(_run_ro, v) for k, v in jobs.items()}
            got = {}
            for k, f in futs.items():
                try:
                    got[k] = f.result(timeout=10)
                except Exception:
                    got[k] = ""
        finally:
            ex.shutdown(wait=False)

        facts = ["Distro: " + (got.get("distro") or "unknown"),
                 "Kernel: " + got.get("kernel", ""),
                 f"Session: {os.environ.get('XDG_SESSION_TYPE','?')} / "
                 f"{os.environ.get('XDG_CURRENT_DESKTOP','?')}"]
        if "packages" in got:
            facts.append("Packages: " + got["packages"])
            facts.append("AUR helper: " + ("paru" if shutil.which("paru")
                         else ("yay" if shutil.which("yay") else "none")))
        if got.get("gpu"):
            facts.append("GPU: " + got["gpu"].replace("\n", "; "))
        _CTX["text"] = "\n".join(facts)
        return _CTX["text"]


def capture_screen(path):
    """Grab the whole desktop to `path` (PNG). True on success. Wayland via
    grim, KDE via spectacle, X11 via scrot or gnome-screenshot."""
    for tool, argv in (("grim", ["grim", path]),
                       ("spectacle", ["spectacle", "-b", "-n", "-o", path]),
                       ("scrot", ["scrot", "-o", path]),
                       ("gnome-screenshot", ["gnome-screenshot", "-f", path])):
        if shutil.which(tool):
            try:
                subprocess.run(argv, timeout=20, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return os.path.isfile(path) and os.path.getsize(path) > 0
            except Exception:
                continue
    return False


def screenshot_to_b64():
    tmp = str(CONFIG_DIR / ".shot.png")
    try:
        if not capture_screen(tmp):
            return None
        return base64.b64encode(Path(tmp).read_bytes()).decode()
    except Exception:
        return None
    finally:
        # A full capture of the user's desktop is not something to leave lying
        # in ~/.config once it has been encoded and sent.
        try:
            os.unlink(tmp)
        except OSError:
            pass


_IMG_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp", ".gif": "image/gif"}


def image_payload(path, max_side=1568):
    """(base64, mime) for sending an image to a vision model, or None.

    A 4K screenshot is ~4x the tokens of the same picture at 1568px and the
    model sees no more detail in it, so anything larger is scaled down first.
    Small images in a format every API accepts go through untouched."""
    try:
        ext = Path(path).suffix.lower()
        info = GdkPixbuf.Pixbuf.get_file_info(path)
        w, h = (info[1], info[2]) if info and info[0] is not None else (0, 0)
        if w and h and max(w, h) <= max_side and ext in _IMG_MIME:
            return base64.b64encode(Path(path).read_bytes()).decode(), _IMG_MIME[ext]
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, max_side, max_side, True)
        ok, buf = pb.save_to_bufferv("png", [], [])
        if ok:
            return base64.b64encode(bytes(buf)).decode(), "image/png"
    except Exception:
        pass
    try:                                   # no GdkPixbuf (headless): send as-is
        ext = Path(path).suffix.lower()
        if ext in _IMG_MIME and os.path.getsize(path) < 8_000_000:
            return base64.b64encode(Path(path).read_bytes()).decode(), _IMG_MIME[ext]
    except OSError:
        pass
    return None


def _scratch_png(prefix):
    """A fresh file in CONFIG_DIR for a captured or pasted image (swept later)."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=".png", dir=str(CONFIG_DIR))
    os.close(fd)
    return name


# The session's working folder. Every command and program Chuck runs starts
# here, so `ls`, `git status` or `pytest` mean "in the user's project" rather
# than wherever the app happened to be launched. Module-level (one window, one
# session) so every runner — and the test harness's stand-ins — sees it.
_WORKDIR = {"path": None}


# Where the shell is. `cd` in one command carries to the next, the way a
# terminal does (each command is a fresh bash, so it used to be forgotten).
# Reset to the working folder whenever that changes, and on a new chat.
_SHELL = {"cwd": None}


def _cwd():
    for p in (_SHELL["cwd"], _WORKDIR["path"]):
        if p and os.path.isdir(str(p)):
            return str(p)
    return None


def _shell_cwd_note():
    """' (shell is now in X)' when a cd left the shell outside the working folder."""
    c, w = _SHELL["cwd"], _WORKDIR["path"]
    if c and os.path.isdir(c) and (not w or os.path.realpath(c) != os.path.realpath(str(w))):
        return f"\n(The shell is now in {c} — file tools still resolve paths in the working folder.)"
    return ""


_RUN_ENV = {
    "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "cat", "SYSTEMD_LESS": "",
    "TERM": "dumb", "DEBIAN_FRONTEND": "noninteractive", "GIT_TERMINAL_PROMPT": "0",
    "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "CLICOLOR": "0",
}

# pacman/paru/yay prompt before acting. The approve-to-run card IS the user's
# confirmation, so the command must not then sit waiting for a second one it can
# never receive. Added visibly, so the card shows exactly what will run.
# pacman/paru/yay prompt before acting. The approve-to-run card IS the user's
# confirmation, so the command must not then sit waiting for a second one it can
# never receive. Added visibly, so the card shows exactly what will run.
#
# The operation is matched as a FLAG CLUSTER (-R, -Rs, -Rsn, -Rns, -S, -Syu, -U),
# not as a fixed list of spellings. The old list-of-alternatives regex happened
# to contain `Rns` but not `Rs` or `Rsn`, so the two removal forms people
# actually type never got --noconfirm — pacman then hit `[Y/n]` against a
# stdin of /dev/null and aborted.
_NEEDS_NOCONFIRM = re.compile(
    r"\b(pacman|paru|yay|pamac)\b(?=[^|;&]*\s(?:-[A-Za-z]*[SRU][A-Za-z]*"
    r"|--(?:sync|remove|upgrade))\b)",
    re.IGNORECASE)


def needs_noconfirm(cmd):
    """True if this is a package operation that will stop and ask."""
    if not cmd or "--noconfirm" in cmd or "-y " in f" {cmd} ":
        return False
    if not _NEEDS_NOCONFIRM.search(cmd):
        return False
    # read-only queries never prompt
    return not re.search(r"-{1,2}(Ss|Si|Sl|Sg|Sw|Q[a-z]*|F[a-z]*|search|info)\b", cmd)


def add_noconfirm(cmd):
    """Insert --noconfirm right after the package manager it belongs to."""
    if not needs_noconfirm(cmd):
        return cmd
    return re.sub(r"\b(pacman|paru|yay|pamac)\b", r"\1 --noconfirm", cmd, count=1)


def clip_output(out, limit=7000):
    """Keep the START and the END of long output.

    Errors almost always land at the END — a failing build prints thousands of
    'compiling ... ok' lines and then the one line that matters. Truncating from
    the front threw exactly that line away, so he'd read a failed build as a
    success. Keep both ends and say what was dropped.
    """
    out = out or ""
    if len(out) <= limit:
        return out
    head = limit // 3
    tail = limit - head
    dropped = len(out) - limit
    return (out[:head] + f"\n\n[... {dropped} characters trimmed from the middle ...]\n\n"
            + out[-tail:])


def _clip_feedback(parts, limit=60000):
    """Join one round of tool results for the model. Each part keeps its head
    AND tail when the round is over budget — the old flat [:12000] cut chopped
    whatever came last, which was usually the file just read or the error."""
    parts = [p for p in parts if p]
    total = sum(len(p) for p in parts)
    if total <= limit:
        return "\n\n".join(parts)
    share = max(2000, limit // max(1, len(parts)))
    return "\n\n".join(clip_output(p, share) for p in parts)


def _run_report(what, rc, out):
    """What the model is told after a command runs.

    A non-zero exit is stated plainly and first, because the single most useful
    thing here is that he reacts to a real failure instead of carrying on as if
    it worked.
    """
    body = clip_output(out)
    if rc == 0:
        return f"I ran {what}. It SUCCEEDED (exit 0). Output:\n{body}"
    hint = ""
    if rc == 124:
        hint = " It timed out — if it needs input it can't run from here."
    elif rc == 127:
        hint = " Exit 127 usually means the command or a package isn't installed."
    elif rc == 126:
        hint = " Exit 126 usually means it isn't executable, or permission was denied."
    elif rc == 1 and "not found" in (out or "").lower():
        hint = " Something it referenced doesn't exist."
    return (f"I ran {what}. It FAILED with exit {rc}.{hint} Read the output, say what went "
            f"wrong in one line, and fix it — do not carry on as though it worked:\n{body}")


# ── privilege escalation ────────────────────────────────────────────────────
# Chuck is an agent that installs and fixes things, so `sudo` has to actually
# work — not pop a per-command pkexec dialog and give up when it isn't there.
# This is the same escalation model a good sysadmin agent uses: detect the tool,
# skip the whole dance when the box is passwordless, otherwise authenticate ONCE
# with a password held only in memory. The password never touches disk, the log,
# the model, or a command's own stdin.
_SUDO_AUTH_FAILED = 97          # private rc: sudo could not authenticate
_PRIV_ESC_CACHE = None


def _ro(argv, timeout=8):
    """Run a read-only helper command; return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception:
        return 1, "", ""


def detect_priv_esc():
    """How to become root on THIS box. CachyOS may ship sudo-rs (the Rust
    rewrite, missing -A/SUDO_ASKPASS on older builds) or doas instead of classic
    sudo. Keys: tool ('sudo'|'sudo-rs'|'doas'|None), bin, askpass (bool),
    stdin (bool), version. Cached; runs only --version/--help once."""
    global _PRIV_ESC_CACHE
    if _PRIV_ESC_CACHE is not None:
        return _PRIV_ESC_CACHE
    tool = bin_ = None
    version = ""
    if shutil.which("sudo"):
        bin_ = "sudo"
        rc, out, err = _ro(["sudo", "--version"], timeout=5)
        first = (out or err or "").splitlines()
        version = first[0].strip() if first else ""
        tool = "sudo-rs" if "sudo-rs" in version.lower() else "sudo"
    elif shutil.which("doas"):
        tool, bin_ = "doas", "doas"
    askpass = stdin = False
    if tool == "sudo":
        askpass = stdin = True
    elif tool == "sudo-rs":
        stdin = True
        rc, out, err = _ro(["sudo", "--help"], timeout=5)
        askpass = "-A" in (out or "") or "askpass" in (out or "").lower()
    _PRIV_ESC_CACHE = {"tool": tool, "bin": bin_, "askpass": askpass,
                       "stdin": stdin, "version": version}
    return _PRIV_ESC_CACHE


def priv_esc_prefix():
    """'sudo ' / 'doas ' (or '' if neither exists) — so install hints match the box."""
    pe = detect_priv_esc()
    return f"{pe['bin']} " if pe["bin"] else ""


def _sudo_ready():
    """True if we can escalate RIGHT NOW with no password — a NOPASSWD sudoers
    rule or a still-valid cached timestamp (common on single-user CachyOS). Cheap
    (`sudo -n true`); when true the whole password dance is skipped."""
    pe = detect_priv_esc()
    if pe["tool"] in ("sudo", "sudo-rs"):
        rc, _, _ = _ro(["sudo", "-n", "true"], timeout=5)
        return rc == 0
    if pe["tool"] == "doas":
        rc, _, _ = _ro(["doas", "-n", "true"], timeout=5)
        return rc == 0
    return False


# `sudo` at command position (start, after a separator, or after leading env
# assignments) — so `echo pseudo`, `/opt/sudoku`, `# sudo …` don't false-match.
_SUDO_RE = re.compile(r'(?:^|[\n;&|(]\s*|\b&&\s*|\b\|\|\s*)(?:\w+=\S*\s+)*sudo\b')
_SUDO_INJECT_RE = re.compile(r'(^|[\n;&|(]\s*|&&\s*|\|\|\s*)sudo(?=\s|$)')


def command_needs_sudo(command):
    """True if the command contains a real `sudo` invocation."""
    return bool(command) and bool(_SUDO_RE.search(command))


def _inject_askpass(command):
    """Turn each `sudo` into `sudo -A` (use SUDO_ASKPASS). Unlike -S, askpass
    never reads the command's stdin, so `sudo -A tee file` still works."""
    if "sudo -A" in command:
        return command
    return _SUDO_INJECT_RE.sub(r'\1sudo -A', command)


def _ensure_askpass_helper():
    """Write (once, 0700) a tiny askpass helper that echoes $CHUCK_SUDO_PW. The
    script holds NO secret — the password reaches it only via the environment of
    the single sudo call."""
    path = CONFIG_DIR / ".chuck-askpass.sh"
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.fchmod(fd, 0o700)          # defeat umask before content lands
            os.write(fd, b'#!/bin/sh\nprintf "%s\\n" "$CHUCK_SUDO_PW"\n')
        finally:
            os.close(fd)
        return str(path)
    except Exception:
        return None


def _bash_bin():
    return shutil.which("bash") or "sh"


def _shell_out(p):
    return _shell_out_parts(p.stdout, p.stderr)


def _shell_out_parts(stdout, stderr):
    return ((stdout or "") + (stderr or "")).strip() or "(no output)"


def _run_sudo_askpass(script, password, timeout, env):
    """Fallback for hardened sudoers (timestamp_timeout=0) or sudo-rs where the
    inline cached credential won't carry to the command's own sudo. SUDO_ASKPASS
    authenticates each sudo independently. Returns (rc, out) or None if the helper
    can't be set up."""
    helper = _ensure_askpass_helper()
    if not helper:
        return None
    e2 = dict(env)
    e2["SUDO_ASKPASS"] = helper
    e2["CHUCK_SUDO_PW"] = password
    try:
        p = _guarded_proc([_bash_bin(), "-c", _inject_askpass(script)], timeout, e2)
        low = (p.stderr or "").lower()
        if p.returncode != 0 and ("a terminal is required" in low or "askpass" in low
                                  or "no password was provided" in low
                                  or "a password is required" in low):
            return _SUDO_AUTH_FAILED, "sudo: askpass authentication failed."
        return p.returncode, _shell_out(p)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    finally:
        e2["CHUCK_SUDO_PW"] = ""


def _run_sudo(script, password, timeout, env):
    """Authenticate and run in ONE bash session so the fresh credential applies
    to the script's own sudo calls, with an askpass fallback for hardened boxes.
    Returns (rc, out); rc 97 == authentication failed."""
    pe = detect_priv_esc()
    binname = pe["bin"] or "sudo"
    if pe["tool"] == "doas":
        return _SUDO_AUTH_FAILED, ("this box uses doas, which can't take a password "
                                   "on stdin. Add a persist/nopass rule in "
                                   "/etc/doas.conf, or install sudo.")
    # -k clears any stale timestamp so -S -v truly validates the piped password;
    # rc 97 is our private "auth failed" sentinel.
    prelude = f"{binname} -k 2>/dev/null\n{binname} -S -p '' -v || exit 97\n"
    try:
        p = _guarded_proc([_bash_bin(), "-c", prelude + script], timeout, env,
                          input_text=password + "\n")
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    if p.returncode == 97:
        err = (p.stderr or "").lower()
        if "not in the sudoers" in err or "not allowed" in err:
            return _SUDO_AUTH_FAILED, "sudo: this account isn't permitted to use sudo."
        if pe["askpass"]:
            alt = _run_sudo_askpass(script, password, timeout, env)
            if alt is not None and alt[0] != _SUDO_AUTH_FAILED:
                return alt
        return _SUDO_AUTH_FAILED, ("sudo: password rejected. On CachyOS this is usually "
                                   "a typo, a `Defaults rootpw/targetpw` policy (sudo "
                                   "wants ROOT's password, not yours), or your user not "
                                   "being in the wheel group.")
    # -v succeeded but an inner sudo still failed (timestamp didn't carry) → askpass
    low = (p.stderr or "").lower()
    if p.returncode != 0 and pe["askpass"] and (
            "a terminal is required" in low or "no password was provided" in low
            or "a password is required" in low or "askpass" in low):
        alt = _run_sudo_askpass(script, password, timeout, env)
        if alt is not None:
            return alt
    return p.returncode, _shell_out(p)


# ── command runtime awareness: how long should this take, when to give up ─────
_QUICK_CMDS = {
    "ls", "cat", "echo", "whoami", "id", "pwd", "cd", "head", "tail", "grep",
    "which", "whereis", "type", "stat", "file", "wc", "date", "uname",
    "hostname", "env", "printenv", "ps", "df", "du", "free", "ping", "dig",
    "host", "nslookup", "cut", "awk", "sed", "sort", "uniq", "tr", "chmod",
    "chown", "mkdir", "touch", "rm", "cp", "mv", "ln", "kill", "pkill",
    "export", "readlink", "basename", "dirname", "test", "true", "false",
    "sleep", "systemctl", "service", "ss", "netstat", "ip", "ifconfig",
}
_LONG_CMDS = {
    "apt", "apt-get", "dpkg", "yay", "paru", "pamac", "yum", "dnf", "pacman",
    "zypper", "make", "cmake", "gcc", "g++", "clang", "cargo", "go", "pip",
    "pip3", "pipx", "npm", "yarn", "pnpm", "docker", "podman", "docker-compose",
    "rsync", "dd", "wget", "curl", "git", "gem", "bundle", "mvn", "gradle",
    "nmap", "meson", "ninja", "makepkg", "flatpak", "snap",
}
_LONG_WORDS = {"upgrade", "dist-upgrade", "install", "update", "build",
               "compile", "pull", "clone", "download", "-Syu", "-S"}
# Long-running servers / daemons — these do NOT return on their own.
_SERVER_CMDS = {
    "flask", "uvicorn", "gunicorn", "hypercorn", "daphne", "streamlit", "gradio",
    "nodemon", "puma", "jekyll", "hugo",
    "http-server", "serve", "webpack-dev-server",
    "php-fpm", "nginx", "httpd", "caddy", "mongod", "mysqld", "mariadbd",
    "postgres", "redis-server", "ncat", "socat",
}


def estimate_runtime(command):
    """Estimate how long a command should take and the hard timeout to enforce,
    so a hung command (classically a server that won't start) is killed fast
    instead of blocking the full window. Pure heuristic; runs nothing.
    Returns {kind, expected_seconds, hard_timeout_seconds, is_server, backgrounded}."""
    cmd = (command or "").strip()
    low = cmd.lower()
    backgrounded = bool(re.search(r"(?<!&)&\s*$", cmd)) or "nohup " in low \
        or " disown" in low
    heads = []
    server_hit = False
    for seg in re.split(r"[\n;|]+|&&|\|\|", low):
        words = seg.split()
        i = 0
        while i < len(words) and ("=" in words[i] or words[i] in (
                "sudo", "nohup", "time", "env", "exec", "setsid", "stdbuf")):
            i += 1
        if i >= len(words):
            continue
        head = os.path.basename(words[i])
        heads.append(head)
        rest = words[i + 1:]
        if head in _SERVER_CMDS:
            server_hit = True
        elif head in ("python", "python3", "py") and (
                "runserver" in " ".join(rest) or "http.server" in " ".join(rest)):
            server_hit = True
        elif head in ("npm", "yarn", "pnpm", "bun", "deno") and any(
                w in ("start", "dev", "serve", "preview", "watch") for w in rest):
            server_hit = True
        elif head in ("next", "nuxt", "vite", "ng", "rails") and (
                not rest or rest[0] in ("dev", "start", "serve", "preview", "s", "server")):
            server_hit = True                    # `vite build`, `next lint` finish
        elif head in ("node", "deno", "bun") and rest and re.search(
                r"(?:^|/)(?:server|serve|app)\.[cm]?[jt]s$", rest[-1]):
            server_hit = True                    # `node server.js`; `node build.js` finishes
    if server_hit and not backgrounded:
        return {"kind": "server", "expected_seconds": 8, "hard_timeout_seconds": 25,
                "is_server": True, "backgrounded": False}
    if backgrounded:
        return {"kind": "background", "expected_seconds": 3, "hard_timeout_seconds": 15,
                "is_server": server_hit, "backgrounded": True}
    is_long = any(h in _LONG_CMDS for h in heads) or \
        any(w in _LONG_WORDS for w in low.split())
    if is_long:
        return {"kind": "long", "expected_seconds": 300, "hard_timeout_seconds": 1800,
                "is_server": False, "backgrounded": False}
    if heads and all(h in _QUICK_CMDS for h in heads):
        return {"kind": "quick", "expected_seconds": 5, "hard_timeout_seconds": 120,
                "is_server": False, "backgrounded": False}
    # Unknown means "a script, a test run, a render": give it room. A hang is
    # still bounded, and Stop kills it at once.
    return {"kind": "unknown", "expected_seconds": 30, "hard_timeout_seconds": 600,
            "is_server": False, "backgrounded": False}


def _timeout_note(command, timeout):
    """An informative timeout message so Chuck knows the command didn't finish
    (and what to do about a server) instead of pretending it hung by accident."""
    est = estimate_runtime(command)
    note = (f"(timed out after {timeout}s — expected ~{est['expected_seconds']}s for a "
            f"{est['kind']} command. It did NOT complete and was killed; do not just "
            f"wait for it, it won't finish as-is.")
    if est["is_server"] and not est["backgrounded"]:
        note += (" This looks like a server/daemon: start it in the BACKGROUND "
                 "(nohup CMD >/tmp/srv.log 2>&1 &), then confirm it came up by probing "
                 "the port/URL — a foreground start blocks until timeout regardless.")
    return note + ")"


def _run_shell(script, timeout=None, sudo_password=None):
    """Run a shell script (one line or many) as the operator's user, with real
    sudo handling. Returns (rc, output). rc 97 == sudo needs a password / auth
    failed, so the caller can prompt once and retry. Timeout is auto-estimated
    (a package build gets 30 min; a server that never returns is capped fast)."""
    if timeout is None:
        timeout = estimate_runtime(script)["hard_timeout_seconds"]
    env = dict(os.environ)
    env.update(_RUN_ENV)
    cwd_file = None
    start = os.path.realpath(_cwd() or os.getcwd())
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, cwd_file = tempfile.mkstemp(prefix=".cwd_", dir=str(CONFIG_DIR))
        os.close(fd)
        env["CHUCK_CWD_FILE"] = cwd_file
        # same line as the script's first, so bash's "line N:" errors stay right
        script = "trap 'pwd -P >\"$CHUCK_CWD_FILE\" 2>/dev/null' EXIT; " + script
    except OSError:
        cwd_file = None
    try:
        return _run_shell_in(script, timeout, sudo_password, env)
    finally:
        if cwd_file:
            try:
                where = Path(cwd_file).read_text().strip()
                # only a real cd moves the shell; otherwise leave it where it was
                if where and os.path.isdir(where) and os.path.realpath(where) != start:
                    _SHELL["cwd"] = where
                os.unlink(cwd_file)
            except OSError:
                pass


def _run_shell_in(script, timeout, sudo_password, env):
    if command_needs_sudo(script) and not _sudo_ready():
        pe = detect_priv_esc()
        if pe["tool"] is None:
            return 127, ("no privilege-escalation tool on PATH (looked for sudo, "
                         "sudo-rs, doas). Install sudo, or launch Chuck from a root shell.")
        if sudo_password:
            return _run_sudo(script, sudo_password, timeout, env)
        return _SUDO_AUTH_FAILED, "sudo password required (none provided)."
    # plain path: no sudo, or the box is already passwordless. stdin=DEVNULL so
    # anything that tries to prompt gets EOF and fails fast instead of hanging.
    try:
        return _run_guarded([_bash_bin(), "-c", script], timeout, env,
                            lambda: _timeout_note(script, timeout))
    except Exception as ex:
        return 1, f"(error: {ex})"


def run_command(cmd, timeout=None, sudo_password=None):
    """Run a shell command as the user. sudo is handled transparently."""
    return _run_shell(cmd, timeout=timeout, sudo_password=sudo_password)


# Every command Chuck is running right now, so Stop can end them. Stop used to
# end only the TURN: the command itself ran on, invisibly, until its timeout.
_LIVE_PROCS = set()
_LIVE_LOCK = threading.Lock()


# A worker thread that wants to watch its command's output as it happens sets
# _LIVE_OUT.fn (called with each decoded chunk, from reader threads). Thread-
# local, so it needs threading through no signatures and can't leak between
# commands, which each run on their own worker.
_LIVE_OUT = threading.local()


def _streamed(proc, input_text, timeout, on_chunk):
    """communicate(), but each chunk of stdout/stderr also goes to on_chunk as
    it arrives. -> (out, err). Raises TimeoutExpired like communicate()."""
    import codecs
    bufs = {"out": [], "err": []}

    def reader(stream, key):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = stream.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            text = dec.decode(data)
            if text:
                bufs[key].append(text)
                try:
                    on_chunk(text)
                except Exception:
                    pass
        tail = dec.decode(b"", final=True)
        if tail:
            bufs[key].append(tail)

    threads = [threading.Thread(target=reader, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=reader, args=(proc.stderr, "err"), daemon=True)]
    for t in threads:
        t.start()
    if proc.stdin is not None:
        try:
            if input_text:
                proc.stdin.write(input_text.encode())
            proc.stdin.close()
        except OSError:
            pass
    proc.wait(timeout=timeout)                       # raises TimeoutExpired
    for t in threads:
        t.join(timeout=5)
    return "".join(bufs["out"]), "".join(bufs["err"])


def _guarded_proc(argv, timeout, env, input_text=None):
    """Run argv in its own process group, registered as live while it runs.
    Returns a CompletedProcess. On timeout the WHOLE tree is killed and
    TimeoutExpired is raised; a Stop (kill_live_commands) ends it early with
    whatever exit status the kill produced."""
    on_chunk = getattr(_LIVE_OUT, "fn", None)
    proc = subprocess.Popen(argv, cwd=_cwd(),
                            stdin=subprocess.PIPE if input_text is not None
                            else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=on_chunk is None, errors="replace" if on_chunk is None else None,
                            env=env, preexec_fn=_sandbox_preexec)
    with _LIVE_LOCK:
        _LIVE_PROCS.add(proc)
    try:
        try:
            if on_chunk is not None:
                out, err = _streamed(proc, input_text, timeout, on_chunk)
            else:
                out, err = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            raise
    finally:
        with _LIVE_LOCK:
            _LIVE_PROCS.discard(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def kill_live_commands():
    """Kill every command Chuck is running (their whole process trees)."""
    with _LIVE_LOCK:
        procs = list(_LIVE_PROCS)
    for p in procs:
        if p.poll() is None:
            _kill_tree(p)
    return len(procs)


def _run_guarded(argv, timeout, env, timeout_note):
    """subprocess.run, but the child gets its own process group, and a timeout
    (or Stop) kills the whole tree.

    subprocess.run's own timeout kills only the direct child. A skill that
    backgrounds work, or a shell that spawned a compile, left orphans running
    after Chuck had already given up on them and moved on — invisible processes
    still eating the box.
    """
    try:
        p = _guarded_proc(argv, timeout, env)
    except subprocess.TimeoutExpired:
        return 124, timeout_note()
    return p.returncode, _shell_out_parts(p.stdout, p.stderr)


def _sandbox_preexec():
    """Applied in the child, after fork, before exec.

    Chuck's own note said skills "run raw in-process" and needed restricted
    builtins — that is not what happens. Skills already execute as
    `bash /path/skill.sh` through subprocess, so a buggy skill cannot crash the
    app and restricted builtins would achieve nothing.

    The REAL gap is that the child inherits no limits at all: a runaway skill
    could fork-bomb, fill the disk, or exhaust RAM on a 16GB box and take the
    desktop down with it. So: a fresh process group (so a timeout kills the
    whole tree, not just the shell that spawned it) and soft resource caps.

    Deliberately NOT sandboxed away: filesystem and network. This is a system
    assistant — a skill that cannot touch the filesystem or reach the network is
    useless. The safety story here is the approve-to-run card and the risk
    tiers, not a jail.
    """
    try:
        os.setsid()                       # own process group -> killable as a tree
    except OSError:
        pass
    try:
        import resource
        # NO process, memory or file-size caps. Each one broke real work:
        #  * RLIMIT_NPROC counts EVERY task the user owns, the whole desktop
        #    included — a flat 512 was already exceeded on a normal Plasma
        #    session, so every fork failed ("fork: Resource temporarily
        #    unavailable"), and any count-based cap can be tripped the same way;
        #  * RLIMIT_AS stopped Chromium, Node and the JVM from starting (they
        #    reserve tens of GB of address space they never touch);
        #  * RLIMIT_FSIZE killed downloads and builds of large files.
        # Runaways are handled by the process group above: a timeout or Stop
        # kills the whole tree. Commands run like they would in your terminal.
        _limit(resource, "RLIMIT_CORE", 0)             # no core dumps
    except Exception:
        pass


def _limit(resource, name, soft_cap):
    """Lower a soft limit to soft_cap, never raise it, never exceed the hard cap."""
    try:
        which = getattr(resource, name, None)
        if which is None:
            return
        soft, hard = resource.getrlimit(which)
        target = soft_cap if hard == resource.RLIM_INFINITY else min(soft_cap, hard)
        if soft == resource.RLIM_INFINITY or soft > target:
            resource.setrlimit(which, (target, hard))
    except (ValueError, OSError):
        pass


def _kill_tree(proc):
    """Kill a timed-out command's whole process group, not just its shell."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# language -> (interpreter argv builder). Chuck writes code, it runs in a temp
# file so multi-line programs work; bash/sh route through the sudo-aware runner.
_LANG_RUN = {
    "python": lambda p: ["python3", p], "py": lambda p: ["python3", p],
    "node": lambda p: ["node", p], "javascript": lambda p: ["node", p],
    "js": lambda p: ["node", p],
    "bash": lambda p: ["bash", p], "sh": lambda p: ["sh", p],
}
_LANG_EXT = {"python": "py", "py": "py", "node": "js", "javascript": "js", "js": "js",
             "bash": "sh", "sh": "sh"}
_LANG_BIN = {"python": "python3", "py": "python3", "node": "node", "javascript": "node",
             "js": "node", "bash": "bash", "sh": "sh"}


def run_code(lang, body, timeout=None, sudo_password=None):
    """Run a snippet in the given language. Returns (rc, output).

    bash/sh go through the sudo-aware shell runner (so `sudo pacman …` in a code
    block actually authenticates); other languages run from a temp file."""
    lang = (lang or "bash").lower()
    if lang in ("bash", "sh"):
        return _run_shell(body, timeout=timeout, sudo_password=sudo_password)
    if lang not in _LANG_RUN:
        return 2, f"(unsupported language: {lang})"
    binname = _LANG_BIN[lang]
    if not shutil.which(binname):
        return 127, f"({binname} not installed — need it to run {lang})"
    if timeout is None:
        timeout = estimate_runtime(body)["hard_timeout_seconds"]
    tmp = None
    try:
        # tempfile, not hash(body): a collision would have one run clobber
        # another's source mid-execution.
        fd, name = tempfile.mkstemp(prefix=".run_", suffix="." + _LANG_EXT[lang],
                                    dir=str(CONFIG_DIR))
        os.close(fd)
        tmp = Path(name)
        tmp.write_text(body)
        argv = _LANG_RUN[lang](str(tmp))
        env = dict(os.environ)
        env.update(_RUN_ENV)
        p = _guarded_proc(argv, timeout, env)
        return p.returncode, _shell_out(p)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(body, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    finally:
        # was skipped entirely on the timeout path, so every killed run left
        # its source behind in ~/.config/chucknorris
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def read_file_safe(path, limit=180_000):
    """Read a text file from disk for Chuck. Returns (ok, content_or_error).

    Two traps this guards against:
      - Character/block devices and FIFOs (/dev/zero, /dev/urandom, a pipe)
        report st_size == 0, so a size check alone lets them through and the
        read never terminates — it will happily eat all RAM until the OS kills
        the app. Only regular files are read.
      - st_size is also 0 for /proc entries, which ARE worth reading, so the
        read itself is byte-capped rather than trusting the reported size.
    """
    try:
        if not path or not str(path).strip():
            return False, "no path given"
        p = Path(os.path.expanduser(str(path).strip()))
        if not p.is_absolute() and _cwd():
            p = Path(_cwd()) / p
        if not p.exists():
            return False, f"no such file: {p}"
        if p.is_dir():
            entries = sorted(os.listdir(p))[:200]
            return True, f"[directory {p}]\n" + "\n".join(entries)
        if not p.is_file():
            return False, (f"{p.name} isn't a regular file (device, socket or pipe) "
                           "— nothing safe to read there")
        if p.stat().st_size > 4_000_000:
            return False, f"file too large ({p.stat().st_size // 1024} KB) — point me at part of it"
        # bounded read: never trust st_size to bound us
        with open(p, "rb") as fh:
            data = fh.read(limit * 4 + 4096)
        if b"\x00" in data[:4096]:
            return False, f"{p.name} looks binary — I read text files"
        return True, data.decode("utf-8", "ignore")[:limit]
    except PermissionError:
        return False, f"permission denied: {path} (try a sudo cat card instead)"
    except Exception as ex:
        return False, f"couldn't read {path}: {ex}"



def junk_scan():
    lines, cmds = [], []

    def sz(p):
        # quote the path: this helper must stay injection-proof even if a future
        # caller passes something that isn't a hardcoded literal.
        return _run_ro(["sh", "-c",
                        "du -sh " + shlex.quote(os.path.expanduser(p)) +
                        " 2>/dev/null | cut -f1"]) or "0"
    lines.append(f"~/.cache: {sz('~/.cache')}")
    cmds.append(("Clear thumbnail cache", "rm -rf ~/.cache/thumbnails/*"))
    if shutil.which("pacman"):
        lines.append(f"pacman package cache: {sz('/var/cache/pacman/pkg')}")
        cmds.append(("Trim pacman cache (keep 1)",
                     "sudo paccache -rk1" if shutil.which("paccache")
                     else "sudo pacman -Syu --needed pacman-contrib && sudo paccache -rk1"))
        orph = _run_ro(["sh", "-c", "pacman -Qtdq 2>/dev/null | wc -l"])
        lines.append(f"orphan packages: {orph}")
        if orph and orph != "0":
            cmds.append(("Remove orphans", "sudo pacman -Rns $(pacman -Qtdq)"))
    jsize = _run_ro(["sh", "-c",
             "journalctl --disk-usage 2>/dev/null | grep -oE '[0-9.]+[KMG]' | tail -1"])
    if jsize:
        lines.append(f"systemd journal: {jsize}")
        cmds.append(("Shrink journal to 200M", "sudo journalctl --vacuum-size=200M"))
    lines.append(f"Trash: {sz('~/.local/share/Trash')}")
    cmds.append(("Empty trash", "rm -rf ~/.local/share/Trash/files/* ~/.local/share/Trash/info/*"))
    lines.append(f"coredumps: {sz('/var/lib/systemd/coredump')}")
    cmds.append(("Clear coredumps", "sudo rm -rf /var/lib/systemd/coredump/*"))
    return "\n".join(lines), cmds


# ── SiliconFlow streaming ───────────────────────────────────────────────────
def _is_transient(ex):
    """Worth retrying? Cold DNS, a dropped handshake and 5xx/429 are temporary;
    a rejected key never is, and retrying it just wastes the user's time."""
    if isinstance(ex, urllib.error.HTTPError):
        return ex.code in (408, 425, 429, 500, 502, 503, 504)
    if isinstance(ex, urllib.error.URLError):
        return True                      # DNS / refused / unreachable / proxy not up
    if isinstance(ex, (socket.timeout, TimeoutError, ConnectionError, OSError)):
        return True
    return False


def _explain(ex):
    """A message the user can act on, instead of a raw traceback string."""
    if isinstance(ex, urllib.error.HTTPError):
        if ex.code in (401, 403):
            return "API key rejected — check it in Settings."
        if ex.code == 429:
            return "Rate limited by the API. Give it a moment."
        if ex.code >= 500:
            return f"The API is having trouble (HTTP {ex.code}). Try again shortly."
        return f"API error (HTTP {ex.code})."
    if isinstance(ex, urllib.error.URLError):
        return f"Can't reach the API ({getattr(ex, 'reason', ex)}) — check your connection."
    if isinstance(ex, (socket.timeout, TimeoutError)):
        return "The API timed out. Try again."
    return f"backend error: {ex}"


def _err_body(ex):
    """An HTTPError's body, read ONCE (a second read() returns nothing)."""
    if "_chuck_body" not in ex.__dict__:
        try:
            ex.__dict__["_chuck_body"] = ex.read(4096).decode("utf-8", "ignore").lower()
        except Exception:
            ex.__dict__["_chuck_body"] = ""
    return ex.__dict__["_chuck_body"]


def _mentions_stream_options(ex):
    """Did this 400 complain about stream_options specifically? Anything else
    (a bad model id, a malformed message) is a real error and is not retried —
    and must not switch usage reporting off for the rest of the session."""
    body = _err_body(ex)
    return "stream_options" in body or "include_usage" in body


def _model_unavailable(ex):
    """This MODEL can't serve the request (retired, renamed, not on this
    account) — as opposed to the request itself being bad."""
    if not isinstance(ex, urllib.error.HTTPError):
        return False
    if ex.code == 404:
        return True
    if ex.code in (400, 403, 422):
        b = _err_body(ex)
        return "model" in b and any(w in b for w in (
            "not exist", "not found", "does not exist", "invalid model", "unsupported",
            "disabled", "not available", "unavailable", "not authorized", "no access",
            "permission"))
    return False


class Backend:
    # Some OpenAI-compatible servers reject stream_options with a 400. The
    # first rejection is remembered for the process so it costs one retry,
    # once, rather than a failed request every turn.
    _no_stream_opts = False

    def __init__(self, settings):
        self.s = settings
        # Per-stream side channels. Read by the window after on_done; set as
        # attributes (not callback kwargs) so any stand-in backend that lacks
        # them simply reports nothing instead of breaking the call.
        self.last_usage = None       # {prompt, completion, cached, reasoning}
        self.last_finish = None      # "stop" | "length" | ...
        self.last_model = None       # the model that actually answered (fallback-aware)
        self.on_think = None         # callable(chunk) for reasoning deltas
        self.on_fallback = None      # callable(failed_model, fallback, why)

    def key(self):
        return (self.s.get("siliconflow_api_key") or "").strip()

    def base(self):
        return (self.s.get("siliconflow_base_url") or DEFAULT_BASE).rstrip("/")

    def model(self, vision=False):
        """The chat model — unless this request carries images and the chat
        model can't see, in which case the vision model takes it. With a
        multimodal chat model (the default) one model does both."""
        chat = (self.s.get("model") or DEFAULT_MODEL).strip()
        if vision and not _pricing.sees_images(chat):
            return (self.s.get("vision_model") or DEFAULT_VISION).strip()
        return chat

    def fallback_for(self, model, vision=False):
        """The model to try when `model` fails: Settings' choice, or the first
        suggested model that isn't this one (and can see, if images ride
        along). 'none' turns fallback off."""
        want = (self.s.get("fallback_model") or "").strip()
        if want.lower() == "none":
            return None
        if want and want != model:
            return want
        for cand in _pricing.SUGGESTED:
            if cand != model and (not vision or _pricing.sees_images(cand)):
                return cand
        return None

    def warm_up(self):
        """Open a throwaway connection to the API host in the background.

        The first request of a session otherwise pays for DNS resolution and a
        TLS handshake at the exact moment the user is waiting on it — and if
        anything there hiccups (VPN still coming up, resolver cold) the very
        first message fails while every later one works. Doing it at startup
        moves that cost off the user's first turn.
        """
        def go():
            try:
                host, port = _config.api_connect_host(self.base())
                if host:
                    socket.create_connection((host, port), timeout=5).close()
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()

    def stream(self, messages, on_delta, on_done, on_error, vision=False, should_stop=None,
               attempts=3, on_open=None):
        self.last_usage = None
        self.last_finish = None
        if not self.key():
            on_error("No SiliconFlow API key. Add one in Settings.")
            return
        model = self.model(vision)
        try:
            temp = min(1.5, max(0.0, float(self.s.get("temperature", 0.35))))
        except Exception:
            temp = 0.35
        # A ceiling, not a target: you pay for what is generated, so this only
        # bites on a runaway. It was 1600, which truncated any complete file
        # longer than ~150 lines mid-```write``` — the block never closed, so
        # the file was silently never written. A reply that hits the ceiling is
        # now detected via finish_reason and CONTINUED by the window, so file
        # length is bounded by patience, not by this number.
        try:
            maxtok = int(self.s.get("max_tokens", 8192))
        except Exception:
            maxtok = 8192
        maxtok = max(256, min(32768, maxtok))
        payload = {"model": model, "messages": messages, "stream": True,
                   "temperature": temp, "max_tokens": maxtok}
        if not Backend._no_stream_opts:
            # Real token counts on the final chunk, so the meter shows what the
            # API actually billed rather than a chars/4 guess.
            payload["stream_options"] = {"include_usage": True}
        url = self.base() + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.key(),
                   "Content-Type": "application/json"}
        think = self.on_think

        attempt = 0
        switched = False
        while attempt < attempts:
            if should_stop and should_stop():
                return
            self.last_model = model
            got_any = False             # visible content reached the screen
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                             headers=headers)
                # The proxy setting used to cover page fetches, images and video
                # but NOT this call — so a user who pointed Chuck at Mullvad had
                # every prompt they typed leave over the bare connection while
                # the browsing traffic went through the tunnel. That is the
                # opposite of what the setting appears to promise.
                _op = _config.api_opener()
                _open = _op.open if _op else urllib.request.urlopen
                with _open(req, timeout=180) as resp:
                    if on_open:
                        on_open()          # connection is live — proof of life
                    for raw in resp:
                        if should_stop and should_stop():
                            try:
                                resp.close()
                            except Exception:
                                pass
                            return  # cancelled — the canceller owns cleanup
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            d = json.loads(data)
                        except Exception:
                            continue
                        u = _pricing.usage_from_chunk(d)
                        if u:
                            self.last_usage = u
                        try:
                            ch = (d.get("choices") or [{}])[0]
                            delta = ch.get("delta") or {}
                            if ch.get("finish_reason"):
                                self.last_finish = ch["finish_reason"]
                            r = delta.get("reasoning_content")
                            if r and think:
                                # reasoning is NOT content: a drop after only
                                # reasoning retries cleanly instead of being
                                # reported as an empty, "Done." reply
                                think(r)
                            c = delta.get("content")
                            if c:
                                got_any = True
                                on_delta(c)
                        except Exception:
                            continue
                if should_stop and should_stop():
                    return
                on_done()
                return
            except Exception as ex:
                if should_stop and should_stop():
                    return
                if (isinstance(ex, urllib.error.HTTPError) and ex.code in (400, 422)
                        and "stream_options" in payload and not got_any
                        and _mentions_stream_options(ex)):
                    # Not a real failure: this server doesn't take stream_options.
                    Backend._no_stream_opts = True
                    payload.pop("stream_options", None)
                    continue                     # same attempt, without it
                if got_any:
                    # Text already reached the screen — replaying the request
                    # would duplicate it, so keep what we have and carry on.
                    on_done()
                    return
                attempt += 1
                # This model is down, rate-limited past our retries, or gone:
                # answer THIS request with the fallback model once, and say so,
                # rather than stop the user's task dead.
                # Only when the API ANSWERED (an HTTP status): a dead network or
                # DNS failure is no model's fault, and another model can't help.
                exhausted = (attempt >= attempts and _is_transient(ex)
                             and isinstance(ex, urllib.error.HTTPError))
                fb = None if switched else self.fallback_for(model, vision)
                if fb and (exhausted or _model_unavailable(ex)):
                    switched = True
                    if self.on_fallback:
                        try:
                            self.on_fallback(model, fb, _explain(ex))
                        except Exception:
                            pass
                    model = payload["model"] = fb
                    attempt = 0
                    continue
                if attempt >= attempts or not _is_transient(ex):
                    on_error(_explain(ex))
                    return
                time.sleep(0.5 * attempt)       # brief backoff, then retry


# ── UI ──────────────────────────────────────────────────────────────────────
def _follow_bottom(sw, alive=lambda: True):
    """Keep a scrolled window pinned to its newest line, like a terminal — but
    only while it is at the bottom, so scrolling up to read is never yanked
    back. The pin is deferred to idle: notify::upper fires during allocation,
    and a value set then moves the scrollbar but not the content."""
    st = {"follow": True, "pending": False}
    try:
        adj = sw.get_vadjustment()
    except Exception:
        return st

    def pin():
        st["pending"] = False
        if st["follow"] and alive():
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def on_size(a, *_):
        if st["follow"] and not st["pending"]:
            st["pending"] = True
            GLib.idle_add(pin)

    def on_value(a, *_):
        if not st["pending"]:                    # our own pin isn't the user scrolling
            st["follow"] = a.get_value() >= a.get_upper() - a.get_page_size() - 24
    try:
        adj.connect("notify::upper", on_size)
        adj.connect("notify::page-size", on_size)   # the panel grows to its cap
        adj.connect("value-changed", on_value)
    except Exception:
        pass
    return st


class _LiveFeed:
    """A command's output, shown in its card WHILE it runs.

    push() is called from reader threads with each chunk; the panel is built
    lazily on the first one (a quick command never shows it) and refreshed at
    most five times a second, keeping the last 12k characters, pinned to the
    bottom like a terminal. close() (UI thread) removes it; the final, full
    output panel replaces it."""
    TAIL = 12000

    def __init__(self, win, sink):
        self.win, self.sink = win, sink
        self.lock = threading.Lock()
        self.text = ""
        self.lines = 0
        self.panel = self.label = self.meta = self.sw = None
        self.scheduled = False
        self.closed = False
        self.t0 = time.time()

    def push(self, chunk):
        if self.sink is None:
            return
        with self.lock:
            self.text = (self.text + chunk)[-self.TAIL:]
            self.lines += chunk.count("\n")
            if self.scheduled or self.closed:
                return
            self.scheduled = True
        GLib.timeout_add(200, self._flush)

    def _flush(self):
        with self.lock:
            self.scheduled = False
            if self.closed:
                return False
            text, lines = self.text, self.lines
        try:
            if self.panel is None:
                self._build()
            self.label.set_text(text.rstrip("\n"))
            self.meta.set_text(self._meta_text())
        except Exception:
            pass
        return False

    def _meta_text(self):
        secs = int(time.time() - self.t0)
        t = f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"
        return f"{self.lines} line{'' if self.lines == 1 else 's'} \u00b7 {t}"

    def _tick(self):
        if self.closed or self.meta is None:
            return False
        try:
            self.meta.set_text(self._meta_text())
        except Exception:
            return False
        return True

    def _build(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("term-card"); card.add_css_class("term-live")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("term-head")
        tl = Gtk.Label(label="LIVE", xalign=0); tl.add_css_class("term-label")
        self.meta = Gtk.Label(label="", xalign=0, hexpand=True)
        self.meta.add_css_class("term-meta")
        head.append(tl); head.append(self.meta)
        self.label = Gtk.Label(label="", xalign=0, selectable=False, wrap=False)
        self.label.add_css_class("term-body")
        self.label.set_valign(Gtk.Align.START)
        self.sw = Gtk.ScrolledWindow()
        self.sw.add_css_class("term-scroll")
        try:
            self.sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            self.sw.set_max_content_height(220)
            self.sw.set_propagate_natural_height(True)
        except Exception:
            pass
        self.sw.set_child(self.label)
        self.win._chain_scroll(self.sw, vertical=True)
        _follow_bottom(self.sw, lambda: not self.closed)
        GLib.timeout_add_seconds(1, self._tick)    # the clock runs through silent steps
        card.append(head); card.append(self.sw)
        self.sink.append(card)
        self.panel = card

    def close(self):
        with self.lock:
            self.closed = True
        if self.panel is not None:
            try:
                self.sink.remove(self.panel)
            except Exception:
                pass
            self.panel = None


class GlyphField:
    """The welcome screen's background: a hidden net of tiny code glyphs that
    lights up in waves wherever you click — like a disturbed water surface
    catching the light.

    At rest the net is INVISIBLE and nothing is drawn: zero idle cost. A click
    starts a ripple; the glyphs it reaches glow up (neutral at the edge of the
    wave, violet at its crest), shift outward and flip to other characters,
    then fade back to nothing as the wave passes.

    Built to stay at 60 fps with ~18,000 glyphs:
      * no per-click work — for each row of the net, the ring crosses at most
        two column spans, computed exactly from the circle equation, so a frame
        visits only the glyphs inside the ring;
      * the wave falloff is a lookup table, not exp() per glyph;
      * lit glyphs are bucketed into 8 brightness levels and each level is one
        cairo show_glyphs() call (glyph ids resolved once), instead of one
        text-shaping show_text() call per glyph.
    Honours the desktop's reduce-animation setting. Not a subclass (the
    headless test stub can't be subclassed): it owns a DrawingArea instead.
    """
    CHARS = "{}[]()<>=;:+-*/#&|~$%01λ0123456789abcdef"
    CELL_W, CELL_H, FONT = 7, 9, 6.0
    SPEED, BAND, LIFE, PUSH = 340.0, 16.0, 2.3, 4.0
    LEVELS = 8
    MAX_WAVES = 3
    STEP = 320                       # the net grows in these steps

    def __init__(self):
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_can_target(False)
        self.ripples = []            # [x, y, t0]
        self._tick = 0
        self._size = (0, 0)
        self._rows = []              # rows[r][c] = (x, y, char_idx, bright, seed) | None
        self._gids = None            # glyph id per CHARS entry, for show_glyphs
        reach = int(2 * self.BAND) + 2
        self._falloff = [math.exp(-(o * o) / (self.BAND * self.BAND)) for o in range(reach)]
        self.enabled = self._cairo_ok()
        if self.enabled:
            try:
                self.area.set_draw_func(self._draw)
            except Exception:
                self.enabled = False

    @staticmethod
    def _cairo_ok():
        """PyGObject needs its cairo bridge to hand a draw function a context.
        Without it every frame would raise, so the field simply stays plain."""
        try:
            import cairo  # noqa: F401
            gi.require_foreign("cairo")
            return True
        except Exception:
            return False

    @staticmethod
    def _animations_on():
        try:
            return bool(Gtk.Settings.get_default().get_property("gtk-enable-animations"))
        except Exception:
            return True

    def listen(self, widget):
        """Ripple on clicks anywhere over `widget`, without stealing them from
        the buttons on top (capture phase, never claims the sequence)."""
        try:
            g = Gtk.GestureClick()
            g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            g.connect("pressed", lambda _g, _n, x, y: self.poke(x, y))
            widget.add_controller(g)
        except Exception:
            pass

    def _ensure(self, w, h):
        import random
        W = (int(w) // self.STEP + 1) * self.STEP
        H = (int(h) // self.STEP + 1) * self.STEP
        if (W, H) == self._size and self._rows:
            return
        rnd = random.Random(0x0C4C)
        cx, cy = w / 2.0, h * 0.42
        maxd = math.hypot(max(cx, w - cx), max(cy, h - cy)) or 1.0
        n = len(self.CHARS)
        rows = []
        for r in range(H // self.CELL_H + 1):
            row = []
            for c in range(W // self.CELL_W + 1):
                if rnd.random() < 0.1:                  # a few gaps keep it organic
                    row.append(None)
                    continue
                x = c * self.CELL_W + 1 + (r % 2) * (self.CELL_W / 2)
                y = r * self.CELL_H + 7
                d = math.hypot(x - cx, y - cy) / maxd
                # dimmer behind the content in the middle, brighter toward the edges
                bright = (0.55 + rnd.random() * 0.45) * (0.45 + 0.55 * min(1.0, d * 1.25))
                row.append((x, y, rnd.randrange(n), bright, rnd.randrange(997)))
            rows.append(row)
        self._rows = rows
        self._size = (W, H)

    def poke(self, x, y):
        if not self.enabled or not self._animations_on():
            return
        self.ripples = (self.ripples + [(float(x), float(y), time.monotonic())])[-self.MAX_WAVES:]
        if not self._tick:
            try:
                self._tick = self.area.add_tick_callback(self._on_tick)
            except Exception:
                self._tick = 0
        self.area.queue_draw()

    def _on_tick(self, _w, _clock):
        now = time.monotonic()
        self.ripples = [r for r in self.ripples if now - r[2] < self.LIFE]
        self.area.queue_draw()
        if not self.ripples:
            self._tick = 0
            return False
        return True

    def _glyph_ids(self, cr):
        if self._gids is None:
            sf = cr.get_scaled_font()
            ids = []
            for ch in self.CHARS:
                g = sf.text_to_glyphs(0, 0, ch, False)
                ids.append(g[0][0] if g else 0)
            self._gids = ids
        return self._gids

    def _lit(self, w, h, now):
        """{(row, col): [intensity, dx, dy, age]} for every glyph a wave is on."""
        hit = {}
        cw, ch = self.CELL_W, self.CELL_H
        fall, reach = self._falloff, len(self._falloff) - 1
        nrows = len(self._rows)
        for (rx, ry, t0) in self.ripples:
            age = now - t0
            rad = age * self.SPEED
            amp = (1.0 - age / self.LIFE) ** 1.5
            if amp <= 0.02:
                continue
            r1, r2 = max(0.0, rad - reach), rad + reach
            r1s, r2s = r1 * r1, r2 * r2
            row_lo = max(0, int((ry - r2 - 7) // ch))
            row_hi = min(nrows - 1, int((ry + r2 - 7) // ch) + 1)
            for r in range(row_lo, row_hi + 1):
                row = self._rows[r]
                y = r * ch + 7
                dy = y - ry
                dy2 = dy * dy
                if dy2 > r2s:
                    continue
                outer = math.sqrt(r2s - dy2)
                inner = math.sqrt(r1s - dy2) if dy2 < r1s else 0.0
                shift = 1 + (r % 2) * (cw / 2)
                for xa, xb in ((rx - outer, rx - inner), (rx + inner, rx + outer)):
                    c0 = max(0, int((xa - shift) // cw))
                    c1 = min(len(row) - 1, int((xb - shift) // cw) + 1)
                    for c in range(c0, c1 + 1):
                        g = row[c]
                        if g is None:
                            continue
                        dx = g[0] - rx
                        d = math.sqrt(dx * dx + dy2)
                        o = abs(d - rad)
                        if o >= reach:
                            continue
                        k = amp * fall[int(o)]
                        if k < 0.05:
                            continue
                        key = (r, c)
                        e = hit.get(key)
                        if e is None:
                            e = hit[key] = [0.0, 0.0, 0.0, age]
                        e[0] += k
                        if d > 0.5:
                            e[1] += dx / d * k * self.PUSH
                            e[2] += dy / d * k * self.PUSH
                    if inner == 0.0:
                        break                           # one span covered the row
        return hit

    def _draw(self, _area, cr, w, h):
        if not self.ripples:
            return                                      # invisible at rest
        try:
            import cairo
        except Exception:
            return
        self._ensure(w, h)
        hit = self._lit(w, h, time.monotonic())
        if not hit:
            return
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(self.FONT)
        try:
            # grey AA, no hinting: identical at 6px, far cheaper than subpixel
            fo = cairo.FontOptions()
            fo.set_antialias(cairo.ANTIALIAS_GRAY)
            fo.set_hint_style(cairo.HINT_STYLE_NONE)
            fo.set_hint_metrics(cairo.HINT_METRICS_OFF)
            cr.set_font_options(fo)
        except Exception:
            pass
        gids = self._glyph_ids(cr)
        n, L = len(self.CHARS), self.LEVELS
        buckets = [[] for _ in range(L)]
        for (r, c), (inten, dx, dy, age) in hit.items():
            x, y, ci, bright, seed = self._rows[r][c]
            inten = min(1.0, inten)
            v = inten * bright
            lvl = min(L - 1, int(v * L))
            if lvl <= 0 and v < 0.06:
                continue
            if inten > 0.45:                            # flips while the crest passes
                ci = (seed + int(age * 26)) % n
            buckets[lvl].append((gids[ci], x + dx, y + dy))
        for lvl, glyphs in enumerate(buckets):
            if not glyphs:
                continue
            t = (lvl + 0.5) / L
            # neutral at the edge of the wave, violet at the crest
            cr.set_source_rgba(0.74 + 0.02 * t, 0.72 - 0.14 * t, 0.86 + 0.12 * t,
                               min(0.9, 0.08 + t * 0.85))
            cr.show_glyphs(glyphs)


_FILE_FENCE = re.compile(r"^[ \t]*(`{3,4})(write|edit|patch)(?![A-Za-z])[ \t]*(.*)$")
_DOC_EXT = re.compile(r"\.(md|markdown|mdx|rst|txt|adoc|html?|ipynb|org)$", re.IGNORECASE)


def file_blocks(text):
    """Find ```write / ```edit blocks with NESTED-FENCE awareness.

    A README or a docs page contains ``` fences of its own. The old non-greedy
    regex ended the write block at the FIRST inner fence: the file was cut
    short, and the code block that followed it inside the file was then picked
    up as a separate ```bash block and EXECUTED. Here a 3-backtick block tracks
    depth — an inner ```lang opens, a bare ``` closes — and only the bare ```
    at depth zero ends it. A 4-backtick block ends only at ````. If a block
    never balances, the old first-bare-fence rule is used, so nothing parses
    worse than before. Returns [{tag, path, body, start, end}].
    """
    lines = text.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    out, i = [], 0
    while i < len(lines):
        m = _FILE_FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        fence, tag, head = m.group(1), m.group(2), m.group(3).strip()
        j = i + 1
        if head.endswith(fence):                     # ```write a.py``` — empty, ignore
            i += 1
            continue
        if tag == "patch":
            # A diff: every body line starts with ' ', '+' or '-', so the ONLY
            # line that can be exactly the fence is the closing one — no depth
            # counting, no four backticks needed, even for a README's ``` lines.
            k = j
            while k < len(lines) and lines[k].rstrip() != fence:
                k += 1
            if k >= len(lines):
                i += 1
                continue
            body = "\n".join(([head] if head else []) + lines[j:k]) + "\n"
            out.append({"tag": "patch", "path": "", "body": body, "ambiguous": False,
                        "start": offs[i], "end": min(offs[k] + len(lines[k]), len(text))})
            i = k + 1
            continue
        if not head:                                 # path on the next line
            if j >= len(lines):
                break
            head, j = lines[j].strip(), j + 1
        body_start, end_line, end_col, depth, first_bare = j, None, None, 0, None
        k = j
        while k < len(lines):
            st = lines[k].strip()
            if len(fence) == 4:
                if st == "````":
                    end_line = k
                    break
            elif st == "```":
                if first_bare is None:
                    first_bare = k
                if depth == 0:
                    end_line = k
                    break
                depth -= 1
            elif st.startswith("```") and not st.startswith("````"):
                depth += 1
            elif lines[k].rstrip().endswith(fence) and depth == 0 and k > j - 1:
                end_line, end_col = k, lines[k].rstrip().rfind(fence)   # body```
                break
            k += 1
        if end_line is None and first_bare is not None:
            end_line = first_bare                    # unbalanced: old behaviour
        if end_line is None:                         # never closed (cut off)
            i += 1
            continue
        if end_col is not None:
            body = "\n".join(lines[body_start:end_line] + [lines[end_line][:end_col]])
        else:
            # the fence sits on its own line, so the file's last line ended
            # with a newline — keep it (POSIX text files end with one)
            body = "\n".join(lines[body_start:end_line])
            if end_line > body_start:
                body += "\n"
        end = offs[end_line] + len(lines[end_line])
        # A bare ``` inside a 3-backtick block is undecidable — it may close
        # the file or open a block inside it. For a DOCUMENT (where inner
        # fences are expected) a later bare fence means we may have closed
        # too early: flag it rather than write half a file and run the rest.
        # Blocks that legitimately follow (```bash to open it in a browser,
        # ```look at the screenshot) are balanced: their closing fence is not
        # evidence. Only a bare fence that closes NOTHING is — counting every
        # bare fence flagged any doc written alongside another tool.
        ambiguous = False
        if len(fence) == 3 and tag == "write" and _DOC_EXT.search(head):
            depth_after = 0
            for ln in lines[end_line + 1:]:
                if _FILE_FENCE.match(ln):
                    break
                st = ln.strip()
                if st == "```":
                    if depth_after:
                        depth_after -= 1
                        continue
                    ambiguous = True
                    break
                if st.startswith("```") and not st.startswith("````"):
                    if not (len(st) > 6 and st.endswith("```")):   # not ```look x```
                        depth_after += 1
        out.append({"tag": tag, "path": head, "body": body, "ambiguous": ambiguous,
                    "start": offs[i], "end": min(end, len(text))})
        i = end_line + 1
    return out


def _cut_spans(text, spans):
    """text with [start, end) spans removed."""
    out, last = [], 0
    for a, b in sorted(spans):
        if a >= last:
            out.append(text[last:a])
            last = b
    out.append(text[last:])
    return "".join(out)


def _lane(widget):
    """Wrap a piece of machine work (a command card, a note, a plan, an image
    Chuck fetched) in the work lane: inset, on a rail that hangs off Chuck's
    avatar. Rebuilt widgets are re-wrapped, so the lane survives windowing."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("lane")
    box.append(widget)
    return box


def _short_path(p):
    """~/code/thing instead of /home/you/code/thing."""
    if not p:
        return ""
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def _wrap_anywhere(label):
    """Wrap at words, but break an unbroken run (a URL, a hash, a path) rather
    than let it push the column wider than the window."""
    try:
        label.set_wrap(True)
        if Pango is not None:
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    except Exception:
        pass


def _ago(path):
    """'just now' / '12m ago' / '3h ago' / '2d ago' from a file's mtime."""
    try:
        secs = max(0, time.time() - Path(path).stat().st_mtime)
    except OSError:
        return ""
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


class ChuckWindow(Adw.ApplicationWindow):
    # Suggestions on the empty screen, per mode: (title, subtitle, prompt).
    # They PREFILL rather than send, so the first move is still the user's.
    AGENT_SUGGEST = (
        ("Check system health", "failed services, disk, errors since boot",
         "Check my system's health: failed services, disk space and errors since the last boot."),
        ("Update safely", "keyring first, then a full -Syu",
         "Update the system safely."),
        ("What's eating my disk?", "biggest folders, caches, orphans",
         "What's eating my disk?"),
        ("Map this project", "structure, how to run it, what's broken",
         "Map the project in the working folder: structure, how to run it, and anything broken."),
    )
    CHAT_SUGGEST = (
        ("Explain something", "clearly, with an example",
         "Explain how "),
        ("Compare options", "trade-offs, then a recommendation",
         "Compare "),
        ("What's new", "checked against live sources",
         "What's the latest on "),
        ("Draft it for me", "an email, a README, a plan",
         "Draft "),
    )

    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Chuck")
        self.set_default_size(1180, 820)
        try:
            self.set_size_request(560, 480)
        except Exception:
            pass
        self.add_css_class("main-window")
        self.settings = _SETTINGS
        self.backend = Backend(self.settings)
        self.pending_shot = None
        self.pending_file = None
        self._bot_label = None
        self._bot_text = ""
        self._busy_n = 0
        self._hops = 0
        self._steps = 0                 # agent steps taken this turn
        self._nudges = 0                # "you said you'd do it — do it" pushes this turn
        self._plan_nudges = 0           # "your plan isn't finished" pushes this turn
        self._verify_nudges = 0         # "you never ran what you changed" pushes this turn
        self._dirty = set()             # code files changed since anything last ran
        self._asked = False             # this reply asked the user something: wait
        self._step_forced = False       # already told to wrap up this turn
        self._pending_tools = 0
        self._tool_feedback = []
        self._loop_web = None
        self._log = []              # rebuildable transcript entries
        self._win_start = 0         # index of the first materialised entry
        self._loading_older = False
        self._bot_entry = None
        self._seen_queries = []     # queries already run this turn
        self._seen_urls = set()     # pages already read this turn
        self._dead_urls = set()     # pages that failed — don't retry
        self._forced_answer = False
        self._awaiting_first = False
        self._thinking = False          # reasoning tokens are arriving
        self._think_text = ""
        self._think_t0 = 0.0
        self._last_paint = 0.0
        self._activity_box = None
        self._activity_steps = []
        self._activity_lock = threading.Lock()
        self._running = False           # a turn is in progress
        self._cancelled = False         # user pressed Stop
        # Turn generation. Stop and every new turn bump it; anything that
        # arrives late (a stream still draining, a command finishing after
        # Stop) checks it and drops itself instead of writing into a newer turn.
        self._gen = 0
        self._sudo_pw = None            # sudo password: memory only, this chat
        self._sudo_pw_time = 0.0        # when it was entered (30-min TTL)
        self._awaiting_input = False    # a modal (sudo prompt) is open — pause the watchdog
        self._await_lock = threading.Lock()
        self._await_depth = 0           # how many workers are blocked on a modal
        self._sudo_cv = threading.Condition()
        self._sudo_asking = False       # a sudo dialog is open RIGHT NOW
        self._sudo_denied = False       # user cancelled — don't re-ask this turn
        self._exec_lock = threading.RLock()   # ONE command runs at a time
        self._run_started = 0.0         # wall-clock start of the current run
        self._heartbeat_id = 0          # GLib timer id for the "still working" ticker
        self._watchdog_id = 0           # GLib timer id for the stuck-detector
        self._last_progress = 0.0       # last time anything happened (for watchdog)
        self._run_budget = 0            # seconds the currently-running command is allowed
        self._dispatching = False       # _finalise is still handing out tool work
        self._plan_entry = None         # this turn's live plan card
        self._attachments = []          # the tray: files/images going with the next message
        self._changes = []              # file checkpoints: what every write/edit replaced
        self._turn_no = 0               # which task a change belongs to (for "undo all")
        self._user_notes = []           # things the user did that the model must hear about
        self._steer = []                # messages typed while a task runs, for its next step
        self.plan_mode = False          # investigate read-only, propose, wait for approval
        self._carry_plan = None         # an approved plan card the next turn ticks off
        self._quiet_send = False
        self._edit_at = None            # editing the user message at this history index
        self._hook_runs = 0
        self._hook_passed_turn = None
        self._acted_since_nudge = False
        self._jobs = {}                 # background jobs: name -> {proc, log, cmd, card}
        self._py = None                 # the ```py``` session, started on first use
        # MCP servers from ~/.config/chucknorris/mcp.json, started in the
        # background; when their tool lists arrive the system prompt is rebuilt
        self.MCP_CONFIG = CONFIG_DIR / "mcp.json"
        self._mcp = None
        if _mcp is not None:
            self._mcp = _mcp.Manager(
                str(self.MCP_CONFIG), _config.VERSION,
                roots=lambda: [str(self.workspace)] if self.workspace else [],
                on_change=lambda: GLib.idle_add(lambda: (self._refresh_system(), False)[1]))
            if self.MCP_CONFIG.exists():
                self._mcp.start_all()
        self._jobs_timer = 0
        self._tool_images = []          # images the NEXT request carries (base64, mime)
        self._reset_continuation()
        self._hint = None
        # scrolling: follow the newest output only while the user is AT the
        # bottom; the moment they scroll up to read, stop dragging them down.
        self._stick = True
        self._anchor = None             # (old_upper, old_value) while paging older
        self._banner = None
        # usage: real counts from the API when it reports them, estimates when not
        self._tok_in = self._tok_out = 0
        self._usage = self._blank_usage()
        self._cap_extra = 0.0           # spending approved past the cap, this chat
        self._compact = None            # {"upto": i, "text": summary} of history[1:i]
        self._compacting = False
        self._session = self._blank_usage()
        self._balance = ""
        self.workspace = None
        _WORKDIR["path"] = None
        self.agent_mode = bool(self.settings.get("agent_mode", True))
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()

        self._build_ui()
        self.connect("close-request", self._on_close)

        # Prime DNS/TLS to the API now, so the FIRST message isn't the one
        # paying for a cold connection.
        try:
            self.backend.warm_up()
        except Exception:
            pass

        harden_existing_permissions()
        _sweep_scratch()
        # purge on launch, then keep purging + refreshing while the app runs.
        # NOTE the argument: purge_old_chats() with no TTL falls back to the
        # shipped 24h default, so raising "auto-delete after" to a week in
        # Settings still lost everything over 24h on the next launch. The
        # timed sweep already used cfg(); the launch purge did not.
        purge_old_chats(self.cfg('chat_ttl_hours', CHAT_TTL_HOURS, 1, 720))
        self._refresh_sidebar()
        GLib.timeout_add_seconds(600, self._sweep_chats)
        self._apply_mode(announce=False)
        self._show_empty_hint()
        GLib.timeout_add_seconds(20, self._update_check)   # once a day, after startup settles

    # ── layout ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        # header: sidebar + new chat | wordmark + context | usage, memory, voice, settings
        header = Adw.HeaderBar()
        header.add_css_class("main-header")
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        mark = Gtk.Label(label="C")
        mark.add_css_class("mark")
        mark.set_valign(Gtk.Align.CENTER)
        tb.append(mark)
        tcol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tcol.set_valign(Gtk.Align.CENTER)
        tl = Gtk.Label(label="Chuck", xalign=0)
        tl.add_css_class("title")
        self._subtitle = Gtk.Label(label="", xalign=0)
        self._subtitle.add_css_class("sub")
        try:
            self._subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            self._subtitle.set_max_width_chars(48)
        except Exception:
            pass
        tcol.append(tl); tcol.append(self._subtitle)
        tb.append(tcol)
        header.set_title_widget(tb)

        self.side_btn = Gtk.ToggleButton(
            icon_name=_pick_icon("sidebar-show-symbolic", "view-dual-symbolic",
                                 "view-list-bullet-symbolic", "format-justify-fill-symbolic",
                                 "document-open-recent-symbolic"))
        self.side_btn.add_css_class("headerbtn")
        self.side_btn.set_tooltip_text("Chats  (Ctrl+B)")
        self.side_btn.set_active(bool(self.settings.get("sidebar_open", True)))
        self.side_btn.connect("toggled", self._toggle_sidebar)
        header.pack_start(self.side_btn)
        newb = Gtk.Button(icon_name=_pick_icon("document-new-symbolic", "tab-new-symbolic",
                                               "list-add-symbolic"))
        newb.add_css_class("headerbtn")
        newb.set_tooltip_text("New chat  (Ctrl+N)")
        newb.connect("clicked", lambda *_: self.new_chat())
        header.pack_start(newb)

        setb = Gtk.Button(icon_name=_pick_icon("emblem-system-symbolic", "preferences-system-symbolic"))
        setb.add_css_class("headerbtn"); setb.set_tooltip_text("Settings  (Ctrl+,)")
        setb.connect("clicked", self.open_settings)
        header.pack_end(setb)
        self.tts_btn = Gtk.ToggleButton(icon_name=_pick_icon(
            "audio-volume-high-symbolic", "audio-speakers-symbolic", "audio-x-generic-symbolic"))
        self.tts_btn.add_css_class("headerbtn")
        self.tts_btn.set_tooltip_text("Read replies aloud")
        self.tts_btn.set_active(self.settings.get("tts", True))
        self.tts_btn.connect("toggled", self._on_tts_toggled)
        header.pack_end(self.tts_btn)
        memb = Gtk.Button(icon_name=_pick_icon("user-bookmarks-symbolic", "starred-symbolic",
                                               "view-list-symbolic"))
        memb.add_css_class("headerbtn"); memb.set_tooltip_text("Memory & skills")
        memb.connect("clicked", self.open_memory)
        header.pack_end(memb)
        self.mem_btn = memb
        # the meter: what this chat has cost, one click for the breakdown
        self._meter_lbl = Gtk.Label(label="$0.00")
        self.meter_btn = Gtk.MenuButton()
        self.meter_btn.add_css_class("meter")
        self.meter_btn.set_child(self._meter_lbl)
        self.meter_btn.set_tooltip_text("Usage and cost")
        self.meter_btn.set_valign(Gtk.Align.CENTER)
        self._usage_pop = Gtk.Popover()
        self._usage_pop.connect("show", lambda *_: self._fill_usage_popover())
        self.meter_btn.set_popover(self._usage_pop)
        header.pack_end(self.meter_btn)

        # transcript. No horizontal scrolling at the page level, ever: wide
        # content (code, tables, terminal output) scrolls inside its own block.
        # spacing 0: rhythm comes from per-row margins, so consecutive work-lane
        # items join into one continuous rail instead of a dashed line
        self.msgbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for m, v in (("top", 24), ("bottom", 28), ("start", 22), ("end", 22)):
            getattr(self.msgbox, f"set_margin_{m}")(v)
        self.scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.scroller.add_css_class("chat-scroll")
        try:
            self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        except Exception:
            pass
        clamp = Adw.Clamp()
        try:
            clamp.set_maximum_size(820)
            clamp.set_tightening_threshold(620)
        except Exception:
            pass
        clamp.set_child(self.msgbox)
        # An explicit viewport so scroll-to-focus can be turned OFF. With it on,
        # GTK scrolls to whatever has keyboard focus — click a reply to select
        # text, and every repaint while the next answer streams yanked the view
        # back up to that label.
        vp = Gtk.Viewport()
        try:
            vp.set_scroll_to_focus(False)
            # NATURAL, not the default MINIMUM: with MINIMUM every child is laid
            # out at its minimum height, which for a nested scroller (terminal
            # output, a diff) is ONE LINE — the panels showed a single row.
            vp.set_vscroll_policy(Gtk.ScrollablePolicy.NATURAL)
        except Exception:
            pass
        vp.set_child(clamp)
        self.scroller.set_child(vp)
        adj = self.scroller.get_vadjustment()
        adj.connect("value-changed", self._on_scroll)
        adj.connect("notify::upper", self._on_extent)
        adj.connect("notify::page-size", self._on_extent)

        chat_ov = Gtk.Overlay()
        chat_ov.set_child(self.scroller)
        self.jump_btn = Gtk.Button(label="↓  Jump to latest")
        self.jump_btn.add_css_class("jump-btn")
        self.jump_btn.set_halign(Gtk.Align.CENTER)
        self.jump_btn.set_valign(Gtk.Align.END)
        self.jump_btn.set_margin_bottom(14)
        self.jump_btn.set_visible(False)
        self.jump_btn.connect("clicked", lambda *_: self._scroll_down(force=True))
        chat_ov.add_overlay(self.jump_btn)

        self.view_stack = Gtk.Stack()
        try:
            self.view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
            self.view_stack.set_transition_duration(160)
        except Exception:
            pass
        self._field = GlyphField()
        self._empty_holder = Gtk.Overlay()
        self._empty_holder.set_vexpand(True)
        self._empty_holder.set_child(self._field.area)
        self._field.listen(self._empty_holder)
        self.view_stack.add_named(self._empty_holder, "empty")
        self.view_stack.add_named(chat_ov, "chat")

        # status line: spinner + "working… 12s"
        self.spinner = Gtk.Spinner(); self.spinner.set_visible(False)
        self.live = Gtk.Label(label="", xalign=0, hexpand=True)
        self.live.add_css_class("live")
        try:
            self.live.set_ellipsize(Pango.EllipsizeMode.END)
        except Exception:
            pass
        self.live.set_visible(False)
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        status.add_css_class("status-row")
        status.append(self.spinner); status.append(self.live)

        # composer: text on top, a toolbar under it
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.entry.add_css_class("composer-entry")
        self.entry.set_top_margin(6); self.entry.set_bottom_margin(6)
        self.entry.set_left_margin(6); self.entry.set_right_margin(6)
        try:
            self.entry.set_accepts_tab(False)
        except Exception:
            pass
        _kc = Gtk.EventControllerKey(); _kc.connect("key-pressed", self._on_key)
        self.entry.add_controller(_kc)
        self.entry.get_buffer().connect("changed", lambda *_: self._sync_placeholder())
        ev = Gtk.ScrolledWindow(hexpand=True)
        ev.add_css_class("composer-scroll")
        try:
            ev.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            ev.set_min_content_height(30)
            ev.set_max_content_height(220)
            ev.set_propagate_natural_height(True)
        except Exception:
            pass
        ev.set_child(self.entry)
        self._placeholder = Gtk.Label(label="", xalign=0)
        self._placeholder.add_css_class("placeholder")
        self._placeholder.set_can_target(False)
        self._placeholder.set_halign(Gtk.Align.START)
        self._placeholder.set_valign(Gtk.Align.START)
        self._placeholder.set_margin_start(8); self._placeholder.set_margin_top(6)
        eo = Gtk.Overlay(); eo.set_child(ev); eo.add_overlay(self._placeholder)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        sw.add_css_class("mode-switch")
        sw.set_valign(Gtk.Align.CENTER)
        self.agent_btn = Gtk.ToggleButton(label="Agent")
        self.agent_btn.add_css_class("mode-btn")
        self.agent_btn.set_tooltip_text("Agent: Chuck runs commands and edits files  (Tab)")
        self.chat_btn = Gtk.ToggleButton(label="Chat")
        self.chat_btn.add_css_class("mode-btn"); self.chat_btn.add_css_class("mode-chat")
        self.chat_btn.set_tooltip_text("Chat: answers only, nothing runs  (Tab)")
        try:
            self.chat_btn.set_group(self.agent_btn)
        except Exception:
            pass
        self.agent_btn.connect("toggled", self._on_mode_btn)
        sw.append(self.agent_btn); sw.append(self.chat_btn)
        bar.append(sw)
        self.plan_btn = Gtk.ToggleButton(label="Plan")
        self.plan_btn.add_css_class("chip"); self.plan_btn.set_valign(Gtk.Align.CENTER)
        self.plan_btn.set_tooltip_text("Plan first: Chuck investigates read-only and proposes a "
                                       "plan; nothing changes until you approve it  (/plan)")
        self.plan_btn.connect("toggled", lambda b: self.set_plan_mode(b.get_active()))
        bar.append(self.plan_btn)

        self.ws_btn = Gtk.Button()
        self.ws_btn.add_css_class("chip")
        self.ws_btn.set_valign(Gtk.Align.CENTER)
        wsb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        wsb.append(Gtk.Image.new_from_icon_name(_pick_icon("folder-symbolic", "folder")))
        self._ws_lbl = Gtk.Label(label="No folder")
        try:
            self._ws_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            self._ws_lbl.set_max_width_chars(22)
        except Exception:
            pass
        wsb.append(self._ws_lbl)
        self.ws_btn.set_child(wsb)
        self.ws_btn.set_tooltip_text("Working folder for this session  (Ctrl+O)")
        self.ws_btn.connect("clicked", self.open_workspace)
        bar.append(self.ws_btn)

        att = Gtk.Button(icon_name=_pick_icon("mail-attachment-symbolic", "edit-copy-symbolic",
                                              "list-add-symbolic"))
        att.add_css_class("icon-btn"); att.set_valign(Gtk.Align.CENTER)
        att.set_tooltip_text("Attach files  (or drop them in, or paste an image)")
        att.connect("clicked", self.on_attach)
        cam = Gtk.Button(icon_name=_pick_icon("camera-photo-symbolic", "camera-symbolic",
                                              "applets-screenshooter-symbolic"))
        cam.add_css_class("icon-btn"); cam.set_valign(Gtk.Align.CENTER)
        cam.set_tooltip_text("Attach a screenshot of your screen")
        self.cam_btn = cam
        cam.connect("clicked", self.on_screenshot)
        bar.append(att); bar.append(cam)
        spacer = Gtk.Box(hexpand=True); bar.append(spacer)
        self._model_lbl = Gtk.Label(label="")
        try:
            self._model_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            self._model_lbl.set_max_width_chars(22)
        except Exception:
            pass
        mb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        mb.append(self._model_lbl)
        mb.append(Gtk.Image.new_from_icon_name(_pick_icon("pan-down-symbolic", "go-down-symbolic")))
        self.model_btn = Gtk.MenuButton()
        self.model_btn.add_css_class("model-btn")
        self.model_btn.set_child(mb)
        self.model_btn.set_valign(Gtk.Align.CENTER)
        self.model_btn.set_tooltip_text("Switch model")
        self._model_pop = Gtk.Popover()
        self._model_pop.connect("show", lambda *_: self._fill_model_popover())
        self.model_btn.set_popover(self._model_pop)
        bar.append(self.model_btn)
        self.send_btn = Gtk.Button(icon_name=_pick_icon("go-up-symbolic", "pan-up-symbolic", "up-symbolic"))
        self.send_btn.add_css_class("send-fab")
        self.send_btn.set_valign(Gtk.Align.CENTER)
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.send_btn.connect("clicked", self._on_send_or_stop)
        bar.append(self.send_btn)

        self._tray = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._tray.add_css_class("tray")
        self._tray.set_visible(False)
        pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pill.add_css_class("composer")
        pill.append(self._tray); pill.append(eo); pill.append(bar)
        ccol = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        ccol.append(status); ccol.append(pill)
        cclamp = Adw.Clamp()
        try:
            cclamp.set_maximum_size(820)
            cclamp.set_tightening_threshold(620)
        except Exception:
            pass
        cclamp.set_child(ccol)
        for m, v in (("top", 4), ("bottom", 16), ("start", 16), ("end", 16)):
            getattr(cclamp, f"set_margin_{m}")(v)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        # toasts float over the TRANSCRIPT, just above the composer — not over
        # the composer's own controls, which they used to cover
        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.view_stack)
        self.toasts.set_vexpand(True)
        body.append(self.toasts); body.append(cclamp)

        # sidebar: new chat, search, saved chats
        side_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        side_box.add_css_class("sidebar")
        side_box.set_size_request(268, -1)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "start", "end"):
            getattr(top, f"set_margin_{m}")(12)
        nb = Gtk.Button()
        nb.add_css_class("side-new")
        nbb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        nbb.append(Gtk.Image.new_from_icon_name(_pick_icon("list-add-symbolic", "document-new-symbolic")))
        nbb.append(Gtk.Label(label="New chat", xalign=0, hexpand=True))
        kb = Gtk.Label(label="Ctrl N"); kb.add_css_class("kbd"); nbb.append(kb)
        nb.set_child(nbb)
        nb.connect("clicked", lambda *_: self.new_chat())
        top.append(nb)
        self.chat_search = Gtk.SearchEntry()
        self.chat_search.add_css_class("side-search")
        try:
            self.chat_search.set_placeholder_text("Search chats")
        except Exception:
            pass
        self.chat_search.connect("search-changed", lambda *_: self._refresh_sidebar())
        top.append(self.chat_search)
        side_box.append(top)
        hd = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        for m in ("start", "end"):
            getattr(hd, f"set_margin_{m}")(14)
        sh = Gtk.Label(label="RECENT", xalign=0, hexpand=True)
        sh.add_css_class("side-head")
        self._ttl_note = Gtk.Label(label="", xalign=1)
        self._ttl_note.add_css_class("side-note")
        hd.append(sh); hd.append(self._ttl_note)
        side_box.append(hd)
        self.chat_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("bottom", "start", "end"):
            getattr(self.chat_list, f"set_margin_{m}")(8)
        side_scroll = Gtk.ScrolledWindow(vexpand=True)
        try:
            side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        except Exception:
            pass
        side_scroll.set_child(self.chat_list)
        side_box.append(side_scroll)
        self.sidebar = Gtk.Revealer()
        # hexpand propagates UP from children in GTK4: the expanding chat rows
        # made the whole sidebar claim a share of the window's spare width.
        self.sidebar.set_hexpand(False)
        side_box.set_hexpand(False)
        self.sidebar.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.sidebar.set_child(side_box)
        self.sidebar.set_reveal_child(bool(self.settings.get("sidebar_open", True)))

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(self.sidebar); outer.append(body)
        tv = Adw.ToolbarView(); tv.add_top_bar(header); tv.set_content(outer)
        self.set_content(tv)
        self._narrow = False
        self._add_breakpoints()

        # window-wide shortcuts. Tab flips Agent/Chat from anywhere in the main
        # window — captured before GTK's focus navigation can eat it.
        wk = Gtk.EventControllerKey()
        try:
            wk.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            pass
        wk.connect("key-pressed", self._on_window_key)
        self.add_controller(wk)
        # drag files (or an image) from anywhere onto the window to attach them
        try:
            from gi.repository import GObject
            dt = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
            dt.set_gtypes([Gdk.FileList.__gtype__, Gdk.Texture.__gtype__])
            dt.connect("drop", self._on_drop)
            self.add_controller(dt)
        except Exception:
            pass

    def _toast(self, text):
        try:
            t = Adw.Toast.new(text)
            t.set_timeout(2)
            self.toasts.add_toast(t)
        except Exception:
            pass

    # ── mode: Agent (acts) / Chat (answers) ──────────────────────────────────
    def _system_text(self):
        base = SYSTEM_PROMPT if self.agent_mode else CHAT_PROMPT
        text = base + "\n\nCurrent machine:\n" + gather_context()
        # In the system message, not the per-hop note: rules change rarely, so
        # they belong in the cached prefix rather than re-billed on every hop.
        rules = project_instructions(self.workspace)
        if self.agent_mode and getattr(self, "_mcp", None) is not None:
            tools = self._mcp.index()          # costs nothing when no server is set up
            if tools:
                text += "\n\n" + tools
        return text + ("\n\n" + rules if rules else "")

    def _refresh_system(self):
        """Rebuild history[0] (mode, machine, instructions). Only a real change
        rewrites it, so the API's prompt cache survives an unchanged prefix."""
        h = self.__dict__.get("history")       # may run before __init__ built it (MCP boot)
        if isinstance(h, list) and h and h[0].get("role") == "system":
            new = self._system_text()
            if h[0].get("content") != new:
                h[0]["content"] = new

    def set_agent_mode(self, on, announce=True):
        on = bool(on)
        if on == self.agent_mode and not announce:
            self._apply_mode(announce=False)
            return
        self.agent_mode = on
        self.settings["agent_mode"] = on
        save_settings(self.settings)
        self._apply_mode(announce)

    def toggle_mode(self, *_):
        self.set_agent_mode(not self.agent_mode)

    def _apply_mode(self, announce=True):
        """Reflect the mode everywhere: switch, prompt, placeholder, empty state.

        The system prompt is swapped in place on history[0]. It is the stable
        prefix every request starts with, so this costs one cache miss on the
        next call and nothing after."""
        self._mode_guard = True
        try:
            self.agent_btn.set_active(self.agent_mode)
            self.chat_btn.set_active(not self.agent_mode)
        finally:
            self._mode_guard = False
        if self.history and self.history[0].get("role") == "system":
            self.history[0]["content"] = self._system_text()
        self._sync_placeholder()
        self._update_subtitle()
        try:
            self.plan_btn.set_visible(self.agent_mode)    # planning is for acting
        except Exception:
            pass
        if self._hint is not None:
            self._show_empty_hint()
        if announce:
            self._toast("Agent mode — Chuck can act on your machine" if self.agent_mode
                        else "Chat mode — answers only, nothing runs")

    def _on_mode_btn(self, btn):
        if getattr(self, "_mode_guard", False):
            return
        want = bool(self.agent_btn.get_active())
        if want != self.agent_mode:
            self.set_agent_mode(want)

    def _sync_placeholder(self):
        ph = getattr(self, "_placeholder", None)
        if ph is None:
            return
        if getattr(self, "_attachments", None):
            ph.set_label("Say what to do with the attachment\u2026")
        elif getattr(self, "_running", False):
            ph.set_label("Type to steer \u2014 Chuck reads it at his next step  \u00b7  Esc stops")
        elif self.agent_mode and getattr(self, "plan_mode", False):
            ph.set_label("Plan mode \u2014 describe the task; Chuck proposes a plan before "
                         "changing anything")
        elif self.agent_mode:
            ph.set_label("Ask Chuck to do something\u2026" + (
                f"  (in {self.workspace.name})" if self.workspace else "")
                + "   \u00b7  / commands  \u00b7  @ files")
        else:
            ph.set_label("Ask anything\u2026   \u00b7  / commands")
        try:
            b = self.entry.get_buffer()
            ph.set_visible(b.get_char_count() == 0)
        except Exception:
            pass

    def _update_subtitle(self):
        model = (self.settings.get("model") or DEFAULT_MODEL).split("/")[-1]
        parts = [model, "Agent" if self.agent_mode else "Chat"]
        if self.workspace:
            parts.append(_short_path(self.workspace))
        try:
            self._subtitle.set_label("  ·  ".join(parts))
            self._model_lbl.set_label(model)
        except Exception:
            pass

    # ── working folder ──────────────────────────────────────────────────────
    def set_workspace(self, path, announce=True):
        """Point the session at a folder (or None). Commands run there; read,
        grep, write and edit resolve relative paths inside it; ```project```
        facts are scoped to it."""
        p = None
        if path:
            try:
                p = Path(os.path.expanduser(str(path))).resolve()
            except Exception:
                p = None
            if p is not None and not p.is_dir():
                self._toast(f"Not a folder: {path}")
                return False
        self.workspace = p
        _WORKDIR["path"] = str(p) if p else None
        _SHELL["cwd"] = None                    # a new folder: the shell starts there
        self._refresh_system()                  # that folder's CHUCK.md / AGENTS.md
        if p:
            rec = [r for r in (self.settings.get("recent_workspaces") or [])
                   if isinstance(r, str) and r != str(p)]
            self.settings["recent_workspaces"] = [str(p)] + rec[:7]
            save_settings(self.settings)
        try:
            self._ws_lbl.set_label(p.name if p else "No folder")
            if p:
                self.ws_btn.add_css_class("chip-on")
            else:
                self.ws_btn.remove_css_class("chip-on")
        except Exception:
            pass
        self._sync_placeholder()
        self._update_subtitle()
        if self._hint is not None:
            self._show_empty_hint()
        if announce:
            self._toast(f"Working in {_short_path(p)}" if p else "No working folder")
        return True

    def _workroot(self):
        """Where file tools operate: the working folder — exactly what the chip
        shows. It used to fall back to the project remembered from a PREVIOUS
        session, so with the chip reading "No folder" files were quietly
        written into some old ~/ChuckProjects folder."""
        if self.workspace and self.workspace.is_dir():
            return self.workspace
        return None

    # ── history / saved chats ──
    def _new_history(self):
        self.history = [{"role": "system", "content": self._system_text()}]

    def new_chat(self):
        # Cancel first. Without this the in-flight stream's _finalise appended
        # the OLD question's answer to the NEW history — leaving a chat whose
        # first message is an assistant turn with no user turn before it, which
        # then got sent to the API and written to disk.
        if self._running:
            self.stop_run(auto=True)
        self._save_chat()
        self._clear_sudo_pw()           # a fresh chat re-asks for root; never carry it over
        _SHELL["cwd"] = None                    # and the shell starts in the folder again
        self._py_close()                        # and Python starts empty
        self._compact = None
        self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._new_history()
        self._reset_meter()
        self._clear_msgs()
        self._show_empty_hint()
        self._refresh_sidebar()
        try:
            self.entry.grab_focus()
        except Exception:
            pass

    def _show_empty_hint(self):
        """The empty screen: a mark, one line on what this mode does, and four
        one-click starters. It lives in its own stack page rather than in the
        transcript, so no note or card can ever push it around."""
        holder = self._empty_holder
        old = getattr(self, "_hint_widget", None)
        if old is not None:
            try:
                holder.remove_overlay(old)
            except Exception:
                pass
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_vexpand(True)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        for m in ("start", "end"):
            getattr(box, f"set_margin_{m}")(24)
        mk = Gtk.Label(label="C")
        mk.add_css_class("mark"); mk.add_css_class("mark-lg")
        mk.set_halign(Gtk.Align.CENTER)
        box.append(mk)
        title = Gtk.Label(label="What are we working on?" if self.agent_mode
                          else "Ask me anything", xalign=0.5)
        title.add_css_class("empty-title"); title.set_margin_top(22)
        box.append(title)
        if self.agent_mode:
            where = (f" in {_short_path(self.workspace)}" if self.workspace
                     else " — pick a working folder for project work")
            sub = "Agent mode · runs commands, reads and edits files" + where
        else:
            sub = "Chat mode · answers only — nothing runs on your machine"
        sl = Gtk.Label(label=sub, xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER)
        sl.add_css_class("empty-sub"); sl.set_margin_top(8)
        sl.set_max_width_chars(64)
        box.append(sl)

        if not self.backend.key():
            kr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            kr.add_css_class("ws-current")
            kr.set_margin_top(22); kr.set_halign(Gtk.Align.CENTER)
            kl = Gtk.Label(label="Add your SiliconFlow API key to get started.", xalign=0)
            kl.add_css_class("dlg-sub")
            kb = Gtk.Button(label="Open Settings"); kb.add_css_class("accent-btn")
            kb.connect("clicked", self.open_settings)
            kr.append(kl); kr.append(kb)
            box.append(kr)

        # a FlowBox, not a fixed 2-column grid: in a narrow window the cards
        # wrap to one column instead of being clipped
        grid = Gtk.FlowBox()
        grid.add_css_class("suggest-flow")
        grid.set_margin_top(28); grid.set_halign(Gtk.Align.CENTER)
        try:
            grid.set_selection_mode(Gtk.SelectionMode.NONE)
            grid.set_max_children_per_line(2); grid.set_min_children_per_line(1)
            grid.set_column_spacing(10); grid.set_row_spacing(10)
            grid.set_homogeneous(True)
        except Exception:
            pass
        items = self.AGENT_SUGGEST if self.agent_mode else self.CHAT_SUGGEST
        for i, (t, d, prompt) in enumerate(items):
            b = Gtk.Button()
            b.add_css_class("suggest")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            a = Gtk.Label(label=t, xalign=0); a.add_css_class("suggest-title")
            s2 = Gtk.Label(label=d, xalign=0); s2.add_css_class("suggest-sub")
            try:
                s2.set_ellipsize(Pango.EllipsizeMode.END)
            except Exception:
                pass
            v.append(a); v.append(s2)
            b.set_child(v)
            b.set_size_request(250, -1)
            if self.agent_mode and i == 3 and not self.workspace:
                b.connect("clicked", self.open_workspace)
            else:
                b.connect("clicked", lambda _b, p=prompt: self._prefill(p))
            grid.append(b)
        box.append(grid)
        keys = Gtk.Label(label=("Tab  switch Agent / Chat     Ctrl+O  working folder     "
                                "/  commands     @  files" if self.agent_mode else
                                "Tab  switch Agent / Chat     /  commands     "
                                "Shift+Enter  new line"), xalign=0.5)
        keys.add_css_class("empty-keys"); keys.set_margin_top(26)
        keys.set_wrap(True); keys.set_justify(Gtk.Justification.CENTER)
        box.append(keys)
        for m in ("top", "bottom"):
            getattr(box, f"set_margin_{m}")(24)
        # in a scroller: a short window scrolls the welcome instead of cutting
        # off its top and sliding the shortcut line under the composer
        sw = Gtk.ScrolledWindow()
        try:
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        except Exception:
            pass
        sw.set_child(box)
        holder.add_overlay(sw)
        self._hint_widget = sw
        self._hint = box
        try:
            self.view_stack.set_visible_child_name("empty")
        except Exception:
            pass

    def _prefill(self, text):
        """Drop a suggestion into the composer and focus it — deliberately NOT
        sending, so the first thing Chuck does is still the user's decision."""
        try:
            buf = self.entry.get_buffer()
            buf.set_text(text, -1)
            self.entry.grab_focus()
            buf.place_cursor(buf.get_end_iter())
        except Exception:
            pass

    # ── windowed transcript ────────────────────────────────────────────────
    # The transcript lives as a render log of plain data and only a window of
    # it is materialised: the newest RENDER_KEEP while you're at the bottom,
    # growing by RENDER_PAGE each time you scroll to the top, and shrinking
    # again when you return to the bottom. Text, notes and images are rebuilt
    # from their data on demand; interactive cards are pinned — kept alive,
    # because their run state can't be recreated — and shown in their real
    # place in the conversation when the window reaches them.
    #
    # The box is RECONCILED, not rebuilt: widgets already in place stay put and
    # only the difference is inserted or removed. Tearing every child out and
    # re-adding it on each new message is what made the transcript flicker and
    # jump while a reply was being written.

    def _in_log(self, entry):
        return entry is not None and any(e is entry for e in self._log)

    def _log_add(self, kind, data):
        """Record a rebuildable entry and materialise it at the bottom."""
        self._drop_hint()
        entry = {"kind": kind, "data": data, "w": None, "pinned": False}
        self._log.append(entry)
        self._collapse_window(scroll=True)
        return entry

    def _log_pin(self, widget):
        """Record an interactive widget that must never be torn down. It is
        machine work, so it goes in the work lane."""
        self._drop_hint()
        entry = {"kind": "pinned", "data": None, "w": _lane(widget), "pinned": True}
        self._log.append(entry)
        self._collapse_window(scroll=True)
        return entry

    def _build_entry(self, e):
        k, d = e["kind"], e["data"] or {}
        if k == "user":
            w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.END, hexpand=True,
                        spacing=6)
            w.add_css_class("turn-row"); w.add_css_class("user-row")
            imgs = [p for p in (d.get("images") or []) if p and os.path.isfile(p)]
            if imgs:
                ib = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                             halign=Gtk.Align.END)
                for pth in imgs[:6]:
                    try:
                        pic, _pb = _pic_from_file(pth, 180, 140)
                        fr = Gtk.Button(); fr.add_css_class("thumb-btn"); fr.set_child(pic)
                        fr.set_tooltip_text(os.path.basename(pth))
                        fr.connect("clicked", lambda _b, q=pth: _open_path(q))
                        ib.append(fr)
                    except Exception:
                        continue
                w.append(ib)
            files = d.get("files") or []
            if files:
                fb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                             halign=Gtk.Align.END)
                for nm in files[:8]:
                    chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                    chip.add_css_class("att-chip")
                    chip.append(Gtk.Image.new_from_icon_name(_pick_icon(
                        "text-x-generic-symbolic", "document-open-symbolic")))
                    nl = Gtk.Label(label=nm); nl.add_css_class("att-name")
                    chip.append(nl)
                    fb.append(chip)
                w.append(fb)
            txt = d.get("text", "")
            lbl = Gtk.Label(label=txt + ("  \U0001F4F7" if d.get("shot") else ""),
                            xalign=0, wrap=True, selectable=True)
            _wrap_anywhere(lbl)
            lbl.set_max_width_chars(64)
            _apply_line_height(lbl, 1.4)
            card = Gtk.Box(); card.add_css_class("user-bubble"); card.append(lbl)
            w.append(card)
            return w
        if k == "bot":
            return self._build_bot(e)
        if k == "note":
            return _lane(self._build_note(d))
        if k == "plan":
            return _lane(self._build_plan(d.get("items") or [], e))
        if k == "image":
            return _lane(self._build_image_widget(d.get("path"), d.get("src")))
        if k == "fold":
            return _lane(self._build_fold(d))
        return Gtk.Label(label="")

    def _build_fold(self, d):
        """Tool results from a reopened chat: folded, one click to read them.
        (The live cards aren't kept, but what the machine did is.)"""
        text = d.get("text", "")
        n = text.count("\n") + 1
        exp = Gtk.Expander()
        title = Gtk.Label(label=f"{d.get('title', 'Tool results')} \u00b7 {n} line{'' if n == 1 else 's'}",
                          xalign=0)
        try:
            title.set_ellipsize(Pango.EllipsizeMode.END)   # a long title never widens the page
        except Exception:
            pass
        exp.set_label_widget(title)
        exp.add_css_class("think")
        exp.set_child(self._term_panel(text, None, title="RESULTS"))
        return exp

    def _build_note(self, d):
        # A system aside ("added --noconfirm"). Small and muted so it reads
        # as machine chatter and never competes with the reply.
        l = Gtk.Label(label=d.get("text", ""), xalign=0, wrap=True, hexpand=True,
                      selectable=d.get("css") == "mono")
        _wrap_anywhere(l)
        l.add_css_class(d.get("css", "dim"))
        _apply_line_height(l, 1.3)
        wrap = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        wrap.add_css_class("sys-note")
        wrap.append(l)
        return wrap

    def _build_bot(self, e):
        d = e["data"]
        cont = bool(d.get("cont"))
        w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True)
        w.add_css_class("turn-row")
        # A later hop of the same turn gets no second byline. Its prose still
        # sits in the main column — only machine work goes on the rail, so
        # what Chuck SAID and what the machine DID never share a lane.
        w.add_css_class("bot-cont" if cont else "bot-row")
        by = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        av = Gtk.Label(label="C"); av.add_css_class("avatar")
        av.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label="Chuck", xalign=0); name.add_css_class("byline")
        by.append(av); by.append(name)
        meta = Gtk.Label(label=d.get("meta", ""), xalign=0)
        meta.add_css_class("byline-meta")
        by.append(meta)
        e["meta_w"] = meta
        if not cont:
            w.append(by)
        # reasoning, folded away: there if you want it, never in the way
        exp = Gtk.Expander(label="")
        exp.add_css_class("think")
        tl = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        tl.add_css_class("think-body"); _wrap_anywhere(tl)
        tl.set_margin_top(6)
        exp.set_child(tl)
        exp.set_visible(False)
        w.append(exp)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        content.add_css_class("bot-bubble")
        w.append(content)
        e["label"] = content
        e["think_w"] = (exp, tl)
        e["segs"] = []
        self._render_bot(e)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row.add_css_class("msg-tools")
        play = Gtk.Button(icon_name=_pick_icon("media-playback-start-symbolic",
                                               "audio-volume-high-symbolic",
                                               "media-playback-start"))
        play.add_css_class("playbtn")
        play.set_tooltip_text("Read this aloud")
        play.connect("clicked", lambda _b, ent=e: self._speak_entry(ent))
        row.append(play)
        copy = self._copy_button(
            lambda ent=e: (ent.get("data") or {}).get("text", ""),
            "Copy this reply")
        copy.add_css_class("playbtn")
        row.append(copy)
        w.append(row)
        e["tools_w"] = row
        row.set_visible(bool((d.get("text") or "").strip()))
        last = next((x for x in reversed(self._log) if x.get("kind") == "bot"), None)
        if last is e:
            prev = getattr(self, "_latest_tools", None)
            if prev is not None and prev is not row:
                try:
                    prev.remove_css_class("latest")
                except Exception:
                    pass
            row.add_css_class("latest")
            self._latest_tools = row
        return w

    def _render_bot(self, e):
        """Draw a reply's text into its content box as segments — prose, code
        blocks, tables — updating widgets in place where the segment kind is
        unchanged. That is what lets markdown render LIVE while the reply
        streams without rebuilding (and re-flowing) the whole message twenty
        times a second."""
        content = e.get("label")
        if content is None:
            return
        d = e["data"]
        txt = d.get("text", "") or ""
        try:
            content.set_visible(bool(txt.strip()))   # no empty prose box leaving a gap
            if e.get("tools_w") is not None:          # nothing to read aloud or copy
                e["tools_w"].set_visible(bool(txt.strip()))
        except Exception:
            pass
        segs = md_segments(txt) if d.get("rich") else [("plain", txt)]
        if not segs:
            segs = [("plain", "")]
        have = e.setdefault("segs", [])
        for i, sg in enumerate(segs):
            kind = sg[0]
            if i < len(have) and have[i][0] == kind:
                self._update_seg(have[i], sg)
                continue
            # kind changed (or new): drop this and everything after, rebuild
            for old in have[i:]:
                try:
                    content.remove(old[1])
                except Exception:
                    pass
            del have[i:]
            have.append(self._make_seg(sg))
            content.append(have[-1][1])
        for old in have[len(segs):]:
            try:
                content.remove(old[1])
            except Exception:
                pass
        del have[len(segs):]
        tw = e.get("think_w")
        if tw is not None:
            exp, tl = tw
            th = d.get("think") or ""
            if th:
                secs = d.get("think_secs") or 0
                exp.set_label(f"Thought for {secs}s" if secs else "Reasoning")
                tl.set_text(th[-12000:])
            exp.set_visible(bool(th))

    def _make_seg(self, sg):
        kind = sg[0]
        if kind == "code":
            box, lang_l, body_l = self._code_block(sg[1], sg[2])
            return ["code", box, (lang_l, body_l), sg[2]]
        if kind == "table":
            w = self._table_widget(sg[1])
            return ["table", w, None, repr(sg[1])]
        lbl = Gtk.Label(label="", xalign=0, wrap=True, selectable=True, hexpand=True)
        lbl.add_css_class("prose")
        _wrap_anywhere(lbl)
        lbl.set_max_width_chars(0)
        rec = [kind, lbl, None, None]
        self._update_seg(rec, sg)
        return rec

    def _update_seg(self, rec, sg):
        kind = sg[0]
        if kind == "code":
            if rec[3] != sg[2]:
                rec[2][1].set_text(sg[2])
                rec[3] = sg[2]
            rec[2][0].set_label((sg[1] or "code").upper())
        elif kind == "table":
            key = repr(sg[1])
            if rec[3] != key:
                parent = rec[1].get_parent()
                new = self._table_widget(sg[1])
                if parent is not None:
                    try:
                        parent.insert_child_after(new, rec[1])
                        parent.remove(rec[1])
                    except Exception:
                        pass
                rec[1], rec[3] = new, key
        elif kind == "md":
            if rec[3] != sg[1]:
                set_rich(rec[1], sg[1].strip("\n"))
                rec[3] = sg[1]
        else:
            if rec[3] != sg[1]:
                rec[1].set_text(sg[1])
                _apply_line_height(rec[1])
                rec[3] = sg[1]

    def _code_block(self, lang, body):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("code-block")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.add_css_class("code-head")
        ll = Gtk.Label(label=(lang or "code").upper(), xalign=0, hexpand=True)
        ll.add_css_class("code-lang")
        head.append(ll)
        bl = Gtk.Label(label=body, xalign=0, selectable=True, wrap=False)
        bl.add_css_class("code-body")
        bl.set_valign(Gtk.Align.START)
        head.append(self._copy_button(lambda: bl.get_text(), "Copy code"))
        box.append(head)
        sw = Gtk.ScrolledWindow()
        sw.add_css_class("code-scroll")
        try:
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            sw.set_propagate_natural_height(True)
        except Exception:
            pass
        sw.set_child(bl)
        self._chain_scroll(sw)
        box.append(sw)
        return box, ll, bl

    def _table_widget(self, rows):
        grid = Gtk.Grid()
        grid.add_css_class("md-table")
        ncol = max((len(r) for r in rows), default=0)
        for ri, r in enumerate(rows):
            for ci in range(ncol):
                cell = r[ci] if ci < len(r) else ""
                lb = Gtk.Label(xalign=0, wrap=True, hexpand=True)
                _wrap_anywhere(lb)
                lb.set_selectable(True)
                try:
                    mk = _md_inline(_html.escape(cell, quote=False))
                    if _markup_ok(mk):
                        lb.set_markup(mk)
                    else:
                        lb.set_text(cell)
                except Exception:
                    lb.set_text(cell)
                lb.add_css_class("md-th" if ri == 0 else "md-td")
                grid.attach(lb, ci, ri, 1, 1)
        # No scroller: wrapping cells inside a sideways scroller are measured at
        # their narrowest (one word per line), which inflated a two-row table
        # into a 500px empty box. Cells wrap inside the column instead.
        grid.set_hexpand(True)
        return grid

    def _chain_scroll(self, inner, vertical=False):
        """Make a nested scroller behave like one in a browser.

        Without this, the mouse wheel over a code block or an output panel was
        captured by the inner scroller — and GTK turns a vertical wheel into
        horizontal scrolling when only sideways is possible — so the page
        simply stopped moving under the pointer. Vertical motion now goes to
        the page unless the inner panel can itself still scroll that way;
        sideways motion stays with the inner panel."""
        try:
            ctl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
            ctl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            return

        def on_scroll(c, dx, dy):
            if abs(dx) > abs(dy) or dy == 0:
                return False
            if vertical:
                va = inner.get_vadjustment()
                room = va.get_upper() - va.get_page_size()
                if room > 1:
                    v = va.get_value()
                    if (dy < 0 and v > 0.5) or (dy > 0 and v < room - 0.5):
                        return False          # inner panel scrolls first
            oa = self.scroller.get_vadjustment()
            try:
                wheel = c.get_unit() == Gdk.ScrollUnit.WHEEL
            except Exception:
                wheel = True
            step = dy * (oa.get_page_size() ** (2.0 / 3.0)) if wheel else dy
            top = max(0.0, oa.get_upper() - oa.get_page_size())
            oa.set_value(max(0.0, min(top, oa.get_value() + step)))
            return True
        ctl.connect("scroll", on_scroll)
        inner.add_controller(ctl)

    def _speak_entry(self, entry):
        """Speak one specific message, replacing whatever is being read."""
        txt = (entry.get("data") or {}).get("text", "")
        if txt.strip():
            speak(txt, self.settings)

    def _window_bounds(self):
        keep = self.cfg("render_keep", RENDER_KEEP, 4, 200)
        return max(0, len(self._log) - keep)

    def _collapse_window(self, scroll=False):
        """Drop back to the newest slice — called on new output and when the
        user returns to the bottom."""
        self._win_start = self._window_bounds()
        self._apply_window()
        if scroll:
            self._scroll_down()

    def _apply_window(self, keep_offset=False):
        """Make msgbox show exactly the current window. Widgets that fall out of
        it lose their last reference here, so GTK frees them."""
        if keep_offset:
            try:
                a = self.scroller.get_vadjustment()
                self._anchor = (float(a.get_upper()), float(a.get_value()))
                GLib.timeout_add(600, self._clear_anchor)
            except Exception:
                self._anchor = None
        lo = self._win_start
        show = []
        if lo > 0:
            show.append(self._older_banner())
        for i, e in enumerate(self._log):
            if i < lo:
                if not e["pinned"]:
                    e["w"] = None                  # freed
                    e.pop("label", None); e.pop("segs", None); e.pop("think_w", None)
                continue
            if e["w"] is None:
                e["w"] = self._build_entry(e)
            show.append(e["w"])
        self._reconcile(show)
        if self._log:
            try:
                self.view_stack.set_visible_child_name("chat")
            except Exception:
                pass

    def _reconcile(self, desired):
        box = self.msgbox
        want = {id(w) for w in desired}
        c = box.get_first_child()
        while c is not None:
            n = c.get_next_sibling()
            if id(c) not in want:
                box.remove(c)
            c = n
        prev = None
        for w in desired:
            if w.get_parent() is box:
                if w.get_prev_sibling() is not prev:
                    box.reorder_child_after(w, prev)
            else:
                box.insert_child_after(w, prev)
            prev = w

    def _clear_anchor(self):
        self._anchor = None
        return False

    def _older_banner(self):
        n = self._win_start
        if self._banner is None:
            self._banner = Gtk.Label(label="", xalign=0.5)
            self._banner.add_css_class("older-note")
        if self._loading_older:
            self._banner.set_label("loading earlier messages…")
        else:
            self._banner.set_label(f"↑ {n} earlier message{'s' if n != 1 else ''}")
        return self._banner

    # ── scrolling ───────────────────────────────────────────────────────────
    def _on_extent(self, adj, *_):
        """Content height or viewport height changed (a token arrived, a card
        landed, the window was resized). Re-pin to the bottom if we are
        following, or hold the reading position steady after older messages
        were inserted above it.

        This fires DURING GTK's layout pass, and a value set there is dropped —
        the adjustment says "bottom" while the viewport stays where it was, and
        the last line of a reply sits hidden under the composer. So the move is
        applied once layout has finished (idle runs after the frame's layout),
        coalesced to one per frame however many times the height changed."""
        if getattr(self, "_pin_pending", False):
            return
        self._pin_pending = True

        def pin():
            self._pin_pending = False
            try:
                a = self.scroller.get_vadjustment()
                if self._anchor is not None:
                    old_upper, old_value = self._anchor
                    self._anchor = None
                    a.set_value(max(0.0, a.get_upper() - old_upper + old_value))
                elif self._stick:
                    a.set_value(max(0.0, a.get_upper() - a.get_page_size()))
            except Exception:
                pass
            return False
        GLib.idle_add(pin)

    def _on_scroll(self, adj, *_):
        try:
            v, upper, page = adj.get_value(), adj.get_upper(), adj.get_page_size()
            dist = upper - (v + page)
        except Exception:
            return
        self._stick = dist <= 48
        self._sync_jump()
        if not self._log:
            return
        if v <= 40 and self._win_start > 0 and not self._loading_older:
            self._loading_older = True
            self._older_banner()                      # "loading…"
            GLib.timeout_add(180, self._load_older)
        elif dist <= 40 and self._win_start != self._window_bounds():
            self._collapse_window()                    # back at the bottom: offload

    def _sync_jump(self):
        jb = getattr(self, "jump_btn", None)
        if jb is not None:
            try:
                jb.set_visible(not self._stick and bool(self._log))
            except Exception:
                pass

    def _load_older(self):
        page = self.cfg("render_page", RENDER_PAGE, 5, 200)
        self._win_start = max(0, self._win_start - page)
        self._loading_older = False
        self._apply_window(keep_offset=True)
        return False

    def _drop_hint(self):
        if self._hint is not None:
            self._hint = None
            try:
                self.view_stack.set_visible_child_name("chat")
            except Exception:
                pass

    def _clear_msgs(self):
        self._hint = None
        self._log = []
        self._win_start = 0
        self._loading_older = False
        self._bot_entry = None
        self._bot_label = None
        self._plan_entry = None
        self._seen_queries = []     # queries already run this turn
        self._seen_urls = set()     # pages already read this turn
        self._dead_urls = set()     # pages that failed — don't retry
        self._forced_answer = False
        self._stick = True
        self._anchor = None
        self._banner = None
        c = self.msgbox.get_first_child()
        while c:
            n = c.get_next_sibling(); self.msgbox.remove(c); c = n
        self._sync_jump()

    # ── dialogs: shared frame ───────────────────────────────────────────────
    def _dialog(self, title, width=560, height=560):
        """A modal window with a flat header, scrolling content, dark like the
        rest. Returns (dialog, content_box)."""
        dlg = Adw.Window(transient_for=self, modal=True, title=title,
                         default_width=width)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m, v in (("top", 4), ("bottom", 24), ("start", 24), ("end", 24)):
            getattr(box, f"set_margin_{m}")(v)
        hb = Adw.HeaderBar()
        try:
            hb.set_show_title(False)        # the page carries its own heading
        except Exception:
            hb.set_title_widget(Gtk.Box())
        wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb)
        sw = Gtk.ScrolledWindow(vexpand=True)
        try:
            sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            # size to the content, up to a ceiling, instead of a fixed height
            # that left half the dialog empty
            sw.set_propagate_natural_height(True)
            sw.set_max_content_height(height)
        except Exception:
            pass
        clamp = Adw.Clamp()
        try:
            clamp.set_maximum_size(width - 40)
        except Exception:
            pass
        clamp.set_child(box)
        sw.set_child(clamp)
        wrap.set_content(sw)
        dlg.set_content(wrap)
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed",
                   lambda _c, kv, _k, _s: (dlg.close(), True)[1] if kv == Gdk.KEY_Escape else False)
        dlg.add_controller(kc)
        return dlg, box

    @staticmethod
    def _heading(box, title, sub=None):
        t = Gtk.Label(label=title, xalign=0); t.add_css_class("dlg-title")
        box.append(t)
        if sub:
            s = Gtk.Label(label=sub, xalign=0, wrap=True); s.add_css_class("dlg-sub")
            box.append(s)

    @staticmethod
    def _section(box, title):
        lb = Gtk.Label(label=title.upper(), xalign=0)
        lb.add_css_class("set-section"); lb.set_margin_top(10)
        box.append(lb)

    # ── working-folder dialog ───────────────────────────────────────────────
    def open_workspace(self, *_):
        """Choose the session's working folder — or run with none."""
        dlg, box = self._dialog("Working folder", 560, 540)
        self._heading(box, "Working folder",
                      "Commands run here, and Chuck reads, searches, writes and edits files "
                      "relative to it. Leave it empty for questions and system tasks.")

        cur = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cur.add_css_class("ws-current")
        cur.append(Gtk.Image.new_from_icon_name(_pick_icon("folder-symbolic", "folder")))
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        cl = Gtk.Label(label="CURRENT", xalign=0); cl.add_css_class("set-section")
        cp = Gtk.Label(label=_short_path(self.workspace) if self.workspace else "No folder",
                       xalign=0, wrap=True)
        cp.add_css_class("ws-path"); _wrap_anywhere(cp)
        if self.workspace:
            cp.set_tooltip_text(str(self.workspace))
        col.append(cl); col.append(cp)
        cur.append(col)
        box.append(cur)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        choose = Gtk.Button(label="Choose folder…"); choose.add_css_class("accent-btn")
        none = Gtk.Button(label="Use no folder"); none.add_css_class("quick")
        row.append(choose); row.append(none)
        if self.workspace:
            ob = Gtk.Button(label="Open in file manager"); ob.add_css_class("quick")
            ob.connect("clicked", lambda *_: _open_path(str(self.workspace)))
            row.append(ob)
        box.append(row)

        def use(path):
            if self.set_workspace(path):
                dlg.close()

        def picked(dialog, res):
            try:
                f = dialog.select_folder_finish(res)
            except Exception:
                return
            if f is not None and f.get_path():
                use(f.get_path())

        def do_choose(*_a):
            try:
                fd = Gtk.FileDialog()
                fd.set_title("Choose a working folder")
                if self.workspace:
                    fd.set_initial_folder(Gio.File.new_for_path(str(self.workspace)))
                fd.select_folder(dlg, None, picked)
            except Exception:
                # GTK < 4.10: the older chooser
                try:
                    nat = Gtk.FileChooserNative.new("Choose a working folder", dlg,
                                                    Gtk.FileChooserAction.SELECT_FOLDER,
                                                    "Select", "Cancel")

                    def resp(n, r):
                        if r == Gtk.ResponseType.ACCEPT and n.get_file():
                            use(n.get_file().get_path())
                    nat.connect("response", resp)
                    nat.show()
                    dlg._nat = nat
                except Exception as ex:
                    self._toast(f"Couldn't open a folder picker: {ex}")
        choose.connect("clicked", do_choose)
        none.connect("clicked", lambda *_: use(None))

        recent = [r for r in (self.settings.get("recent_workspaces") or [])
                  if isinstance(r, str) and os.path.isdir(r)]
        proj = []
        if _builder:
            try:
                proj = [str(_builder.PROJECTS / n) for n, _c in _builder.project_list()][:6]
            except Exception:
                proj = []
        for title, paths in (("Recent", recent), ("Chuck's projects", proj)):
            paths = [p for p in paths if not self.workspace or p != str(self.workspace)]
            if not paths:
                continue
            self._section(box, title)
            lb = Gtk.ListBox()
            lb.add_css_class("boxed-list")
            try:
                lb.set_selection_mode(Gtk.SelectionMode.NONE)
            except Exception:
                pass
            for pth in paths[:8]:
                r = Adw.ActionRow()
                r.set_title(_html.escape(Path(pth).name or pth))
                r.set_subtitle(_html.escape(_short_path(pth)))
                r.set_activatable(True)
                r.add_prefix(Gtk.Image.new_from_icon_name(_pick_icon("folder-symbolic", "folder")))
                r.connect("activated", lambda _r, p=pth: use(p))
                lb.append(r)
            box.append(lb)
        dlg.present()

    # ── memory viewer ───────────────────────────────────────────────────────
    def open_memory(self, *_):
        """What Chuck remembers and what skills he has saved — searchable,
        addable, deletable. Memory you can't inspect is memory you can't trust."""
        dlg, box = self._dialog("Memory", 620, 640)
        n = _memory.count() if _memory else 0
        self._heading(box, "Memory",
                      f"{n} fact{'s' if n != 1 else ''} stored. Only the few relevant to a "
                      "question are sent with it — never the whole store. Project facts "
                      "are only recalled while their folder is open.")
        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add = Gtk.Entry(hexpand=True)
        try:
            add.set_placeholder_text("Add a fact, e.g. “User runs CachyOS on a ThinkPad”")
        except Exception:
            pass
        addb = Gtk.Button(label="Remember"); addb.add_css_class("accent-btn")
        add_row.append(add); add_row.append(addb)
        box.append(add_row)
        search = Gtk.SearchEntry()
        try:
            search.set_placeholder_text("Filter")
        except Exception:
            pass
        box.append(search)
        lb = Gtk.ListBox()
        lb.add_css_class("boxed-list")
        try:
            lb.set_selection_mode(Gtk.SelectionMode.NONE)
        except Exception:
            pass
        box.append(lb)

        def fill():
            c = lb.get_first_child()
            while c:
                nx = c.get_next_sibling(); lb.remove(c); c = nx
            q = (search.get_text() or "").strip().lower()
            recs = _memory.all_records() if _memory else []
            shown = 0
            for f in recs:
                if q and q not in f["text"].lower():
                    continue
                shown += 1
                r = Adw.ActionRow()
                r.set_title(_html.escape(f["text"]))
                try:
                    r.set_title_lines(3)
                except Exception:
                    pass
                sc = f.get("scope") or ""
                bits = ["core" if f.get("kind") == "core" else "fact",
                        ("project · " + Path(sc).name) if sc else "global"]
                when = f.get("ts")
                if when:
                    bits.append(datetime.fromtimestamp(when).strftime("%d %b %Y"))
                r.set_subtitle(_html.escape("  ·  ".join(bits)))
                rm = Gtk.Button(icon_name=_pick_icon("user-trash-symbolic", "edit-delete-symbolic"))
                rm.add_css_class("ghost"); rm.set_valign(Gtk.Align.CENTER)
                rm.set_tooltip_text("Forget this")

                def drop(_b, t=f["text"]):
                    _memory.forget(t)
                    fill()
                rm.connect("clicked", drop)
                r.add_suffix(rm)
                lb.append(r)
            if not shown:
                r = Adw.ActionRow()
                r.set_title("Nothing here yet" if not q else "No match")
                r.set_subtitle("Tell Chuck durable things — your setup, your preferences "
                               "— and he keeps them.")
                lb.append(r)

        def do_add(*_a):
            t = add.get_text().strip()
            if t and _memory:
                scope = ""
                if t.lower().startswith("project:") and self.workspace:
                    t, scope = t[8:].strip(), str(self.workspace)
                _memory.remember(t, scope=scope)
                add.set_text("")
                fill()
        addb.connect("clicked", do_add)
        add.connect("activate", do_add)
        search.connect("search-changed", lambda *_a: fill())
        fill()

        self._section(box, "Saved skills")
        sl = Gtk.ListBox()
        sl.add_css_class("boxed-list")
        try:
            sl.set_selection_mode(Gtk.SelectionMode.NONE)
        except Exception:
            pass
        skills = _skills.skill_list() if _skills else []
        if not skills:
            r = Adw.ActionRow(); r.set_title("No skills saved yet")
            sl.append(r)
        for name, lang, desc in skills:
            r = Adw.ActionRow()
            r.set_title(_html.escape(name))
            r.set_subtitle(_html.escape(f"{lang} · {desc}" if desc else lang))
            rb = Gtk.Button(icon_name=_pick_icon("user-trash-symbolic", "edit-delete-symbolic"))
            rb.add_css_class("ghost"); rb.set_valign(Gtk.Align.CENTER)
            rb.set_tooltip_text("Archive this skill")

            def drop_skill(_b, nm=name, row=r):
                if _skills:
                    _skills.skill_forget(nm)
                try:
                    sl.remove(row)
                except Exception:
                    pass
            rb.connect("clicked", drop_skill)
            r.add_suffix(rb)
            sl.append(r)
        box.append(sl)
        dlg.present()

    def _save_chat(self):
        msgs = [m for m in self.history if m["role"] != "system" and isinstance(m.get("content"), str)]
        if not msgs:
            return
        title = next((m["content"][:60] for m in msgs if m["role"] == "user"), "chat")
        try:
            (CHATS_DIR / f"{self.chat_id}.json").write_text(json.dumps(
                {"title": title, "ts": self.chat_id, "history": self.history,
                 "workspace": str(self.workspace) if self.workspace else "",
                 "usage": self._usage, "compact": self._compact}, indent=2))
        except Exception:
            pass
        self._refresh_sidebar()

    def _on_tts_toggled(self, btn):
        on = btn.get_active()
        if not on:
            stop_speaking()          # switching voice off shuts it up immediately
        self.settings["tts"] = on
        save_settings(self.settings)

    def _add_breakpoints(self):
        """Responsive layout: below 900px the chat list folds away so the
        conversation and the composer keep their width (the toggle still opens
        it), and your saved preference is left alone."""
        if not (hasattr(Adw, "Breakpoint") and hasattr(self, "add_breakpoint")):
            return
        try:
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
            bp.add_setter(self.side_btn, "active", False)
            bp.add_setter(self.sidebar, "reveal-child", False)

            def narrow(on):
                self._narrow = on
            bp.connect("apply", lambda *_a: narrow(True))
            bp.connect("unapply", lambda *_a: narrow(False))
            self.add_breakpoint(bp)
            # very narrow: secondary controls step aside (memory: /memory,
            # voice: Settings, screen capture: Ctrl+V a screenshot)
            tiny = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 680sp"))
            tiny.add_setter(self.side_btn, "active", False)
            tiny.add_setter(self.sidebar, "reveal-child", False)
            for wdg in (self.mem_btn, self.tts_btn, self.cam_btn):
                tiny.add_setter(wdg, "visible", False)
            tiny.connect("apply", lambda *_a: narrow(True))
            tiny.connect("unapply", lambda *_a: narrow(self.get_width() <= 900))
            self.add_breakpoint(tiny)
        except Exception:
            pass

    def _toggle_sidebar(self, btn):
        on = btn.get_active()
        self.sidebar.set_reveal_child(on)
        if not getattr(self, "_narrow", False):   # a narrow window's auto-collapse isn't a choice
            self.settings["sidebar_open"] = on
            save_settings(self.settings)
        if on:
            self._refresh_sidebar()

    def _sweep_chats(self):
        """Runs every 10 min: drop anything past its life, refresh the list
        (so the 'time left' countdowns stay honest). Keeps running forever."""
        try:
            purge_old_chats(self.cfg('chat_ttl_hours', CHAT_TTL_HOURS, 1, 720))
            self._refresh_sidebar()
        except Exception:
            pass
        return True

    def _refresh_sidebar(self):
        """Rebuild the saved-chat list. Cheap — a handful of stat() calls."""
        ttl = self.cfg('chat_ttl_hours', CHAT_TTL_HOURS, 1, 720)
        note = getattr(self, "_ttl_note", None)
        if note is not None:
            try:
                note.set_label(f"kept {ttl}h" if ttl < 48 else f"kept {ttl // 24}d")
            except Exception:
                pass
        if not hasattr(self, "chat_list"):
            return
        c = self.chat_list.get_first_child()
        while c:
            n = c.get_next_sibling(); self.chat_list.remove(c); c = n
        q = ""
        try:
            q = (self.chat_search.get_text() or "").strip().lower()
        except Exception:
            q = ""
        files = chat_files()
        shown = 0
        for f in files:
            try:
                meta = json.loads(f.read_text())
            except Exception:
                continue
            title = (meta.get("title") or "chat").strip().replace("\n", " ")
            if q:
                hay = title.lower() + " " + " ".join(
                    m.get("content", "") for m in meta.get("history", [])
                    if isinstance(m.get("content"), str) and m.get("role") != "system").lower()
                if q not in hay:
                    continue
            shown += 1
            rowb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            rowb.add_css_class("chat-row")
            open_b = Gtk.Button(hexpand=True)
            open_b.add_css_class("chat-open")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            tl = Gtk.Label(label=title or "chat", xalign=0)
            try:
                tl.set_ellipsize(Pango.EllipsizeMode.END)
                tl.set_max_width_chars(28)
            except Exception:
                pass
            tl.add_css_class("chat-title")
            ml = Gtk.Label(label=_ago(f) + "  ·  " + chat_expires_in(f, ttl), xalign=0)
            ml.add_css_class("chat-meta")
            inner.append(tl); inner.append(ml)
            open_b.set_child(inner)
            if str(f.stem) == str(self.chat_id):
                rowb.add_css_class("chat-current")
            open_b.connect("clicked", lambda _b, path=f: self._load_chat(path))
            rowb.append(open_b)
            dl = Gtk.Button(icon_name=_pick_icon("user-trash-symbolic", "edit-delete-symbolic"))
            dl.add_css_class("ghost"); dl.add_css_class("chat-del")
            dl.set_valign(Gtk.Align.CENTER)
            dl.set_tooltip_text("Delete chat")
            dl.connect("clicked", lambda _b, path=f: self._delete_chat(path))
            rowb.append(dl)
            self.chat_list.append(rowb)
        if not shown:
            empty = Gtk.Label(label="No matching chats." if q else "No saved chats yet.",
                              xalign=0, wrap=True)
            empty.add_css_class("side-note")
            empty.set_margin_start(8); empty.set_margin_top(6)
            self.chat_list.append(empty)

    def _delete_chat(self, path):
        try:
            Path(path).unlink()
        except OSError:
            pass
        if Path(path).stem == str(self.chat_id):
            # the open chat is gone: start fresh without re-saving it
            if self._running:
                self.stop_run(auto=True)
            self.chat_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._new_history()
            self._reset_meter()
            self._clear_msgs()
            self._show_empty_hint()
        self._refresh_sidebar()
        self._toast("Chat deleted")

    def _load_chat(self, path):
        try:
            meta = json.loads(Path(path).read_text())
        except Exception:
            return
        # Same reason as new_chat: an in-flight answer would otherwise land in
        # the chat we just switched to.
        if self._running:
            self.stop_run(auto=True)
        self._save_chat()
        self._clear_sudo_pw()
        self._py_close()                # another chat's variables don't belong here
        self.history = meta.get("history", [])
        if self.history and self.history[0].get("role") == "system":
            # old chats carry whatever prompt they were started with (v13's
            # was 5.7k characters); every resend would pay for it again
            self.history[0]["content"] = self._system_text()
        else:
            self.history.insert(0, {"role": "system", "content": self._system_text()})
        self.chat_id = meta.get("ts", datetime.now().strftime("%Y%m%d-%H%M%S"))
        c = meta.get("compact")
        self._compact = (c if isinstance(c, dict) and isinstance(c.get("upto"), int)
                         and isinstance(c.get("text"), str) and 1 < c["upto"] <= len(self.history)
                         else None)
        ws = meta.get("workspace") or ""
        if ws and os.path.isdir(ws) and (not self.workspace or str(self.workspace) != ws):
            self.set_workspace(ws, announce=False)
        self._reset_meter()
        u = meta.get("usage")
        if isinstance(u, dict):
            for k in self._usage:
                if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool):
                    self._usage[k] = u[k]
            self._paint_meter()
        self._clear_msgs()
        self._rebuild_transcript()
        self._refresh_sidebar()

    _INTERNAL = ("TOOL RESULTS", "RESEARCH BUDGET", "STEP BUDGET", "CONTINUE:", "I ran ",
                 "FILE ", "CONVERSATION SUMMARY", "[MESSAGE FROM THE USER")

    def _is_real_user(self, m):
        """A message the USER wrote (not tool results or other scaffolding)."""
        c = m.get("content")
        return (m.get("role") == "user" and
                not (isinstance(c, str) and c.startswith(self._INTERNAL)))

    @staticmethod
    def _typed_text(c):
        """What the user actually typed, from a stored user message."""
        if not isinstance(c, str):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        if c.startswith("[Since your last reply:"):
            c = c.split("]\n\n", 1)[-1]
        for cut in ("\n\n--- attached file:", "\n\n[attached file:", "\n[image attached:"):
            c = c.split(cut, 1)[0]
        return c

    _ACTION_RE = re.compile(r"`{3,}([a-z]+)[ \t]*([^\n`]*)\n?([^\n`]*)")
    _CODE_TAGS = ("bash", "sh", "py", "python", "node", "js", "javascript")

    @classmethod
    def _actions_of(cls, text):
        """'plan · write app.py · bash: python3 app.py' for a reply's tool blocks."""
        acts = []
        for tag, arg, first in cls._ACTION_RE.findall(text or ""):
            if tag in cls._CODE_TAGS:
                acts.append(f"{tag}: {(arg or first).strip()[:60]}".rstrip(": "))
            elif tag in ("plan", "ask"):
                acts.append(tag)
            else:
                acts.append(f"{tag} {arg.strip()[:60]}".strip())
        if len(acts) > 4:
            acts = acts[:4] + [f"+{len(acts) - 4} more"]
        return " \u00b7 ".join(acts)

    def _rebuild_transcript(self):
        """The visible transcript, from history (after a load, retry or edit)."""
        self._clear_msgs()
        did = ""                                 # what the last reply asked the machine to do
        spoke = False                            # Chuck has a byline in this turn already
        for m in self.history:
            if m["role"] == "system":
                continue
            if m["role"] == "user":
                if self._is_real_user(m):
                    spoke = False
                    self._user_bubble(self._typed_text(m.get("content")))
                elif isinstance(m.get("content"), str):
                    c = m["content"]
                    if c.startswith("CONVERSATION SUMMARY") or c.startswith("CONTINUE:"):
                        continue
                    body = c.split("\n\n", 1)[1] if c.startswith("TOOL RESULTS") and "\n\n" in c else c
                    if body.strip():
                        self._log_add("fold", {"text": clip_output(body.strip(), 12000),
                                               "title": did or "Tool results"})
                    did = ""
            else:
                raw = m.get("content") if isinstance(m.get("content"), str) else ""
                shown = self._display_text(raw)
                did = self._actions_of(raw)
                q = re.search(r"```ask[ \t]*\n(.*?)```", raw, re.S)
                if q:                            # the question you answered, not a blank
                    ask = "\n".join(ln for ln in q.group(1).strip().splitlines()
                                    if not re.match(r"\s*[-*]\s", ln)).strip()
                    shown = (shown + "\n\n" if shown else "") + ask
                if shown or (did and not spoke):
                    # an action-only reply still gets its byline, so what it ran
                    # hangs off Chuck rather than off your message; later hops of
                    # the same turn get none, as they don't live
                    self._log_add("bot", {"text": shown, "rich": True, "cont": spoke})
                    spoke = True
        if not self._log:
            self._show_empty_hint()
        self._stick = True
        self._scroll_down(force=True)

    def _rewind_to(self, n):
        """Keep history[:n]; anything derived from what's cut goes with it."""
        del self.history[n:]
        c = self._compact
        if c and c["upto"] >= len(self.history) - 1:
            self._compact = None                 # it summarised turns that no longer exist
        self._rebuild_transcript()

    def _last_user_index(self):
        for i in range(len(self.history) - 1, 0, -1):
            if self._is_real_user(self.history[i]):
                return i
        return None

    def retry_last(self):
        """Ctrl+R / /retry: ask the last question again, from where it was asked.
        Files that answer changed stay changed — /undo reverts them."""
        if self._running:
            return False
        i = self._last_user_index()
        if i is None:
            self._sys_note("Nothing to retry yet.", "dim")
            return False
        self._rewind_to(i + 1)
        self._reset_turn_state()
        self._sys_note("\u21bb retrying" + (" \u2014 files that answer changed stay as they "
                                            "are (/undo reverts them)" if any(
                                                c["turn"] == self._turn_no - 1
                                                for c in self._changes) else ""), "dim")
        self._start_run()
        self._ask_model()
        return True

    def edit_last(self):
        """↑ in an empty composer: bring your last message back to edit and resend."""
        if self._running:
            return False
        i = self._last_user_index()
        if i is None:
            return False
        self._edit_at = i
        self.entry.get_buffer().set_text(self._typed_text(self.history[i].get("content")))
        self._toast("Editing your last message \u2014 Enter resends from there, Esc cancels")
        return True

    def _on_close(self, *_):
        self._save_chat()
        self._py_close()
        # background jobs belong to this session: don't leave a dev server
        # holding a port after the window that started it is gone
        try:
            self._stop_all_jobs()
        except Exception:
            pass
        try:
            if self._mcp is not None:
                self._mcp.stop_all()
        except Exception:
            pass
        # Speech outlives the window otherwise — the process keeps a daemon
        # thread feeding paplay, so closing mid-answer left a disembodied voice
        # finishing the reply.
        try:
            stop_speaking()
        except Exception:
            pass
        # recall() batches hit-count writes to keep them off the per-turn
        # critical path, so a session's worth of counters is still in memory
        # here. Without this they are lost, and the ranking that decides which
        # facts survive pruning never learns anything from a short session.
        if _memory is not None:
            try:
                _memory.flush()
            except Exception:
                pass
        return False

    # ── keyboard ──
    def _on_key(self, ctrl, keyval, keycode, state):
        """Composer keys: Enter sends, Shift+Enter is a new line, Ctrl+V takes
        an image or copied files as attachments."""
        try:
            if (state & Gdk.ModifierType.CONTROL_MASK) and Gdk.keyval_to_lower(keyval) == Gdk.KEY_v:
                if self._paste():
                    return True
        except Exception:
            pass
        if keyval == getattr(Gdk, "KEY_Up", -1) and not self._running and not self._get_entry():
            return self.edit_last()
        if keyval == Gdk.KEY_Escape and self._edit_at is not None and not self._running:
            self._edit_at = None
            self.entry.get_buffer().set_text("")
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not (state & Gdk.ModifierType.SHIFT_MASK):
            if self._running and not self._get_entry():
                return False    # busy and nothing typed: Enter is just a newline
            self.on_send()      # while busy, this queues the message for the next step
            return True
        return False

    def _on_window_key(self, ctrl, keyval, keycode, state):
        """Window shortcuts. Tab flips Agent/Chat; Ctrl+N/O/B/, are the usual
        suspects; Escape stops a running turn."""
        try:
            mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
                            | Gdk.ModifierType.SHIFT_MASK)
            ctrl_on = bool(state & Gdk.ModifierType.CONTROL_MASK)
        except Exception:
            return False
        # Settings (an Adw.Dialog) lives INSIDE this window, so its key events
        # pass through this capture handler first: Tab flipped the mode instead
        # of moving between fields. Shortcuts apply only to the main content.
        try:
            if getattr(self, "get_visible_dialog", None) and self.get_visible_dialog():
                return False
            focus = self.get_focus()
            content = self.get_content()
            if focus is not None and content is not None and focus is not content \
                    and not focus.is_ancestor(content):
                return False
            if (keyval == Gdk.KEY_Tab and focus is not None and focus is not self.entry
                    and isinstance(focus, Gtk.Text)):
                return False                     # Tab in the chat search moves focus
        except Exception:
            pass
        if keyval == Gdk.KEY_Tab and not mods:
            self.toggle_mode()
            return True
        if keyval == Gdk.KEY_Escape and self._running:
            self.stop_run()
            return True
        if ctrl_on:
            k = Gdk.keyval_to_lower(keyval)
            if k == Gdk.KEY_n:
                self.new_chat(); return True
            if k == Gdk.KEY_o:
                self.open_workspace(); return True
            if k == Gdk.KEY_b:
                self.side_btn.set_active(not self.side_btn.get_active()); return True
            if k == Gdk.KEY_comma:
                self.open_settings(); return True
            if k == getattr(Gdk, "KEY_r", -2):
                self.retry_last(); return True
        return False

    # ── indicators ──
    def _busy(self, on):
        def go():
            self._busy_n = max(0, self._busy_n + (1 if on else -1))
            if self._busy_n > 0:
                self.spinner.set_visible(True); self.spinner.start()
            else:
                self.spinner.stop(); self.spinner.set_visible(False)
            return False
        GLib.idle_add(go)

    # ── run lifecycle: send↔stop button, "still working" heartbeat, watchdog ──
    RUN_HARD_CAP = 3600     # seconds: ceiling on one turn. It was 900, which killed real
                            # agent work (a 25-step build-and-test task runs past 15 min);
                            # true hangs are the stuck-watchdog's job, not this cap's.
    STUCK_AFTER = 120       # seconds with zero progress BETWEEN steps → assume stuck
    AWAIT_FIRST = 180       # seconds allowed for the model's FIRST token
    # Waiting on the first token looks identical to being stuck — nothing is
    # arriving — but the request is open and the model is simply thinking or
    # queued. Judging that by the tight between-steps timer is what killed
    # perfectly healthy first messages at 45s.
    #
    # The same mistake applied to COMMANDS. A shell command blocks its worker
    # thread with nothing to report until it exits, so `pacman -Syu`, a makepkg
    # build or a large npm install went 120s without progress and got shot as
    # "stuck" — while estimate_runtime had already granted the same command up
    # to 1800s. The turn was cancelled, and the real output was then dropped on
    # arrival because _tool_done early-returns on _cancelled. So: while a
    # command is running, the watchdog uses THAT command's own budget, and the
    # hard cap stretches to cover it.

    def _arm_run_budget(self, command):
        """A command is about to block a worker: give the watchdog its real
        expected runtime, so a legitimately slow one isn't mistaken for a hang."""
        try:
            est = estimate_runtime(command)
            self._run_budget = int(est["hard_timeout_seconds"]) + 30   # + grace
        except Exception:
            self._run_budget = self.STUCK_AFTER
        self._note_progress()

    def _note_progress(self):
        """Anything happening (a delta, a step, a hop) calls this — resets the
        stuck-watchdog so real long work isn't killed, only true hangs are."""
        self._last_progress = time.time()

    def _start_run(self):
        if self._running:
            return
        self._running = True
        self._cancelled = False
        self._gen += 1
        self._sync_placeholder()
        self._run_started = time.time()
        self._last_progress = time.time()
        # flip Send → Stop
        self.send_btn.set_icon_name(_pick_icon("media-playback-stop-symbolic", "process-stop-symbolic"))
        self.send_btn.remove_css_class("send-fab"); self.send_btn.add_css_class("stop-fab")
        self.send_btn.set_tooltip_text("Stop  (Esc)")
        # show the "still working" ticker
        self.live.set_visible(True)
        self.spinner.set_visible(True)
        try:
            self.spinner.start()
        except Exception:
            pass
        self._tick(0)
        if not self._heartbeat_id:
            self._heartbeat_id = GLib.timeout_add(1000, self._heartbeat)

    def _end_run(self):
        self._running = False
        self._thinking = False
        self._sync_placeholder()
        self.send_btn.set_icon_name(_pick_icon("go-up-symbolic", "pan-up-symbolic", "up-symbolic"))
        self.send_btn.remove_css_class("stop-fab"); self.send_btn.add_css_class("send-fab")
        self.send_btn.set_tooltip_text("Send  (Enter)")
        self.live.set_visible(False)
        try:
            self.spinner.stop()
        except Exception:
            pass
        self.spinner.set_visible(False)
        if self._heartbeat_id:
            GLib.source_remove(self._heartbeat_id); self._heartbeat_id = 0

    def _tick(self, secs):
        s = int(secs)
        if getattr(self, "_thinking", False):
            what = "thinking"
        elif getattr(self, "_awaiting_first", False):
            what = "waiting for the model"
        else:
            what = "working"
        step = f"  \u00b7  step {self._steps + 1}" if self._steps else ""
        self.live.set_text(f"{what}\u2026 {s}s{step}    Esc to stop")

    def _heartbeat(self):
        """Runs every second while a turn is active: updates the elapsed clock so
        you can SEE it's alive, and enforces the hard cap + stuck-watchdog."""
        if not self._running:
            return False  # stop the timer
        now = time.time()
        # A modal password prompt is open and we're blocked waiting on the user —
        # that's not "stuck", so keep the progress clock fresh so the watchdog
        # doesn't kill the turn out from under the dialog.
        if getattr(self, "_awaiting_input", False):
            self._last_progress = now
        elapsed = now - self._run_started
        self._tick(elapsed)
        run_budget = getattr(self, "_run_budget", 0)
        # A command that is legitimately allowed 1800s must not be guillotined
        # by a 900s turn cap; the cap stretches to cover whatever is running.
        cap = max(self.RUN_HARD_CAP, run_budget + self.STUCK_AFTER)
        if elapsed > cap:
            self._sys_note(f"\u23f1 hit the {cap}s time cap \u2014 stopping.", "danger")
            self.stop_run(auto=True)
            return False
        waiting = getattr(self, "_awaiting_first", False)
        if waiting:
            budget = self.AWAIT_FIRST
        else:
            budget = max(self.STUCK_AFTER, run_budget)
        if now - self._last_progress > budget:
            if waiting:
                self._sys_note(f"\u26a0 the model didn't respond within {budget}s. "
                               "Send it again \u2014 the connection is warm now.", "danger")
            else:
                self._sys_note(f"\u26a0 no progress for {budget}s \u2014 looks stuck, stopping.",
                               "danger")
            self.stop_run(auto=True)
            return False
        return True  # keep ticking

    def _on_send_or_stop(self, *_):
        if self._running:
            self.stop_run()
        else:
            self.on_send()

    def stop_run(self, auto=False):
        """Cancel the current turn cleanly: signal the stream/loops to bail,
        reset all loop state, revert the button."""
        if not self._running and not auto:
            return
        self._cancelled = True
        self._gen += 1
        # reset the tool-loop so nothing re-fires.
        # This said `self._hops = MAX_TOOL_HOPS` and claimed to block further
        # hops — but the cap it is compared against is cfg('research_hops'),
        # which the user can raise to 8. With the constant at 4 and the setting
        # at 8, Stop set the counter to 4 < 8 and blocked precisely nothing.
        self._hops = max(MAX_TOOL_HOPS, self.cfg('research_hops', MAX_TOOL_HOPS, 1, 8))
        self._pending_tools = 0
        self._loop_web = None
        self._tool_feedback = []
        self._run_budget = 0
        self._dispatching = False
        with self._sudo_cv:
            self._sudo_denied = True    # stop asking; the turn is over
            self._sudo_cv.notify_all()
        kill_live_commands()            # Stop means the COMMAND stops too
        if self._py is not None:
            self._py.interrupt()        # Ctrl+C for the Python session (it survives)
        if self._steer:
            # undelivered messages go back in the composer, not into the void
            if not self._get_entry():
                self.entry.get_buffer().set_text("\n\n".join(self._steer))
            self._steer = []
        # drain the busy counter
        self._busy_n = 0
        self.spinner.stop(); self.spinner.set_visible(False)
        if getattr(self, "_activity_box", None) is not None:
            self._activity_end()
        stop_speaking()          # Stop means stop talking, too
        if not auto:
            self._sys_note("\u25A0 stopped.", "dim")
        # finalize whatever partial text exists so it isn't lost
        if getattr(self, "_cont_prefix", None) is not None:
            self._collapse_continuation(self._bot_text)
            self.history.append({"role": "assistant", "content": self._bot_text})
        if self._bot_label is not None and self._bot_text:
            shown = self._display_text(self._bot_text, partial=True)
            if shown:
                self._set_bot_text(shown, rich=True)
        self._thinking = False
        self._end_run()

    def _live(self, text):
        def go():
            self.live.set_text(text); self.live.set_visible(bool(text)); return False
        GLib.idle_add(go)

    # ── live activity panel: a running checklist of exactly what Chuck is doing,
    #    so you can see every step (searching BBC, reading <article>, verifying…)
    #    instead of a silent spinner. Steps persist in the chat.
    def _activity_start(self, header="Working"):
        def go():
            self._drop_hint()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("step-box")
            hl = Gtk.Label(label=header, xalign=0); hl.add_css_class("step-head")
            box.append(hl)
            self._log_pin(box)
            self._activity_box = box
            self._activity_steps = []
            self._scroll_down()
            return False
        GLib.idle_add(go)

    def _activity_step(self, text):
        """Add a step marked 'running'. Returns an index to finish it later.
        Thread-safe: reserves the slot under a lock (parallel fetches call this
        concurrently), then schedules the widget onto the main loop."""
        if not hasattr(self, "_activity_lock"):
            self._activity_lock = threading.Lock()
        with self._activity_lock:
            if not hasattr(self, "_activity_steps"):
                self._activity_steps = []
            idx = len(self._activity_steps)
            self._activity_steps.append(None)

        def go():
            box = getattr(self, "_activity_box", None)
            if box is None:
                return False
            lbl = Gtk.Label(label="\u25CF " + text, xalign=0, wrap=True)
            lbl.add_css_class("step-run")
            box.append(lbl)
            if idx < len(self._activity_steps):
                self._activity_steps[idx] = lbl
            self._scroll_down()
            return False
        GLib.idle_add(go)
        return idx

    def _activity_done(self, idx, text=None, ok=True):
        def go():
            steps = getattr(self, "_activity_steps", [])
            if idx < 0 or idx >= len(steps) or steps[idx] is None:
                return False
            lbl = steps[idx]
            cur = lbl.get_text().lstrip("\u25CF ").strip()
            mark = "\u2713" if ok else "\u2717"
            lbl.set_text(f"{mark} {text if text is not None else cur}")
            lbl.remove_css_class("step-run")
            lbl.add_css_class("step-done" if ok else "step-fail")
            return False
        GLib.idle_add(go)

    def _activity_end(self):
        # leave the finished panel in place as a record; just clear the handle
        def go():
            self._activity_box = None
            self._activity_steps = []
            return False
        GLib.idle_add(go)

    # ── bubbles ──
    def _scroll_down(self, force=False):
        """Follow the newest output — but only while following. A user who has
        scrolled up to read is left exactly where they are (the jump button
        offers the way back); force=True is that way back, and what sending a
        message does."""
        if force:
            self._stick = True
            self._sync_jump()
        if not self._stick:
            return

        def go():
            try:
                a = self.scroller.get_vadjustment()
                a.set_value(max(0.0, a.get_upper() - a.get_page_size()))
            except Exception:
                pass
            return False
        GLib.idle_add(go)

    def _user_bubble(self, text, shot=False, files=None, images=None):
        self._log_add("user", {"text": text, "shot": shot, "files": files or [],
                               "images": images or []})

    def _bot_bubble(self, text=""):
        model = (self.settings.get("model") or DEFAULT_MODEL).split("/")[-1]
        cont = False
        for prev in reversed(self._log):
            if prev["kind"] == "user":
                break
            if prev["kind"] == "bot":
                cont = True
                break
        e = self._log_add("bot", {"text": text, "rich": False, "meta": model, "cont": cont})
        self._bot_entry = e
        return e.get("label")

    # Fenced blocks that are TOOLS (run, not shown) in each mode. In Chat mode
    # code fences are just code, so ```bash renders as a copyable block.
    _WEB_TAGS = "search|fetch|images|videos|video|remember|forget|ask"
    _CODE_TAGS = "python|py|node|javascript|js|bash|sh"

    def _tool_tags(self):
        tags = self._WEB_TAGS + "|" + "|".join(_AGENT_TAGS + tuple(_TOOL_REG))
        return tags + "|" + self._CODE_TAGS if self.agent_mode else tags

    def _display_text(self, text, partial=False):
        """A reply with its tool blocks removed: what the user reads. partial=
        True also drops a tool block that is still open (mid-stream, or cut off
        by the output cap) so half a command never flashes up as prose."""
        if not text:
            return ""
        if "```" not in text:
            return text.strip()
        tags = self._tool_tags()
        if self.agent_mode:
            text = _cut_spans(text, [(b["start"], b["end"]) for b in file_blocks(text)])
        shown = re.sub(r"````(?:" + tags + r")(?![A-Za-z]).*?````", "", text, flags=re.DOTALL)
        shown = re.sub(r"```(?:" + tags + r")(?![A-Za-z]).*?```", "", shown, flags=re.DOTALL)
        if partial:
            shown = re.sub(r"`{3,4}(?:" + tags + r")(?![A-Za-z])(?:(?!```).)*$", "",
                           shown, flags=re.DOTALL)
        return re.sub(r"\n{3,}", "\n\n", shown).strip()

    def _visible_stream_text(self):
        """The narration to show mid-stream, with tool blocks (closed or still
        open) removed. Short-circuits for the common reply with no fences."""
        return self._display_text(self._bot_text, partial=True)

    def _paint_stream(self):
        if self._cancelled or self._bot_label is None:
            return False
        txt = self._visible_stream_text()
        if txt:
            self._set_bot_text(txt, rich=True)
        else:
            self._set_bot_text("Thinking…" if self._thinking or self._awaiting_first
                               else "Working…")
        return False

    def _set_bot_text(self, text, rich=False):
        """Update the live reply AND its log entry, so the text survives being
        scrolled out of the window and rebuilt later."""
        if self._bot_entry is not None:
            self._bot_entry["data"]["text"] = text
            self._bot_entry["data"]["rich"] = rich
        if self._bot_entry is not None and self._bot_entry.get("label") is not None:
            self._render_bot(self._bot_entry)

    def _sys_note(self, text, css="dim"):
        self._log_add("note", {"text": text, "css": css})

    def _build_image_widget(self, path, src_url=None):
        """Construct (or reconstruct) an image bubble from its path — images are
        the heaviest thing in a transcript, so they unload with the window and
        are re-read from disk if you scroll back to them."""
        try:
            pic, pb = _pic_from_file(path, 320, 320)
            pic.set_size_request(-1, pb.get_height())
            if src_url:
                btn = Gtk.Button(); btn.add_css_class("sendbtn"); btn.set_child(pic)
                btn.set_tooltip_text("Open in Brave")
                btn.connect("clicked", lambda *_: open_in_brave(src_url))
                child = btn
            else:
                child = pic
            w = Gtk.Box(halign=Gtk.Align.START); w.append(child)
            return w
        except Exception:
            return Gtk.Label(label="[image unavailable]", xalign=0)

    def _image_bubble(self, path, src_url=None):
        self._log_add("image", {"path": path, "src": src_url})


    # ── the meter ────────────────────────────────────────────────────────────
    # Every hop of an agent loop re-sends the conversation, so a chat bills for
    # its prefix once per tool call. That is invisible by default, which is how
    # a cheap-looking chat quietly turns into a bill. The meter counts what the
    # API REPORTS (prompt, cache hits, completion, reasoning), prices it per
    # model, and falls back to a labelled chars/4 estimate only when a response
    # carried no usage block.

    @staticmethod
    def _blank_usage():
        return {"prompt": 0, "completion": 0, "cached": 0, "reasoning": 0,
                "calls": 0, "cost": 0.0, "estimated": 0, "unpriced": 0}

    @staticmethod
    def _est_tokens(chars):
        """Rough token count for a response with no usage block: four
        characters per token, the usual rule of thumb."""
        return max(0, int(chars / 4))

    def _account(self, sent_chars, recv_chars, vision=False, usage=None, model=None):
        """Book one model call into this chat's and this session's totals."""
        u = usage if usage is not None else getattr(self.backend, "last_usage", None)
        est = not u
        if est:
            u = {"prompt": self._est_tokens(sent_chars),
                 "completion": self._est_tokens(recv_chars), "cached": 0, "reasoning": 0}
        if model is None:
            try:
                model = self.backend.model(vision)
            except Exception:
                model = self.settings.get("model") or DEFAULT_MODEL
        c = _pricing.cost(u, _pricing.price_for(model, self.settings))
        for tot in (self._usage, self._session):
            for k in ("prompt", "completion", "cached", "reasoning"):
                tot[k] += int(u.get(k) or 0)
            tot["calls"] += 1
            tot["estimated"] += 1 if est else 0
            if c is None:
                tot["unpriced"] += 1
            else:
                tot["cost"] += c
        self._tok_in, self._tok_out = self._usage["prompt"], self._usage["completion"]
        self._paint_meter()

    def _meter(self, sent_chars=0, recv_chars=0):
        """Compatibility shim: book an estimate-only call."""
        if sent_chars or recv_chars:
            self._account(sent_chars, recv_chars)
        else:
            self._paint_meter()

    def _paint_meter(self):
        def go():
            lbl = getattr(self, "_meter_lbl", None)
            if lbl is None:
                return False
            u = self._usage
            tok = _pricing.fmt_tokens(u["prompt"] + u["completion"])
            if not u["calls"]:
                lbl.set_label("$0.00")
            elif u["unpriced"] == u["calls"]:
                lbl.set_label(f"{tok} tok")
            else:
                lbl.set_label(f"{_pricing.fmt_usd(u['cost'])}  ·  {tok}")
            return False
        GLib.idle_add(go)

    def _reset_meter(self):
        self._usage = self._blank_usage()
        self._cap_extra = 0.0           # spending the user approved past the cap, this chat
        self._tok_in = self._tok_out = 0
        self._paint_meter()

    def set_model(self, mid):
        mid = (mid or "").strip()
        if not mid:
            return
        self.settings["model"] = mid
        save_settings(self.settings)
        self._update_subtitle()
        try:
            self._model_pop.popdown()
        except Exception:
            pass
        self._toast(f"Model: {mid.split('/')[-1]}")

    def _fill_model_popover(self):
        """Quick model switch: the main models with their prices, the free tier,
        and whatever was fetched from the account in Settings."""
        cur = (self.settings.get("model") or DEFAULT_MODEL).strip().lower()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{m}")(8)

        def section(box, t):
            l = Gtk.Label(label=t, xalign=0); l.add_css_class("set-section")
            l.set_margin_start(8); l.set_margin_top(6); l.set_margin_bottom(2)
            box.append(l)

        def item(box, mid):
            b = Gtk.Button(); b.add_css_class("pick-row")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
            t = Gtk.Label(label=mid.split("/")[-1], xalign=0); t.add_css_class("pick-title")
            pr = _pricing.PRICES.get(mid.lower())
            sub = ("free" if _pricing.is_free(mid) else
                   f"${pr[0]:g} in \u00b7 ${pr[2]:g} out per 1M" if pr else mid.split("/")[0])
            if _pricing.sees_images(mid):
                sub += "  \u00b7  sees images"
            st = Gtk.Label(label=sub, xalign=0); st.add_css_class("pick-sub")
            col.append(t); col.append(st)
            row.append(col)
            if mid.lower() == cur:
                row.append(Gtk.Image.new_from_icon_name(_pick_icon("object-select-symbolic",
                                                                   "emblem-ok-symbolic")))
            b.set_child(row)
            b.connect("clicked", lambda _b, x=mid: self.set_model(x))
            box.append(b)

        section(root, "MODELS")
        for mid in _pricing.SUGGESTED:
            item(root, mid)
        section(root, "FREE")
        for mid in _pricing.FREE:
            item(root, mid)
        known = [m.lower() for m in _pricing.SUGGESTED + _pricing.FREE]
        extra = [m for m in (self.settings.get("fetched_models") or [])
                 if isinstance(m, str) and m.lower() not in known]
        if cur not in known and cur not in [m.lower() for m in extra]:
            extra = [self.settings.get("model") or DEFAULT_MODEL] + extra
        if extra:
            section(root, "FROM YOUR ACCOUNT")
            sw = Gtk.ScrolledWindow()
            try:
                sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                sw.set_max_content_height(220); sw.set_propagate_natural_height(True)
            except Exception:
                pass
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            for mid in extra[:80]:
                item(inner, mid)
            sw.set_child(inner)
            root.append(sw)
        root.set_size_request(300, -1)
        self._model_pop.set_child(root)

    def _fill_usage_popover(self):
        """The breakdown behind the meter: tokens by kind, what the model costs,
        the session total, and the live account balance on request."""
        u, ss = self._usage, self._session
        model = self.settings.get("model") or DEFAULT_MODEL
        price = _pricing.price_for(model, self.settings)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        hdr = Gtk.Label(label="THIS CHAT", xalign=0); hdr.add_css_class("set-section")
        box.append(hdr)
        big = Gtk.Label(label=_pricing.fmt_usd(u["cost"]) if u["calls"] and
                        u["unpriced"] < u["calls"] else "—", xalign=0)
        big.add_css_class("usage-big")
        box.append(big)
        g = Gtk.Grid(column_spacing=18, row_spacing=6)
        rows = [
            ("Input", f"{_pricing.fmt_tokens(u['prompt'])}"
                      + (f"  ({_pricing.fmt_tokens(u['cached'])} cached)" if u["cached"] else "")),
            ("Output", f"{_pricing.fmt_tokens(u['completion'])}"
                       + (f"  ({_pricing.fmt_tokens(u['reasoning'])} reasoning)"
                          if u["reasoning"] else "")),
            ("Model calls", str(u["calls"])),
            ("Model", model.split("/")[-1]),
            ("Price / 1M", (f"${price[0]:g} in · ${price[2]:g} out"
                            + (f" · ${price[1]:g} cached" if price[1] is not None else ""))
             if price else "unknown — set one in Settings"),
            ("Session total", _pricing.fmt_usd(ss["cost"])
             + f"  \u00b7  {ss['calls']} call{'' if ss['calls'] == 1 else 's'}"),
        ]
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=0); kl.add_css_class("usage-k")
            vl = Gtk.Label(label=v, xalign=0, selectable=True); vl.add_css_class("usage-v")
            g.attach(kl, 0, i, 1, 1); g.attach(vl, 1, i, 1, 1)
        box.append(g)
        if u["estimated"]:
            n = Gtk.Label(label=f"{u['estimated']} call(s) estimated (no usage reported)",
                          xalign=0, wrap=True)
            n.add_css_class("set-hint"); box.append(n)
        br = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bl = Gtk.Label(label="Balance  " + (self._balance or "—"), xalign=0, hexpand=True)
        bl.add_css_class("usage-k")
        rb = Gtk.Button(label="Check"); rb.add_css_class("quick")

        def check(_b):
            bl.set_label("Balance  …")

            def work():
                ok, txt = _pricing.fetch_balance(self.backend.base(), self.backend.key(),
                                                 opener=_config.api_opener())
                if ok:
                    self._balance = txt

                def show():
                    bl.set_label("Balance  " + txt)
                    return False
                GLib.idle_add(show)
            threading.Thread(target=work, daemon=True).start()
        rb.connect("clicked", check)
        br.append(bl); br.append(rb)
        box.append(br)
        self._usage_pop.set_child(box)

    # ── card furniture ───────────────────────────────────────────────────────
    # Everything the MACHINE did is drawn as a card: a titled bar, the payload,
    # then the result. Chuck's own prose is never in one. That single rule is
    # what stops a build log reading like a sentence he wrote.

    def _clip(self, text):
        """Put text on the clipboard. GTK4 has moved this API around, so try the
        content-provider route first and fall back to the convenience setter."""
        try:
            cb = self.get_clipboard()
        except Exception:
            return False
        try:
            from gi.repository import GObject
            cb.set_content(Gdk.ContentProvider.new_for_value(
                GObject.Value(str, text)))
            return True
        except Exception:
            pass
        try:
            cb.set(text)
            return True
        except Exception:
            return False

    def _copy_button(self, getter, tip="Copy"):
        """A quiet copy affordance. Takes a callable so the text is read at click
        time — a command card's output doesn't exist yet when the button is made."""
        b = Gtk.Button(icon_name=_pick_icon("edit-copy-symbolic", "edit-paste-symbolic",
                                            "document-save-symbolic"))
        b.add_css_class("ghost")
        b.set_tooltip_text(tip)
        b.set_valign(Gtk.Align.CENTER)

        def go(_b):
            ok = self._clip(getter() or "")
            b.set_tooltip_text("Copied" if ok else "Couldn't copy")
        b.connect("clicked", go)
        return b

    @staticmethod
    def _set_status(pill, text, kind):
        """Move a status chip between its states. Defensive about the class list
        because _run_card is also called directly (by the test harness) with a
        plain label rather than a pill."""
        for c in ("pill-wait", "pill-run", "pill-ok", "pill-fail", "dim", "ok", "danger"):
            try:
                pill.remove_css_class(c)
            except Exception:
                pass
        try:
            pill.add_css_class("pill")
            pill.add_css_class(f"pill-{kind}")
        except Exception:
            pass
        pill.set_label(text)

    @staticmethod
    def _pill(text, kind="wait"):
        """A small status chip — 'exit 0' in green, 'exit 1' in red, 'running' in
        clay. The exit code is the most important fact on a card, so it gets a
        shape of its own instead of being a grey word at the end of a row."""
        l = Gtk.Label(label=text, xalign=0.5)
        l.add_css_class("pill")
        l.add_css_class(f"pill-{kind}")
        l.set_valign(Gtk.Align.CENTER)
        return l

    def _card_shell(self, kind, copy_getter=None):
        """Build the standard card: (card, head, body, foot).

        head — the kind label ('COMMAND', 'PYTHON', 'FILE') plus its controls
        body — the payload, padded
        foot — status, Run button, risk warnings, and any output panel
        """
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("cmd-card")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.add_css_class("card-head")
        kl = Gtk.Label(label=kind, xalign=0, hexpand=True)
        kl.add_css_class("card-kind")
        head.append(kl)
        if copy_getter is not None:
            head.append(self._copy_button(copy_getter, "Copy to clipboard"))
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        body.add_css_class("card-body")
        card.append(body)
        foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        foot.add_css_class("card-foot")
        card.append(foot)
        return card, head, body, foot

    def _term_panel(self, text, rc=None, title="OUTPUT"):
        """Real stdout/stderr, in its own black panel.

        Three things this fixes. It is visibly a TERMINAL, not prose. It is
        height-capped and scrolls, so a 500-line pacman transaction stops
        burying the answer that follows it. And lines are NOT wrapped, so
        columnar output (ls -l, df, systemctl) keeps its alignment instead of
        folding into mush — you scroll sideways for the long ones, exactly like
        a terminal.
        """
        text = (text or "").rstrip("\n")
        # Count BEFORE truncating — "367 lines … truncated" on a 4,000-line log
        # would be a quietly wrong number, and the whole point of the header is
        # to tell you how much you are not seeing.
        n = text.count("\n") + 1 if text else 0
        clipped = ""
        if len(text) > 12000:
            text, clipped = text[:12000], "  \u2026 truncated"

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("term-card")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("term-head")
        tl = Gtk.Label(label=title, xalign=0)
        tl.add_css_class("term-label")
        head.append(tl)
        meta = Gtk.Label(label=f"{n} line{'' if n == 1 else 's'}{clipped}",
                         xalign=0, hexpand=True)
        meta.add_css_class("term-meta")
        head.append(meta)
        if rc is not None:
            head.append(self._pill(f"exit {rc}", "ok" if rc == 0 else "fail"))
        head.append(self._copy_button(lambda: text, "Copy output"))

        body = Gtk.Label(label=text, xalign=0, selectable=True, wrap=False)
        body.add_css_class("term-body")
        body.set_valign(Gtk.Align.START)
        sw = Gtk.ScrolledWindow()
        sw.add_css_class("term-scroll")
        try:
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sw.set_max_content_height(260)
            sw.set_propagate_natural_height(True)
        except Exception:
            pass
        sw.set_child(body)
        self._chain_scroll(sw, vertical=True)
        _follow_bottom(sw)              # open on the END of a long log: that's where errors are

        fold = Gtk.Button(label="hide")
        fold.add_css_class("ghost")
        fold.set_valign(Gtk.Align.CENTER)

        def toggle(_b):
            shown = sw.get_visible()
            sw.set_visible(not shown)
            fold.set_label("show" if shown else "hide")
        fold.connect("clicked", toggle)
        head.append(fold)

        card.append(head)
        card.append(sw)
        return card

    def _show_output(self, out, rc, sink=None):
        """Attach the result of a run. Inside its own card when we have one, so
        the command and what it printed stay one object; otherwise as a
        standalone panel in the transcript."""
        if not (out or "").strip() or out.strip() == "(no output)":
            return                       # an empty result needs no panel saying so
        if sink is not None:
            # The card's own status chip already states the exit code, so the
            # panel doesn't repeat it — two identical pills side by side just
            # reads as noise.
            sink.append(self._term_panel(out, None))
        else:
            self._log_pin(self._term_panel(out, rc))

    # ── command cards ──
    def _risk_gate(self, card, run_btn, text, what="command"):
        """Attach the right warning to a card and, for CRITICAL commands, keep the
        Run button DISARMED until the user explicitly confirms. A single misclick
        must never be able to wipe a disk."""
        tier = classify_command(text)
        if not tier:
            return
        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 read it before you run it", xalign=0)
            wl.add_css_class("danger"); card.append(wl)
            return
        # critical: disarm Run, require a deliberate second action
        run_btn.set_sensitive(False)
        wl = Gtk.Label(
            label=f"\u26d4 CRITICAL \u2014 this {what} can destroy data or make the system "
                  "unbootable. It cannot be undone.",
            xalign=0, wrap=True)
        wl.add_css_class("critical"); card.append(wl)
        arm_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        arm = Gtk.CheckButton(label="I've read it and I accept the risk")
        arm.add_css_class("danger")

        def on_arm(btn):
            run_btn.set_sensitive(btn.get_active())
        arm.connect("toggled", on_arm)
        arm_row.append(arm); card.append(arm_row)

    def _sanitise_skill_body(self, body, launch_cmd):
        """Apply pacman hygiene to a skill BODY and rewrite it in place.

        Returns the corrected body (also used for risk classification). If the
        file can't be rewritten the corrected text is still returned, so the
        classifier judges the worse of the two.
        """
        fixed = add_noconfirm(enforce_syu(body))
        if fixed == body:
            return body
        self._sys_note("corrected pacman flags inside the skill (-Syu / --noconfirm)", "dim")
        m = re.match(r"^(?:bash|sh|python3?)\s+(.+)$", (launch_cmd or "").strip())
        if m:
            try:
                target = Path(shlex.split(m.group(1))[0]).expanduser()
                if target.is_file():
                    target.write_text(fixed)
            except Exception:
                pass
        return fixed

    def _command_card(self, cmd, gate_text=None):
        """Run a shell command and report the real result.

        Commands EXECUTE immediately — Chuck is an agent, not a suggestion box.
        The one exception is the CRITICAL tier (disk wipes, rm -rf /, curl|sh,
        reformatting, pulling core packages): those still need a deliberate tick
        first, because the cost of a hallucinated one is unrecoverable and no
        amount of convenience is worth it. Everything else just runs.

        gate_text: when the command merely LAUNCHES a script (a saved skill),
        pass the script body so the risk tier is judged on what will actually
        execute, not on the harmless-looking `bash foo.sh` wrapper.
        """
        fixed = enforce_syu(cmd)
        if fixed != cmd:
            self._sys_note("using -Syu (a plain -S risks a partial upgrade)", "dim")
        with_nc = add_noconfirm(fixed)
        if with_nc != fixed:
            self._sys_note("added --noconfirm so it can't stall on a prompt", "dim")
        cmd = with_nc
        # A skill launches as `bash /path/to/skill.sh`, so the rewriters above
        # only ever saw the wrapper — a bare `pacman -S` inside the BODY sailed
        # through un-rewritten and un-noconfirmed. The risk classifier already
        # judged gate_text; now the pacman hygiene does too, and the body is
        # rewritten on disk so what runs is what was checked.
        if gate_text is not None:
            gate_text = self._sanitise_skill_body(gate_text, cmd)
        judged = cmd if gate_text is None else (cmd + "\n" + gate_text)
        tier = classify_command(judged)

        card, head, cbody, foot = self._card_shell("COMMAND", lambda: cmd)
        prompt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dollar = Gtk.Label(label="$", xalign=0, valign=Gtk.Align.START)
        dollar.add_css_class("cmd-prompt")
        ct = Gtk.Label(label=cmd, xalign=0, wrap=True, selectable=True, hexpand=True)
        ct.add_css_class("cmd-text"); _wrap_anywhere(ct)
        prompt.append(dollar); prompt.append(ct)
        cbody.append(prompt)
        status = self._pill("queued", "wait")

        if tier == "critical":
            run = Gtk.Button(label="Run"); run.add_css_class("gold")
            self._risk_gate(foot, run, judged, "command")
            self._set_status(status, "needs approval", "wait")
            run.connect("clicked",
                        lambda _b: self._run_card(cmd, run, status, gate_text, sink=foot))
            rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rw.append(run); rw.append(status); foot.append(rw)
            self._log_pin(card)
            self._tool_feedback.append(
                f"[`{cmd}` is CRITICAL and is waiting for the user to confirm it. "
                "Do not assume it ran.]")
            return

        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 running it now", xalign=0)
            wl.add_css_class("danger"); foot.append(wl)
        head.insert_child_after(status, head.get_first_child())
        self._log_pin(card)
        self._run_now(cmd, status, sink=foot)

    # ── sudo credential: memory only, this chat, 30-min TTL ──────────────────
    def _sudo_pw_valid(self):
        return bool(self._sudo_pw) and (time.time() - self._sudo_pw_time) < 1800

    def _cache_sudo_pw(self, pw):
        """Hold the sudo password in memory for this chat. Never written to disk,
        the log, or the conversation — the model has no way to read it."""
        self._sudo_pw = pw or None
        self._sudo_pw_time = time.time() if pw else 0.0

    def _clear_sudo_pw(self):
        self._sudo_pw = None
        self._sudo_pw_time = 0.0
        with self._sudo_cv:
            self._sudo_denied = False
            self._sudo_asking = False
            self._sudo_cv.notify_all()

    def _sudo_prompt(self, script, cb):
        """Ask for the sudo password once. Calls cb(password_or_None) exactly
        once. Runs on the main thread (a worker marshals here via idle_add)."""
        state = {"done": False}

        def finish(pw):
            if state["done"]:
                return
            state["done"] = True
            cb(pw)

        dlg = Adw.Window(transient_for=self, modal=True, title="sudo password")
        dlg.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        msg = Gtk.Label(
            label="This step needs root. Enter your sudo password so Chuck can run "
                  "it — kept in memory for this chat only, never written or logged.",
            xalign=0, wrap=True)
        box.append(msg)
        cl = Gtk.Label(label=(script or "").strip()[:200], xalign=0, wrap=True,
                       selectable=True)
        cl.add_css_class("mono")
        box.append(cl)
        pw = Gtk.PasswordEntry()
        try:
            pw.set_show_peek_icon(True)
            pw.set_property("placeholder-text", "sudo password")
        except Exception:
            pass
        box.append(pw)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        go = Gtk.Button(label="Authenticate & run")
        go.add_css_class("suggested-action")
        row.append(cancel)
        row.append(go)
        box.append(row)
        hb = Adw.HeaderBar()
        wrap = Adw.ToolbarView()
        wrap.add_top_bar(hb)
        wrap.set_content(box)
        dlg.set_content(wrap)

        def do_go(*_):
            p = pw.get_text()
            dlg.close()
            finish(p or None)

        def do_cancel(*_):
            dlg.close()
            finish(None)

        go.connect("clicked", do_go)
        cancel.connect("clicked", do_cancel)
        pw.connect("activate", do_go)
        dlg.connect("close-request", lambda *_: (finish(None), False)[1])
        try:
            dlg.set_resizable(False)
        except Exception:
            pass
        dlg.present()
        # Explicit focus: with a modal presented over a busy window the entry
        # does not reliably get the keyboard grab, which looks identical to a
        # dialog you "can't type into".
        try:
            pw.grab_focus()
        except Exception:
            pass

    def _enter_await(self):
        """A modal is open. Depth-counted: a plain bool let the FIRST waiter to
        finish clear the flag while others were still blocked, un-pausing the
        watchdog mid-prompt."""
        with self._await_lock:
            self._await_depth += 1
            self._awaiting_input = True

    def _exit_await(self):
        with self._await_lock:
            self._await_depth = max(0, self._await_depth - 1)
            self._awaiting_input = self._await_depth > 0

    def _get_sudo_pw(self, script):
        """Return a usable sudo password: the cached one, or prompt for it ONCE.

        SINGLE FLIGHT. This used to be a bare check-then-prompt with no guard, so
        every worker thread that needed root checked the cache at the same
        moment, all saw it empty, and all called GLib.idle_add on their own
        dialog. Fifteen commands meant fifteen stacked modals, all transient for
        the same window, contending for the same keyboard grab — which is why
        none of them could be typed into.

        Now exactly one caller opens a dialog. Everyone else blocks on the
        condition variable and takes that dialog's answer. If the user cancels,
        the refusal is remembered for the rest of the turn and later callers get
        None immediately rather than being asked again.
        """
        if self._sudo_pw_valid():
            return self._sudo_pw
        with self._sudo_cv:
            while True:
                if self._sudo_pw_valid():
                    return self._sudo_pw
                if self._sudo_denied:
                    return None                 # asked once this turn, refused
                if not self._sudo_asking:
                    self._sudo_asking = True    # this thread owns the prompt
                    break
                # someone else is asking — wait for THEIR dialog, don't open one
                if not self._sudo_cv.wait(320):
                    return None

        box = {"pw": None}
        ev = threading.Event()

        def show():
            self._sudo_prompt(script, lambda p: (box.__setitem__("pw", p), ev.set()))
            return False

        self._enter_await()
        try:
            GLib.idle_add(show)
            ev.wait(300)              # bounded: if they walk away, give up rather than hang
        finally:
            self._exit_await()
            with self._sudo_cv:
                self._sudo_asking = False
                if box["pw"]:
                    self._cache_sudo_pw(box["pw"])
                else:
                    self._sudo_denied = True
                self._sudo_cv.notify_all()   # release everyone waiting on this answer
        return box["pw"]

    def _execute_shell(self, script, execute):
        """Run a shell script through `execute(password) -> (rc, out)`, collecting
        the sudo password first if the script needs root and the box isn't already
        passwordless, and re-prompting once if a cached password was rejected.
        Returns (rc, out); rc 97 means sudo could not authenticate.

        SERIALISED. One command runs at a time, process-wide. Nothing here is
        safe to overlap: two commands racing means two sudo prompts, interleaved
        output, and a user who has to approve things in an order nobody chose.
        A queued command also picks up the password the one ahead of it already
        collected, so a turn asks once and only once.
        """
        with self._exec_lock:
            started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            pw = None
            if command_needs_sudo(script) and not _sudo_ready():
                pw = self._get_sudo_pw(script)
                if pw is None and not _sudo_ready():
                    return _SUDO_AUTH_FAILED, ("sudo: cancelled \u2014 not running this. "
                                               "Nothing was changed.")
            rc, out = execute(pw)
            if rc == _SUDO_AUTH_FAILED and pw is not None:   # the password was wrong
                self._clear_sudo_pw()
                with self._sudo_cv:
                    self._sudo_denied = False    # a WRONG password earns one retry
                pw2 = self._get_sudo_pw(script)
                if pw2:
                    rc, out = execute(pw2)
            # Every command that actually ran, recorded — regardless of outcome,
            # and regardless of whether the model ever reports it honestly.
            if _ledger is not None:
                _ledger.record(script, rc, out, kind="shell",
                               chat_id=getattr(self, "chat_id", ""), started=started)
            return rc, out

    def _run_card(self, cmd, run_btn, status, gate_text=None, sink=None):
        """Run a gated (CRITICAL) command card once the user has armed the risk
        checkbox and pressed Run. Disarms the button so a second click can't
        double-fire it, then hands off to the same execute-and-report path plain
        commands use. gate_text was only needed to classify the risk tier up
        front — at run time the command itself is what executes.
        """
        if not run_btn.get_sensitive():
            return
        run_btn.set_sensitive(False)
        self._run_now(cmd, status, sink=sink)

    def _run_now(self, cmd, status, sink=None):
        """Execute, show the output, and hand the REAL result back to the model.

        This is the whole point: he does not get to claim success. The exit code
        and output go back, and a non-zero exit is reported as a failure he must
        fix before taking another step.
        """
        # A gated card can be approved AFTER the turn that proposed it has already
        # ended. Executing it feeds the result back into the model and resumes
        # streaming, so the run lifecycle (Stop button + stuck-watchdog + the
        # "working" indicator) must be live again. _start_run is a no-op when a
        # turn is already active, so this is safe on the normal auto-run path too.
        self._start_run()
        self._dirty = set()             # something is being run: the changes get exercised
        self._set_status(status, "running", "run")
        self._busy(True)
        self._arm_run_budget(cmd)

        def work():
            live = _LiveFeed(self, sink)
            _LIVE_OUT.fn = live.push
            try:
                rc, out = self._execute_shell(cmd, lambda pw: run_command(cmd, sudo_password=pw))
            finally:
                _LIVE_OUT.fn = None
            auth_failed = (rc == _SUDO_AUTH_FAILED)

            def show():
                live.close()
                if auth_failed:
                    self._set_status(status, "sudo auth failed", "fail")
                else:
                    self._set_status(status, *(("stopped", "wait") if self._cancelled and rc < 0
                                              else (f"exit {rc}", "ok" if rc == 0 else "fail")))
                self._show_output(out, rc, sink)
                return False
            GLib.idle_add(show)

            if auth_failed:
                return (f"COULD NOT RUN `{cmd}` \u2014 sudo authentication failed. {out}\n"
                        "Do NOT retry the same sudo command blindly. Root access is needed "
                        "(a wrong/absent password or a sudoers policy); tell the user and "
                        "either wait for them or suggest they run it in a terminal.")
            if rc == 0:
                return (f"RAN `{cmd}` \u2014 SUCCEEDED (exit 0). Real output:\n{clip_output(out, 20000)}{_shell_cwd_note()}\n"
                        "Verify this output actually shows what you needed. If the whole task "
                        "is done, give the final answer; otherwise take the NEXT single step.")
            return (f"RAN `{cmd}` \u2014 FAILED (exit {rc}). Real output:\n{clip_output(out, 20000)}{_shell_cwd_note()}\n"
                    "Do NOT move on. Read the error, fix the cause, and either retry the "
                    "corrected command or explain why it can't work.")
        self._pending_tools += 1
        self._tool_thread(work, f"`{cmd[:40]}`")

    def _code_card(self, lang, body, verified=False):
        """Run a code block Chuck wrote and report the real result.

        Same rule as commands: it executes. Only the CRITICAL tier waits for a
        deliberate tick.
        """
        if lang in ("bash", "sh"):
            fixed = enforce_syu(body)
            if fixed != body:
                self._sys_note("using -Syu (a plain -S risks a partial upgrade)", "dim")
            body = add_noconfirm(fixed)
        tier = classify_command(body)

        card, chead, cbody, foot = self._card_shell(
            "PYTHON SESSION" if lang == "py" and _pysession is not None else lang.upper(),
            lambda: body)
        if verified:
            vt = Gtk.Label(label="\u2713 verified")
            vt.add_css_class("verified")
            chead.insert_child_after(vt, chead.get_first_child())
        ct = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True)
        ct.add_css_class("cmd-text"); _wrap_anywhere(ct); cbody.append(ct)
        status = self._pill("queued", "wait")

        if tier == "critical":
            run = Gtk.Button(label="Run"); run.add_css_class("gold")
            self._risk_gate(foot, run, body, "code")

            def go(_b):
                if not run.get_sensitive():
                    return
                run.set_sensitive(False)
                self._run_code_now(lang, body, status, sink=foot)
            self._set_status(status, "needs approval", "wait")
            run.connect("clicked", go)
            rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            rw.append(run); rw.append(status); foot.append(rw)
            self._log_pin(card)
            self._tool_feedback.append(
                f"[that {lang} block is CRITICAL and is waiting for the user to confirm it. "
                "Do not assume it ran.]")
            return

        if tier == "danger":
            wl = Gtk.Label(label="\u26a0 destructive \u2014 running it now", xalign=0)
            wl.add_css_class("danger"); foot.append(wl)
        chead.insert_child_after(status, chead.get_last_child() if verified
                                 else chead.get_first_child())
        self._log_pin(card)
        self._run_code_now(lang, body, status, sink=foot)

    def _run_code_now(self, lang, body, status, sink=None):
        # Same reason as _run_now: a CRITICAL code card can be approved after its
        # turn has ended, and running it resumes the model — so re-arm the run
        # lifecycle (Stop + watchdog). No-op when a turn is already active.
        self._start_run()
        self._dirty = set()
        self._set_status(status, "running", "run")
        self._busy(True)
        self._arm_run_budget(body)

        def work():
            live = _LiveFeed(self, sink)
            _LIVE_OUT.fn = live.push
            try:
                plots = []
                if lang in ("bash", "sh"):
                    rc, out = self._execute_shell(
                        body, lambda pw: run_code(lang, body, sudo_password=pw))
                elif lang == "py" and _pysession is not None:
                    rc, out, plots = self._run_py(body, live.push)
                else:
                    rc, out = run_code(lang, body)
            finally:
                _LIVE_OUT.fn = None
            auth_failed = (rc == _SUDO_AUTH_FAILED)
            for pth in plots[:4]:
                pay = image_payload(pth)
                if pay:
                    self._tool_images.append(pay)

            def show():
                live.close()
                if auth_failed:
                    self._set_status(status, "sudo auth failed", "fail")
                else:
                    self._set_status(status, *(("stopped", "wait") if self._cancelled and rc < 0
                                              else (f"exit {rc}", "ok" if rc == 0 else "fail")))
                self._show_output(out, rc, sink)
                self._show_plots(plots[:4], sink)
                return False
            GLib.idle_add(show)

            if auth_failed:
                return (f"COULD NOT RUN that {lang} \u2014 sudo authentication failed. {out}\n"
                        "Do NOT blindly retry. Root access is needed; tell the user.")
            if lang == "py" and _pysession is not None:
                return self._py_feedback(rc, out, plots)
            if rc == 0:
                return (f"RAN that {lang} \u2014 SUCCEEDED (exit 0). Real output:\n{clip_output(out, 20000)}{_shell_cwd_note()}\n"
                        "Check the output is actually what you needed before moving on.")
            return (f"RAN that {lang} \u2014 FAILED (exit {rc}). Real output:\n{clip_output(out, 20000)}{_shell_cwd_note()}\n"
                    "Do NOT move on. Fix the cause and retry.")
        self._pending_tools += 1
        self._tool_thread(work, f"{lang} block")

    # ── the ```py``` session: one interpreter per chat ──
    def _py_session(self):
        if self._py is None:
            self._py = _pysession.PySession(
                cwd=_cwd(), plots_dir=str(CONFIG_DIR), env=dict(os.environ, **_RUN_ENV),
                python=shutil.which("python3") or sys.executable, preexec=_sandbox_preexec)
        return self._py

    def _py_close(self):
        if self._py is not None:
            try:
                self._py.close()
            except Exception:
                pass
            self._py = None

    def _py_names(self):
        return list(self._py.names) if self._py is not None else []

    def _run_py(self, body, on_chunk):
        """-> (rc, output, plot paths)."""
        sess = self._py_session()
        if body.strip() in ("%reset", "reset()"):
            self._py_close()
            return 0, "(the Python session was reset: no variables)", []
        timeout = estimate_runtime(body)["hard_timeout_seconds"]
        r = sess.run(body, timeout=timeout, on_chunk=on_chunk,
                     should_stop=lambda: self._cancelled)
        out = r["out"] if r["out"].strip() else "(no output)"
        if r["restarted"]:
            out = ("(the Python session had ended, so this ran in a FRESH one: variables "
                   "from earlier blocks are gone)\n" + out)
        if r["timed_out"]:
            out += f"\n(stopped after {timeout}s)"
        if r["interrupted"] and self._cancelled and not r["timed_out"]:
            return -2, out, r["plots"]           # Stop, not a failure: the card says "stopped"
        rc = 0 if r["ok"] else (124 if r["timed_out"] else
                                (r["exit"] if isinstance(r["exit"], int) and r["exit"] > 0 else 1))
        return rc, out, r["plots"]

    def _py_feedback(self, rc, out, plots):
        names = self._py_names()
        tail = (f"\nSession variables: {', '.join(names[:40])}" if names else "")
        if plots:
            tail += (f"\n{len(plots)} figure(s) saved and shown to the user"
                     + (" \u2014 the IMAGE is attached; look at it before going on" if
                        self._tool_images else "") + ": " + ", ".join(plots[:4]))
        if rc == 0:
            return (f"RAN that py block in the persistent session \u2014 SUCCEEDED. Real output:\n"
                    f"{clip_output(out, 20000)}{tail}\nVariables stay defined for the next ```py``` block.")
        return (f"RAN that py block \u2014 FAILED. Real output:\n{clip_output(out, 20000)}{tail}\n"
                "Do NOT move on. Fix the cause and run the corrected block (earlier variables "
                "are still defined unless the output says the session restarted).")

    def _show_plots(self, plots, sink):
        for pth in plots:
            try:
                w = self._build_image_widget(pth, None)
                if sink is not None:
                    sink.append(w)
            except Exception:
                pass
        return False

    def _tool_thread(self, work, label):
        """Run an async tool worker so it can NEVER strand the tool loop.

        Every counted tool increments _pending_tools; if its worker thread dies
        on an exception the matching _tool_done is never called, the counter
        never reaches zero, and the whole turn hangs until the watchdog kills it
        ("no progress — looks stuck"). One helper, so the guarantee can't be
        forgotten the next time a tool is added.

        `work` returns the feedback string; anything it raises is reported and
        the slot is still released.
        """
        gen = self._gen

        def runner():
            fb = None
            try:
                fb = work()
            except Exception as exc:
                # bind to a plain local: `except ... as exc` unbinds exc when the
                # block exits, which would break the closure below.
                why = str(exc)
                if _crashlog is not None:
                    _crashlog.record(f"tool {label}", traceback.format_exc())
                fb = f"[{label} failed: {why}]"

                def note(w=why):
                    self._sys_note(f"\u2717 {label} failed: {w}", "danger")
                    return False
                GLib.idle_add(note)
            finally:
                self._busy(False)

                def finish(text=fb):
                    self._tool_done(text if text is not None else f"[{label}: no result]",
                                    gen=gen)
                    return False
                GLib.idle_add(finish)
        threading.Thread(target=runner, daemon=True).start()

    def _do_images(self, query):
        self._sys_note(f"\U0001F5BC images for \u201c{query}\u201d")
        self._busy(True)
        self._activity_start("Images")

        def work():
            # Search AND download off the UI thread; downloads run in parallel.
            urls = image_search(query)
            got = []
            if urls:
                def grab_one(item):
                    i, u = item
                    if self._cancelled:
                        return None
                    si = self._activity_step(f"fetching image {i}/{len(urls)}")
                    p = download_image(u)
                    self._activity_done(si, f"image {i}/{len(urls)}", ok=bool(p))
                    return (p, u) if p else None
                ex = ThreadPoolExecutor(max_workers=4)
                try:
                    for r in ex.map(grab_one, list(enumerate(urls, 1))):
                        if r:
                            got.append(r)
                finally:
                    ex.shutdown(wait=False)

            def show():
                self._activity_end()
                if not self._cancelled:
                    if not got:
                        self._sys_note("No images came back (search blocked, offline, "
                                       "or nothing found).", "danger")
                    else:
                        for p, u in got:
                            self._image_bubble(p, u)
                return False
            GLib.idle_add(show)
            return f"[images '{query}': showed {len(got)} picture(s) to the user]"
        self._tool_thread(work, f"image search '{query}'")

    def _do_video_search(self, query):
        self._sys_note(f"\U0001F3AC videos for \u201c{query}\u201d")
        self._busy(True)

        def work():
            rows = video_search(query)

            def show():
                if not self._cancelled:
                    if not rows:
                        self._sys_note("No videos found.", "danger")
                    else:
                        # video_search returns (title, url, snippet) — the same
                        # 3-tuple shape as web_search. Unpacking two here meant
                        # every ```videos``` block died with a ValueError before
                        # a single card was drawn.
                        for (title, url, _snip) in rows:
                            self._video_card(title, url)
                return False
            GLib.idle_add(show)
            if not rows:
                return f"[videos '{query}': nothing found]"
            listing = "; ".join(f"{t} {u}" for t, u, _s in rows[:6])
            return f"[videos '{query}': showed {len(rows)} to the user \u2014 {listing}]"
        self._tool_thread(work, f"video search '{query}'")

    def _video_card(self, title, url):
        card, _h, body, foot = self._card_shell("VIDEO", lambda: url)
        t = Gtk.Label(label=title or url, xalign=0, wrap=True, selectable=True)
        t.add_css_class("cmd-text"); body.append(t)
        u = Gtk.Label(label=url, xalign=0, wrap=True, selectable=True)
        u.add_css_class("dim"); body.append(u)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        openb = Gtk.Button(label="Open in Brave"); openb.add_css_class("quick")
        openb.connect("clicked", lambda *_: open_in_brave(url))
        dlb = Gtk.Button(label="Download"); dlb.add_css_class("gold")
        dlb.connect("clicked", lambda *_: self._do_video(url))
        rw.append(openb); rw.append(dlb); foot.append(rw)
        self._log_pin(card)

    def _do_video(self, url):
        if not url.startswith("http"):
            return
        if not shutil.which("yt-dlp"):
            self._sys_note("yt-dlp isn't installed:"); self._command_card("sudo pacman -Syu --needed yt-dlp"); return
        self._sys_note(f"\u2B07 downloading {url}")
        self._busy(True); self._live("\u2B07 downloading video\u2026")

        def worker():
            cmd = ["yt-dlp", "-o", str(DL_DIR / "%(title)s.%(ext)s")]
            proxy = (self.settings.get("proxy") or "").strip()
            if proxy:
                cmd += ["--proxy", proxy]
            cmd.append(url)
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                msg = "\u2713 saved to " + str(DL_DIR) if p.returncode == 0 else \
                      "download failed:\n" + (p.stderr or "")[-800:]
            except Exception as ex:
                msg = f"error: {ex}"
            self._live(""); self._busy(False)
            GLib.idle_add(self._sys_note, msg, "ok" if msg.startswith("\u2713") else "danger")
        threading.Thread(target=worker, daemon=True).start()

    def _save_skill(self, block):
        """Parse a ```skill``` block and persist it as a smart file."""
        if not _skills:
            self._sys_note("skills unavailable (chucknorris_ext not installed).", "danger"); return
        name = lang = "" ; desc = ""; body = ""
        head, _, rest = block.partition("---")
        for line in head.splitlines():
            k, _, v = line.partition(":")
            k = k.strip().lower(); v = v.strip()
            if k == "name": name = v
            elif k == "lang": lang = v
            elif k in ("desc", "description"): desc = v
        body = rest.strip()
        if not name or not body:
            self._sys_note("skill needs a name and a body.", "danger"); return
        ok, msg, run_cmd = _skills.skill_write(name, lang or "bash", body, desc)
        self._sys_note(("\U0001F4BE " if ok else "\u26a0 ") + msg, "ok" if ok else "danger")
        if ok and run_cmd:
            self._sys_note("\u2192 run it now:"); self._command_card(run_cmd, gate_text=body)

    def _run_skill(self, name):
        if not _skills:
            self._sys_note("skills unavailable.", "danger"); return
        cmd = _skills.skill_run_cmd(name)
        if not cmd:
            self._sys_note(f"no saved skill '{name}'.", "danger"); return
        body = ""
        try:
            _lang, body = _skills.skill_read(name)
        except Exception:
            body = ""
        self._sys_note(f"\u25B6 skill '{name}':")
        self._command_card(cmd, gate_text=body or None)

    def _do_junk(self):
        self._sys_note("\U0001F9F9 scanning for junk\u2026")
        self._busy(True)

        def work():
            report, cmds = junk_scan()

            def show():
                if not self._cancelled:
                    self._sys_note(report, "mono")
                    for label, cmd in cmds:
                        self._sys_note("\u2192 " + label)
                        self._command_card(cmd)
                return False
            GLib.idle_add(show)
            return f"[junk scan done \u2014 offered {len(cmds)} cleanup command(s)]"
        self._tool_thread(work, "junk scan")

    def _do_ledger(self):
        """Show the tamper-evident command record, and check its chain.

        The ledger has been written on every command since 12.0.3, but nothing
        could ever display it — ledger.summary() had a docstring promising a
        ```ledger``` request that did not exist, so the whole subsystem wrote
        evidence nobody could read. A record you cannot inspect is not evidence.
        """
        if not _ledger:
            self._sys_note("ledger unavailable.", "danger")
            self._tool_done("[ledger: module not installed]")
            return
        self._sys_note("\U0001F4D3 evidence ledger", "dim")
        self._busy(True)

        def work():
            text = _ledger.summary(limit=15)
            ok, count, bad, msg = _ledger.verify()

            def show():
                self._sys_note(text, "mono")
                self._sys_note(("\u2713 " if ok else "\u26a0 ") + msg,
                               "ok" if ok else "danger")
                return False
            GLib.idle_add(show)
            return (f"[ledger: {count} commands recorded; chain "
                    f"{'VERIFIED' if ok else f'BROKEN at entry {bad}'} — {msg}]\n"
                    + text[:4000])
        self._tool_thread(work, "ledger")

    _RANGE_RE = re.compile(r"^(.*?)(?::|\s+)(\d+)(?:\s*-\s*(\d+))?\s*$")

    def _do_read(self, path):
        """Read a file (or a line range: ```read src/app.py:120-400```) and feed
        it back into the run. Big files come back in pages with an exact
        pointer to the next page, never silently cut. A directory is listed;
        an image path is SHOWN in the chat. Relative paths resolve in the
        working folder."""
        spec = (path or "").strip().strip("'\"")
        start = end = None
        m = self._RANGE_RE.match(spec)
        if m and not os.path.exists(os.path.expanduser(spec)):
            spec, start, end = m.group(1).strip(), int(m.group(2)), (
                int(m.group(3)) if m.group(3) else None)
        root = self._workroot()
        full = os.path.expanduser(spec)
        if not os.path.isabs(full):
            # relative: the working folder, or home when there is none — never
            # wherever the app happened to be launched from
            full = str((root if root is not None else Path.home()) / full)
        if _is_image_path(spec) and os.path.isfile(full):
            self._sys_note(f"\U0001F5BC {os.path.basename(full)}")
            self._image_bubble(full)
            self._tool_done(f"[showed the image {spec} to the user]")
            return
        rng = f":{start}-{end or ''}" if start else ""
        self._sys_note(f"\U0001F4C4 read {spec}{rng}"); self._busy(True)
        gen = self._gen

        def worker():
            if os.path.isfile(full) and _builder:
                ok, content, footer = _builder.read_lines(full, start, end, label=spec)
                if ok:
                    content = f"FILE {spec}{rng}:\n{content}" + (f"\n{footer}" if footer else "")
            else:
                ok, content = read_file_safe(full)
                if ok:
                    content = f"FILE {spec}:\n{content[:48000]}"

            def show():
                self._busy(False)
                if gen != self._gen:
                    return False
                if not ok:
                    self._sys_note(content, "danger")
                    self._tool_done(f"[read {spec}: {content}]")
                else:
                    self._tool_done(content)
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_map(self):
        """The repo map: every file with what it defines, in one cheap call."""
        root = self._workroot()
        if root is None or not _builder:
            self._tool_done("[map: no working folder — pick one first (Ctrl+O) or open a project]")
            return
        self._sys_note(f"\U0001F5FA mapping {_short_path(root)}"); self._busy(True)

        def work():
            ok, out = _builder.repo_map(root)
            return f"REPO MAP of {root}:\n{out}" if ok else f"[map failed: {out}]"
        self._tool_thread(work, "repo map")

    # ── sub-agents ──────────────────────────────────────────────────────────
    def _sub_tools(self):
        """The read-only toolbox a sub-agent gets — the same implementations
        the main agent uses, minus anything that writes or executes."""
        root = self._workroot()
        n_src = self.cfg('research_sources', RESEARCH_MAX_SOURCES, 1, 10)
        f_timeout = self.cfg('fetch_timeout', 6, 3, 30)

        def search(q):
            rows = web_search(q, n=max(4, n_src)) or []
            return "\n".join(f"- {t}  {u}\n  {sn[:240]}" for t, u, sn in rows) or "no results"

        def fetch(u):
            return self._fetch_text(u.split()[0], f_timeout)[0] or "(empty or unreachable)"

        def read(spec):
            spec = spec.strip().strip("'\"")
            start = end = None
            m = self._RANGE_RE.match(spec)
            if m and not os.path.exists(os.path.expanduser(spec)):
                spec, start = m.group(1).strip(), int(m.group(2))
                end = int(m.group(3)) if m.group(3) else None
            full = os.path.expanduser(spec)
            if not os.path.isabs(full) and root is not None:
                full = str(root / full)
            if os.path.isdir(full):
                return read_file_safe(full)[1]
            ok, txt, foot = _builder.read_lines(full, start, end, max_chars=24000, label=spec)
            return (txt + ("\n" + foot if foot else "")) if ok else txt

        def grep(body):
            r, sub, pat = self._grep_args(body)
            if r is None:
                return "no working folder"
            return _builder.grep(r, pat, sub)[1]

        def glob(pat):
            return _builder.glob_files(root, pat)[1] if root else "no working folder"

        def map_(_arg):
            return _builder.repo_map(root, 12000)[1] if root else "no working folder"
        return {"search": search, "fetch": fetch, "read": read, "grep": grep,
                "glob": glob, "map": map_}

    def _sub_call(self):
        """A blocking model call for a sub-agent. Each gets its own Backend so
        concurrent calls never overwrite each other's usage record."""
        gen = self._gen

        def call(messages):
            if self._over_cap():
                # sub-agents spend real money too: eight of them must not run
                # on past the cap while the main loop waits to ask
                raise RuntimeError("the chat's spending cap is reached")
            b = Backend(self.settings) if isinstance(self.backend, Backend) else self.backend
            out, err = [], []
            stale = lambda: self._cancelled or gen != self._gen   # noqa: E731
            if isinstance(b, Backend):
                # a slow sub-agent call is still progress: without this a model
                # thinking for two minutes tripped the stuck-watchdog
                b.on_think = lambda _c: self._note_progress()

            def delta(c):
                out.append(c)
                self._note_progress()
            b.stream(messages, delta, lambda: None, err.append,
                     should_stop=stale, on_open=self._note_progress)
            if err:
                raise RuntimeError(err[0])
            sent = sum(len(m.get("content") or "") for m in messages
                       if isinstance(m.get("content"), str))
            u, txt = getattr(b, "last_usage", None), "".join(out)
            mdl = b.model() if hasattr(b, "model") else None
            GLib.idle_add(lambda: (self._account(sent, len(txt), usage=u, model=mdl), False)[1])
            self._note_progress()
            return txt
        return call

    def _do_tasks(self, briefs):
        """Fan briefs out to parallel sub-agents, show each one's progress live,
        and hand the reports back as one tool result."""
        rows = self._subagent_card(briefs)
        n_par = self.cfg("subagents", 4, 1, 8)
        steps = self.cfg("subagent_steps", 8, 2, 20)
        ctx = _now_line() + (f"\nWorking folder: {self.workspace}" if self.workspace else "")
        tools = self._sub_tools()
        call = self._sub_call()
        gen = self._gen
        self._busy(True)

        def one(i, brief):
            GLib.idle_add(self._sub_update, rows[i], "running", "starting")

            def ev(txt):
                self._note_progress()
                GLib.idle_add(self._sub_update, rows[i], "running", txt)
            try:
                rep = _agents.run_subagent(
                    brief, call, tools, max_steps=steps, on_event=ev, context=ctx,
                    should_stop=lambda: self._cancelled or gen != self._gen)
                GLib.idle_add(self._sub_update, rows[i], "done", "", rep)
            except Exception as ex:
                rep = f"(sub-agent failed: {ex})"
                GLib.idle_add(self._sub_update, rows[i], "fail", str(ex)[:80], rep)
            return rep

        def work():
            reports = _agents.run_parallel(briefs, one, max_workers=n_par)
            parts = [f"### Sub-agent {i + 1}: {b.strip().splitlines()[0][:100]}\n{r}"
                     for i, (b, r) in enumerate(zip(briefs, reports, strict=False))]
            return ("SUB-AGENT REPORTS (they only read; nothing was changed):\n\n"
                    + _clip_feedback(parts, 40000))
        self._tool_thread(work, "sub-agents")

    def _subagent_card(self, briefs):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("cmd-card")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.add_css_class("card-head")
        kl = Gtk.Label(label=f"SUB-AGENTS \u00b7 {len(briefs)} IN PARALLEL", xalign=0,
                       hexpand=True)
        kl.add_css_class("card-kind")
        head.append(kl)
        card.append(head)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.add_css_class("card-body")
        card.append(body)
        rows = []
        for i, brief in enumerate(briefs):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            r.add_css_class("sub-row")
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            mark = Gtk.Label(label="\u25cb"); mark.add_css_class("plan-box")
            title = Gtk.Label(label=brief.strip().splitlines()[0][:140] if brief.strip()
                              else f"task {i + 1}", xalign=0, hexpand=True)
            title.add_css_class("sub-title")
            try:
                title.set_ellipsize(Pango.EllipsizeMode.END)
            except Exception:
                pass
            pill = self._pill("queued", "wait")
            top.append(mark); top.append(title); top.append(pill)
            r.append(top)
            act = Gtk.Label(label="", xalign=0); act.add_css_class("sub-act")
            try:
                act.set_ellipsize(Pango.EllipsizeMode.END)
            except Exception:
                pass
            r.append(act)
            exp = Gtk.Expander(label="Report"); exp.add_css_class("think")
            rl = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
            _wrap_anywhere(rl); rl.add_css_class("think-body"); rl.set_margin_top(4)
            exp.set_child(rl); exp.set_visible(False)
            r.append(exp)
            body.append(r)
            rows.append({"mark": mark, "pill": pill, "act": act, "exp": exp, "rep": rl})
        self._log_pin(card)
        return rows

    def _sub_update(self, row, state, activity="", report=None):
        if state == "running":
            row["mark"].set_label("\u25b8"); self._set_status(row["pill"], "working", "run")
            row["mark"].remove_css_class("plan-box"); row["mark"].add_css_class("plan-cur")
        elif state == "done":
            row["mark"].set_label("\u2713"); self._set_status(row["pill"], "done", "ok")
            row["mark"].remove_css_class("plan-cur"); row["mark"].add_css_class("plan-tick")
        elif state == "fail":
            row["mark"].set_label("\u2717"); self._set_status(row["pill"], "failed", "fail")
        row["act"].set_label(activity)
        row["act"].set_visible(bool(activity))
        if report is not None:
            row["rep"].set_text(report[:20000])
            row["exp"].set_visible(True)
        return False

    # ── background jobs: servers, watchers, anything that doesn't return ────
    JOB_DIR = CONFIG_DIR / "jobs"

    def _do_bg(self, body):
        """```bg <name>\n<command>``` (or ```bg <command>```): start it in the
        working folder, detached, logging to a file. A dev server, a watcher,
        a long build — then ```logs``` to read it and ```kill``` to stop it."""
        lines = [ln for ln in (body or "").strip().splitlines() if ln.strip()]
        if not lines:
            self._tool_feedback.append("[bg: no command given]")
            return
        if len(lines) > 1 and re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", lines[0].strip()):
            name, cmd = lines[0].strip(), "\n".join(lines[1:])
        else:
            cmd = "\n".join(lines)
            base = re.sub(r"[^A-Za-z0-9_.-]", "", os.path.basename(cmd.split()[0]))[:20] or "job"
            name, n = base, 2
            while name in self._jobs and self._job_alive(name):
                name, n = f"{base}{n}", n + 1
        if self._job_alive(name):
            self._tool_feedback.append(f"[bg: a job called '{name}' is already running — "
                                       "```kill " + name + "``` first or pick another name]")
            return
        if classify_command(cmd) == "critical":
            self._tool_feedback.append("[bg refused: that command is CRITICAL and needs the "
                                       "user's approval — run it as a normal ```bash``` step]")
            return
        if command_needs_sudo(cmd) and not _sudo_ready():
            self._tool_feedback.append("[bg refused: it needs root, and a background job can't "
                                       "ask for the password — run it as a normal ```bash``` "
                                       "step]")
            return
        try:
            self.JOB_DIR.mkdir(parents=True, exist_ok=True)
            log = self.JOB_DIR / f"{name}.log"
            fh = open(log, "wb")
            env = dict(os.environ); env.update(_RUN_ENV)
            proc = subprocess.Popen([_bash_bin(), "-c", cmd], cwd=_cwd(), stdin=subprocess.DEVNULL,
                                    stdout=fh, stderr=subprocess.STDOUT, env=env,
                                    start_new_session=True)
            fh.close()
        except Exception as ex:
            self._tool_feedback.append(f"[bg failed to start: {ex}]")
            return
        job = {"proc": proc, "log": log, "cmd": cmd, "name": name, "card": None,
               "started": time.time()}
        self._jobs[name] = job
        if _ledger is not None:
            try:
                _ledger.record("[bg] " + cmd, 0, f"started pid {proc.pid}", kind="job",
                               chat_id=self.chat_id)
            except Exception:
                pass
        job["card"] = self._job_card(job)
        self._dirty = set()
        if not self._jobs_timer:
            self._jobs_timer = GLib.timeout_add_seconds(2, self._tick_jobs)
        # give it a moment: most broken servers die in the first second
        self._busy(True)

        def work():
            time.sleep(1.5)
            rc = proc.poll()
            tail = self._log_tail(log, 25)
            if rc is None:
                return (f"[job '{name}' is RUNNING (pid {proc.pid}) in {_cwd() or '~'}. "
                        f"First output:\n{tail or '(nothing yet)'}\nRead more with ```logs "
                        f"{name}```, stop it with ```kill {name}```.]")
            return (f"[job '{name}' EXITED immediately with code {rc}. Output:\n{tail}\n"
                    "Fix the cause before starting it again.]")
        self._pending_tools += 1                      # released by _tool_thread
        self._tool_thread(work, f"bg {name}")

    def _job_alive(self, name):
        j = self._jobs.get(name)
        return bool(j) and j["proc"].poll() is None

    @staticmethod
    def _log_tail(path, n=120, limit=12000):
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 64 * 1024))
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            return ""
        return clip_output("\n".join(data.splitlines()[-n:]), limit)

    def _do_logs(self, name):
        name = (name or "").strip()
        if not name or name not in self._jobs:
            listing = ", ".join(f"{k} ({'running' if self._job_alive(k) else 'exited'})"
                                for k in self._jobs) or "none"
            self._tool_feedback.append(f"[logs: no job '{name}'. Jobs: {listing}]")
            return
        j = self._jobs[name]
        rc = j["proc"].poll()
        state = "RUNNING" if rc is None else f"EXITED with code {rc}"
        self._sys_note(f"\U0001F4DC logs {name}", "dim")
        self._tool_feedback.append(f"[job '{name}' {state}. Last output:\n"
                                   f"{self._log_tail(j['log']) or '(empty)'}]")

    def _kill_job(self, name):
        j = self._jobs.get(name)
        if not j:
            return f"no job '{name}'"
        if j["proc"].poll() is None:
            try:
                os.killpg(os.getpgid(j["proc"].pid), signal.SIGTERM)
                try:
                    j["proc"].wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(j["proc"].pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._update_job_card(j)
            return f"stopped '{name}'"
        return f"'{name}' had already exited (code {j['proc'].poll()})"

    def _do_kill(self, name):
        name = (name or "").strip()
        msg = self._kill_job(name)
        self._sys_note("\u25A0 " + msg, "dim")
        self._tool_feedback.append(f"[kill: {msg}]")

    def _job_card(self, job):
        card, head, body, foot = self._card_shell("JOB \u00b7 " + job["name"].upper(),
                                                  lambda j=job: j["cmd"])
        prompt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dollar = Gtk.Label(label="&", xalign=0, valign=Gtk.Align.START)
        dollar.add_css_class("cmd-prompt")
        ct = Gtk.Label(label=job["cmd"], xalign=0, wrap=True, selectable=True, hexpand=True)
        ct.add_css_class("cmd-text"); _wrap_anywhere(ct)
        prompt.append(dollar); prompt.append(ct)
        body.append(prompt)
        pill = self._pill("running", "run")
        head.insert_child_after(pill, head.get_first_child())
        stop = Gtk.Button(label="Stop"); stop.add_css_class("ghost")
        stop.connect("clicked", lambda _b, n=job["name"]: (self._kill_job(n), None)[1])
        head.append(stop)
        info = {"pill": pill, "stop": stop}
        self._log_pin(card)
        return info

    def _update_job_card(self, job):
        c = job.get("card")
        if not c:
            return
        rc = job["proc"].poll()
        if rc is None:
            secs = int(time.time() - job["started"])
            self._set_status(c["pill"], f"running \u00b7 {secs // 60}m {secs % 60:02d}s"
                             if secs >= 60 else f"running \u00b7 {secs}s", "run")
        else:
            self._set_status(c["pill"], f"exited {rc}", "ok" if rc in (0, -15) else "fail")
            c["stop"].set_sensitive(False)

    def _tick_jobs(self):
        for j in list(self._jobs.values()):
            self._update_job_card(j)
        if not any(self._job_alive(n) for n in self._jobs):
            self._jobs_timer = 0
            return False
        return True

    def _stop_all_jobs(self):
        for name in list(self._jobs):
            self._kill_job(name)

    def _do_open(self, target):
        """Hand the user something: a URL in the browser, a file or folder in
        its default app."""
        t = (target or "").strip().strip("'\"")
        if not t:
            self._tool_feedback.append("[open: nothing given]")
            return
        if re.match(r"^https?://", t):
            ok = open_in_brave(t)
        else:
            full = os.path.expanduser(t)
            if not os.path.isabs(full):
                full = str((self._workroot() or Path.home()) / full)
            ok = os.path.exists(full) and _open_path(full)
            t = full
        self._sys_note(("\u2197 opened " if ok else "\u2717 couldn't open ") + t,
                       "dim" if ok else "danger")
        self._tool_feedback.append(f"[open: {'opened' if ok else 'could not open'} {t} "
                                   "for the user]")

    def _notify_done(self, disp):
        """A desktop notification when a long task finishes while you're in
        another window — the point of an agent is that you can look away."""
        try:
            if self.is_active() or time.time() - self._run_started < 20:
                return
            n = Gio.Notification.new("Chuck finished")
            first = next((ln.strip() for ln in (disp or "").splitlines() if ln.strip()), "Done.")
            n.set_body(re.sub(r"[*_`#>]", "", first)[:160])
            self.get_application().send_notification("chuck-done", n)
        except Exception:
            pass

    def _do_screenshot(self):
        """Chuck looks at the screen: capture it, show it in the chat, and
        attach it to his next request so he actually SEES what he built."""
        self._sys_note("\U0001F4F7 looking at the screen"); self._busy(True)
        self._dirty = set()

        def work():
            path = _scratch_png(".see_")
            if not capture_screen(path):
                return ("[screenshot failed: no capture tool (grim/spectacle/scrot). For a web "
                        "page use: chromium --headless --screenshot=/tmp/s.png --window-size=1280,800 URL, "
                        "then ```look /tmp/s.png```]")
            return self._see(path, "screen")
        self._tool_thread(work, "screenshot")

    def _do_look(self, path):
        """Chuck looks at an image file — a rendered page, a plot, a mockup."""
        spec = (path or "").strip().strip("'\"")
        full = os.path.expanduser(spec)
        root = self._workroot()
        if not os.path.isabs(full) and root is not None:
            full = str(root / full)
        self._sys_note(f"\U0001F441 looking at {spec}"); self._busy(True)

        def work():
            if not os.path.isfile(full):
                return f"[look: no such image: {spec}]"
            return self._see(full, spec)
        self._tool_thread(work, "look")

    def _see(self, path, label):
        pay = image_payload(path)
        if not pay:
            return f"[look: couldn't read {label} as an image]"
        self._tool_images.append(pay)
        GLib.idle_add(self._image_bubble, path)
        return (f"[IMAGE of {label} is attached to this message \u2014 look at it carefully and "
                "say what you see before deciding the next step]")

    def _do_glob(self, pattern):
        root = self._workroot()
        if root is None or not _builder:
            self._tool_done("[glob: no working folder — pick one first]")
            return
        pattern = (pattern or "").strip().splitlines()[0] if (pattern or "").strip() else ""
        self._sys_note(f"\U0001F50E files matching {pattern}"); self._busy(True)

        def work():
            ok, out = _builder.glob_files(root, pattern)
            return f"GLOB {pattern}:\n{out}" if ok else f"[glob failed: {out}]"
        self._tool_thread(work, "glob")

    def _do_check(self, lang, body, run_after=False):
        """Verify code (syntax+lint+security+tests). On clean: optionally show a
        Run card. On issues: feed the report back so Chuck fixes it — the card
        is withheld until the code is clean, so bad code never reaches a button."""
        if not run_after:
            self._sys_note(f"\U0001F50D verifying {lang}\u2026", "dim")
        self._busy(True)
        gen = self._gen

        def worker():
            try:
                res = _codecheck.check(lang, body, known_names=self._py_names() if lang == "py" else ())
                rep = _codecheck.report(res)
                clean = res.get("ok", False)
            except Exception as ex:
                res, rep, clean = None, f"(verifier error: {ex})", True  # fail open

            def show():
                self._busy(False)
                if gen != self._gen:
                    return False            # the turn this belonged to is over
                if clean:
                    if not run_after:
                        self._sys_note("\u2713 " + rep.split("\n")[0].lstrip("\u2713 "), "ok")
                    # Advisory findings no longer block the Run button, so they
                    # have to be VISIBLE — otherwise demoting them would just be
                    # hiding them. The user sees them next to the card.
                    adv = (res or {}).get("advisory") or []
                    for a in adv[:4]:
                        self._sys_note("\u26a0 " + a, "danger")
                    if run_after:
                        self._code_card(lang, body, verified=res is not None)
                    self._tool_done(f"[verify {lang}: clean]" +
                                    (" advisories: " + "; ".join(adv[:4]) if adv else ""))
                else:
                    self._sys_note("\u26a0 verification found issues \u2014 fixing", "danger")
                    if not run_after:
                        # explicit check: just report, no fix loop implied
                        self._tool_done("VERIFY REPORT:\n" + rep)
                    else:
                        # auto-verify before run: withhold the card, ask for a fix
                        self._tool_done(
                            "CODE VERIFICATION FAILED — do NOT show this to the user as-is. "
                            "Fix every issue below and re-emit the corrected code block; it "
                            "will be re-verified:\n" + rep)
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    # ── building things ────────────────────────────────────────────────────
    def _project_or_note(self):
        """The folder file tools work in: the session's working folder, else
        the open project, else a fresh 'scratch' project (which then becomes
        the working folder, so commands run where the files are)."""
        if not _builder:
            self._sys_note("builder unavailable", "danger")
            return None
        p = self._workroot()
        if p is None:
            p, _ = _builder.project_open("scratch")
            self.set_workspace(p, announce=False)
            self._sys_note(f"\U0001F4C1 working in {_short_path(p)}", "dim")
        return p

    def _do_project(self, name):
        """Open (or create) a project under ~/ChuckProjects and make it the
        working folder — one notion of 'where we are', not two."""
        if not _builder:
            return
        p, created = _builder.project_open(name)
        self.set_workspace(p, announce=False)
        self._sys_note(f"\U0001F4C1 project '{p.name}' "
                       f"({'created' if created else 'opened'}) \u2014 {_short_path(p)}", "ok")
        others = [n for n, _c in _builder.project_list() if n != p.name]
        extra = f" Other projects: {', '.join(others[:6])}." if others else ""
        self._tool_feedback.append(
            f"[project '{p.name}' ready at {p} and is now the working folder. "
            f"Write files with ```write <relpath>```.{extra}]")

    def _verify_file(self, rel, content):
        """Verify a code file that was just written or edited. Returns None if
        clean (or not code), else the report to hand back."""
        lang = {".py": "python", ".sh": "bash", ".js": "js"}.get(Path(rel).suffix, None)
        if not (lang and _codecheck):
            return None
        try:
            res = _codecheck.check(lang, content)
        except Exception:
            return None
        return None if res.get("ok") else _codecheck.report(res)

    # ── file changes: checkpointed, shown as a diff, revertible ────────────
    def _snapshot(self, root, rel):
        """The file's content before a change (None if it doesn't exist)."""
        try:
            path = _builder._safe(root, rel)
        except Exception:
            return None, None
        try:
            return path, (path.read_text(encoding="utf-8") if path.is_file() else None)
        except (OSError, UnicodeDecodeError):
            return path, None

    def _record_change(self, path, rel, before, after, kind):
        ch = {"path": str(path), "rel": rel, "before": before, "after": after,
              "kind": kind, "turn": self._turn_no, "reverted": False, "card": None}
        self._changes.append(ch)
        del self._changes[:-200]                    # bounded history
        if rel.strip().lstrip("./") in _INSTRUCTION_FILES:
            self._refresh_system()                  # rules it just wrote apply now
        if kind != "rm":
            self._after_write_hook(ch)
        ch["card"] = self._file_card(ch)
        return ch

    def _revert(self, ch, by_user=False):
        """Put a file back the way it was before one change. -> message."""
        if ch["reverted"]:
            return f"{ch['rel']} was already reverted"
        path = Path(ch["path"])
        try:
            if ch["before"] is None:
                if path.is_file():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ch["before"], encoding="utf-8")
        except OSError as ex:
            return f"couldn't revert {ch['rel']}: {ex}"
        ch["reverted"] = True
        card = ch.get("card")
        if card is not None:
            try:
                card["btn"].set_sensitive(False)
                card["btn"].set_label("Reverted")
                self._set_status(card["pill"], "reverted", "wait")
            except Exception:
                pass
        what = ("deleted the new file " if ch["before"] is None else "restored ") + ch["rel"]
        if by_user:
            self._user_notes.append(f"the user reverted your change to {ch['rel']} ({what})")
            self._toast(f"Reverted {ch['rel']}")
        return what

    def _do_undo(self, arg):
        """```undo``` reverts the last file change; ```undo all``` every change
        made since the user's last message."""
        live = [c for c in self._changes if not c["reverted"]]
        if not live:
            self._tool_feedback.append("[undo: there are no file changes to revert]")
            return
        targets = ([c for c in live if c["turn"] == self._turn_no][::-1]
                   if (arg or "").strip().lower() == "all" else [live[-1]])
        if not targets:
            self._tool_feedback.append("[undo all: no file changes in this task]")
            return
        done = [self._revert(c) for c in targets]
        self._sys_note("\u21a9 " + "; ".join(done), "ok")
        self._tool_feedback.append("[undo: " + "; ".join(done) + "]")

    @tool("browse", after_actions=True, cap=2)
    def _t_browse(self, body):
        """```browse <url|file> [WxH]``` plus optional action lines (click <css|x,y>,
        type <text>, press <keys>, wait <ms>, scroll <px>, eval <js>, goto <url>,
        size <WxH>): load it in headless Chromium, act on it, and come back with
        JS errors, failed requests, the visible text and a screenshot."""
        if _browser is None:
            return "[browse: the browser module is missing — reinstall Chuck]"
        lines = [ln for ln in (body or "").splitlines() if ln.strip()]
        if not lines:
            return "[browse: give a URL or a file path]"
        first = lines[0].split()
        size = (1280, 800)
        if len(first) > 1 and re.fullmatch(r"\d{2,5}[x×]\d{2,5}", first[-1]):
            a, b = re.split(r"[x×]", first.pop())
            size = (max(200, min(int(a), 3840)), max(200, min(int(b), 2400)))
        target, actions = " ".join(first), lines[1:20]
        root = self._workroot()
        self._sys_note(f"\U0001F310 browsing {target}" + (f" ({len(actions)} actions)"
                                                          if actions else ""), "dim")
        self._dirty = set()                     # looking at the page is verifying it

        def work():
            shot = _scratch_png(".browse_")
            try:
                r = _browser.browse(target, actions, size=size, root=root, shot_path=shot,
                                    engine=self.settings.get("browser") or "auto")
            except FileNotFoundError as ex:
                return f"[browse: {ex}]"
            except Exception as ex:
                return f"[browse failed: {ex}]"
            text = _browser.report(r)
            pay = image_payload(r["shot"]) if r.get("shot") else None
            if pay:
                self._tool_images.append(pay)
                text += (f"\n[IMAGE of {target} in the browser is attached to this message — "
                         "look at it carefully and say what you see before deciding the next step]")
            GLib.idle_add(self._browse_card, target, r)
            return text
        return work

    def _browse_card(self, target, r):
        """What the browser saw, for the USER too: errors first, then failed
        requests, what each action did, and the page itself."""
        card, head, body, foot = self._card_shell("BROWSER", lambda: _browser.report(r))
        n = len(r["errors"])
        head.insert_child_after(self._pill(f"{n} JS error{'' if n == 1 else 's'}" if n
                                           else "no errors", "fail" if n else "ok"),
                                head.get_first_child())
        w, h = r["size"]
        top = Gtk.Label(label=f"{target}  \u00b7  {w}\u00d7{h}  \u00b7  {r['ms'] / 1000:.1f}s"
                        + (f"  \u00b7  {r['title']}" if r.get("title") else ""), xalign=0)
        top.add_css_class("cmd-text"); _wrap_anywhere(top)
        body.append(top)

        def rows(items, css, cap):
            for it in items[:cap]:
                l = Gtk.Label(label=it, xalign=0, wrap=True, selectable=True)
                l.add_css_class(css); _wrap_anywhere(l)
                body.append(l)
            if len(items) > cap:
                more = Gtk.Label(label=f"\u2026 {len(items) - cap} more", xalign=0)
                more.add_css_class("dim"); body.append(more)
        rows(r["errors"], "browse-err", 8)
        rows([f"\u2717 {x}" for x in r["failed"]], "browse-warn", 5)
        rows([f"\u203a {x}" for x in r["steps"]], "browse-step", 12)
        if r.get("shot"):
            try:
                body.append(self._build_image_widget(r["shot"], None))
            except Exception:
                pass
        self._log_pin(card)
        return False

    @tool("diagnose", bare=True, cap=1)
    def _t_diagnose(self, body):
        """```diagnose [paths]``` — lint and type-check files on disk with the
        tools installed (LSP-lite). No paths: the whole working folder."""
        if _diagnose is None:
            return "[diagnose: the module is missing — reinstall Chuck]"
        root = self._workroot()
        if root is None:
            return "[diagnose: no working folder — pick one first]"
        paths = (body or "").split()
        label = " ".join(paths) if paths else "the working folder"
        self._sys_note(f"\U0001FA7A diagnosing {label}", "dim")

        def work():
            found, summary, notes = _diagnose.diagnose(str(root), paths)
            return _diagnose.report(found, summary, notes, label)
        return work

    @tool("patch", acts=True, cap=0)
    def _t_patch(self, body):
        """```patch``` with a unified diff (git diff style), any number of files:
        applied all-or-nothing. Hunks are located by their context, so slightly
        wrong line numbers don't matter; a hunk that doesn't match changes
        NOTHING anywhere. New files (--- /dev/null) and deletions (+++ /dev/null)
        work; every file is checkpointed like an edit."""
        p = self._project_or_note()
        if p is None:
            return "[patch: no working folder]"
        try:
            plan = _builder.plan_patch(p, body)
        except (ValueError, OSError) as ex:
            self._sys_note(f"\u2717 patch not applied: {ex}", "danger")
            return f"[patch NOT applied, nothing was changed: {ex}]"
        done, bad = [], []
        for rel, before, after in plan:
            path = _builder._safe(p, rel)
            if after is None:
                ok, msg = _builder.delete_file(p, rel)
                if ok:
                    self._record_change(path, rel, before, "", "rm")
            else:
                ok, msg, written = _builder.write_file(p, rel, after)
                if ok:
                    self._record_change(written, rel, before, after,
                                        "write" if before is None else "edit")
                    if self._CODE_EXT.search(rel):
                        self._dirty.add(rel)
                        issue = self._verify_file(rel, after)
                        if issue:
                            bad.append(f"{rel} does not verify:\n{issue}")
            (done if ok else bad).append(rel if ok else f"{rel}: {msg}")
        self._sys_note(f"\u2713 patched {len(done)} file(s)", "ok" if not bad else "danger")
        return (f"[patch applied to {len(done)} file(s): {', '.join(done)}]"
                + ("\n[" + "\n".join(bad) + "]" if bad else ""))

    @tool("mcp", acts=True, cap=4)
    def _t_mcp(self, body):
        """```mcp server.tool``` + JSON arguments: call a tool on a connected MCP
        server. Text comes back as the result; images are shown and seen."""
        if self._mcp is None:
            return "[mcp: the MCP module is missing — reinstall Chuck]"
        first, _, rest = (body or "").strip().partition("\n")
        target = first.strip()
        if "." not in target:
            return "[mcp: name the tool as server.tool on the first line]"
        rest = rest.strip()
        if not rest and target.endswith("}"):
            target, _, rest = target.partition(" ")      # all on one line
        try:
            args = json.loads(rest) if rest else {}
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as ex:
            return f"[mcp {target}: arguments aren't valid JSON ({ex})]"
        self._sys_note(f"\U0001F50C {target}", "dim")

        def work():
            ok, text, images = self._mcp.call(target, args)
            seen = []
            for i, (mime, b64) in enumerate(images[:4]):
                path = _scratch_png(".mcp_")
                try:
                    with open(path, "wb") as fh:
                        fh.write(base64.b64decode(b64))
                    seen.append(self._see(path, f"{target} image {i + 1}"))
                except Exception:
                    pass
            head = f"[mcp {target} {'OK' if ok else 'FAILED'}]"
            return head + "\n" + clip_output(text or "(no text)", 20000) + (
                "\n" + "\n".join(seen) if seen else "")
        return work

    def mcp_reload(self):
        if self._mcp is not None:
            self._mcp.start_all()
            self._sys_note("\U0001F50C reloading MCP servers\u2026", "dim")

    @tool("changes", bare=True, cap=1)
    def _t_changes(self, body):
        """```changes``` — every file this task changed, net of reverts, as one
        compact diff per file: the agent reviews its own work before it calls
        it done. ```changes all``` covers the whole chat."""
        scope_all = (body or "").strip().lower() == "all"
        net = {}                                      # path -> [rel, first before, last after]
        for c in self._changes:
            if c["reverted"] or (not scope_all and c["turn"] != self._turn_no):
                continue
            after = None if c["kind"] == "rm" else c["after"]
            if c["path"] in net:
                net[c["path"]][2] = after
            else:
                net[c["path"]] = [c["rel"], c["before"], after]
        self._sys_note("\U0001F4CB reviewing the changes", "dim")
        if not net:
            return "[changes: no file changes " + ("in this chat]" if scope_all else
                                                  "in this task yet]")
        parts, budget = [], 24000
        for rel, before, after in net.values():
            if before == after:
                continue
            diff, add, rem = _builder.diff_lines(before, after, rel)
            kind = "new" if before is None else "deleted" if after is None else "modified"
            text = "\n".join(diff[2:]) if diff else ""
            if len(text) > budget:
                text = text[:budget] + "\n… (diff clipped — read the file for the rest)"
            budget = max(2000, budget - len(text))
            parts.append(f"{rel} ({kind}, +{add} -{rem})\n{text}".rstrip())
        if not parts:
            return "[changes: every change was undone — the files are as they started]"
        return "CHANGES (" + str(len(parts)) + " file(s)):\n\n" + "\n\n".join(parts)

    def _file_card(self, ch):
        """A file change on the work lane: what, how much, the diff, Revert."""
        diff, add, rem = _builder.diff_lines(ch["before"], ch["after"], ch["rel"])
        kind = {"write": "NEW FILE" if ch["before"] is None else "WRITE",
                "edit": "EDIT", "rm": "DELETE"}.get(ch["kind"], "FILE")
        card, head, body, foot = self._card_shell(kind)
        name = Gtk.Label(label=ch["rel"], xalign=0, hexpand=True)
        name.add_css_class("cmd-text"); _wrap_anywhere(name)
        stat = Gtk.Label(xalign=1)
        stat.add_css_class("diffstat")
        try:
            stat.set_markup(f'<span foreground="#4ADE9A">+{add}</span>  '
                            f'<span foreground="#F87171">\u2212{rem}</span>')
        except Exception:
            stat.set_text(f"+{add} -{rem}")
        same = ch["before"] == ch["after"]
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.append(name)
        if not same:
            row.append(stat)
        body.append(row)
        # rewriting a file with what it already holds changed nothing: say so,
        # rather than a "+0 −0" card offering to revert a no-op
        pill = self._pill("no change" if same else "applied", "wait" if same else "ok")
        head.insert_child_after(pill, head.get_first_child())
        btn = Gtk.Button(label="Revert"); btn.add_css_class("ghost")
        btn.set_tooltip_text("Put the file back the way it was before this change")
        btn.set_visible(not same)
        head.append(btn)
        if diff and len(diff) > 2:
            foot.append(self._diff_view(diff[2:], expanded=len(diff) <= 40))
        info = {"btn": btn, "pill": pill}
        btn.connect("clicked", lambda _b, c=ch: self._revert(c, by_user=True))
        self._log_pin(card)
        return info

    def _diff_view(self, lines, expanded=True, label=None, height=320):
        """Coloured unified-diff lines in a scrolling panel, folded in an expander."""
        exp = Gtk.Expander(label=label or f"Diff \u00b7 {len(lines)} lines")
        exp.add_css_class("think")
        exp.set_expanded(expanded)
        dl = Gtk.Label(xalign=0, selectable=True, wrap=False)
        dl.add_css_class("term-body")
        dl.set_valign(Gtk.Align.START)
        esc = lambda t: _html.escape(t, quote=False)   # noqa: E731
        parts = []
        for ln in lines:
            colour = ("#4ADE9A" if ln.startswith("+") and not ln.startswith("+++") else
                      "#F87171" if ln.startswith("-") and not ln.startswith("---") else
                      "#8F80C9" if ln.startswith(("@@", "diff --git")) else None)
            parts.append(f'<span foreground="{colour}">{esc(ln)}</span>' if colour else esc(ln))
        mk = "\n".join(parts)
        try:
            if _markup_ok(mk):
                dl.set_markup(mk)
            else:
                dl.set_text("\n".join(lines))
        except Exception:
            dl.set_text("\n".join(lines))
        sw = Gtk.ScrolledWindow()
        sw.add_css_class("term-scroll")
        try:
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sw.set_max_content_height(height)
            sw.set_propagate_natural_height(True)
        except Exception:
            pass
        sw.set_child(dl)
        self._chain_scroll(sw, vertical=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("term-card"); box.set_margin_top(6)
        box.append(sw)
        exp.set_child(box)
        return exp

    # ── /doctor ──
    def doctor_checks(self):
        """[(state, name, detail)] — state is ok, warn or fail. Local checks
        only; the API check runs in the background (doctor_api)."""
        S, rows = self.settings, []

        def add(state, name, detail):
            rows.append((state, name, detail))
        try:
            gtkv = f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}"
            adwv = f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}"
        except Exception:
            gtkv, adwv = "GTK ?", "libadwaita ?"
        add("ok", "Chuck", f"v{_config.VERSION} \u00b7 Python {sys.version.split()[0]} \u00b7 {gtkv} \u00b7 {adwv}")
        key = self.backend.key()
        add("ok" if key else "fail", "API key",
            "set" if key else "missing \u2014 add a SiliconFlow key in Settings \u203a Model")
        add("ok", "Model", (S.get("model") or DEFAULT_MODEL)
            + (f" \u00b7 fallback {S.get('fallback_model')}" if S.get("fallback_model") else ""))
        add("ok" if self.workspace else "warn", "Working folder",
            _short_path(self.workspace) if self.workspace else "none \u2014 pick one for project work (/folder)")
        if _browser is not None:
            try:
                kind, exe = _browser.resolve_engine(S.get("browser") or "auto")
                add("ok", "Browser", "Camoufox" if kind == "camoufox" else exe)
            except RuntimeError as ex:
                add("warn", "Browser", f"{ex} \u2014 browse won't work")
        py = shutil.which("ruff") or shutil.which("pyflakes")
        add("ok" if py else "warn", "Python linter",
            py or "none \u2014 pip install --user ruff (catches undefined names before running)")
        add("ok" if shutil.which("shellcheck") else "warn", "Shell linter",
            shutil.which("shellcheck") or "none \u2014 install shellcheck (bash -n is used instead)")
        add("ok" if shutil.which("node") else "warn", "Node",
            shutil.which("node") or "none \u2014 JavaScript can't be checked or run")
        shot = next((t for t in ("grim", "spectacle", "scrot", "gnome-screenshot") if shutil.which(t)), None)
        add("ok" if shot else "warn", "Screenshots", shot or "no capture tool \u2014 install grim (Wayland) or scrot (X11)")
        voice = shutil.which("piper") or shutil.which("espeak-ng")
        add("ok" if voice else "warn", "Voice", voice or "no piper or espeak-ng \u2014 replies can't be read aloud")
        if self._mcp is not None:
            st = self._mcp.status()
            bad = [(n, d) for n, state, d in st if state in ("failed", "error")]
            add("fail" if bad else "ok", "MCP servers",
                ("none configured" if not st else
                 ", ".join(f"{n} {state}" for n, state, _d in st))
                + (f" \u2014 {bad[0][0]}: {bad[0][1]}" if bad else ""))
        try:
            free = shutil.disk_usage(str(Path.home())).free / 1e9
            add("ok" if free > 2 else "warn", "Disk", f"{free:.1f} GB free in your home"
                + ("" if free > 2 else " \u2014 low; builds and downloads may fail"))
        except OSError:
            pass
        if _crashlog is not None:
            errs = _crashlog.recent(3)
            add("warn" if errs else "ok", "Errors (7 days)",
                (f"{len(errs)} recent \u2014 latest {errs[0][0]}: {errs[0][2][:140]}  "
                 f"(log: {_short_path(_crashlog.log_path())})") if errs
                else "none logged")
        return rows

    def run_doctor(self):
        """/doctor: one card that says what works and how to fix what doesn't."""
        rows = self.doctor_checks()
        card, head, body, foot = self._card_shell(
            "DOCTOR", lambda: "\n".join(f"[{st}] {n}: {d}" for st, n, d in rows))
        bad = sum(1 for st, _n, _d in rows if st != "ok")
        pill = self._pill(f"{bad} to look at" if bad else "all good", "wait" if bad else "ok")
        head.insert_child_after(pill, head.get_first_child())
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        icon = {"ok": ("\u2713", "plan-tick"), "warn": ("!", "doctor-warn"), "fail": ("\u2717", "doctor-fail")}
        api_detail = None
        for i, (st, name, detail) in enumerate(rows + [("warn", "API", "checking\u2026")]):
            ic, css = icon[st]
            il = Gtk.Label(label=ic); il.add_css_class(css)
            nl = Gtk.Label(label=name, xalign=0); nl.add_css_class("doctor-name")
            dl = Gtk.Label(label=detail, xalign=0, wrap=True, hexpand=True, selectable=True)
            dl.add_css_class("dim"); _wrap_anywhere(dl)
            grid.attach(il, 0, i, 1, 1); grid.attach(nl, 1, i, 1, 1); grid.attach(dl, 2, i, 1, 1)
            if name == "API":
                api_detail, api_icon = dl, il
        body.append(grid)
        self._log_pin(card)
        key, base = self.backend.key(), self.backend.base()

        def work():
            if not key:
                return ("fail", "no key to check")
            try:
                ok, msg = _pricing.fetch_balance(base, key, opener=_config.api_opener())
            except Exception as ex:
                ok, msg = False, str(ex)
            return ("ok" if ok else "fail", f"reachable \u00b7 balance {msg}" if ok
                    else f"{msg} \u2014 check the key and your connection")

        def done(res):
            st, text = res
            try:
                api_detail.set_text(text)
                api_icon.set_text(icon[st][0])
                for c in ("plan-tick", "doctor-warn", "doctor-fail"):
                    api_icon.remove_css_class(c)
                api_icon.add_css_class(icon[st][1])
            except Exception:
                pass
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()
        return rows

    # ── updates ──
    UPDATE_STATE = DATA_DIR / "update.json"

    def _update_check(self, force=False):
        """Once a day: is there a newer Chuck on GitHub? A toast if so."""
        if _update is None or os.environ.get("CHUCK_NO_UPDATE_CHECK"):
            return False
        if not force and (not self.settings.get("check_updates", True)
                          or not _update.due(self.UPDATE_STATE)):
            return False

        def work():
            ok, v = _update.remote_version()
            if ok:
                _update.mark_checked(self.UPDATE_STATE, v)
                if _update.is_newer(v, _config.VERSION):
                    GLib.idle_add(self._update_toast, v)
        threading.Thread(target=work, daemon=True, name="update-check").start()
        return False

    def _update_toast(self, version):
        try:
            t = Adw.Toast.new(f"Chuck {version} is out (you have {_config.VERSION})")
            t.set_button_label("Update")
            t.set_timeout(0)                      # stays until dismissed
            t.connect("button-clicked", lambda *_a: self.run_update())
            self.toasts.add_toast(t)
        except Exception:
            self._sys_note(f"Chuck {version} is out — /update installs it.", "dim")
        return False

    @staticmethod
    def _asset_dir():
        return HERE.parent / "assets" if HERE.name == "app" else HERE / "assets"

    def run_update(self):
        """/update: fetch the newest Chuck, check it, swap it in, offer a restart."""
        if _update is None:
            self._sys_note("The updater module is missing — re-run the installer.", "danger")
            return
        if getattr(self, "_updating", False):
            return
        self._updating = True
        card, head, body, foot = self._card_shell("UPDATE")
        pill = self._pill("checking…", "run")
        head.insert_child_after(pill, head.get_first_child())
        status = Gtk.Label(label=f"You have v{_config.VERSION}. Looking for a newer one…",
                           xalign=0, wrap=True)
        status.add_css_class("dim"); _wrap_anywhere(status)
        body.append(status)
        self._log_pin(card)

        def set_pill(text, kind):
            for k in ("run", "ok", "wait", "fail"):
                pill.remove_css_class(f"pill-{k}")
            pill.set_text(text)
            pill.add_css_class(f"pill-{kind}")

        def progress(line):
            GLib.idle_add(lambda: (status.set_text(line + "…"), False)[1])

        def work():
            if getattr(self, "_update_ready", None):
                return ("ok", f"installed v{self._update_ready}", self._update_ready)
            ok, v = _update.remote_version()
            if not ok:
                return ("fail", v, None)
            _update.mark_checked(self.UPDATE_STATE, v)
            if not _update.is_newer(v, _config.VERSION):
                return ("ok", f"You're on the newest Chuck (v{_config.VERSION}).", None)
            ok, msg, nv = _update.apply(HERE, self._asset_dir(), log=progress)
            return ("ok" if ok else "fail", msg, nv)

        def done(res):
            self._updating = False
            st, msg, nv = res
            status.set_text(msg if not nv else
                            f"{msg[0].upper()}{msg[1:]}. Restart Chuck to use it — "
                            "this chat is saved and reopens.")
            set_pill("up to date" if st == "ok" and not nv else
                     f"v{nv} ready" if nv else "failed", "ok" if st == "ok" else "fail")
            if nv:
                self._update_ready = nv
                rb = Gtk.Button(label="Restart now"); rb.add_css_class("quick")
                rb.add_css_class("accent-btn"); rb.set_halign(Gtk.Align.START)
                rb.connect("clicked", lambda *_a: self.restart_app())
                foot.append(rb)
                foot.set_visible(True)
            if st == "fail" and _crashlog is not None:
                _crashlog.record("update", msg)
            return False
        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True,
                         name="update").start()

    def restart_app(self):
        """Save everything, then replace this process with a fresh Chuck that
        reopens the current chat."""
        chat = self.chat_id
        self._on_close()
        argv = [sys.executable, str(HERE / "chucknorris.py")]
        if chat:
            os.environ["CHUCK_REOPEN"] = str(chat)
        try:
            os.execv(sys.executable, argv)
        except OSError as ex:
            self._sys_note(f"Couldn't restart ({ex}) — close and reopen Chuck.", "danger")

    # ── git ──
    def _git(self, *args, timeout=15):
        root = self._workroot()
        if root is None:
            return 128, "no working folder"
        try:
            p = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                               errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, **_RUN_ENV))
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as ex:
            return 127, str(ex)

    def show_git_diff(self):
        """/diff: the working tree against HEAD, as a card. No model call."""
        if not shutil.which("git"):
            self._sys_note("git isn't installed.", "danger"); return False
        rc, top = self._git("rev-parse", "--show-toplevel")
        if rc != 0:
            self._sys_note("The working folder isn't a git repository"
                           + ("" if self._workroot() else " (no folder chosen)") + ".", "dim")
            return False
        _rc, status = self._git("status", "--short", "--branch")
        _rc, diff = self._git("diff", "HEAD", "--no-color", "--stat=100")
        _rc, full = self._git("diff", "HEAD", "--no-color")
        if _rc != 0:                                  # no commits yet
            _rc, full = self._git("diff", "--no-color", "--cached")
        lines = [ln for ln in status.splitlines() if ln.strip()]
        branch = lines[0][3:] if lines and lines[0].startswith("##") else ""
        files = [ln for ln in lines if not ln.startswith("##")]
        card, head, body, foot = self._card_shell("WORKING TREE", lambda: full or status)
        head.insert_child_after(self._pill(f"{len(files)} changed" if files else "clean",
                                           "run" if files else "ok"), head.get_first_child())
        if branch:
            bl = Gtk.Label(label=branch, xalign=0); bl.add_css_class("cmd-text")
            body.append(bl)
        for ln in files[:40]:
            fl = Gtk.Label(label=ln, xalign=0, selectable=True); fl.add_css_class("browse-step")
            body.append(fl)
        if len(files) > 40:
            more = Gtk.Label(label=f"\u2026 {len(files) - 40} more", xalign=0)
            more.add_css_class("dim"); body.append(more)
        if full.strip():
            dl = full.splitlines()
            foot.append(self._diff_view(dl[:4000], expanded=len(dl) <= 60))
        self._log_pin(card)
        return True

    COMMIT_ASK = ("Commit the work: read `git status` and `git diff`, stage only the changes "
                  "that belong (never secrets, build output or unrelated files), and commit "
                  "with a concise message that says why, not just what. Then show "
                  "`git log -1 --stat`.")
    REVIEW_ASK = ("Review the uncommitted changes (`git diff HEAD` and any untracked files) as "
                  "a careful senior reviewer: bugs, broken edge cases, security problems, "
                  "missing tests. Report each finding with file:line and severity, most "
                  "severe first. Change nothing.")

    def _do_write(self, rel, content):
        """Write one complete file into the working folder — checkpointed, shown
        as a diff, and verified if it's code, so a broken file never sits
        silently in a deliverable."""
        p = self._project_or_note()
        if p is None:
            return
        path, before = self._snapshot(p, rel)
        ok, msg, written = _builder.write_file(p, rel, content)
        if not ok:
            self._sys_note("\u2717 " + msg, "danger")
            self._tool_feedback.append(f"[write failed: {msg}]")
            return
        self._record_change(written, rel.strip(), before, content, "write")
        if self._CODE_EXT.search(rel.strip()):
            self._dirty.add(rel.strip())
        bad = self._verify_file(rel, content)
        if bad:
            self._sys_note(f"\u26a0 {rel}: verification found issues", "danger")
            self._tool_feedback.append(
                f"[wrote {rel} BUT it does not verify — fix it and write it again:\n{bad}]")
            return
        note = msg + ("  \u2713 verified" if Path(rel).suffix in (".py", ".sh", ".js")
                      and _codecheck else "")
        self._tool_feedback.append(f"[{note}]")

    def _do_edit(self, rel, blocks):
        """Apply SEARCH/REPLACE edits to one file — the cheap way to change an
        existing file: only the changed lines are sent, not the whole file."""
        p = self._project_or_note()
        if p is None:
            return
        rel = rel.strip()
        path, before = self._snapshot(p, rel)
        ok, msg = _builder.edit_file(p, rel, blocks)
        if not ok:
            self._sys_note("\u2717 " + msg, "danger")
            self._tool_feedback.append(f"[edit failed, nothing was changed: {msg}]")
            return
        try:
            content = (p / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content = ""
        if path is not None:
            self._record_change(path, rel, before, content, "edit")
            if self._CODE_EXT.search(rel):
                self._dirty.add(rel)
        bad = self._verify_file(rel, content)
        if bad:
            self._sys_note(f"\u26a0 {rel}: edited, but it no longer verifies", "danger")
            self._tool_feedback.append(
                f"[{msg}, BUT the file no longer verifies — fix it:\n{bad}]")
            return
        self._tool_feedback.append(f"[{msg}]")

    def _grep_args(self, body):
        """```grep <regex> [path]``` -> (root, sub, pattern). The last word is a
        path when it exists (inside the working folder, or absolute)."""
        body = (body or "").strip().splitlines()[0] if (body or "").strip() else ""
        root, sub, pattern = self._workroot(), "", body
        parts = body.rsplit(None, 1)
        if len(parts) == 2:
            last = os.path.expanduser(parts[1].strip("'\""))
            if last.startswith("/") and os.path.exists(last):
                root, sub, pattern = Path(last), "", parts[0]
            elif root is not None and (root / last).exists():
                sub, pattern = last, parts[0]
        if len(pattern) > 2 and pattern[0] == pattern[-1] and pattern[0] in "'\"":
            pattern = pattern[1:-1]
        return root, sub, pattern.strip()

    def _do_grep(self, body):
        """Regex search: ```grep <regex> [path]```. Searches the working folder
        (or an absolute path given as the last word). Read-only, so several can
        run in one reply."""
        root, sub, pattern = self._grep_args(body)
        if root is None:
            self._tool_done("[grep: no working folder — pick one, or give an absolute path "
                            "as the last word]")
            return
        if not pattern:
            self._tool_done("[grep: no pattern given]")
            return
        self._sys_note(f"\U0001F50E grep /{pattern}/ in {_short_path(root / sub if sub else root)}")
        self._busy(True)

        def work():
            ok, out = _builder.grep(root, pattern, sub)
            return f"GREP /{pattern}/ {sub or '.'}:\n{out}" if ok else f"[grep failed: {out}]"
        self._tool_thread(work, "grep")

    def _do_plan(self, body, nudge=False):
        """A live checklist for multi-step work. Re-posting it updates the same
        card in place, so the user watches steps tick off instead of reading a
        new list each time."""
        items = []
        for line in (body or "").splitlines():
            m = re.match(r"^\s*(?:[-*+]|\d+[.)])?\s*\[([ xX~>])\]\s*(.+)$", line)
            if m:
                items.append(("x" if m.group(1) in "xX" else
                              ">" if m.group(1) in "~>" else " ", m.group(2).strip()))
            elif line.strip() and not line.strip().startswith("#"):
                items.append((" ", re.sub(r"^\s*(?:[-*+]|\d+[.)])\s*", "", line).strip()))
        items = items[:20]
        if not items:
            return
        e = self._plan_entry
        if self._in_log(e):
            e["data"]["items"] = items
            if e["w"] is not None:
                e["w"] = None
                self._apply_window()
        else:
            self._plan_entry = self._log_add("plan", {"items": items})
        left = sum(1 for st, _t in items if st != "x")
        if nudge and left:
            nxt = next(t for st, t in items if st != "x")
            self._tool_feedback.append(f"[plan noted: {len(items) - left}/{len(items)} done. "
                                       f"Do the next step now: {nxt}]")

    def _build_plan(self, items, entry=None):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("plan-card")
        done = sum(1 for st, _t in items if st == "x")
        hd = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hl = Gtk.Label(label="PLAN", xalign=0, hexpand=True); hl.add_css_class("plan-head")
        cl = Gtk.Label(label=f"{done}/{len(items)}"); cl.add_css_class("plan-head")
        hd.append(hl); hd.append(cl)
        card.append(hd)
        state = (entry or {}).get("data", {}).get("approval")
        cur_marked = state == "pending"           # nothing is under way until it's approved
        for st, t in items:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            now = st == ">" or (st == " " and not cur_marked)
            if st == "x":
                ic, ic_css, tx_css = "\u2713", "plan-tick", "plan-done"
            elif now:
                ic, ic_css, tx_css = "\u25b8", "plan-cur", "plan-now"
                cur_marked = True
            else:
                ic, ic_css, tx_css = "\u25cb", "plan-box", "plan-todo"
            il = Gtk.Label(label=ic); il.add_css_class(ic_css); il.set_valign(Gtk.Align.START)
            il.set_margin_top(3)                  # level with the rich text's first line
            tl = Gtk.Label(xalign=0, wrap=True, hexpand=True)
            _wrap_anywhere(tl); tl.add_css_class(tx_css)
            set_rich(tl, t)                       # `code` and **bold** in a step render
            row.append(il); row.append(tl)
            card.append(row)
        if state == "pending":
            card.add_css_class("plan-proposed")
            bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            bar.set_margin_top(6)
            ok = Gtk.Button(label="Approve & run"); ok.add_css_class("quick")
            rv = Gtk.Button(label="Revise"); rv.add_css_class("ghost")
            ok.connect("clicked", lambda *_a: self._approve_plan(entry))
            rv.connect("clicked", lambda *_a: self._revise_plan(entry))
            bar.append(ok); bar.append(rv)
            card.append(bar)
        elif state == "approved":
            l = Gtk.Label(label="\u2713 approved", xalign=0); l.add_css_class("plan-tick")
            card.append(l)
        return card

    # ── plan mode ──
    PLAN_NOTE = ("PLAN MODE is on: the user wants a plan before any change. Investigate with "
                 "read-only tools only (map, glob, grep, read, search, fetch, browse, diagnose, "
                 "task). Change NOTHING: no bash/python, write, edit, patch, bg or rmfile. When "
                 "you understand the task, post the steps as a ```plan``` with a short summary "
                 "of the approach and any risk, then stop: the user approves it first.")
    _PLAN_BLOCKED = "commands, file changes and other actions"

    def set_plan_mode(self, on):
        on = bool(on)
        if getattr(self, "_plan_guard", False):
            return
        self.plan_mode = on
        try:
            self._plan_guard = True
            self.plan_btn.set_active(on)
            (self.plan_btn.add_css_class if on else self.plan_btn.remove_css_class)("chip-on")
        except Exception:
            pass
        finally:
            self._plan_guard = False
        self._sync_placeholder()
        self._toast("Plan mode \u2014 Chuck proposes, you approve, then he acts" if on
                    else "Plan mode off")

    def _approve_plan(self, entry):
        if self._running or not self._in_log(entry):
            return
        entry["data"]["approval"] = "approved"
        entry["w"] = None
        self._apply_window()
        self.set_plan_mode(False)
        self._carry_plan = entry                 # the run ticks THIS card off
        self.entry.get_buffer().set_text("Approved \u2014 carry out the plan now, step by "
                                         "step, ticking it off as you go.")
        self.on_send()

    def _revise_plan(self, entry):
        if self._in_log(entry):
            entry["data"]["approval"] = "revise"
            entry["w"] = None
            self._apply_window()
        self._sys_note("Say what to change \u2014 Chuck will revise the plan.", "dim")
        try:
            self.entry.grab_focus()
        except Exception:
            pass

    def _do_rmfile(self, rel):
        p = self._project_or_note()
        if p is None:
            return
        path, before = self._snapshot(p, rel)
        ok, msg = _builder.delete_file(p, rel)
        if ok and before is not None:
            # a deleted text file is checkpointed like any other change
            self._record_change(path, rel, before, "", "rm")
        self._sys_note(("\u2713 " if ok else "\u2717 ") + msg, "ok" if ok else "danger")
        self._tool_feedback.append(f"[{msg}]")

    def _do_tree(self):
        p = self._project_or_note()
        if p is None:
            return
        t = _builder.tree(p)
        self._sys_note(f"\U0001F5C2 {_short_path(p)}\n{t}", "mono")
        self._tool_feedback.append(f"[file tree of {p}:\n{t}]")

    def _do_runtests(self):
        """Run the project's own tests and feed the REAL result back."""
        p = self._project_or_note()
        if p is None:
            self._tool_done("[no project]")
            return
        self._sys_note("\u25B6 running the project's tests\u2026", "dim"); self._busy(True)
        self._dirty = set()
        gen = self._gen

        def worker():
            rc, out = _builder.run_tests(p)

            def show():
                self._busy(False)
                if gen != self._gen:
                    return False
                if rc == -1:
                    self._sys_note("no tests found in the project", "danger")
                    self._tool_done("[no tests found — write tests/test_*.py or run_tests.sh, "
                                    "then run them]")
                elif rc == 0:
                    self._sys_note("\u2713 tests passed", "ok")
                    self._tool_done(f"[project tests PASSED:\n{clip_output(out, 6000)}]")
                else:
                    self._sys_note(f"\u2717 tests failed (exit {rc})", "danger")
                    self._tool_done(f"[project tests FAILED (exit {rc}). Fix the code and run "
                                    f"them again:\n{clip_output(out, 20000)}]")
                return False
            GLib.idle_add(show)
        threading.Thread(target=worker, daemon=True).start()

    def _do_package(self):
        """Zip the project and put a real, openable deliverable in front of the user."""
        p = self._project_or_note()
        if p is None:
            return
        ok, zpath, msg = _builder.package(p, note=_builder.manifest(p))
        if not ok:
            self._sys_note("\u2717 " + msg, "danger")
            self._tool_feedback.append(f"[packaging failed: {msg}]")
            return
        card, _h, body, foot = self._card_shell("PACKAGE", lambda: str(zpath))
        t = Gtk.Label(label=Path(zpath).name, xalign=0, wrap=True, selectable=True)
        t.add_css_class("cmd-text"); body.append(t)
        sub = Gtk.Label(label=msg + f"\n{zpath}", xalign=0, wrap=True, selectable=True)
        sub.add_css_class("dim"); body.append(sub)
        rw = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        b1 = Gtk.Button(label="Open folder"); b1.add_css_class("gold")
        b1.connect("clicked", lambda *_: _open_path(str(Path(zpath).parent)))
        b2 = Gtk.Button(label="Open project"); b2.add_css_class("quick")
        b2.connect("clicked", lambda *_: _open_path(str(p)))
        rw.append(b1); rw.append(b2); foot.append(rw)
        self._log_pin(card)
        self._tool_feedback.append(f"[{msg}. The user has it at {zpath}.]")

    # ── attachments: files, pasted images, screenshots ──────────────────────
    # Everything the user hands over lands in a tray above the composer first —
    # visible, removable — and goes with the next message. Images travel to
    # the model as IMAGES (it sees them); text files as their contents; any
    # other file as its path, so in Agent mode Chuck can still work with it.
    ATT_TEXT_MAX = 400_000

    def add_attachment(self, path, display_name=None):
        path = os.path.expanduser(str(path))
        name = display_name or os.path.basename(path)
        if not os.path.isfile(path):
            self._toast(f"Not a file: {name}")
            return False
        if any(a["path"] == path for a in self._attachments):
            return False
        if _is_image_path(path):
            pay = image_payload(path)
            if not pay:
                self._toast(f"Couldn't read image {name}")
                return False
            self._attachments.append({"kind": "image", "name": name, "path": path,
                                      "b64": pay[0], "mime": pay[1]})
        else:
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as fh:
                    head = fh.read(min(size, self.ATT_TEXT_MAX * 4))
            except OSError as ex:
                self._toast(f"Couldn't read {name}: {ex.strerror or ex}")
                return False
            if b"\x00" in head[:8192]:
                self._attachments.append({"kind": "path", "name": name, "path": path,
                                          "size": size})
            else:
                text = head.decode("utf-8", "replace")
                cut = len(text) > self.ATT_TEXT_MAX or size > len(head)
                self._attachments.append({"kind": "text", "name": name, "path": path,
                                          "text": text[:self.ATT_TEXT_MAX], "cut": cut,
                                          "size": size})
        self._refresh_tray()
        return True

    def _remove_attachment(self, att):
        try:
            self._attachments.remove(att)
        except ValueError:
            pass
        self._refresh_tray()

    def _refresh_tray(self):
        tray = getattr(self, "_tray", None)
        if tray is None:
            return
        c = tray.get_first_child()
        while c:
            n = c.get_next_sibling(); tray.remove(c); c = n
        for att in self._attachments:
            chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            chip.add_css_class("att-chip")
            if att["kind"] == "image":
                try:
                    pic, _pb = _pic_from_file(att["path"], 28, 28)
                    pic.add_css_class("att-thumb")
                    chip.append(pic)
                except Exception:
                    chip.append(Gtk.Image.new_from_icon_name(_pick_icon("image-x-generic-symbolic")))
            else:
                chip.append(Gtk.Image.new_from_icon_name(_pick_icon(
                    "text-x-generic-symbolic" if att["kind"] == "text" else "package-x-generic-symbolic",
                    "document-open-symbolic")))
            nl = Gtk.Label(label=att["name"])
            try:
                nl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                nl.set_max_width_chars(22)
            except Exception:
                pass
            nl.add_css_class("att-name")
            chip.append(nl)
            x = Gtk.Button(icon_name=_pick_icon("window-close-symbolic", "edit-delete-symbolic"))
            x.add_css_class("ghost")
            x.set_tooltip_text("Remove")
            x.connect("clicked", lambda _b, a=att: self._remove_attachment(a))
            chip.append(x)
            tray.append(chip)
        tray.set_visible(bool(self._attachments))
        self._sync_placeholder()

    def on_screenshot(self, *_):
        """Capture the screen into the tray, after the window gets out of the
        way for a moment so it isn't the main thing in the picture."""
        self._toast("Capturing the screen\u2026")

        def worker():
            time.sleep(0.4)
            path = _scratch_png(".att_shot-")
            ok = capture_screen(path)

            def done():
                if ok:
                    self.add_attachment(path, "screenshot.png")
                else:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    self._toast("No screenshot tool found (install grim, spectacle or scrot)")
                return False
            GLib.idle_add(done)
        threading.Thread(target=worker, daemon=True).start()

    def on_attach(self, *_):
        try:
            fd = Gtk.FileDialog()
            fd.set_title("Attach files")
            if self.workspace:
                fd.set_initial_folder(Gio.File.new_for_path(str(self.workspace)))
            fd.open_multiple(self, None, self._on_files_chosen)
        except Exception:
            try:
                Gtk.FileDialog().open(self, None, self._on_file_chosen)
            except Exception as ex:
                self._toast(f"Couldn't open the file picker: {ex}")

    def _on_files_chosen(self, dialog, res):
        try:
            files = dialog.open_multiple_finish(res)
        except Exception:
            return
        for i in range(files.get_n_items() if files else 0):
            f = files.get_item(i)
            if f is not None and f.get_path():
                self.add_attachment(f.get_path())

    def _on_file_chosen(self, dialog, res):
        try:
            path = dialog.open_finish(res).get_path()
        except Exception:
            return
        if path:
            self.add_attachment(path)

    def _paste(self):
        """Ctrl+V: an image on the clipboard (a screenshot tool's copy, an image
        copied from a browser) or files copied in a file manager become
        attachments. Returns True if it took the paste; plain text falls
        through to the composer as usual."""
        try:
            cb = self.get_clipboard()
            fmts = cb.get_formats()
        except Exception:
            return False
        try:
            if fmts.contain_gtype(Gdk.FileList.__gtype__):
                def got_files(c, res):
                    try:
                        val = c.read_value_finish(res)
                        for f in val.get_files():
                            if f.get_path():
                                self.add_attachment(f.get_path())
                    except Exception:
                        pass
                cb.read_value_async(Gdk.FileList.__gtype__, GLib.PRIORITY_DEFAULT, None,
                                    got_files)
                return True
        except Exception:
            pass
        try:
            if fmts.contain_gtype(Gdk.Texture.__gtype__) and not fmts.contain_mime_type(
                    "text/plain"):
                def got_tex(c, res):
                    try:
                        tex = c.read_texture_finish(res)
                        path = _scratch_png(".att_paste-")
                        tex.save_to_png(path)
                        self.add_attachment(path, "pasted-image.png")
                    except Exception as ex:
                        self._toast(f"Couldn't paste the image: {ex}")
                cb.read_texture_async(None, got_tex)
                return True
        except Exception:
            pass
        return False

    def _on_drop(self, _target, value, _x, _y):
        """Files dragged in from a file manager (or an image from a browser)."""
        try:
            if isinstance(value, Gdk.FileList):
                n = 0
                for f in value.get_files():
                    if f.get_path() and self.add_attachment(f.get_path()):
                        n += 1
                return n > 0
            if isinstance(value, Gdk.Texture):
                path = _scratch_png(".att_drop-")
                value.save_to_png(path)
                return self.add_attachment(path, "dropped-image.png")
        except Exception:
            return False
        return False

    # ── send / model ──
    def _get_entry(self):
        b = self.entry.get_buffer()
        return b.get_text(b.get_start_iter(), b.get_end_iter(), False).strip()

    # @src/app.py, @README.md, @~/notes.txt — at the start or after a space, so
    # an e-mail address is never one. Trailing punctuation isn't part of it.
    _MENTION = re.compile(r"(?:(?<=\s)|^)@((?:~|\.{1,2})?/?[\w.\-+/]*[\w\-+])")

    def _attach_mentions(self, text):
        """Attach every @file the message names that exists. -> names attached."""
        got = []
        for tok in self._MENTION.findall(text or "")[:10]:
            p = Path(os.path.expanduser(tok))
            if not p.is_absolute():
                if not self.workspace:
                    continue
                p = self.workspace / tok
            if p.is_file() and self.add_attachment(str(p), display_name=tok):
                got.append(tok)
        return got

    SLASH_HELP = [
        ("/help", "this list"),
        ("/init", "survey the folder and write a CHUCK.md of its conventions"),
        ("/clear", "start a new chat"),
        ("/compact", "summarise the conversation so far into one note"),
        ("/model <name>", "switch model (part of the name is enough)"),
        ("/diff", "the working tree as a diff (git)"),
        ("/commit [msg]", "Chuck stages the right changes and commits"),
        ("/review", "Chuck reviews the uncommitted changes for bugs"),
        ("/undo", "revert the last file change"),
        ("/retry", "ask the last question again  (Ctrl+R; \u2191 edits it)"),
        ("/plan", "plan mode on/off: propose first, act after you approve"),
        ("/agent  /chat", "switch mode"),
        ("/folder [path]", "set the working folder (no path: choose one)"),
        ("/cost", "what this chat has cost"),
        ("/memory", "open the memory panel"),
        ("/export", "save this chat as Markdown"),
        ("/mcp [reload]", "MCP servers and their tools"),
        ("/doctor", "check your setup: key, model, browser, tools, errors"),
        ("/update", "install the newest Chuck (then restart)"),
    ]

    def _slash(self, text):
        """Run a /command typed in the composer. -> True if it was one. Anything
        that merely starts with a slash (/etc/fstab is broken) is a message."""
        m = re.match(r"^/([a-z]+)(?:\s+(.*))?$", (text or "").strip(), re.DOTALL)
        if not m:
            return False
        cmd, arg = m.group(1), (m.group(2) or "").strip()
        if cmd == "help":
            card, head, body, foot = self._card_shell("COMMANDS")
            grid = Gtk.Grid(column_spacing=18, row_spacing=6)
            for i, (c, d) in enumerate(self.SLASH_HELP + [("@path", "attach a file from the working folder"),
                                                          ("Tab", "switch Agent / Chat")]):
                cl = Gtk.Label(label=c, xalign=0); cl.add_css_class("slash-cmd")
                dl = Gtk.Label(label=d, xalign=0, wrap=True, hexpand=True); dl.add_css_class("dim")
                grid.attach(cl, 0, i, 1, 1); grid.attach(dl, 1, i, 1, 1)
            body.append(grid)
            self._log_pin(card)
        elif cmd == "clear":
            self.new_chat()
        elif cmd == "retry":
            self.retry_last()
        elif cmd == "diff":
            self.show_git_diff()
        elif cmd == "doctor":
            self.run_doctor()
        elif cmd == "update":
            self.run_update()
        elif cmd in ("commit", "review"):
            if not self.workspace:
                self._sys_note("Pick a working folder first (/folder).", "danger")
                return True
            self.set_agent_mode(True, announce=False)
            ask = self.COMMIT_ASK if cmd == "commit" else self.REVIEW_ASK
            if cmd == "commit" and arg:
                ask += f" Use exactly this commit message: {arg}"
            self.entry.get_buffer().set_text(ask)
            self.on_send()
        elif cmd == "plan":
            if not self.agent_mode:
                self.set_agent_mode(True)
            self.set_plan_mode(not self.plan_mode if arg not in ("on", "off") else arg == "on")
        elif cmd in ("agent", "chat"):
            self.set_agent_mode(cmd == "agent")
        elif cmd == "model":
            ids = list(dict.fromkeys(list(_pricing.SUGGESTED) + list(_pricing.PRICES)))
            hit = [i for i in ids if arg and arg.lower() in i.lower()]
            if not arg:
                self._sys_note("Model: " + (self.settings.get("model") or DEFAULT_MODEL), "dim")
            elif hit:
                self.set_model(hit[0])
                self._sys_note("Model: " + hit[0], "ok")
            else:
                self.set_model(arg)
                self._sys_note("Model: " + arg + " (not in the price list)", "dim")
        elif cmd == "undo":
            live = [c for c in self._changes if not c["reverted"]]
            self._sys_note("\u21a9 " + self._revert(live[-1], by_user=True) if live
                           else "Nothing to undo.", "dim")
        elif cmd == "folder":
            if arg:
                if not self.set_workspace(arg):
                    self._sys_note(f"Not a folder: {arg}", "danger")
            else:
                self.open_workspace()
        elif cmd == "cost":
            u, ss = self._usage, self._session
            self._sys_note(f"This chat: {_pricing.fmt_usd(u['cost'])} \u00b7 "
                           f"{_pricing.fmt_tokens(u['prompt'])} in, "
                           f"{_pricing.fmt_tokens(u['completion'])} out, {u['calls']} calls. "
                           f"Session: {_pricing.fmt_usd(ss['cost'])}.", "dim")
        elif cmd == "memory":
            self.open_memory()
        elif cmd == "mcp":
            if arg == "reload":
                self.mcp_reload()
            elif self._mcp is None:
                self._sys_note("MCP is unavailable (module missing).", "danger")
            else:
                st = self._mcp.status()
                self._sys_note(("MCP servers (" + _short_path(self.MCP_CONFIG) + "):\n"
                                + "\n".join(f"{n:<14} {state:<9} {d}" for n, state, d in st))
                               if st else f"No MCP servers. Add them to {self.MCP_CONFIG} "
                               "(Claude Desktop format: {\"mcpServers\": {...}}), then /mcp reload.",
                               "dim")
        elif cmd == "init":
            if not self.workspace:
                self._sys_note("Pick a working folder first (/folder).", "danger")
                return True
            self.set_agent_mode(True, announce=False)
            self.entry.get_buffer().set_text(
                "Survey this project (map, read the key files, the build and test setup) and "
                "write CHUCK.md in its root: what it is, how to build, run and test it, the "
                "layout, and the conventions a new engineer must follow. Short and concrete. "
                "If CHUCK.md exists, update it instead.")
            self.on_send()
        elif cmd == "compact" and hasattr(self, "compact_now"):
            self.compact_now()
        elif cmd == "export" and hasattr(self, "export_chat"):
            self.export_chat()
        else:
            return False
        return True

    def on_send(self, *_):
        text = self._get_entry()
        if text and not self._attachments and self._slash(text):
            self.entry.get_buffer().set_text("")
            return
        if self._running and text:
            self._queue_steer(text)
            return
        self._attach_mentions(text)
        # legacy single-slot attachments (kept for anything that still sets them)
        if self.pending_file:
            fname, fbody = self.pending_file
            self._attachments.append({"kind": "text", "name": fname, "path": fname,
                                      "text": fbody, "cut": False, "size": len(fbody)})
            self.pending_file = None
        if self.pending_shot:
            self._attachments.append({"kind": "image", "name": "screenshot.png", "path": "",
                                      "b64": self.pending_shot, "mime": "image/png"})
            self.pending_shot = None
        atts = list(self._attachments)
        if not text and not atts:
            return
        self.entry.get_buffer().set_text("")
        self._attachments = []
        self._refresh_tray()
        if self._edit_at is not None:
            # an edited message replaces the original and everything after it
            at, self._edit_at = self._edit_at, None
            if at < len(self.history):
                self._rewind_to(at)
        self._reset_turn_state()
        self._scroll_down(force=True)       # you asked: you see the answer
        self._start_run()

        images = [a for a in atts if a["kind"] == "image"]
        if not text:
            text = ("What's in this image?" if images and len(atts) == 1 else
                    "Look at the attached file(s).")
        body = text
        if self._user_notes:
            # the model's picture of the files must match what's on disk
            body = "[Since your last reply: " + "; ".join(self._user_notes) + "]\n\n" + body
            self._user_notes = []
        for a in atts:
            if a["kind"] == "text":
                note = (f" (first {len(a['text']):,} characters of {a['size']:,} — the "
                        f"full file is at {a['path']})" if a.get("cut") else "")
                body += (f"\n\n--- attached file: {a['name']}{note} ---\n{a['text']}"
                         "\n--- end ---")
            elif a["kind"] == "path":
                body += (f"\n\n[attached file: {a['path']} ({a['size']:,} bytes, binary) \u2014 "
                         "work with it by path]")
            else:
                body += f"\n[image attached: {a['name']}]"
        if not self._quiet_send:        # a queued message already has its bubble
            self._user_bubble(text, files=[a["name"] for a in atts if a["kind"] != "image"],
                              images=[a["path"] for a in images if a.get("path")])
        self.history.append({"role": "user", "content": body})
        # Images ride with THIS request only; history keeps the text, so a
        # picture is paid for once instead of on every later hop.
        self._tool_images = [(a["b64"], a["mime"]) for a in images]
        self._ask_model()

    def _ask_soon(self):
        """Schedule the next model call for THIS turn only. A Stop between
        scheduling and running used to start a fresh stream for a turn the
        user had already ended."""
        gen = self._gen

        def go():
            if gen == self._gen and not self._cancelled:
                self._ask_model()
            return False
        GLib.idle_add(go)

    def _over_cap(self):
        """The chat's spending cap, if it is set and reached (and not yet
        approved past). -> the cap in $, or 0."""
        cap = self.cfg("cost_cap", 0.0, 0.0, 10000.0, cast=float)
        if cap <= 0:
            return 0
        spent = (getattr(self, "_usage", None) or {}).get("cost", 0) or 0
        return cap if spent >= cap + getattr(self, "_cap_extra", 0.0) else 0

    def _cost_pause(self, cap, vision):
        """Stop before the next model call and ask: the cap is the user's money."""
        spent = self._usage.get("cost", 0)
        card, head, cbody, foot = self._card_shell("SPENDING CAP")
        card.add_css_class("ask-card")
        ql = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        ql.add_css_class("prose")
        ql.set_text(f"This chat has spent {_pricing.fmt_usd(spent)} — your cap is "
                    f"{_pricing.fmt_usd(cap)}. The task is paused before the next model call.")
        cbody.append(ql)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        go = Gtk.Button(label=f"Continue (another {_pricing.fmt_usd(cap)})")
        go.add_css_class("quick")
        stop = Gtk.Button(label="Stop here"); stop.add_css_class("ghost")
        row.append(go); row.append(stop)
        foot.append(row)

        def cont(_b):
            go.set_sensitive(False); stop.set_sensitive(False)
            # another `cap` from HERE, not from wherever the cap used to be
            self._cap_extra = max(self._cap_extra, self._usage.get("cost", 0) - cap) + cap
            self._start_run()
            self._ask_model(vision)

        def halt(_b):
            go.set_sensitive(False); stop.set_sensitive(False)
            self._sys_note("Stopped at the spending cap.", "dim")
        go.connect("clicked", cont)
        stop.connect("clicked", halt)
        self._log_pin(card)
        self._end_run()

    def _reset_turn_state(self):
        """Everything a new turn starts without."""
        self._hops = 0
        self._steps = 0
        self._step_forced = False
        self._nudges = 0
        self._plan_nudges = 0
        self._verify_nudges = 0
        self._dirty = set()
        self._asked = False
        self._plan_entry, self._carry_plan = self._carry_plan, None
        self._reset_continuation()
        self._turn_no += 1
        self._refresh_system()                  # instruction files edited since?
        self._seen_queries = []; self._seen_urls = set(); self._dead_urls = set()
        self._forced_answer = False
        self._hook_runs = 0
        self._hook_passed_turn = None
        self._acted_since_nudge = False

    # ── hooks: the user's own commands around the agent's work ──
    def _after_write_hook(self, ch):
        """Settings > Agent > Hooks: run a command (a formatter, say) on every
        file the agent writes. A file the hook changed is re-read into the
        checkpoint, and the model is told, so its next edit matches disk."""
        hook = (self.settings.get("hook_after_write") or "").strip()
        if not hook or not self.agent_mode:
            return
        import fnmatch
        rel = ch["rel"].strip()
        pats = [x.strip() for x in (self.settings.get("hook_after_write_glob") or "*").split(",")
                if x.strip()] or ["*"]
        if not any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p)
                   for p in pats):
            return
        cmd = hook.replace("{path}", shlex.quote(ch["path"]))
        self._busy(True)
        self._pending_tools += 1                     # released by _tool_thread

        def work():
            rc, out = run_command(cmd, timeout=120)
            try:
                now = Path(ch["path"]).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                now = ch["after"]
            changed = now != ch["after"]
            if changed:
                ch["after"] = now                    # the checkpoint matches disk
            if rc == 0 and not changed:
                return ""                            # nothing to tell the model
            return (f"[after-write hook on {rel}: " + ("ok" if rc == 0 else f"FAILED (exit {rc})")
                    + (" \u2014 it changed the file; re-read it before editing it again"
                       if changed else "") + "]"
                    + (f"\n{clip_output(out, 4000)}" if rc != 0 else ""))
        self._tool_thread(work, "hook")

    def _before_done_hook(self):
        """The 'before finishing' hook: run once the agent wants to stop, if it
        changed files. A failure goes back to the model to fix (at most three
        rounds); a pass lets the turn end. -> True if it started."""
        hook = (self.settings.get("hook_before_done") or "").strip()
        if (not hook or not self.agent_mode or self.plan_mode or self._cancelled
                or self._hook_runs >= 3 or self._hook_passed_turn == self._turn_no
                or not any(c["turn"] == self._turn_no and not c["reverted"]
                           for c in self._changes)):
            return False
        self._hook_runs += 1
        turn = self._turn_no
        self._sys_note(f"\u2691 before-finish hook: {hook}", "dim")
        self._busy(True)
        self._pending_tools += 1

        def work():
            rc, out = run_command(hook, timeout=900)
            if rc == 0:
                self._hook_passed_turn = turn
                GLib.idle_add(self._sys_note, "\u2713 before-finish hook passed", "ok")
                return ""
            return (f"[before-finish hook `{hook}` FAILED (exit {rc}) \u2014 fix what it "
                    f"reports, then finish:\n{clip_output(out, 8000)}]")
        self._tool_thread(work, "before-finish hook")
        return True

    def _fallback_note(self, bad, fb, why):
        """A fallback model is answering: say so, and put ITS name on the byline."""
        self._sys_note(f"\u21c4 {bad.split('/')[-1]} failed ({why}) \u2014 "
                       f"{fb.split('/')[-1]} is answering this one", "dim")
        e = self._bot_entry
        if e is not None:
            e["data"]["meta"] = fb.split("/")[-1]
            try:
                e["meta_w"].set_text(e["data"]["meta"])
            except (KeyError, AttributeError):
                pass
        return False

    def _queue_steer(self, text):
        """You typed while it works: show it now, deliver it at the next step."""
        self.entry.get_buffer().set_text("")
        self._steer.append(text)
        self._user_bubble(text)
        self._sys_note("\u21b3 queued \u2014 Chuck reads it at his next step", "dim")

    def _deliver_steer(self):
        """Fold queued messages into the request about to be sent. Never into
        a reply that's being continued (it would corrupt the stitch)."""
        if not self._steer or getattr(self, "_cont_prefix", None) is not None:
            return
        msg = ("[MESSAGE FROM THE USER, sent while you were working \u2014 read it first; "
               "it may change what you do next]:\n" + "\n\n".join(self._steer))
        self._steer = []
        last = self.history[-1] if self.history else None
        if last and last.get("role") == "user" and isinstance(last.get("content"), str):
            last["content"] += "\n\n" + msg
        else:
            self.history.append({"role": "user", "content": msg})
        self._sys_note("\u21b3 delivered", "dim")

    def _ask_model(self, vision=False):
        self._deliver_steer()
        cap = self._over_cap()
        if cap:
            self._cost_pause(cap, vision)
            return
        imgs, self._tool_images = list(self._tool_images), []
        last = self.history[-1] if self.history else None
        if imgs and last and last.get("role") == "user" and isinstance(last.get("content"), str):
            vmsg = {"role": "user", "content": [{"type": "text", "text": last["content"]}] + [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                for b64, mime in imgs[:6]]}
            self._stream_into_bubble(self.history[:-1] + [vmsg], vision=True)
            return
        self._stream_into_bubble(self.history, vision=vision)

    def cfg(self, key, default, lo=None, hi=None, cast=int):
        """Read a tuned setting, falling back to the shipped default. Values are
        clamped so a hand-edited settings.json can't put the app in a bad state."""
        try:
            v = cast(self.settings.get(key, default))
        except Exception:
            return default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def export_markdown(self):
        """This chat as a readable Markdown document."""
        title = next((m["content"][:80] for m in self.history
                      if m.get("role") == "user" and isinstance(m.get("content"), str)
                      and not m["content"].startswith(("TOOL RESULTS", "I ran ", "CONTINUE:"))),
                     "Chat")
        u = self._usage
        lines = [f"# {title.splitlines()[0]}", "",
                 f"*{datetime.now().strftime('%Y-%m-%d %H:%M')} · "
                 f"{(self.settings.get('model') or DEFAULT_MODEL).split('/')[-1]} · "
                 f"{'Agent' if self.agent_mode else 'Chat'} mode"
                 + (f" · {self.workspace}" if self.workspace else "")
                 + (f" · {_pricing.fmt_usd(u['cost'])}" if u.get("calls") else "") + "*", ""]
        for m in self.history[1:]:
            c = m.get("content")
            if not isinstance(c, str):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) + " [image]"
            role = m.get("role")
            internal = c.startswith(("TOOL RESULTS", "I ran ", "STEP BUDGET", "RESEARCH BUDGET",
                                     "CONTINUE:", "FILE "))
            if role == "user" and internal:
                body = clip_output(c, 6000).replace("</details>", "&lt;/details&gt;")
                lines += ["<details><summary>Tool results</summary>", "", "````text", body,
                          "````", "", "</details>", ""]
            elif role == "user":
                lines += ["## You", "", self._typed_text(c), ""]
            else:
                lines += ["## Chuck", "", c, ""]
        return "\n".join(lines).rstrip() + "\n"

    def export_chat(self):
        """/export: write the chat as Markdown next to your downloads."""
        out_dir = Path.home() / "Downloads"
        if not out_dir.is_dir():
            out_dir = DATA_DIR / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"chuck-{self.chat_id}.md"
        try:
            path.write_text(self.export_markdown(), encoding="utf-8")
        except OSError as ex:
            self._sys_note(f"\u2717 couldn't export: {ex}", "danger")
            return None
        self._sys_note(f"\U0001F4C4 exported to {path}", "ok")
        self._toast(f"Exported {path.name}")
        return path

    COMPACT_PROMPT = (
        "You are compacting an engineering agent's conversation so the work can continue "
        "with less context. Write a dense summary, at most ~1200 words, of the conversation "
        "below: the user's goals and requirements (quote exact wording where it matters), "
        "decisions made and why, files created or changed and what they do, important "
        "commands and their outcomes, the current state, open problems, and the next steps. "
        "Facts only, no preamble, no advice to the user.")
    COMPACT_KEEP = 6                     # newest messages always sent as they are

    def _maybe_auto_compact(self, messages):
        """When what we'd send is nearing the context budget, compact in the
        background so the NEXT request carries a summary instead of losing the
        oldest turns outright."""
        if self._compacting or not self.settings.get("auto_compact", True) or self._over_cap():
            return
        budget = self.cfg("context_chars", SEND_CHAR_BUDGET, 40000, 800000)
        size = sum(len(m["content"]) if isinstance(m.get("content"), str) else 2000
                   for m in messages[1:])
        done = self._compact["upto"] if self._compact else 1
        if size > budget * 0.8 and len(self.history) - done > self.COMPACT_KEEP + 6:
            GLib.idle_add(lambda: (self.compact_now(auto=True), False)[1])

    def compact_now(self, auto=False):
        """Summarise everything but the newest few messages, in the background.
        The transcript is never touched: the summary only replaces those turns
        in what is SENT. /compact runs it on demand."""
        if self._compacting:
            return False
        if getattr(self, "_cont_prefix", None) is not None:
            # mid-way through a reply that is being continued: its scaffolding
            # is about to be collapsed out of history, so wait for it
            if not auto:
                self._sys_note("Busy finishing a long reply \u2014 /compact again in a moment.",
                               "dim")
            return False
        upto = len(self.history) - self.COMPACT_KEEP
        prev = self._compact
        start = prev["upto"] if prev else 1
        if upto - start < 2:
            if not auto:
                self._sys_note("Nothing to compact yet.", "dim")
            return False
        parts = []
        if prev:
            parts.append("EARLIER SUMMARY:\n" + prev["text"])
        for m in self.history[start:upto]:
            c = m.get("content")
            if not isinstance(c, str):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            parts.append(f"[{m.get('role')}] " + clip_output(c, 4000))
        transcript = clip_output("\n\n".join(parts), 150000)
        msgs = [{"role": "system", "content": self.COMPACT_PROMPT},
                {"role": "user", "content": transcript}]
        self._compacting = True
        chat = self.chat_id
        self._sys_note("\U0001F5DC compacting earlier turns\u2026", "dim")

        def work():
            out, err = [], []
            b = Backend(self.settings) if isinstance(self.backend, Backend) else self.backend
            try:
                b.stream(msgs, out.append, lambda: None, err.append)
            except Exception as ex:
                err.append(str(ex))
            text = "".join(out).strip()

            def land():
                self._compacting = False
                if chat != self.chat_id or upto > len(self.history) - 1:
                    return False                 # another chat, or history changed under it
                if err or len(text) < 40:
                    self._sys_note("\u26a0 couldn't compact: " + (err[0] if err else
                                                                   "empty summary"), "danger")
                    return False
                self._account(len(transcript), len(text), usage=getattr(b, "last_usage", None),
                              model=getattr(b, "last_model", None))
                self._compact = {"upto": upto, "text": text}
                self._sys_note(f"\U0001F5DC compacted {upto - start} earlier messages into a "
                               f"{len(text) // 1000 or 1}k summary", "dim")
                self._save_chat()
                return False
            GLib.idle_add(land)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _trim_for_send(self, messages):
        """Bound the conversation actually sent to the model.

        Never mutates self.history — the user keeps their whole transcript on
        disk. This only decides what rides along this turn: the system prompt,
        then as much recent conversation as fits the budget, with stale research
        blobs dropped first (they're huge, and the answer that followed them
        already contains their conclusions).
        """
        if not messages:
            return messages
        c = self._compact
        if c and 1 < c["upto"] < len(messages) - 1:
            # the summary stands in for every turn it covers
            messages = (messages[:1] + [{"role": "user", "content":
                        "CONVERSATION SUMMARY (earlier turns, compacted — the work so far):\n"
                        + c["text"]}] + messages[c["upto"]:])
        self._maybe_auto_compact(messages)
        # Compress BEFORE deciding what fits. The old order dropped whole blobs
        # the moment the budget was tight, which threw away the middle of a
        # research chain entirely; squeezing them first means more of the chain
        # survives in the same number of characters. Extractive only — head,
        # tail and any error lines are kept verbatim, nothing is paraphrased.
        if _compress is not None:
            messages, saved = _compress.compress_history(
                messages, keep_recent=4, budget_chars=4000)
            if saved > 2000:
                self._sys_note(f"✂ trimmed {saved // 1000}k chars of older tool "
                               "output to save context", "dim")
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        budget = self.cfg("context_chars", SEND_CHAR_BUDGET, 40000, 800000)
        kept, used, blobs = [], 0, 0
        for m in reversed(rest):
            c = m.get("content")
            is_blob = isinstance(c, str) and c.startswith(("TOOL RESULTS", "FILE "))
            if is_blob:
                blobs += 1
                if blobs > TOOL_BLOBS_KEPT:
                    continue        # stale research — already digested
            size = len(c) if isinstance(c, str) else 2000
            if kept and used + size > budget:
                break
            kept.append(m)
            used += size
        kept.reverse()
        # Trimming from the front can leave an assistant turn first; several
        # chat APIs reject a conversation that doesn't open with the user.
        while len(kept) > 1 and kept[0].get("role") == "assistant":
            kept.pop(0)
        return sys_msgs + kept

    def _workspace_note(self):
        """One compact line about the working folder: where it is and what is at
        its top level. Enough for the model to orient without spending a tool
        call on `ls`, and capped so a huge folder costs no more than a small one."""
        root = self.workspace
        if not root or not root.is_dir():
            return ""
        try:
            names = sorted(os.listdir(root), key=lambda n: (not (root / n).is_dir(), n.lower()))
        except OSError:
            return f"Working folder: {root}"
        skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
                ".ruff_cache", ".pytest_cache", ".idea", ".vscode", "dist", "build"}
        shown = [n + ("/" if (root / n).is_dir() else "") for n in names
                 if n not in skip and not n.startswith(".")][:40]
        more = len(names) - len(shown)
        tail = f" (+{more} more)" if more > 0 else ""
        return (f"Working folder: {root} (commands run here; relative paths resolve here).\n"
                f"Top level: {', '.join(shown) or '(empty)'}{tail}")

    def _augment(self, messages):
        """Return messages + an EPHEMERAL system note (fresh date/time, working
        folder, relevant memory, skill index, and any playbooks the task
        triggers). Not saved to history — rebuilt every call so 'today' is
        always right, and appended LAST so the long stable prefix before it
        stays cacheable on the API side."""
        # Scan the last few REAL user turns (skip our own TOOL RESULTS / "I ran"
        # injections) so the right playbook stays loaded all through a research
        # loop, not just on the first hop.
        scan = []
        for m in reversed(messages):
            if len(scan) >= 6:
                break
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                c = m["content"]
                if c.startswith(("TOOL RESULTS", "I ran ", "FILE ", "RESEARCH BUDGET",
                                 "STEP BUDGET")):
                    continue
                scan.append(c)
        task_text = "\n".join(scan)
        note = [_now_line()]
        if self.agent_mode and self.plan_mode:
            note.append(self.PLAN_NOTE)
        if self.agent_mode:
            ws = self._workspace_note()
            if ws:
                note.append(ws)
        # relevance-scoped memory: a few relevant facts + core, never the store
        mem_added = False
        if _memory and task_text:
            block = _memory.memory_block(
                task_text, str(self.workspace) if self.workspace else "")
            if block:
                note.append(block); mem_added = True
        if _skills and self.agent_mode:
            idx = _skills.skills_index_line()
            if idx:
                note.append(idx)
        specs_added = False
        if _specs and task_text and self.agent_mode:
            # At most TWO playbooks. Broad triggers ("fix", "file", "script")
            # used to stack four or five of them — several thousand characters
            # re-sent on every hop of the turn.
            for _label, spec in _specs.specs_for(task_text)[:2]:
                note.append(spec); specs_added = True
        if len(note) == 1 and not (specs_added or mem_added):
            ephemeral = {"role": "system", "content": note[0]}
        else:
            ephemeral = {"role": "system", "content": "\n\n".join(note)}
        return self._trim_for_send(messages) + [ephemeral]

    def _stream_into_bubble(self, messages, vision=False):
        try:
            self._stream_into_bubble_inner(messages, vision)
        except Exception as ex:
            # A throw here used to escape into the GTK signal handler, leaving the
            # button stuck on Stop and the turn half-dead — which is why a failed
            # first message could take several tries to recover from.
            self._sys_note(f"⚠ couldn't start that turn: {ex}", "danger")
            if _crashlog is not None:
                _crashlog.record("turn start", traceback.format_exc())
            self._busy(False)
            self._end_run()

    def _stream_into_bubble_inner(self, messages, vision=False):
        self._bot_text = ""
        self._think_text = ""
        self._thinking = False
        self._think_t0 = 0.0
        self._think_secs = 0
        if self._cont_prefix is not None and self._in_log(self._bot_entry):
            # continuing a reply that hit the output limit: same message, same text
            self._bot_text = self._cont_prefix
        else:
            self._bot_label = self._bot_bubble("Thinking\u2026")
        self._busy(True)
        self._awaiting_first = True     # nothing back from the model yet
        self._last_paint = 0.0
        self._note_progress()
        send_msgs = self._augment(messages)
        sent_chars = sum(len(m.get("content") or "") for m in send_msgs
                         if isinstance(m.get("content"), str))
        self._last_vision = vision
        gen = self._gen

        def stale():
            return self._cancelled or gen != self._gen

        def on_think(chunk):
            # Reasoning tokens. They are progress — a model can think for well
            # over the first-token budget — so they feed the watchdog, and they
            # are kept (folded away) under the reply rather than thrown out.
            if stale():
                return
            if not self._thinking:
                self._think_t0 = time.time()
            self._thinking = True
            self._awaiting_first = False
            self._note_progress()
            if len(self._think_text) < 60000:
                self._think_text += chunk

        try:
            self.backend.on_think = on_think
            self.backend.on_fallback = lambda bad, fb, why: GLib.idle_add(
                self._fallback_note, bad, fb, why)
        except Exception:
            pass

        def on_delta(chunk):
            if stale():
                return
            self._awaiting_first = False
            if self._thinking:
                self._thinking = False
                self._think_secs = int(time.time() - self._think_t0)
            self._note_progress()
            self._bot_text += chunk
            # Repainting on EVERY token meant a full re-layout per token plus a
            # regex pass over the whole reply each time — quadratic, and the
            # visible lag on long answers. Coalesce to a steady refresh; the
            # final text is always flushed in _finalise.
            now = time.monotonic()
            if now - self._last_paint < _STREAM_REPAINT:
                return
            self._last_paint = now
            GLib.idle_add(self._paint_stream)

        def on_done():
            self._awaiting_first = False
            self._busy(False)
            recv = len(self._bot_text or "") + len(self._think_text or "")
            # the tokens were billed whether or not the turn still wants them
            used = getattr(self.backend, "last_model", None)     # a fallback bills as itself
            GLib.idle_add(lambda: (self._account(sent_chars, recv, vision, model=used), False)[1])
            if stale():
                return
            GLib.idle_add(lambda: (self._finalise() if not stale() else False, False)[1])

        def on_error(msg):
            self._awaiting_first = False
            self._thinking = False
            self._busy(False)
            if stale():
                return
            if self._cont_prefix is not None:
                partial = self._cont_prefix

                def keep():
                    self._collapse_continuation(partial)
                    self.history.append({"role": "assistant", "content": partial})
                    return False
                GLib.idle_add(keep)
                GLib.idle_add(self._sys_note, "\u26a0 " + msg, "danger")
            else:
                GLib.idle_add(self._set_bot_text, "\u26a0 " + msg)
            GLib.idle_add(self._end_run)

        threading.Thread(
            target=self.backend.stream,
            args=(send_msgs, on_delta, on_done, on_error, vision),
            kwargs={"should_stop": stale,
                    "on_open": self._note_progress},
            daemon=True).start()

    _CONTINUE_MSG = ("CONTINUE: your previous message was cut off by the output limit. "
                     "Continue EXACTLY from its last character: no repetition, no preamble, "
                     "do not reopen a code block that is already open.")
    MAX_CONTINUES = 12

    def _reset_continuation(self):
        self._cont_prefix = None       # the reply so far, while it is being continued
        self._cont_hist_at = None      # history index where its pieces begin
        self._continues = 0

    @staticmethod
    def _join_overlap(prefix, piece):
        """Glue a continuation on, dropping any stretch the model repeated
        from the end of what it had already written. Text is never glued onto
        a fence line: "```" + "Written." would read as a fence opening a
        language called "Written." and the file block would never close."""
        top = min(len(prefix), len(piece), 400)
        for k in range(top, 11, -1):
            if prefix.endswith(piece[:k]):
                piece = piece[k:]
                break
        last = prefix.rsplit("\n", 1)[-1].strip()
        if piece and not piece.startswith("\n") and re.fullmatch(r"`{3,4}", last or "-"):
            piece = "\n" + piece
        return prefix + piece

    def _continue_if_cut(self, text):
        """A reply that hit max_tokens is CONTINUED, not abandoned: the partial
        is kept, the model is asked to carry on from the exact character, and
        the pieces are stitched into one reply before any tool is parsed. This
        is what lets a ```write``` of a 3,000-line file land intact — before,
        the block never closed and the file was silently never written.
        Returns True if a continuation was started."""
        if getattr(self.backend, "last_finish", None) != "length":
            return False
        if self._continues >= self.MAX_CONTINUES:
            return False
        prev = self._cont_prefix or ""
        piece = text[len(prev):] if text.startswith(prev) else text
        if not piece.strip():
            # The model spent the whole budget reasoning and wrote nothing.
            # Asking again would loop on empty replies; report it instead.
            return False
        if self._cont_hist_at is None:
            self._cont_hist_at = len(self.history)
        self.history.append({"role": "assistant", "content": piece})
        self.history.append({"role": "user", "content": self._CONTINUE_MSG})
        self._cont_prefix = text
        self._continues += 1
        self._live(f"continuing a long reply ({self._continues})\u2026")
        self._ask_soon()
        return True

    def _collapse_continuation(self, text):
        """Replace the pieces + CONTINUE prompts with the one stitched reply."""
        if self._cont_hist_at is not None:
            del self.history[self._cont_hist_at:]
            c = self._compact
            if c and c["upto"] > self._cont_hist_at:
                # a summary must never reach past the end of the history it
                # summarises: it would hide the messages that now sit there
                c["upto"] = self._cont_hist_at
                if c["upto"] < 2:
                    self._compact = None
        self._reset_continuation()

    def _finalise(self):
        if self._cancelled:
            return False
        self._note_progress()
        self._thinking = False
        text = self._bot_text
        if self._cont_prefix is not None:
            # stitch: what streamed after the prefix is the continuation
            piece = text[len(self._cont_prefix):]
            text = self._join_overlap(self._cont_prefix, piece)
            self._bot_text = text
        if self._continue_if_cut(text):
            return False
        if self._cont_prefix is not None:
            self._collapse_continuation(text)
        self.history.append({"role": "assistant", "content": text})
        if self._think_text and self._bot_entry is not None:
            self._bot_entry["data"]["think"] = self._think_text
            self._bot_entry["data"]["think_secs"] = self._think_secs or (
                int(max(0, time.time() - self._think_t0)) if self._think_t0 else 0)
        # still cut off after every continuation we allow
        truncated = getattr(self.backend, "last_finish", None) == "length"

        # Tools are looked for OUTSIDE ```write/```edit bodies: a README being
        # written may well contain ```bash or ```search examples.
        outside = _cut_spans(text, [(b["start"], b["end"]) for b in file_blocks(text)])

        def grab(tag):
            # (?![A-Za-z]) stops ```video from also matching ```videos blocks,
            # and ```read from matching ```readskill/```runskill etc.
            return [x.strip() for x in
                    re.findall(r"```" + tag + r"(?![A-Za-z])[ \t]*\n?(.*?)```", outside,
                               re.DOTALL)
                    if x.strip()]

        def has(tag):
            """Presence check for bodiless tags (```tree```, ```package```).
            grab() drops empty bodies, so those tags need their own test."""
            return bool(re.search(r"```" + tag + r"(?![A-Za-z])", outside))

        # cap how many of each tool one reply can fire, so a malformed/looping
        # model can't spawn dozens of threads or cards in a single turn.
        CAP = 6
        searches = grab("search")[:CAP]
        fetches = grab("fetch")[:CAP]
        images = grab("images")[:CAP]
        vid_searches = grab("videos")[:CAP]     # search-and-show videos
        videos = grab("video")[:CAP]            # download by URL
        junk = bool(re.search(r"```junk", outside))
        skill_blocks = grab("skill")[:CAP]      # save a smart file
        runskills = grab("runskill")[:CAP]      # run a saved smart file
        reads = grab("read")[:CAP]              # read a file from disk by path
        greps = grab("grep")[:CAP]              # regex search in the working folder
        globs = grab("glob")[:CAP]              # find files by pattern
        asks = grab("ask")[:1]                  # a question only the user can answer
        self._asked = bool(asks)
        briefs = grab("task")[:8]               # parallel read-only sub-agents
        bgs = grab("bg")[:3]                    # background jobs
        logss = grab("logs")[:4]
        kills = grab("kill")[:4]
        undo_req = has("undo")
        undo_arg = (grab("undo") or [""])[0]
        opens = grab("open")[:3]
        shots = has("screenshot")               # see the screen
        looks = grab("look")[:3]                # see an image file
        maps = has("map")                       # the repo map: files + what they define
        plans = grab("plan")[-1:]               # the live checklist (latest wins)
        remembers = grab("remember")[:CAP]      # store a durable fact
        forgets = grab("forget")[:CAP]          # prune a fact
        projects = grab("project")[:2]        # open / switch a build workspace
        trees = has("tree")                    # show what's been built
        rmfiles = grab("rmfile")[:4]           # remove a file from the project
        packages = has("package")              # zip the project and hand it over
        runtests = has("runtests")             # run the project's own tests
        ledger_req = has("ledger")             # show the command record + verify it
        # ```write <relpath>\n<content>``` and ```edit <relpath>\n<blocks>```.
        # The path may sit on the same line as the tag or on the line below it —
        # accept both, so the parser can never disagree with the prompt. A file
        # that itself contains ``` fences (a README) comes wrapped in FOUR
        # backticks; those are parsed first and blanked so the inner fences
        # can't be mistaken for the end of the block.
        fblocks = file_blocks(text)
        writes = [(b["path"], b["body"]) for b in fblocks if b["tag"] == "write"][:12]
        edits = [(b["path"], b["body"]) for b in fblocks if b["tag"] == "edit"][:12]
        fence_clash = [b["path"] for b in fblocks if b.get("ambiguous")]
        # what's left once file contents are cut out: code INSIDE a file being
        # written is never mistaken for a block to run
        rest = _cut_spans(text, [(b["start"], b["end"]) for b in fblocks])
        checks = []
        for m in re.finditer(r"```check[ \t]+([a-z0-9+]+)[ \t]*\n(.*?)```", outside,
                             re.DOTALL | re.IGNORECASE):
            checks.append((m.group(1).strip().lower(), m.group(2)))
        checks = checks[:CAP]
        # Runnable code/shell blocks, in the order they appear in the reply.
        # Collecting per-language and concatenating would order them by language
        # priority instead — so a shell step written FIRST could be dropped in
        # favour of a python step written later. Capture each block's position
        # and sort by it, so "the first runnable thing" is the first one written.
        code_hits = []
        for lang in ("python", "py", "node", "javascript", "js", "bash", "sh"):
            for m in re.finditer(r"```" + lang + r"(?![A-Za-z])[ \t]*\n?(.*?)```",
                                 rest, re.DOTALL):
                cbody = m.group(1).strip()
                if cbody:
                    code_hits.append((m.start(), lang, cbody))
        code_hits.sort(key=lambda t: t[0])
        codes = [(lang, body) for _pos, lang, body in code_hits]   # (lang, body)
        reg_calls = []                          # registered tools, in registry order
        for rtag, spec in _TOOL_REG.items():
            items = grab(rtag)[:spec["cap"]] or ([""] if spec["bare"] and has(rtag) else [])
            reg_calls += [(rtag, b) for b in items]
        # ```patch``` bodies come from the fence-aware parser, like write/edit
        reg_calls += [("patch", b["body"]) for b in fblocks if b["tag"] == "patch"][:4]

        blocked_agent = False
        if not self.agent_mode:
            # Chat mode: nothing touches the machine. Code fences are just code
            # (they stay in the reply); agent-only tools are dropped.
            blocked_agent = bool(skill_blocks or runskills or reads or greps or plans
                                 or projects or trees or rmfiles or packages or runtests
                                 or ledger_req or writes or edits or checks or junk
                                 or globs or maps or shots or looks or briefs or bgs
                                 or logss or kills or undo_req or opens)
            blocked_agent = blocked_agent or bool(videos)
            codes, skill_blocks, runskills, reads, greps, plans = [], [], [], [], [], []
            globs, maps, shots, looks, briefs = [], False, False, [], []
            bgs, logss, kills, opens, undo_req = [], [], [], [], False
            videos = []                 # a download runs yt-dlp: that is acting
            projects, rmfiles, writes, edits, checks = [], [], [], [], []
            trees = packages = runtests = ledger_req = junk = False

        # ONE runnable command per turn — across EVERY source of them.
        # The cap used to apply to ```bash``` blocks only, so a reply with five
        # ```runskill``` blocks launched five commands at once. Each took a
        # worker, each needed root, and the result was a stack of sudo dialogs
        # nobody could type into. Skills are commands; they are capped with the
        # rest of them, and the remainder is handed back to the model to offer
        # again on the next turn.
        runnable = ([("code", c) for c in codes]
                    + [("skill", b) for b in skill_blocks]
                    + [("runskill", n) for n in runskills])
        dropped_extra = max(0, len(runnable) - 1)
        keep = runnable[:1]
        codes = [v for k, v in keep if k == "code"]
        skill_blocks = [v for k, v in keep if k == "skill"]
        runskills = [v for k, v in keep if k == "runskill"]

        # A screenshot or ```look``` beside a command or a write would see the
        # world BEFORE that action ran: dispatch is one pass, and the command
        # finishes later. It read a stale /tmp/s.png, or none at all.
        if not self.agent_mode:
            gated = [c for c in reg_calls if _TOOL_REG[c[0]]["agent_only"]]
            if gated:
                blocked_agent = True
                reg_calls = [c for c in reg_calls if c not in gated]
        async_action = bool(codes or skill_blocks or runskills or bgs or runtests or packages
                            or any(_TOOL_REG[t]["acts"] for t, _ in reg_calls))
        held_reg = [c for c in reg_calls
                    if _TOOL_REG[c[0]]["after_actions"] and async_action]
        reg_calls = [c for c in reg_calls if c not in held_reg]
        held_see = [f"{t} {b.splitlines()[0] if b else ''}".strip() for t, b in held_reg]
        if (shots or looks) and (codes or skill_blocks or runskills or writes or edits
                                 or bgs or runtests or packages):
            held_see += (["screenshot"] if shots else []) + [f"look {p.strip()}" for p in looks]
            shots, looks = False, []        # never dispatched, so never counted
        disp = self._display_text(text, partial=True)
        if self._forced_answer:
            # he was told to answer from what he has — ignore any further research
            searches, fetches = [], []
        if fence_clash:
            # can't tell where the document ends: write nothing, run nothing
            writes, edits, codes, skill_blocks, runskills = [], [], [], [], []
            bgs = []
        if self._step_forced:
            # told to wrap up: no more actions this turn, only the summary
            codes, skill_blocks, runskills, writes, edits, checks = [], [], [], [], [], []
            runtests = packages = junk = False
            bgs = []
        plan_blocked = []
        if self.agent_mode and self.plan_mode:
            # nothing changes until the user approves the plan
            plan_blocked = ([f"```{lang}```" for lang, _ in codes] + [f"write {p}" for p, _ in writes]
                            + [f"edit {p}" for p, _ in edits] + [f"bg {b[:30]}" for b in bgs]
                            + [f"rmfile {r}" for r in rmfiles]
                            + [t for t, _ in reg_calls if _TOOL_REG[t]["acts"]]
                            + (["runskill"] if (skill_blocks or runskills) else [])
                            + (["runtests"] if runtests else []) + (["package"] if packages else [])
                            + (["undo"] if undo_req else []) + (["kill"] if kills else []))
            codes, writes, edits, bgs, rmfiles, skill_blocks, runskills = [], [], [], [], [], [], []
            runtests = packages = junk = undo_req = False
            kills, projects = [], []
            reg_calls = [c for c in reg_calls if not _TOOL_REG[c[0]]["acts"]]
            if plans:
                self._asked = True               # a proposed plan waits for the user
        if fence_clash or self._step_forced:
            reg_calls = [c for c in reg_calls if not _TOOL_REG[c[0]]["acts"]]
        acting = bool(searches or fetches or images or vid_searches or videos or junk
                      or skill_blocks or runskills or reads or greps or codes or checks
                      or projects or writes or edits or trees or packages or runtests
                      or rmfiles or ledger_req or plans or globs or maps or shots or looks
                      or briefs or bgs or logss or kills or undo_req or opens or asks
                      or reg_calls)
        # Chuck's own narration is what shows in chat — never swallow it.
        # a step that is only tool blocks says nothing: its cards are the story
        # ("Working on it…" used to stay there long after the step was done)
        self._set_bot_text(disp or ("" if acting else "Done."), rich=True)
        if blocked_agent:
            self._sys_note("Chat mode is on \u2014 nothing was run. Press Tab for Agent "
                           "mode to let Chuck act.", "dim")
        stall = None if (acting or self._asked) else self._stalled(disp, outside)

        # ── ONE owner for the tool counter ───────────────────────────────────
        # Reset FIRST, then count the whole turn, then dispatch. Two bugs lived
        # in the old ordering:
        #   * the reset sat BELOW the skill loops, so a ```runskill``` card that
        #     had already incremented the counter got it zeroed underneath it,
        #     along with its "waiting for the user to confirm" feedback;
        #   * counting one-at-a-time inside the dispatch loop meant any tool
        #     that finishes SYNCHRONOUSLY (_do_read on a project file or an
        #     image, _do_runtests with no project) dropped the counter to zero
        #     mid-loop and fired _continue_or_finish while later tools were
        #     still queued — two model round-trips, two "final" answers.
        # _dispatching additionally holds the turn open for the duration of the
        # dispatch block, so a synchronous _tool_done can never end it early.
        self._pending_tools = 0
        self._tool_feedback = []
        self._loop_web = (searches, fetches) if (searches or fetches) else None
        self._dispatching = True
        if stall:
            self._nudges += 1
            self._sys_note("\u21bb nothing was done yet \u2014 asking Chuck to carry it out", "dim")
            self._tool_feedback.append("[" + stall + "]")

        if fence_clash:
            self._sys_note("\u26a0 couldn't tell where " + ", ".join(fence_clash)
                           + " ends (``` fences inside it) \u2014 nothing written or run",
                           "danger")
            self._tool_feedback.append(
                "[NOTHING was written or run: " + ", ".join(fence_clash) + " contains ``` "
                "fences inside a 3-backtick ```write block, so its end is ambiguous. Send it "
                "again wrapped in FOUR backticks: ````write <path> ... ````]")
        if truncated:
            # The reply hit max_tokens. If it was mid-block, the block never
            # closed and its tool silently never ran — say so, both ways.
            self._sys_note("\u2702 the reply hit the output limit and was cut off"
                           + ("" if text.strip() else " (the model used it all reasoning "
                              "\u2014 raise Max output tokens in Settings)"), "danger")
            self._tool_feedback.append(
                "[Your last reply was CUT OFF by the output token limit. Anything after "
                "the cut did not happen. Continue from there; split a large file into "
                "smaller ```write``` / ```edit``` steps.]")

        # skills: save any new smart files, then offer run cards. These go
        # through _command_card, which counts itself — hence: after the reset.
        for blk in skill_blocks:
            self._save_skill(blk)
        for name in runskills:
            self._run_skill(name.strip())

        # memory: store/prune durable facts (quiet — a small note, doesn't pause
        # the run or enter the loop).
        if _memory:
            for fact in remembers:
                fact = fact.strip()
                scope = ""
                if fact.lower().startswith("project:"):
                    fact = fact[8:].strip()
                    scope = str(self.workspace) if self.workspace else ""
                ok, msg = _memory.remember(fact, scope=scope)
                if ok:
                    self._sys_note("\U0001F9E0 " + ("updated memory" if msg == "updated"
                                                    else "remembered"), "dim")
            for q in forgets:
                _memory.forget(q.strip())
                self._sys_note("\U0001F9E0 forgot that", "dim")

        # code / shell to run: AUTO-VERIFY first (syntax+lint+security), then
        # either feed problems back for Chuck to fix, or show an approve-to-run
        # card. This moves correctness into the scaffolding — less pressure on
        # the model to be perfect first try.
        self._pending_tools += (
            (1 if runtests else 0) + len(checks) + len(images)
            + len(vid_searches) + (1 if junk else 0) + len(reads)
            + (1 if ledger_req else 0) + len(greps) + len(globs) + (1 if maps else 0)
            + (1 if shots else 0) + len(looks) + (1 if briefs and _agents else 0)
            + (len(codes) if _codecheck else 0))

        plan_only = bool(plans) and not (
            searches or fetches or images or vid_searches or videos or junk or skill_blocks
            or runskills or reads or greps or codes or checks or projects or writes or edits
            or trees or packages or runtests or rmfiles or ledger_req or globs or maps
            or shots or looks or briefs or bgs or logss or kills or undo_req or opens
            or reg_calls)
        proposing = self.agent_mode and self.plan_mode and bool(plans)
        for body in plans:
            self._do_plan(body, nudge=plan_only and not proposing)
        if proposing and self._in_log(self._plan_entry):
            self._plan_entry["data"]["approval"] = "pending"   # Approve / Revise buttons
            self._plan_entry["w"] = None
            self._apply_window()
        for nm in projects:
            self._do_project(nm.strip())
        for rel, body in writes:
            self._do_write(rel, body)
        for rel, body in edits:
            self._do_edit(rel, body)
        for rel in rmfiles:
            self._do_rmfile(rel.strip())
        if undo_req:
            self._do_undo(undo_arg)
        for body in bgs:
            self._do_bg(body)
        for nm in logss:
            self._do_logs(nm)
        for nm in kills:
            self._do_kill(nm)
        for t in opens:
            self._do_open(t)
        for q in asks:
            self._do_ask(q)
        if trees:
            self._do_tree()
        if runtests:
            self._do_runtests()
        if packages:
            self._do_package()
        for lang, body in checks:
            self._do_check(lang, body.strip(), run_after=False)
        if dropped_extra:
            self._sys_note(f"showing the first step only ({dropped_extra} more held back)", "dim")
            self._tool_feedback.append(
                f"[You proposed {dropped_extra + 1} commands at once; only the FIRST was shown. "
                "Give ONE command, wait for its real output, then give the next.]")
        for lang, body in codes:
            if _codecheck:
                self._do_check(lang, body.strip(), run_after=True)
            else:
                self._code_card(lang, body.strip())

        for q in images:
            self._do_images(q)
        for q in vid_searches:
            self._do_video_search(q)
        if junk:
            self._do_junk()
        if ledger_req:
            self._do_ledger()
        for path in reads:
            self._do_read(path.strip())
        for body in greps:
            self._do_grep(body)
        for pat in globs:
            self._do_glob(pat)
        if maps:
            self._do_map()
        if briefs and _agents:
            self._do_tasks(briefs)
        if plan_blocked:
            self._sys_note("\U0001F4CB plan mode \u2014 nothing was run or changed", "dim")
            self._tool_feedback.append(
                "[PLAN MODE: not run: " + ", ".join(plan_blocked[:8]) + ". Investigate with "
                "read-only tools, then post the ```plan```; the user approves it before you act.]")
        for rtag, rbody in reg_calls:
            self._run_registered(rtag, rbody)
        if held_see:
            self._tool_feedback.append(
                "[" + ", ".join(held_see) + " held back: it was in the same reply as an action "
                "that had not run yet, so it would have seen the old state. Look again now.]")
        if shots:
            self._do_screenshot()
        for pth in looks:
            self._do_look(pth)
        for u in videos:                     # fire-and-forget download; not gated
            self._do_video(u)

        # Dispatch is over: release the guard and decide the loop exactly once.
        self._dispatching = False
        if self._pending_tools == 0:
            self._continue_or_finish(disp)
        return False

    # An agent that SAYS it will do something and then stops is the most common
    # way a turn dies: "I'll create the game files now…" with no tool block, or
    # the whole game printed in an ```html block instead of written to a file.
    # Neither contains a tool, so the reply looked final and the turn ended.
    _INTENT = re.compile(
        r"\b(i'?ll|i will|let me(?! know)|let'?s|i'?m going to|i am going to|going to|"
        r"now i|next,? i|first,? i|i'?ll start|starting with|here'?s the plan|step \d)\b",
        re.IGNORECASE)
    _CODE_FENCE = re.compile(r"```([A-Za-z0-9_+#.-]*)[^\n]*\n(.*?)```", re.DOTALL)
    MAX_NUDGES = 2

    def _stalled(self, disp, outside):
        """Why this action-free reply is NOT a real final answer, or None."""
        if (not self.agent_mode or self._nudges >= self.MAX_NUDGES or self._step_forced
                or self.plan_mode):
            return None
        tools = set(self._tool_tags().split("|"))
        for lang, body in self._CODE_FENCE.findall(outside):
            if lang.lower() not in tools and body.count("\n") >= 8:
                return ("You PRINTED code instead of creating the file — nothing was saved. "
                        "Write it with ```write <path>``` (complete file), then run or test it.")
        tail = (disp or "").strip()[-260:]
        if tail.endswith("?"):
            return None                 # it asked the user something: waiting is right
        if tail and (self._INTENT.search(tail) or tail.endswith(":")):
            return ("You described what you will do next but emitted NO tool block, so "
                    "nothing happened. Do it now: emit the block (```write``` for files, "
                    "```bash``` for commands). If the task is genuinely finished, reply with "
                    "the final summary and no promises.")
        return None

    _CODE_EXT = re.compile(r"\.(py|js|mjs|cjs|ts|tsx|jsx|sh|bash|go|rs|c|cc|cpp|h|hpp|java|"
                           r"kt|rb|php|lua|swift|cs|html?|css|vue|svelte)$", re.IGNORECASE)

    def _completion_gap(self, disp=""):
        """(note, instruction) if the agent is about to stop with work left, else None."""
        if (not self.agent_mode or self._step_forced or self._asked or self._cancelled
                or self.plan_mode):
            return None
        if (disp or "").strip().endswith("?"):
            return None                 # it asked the user something: waiting is right
        e = self._plan_entry
        # Another nudge only if the agent DID something since the last one: a
        # bare "Done." twice in a row means it is done (and just didn't tick
        # its card), and every extra nudge is a paid model call.
        if (self._in_log(e) and self._plan_nudges < 3
                and (self._plan_nudges == 0 or self._acted_since_nudge)):
            left = [t for st, t in (e["data"].get("items") or []) if st != "x"]
            if left:
                self._plan_nudges += 1
                self._acted_since_nudge = False
                return ("the plan isn't finished \u2014 continuing",
                        f"Your plan still has {len(left)} unfinished step(s): "
                        + "; ".join(left[:5]) + ". Do the next one now. If a step is done or no "
                        "longer needed, re-post the ```plan``` with it checked [x].")
        if self._dirty and self._verify_nudges < 1:
            self._verify_nudges += 1
            files = ", ".join(sorted(self._dirty)[:6])
            return ("checking the changes actually work",
                    f"You changed {files} but nothing ran after the last change. Verify it now: "
                    "run it, ```runtests```, or ```screenshot``` a UI. Then give the final "
                    "report. If it genuinely can't be run here, say why in the report.")
        return None

    def _do_ask(self, body):
        """```ask``` — a question only the user can answer, with optional
        one-click answers. The task pauses until they reply."""
        lines = [ln.rstrip() for ln in (body or "").strip().splitlines() if ln.strip()]
        if not lines:
            return
        opts, q = [], []
        for ln in lines:
            m = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$", ln)
            (opts if m else q).append(m.group(1).strip() if m else ln)
        question = " ".join(q) or "Which way should I go?"
        card, head, cbody, foot = self._card_shell("QUESTION")
        card.add_css_class("ask-card")
        ql = Gtk.Label(xalign=0, wrap=True, selectable=True, hexpand=True)
        ql.add_css_class("prose"); _wrap_anywhere(ql)
        set_rich(ql, question)
        cbody.append(ql)
        btns = []
        if opts:
            fb = Gtk.FlowBox()
            try:
                fb.set_selection_mode(Gtk.SelectionMode.NONE)
                fb.set_max_children_per_line(3)
                fb.set_column_spacing(8); fb.set_row_spacing(8)
                fb.set_homogeneous(False)
            except Exception:
                pass
            for o in opts[:6]:
                b = Gtk.Button(label=o)
                b.add_css_class("quick")
                try:
                    b.get_child().set_wrap(True)
                    b.get_child().set_max_width_chars(40)
                except Exception:
                    pass
                fb.append(b)
                btns.append(b)
            foot.append(fb)
        hint = Gtk.Label(label="Pick one, or type your own answer below.", xalign=0)
        hint.add_css_class("dim")
        foot.append(hint)

        def answer(_b):
            if self._running:
                return                  # mid-run: leave the options live for later
            for x in btns:
                x.set_sensitive(False)
            _b.add_css_class("chip-on")
            self.entry.get_buffer().set_text(_b.get_label(), -1)
            self.on_send()
        for b in btns:
            b.connect("clicked", answer)
        self._log_pin(card)
        try:
            self.entry.grab_focus()
        except Exception:
            pass

    def _run_registered(self, tag, body):
        """Dispatch one registered tool (see _TOOL_REG). A handler that raises
        is reported to the model as a result, never left hanging."""
        try:
            res = _TOOL_REG[tag]["fn"](self, body)
        except Exception as ex:
            if _crashlog is not None:
                _crashlog.record(f"tool {tag}", traceback.format_exc())
            self._sys_note(f"\u2717 {tag} failed: {ex}", "danger")
            self._tool_feedback.append(f"[{tag} failed: {ex}]")
            return
        if callable(res):
            self._busy(True)
            self._pending_tools += 1                  # released by _tool_thread
            self._tool_thread(res, tag)
        elif res:
            self._tool_feedback.append(res)

    def _tool_done(self, feedback_text=None, gen=None):
        """Called (on main thread) when one async show-tool finishes. A result
        from an earlier turn (gen mismatch) is dropped, never counted here."""
        if self._cancelled or (gen is not None and gen != self._gen):
            return
        self._note_progress()
        self._run_budget = 0            # no command is holding the watchdog open
        if feedback_text:
            self._tool_feedback.append(feedback_text)
        self._pending_tools = max(0, self._pending_tools - 1)
        # While _finalise is still handing out work, "zero outstanding" only
        # means "nothing outstanding YET" — the turn is not over.
        if self._pending_tools == 0 and not getattr(self, "_dispatching", False):
            self._continue_or_finish(self._display_text(self._bot_text or ""))

    def _continue_or_finish(self, disp):
        if self._cancelled:
            return
        searches_fetches = self._loop_web
        feedback = list(self._tool_feedback)
        self._loop_web = None; self._tool_feedback = []

        hop_cap = self.cfg('research_hops', MAX_TOOL_HOPS, 1, 8)
        if searches_fetches and self._hops < hop_cap:
            self._hops += 1
            self._run_web_tools(searches_fetches[0], searches_fetches[1], extra_feedback=feedback)
            return
        if searches_fetches and not self._forced_answer:
            # Budget spent but he still wants to search. Dropping it silently is
            # what left him narrating "Working on it..." forever — instead, make
            # him deliver what he has.
            self._forced_answer = True
            self._hops = 0
            read = ", ".join(sorted(_domain(u) for u in self._seen_urls)[:10]) or "nothing"
            self.history.append({"role": "user", "content":
                                 "RESEARCH BUDGET SPENT — no more searching. You already read: "
                                 f"{read}. Write the FINAL answer now from what you have. State "
                                 "plainly what you could not confirm. Do NOT emit another search, "
                                 "fetch or read block."})
            self._sys_note("⏹ research budget spent — answering from what he has", "dim")
            self._ask_soon()
            return
        # Agent steps have their OWN budget. They used to share the research
        # cap (4 by default), so a task needing a fifth command simply stopped
        # mid-way with no summary — an agent that quits on step five is not an
        # agent. Steps are cheap to allow and the watchdog still guards hangs.
        step_cap = self.cfg('agent_steps', 60, 4, 200)
        if feedback:
            self._acted_since_nudge = True
        if feedback and self._steps < step_cap:
            self._steps += 1
            self.history.append({"role": "user",
                                 "content": "TOOL RESULTS (continue — don't stop until the whole "
                                 "task is done, then give your final answer):\n\n"
                                 + _clip_feedback(feedback)})
            self._ask_soon()
            return
        if feedback and not self._step_forced:
            self._step_forced = True
            self.history.append({"role": "user", "content":
                                 "STEP BUDGET SPENT — take no more actions. Results of the last "
                                 "step:\n" + "\n\n".join(feedback)[:6000] + "\n\nNow give the final "
                                 "report: what is done, what is left, and the exact next step."})
            self._sys_note(f"⏹ step budget ({step_cap}) spent — wrapping up", "dim")
            self._ask_soon()
            return

        # Before ending: is the work actually done? A plan with open steps, or
        # code changed and never run, means "not yet" — send it back to finish,
        # the way a careful engineer would (bounded, and never past the budget).
        gap = self._completion_gap(disp)
        if gap and self._steps < step_cap:
            self._steps += 1
            self._sys_note("\u21bb " + gap[0], "dim")
            self.history.append({"role": "user", "content":
                                 "TOOL RESULTS (continue — don't stop until the whole task is "
                                 "done, then give your final answer):\n\n[" + gap[1] + "]"})
            self._ask_soon()
            return

        if self._steps < step_cap and self._before_done_hook():
            return                                   # its result decides what happens next

        # the turn is truly finished
        self._hops = 0
        self._notify_done(disp)
        if self.settings.get("tts", True) and disp:
            speak(disp, self.settings)
        self._save_chat()
        self._end_run()
        if self._steer:
            # it finished before reaching a step that could take your message:
            # the message becomes the next turn instead of being lost
            text, self._steer = "\n\n".join(self._steer), []
            self._quiet_send = True
            try:
                self.entry.get_buffer().set_text(text)
                self.on_send()
            finally:
                self._quiet_send = False

    @staticmethod
    def _qnorm(q):
        return frozenset(w for w in re.findall(r"[a-z0-9][a-z0-9._/-]*", (q or "").lower())
                         if len(w) > 1)

    def _is_repeat_query(self, q):
        """Has he effectively asked this already this run? Re-wording the same
        search ('X github' → 'github X' → 'X github repository') burns the whole
        research budget without learning anything new."""
        toks = self._qnorm(q)
        if not toks:
            return True
        for prev in self._seen_queries:
            if not prev:
                continue
            overlap = len(toks & prev) / max(1, len(toks | prev))
            if overlap >= 0.7:
                return True
        return False

    _BROWSER_SLOTS = threading.BoundedSemaphore(2)     # browsers open at once

    def _fetch_text(self, url, timeout, cancelled=lambda: False):
        """A page's readable text -> (text, how). A plain fetch first; a page
        that walls it off or is empty without JavaScript is read through the
        browser (Settings > Agent > Browser), which waits out the bot check."""
        body = web_fetch(url, timeout=timeout)
        if body:
            return body, ""
        why = _fetch_failure(url)
        if (why not in ("blocked", "js") or _browser is None
                or not self.settings.get("stealth_fetch", True) or cancelled()):
            return "", ("blocked" if why == "blocked" else "")
        with self._BROWSER_SLOTS:
            if cancelled():
                return "", ""
            try:
                text, eng, still = _browser.read_page(
                    url, engine=self.settings.get("browser") or "auto", timeout=45)
            except Exception as ex:                       # no browser, or it failed
                return "", f"blocked; browser: {str(ex)[:80]}"
        name = {"brave": "Brave", "brave-browser": "Brave", "chromium": "Chromium",
                "chromium-browser": "Chromium", "google-chrome": "Chrome",
                "google-chrome-stable": "Chrome"}.get(eng, eng or "browser")
        if still or not text.strip():
            return "", f"blocked even in {name}"
        return text, f"via {name}"

    def _run_web_tools(self, searches, fetches, extra_feedback=None):
        self._busy(True)
        self._activity_start("Researching")
        gen = self._gen

        def worker():
            out = list(extra_feedback or [])
            # 1. gather a diverse candidate list from all queries (fast — snippets)
            candidates, seen_domains, seen_urls = [], set(), set()
            n_src = self.cfg('research_sources', RESEARCH_MAX_SOURCES, 1, 10)
            n_q = self.cfg('research_queries', RESEARCH_QUERIES, 1, 4)
            f_timeout = self.cfg('fetch_timeout', 6, 3, 30)
            skipped = []
            # ── admission: sequential, because the repeat-check is ORDERED ───
            # _is_repeat_query compares against queries already accepted this
            # run, so "X github" and "github X" arriving together must be judged
            # one after the other or both get in. This pass is pure arithmetic —
            # no network — so keeping it serial costs nothing.
            admitted = []
            for q in searches[:n_q]:
                if self._cancelled:
                    return
                if self._is_repeat_query(q):
                    skipped.append(q)
                    continue
                self._seen_queries.append(self._qnorm(q))
                admitted.append(q)

            # ── the searches themselves: CONCURRENT ─────────────────────────
            # Each query is an independent round-trip to a search engine, and
            # they were being run strictly one after the other — so two queries
            # against a slow instance cost the sum of both before the first page
            # was even fetched. The page fetches below were already parallel;
            # this was the remaining serial leg of every research round.
            found = {}
            if admitted and not self._cancelled:
                def search_one(q):
                    if self._cancelled:
                        return q, None, -1
                    si = self._activity_step(f"searching  {q}")
                    try:
                        return q, web_search(q, n=n_src), si
                    except Exception:
                        return q, None, si

                ex_s = ThreadPoolExecutor(max_workers=min(4, len(admitted)))
                try:
                    for q, res, si in ex_s.map(search_one, admitted):
                        found[q] = (res, si)
                finally:
                    ex_s.shutdown(wait=False)

            # ── merge in the ORDER THEY WERE ASKED ──────────────────────────
            # Results arrive in whatever order the engines answered, but the
            # first query is the user's actual question and the later ones are
            # follow-ups. Merging by completion order would let a fast follow-up
            # fill the source budget and crowd the main query out.
            for q in admitted:
                self._note_progress()
                results, si = found.get(q, (None, -1))
                if not results:
                    self._activity_done(si, f"searched  {q}  (no results)", ok=False)
                    out.append(f"[search '{q}': engines returned nothing — try different wording]")
                    continue
                n_new = 0
                for (title, url, snip) in results:
                    if url in seen_urls or url in self._seen_urls or url in self._dead_urls:
                        continue
                    dom = _domain(url)
                    if dom in seen_domains:           # diversity: one read per domain
                        continue
                    seen_domains.add(dom); seen_urls.add(url)
                    candidates.append((title, url, snip))
                    n_new += 1
                    if len(candidates) >= n_src:
                        break
                self._activity_done(si, f"searched  {q}  ({n_new} new sources)")
                if len(candidates) >= n_src:
                    break
            for u in fetches[:n_src]:
                if u in self._seen_urls or u in self._dead_urls:
                    skipped.append(u)
                    continue
                if u not in seen_urls:
                    seen_urls.add(u); candidates.append((_domain(u), u, ""))

            # 2. fetch ALL candidates in PARALLEL — each with its own live step
            def fetch_one(item):
                if self._cancelled:
                    return None
                title, url, snip = item
                dom = _domain(url)
                label = f"{dom}" + (f" — {title[:60]}" if title and title != dom else "")
                si = self._activity_step(f"reading  {label}")
                body, how = self._fetch_text(url, f_timeout, cancelled=lambda: self._cancelled)
                body = body or snip
                self._note_progress()
                self._activity_done(si, f"read  {label}" + (f"  ({how})" if how else ""),
                                    ok=bool(body))
                if body:
                    self._seen_urls.add(url)
                    return f"[{title or dom}] {url}\n{body}"
                self._dead_urls.add(url)
                return None

            got = 0
            if not self._cancelled:
                # explicit executor, not a `with` block: on Stop we must abandon
                # in-flight fetches, and shutdown(wait=True) would sit here for
                # the full fetch timeout before the button responded.
                ex = ThreadPoolExecutor(max_workers=6)
                try:
                    futs = {ex.submit(fetch_one, it): it for it in candidates}
                    for fut in as_completed(futs):
                        if self._cancelled:
                            break
                        try:
                            r = fut.result()
                        except Exception:
                            r = None
                        if r:
                            out.append(r); got += 1
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

            self._busy(False); self._activity_end()
            if self._cancelled:
                return
            hop_cap = self.cfg("research_hops", MAX_TOOL_HOPS, 1, 8)
            left = max(0, hop_cap - self._hops)
            lines = [f"TOOL RESULTS — {got} new source(s) this round."]
            if skipped:
                lines.append("ALREADY DONE, do not repeat: " + "; ".join(str(x)[:60]
                                                                        for x in skipped[:6]))
            if self._seen_urls:
                lines.append(f"Pages already read this turn ({len(self._seen_urls)}): "
                             + ", ".join(sorted(_domain(u) for u in self._seen_urls)[:8]))
            if self._dead_urls:
                lines.append("Unreachable, do not retry: "
                             + ", ".join(sorted(_domain(u) for u in self._dead_urls)[:6]))
            if left <= 1:
                lines.append("This is your LAST research round — write the final answer now "
                             "from what you have.")
            else:
                lines.append(f"{left} research rounds left. If what you have answers the "
                             "question, ANSWER NOW instead of searching again. Only search "
                             "again for something genuinely NOT covered above — never the "
                             "same query reworded.")
            lines.append("Cite URLs; mark single-source claims [UNVERIFIED]. If a thing "
                         "genuinely cannot be found, say so plainly and move on — that is a "
                         "complete answer, not a failure.")
            note = "\n".join(lines) + "\n\n"
            msg = note + "\n\n".join(out)[:16000]

            def deliver():
                # on the UI thread, and only into the turn that asked
                if gen == self._gen and not self._cancelled:
                    self.history.append({"role": "user", "content": msg})
                    self._ask_model()
                return False
            GLib.idle_add(deliver)
        threading.Thread(target=worker, daemon=True).start()

    # ── settings ──
    def open_settings(self, *_):
        """Native preferences: every change applies the moment it is made and is
        saved a beat later. There is no Save button to forget to press."""
        S = self.settings
        if hasattr(Adw, "PreferencesDialog"):
            dlg = Adw.PreferencesDialog()
            present = lambda: dlg.present(self)
        else:                                            # libadwaita < 1.5
            dlg = Adw.PreferencesWindow(transient_for=self, modal=True)
            present = dlg.present
        dlg.set_title("Settings")
        try:
            dlg.set_search_enabled(True)
        except Exception:
            pass
        pending = {"id": 0, "net": False}

        def commit():
            pending["id"] = 0
            save_settings(S)
            if pending["net"]:
                pending["net"] = False
                _config.network_settings_changed()
            return False

        def changed(key, value, net=False):
            if S.get(key) == value:
                return
            S[key] = value
            pending["net"] = pending["net"] or net
            if pending["id"]:
                GLib.source_remove(pending["id"])
            pending["id"] = GLib.timeout_add(350, commit)

        def page(title, icon):
            pg = Adw.PreferencesPage()
            pg.set_title(title)
            pg.set_icon_name(_pick_icon(icon, "preferences-system-symbolic"))
            dlg.add(pg)
            return pg

        def group(pg, title, desc=None):
            g = Adw.PreferencesGroup()
            g.set_title(_html.escape(title))         # these are markup
            if desc:
                g.set_description(_html.escape(desc))
            pg.add(g)
            return g

        def entry(g, title, key, default="", password=False, after=None, net=False):
            r = Adw.PasswordEntryRow() if password else Adw.EntryRow()
            r.set_title(_html.escape(title))
            r.set_text(str(S.get(key, default) or ""))

            def on(_r):
                v = r.get_text().strip()
                changed(key, v, net)
                if after:
                    after(v)
            r.connect("changed", on)
            g.add(r)
            return r

        def spin(g, title, key, lo, hi, step, default, digits=0, sub=None, after=None):
            r = Adw.SpinRow.new_with_range(lo, hi, step)
            r.set_title(_html.escape(title))
            if sub:
                r.set_subtitle(_html.escape(sub))
            r.set_digits(digits)
            try:
                cur = float(S.get(key, default))
            except Exception:
                cur = float(default)
            r.set_value(max(lo, min(hi, cur)))

            def on(*_a):
                v = r.get_value()
                v = round(v, digits) if digits else int(round(v))
                changed(key, v)
                if after:
                    after(v)
            r.connect("notify::value", on)
            g.add(r)
            return r

        def switch(g, title, key, default, sub=None, after=None, net=False):
            r = Adw.SwitchRow()
            r.set_title(_html.escape(title))
            if sub:
                r.set_subtitle(_html.escape(sub))
            r.set_active(bool(S.get(key, default)))

            def on(*_a):
                changed(key, bool(r.get_active()), net)
                if after:
                    after(bool(r.get_active()))
            r.connect("notify::active", on)
            g.add(r)
            return r

        # ── Model & pricing ──
        pg = page("Model", "applications-science-symbolic")
        g = group(pg, "Account", "SiliconFlow keys live at cloud.siliconflow.com/account/ak. "
                                 "Basilisk's key is reused if present. Stored 0600.")
        entry(g, "API key", "siliconflow_api_key", password=True,
              after=lambda _v: self._hint is not None and self._show_empty_hint())
        entry(g, "API base URL (blank = default)", "siliconflow_base_url")
        bal = Adw.ActionRow()
        bal.set_title("Account balance")
        bal.set_subtitle(self._balance or "not checked")
        bb = Gtk.Button(label="Check"); bb.add_css_class("quick"); bb.set_valign(Gtk.Align.CENTER)

        def check_balance(_b):
            bal.set_subtitle("checking…")

            def work():
                ok, txt = _pricing.fetch_balance(self.backend.base(), self.backend.key(),
                                                 opener=_config.api_opener())
                if ok:
                    self._balance = txt
                GLib.idle_add(lambda: (bal.set_subtitle(txt), False)[1])
            threading.Thread(target=work, daemon=True).start()
        bb.connect("clicked", check_balance)
        bal.add_suffix(bb)
        g.add(bal)

        g = group(pg, "Model")
        model_row = entry(g, "Chat model", "model", DEFAULT_MODEL,
                          after=lambda _v: (self._update_subtitle(), refresh_marks()))
        entry(g, "Vision model (screenshots)", "vision_model", DEFAULT_VISION)
        entry(g, "Fallback model (blank: automatic · none: off)", "fallback_model", "")

        g = group(pg, "Models & pricing",
                  "SiliconFlow list prices per 1M tokens, October 2026 — input / output "
                  "(cached input where published). Click one to use it.")
        marks = {}

        def refresh_marks():
            cur = (S.get("model") or DEFAULT_MODEL).strip().lower()
            for mid, img in marks.items():
                img.set_visible(mid.lower() == cur)

        for mid in _pricing.SUGGESTED:
            pr = _pricing.PRICES.get(mid.lower())
            r = Adw.ActionRow()
            r.set_title(_html.escape(mid))
            if pr:
                r.set_subtitle(f"${pr[0]:g} in  ·  ${pr[2]:g} out"
                               + (f"  ·  ${pr[1]:g} cached" if pr[1] is not None else ""))
            r.set_activatable(True)
            ok_img = Gtk.Image.new_from_icon_name(_pick_icon("object-select-symbolic",
                                                             "emblem-ok-symbolic"))
            r.add_suffix(ok_img)
            marks[mid] = ok_img
            r.connect("activated", lambda _r, m=mid: model_row.set_text(m))
            g.add(r)
        refresh_marks()

        g = group(pg, "Free models",
                  "SiliconFlow's free tier \u2014 rate-limited and smaller. Good for chat, "
                  "weaker as an agent. The list rotates; fetch yours below.")
        for mid in _pricing.FREE:
            r = Adw.ActionRow()
            r.set_title(_html.escape(mid))
            r.set_subtitle("free")
            r.set_activatable(True)
            ok_img = Gtk.Image.new_from_icon_name(_pick_icon("object-select-symbolic",
                                                             "emblem-ok-symbolic"))
            r.add_suffix(ok_img)
            marks[mid] = ok_img
            r.connect("activated", lambda _r, m=mid: model_row.set_text(m))
            g.add(r)
        fr = Adw.ActionRow()
        fr.set_title("Models on your account")
        nf = len(S.get("fetched_models") or [])
        fr.set_subtitle(f"{nf} fetched \u2014 they appear in the composer's model menu"
                        if nf else "Fetch the live list your key can use")
        fb = Gtk.Button(label="Fetch"); fb.add_css_class("quick"); fb.set_valign(Gtk.Align.CENTER)

        def fetch_models(_b):
            fr.set_subtitle("fetching\u2026")

            def work():
                ok, res = _pricing.fetch_models(self.backend.base(), self.backend.key(),
                                                opener=_config.api_opener())

                def show():
                    if ok:
                        S["fetched_models"] = res
                        save_settings(S)
                        fr.set_subtitle(f"{len(res)} fetched \u2014 they appear in the "
                                        "composer's model menu")
                    else:
                        fr.set_subtitle(res)
                    return False
                GLib.idle_add(show)
            threading.Thread(target=work, daemon=True).start()
        fb.connect("clicked", fetch_models)
        fr.add_suffix(fb)
        g.add(fr)
        refresh_marks()

        g = group(pg, "Custom price",
                  "Leave at 0 to use the table above. Set these for a model the table "
                  "doesn't know, or when a price changes — the meter uses them.")
        spin(g, "Input  $ / 1M tokens", "price_in", 0, 100, 0.01, 0, digits=3)
        spin(g, "Output  $ / 1M tokens", "price_out", 0, 100, 0.01, 0, digits=3)

        g = group(pg, "Generation")
        spin(g, "Temperature", "temperature", 0.0, 1.2, 0.05, 0.35, digits=2,
             sub="Lower is more literal and repeatable — better for shell work and code.")
        spin(g, "Max output tokens per reply", "max_tokens", 256, 32768, 256, 8192,
             sub="Per reply. Longer files continue automatically across replies.")

        # ── Agent ──
        pg = page("Agent", "system-run-symbolic")
        g = group(pg, "Mode", "Tab switches between Agent and Chat at any time.")
        switch(g, "Agent mode", "agent_mode", True,
               sub="On: runs commands, reads and edits files. Off: answers only.",
               after=lambda v: self.set_agent_mode(v, announce=False))
        g = group(pg, "Budgets")
        spin(g, "Steps per task", "agent_steps", 4, 200, 1, 60,
             sub="Commands and edits one request may take before it must report back.")
        spin(g, "Research rounds", "research_hops", 1, 8, 1, MAX_TOOL_HOPS,
             sub="Search → read → think rounds before he must answer.")
        spin(g, "Sources read per answer", "research_sources", 1, 10, 1, RESEARCH_MAX_SOURCES)
        spin(g, "Searches per round", "research_queries", 1, 4, 1, RESEARCH_QUERIES)
        spin(g, "Page fetch timeout (s)", "fetch_timeout", 3, 30, 1, 6)
        spin(g, "Parallel sub-agents", "subagents", 1, 8, 1, 4,
             sub="How many ```task``` sub-agents run at once.")
        spin(g, "Sub-agent steps", "subagent_steps", 2, 20, 1, 8,
             sub="Tool rounds each sub-agent may take before it must report.")
        spin(g, "Spending cap per chat ($)", "cost_cap", 0, 1000, 0.05, 0, digits=2,
             sub="Pause and ask before spending more than this in one chat. 0 = no cap.")
        spin(g, "Context sent per request (characters)", "context_chars", 40000, 800000,
             10000, SEND_CHAR_BUDGET,
             sub="How much conversation and file content rides along. More sees more "
                 "of a big repo at once; less is cheaper. ~4 characters per token.")

        g = group(pg, "Hooks",
                  "Your own commands around Chuck's work. Their failures go back to him to fix.")
        entry(g, "After each file write  ({path} = the file)", "hook_after_write", "")
        entry(g, "  \u2026only for files matching (e.g. *.py, *.ts)", "hook_after_write_glob", "*")
        entry(g, "Before he may finish  (e.g. pytest -q)", "hook_before_done", "")

        if _browser is not None:
            g = group(pg, "Browser",
                      "What the browse tool drives. Brave and Chromium are fast and exact; "
                      "Camoufox is a Firefox build that sites can't tell from a person.")
            labels = ["Automatic (Brave first)", "Brave", "Chromium", "Chrome", "Camoufox"]
            keys = list(_browser.ENGINES)
            br = Adw.ComboRow(); br.set_title("Browser")
            br.set_model(Gtk.StringList.new(labels))
            cur = (S.get("browser") or "auto").lower()
            br.set_selected(keys.index(cur) if cur in keys else 0)
            status = Adw.ActionRow(); status.set_title("In use")

            def show_engine(*_a):
                try:
                    kind, exe = _browser.resolve_engine(S.get("browser") or "auto")
                    status.set_subtitle(_html.escape("Camoufox" + (f" ({exe})" if exe else "")
                                                     if kind == "camoufox" else exe))
                except RuntimeError as ex:
                    status.set_subtitle(_html.escape(str(ex)))

            def on_pick(*_a):
                changed("browser", keys[br.get_selected()])
                show_engine()
            br.connect("notify::selected", on_pick)
            g.add(br); g.add(status)
            show_engine()
            switch(g, "Read blocked pages through the browser", "stealth_fetch", True,
                   sub="When a site refuses a plain fetch (a bot check, or a page that is "
                       "empty without JavaScript), research reads it in the browser above.")

        if self._mcp is not None:
            g = group(pg, "MCP servers",
                      "Tools from Model Context Protocol servers, configured in mcp.json "
                      "(the same format Claude Desktop uses).")
            rows = []

            def fill_mcp(*_a):
                for r in rows:
                    g.remove(r)
                rows.clear()
                st = self._mcp.status()
                for name, state, detail in st or [("No servers yet", "", "Edit mcp.json to add one")]:
                    r = Adw.ActionRow(); r.set_title(_html.escape(name))
                    r.set_subtitle(_html.escape(" \u00b7 ".join(x for x in (state, detail) if x)))
                    g.add(r); rows.append(r)

            def edit_mcp(*_a):
                if not self.MCP_CONFIG.exists():
                    self.MCP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                    self.MCP_CONFIG.write_text(json.dumps({"mcpServers": {
                        "example-disabled": {"command": "npx", "args": [
                            "-y", "@modelcontextprotocol/server-filesystem", str(Path.home())],
                            "disabled": True}}}, indent=2) + "\n")
                _open_path(str(self.MCP_CONFIG))

            def reload_mcp(*_a):
                self.mcp_reload()
                GLib.timeout_add(1500, lambda: (fill_mcp(), False)[1])
            bar = Adw.ActionRow(); bar.set_title("mcp.json")
            bar.set_subtitle(_html.escape(_short_path(self.MCP_CONFIG)))
            eb = Gtk.Button(label="Edit"); eb.add_css_class("quick"); eb.set_valign(Gtk.Align.CENTER)
            rb = Gtk.Button(label="Reload"); rb.add_css_class("quick"); rb.set_valign(Gtk.Align.CENTER)
            eb.connect("clicked", edit_mcp); rb.connect("clicked", reload_mcp)
            bar.add_suffix(eb); bar.add_suffix(rb)
            g.add(bar)
            fill_mcp()

        g = group(pg, "Code verifier")
        if _codecheck:
            have = _codecheck.available_tools()
            for k, v in have.items():
                r = Adw.ActionRow(); r.set_title(k)
                r.set_subtitle("active" if v else "not installed")
                g.add(r)
        if _ledger:
            g = group(pg, "Evidence ledger",
                      "Every command Chuck runs is recorded in a hash-chained log.")
            lr = Adw.ActionRow(); lr.set_title("Ledger")

            def refresh_ledger(*_a):
                try:
                    ok, count, bad, msg = _ledger.verify()
                    where = "" if bad is None else f" · first break at entry {bad}"
                    lr.set_subtitle(_html.escape(f"{count} commands · "
                                                 f"{'chain intact' if ok else 'CHAIN BROKEN'}{where}"))
                except Exception as ex:
                    lr.set_subtitle(_html.escape(f"couldn't read: {ex}"))
            refresh_ledger()
            vb = Gtk.Button(label="Re-verify"); vb.add_css_class("quick")
            vb.set_valign(Gtk.Align.CENTER); vb.connect("clicked", refresh_ledger)
            ob = Gtk.Button(icon_name=_pick_icon("folder-open-symbolic", "folder-symbolic"))
            ob.add_css_class("ghost"); ob.set_valign(Gtk.Align.CENTER)
            ob.set_tooltip_text("Open folder")
            ob.connect("clicked", lambda *_a: _open_path(str(_ledger.LEDGER.parent)))
            lr.add_suffix(vb); lr.add_suffix(ob)
            g.add(lr)
        if _crashlog is not None:
            g = group(pg, "Error log",
                      "Errors Chuck hit, with tracebacks — attach this file to a bug report. "
                      "It never contains your key, settings or chats.")
            er = Adw.ActionRow(); er.set_title("chuck.log")
            errs = _crashlog.recent(1, days=3650)
            er.set_subtitle(_html.escape(_short_path(_crashlog.log_path()) + (
                f" · last: {errs[0][0]}" if errs else " · empty")))
            lb = Gtk.Button(label="Open"); lb.add_css_class("quick"); lb.set_valign(Gtk.Align.CENTER)
            lb.set_sensitive(bool(errs))
            lb.connect("clicked", lambda *_a: _open_path(str(_crashlog.log_path())))
            db = Gtk.Button(label="Run /doctor"); db.add_css_class("quick")
            db.set_valign(Gtk.Align.CENTER)

            def doctor_from_settings(*_a):
                try:
                    dlg.close()
                except Exception:
                    pass
                self.run_doctor()
            db.connect("clicked", doctor_from_settings)
            er.add_suffix(lb); er.add_suffix(db)
            g.add(er)

        # ── Voice ──
        pg = page("Voice", "audio-speakers-symbolic")
        g = group(pg, "Speech")
        switch(g, "Read replies aloud", "tts", True,
               after=lambda v: (self.tts_btn.set_active(v), None if v else stop_speaking()))
        eng = Adw.ComboRow()
        eng.set_title("Engine")
        eng.set_model(Gtk.StringList.new(["Auto (Piper, else espeak-ng)", "Piper only",
                                          "espeak-ng only"]))
        eng.set_selected({"auto": 0, "piper": 1, "espeak": 2}.get(
            (S.get("voice_engine") or "auto").lower(), 0))
        eng.connect("notify::selected", lambda *_a: changed(
            "voice_engine", ["auto", "piper", "espeak"][int(eng.get_selected() or 0)]))
        g.add(eng)
        spin(g, "Speed", "voice_speed", 0.5, 2.0, 0.05, 1.0, digits=2)
        spin(g, "Pitch (espeak-ng)", "voice_pitch", 0, 99, 1, 28)
        spin(g, "Max characters read per reply", "voice_max_chars", 2000, 40000, 1000, 20000)
        entry(g, "Piper voice model (blank = installed voice)", "piper_model")
        tr = Adw.ActionRow(); tr.set_title("Test the voice")
        tb = Gtk.Button(label="Play"); tb.add_css_class("quick"); tb.set_valign(Gtk.Align.CENTER)
        tb.connect("clicked", lambda *_a: speak(
            "Chuck Norris does not adjust settings. Settings adjust to Chuck Norris.", S))
        tr.add_suffix(tb); g.add(tr)

        # ── General ──
        pg = page("General", "preferences-desktop-appearance-symbolic")
        g = group(pg, "Appearance")
        spin(g, "Text size", "font_size", 9, 28, 1, FONT_SIZE,
             after=lambda v: apply_font_size(v))
        g = group(pg, "Chats")
        spin(g, "Auto-delete saved chats after (hours)", "chat_ttl_hours", 1, 720, 1,
             CHAT_TTL_HOURS, sub="Counted from your last activity in a chat.",
             after=lambda _v: self._refresh_sidebar())
        spin(g, "Messages kept rendered", "render_keep", 4, 200, 1, RENDER_KEEP,
             sub="Older ones are unloaded and rebuilt if you scroll back.")
        spin(g, "Messages loaded per scroll-up", "render_page", 5, 200, 5, RENDER_PAGE)
        g = group(pg, "Network & privacy")
        entry(g, "Preferred SearXNG instance (blank = built-in list)", "searx_url")
        entry(g, "Proxy, e.g. http://host:port", "proxy", net=True)
        switch(g, "Send the model API through the proxy too", "proxy_api", True,
               sub="With this off your prompts bypass the proxy.", net=True)
        spin(g, "Reuse fetched pages for (s)", "web_cache_seconds", 0, 600, 15, 90)
        if _update is not None:
            g = group(pg, "Updates")
            switch(g, "Check for a new version once a day", "check_updates", True,
                   sub="Reads one small file from GitHub. /update installs it.")
        g = group(pg, "Reset")
        rr = Adw.ActionRow(); rr.set_title("Reset tuning to defaults")
        rr.set_subtitle("Keeps your key, model, proxy and folders.")
        rb = Gtk.Button(label="Reset"); rb.add_css_class("quick"); rb.set_valign(Gtk.Align.CENTER)

        def reset(*_a):
            for k in ("voice_engine", "voice_speed", "voice_pitch", "research_sources",
                      "research_queries", "research_hops", "fetch_timeout", "chat_ttl_hours",
                      "render_keep", "render_page", "font_size", "voice_max_chars",
                      "piper_model", "siliconflow_base_url", "temperature", "max_tokens",
                      "web_cache_seconds", "proxy_api", "agent_steps", "price_in", "price_out"):
                S.pop(k, None)
            save_settings(S)
            apply_font_size(FONT_SIZE)
            self._toast("Tuning reset — reopen Settings to see the defaults")
            try:
                dlg.close()
            except Exception:
                pass
        rb.connect("clicked", reset)
        rr.add_suffix(rb); g.add(rr)

        def closed(*_a):
            if pending["id"]:
                GLib.source_remove(pending["id"])
                commit()
            self._update_subtitle()
            self._paint_meter()
            self._collapse_window()
            if self._hint is not None:
                self._show_empty_hint()
            return False
        try:
            dlg.connect("closed", closed)
        except Exception:
            dlg.connect("close-request", closed)
        present()


class ChuckApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        if _crashlog is not None:
            _crashlog.install(_config.VERSION)       # errors land in a file you can send
        # Dark, always: the palette is designed dark, and a light system theme
        # would otherwise bring up light libadwaita dialogs over a dark window.
        try:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        except Exception:
            pass
        _use_adwaita_icons()
        sheets = [CSS_TMPL]
        try:
            if (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6):
                sheets.append(VARS_CSS)      # older GTK would only log a parse error
        except Exception:
            pass
        for css in sheets:
            prov = Gtk.CssProvider()
            try:
                try:
                    prov.load_from_data(css.encode())
                except TypeError:
                    prov.load_from_data(css)
                Gtk.StyleContext.add_provider_for_display(
                    Gdk.Display.get_default(), prov,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            except Exception:
                pass                     # an older GTK rejecting VARS_CSS is fine
        apply_font_size(_SETTINGS.get("font_size", FONT_SIZE))
        # Probe the machine now, off the main thread, so the first chat never
        # waits on `pacman -Qq` to find out what distro it is running on.
        prime_context()
        # seed the ready-made skill library once (idempotent; never clobbers
        # user skills). Runs in a thread so it never delays the window.
        if _skill_library and _skills:
            def _seed():
                try:
                    _skill_library.seed_into(_skills, DATA_DIR / "skills" / ".library_version")
                except Exception:
                    pass
            threading.Thread(target=_seed, daemon=True).start()

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = ChuckWindow(self)
            # after /update's restart: reopen the chat that was on screen
            chat = os.environ.pop("CHUCK_REOPEN", "")
            path = CHATS_DIR / f"{chat}.json"
            if re.fullmatch(r"[\w-]+", chat or "") and path.exists():
                try:
                    win._load_chat(path)
                except Exception:
                    pass
        win.present()


def main():
    Adw.init()
    return ChuckApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
