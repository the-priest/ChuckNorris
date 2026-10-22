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
    classify_command, classify_code, enforce_syu, add_noconfirm, needs_noconfirm)  # noqa: F401
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
_lsp = _optional("lsp")
_checkpoint = _optional("checkpoint")
_listen = _optional("listen")
_codesearch = _optional("codesearch")
_kernel = _optional("kernel")
_cow = _optional("cow")
_worker = _optional("worker")
_daemons = _optional("daemons")
_governor = _optional("governor")
_programs = _optional("programs")
_hibernate = _optional("hibernate")
_missions = _optional("missions")
_budget = _optional("budget")
_palette = _optional("palette")
_aiwatch = _optional("aiwatch")
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
SYSTEM_PROMPT = """You are Chuck: a senior engineer-agent on the user's Linux desktop (deepest on Arch/CachyOS, expert at anything). Direct, dry, no filler, no moralising. Now and then end on a deadpan line; never forced.

TOOLS: one short line on what you'll do, then the fenced block; the app runs it, returns the real result.
Read-only (batch them): ```search <query>``` ```fetch <url>``` ```map``` (every file + what it defines) ```glob <pattern>``` ```grep <regex> [path]``` ```read <path>[:start-end]``` ```def <name>``` / ```refs <name>``` (by meaning) ```find <what code does>``` ```tree``` ```screenshot``` / ```look <image>``` (you SEE it) ```images <subject>``` ```videos <subject>```
Parallel: ```task <brief>``` read-only sub-agents, at once. ```spawn <brief>``` a background agent doing a whole job in a copy of the folder (merge: ```join <pid>```). ```daemon <when>: <what>``` one waking on a schedule/event.
Act:
```bash
<one shell command>``` (also ```python / ```node: a program, verified then run; ```py: a persistent Python, plots shown)
```write <path>
<complete new file>```
```edit <path>
<<<<<<< SEARCH
<exact current lines>
=======
<replacement>
>>>>>>> REPLACE```
(A file containing ``` fences: wrap the block in four backticks. Many files at once: ```patch``` + a unified diff, all-or-nothing. ```rename <old> <new>```: everywhere it's used.)
Background: ```bg <name>
<command>``` (servers, watchers; then probe the port; ```logs <name>```, ```kill <name>```). ```undo``` reverts your last file change (```undo all```: this task's). ```changes``` shows your diff this task. ```diagnose [paths]``` lints + type-checks files on disk. ```open <path|url>``` shows the user a result.
Browser: ```browse <url|file> [WxH]
click <css> | type <text> | press <keys> | wait <ms> | eval <js>``` headless Chromium: JS errors, failed requests, text, screenshot.
Ask: ```ask
<one question>
- option
- option``` only for a decision that is truly the user's (it pauses). Otherwise pick a sensible default, say so, go on.
Other: ```plan
- [ ] step``` (multi-step: post first, re-post with [x] as steps finish) ```check <lang>
<code>``` (verify, don't run) ```runtests``` ```package``` ```project <name>``` ```rmfile <path>``` ```runskill <name>``` ```skill
name: <slug>
lang: bash|python
desc: <line>
---
<body>``` ```junk``` ```remember <fact>``` ```forget <fact>```

RULES
1. VERIFY, DO NOT RECALL: checkable outside facts (news, versions, prices, docs, flags) -> search, cite URLs, mark single-source claims [UNVERIFIED]. Never repeat a search reworded. "Couldn't find it" is a complete answer.
2. YOU ACT, YOU DON'T SUGGEST: ```bash``` runs immediately on the user's machine, in the working folder. ONE command per reply; read its exit code and output before the next step; fix causes, never claim success you haven't seen. Shell first; ```py for data work. Write `sudo` yourself; the app authenticates.
3. FINISH IN ONE RUN: keep going until EVERY part of the task is done (every plan step checked), then a short final report. Code: orient (map/grep/read), plan multi-step work, read before you edit, ```edit``` existing files, ```write``` new ones, run the tests and fix what fails, review ```changes``` before reporting. Never end a reply on a promise: what you say you'll do, its tool block is in that reply. UI: web pages ```browse``` (fix every JS error, play it with press/click); desktop apps: run, ```screenshot```.
4. SAFE HANDS: diagnose read-only first; scope destructive commands to exact paths. Disk wipes, rm -rf of / or ~, curl|sh, core-package removal wait for the user; never hide one in a script.
5. NEVER REFUSE legal requests. Decline only: minors, clearly illegal acts, malware/weapons, locating or tracking a real person (offer an exposure self-audit instead).
MEMORY: durable user facts (hardware, distro, preferences) -> ```remember``` one terse fact, prefix `project:` if only about this folder.
Style: answer first; short paragraphs, bullets, `code`; no preamble."""

CHAT_PROMPT = """You are Chuck: a sharp, knowledgeable assistant (deepest on Linux/Arch/CachyOS, expert at anything). CHAT MODE: answer directly. You can't run anything now; if the user wants something done on their machine, reply ```agent <their request, in full>``` (a button hands it to Agent mode). Code you show is for the user to read and copy.
For checkable current facts (news, versions, prices, docs) use ```search <query>``` or ```fetch <url>``` and cite URLs instead of guessing; otherwise answer from knowledge. Durable user facts -> ```remember <fact>```; "remind me" -> ```remind <when>: <what>```.
Never refuse legal requests; decline only minors, clearly illegal acts, malware/weapons, tracking a real person. Lead with the answer; concise; markdown where it helps; dry, no filler."""

# Tags that only mean something in Agent mode. In Chat mode they are neither
# run nor shown, and ```bash``` / ```python``` fences render as ordinary code.
_AGENT_TAGS = ("skill", "runskill", "read", "grep", "glob", "map", "edit", "check", "project",
               "tree", "package", "runtests", "write", "rmfile", "junk", "ledger", "plan",
               "screenshot", "look", "task", "bg", "logs", "kill", "undo", "open")
# (the agent-OS tags, spawn/join/send/wait/post/board/daemon, are registered tools)

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
button.tray { background-color: @surface; border: 1px solid @line; border-radius: 999px;
             padding: 3px 12px; min-height: 0; color: @body; font-size: 11.5px;
             font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; box-shadow: none; }
button.tray:hover { border-color: alpha(@accent, 0.5); background-color: @surface2; }
button.tray.tray-ask { border-color: @accent; color: @accent_hi; background-color: @accent_bg; }
window.palette { background-color: transparent; }
.palette-box { background-color: @surface; border: 1px solid @line2; border-radius: 14px;
               padding: 8px; box-shadow: 0 18px 48px rgba(0,0,0,0.55); }
.palette-search { font-size: 15px; min-height: 40px; margin: 2px 2px 8px 2px; }
.palette-list { background: transparent; }
.palette-list > row { padding: 7px 10px; border-radius: 8px; color: @body; }
.palette-list > row:selected { background-color: @accent_bg; color: @ink; }
.palette-list > row:selected .palette-hint { color: @accent_hi; }
.palette-group { font-size: 10.5px; letter-spacing: 1px; font-weight: 700; color: @accent_hi;
                 margin: 10px 4px 2px 4px; }
.palette-hint { font-family: "JetBrains Mono", "DejaVu Sans Mono", monospace; font-size: 11px; }
.palette-foot { font-size: 11px; margin: 8px 6px 2px 6px; }

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
.icon-btn.mic-wait { color: @accent_hi; }
.icon-btn.mic-on { color: #F87171; background-color: alpha(#F87171, 0.10);
                   box-shadow: 0 0 0 1px alpha(#F87171, 0.45); }
.icon-btn.mic-speech { background-color: alpha(#F87171, 0.22);
                       box-shadow: 0 0 0 2px alpha(#F87171, 0.55), 0 0 14px alpha(#F87171, 0.35); }
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




PLOTS_DIR = CONFIG_DIR / "plots"      # ```py``` figures (writable inside the sandbox)


def _sweep_scratch(max_age_hours=6):
    """Drop stale scratch files in CONFIG_DIR (fetched images, synthesised wavs,
    interpreter temp files, an old screenshot). Each of these is now unlinked by
    the code that creates it, but an existing install has a backlog, and a hard
    kill can still strand one."""
    cutoff = time.time() - max_age_hours * 3600
    for pat in (".img_*", ".say-*.wav", ".run_*", ".shot.png", ".att_*", ".see_*", ".plot_*",
                "plots/.plot_*"):
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


def _md_inline(s, emphasis=True):
    """Inline spans, run on already-escaped text: `code`, **bold**, *italic*,
    ~~strike~~ and [links](url). Inline code gets a subtle chip so it reads as
    code without the raw backticks; no padding spaces, which would create ugly
    wrap points. Code is matched FIRST and its content is parked in a
    placeholder, so a snippet like `a * b` can't be re-read as italics."""
    parked = []
    s = s.replace("\x00", "")                  # NUL is our placeholder mark (and ends a label)

    def park(markup):
        parked.append(markup)
        return f"\x00{len(parked) - 1}\x00"

    def emph(t):
        if not emphasis:
            # the fallback when spans overlap: no markup, and no raw markers
            for _ in range(3):                  # nested markers come off layer by layer
                t = re.sub(r"(\*\*|__|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
            return re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", t)
        t = re.sub(r"~~(?=\S)([^~]+?)(?<=\S)~~", r"<s>\1</s>", t)
        # bold and italic together: ***x***, **a *b***, ***a* b**
        t = re.sub(r"\*\*\*(?=\S)([^*]+?)(?<=\S)\*\*\*", r"<b><i>\1</i></b>", t)
        t = re.sub(r"\*\*(?=\S)([^*]*?)\*(?=\S)([^*]+?)(?<=\S)\*\*\*", r"<b>\1<i>\2</i></b>", t)
        t = re.sub(r"\*\*\*(?=\S)([^*]+?)(?<=\S)\*([^*]*?)(?<=\S)\*\*", r"<b><i>\1</i>\2</b>", t)
        # ** that hugs its words: `2 ** 10 and 3 ** 2` is arithmetic, not bold
        t = re.sub(r"\*\*(?=\S)([^*]+?)(?<=\S)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"__(?=\S)([^_]+?)(?<=\S)__", r"<b>\1</b>", t)
        return re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", t)

    s = re.sub(r"`([^`]+)`", lambda m: park(
        f'<span font_family="monospace" background="{CODE_BG}" '
        f'foreground="{CODE_FG}">{m.group(1)}</span>'), s)
    # [text](url) — GtkLabel renders <a href> as a real, clickable link. Parked
    # too, so ** or __ inside a URL (pkg/__init__.py) is never formatted. One
    # level of parentheses is part of a URL (wiki/C_(programming_language)).
    # The text is ALREADY HTML-escaped: the href only needs its quotes
    # neutralised — escaping again would break the query string.
    s = re.sub(r"\[([^\]]+)\]\((https?://(?:[^\s()]|\([^\s()]*\))+)\)",
               lambda m: park(f'<a href="{m.group(2).replace(chr(34), "&quot;")}">'
                              f'{emph(m.group(1))}</a>'), s)
    s = emph(s)
    for _ in range(2):                          # a link label may hold inline code
        s = re.sub(r"\x00(\d+)\x00", lambda m: parked[int(m.group(1))]
                   if int(m.group(1)) < len(parked) else "", s)
    return s


# A blank line between paragraphs, rendered at full body height with 1.4 line
# spacing, is a 27px hole — after a heading it looked like the reply had ended.
# Paragraph gaps are drawn as a short spacer line instead.
_GAP = '<span size="5000"> </span>'


def md_to_pango(text, emphasis=True):
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
                       f'{_md_inline(esc(m.group(2)), emphasis)}</span>')
            continue
        if re.match(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$", raw):
            out.append(f'<span foreground="{FAINT}">'
                       + "─" * 28 + '</span>')
            continue
        m = re.match(r"^\s{0,3}>\s?(.*)$", raw)
        if m:
            out.append(f'<span foreground="{MUTE}"><i>▎  '
                       f'{_md_inline(esc(m.group(1)), emphasis)}</i></span>')
            continue
        m = re.match(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            done = m.group(2).lower() == "x"
            box = ('<span foreground="#4ADE9A">☑</span>' if done
                   else f'<span foreground="{FAINT}">☐</span>')
            body = _md_inline(esc(m.group(3)), emphasis)
            out.append("    " * depth + box + "  "
                       + (f'<span foreground="{MUTE}">{body}</span>' if done else body))
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            mark = ("•", "◦", "‣", "•")[depth]
            out.append("    " * depth
                       + f'<span foreground="{ACCENT}">{mark}</span>  '
                       + _md_inline(esc(m.group(2)), emphasis))
            continue
        m = re.match(r"^(\s*)(\d+)[.)]\s+(.*)$", raw)
        if m:
            depth = min(len(m.group(1)) // 2, 3)
            out.append("    " * depth
                       + f'<span foreground="{ACCENT}" weight="bold">{m.group(2)}.</span>  '
                       + _md_inline(esc(m.group(3)), emphasis))
            continue
        out.append(_md_inline(esc(raw), emphasis))
    while out and out[-1] == _GAP:
        out.pop()
    return "\n".join(out)


# a fence may be indented up to three spaces (code inside a numbered step),
# be ``` or ~~~ of any length, and closes only on a line of at least as many
# of the same mark; an empty block closes on the very next line
_FENCE_RE = re.compile(r"^(?P<ind>[ \t]{0,3})(?P<f>`{3,}|~{3,})(?P<lang>[\w+#.-]*)[^\n]*\n"
                       r"(?P<body>.*?)(?:^[ \t]{0,3}(?P=f)[`~]*[ \t]*$|\Z)", re.M | re.S)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line):
    """Cells of a table row. A | inside `code` or written \\| is content."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells = [c.replace("\\|", "|").strip()
             for c in re.findall(r"(?:`[^`]*`|\\\||[^|`])+|(?<=\|)(?=\|)|^(?=\|)", line)]
    return cells if cells else [""]


def md_segments(text):
    """Split a reply into ("md", text) / ("code", lang, body) / ("table", rows)
    segments, so code renders as a copyable block that scrolls sideways
    instead of overflowing the column, and tables render as a real grid.
    An unclosed fence (a reply cut off mid-block) still renders as code."""
    text = (text or "").replace("\r\n", "\n")
    segs = []
    pos = 0
    for m in _FENCE_RE.finditer(text):
        if m.start() > pos:
            segs.extend(_table_split(text[pos:m.start()]))
        body, ind = m.group("body").rstrip("\n"), m.group("ind")
        if ind:                                 # the fence's indent isn't part of the code
            body = "\n".join(ln[len(ind):] if ln.startswith(ind) else ln.lstrip(" \t")
                             for ln in body.split("\n"))
        segs.append(("code", m.group("lang").lower(), body))
        pos = m.end()
    if pos < len(text):
        segs.extend(_table_split(text[pos:]))
    out = []
    for sg in segs:
        if sg[0] == "md" and len(sg[1]) > _MD_CHUNK:
            out.extend(("md", t) for t in _paragraph_chunks(sg[1]))
        else:
            out.append(sg)
    return [sg for sg in out if not (sg[0] == "md" and not sg[1].strip())]


_MD_CHUNK = 3000


def _paragraph_chunks(text):
    """Long prose as several labels, cut at paragraph breaks: while a long
    reply streams, only the last one is re-laid out (one 100k label took
    0.4 s per repaint)."""
    parts, cur = [], ""
    for para in re.split(r"(\n\s*\n)", text):
        if len(cur) + len(para) > _MD_CHUNK and cur.strip() and not para.strip() == "":
            parts.append(cur)
            cur = ""
        cur += para
    if cur:
        parts.append(cur)
    return parts


def _table_split(text):
    lines = text.split("\n")
    out, buf, i = [], [], 0
    while i < len(lines):
        if (i + 1 < len(lines) and "|" in lines[i] and _TABLE_SEP.match(lines[i + 1])
                and len(_split_row(lines[i])) >= 2
                and len(_split_row(lines[i])) == len(_split_row(lines[i + 1]))):
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
        if not _markup_ok(markup):
            # overlapping spans (~~**a~~**): keep headings, lists and code,
            # drop the emphasis, rather than show the whole block raw
            markup = md_to_pango(text, emphasis=False)
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
# where a command starts, as both detection and the sudo -A rewrite see it:
# after a separator, `{`, `!`, a backtick or $( , a shell keyword, a VAR=x
# assignment, or a wrapper (time, env, exec, xargs, nice, nohup)
_SUDO_PREFIX = (r"((?:^|[\n;&|({!`]|\$\()\s*(?:(?:then|do|else|elif|time|env|exec|xargs|nice"
                r"|nohup|command)\s+|\w+=\S*\s+)*)")
_SUDO_RE = re.compile(_SUDO_PREFIX + r"sudo\b")
_SUDO_INJECT_RE = re.compile(_SUDO_PREFIX + r"sudo(?=\s|$)(?!\s+-A\b)")
# these call sudo themselves (AUR helpers, makepkg -si): they need the password too
_SUDO_INSIDE_RE = re.compile(_SUDO_PREFIX + r"(?:paru|yay|pamac\s+(?:install|remove|upgrade|update)"
                             r"|makepkg\s+(?:\S+\s+)*?-\w*[si])\b")


def command_needs_sudo(command):
    """True if the command contains a real `sudo` invocation."""
    return bool(command) and bool(_SUDO_RE.search(command) or _SUDO_INSIDE_RE.search(command))


def _inject_askpass(command):
    """Turn each `sudo` into `sudo -A` (use SUDO_ASKPASS). Unlike -S, askpass
    never reads the command's stdin, so `sudo -A tee file` still works."""
    out = _SUDO_INJECT_RE.sub(r'\1sudo -A', command) if "sudo" in command else command
    # paru and yay run sudo themselves: tell them to use the askpass helper too
    return re.sub(_SUDO_PREFIX + r"(paru|yay)\b(?![^\n;&|]*--sudoflags)",
                  r"\1\2 --sudoflags -A", out)


def _ensure_askpass_helper():
    """Write (once, 0700) a tiny askpass helper that prints the password file
    named by $CHUCK_SUDO_PWFILE. Neither the helper nor the environment holds
    the secret: an env var would be inherited by everything the script runs
    (and `env` would print it into the chat)."""
    path = CONFIG_DIR / ".chuck-askpass.sh"
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            os.fchmod(fd, 0o700)          # defeat umask before content lands
            os.write(fd, b'#!/bin/sh\ncat "$CHUCK_SUDO_PWFILE" 2>/dev/null\n')
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
    pwfile = None
    try:
        # a 0600 file in the user's runtime dir (tmpfs), removed straight after
        fd, pwfile = tempfile.mkstemp(prefix=".chuck-pw-",
                                      dir=os.environ.get("XDG_RUNTIME_DIR") or None)
        with os.fdopen(fd, "w") as fh:
            fh.write(password + "\n")
        e2["CHUCK_SUDO_PWFILE"] = pwfile
        p = _guarded_proc([_bash_bin(), "-c", _inject_askpass(script)], timeout, e2)
        low = (p.stderr or "").lower()
        if p.returncode != 0 and ("a terminal is required" in low or "askpass" in low
                                  or "no password was provided" in low
                                  or "a password is required" in low):
            return _SUDO_AUTH_FAILED, "sudo: askpass authentication failed."
        return p.returncode, _scrub_pw(_shell_out(p), password)
    except subprocess.TimeoutExpired:
        return 124, _timeout_note(script, timeout)
    except Exception as ex:
        return 1, f"(error: {ex})"
    finally:
        if pwfile:
            try:
                os.unlink(pwfile)
            except OSError:
                pass


def _scrub_pw(text, password):
    """Whatever a command printed, the password never reaches the chat."""
    return text.replace(password, "\u2022\u2022\u2022\u2022") if password and len(password) >= 3 else text


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
    if (_ASKPASS_FIRST[0] or _SUDO_INSIDE_RE.search(script)) and pe["askpass"]:
        # this machine's sudo doesn't carry a validated password to the
        # command's own sudo calls: authenticate each one, from the start
        alt = _run_sudo_askpass(script, password, timeout, env)
        if alt is not None:
            return alt
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
    # -v succeeded but an inner sudo still failed (the timestamp didn't carry).
    # Running the script AGAIN with askpass would repeat everything that did
    # run (an append, a mv): instead say so, and use askpass from now on.
    low = (p.stderr or "").lower()
    if p.returncode != 0 and pe["askpass"] and (
            "a terminal is required" in low or "no password was provided" in low
            or "a password is required" in low or "askpass" in low):
        _ASKPASS_FIRST[0] = True
        return p.returncode, _scrub_pw(_shell_out(p), password) + (
            "\n(sudo on this machine doesn't keep the password between calls, so the "
            "sudo steps above failed; the rest of the script ran once. Chuck now "
            "authenticates every sudo call itself \u2014 run the sudo steps again.)")
    return p.returncode, _scrub_pw(_shell_out(p), password)


_ASKPASS_FIRST = [False]


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
    "mkinitcpio", "grub-mkconfig", "reflector", "dkms", "topgrade", "paccache",
    "fwupdmgr", "pikaur", "aura", "trizen", "pacstrap", "rustup", "conda", "mamba",
}
# compared against the LOWERCASED command: the pacman flags must be lowercase
_LONG_WORDS = {"upgrade", "dist-upgrade", "install", "update", "build",
               "compile", "pull", "clone", "download", "-syu", "-s", "-syyu", "-su"}
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
                "sudo", "doas", "pkexec", "run0", "nohup", "time", "env", "exec", "setsid",
                "stdbuf", "nice", "ionice", "timeout", "command")
                or (i > 0 and words[i].startswith("-"))      # sudo -E, nice -n 5, timeout 60
                or (i > 0 and words[i - 1] in ("timeout", "-n") and words[i][:1].isdigit())):
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
    if _SANDBOX["on"] and command_needs_sudo(script):
        return 126, ("(not run: this folder is Sandboxed, so nothing can use sudo or change the "
                     "system. If it truly needs root, ask the user to give the folder Full "
                     "access with /access.)")
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
    bufs = {"out": _HeadTail(), "err": _HeadTail()}

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
                bufs[key].add(text)
                if on_chunk is not None:
                    try:
                        on_chunk(text)
                    except Exception:
                        pass
        tail = dec.decode(b"", final=True)
        if tail:
            bufs[key].add(tail)

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
    return bufs["out"].text(), bufs["err"].text()


class _HeadTail:
    """A command's output, bounded: the first and the last MB are kept (the
    model sees 20k characters and the screen 12k), with a note of what was
    left out between them. `yes` or a chatty build can't fill the memory."""
    KEEP = 1 << 20

    def __init__(self):
        self.head, self.tail, self.tail_len, self.dropped = [], [], 0, 0
        self.head_len = 0

    def add(self, text):
        if self.head_len < self.KEEP:
            take = text[:self.KEEP - self.head_len]
            self.head.append(take)
            self.head_len += len(take)
            text = text[len(take):]
            if not text:
                return
        self.tail.append(text)
        self.tail_len += len(text)
        while self.tail_len - len(self.tail[0]) >= self.KEEP:
            gone = self.tail.pop(0)
            self.tail_len -= len(gone)
            self.dropped += len(gone)

    def text(self):
        mid = (f"\n\u2026 [{self.dropped:,} characters of output not kept] \u2026\n"
               if self.dropped else "")
        return "".join(self.head) + mid + "".join(self.tail)


# A folder's access can be "Sandboxed" (/access, Settings): then commands,
# code and the ```py``` session run under bubblewrap: the whole system
# read-only, only the working folder writable, a private /tmp, no sudo, and
# no network unless the folder allows it. Module-level, like _WORKDIR, so
# every runner sees the same policy.
_SANDBOX = {"on": False, "net": False, "root": None, "writable": [], "mode": "full"}
SANDBOX_NO_BWRAP = ("(not run: this folder is Sandboxed but bubblewrap isn't installed \u2014 "
                    "install bubblewrap, or give the folder Full access with /access)")


# What a sandboxed command must never see: credentials, and Chuck's own config
# (the API key). They are hidden behind empty tmpfs mounts.
_SANDBOX_HIDE = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store",
                 ".config/gh", ".config/chucknorris", ".local/share/keyrings")
_SANDBOX_CACHE = DATA_DIR / "sandbox-cache"


def _secret_path(p):
    """Is p inside a credentials folder the sandbox hides (~/.ssh, the API key)?
    Chuck's own read/grep run outside the sandbox, so in a restricted folder
    they must refuse these too, or the model reads what commands can't."""
    try:
        rp = os.path.realpath(p)
    except (OSError, ValueError):
        return False
    home = str(Path.home())
    for rel in _SANDBOX_HIDE + (".netrc",):
        h = os.path.realpath(os.path.join(home, rel))
        if rp == h or rp.startswith(h + os.sep):
            return True
    return False


def _bwrap_argv(argv, env=None, write_root=True, net=None, persistent=False, root=None,
                extra_ro=()):
    """bubblewrap around argv: the system read-only, the working folder
    writable (or read-only, for tools that only look), a private /tmp, /run
    and /var/tmp (no D-Bus, docker or ssh-agent sockets to escape through),
    credentials hidden, its own PID namespace (Stop kills everything it
    started) and, unless allowed, no network. `root` overrides the working
    folder (a background agent's own copy); `extra_ro` are paths under the
    private /run to show read-only (a daemon reads the journal and the
    system bus)."""
    root = root or _SANDBOX["root"]
    home = str(Path.home())
    args = [shutil.which("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run",
            "--unshare-pid", "--new-session"]
    # --die-with-parent fires when the THREAD that started it exits, so a
    # process meant to outlive one step (the Python session, a language
    # server) must not have it: it would die after its first use. Those exit
    # on their own when Chuck closes their input.
    if not persistent:
        args.append("--die-with-parent")
    try:                                       # DNS: resolv.conf usually points into /run
        rc = os.path.realpath("/etc/resolv.conf")
        if rc.startswith("/run/") and os.path.exists(rc):
            args += ["--ro-bind", rc, rc]
    except OSError:
        pass
    for var in ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DISPLAY",
                "WAYLAND_DISPLAY", "XAUTHORITY"):
        args += ["--unsetenv", var]
    cache = str(_SANDBOX_CACHE)
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError:
        cache = None
    binds = [(root, write_root)] + [(w, True) for w in (_SANDBOX.get("writable") or [])]
    if cache:
        binds.append((cache, True))
    cwdf = (env or {}).get("CHUCK_CWD_FILE")
    if cwdf:
        binds.append((cwdf, True))             # where the shell reports its `cd`
    for b, rw in binds:
        if b and os.path.exists(b):
            args += ["--bind" if rw else "--ro-bind", b, b]
    # credentials are hidden AFTER the binds: bwrap mounts in order, so a
    # working folder of ~ bound later would bring ~/.ssh back
    hidden = []
    for rel in _SANDBOX_HIDE:
        p = os.path.join(home, rel)
        if os.path.isdir(p):
            args += ["--tmpfs", p]
            hidden.append(p)
    if os.path.isfile(os.path.join(home, ".netrc")):
        args += ["--ro-bind", "/dev/null", os.path.join(home, ".netrc")]
    for b, rw in binds[1:]:                    # Chuck's own folders that live under a hidden one
        if b and os.path.exists(b) and any(b.startswith(h + os.sep) for h in hidden):
            args += ["--bind" if rw else "--ro-bind", b, b]
    for a in argv:                             # a script Chuck wrote into its config folder
        if isinstance(a, str) and a.startswith(str(CONFIG_DIR) + os.sep) and os.path.isfile(a):
            args += ["--ro-bind", a, a]
    if cache:                                  # package managers write their caches here
        for var, sub in (("XDG_CACHE_HOME", ""), ("npm_config_cache", "npm"),
                         ("PIP_CACHE_DIR", "pip"), ("CARGO_HOME", "cargo"), ("GOCACHE", "go-build"),
                         ("GOMODCACHE", "go-mod"), ("YARN_CACHE_FOLDER", "yarn"),
                         ("BUN_INSTALL_CACHE_DIR", "bun")):
            args += ["--setenv", var, os.path.join(cache, sub) if sub else cache]
    for x in extra_ro:
        if os.path.exists(x):
            args += ["--ro-bind", x, x]
    if not (_SANDBOX["net"] if net is None else net):
        args.append("--unshare-net")
    return args + ["--"] + list(argv)


def _sandboxed(argv, env=None, persistent=False):
    """argv as it must run under the current policy, or None when the folder is
    sandboxed and there is no bubblewrap to do it (then it must not run)."""
    if not _SANDBOX["on"]:
        return list(argv)
    if not shutil.which("bwrap"):
        return None
    return _bwrap_argv(argv, env, persistent=persistent)


def _sandboxed_look(argv, persistent=False):
    """For tools that only LOOK at the project (tests, linters, language
    servers) but run its code: in a sandboxed folder they get the sandbox; in
    a read-only one, the project mounted read-only and no network."""
    mode = _SANDBOX.get("mode", "full")
    if mode == "full":
        return list(argv)
    if not shutil.which("bwrap"):
        return None
    return _bwrap_argv(argv, None, write_root=mode != "readonly",
                       net=mode == "sandbox-net", persistent=persistent)


def _guarded_proc(argv, timeout, env, input_text=None):
    """Run argv in its own process group, registered as live while it runs.
    Returns a CompletedProcess. On timeout the WHOLE tree is killed and
    TimeoutExpired is raised; a Stop (kill_live_commands) ends it early with
    whatever exit status the kill produced."""
    on_chunk = getattr(_LIVE_OUT, "fn", None)
    wrapped = _sandboxed(argv, env)
    if wrapped is None:
        return subprocess.CompletedProcess(argv, 126, "", SANDBOX_NO_BWRAP)
    argv = wrapped
    proc = subprocess.Popen(argv, cwd=_cwd(),
                            stdin=subprocess.PIPE if input_text is not None
                            else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, preexec_fn=_sandbox_preexec)
    with _LIVE_LOCK:
        _LIVE_PROCS.add(proc)
    try:
        try:
            # always streamed: bounded buffers, whoever is (or isn't) watching
            out, err = _streamed(proc, input_text, timeout, on_chunk)
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


def _group_alive(pgid):
    """Does any process of this group still exist?"""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


# what a daemon may see under the private /run
_LOOK_RUN = ("/run/log/journal",)        # the journal only: NO system bus, so nothing a
                                          # daemon runs can reach services, power or logins


def _run_in(root, command, timeout, net=True, write=True):
    """Run a background agent's command in ITS copy of the project, jailed to
    that copy when bubblewrap is there. write=False (a daemon): everything
    read-only, the folder too. -> (rc, output)."""
    env = dict(os.environ)
    env.update(_RUN_ENV)
    argv = [_bash_bin(), "-c", command]
    if shutil.which("bwrap"):
        argv = _bwrap_argv(argv, env, root=str(root), net=net, write_root=write,
                           extra_ro=() if write else _LOOK_RUN)
    try:
        proc = subprocess.Popen(argv, cwd=str(root), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                start_new_session=True)
    except OSError as ex:
        return 127, f"(couldn't start: {ex})"
    with _LIVE_LOCK:
        _LIVE_PROCS.add(proc)
    try:
        out, err = _streamed(proc, None, timeout, None)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return 124, _timeout_note(command, timeout)
    finally:
        with _LIVE_LOCK:
            _LIVE_PROCS.discard(proc)
    return proc.returncode, _shell_out_parts(out, err)


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
    lines.append(f"thumbnail cache: {sz('~/.cache/thumbnails')} (of ~/.cache {sz('~/.cache')})")
    cmds.append(("Clear thumbnail cache", "rm -rf ~/.cache/thumbnails/*"))
    if shutil.which("pacman"):
        lines.append(f"pacman package cache: {sz('/var/cache/pacman/pkg')}")
        if not shutil.which("paccache"):
            # its own, honestly labelled step: on Arch installing anything
            # means syncing and upgrading the whole system first
            cmds.append(("Install paccache (pacman-contrib) \u2014 upgrades the system first",
                         "sudo pacman -Syu --needed pacman-contrib"))
        cmds.append(("Trim pacman cache (keep 1)", "sudo paccache -rk1"))
        orph = _run_ro(["sh", "-c", "pacman -Qtdq 2>/dev/null | wc -l"])
        lines.append(f"orphan packages: {orph}")
        if orph and orph != "0":
            cmds.append(("Remove orphans", "pacman -Qtdq | sudo pacman -Rns --noconfirm -"))
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
        # who is calling (the governor picks the model by role) and a model a
        # program pinned for itself
        self.role = "main"
        self.pin = None

    def key(self):
        return (self.s.get("siliconflow_api_key") or "").strip()

    def base(self):
        return (self.s.get("siliconflow_base_url") or DEFAULT_BASE).rstrip("/")

    def model(self, vision=False):
        """The chat model — unless this request carries images and the chat
        model can't see, in which case the vision model takes it. With a
        multimodal chat model (the default) one model does both."""
        chat = (self.s.get("model") or DEFAULT_MODEL).strip()
        if self.pin:
            chat = self.pin
        elif _governor is not None:
            chat = _governor.pick(self.s, self.role, chat, _pricing.PRICES, _pricing.SUGGESTED,
                                  lambda m: _pricing.price_for(m, self.s))
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
                    # would duplicate it, so keep what we have and carry on:
                    # as a CUT reply, so the continuation logic picks it up (a
                    # ```write cut by a dropped connection would otherwise be
                    # silently skipped and the turn end as "done")
                    self.last_finish = "length"
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
            text = self.text
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
    """The welcome screen's background: the fabric Chuck is woven from.

    A hidden net of tiny code glyphs. At rest nothing is drawn (zero idle
    cost). Move the pointer and a soft lens of the fabric surfaces under it,
    bulging gently and settling when you stop. Click and the fabric is struck:

      * the wavefront is not a compass circle: each strike gets its own
        lobed shape from smooth angular noise that drifts as it travels, so
        it rolls out unevenly, the way a disturbance runs through cloth;
      * it is a real wave: glyphs ahead of the front are pushed out, those
        behind it pulled back, with a sideways weave, so the net undulates
        rather than merely lighting up;
      * neighbours on the crest are joined by faint threads: the mesh is
        visible exactly where it flexes;
      * colour travels with it: a cool white leading edge, a violet crest,
        and an indigo wake where characters keep decoding as it fades.

    Built to stay smooth with ~18,000 glyphs: a frame visits only the glyphs
    in each front's annulus (row spans from the circle equation, padded for
    the lobes), the front shape is a per-frame angle table rather than noise
    per glyph, falloffs are lookup tables, and glyphs are drawn in batched
    show_glyphs() calls per colour band and brightness level. Honours the
    desktop's reduce-animation setting. Not a subclass (the headless test
    stub can't be subclassed): it owns a DrawingArea instead.
    """
    CHARS = "{}[]()<>=;:+-*/#&|~$%01λ0123456789abcdef"
    CELL_W, CELL_H, FONT = 7, 9, 6.0
    SPEED, LIFE = 300.0, 2.6
    LEAD, WAKE = 7.0, 58.0           # px ahead of the front / length of the wake
    LOBE = 0.24                      # how far the front strays from a circle
    PUSH, WEAVE = 6.0, 3.0
    # each strike sends a main front and a slower, fainter echo that trails
    # it: (speed, strength, shape seed offset, wake length)
    FRONTS = ((1.0, 1.0, 0, 58.0), (0.6, 0.5, 17, 16.0))
    BLOOM_R, BLOOM_T = 46.0, 0.45    # the flash where the click lands
    LEVELS = 6
    MAX_WAVES = 4
    ANGLES = 128
    LENS_R, LENS_FADE = 120.0, 0.9
    STEP = 320                       # the net grows in these steps
    # (lead, crest, wake) colours: cool white, violet, indigo
    BANDS = ((0.86, 0.90, 1.00), (0.70, 0.55, 1.00), (0.42, 0.36, 0.86))

    def __init__(self):
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_can_target(False)
        self.ripples = []            # [x, y, t0, seed]
        self.pointer = None          # (x, y, t) of the last motion, for the lens
        self._lens_since = 0.0
        self._tick = 0
        self._size = (0, 0)
        self._rows = []              # rows[r][c] = (x, y, char_idx, bright, seed) | None
        self._gids = None            # glyph id per CHARS entry, for show_glyphs
        self._lead = [math.exp(-(o / self.LEAD) ** 2) for o in range(int(self.LEAD * 3) + 1)]
        self._wake = [math.exp(-o / (self.WAKE * 0.38)) for o in range(int(self.WAKE) + 1)]
        self._lens_shape = None
        self._strikes = 0
        self._thin = 0                   # 0-3: how much of the wake to skip when frames run slow
        self._frame_ms = 0.0
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
        """Strike on clicks and follow the pointer anywhere over `widget`,
        without stealing either from the buttons on top (capture phase,
        never claims the sequence)."""
        try:
            g = Gtk.GestureClick()
            g.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            g.connect("pressed", lambda _g, _n, x, y: self.poke(x, y))
            widget.add_controller(g)
        except Exception:
            pass
        try:
            m = Gtk.EventControllerMotion()
            m.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            m.connect("motion", lambda _m, x, y: self.hover(x, y))
            m.connect("leave", lambda _m: self.hover(None, None))
            widget.add_controller(m)
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
                bright = (0.55 + rnd.random() * 0.45) * (0.5 + 0.5 * min(1.0, d * 1.25))
                row.append((x, y, rnd.randrange(n), bright, rnd.randrange(997)))
            rows.append(row)
        self._rows = rows
        self._size = (W, H)

    def _run(self):
        if not self._tick:
            try:
                self._tick = self.area.add_tick_callback(self._on_tick)
            except Exception:
                self._tick = 0
        self.area.queue_draw()

    def poke(self, x, y):
        if not self.enabled or not self._animations_on():
            return
        self._strikes += 1
        seed = (self._strikes * 7919 + int(x) * 31 + int(y)) % 100003
        self.ripples = (self.ripples + [(float(x), float(y), time.monotonic(), seed)]
                        )[-self.MAX_WAVES:]
        self._run()

    def hover(self, x, y):
        if not self.enabled or not self._animations_on():
            return
        now = time.monotonic()
        if x is None:
            if self.pointer is not None:
                self.pointer = (self.pointer[0], self.pointer[1], now - 0.35)   # fade out
        else:
            if self.pointer is None or now - self.pointer[2] > self.LENS_FADE:
                self._lens_since = now
            self.pointer = (float(x), float(y), now)
        self._run()

    def _lens_strength(self, now):
        if self.pointer is None:
            return 0.0
        idle = now - self.pointer[2]
        rise = min(1.0, (now - self._lens_since) / 0.25)
        return rise * max(0.0, 1.0 - max(0.0, idle - 0.35) / self.LENS_FADE)

    def _on_tick(self, _w, _clock):
        now = time.monotonic()
        self.ripples = [r for r in self.ripples if now - r[2] < self.LIFE]
        if self.pointer is not None and self._lens_strength(now) <= 0.0:
            self.pointer = None
        self.area.queue_draw()
        if not self.ripples and self.pointer is None:
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

    def _front(self, seed, age):
        """This strike's radius multiplier per angle bin: smooth, lobed, and
        drifting with time, so no two strikes spread alike."""
        rnd = (seed * 2654435761) & 0xFFFFFFFF
        terms = []
        for k in (2, 3, 5, 7):
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
            ph = rnd / 0x7FFFFFFF * 2 * math.pi
            rnd = (rnd * 1103515245 + 12345) & 0x7FFFFFFF
            amp = (0.35 + rnd / 0x7FFFFFFF * 0.65) / k ** 0.7
            terms.append((k, ph, amp, (1 if k % 2 else -1) * 0.5 / k))
        norm = sum(a for _k, _p, a, _s in terms) or 1.0
        out = []
        for i in range(self.ANGLES):
            th = i / self.ANGLES * 2 * math.pi
            v = sum(a * math.sin(k * th + p + s * age * 2 * math.pi) for k, p, a, s in terms)
            out.append(1.0 + self.LOBE * v / norm)
        return out

    def _lit(self, w, h, now):
        """{(row, col): [intensity, dx, dy, age, band]} for every glyph a
        strike (or the pointer's lens) is on. band 0 lead, 1 crest, 2 wake."""
        hit = {}
        cw, ch = self.CELL_W, self.CELL_H
        lead, wake = self._lead, self._wake
        nlead, nwake = len(lead) - 1, len(wake) - 1
        sqrt, atan2, sin, pi = math.sqrt, math.atan2, math.sin, math.pi
        PUSH, WEAVE = self.PUSH, self.WEAVE
        sparse = 5 + 3 * self._thin
        nrows = len(self._rows)
        A = self.ANGLES
        a_k = A / (2 * math.pi)
        fronts = [(rx, ry, t0, seed, fsp, fam, fso, fwk)
                  for (rx, ry, t0, seed) in self.ripples
                  for (fsp, fam, fso, fwk) in self.FRONTS]
        for (rx, ry, t0, seed, fsp, fam, fso, fwk) in fronts:
            age = now - t0
            # the front decelerates a little as it spreads, like a real wave
            rad = fsp * self.SPEED * age * (1.0 - 0.18 * age / self.LIFE)
            amp = fam * (1.0 - age / self.LIFE) ** 1.6
            if amp <= 0.02:
                continue
            nwake = min(int(fwk), len(wake) - 1)
            shape = self._front(seed + fso, age)
            lo, hi = min(shape), max(shape)
            # the shape's local extremes around each angle, so a row only
            # scans the band where the front actually is near it
            win = 9
            ext = shape[-win:] + shape + shape[:win]
            lo_w = [min(ext[b:b + 2 * win + 1]) for b in range(A)]
            hi_w = [max(ext[b:b + 2 * win + 1]) for b in range(A)]
            tick = int(age * 18)
            r1 = max(0.0, rad * lo - nwake)
            r2 = rad * hi + nlead
            r1s, r2s = r1 * r1, r2 * r2
            row_lo = max(0, int((ry - r2 - 7) // ch))
            row_hi = min(nrows - 1, int((ry + r2 - 7) // ch) + 1)
            cap2 = (rad * lo * 0.8) ** 2
            for r in range(row_lo, row_hi + 1):
                row = self._rows[r]
                y = r * ch + 7
                dy = y - ry
                dy2 = dy * dy
                if dy2 > r2s:
                    continue
                shift = 1 + (r % 2) * (cw / 2)
                deep = -1.0                     # |dx| below this is surely deep in the wake
                if dy2 < cap2:
                    # the row crosses the front twice: bound each crossing locally
                    cx = sqrt(rad * rad - dy2)
                    spans, deeps = [], []
                    for side in (-1.0, 1.0):
                        b = int((atan2(dy, side * cx) + pi) * a_k) % A
                        ri = max(0.0, rad * lo_w[b] - nwake)
                        ro = rad * hi_w[b] + nlead
                        xi = sqrt(ri * ri - dy2) if ri * ri > dy2 else 0.0
                        xo = sqrt(ro * ro - dy2)
                        rd = rad * lo_w[b] - 12
                        deeps.append(sqrt(rd * rd - dy2) if rd > 0 and rd * rd > dy2 else -1.0)
                        spans.append((rx - xo, rx - xi) if side < 0 else (rx + xi, rx + xo))
                    deep = min(deeps)               # inside the wake on BOTH sides, or nowhere
                else:
                    outer = sqrt(r2s - dy2)
                    inner = sqrt(r1s - dy2) if dy2 < r1s else 0.0
                    # a row through the ring's cap is one span, else one each side
                    spans = (((rx - outer, rx + outer),) if inner == 0.0 else
                             ((rx - outer, rx - inner), (rx + inner, rx + outer)))
                for xa, xb in spans:
                    c0 = max(0, int((xa - shift) // cw))
                    c1 = min(len(row) - 1, int((xb - shift) // cw) + 1)
                    for c in range(c0, c1 + 1):
                        g = row[c]
                        if g is None:
                            continue
                        dx = g[0] - rx
                        if -deep < dx < deep and ((g[4] + tick) % sparse) > 1:
                            continue            # deep wake, and not its turn to decode
                        d = sqrt(dx * dx + dy2)
                        th = atan2(dy, dx)
                        o = d - rad * shape[int((th + pi) * a_k) % A]
                        if o >= 0:
                            if o >= nlead:
                                continue
                            k = amp * lead[int(o)]
                            band, wave = 0, 1.0
                        else:
                            o = -o
                            if o >= nwake:
                                continue
                            band = 1 if o < 12 else 2
                            # the wake decodes: a shifting few glyphs at a time
                            if band == 2 and ((g[4] + tick) % sparse) > 1:
                                continue
                            k = amp * wake[int(o)]
                            wave = 1.0 - o / 14.0 if o < 28 else -1.0   # out, then back
                        if k < 0.04:
                            continue
                        key = r * 4096 + c
                        e = hit.get(key)
                        if e is None:
                            e = hit[key] = [0.0, 0.0, 0.0, age, band]
                        e[0] += k
                        if band < e[4]:
                            e[4] = band
                        if d > 0.5:
                            ux, uy = dx / d, dy / d
                            push = k * PUSH * wave
                            weave = k * WEAVE * sin(th * 6 + age * 7 + g[4])
                            e[1] += ux * push - uy * weave
                            e[2] += uy * push + ux * weave
        for (rx, ry, t0, seed) in self.ripples:          # the bloom where it landed
            age = now - t0
            if age >= self.BLOOM_T:
                continue
            fade = (1.0 - age / self.BLOOM_T) ** 2
            R = self.BLOOM_R * (0.6 + 0.8 * age / self.BLOOM_T)
            for r in range(max(0, int((ry - R - 7) // ch)), min(nrows - 1, int((ry + R - 7) // ch) + 1) + 1):
                row = self._rows[r]
                dy = r * ch + 7 - ry
                if abs(dy) >= R:
                    continue
                span = sqrt(R * R - dy * dy)
                shift = 1 + (r % 2) * (cw / 2)
                for c in range(max(0, int((rx - span - shift) // cw)),
                               min(len(row) - 1, int((rx + span - shift) // cw) + 1) + 1):
                    g = row[c]
                    if g is None:
                        continue
                    f = 1.0 - sqrt((g[0] - rx) ** 2 + dy * dy) / R
                    if f <= 0 or (g[4] % 100) / 100.0 > f * 1.6:     # a soft, grainy edge
                        continue
                    key = r * 4096 + c
                    e = hit.get(key)
                    if e is None:
                        e = hit[key] = [0.0, 0.0, 0.0, age, 0]
                    e[0] += fade * f * 1.2
                    e[4] = 0
        lens = self._lens_strength(now)
        if lens > 0 and self.pointer is not None:
            px, py, _t = self.pointer
            if self._lens_shape is None or now - self._lens_shape[0] > 0.05:
                self._lens_shape = (now, self._front(4243, now * 0.25))  # slowly breathing outline
            lshape = self._lens_shape[1]
            R = self.LENS_R * max(lshape)
            row_lo = max(0, int((py - R - 7) // ch))
            row_hi = min(nrows - 1, int((py + R - 7) // ch) + 1)
            breathe = 0.85 + 0.15 * math.sin(now * 2.4)
            for r in range(row_lo, row_hi + 1):
                row = self._rows[r]
                y = r * ch + 7
                dy = y - py
                if abs(dy) > R:
                    continue
                span = math.sqrt(R * R - dy * dy)
                shift = 1 + (r % 2) * (cw / 2)
                c0 = max(0, int((px - span - shift) // cw))
                c1 = min(len(row) - 1, int((px + span - shift) // cw) + 1)
                for c in range(c0, c1 + 1):
                    g = row[c]
                    if g is None:
                        continue
                    dx = g[0] - px
                    d = math.sqrt(dx * dx + dy * dy)
                    edge = self.LENS_R * lshape[int((math.atan2(dy, dx) + math.pi) * a_k) % A]
                    if d >= edge:
                        continue
                    f = 1.0 - d / edge
                    # the edge dissolves glyph by glyph instead of stopping at a line
                    if (g[4] % 97) / 97.0 > f * 2.2:
                        continue
                    k = lens * 0.5 * f * breathe
                    if k < 0.03:
                        continue
                    key = r * 4096 + c
                    e = hit.get(key)
                    if e is None:
                        e = hit[key] = [0.0, 0.0, 0.0, now - self._lens_since, 2]
                    e[0] += k
                    if d > 0.5:                         # a lens: bulge outward, gently
                        b = lens * 3.2 * f * (1 - f)
                        e[1] += dx / d * b
                        e[2] += dy / d * b
        return hit

    def _draw(self, _area, cr, w, h):
        if not self.ripples and self.pointer is None:
            return                                      # invisible at rest
        try:
            import cairo  # noqa: F401  (drawing needs pycairo; without it, skip)
        except Exception:
            return
        self._ensure(w, h)
        t_start = time.perf_counter()
        try:
            self._paint(cr, w, h)
        finally:
            # keep the frame inside budget on slower machines: when frames run
            # long, the decoding wake thins out (the crest always stays whole)
            ms = (time.perf_counter() - t_start) * 1000
            self._frame_ms = 0.7 * self._frame_ms + 0.3 * ms
            if self._frame_ms > 11 and self._thin < 3:
                self._thin += 1
                self._frame_ms = 8.0
            elif self._frame_ms < 5 and self._thin > 0:
                self._thin -= 1

    def _paint(self, cr, w, h):
        import cairo
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
        buckets = [[[] for _ in range(L)] for _ in self.BANDS]
        pos = {}
        rows = self._rows
        for key, (inten, dx, dy, age, band) in hit.items():
            x, y, ci, bright, seed = rows[key >> 12][key & 4095]
            inten = min(1.0, inten * 1.6)
            v = inten * (0.35 + 0.65 * bright)
            if v < 0.05:
                continue
            lvl = min(L - 1, int(v * L))
            if inten > 0.4 or band == 2:                # decoding: characters flip
                ci = (seed + int(age * (26 if band < 2 else 9))) % n
            gx, gy = x + dx, y + dy
            if v >= 0.42:
                pos[key] = (gx + 1.5, gy - 2, v)
            buckets[band][lvl].append((gids[ci], gx, gy))
        # threads: the woven mesh, only where the fabric flexes hardest
        lines = [[], []]
        budget = 700 // (1 + self._thin)
        for key, (x1, y1, v1) in pos.items():
            r = key >> 12
            for nb in (key + 1, key + 4096 + (0 if r % 2 else -1)):
                q = pos.get(nb)
                if q is None:
                    continue
                lines[1 if min(v1, q[2]) > 0.7 else 0].append((x1, y1, q[0], q[1]))
                budget -= 1
            if budget <= 0:
                break
        cr.set_line_width(0.6)
        for i, segs in enumerate(lines):
            if not segs:
                continue
            for x1, y1, x2, y2 in segs:
                cr.move_to(x1, y1)
                cr.line_to(x2, y2)
            cr.set_source_rgba(0.62, 0.52, 1.0, 0.10 + 0.12 * i)
            cr.stroke()
        for band, levels in enumerate(buckets):
            br, bg, bb = self.BANDS[band]
            for lvl, glyphs in enumerate(levels):
                if not glyphs:
                    continue
                t = (lvl + 0.5) / L
                # dim glyphs lean neutral; bright ones take the band's colour
                mix = 0.35 + 0.65 * t
                cr.set_source_rgba(0.72 + (br - 0.72) * mix, 0.72 + (bg - 0.72) * mix,
                                   0.80 + (bb - 0.80) * mix, min(0.95, 0.14 + t * 0.85))
                cr.show_glyphs(glyphs)


_FILE_FENCE = re.compile(r"^[ \t]*(`{3,4})(write|edit|patch)(?![A-Za-z])[ \t]*(.*)$")
_DOC_EXT = re.compile(r"\.(md|markdown|mdx|rst|txt|adoc|html?|ipynb|org)$", re.IGNORECASE)
_SR_OPEN = re.compile(r"^<{5,9} ?SEARCH\s*$")
_SR_CLOSE = re.compile(r"^>{5,9} ?REPLACE\s*$")
# a fence that only SHOWS something: four or more backticks, tildes, or a
# document/console language. What's inside is an example, never a tool to run.
_SHOW_FENCE = re.compile(r"^[ \t]*(?:(`{4,}|~{3,})(?!(?:write|edit|patch)\b)\S*|(```)(?:markdown|md|mdx|"
                         r"rst|text|txt|console|output|example|log)\b)[ \t]*$", re.IGNORECASE)


def display_spans(text, skip=()):
    """[(start, end)] of fences that only display content (a README example
    in ````markdown, console output in ```text). Code blocks inside them are
    part of the example: they must never be picked up and run. An unclosed
    one runs to the end of the reply."""
    lines = text.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append(pos)
        pos += len(ln) + 1
    out, i = [], 0
    while i < len(lines):
        m = _SHOW_FENCE.match(lines[i])
        if not m or any(a <= offs[i] < b for a, b in skip):   # inside a write/edit block
            i += 1
            continue
        fence = m.group(1) or m.group(2)
        k, depth = i + 1, 0
        while k < len(lines):
            st = lines[k].strip()
            if len(fence) > 3 or fence[0] == "~":
                if st and set(st) == {fence[0]} and len(st) >= len(fence):
                    break
            elif st == "```":
                if depth == 0:
                    break
                depth -= 1
            elif st.startswith("```"):
                depth += 1
            k += 1
        end = len(text) if k >= len(lines) else offs[k] + len(lines[k])
        out.append((offs[i], end))
        i = k + 1
    return out


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
        in_sr = False                     # inside an edit's SEARCH/REPLACE: text, not fences
        while k < len(lines):
            st = lines[k].strip()
            if tag == "edit":
                if _SR_OPEN.match(st):
                    in_sr = True
                    k += 1
                    continue
                if _SR_CLOSE.match(st):
                    in_sr = False
                    k += 1
                    continue
                if in_sr:
                    k += 1
                    continue
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


class _RawText(str):
    """A non-UTF-8 file's content: shown as text, restored as its exact bytes.
    (None would mean "the file didn't exist", and undo would DELETE it.)"""

    def __new__(cls, text, raw):
        obj = super().__new__(cls, text)
        obj.raw = raw
        return obj


def _msg_key(m):
    """A short fingerprint of a message, to tell whether history still holds it."""
    import hashlib
    c = m.get("content")
    return hashlib.sha1((str(m.get("role")) + "\0" + (c if isinstance(c, str) else repr(c)))
                        .encode("utf-8", "replace")).hexdigest()[:16]


def _merge_spans(spans):
    """Overlapping or nested spans as one: a write block inside an example."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
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
        self._paint_pending = False
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
        self._watch = None              # /watch: AI! comments from your editor
        self.plan_mode = False          # investigate read-only, propose, wait for approval
        self._carry_plan = None         # an approved plan card the next turn ticks off
        self._quiet_send = False
        self._edit_at = None            # editing the user message at this history index
        self._hook_runs = 0
        self._hook_passed_turn = None
        self._acted_since_nudge = False
        self._jobs = {}                 # background jobs: name -> {proc, log, cmd, card}
        self._py = None                 # the ```py``` session, started on first use
        # the agent kernel (docs/OS.md): this chat is PID 1; every sub-agent
        # is a scheduled, metered process under it
        self.kernel = (_kernel.Kernel(slots=self.cfg("agent_slots", 4, 1, 16))
                       if _kernel is not None else None)
        self._pid1 = self.kernel.spawn("chuck", kind="chat").pid if self.kernel else None
        self._bg = {}                   # pid -> a background agent and its workspace
        # daemons (docs/OS.md, phase 19): their triggers are checked every
        # 30 s by plain code, for free; an agent wakes only when one fires
        self._daemons = _daemons.Registry(self.DAEMONS_FILE) if _daemons is not None else None
        self._inbox = _daemons.Inbox(self.INBOX_FILE) if _daemons is not None else None
        self._daemon_live = {}          # daemon id -> pid of its running wake
        self._daemon_checking = False
        self._daemon_probe = self._Probe()
        self._pending_daemon = None
        self._asks = {}                 # pid -> (event, answer box, question): agents asking you
        # what the whole OS may spend (docs/OS.md, phase 24): every call's
        # cost, by day, on disk, across chats and restarts
        self._ledger = _budget.Ledger(DATA_DIR / "spend.json") if _budget is not None else None
        self._missions = {}             # id -> mission (docs/OS.md, phase 23)
        self._mstore = _missions.Store(self.MISSIONS_DIR) if _missions is not None else None
        # hibernate (docs/OS.md, phase 21): every step of a background agent
        # or program is checkpointed; what was cut off is offered at start
        self._hib = _hibernate.Store(self.PROCS_DIR) if _hibernate is not None else None
        # what was on disk at startup: only those runs belong to an earlier
        # session (an agent started in the first seconds is not "interrupted")
        self._hib_boot = self._hib.load() if self._hib is not None else []
        if self._hib is not None:
            if _checkpoint is not None:
                threading.Thread(target=_checkpoint.prune, args=(self.PROC_SHADOWS, 14),
                                 daemon=True, name="srv-shadow-prune").start()
            GLib.timeout_add_seconds(2, lambda: (self.offer_hibernated(), False)[1])
        if self._daemons is not None:
            GLib.timeout_add_seconds(30, self._daemon_tick)
            GLib.timeout_add_seconds(4, self._inbox_greeting)
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
        self._checkpoints = []          # folder snapshots for /rewind (see _checkpoint)
        self._cp_off = set()            # folders too big (or not allowed) to snapshot
        self._compacting = False
        self._session = self._blank_usage()
        self._balance = ""
        self.workspace = None
        _WORKDIR["path"] = None
        self.agent_mode = bool(self.settings.get("agent_mode", True))
        self.chat_id = self._new_chat_id()
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
        self._apply_access()                    # this window's folder policy, not a stale one
        self._interrupted = self._find_interrupted()
        voice.ON_PIPER_FAIL[0] = lambda why: GLib.idle_add(self._piper_note, why)
        voice.ON_VOICE_FAIL[0] = lambda eng, why: GLib.idle_add(self._voice_fail_note, eng, why)
        self._listening = False
        if self.settings.get("tts", True):
            voice.warm_up(self.settings)          # the natural voice loads in the background
        if _listen is not None and self.settings.get("mic_used"):
            _listen.warm_up()
        self._show_empty_hint()
        GLib.timeout_add_seconds(20, self._update_check)   # once a day, after startup settles
        if getattr(_config, "LOAD_PROBLEM", [None])[0]:
            self._sys_note("\u26a0 " + _config.LOAD_PROBLEM[0], "danger")
            _config.LOAD_PROBLEM[0] = None
        threading.Thread(target=self._prune_worktrees, daemon=True, name="srv-wt-prune").start()
        if _checkpoint is not None:             # old folders' snapshots, in the background
            threading.Thread(target=lambda: _checkpoint.prune(self.CHECKPOINT_STORE), daemon=True,
                             name="srv-cp-prune").start()

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
        # the system tray: what the agent OS is doing, at a glance (hidden
        # while nothing is); a question waiting for you lights it up
        self._tray_lbl = Gtk.Label(label="")
        self.tray_btn = Gtk.Button()
        self.tray_btn.add_css_class("tray")
        self.tray_btn.set_child(self._tray_lbl)
        self.tray_btn.set_valign(Gtk.Align.CENTER)
        self.tray_btn.set_visible(False)
        self.tray_btn.connect("clicked", lambda *_a: self.show_activity())
        header.pack_end(self.tray_btn)
        GLib.timeout_add(1000, self._tray_tick)

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
        mic = Gtk.Button(icon_name=_pick_icon("audio-input-microphone-symbolic",
                                              "microphone-sensitivity-high-symbolic",
                                              "audio-input-microphone"))
        mic.add_css_class("icon-btn"); mic.set_valign(Gtk.Align.CENTER)
        mic.set_tooltip_text("Talk to Chuck  (Ctrl+M) \u2014 heard on this machine, never uploaded")
        mic.connect("clicked", lambda *_a: self.toggle_listen())
        self.mic_btn = mic
        bar.append(att); bar.append(cam); bar.append(mic)
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
        acc = self._folder_access() if self.agent_mode and "settings" in self.__dict__ else "full"
        if acc != "full":
            text += {"sandbox": "\n\nFOLDER ACCESS: sandboxed, offline. Only the working folder "
                                "is writable; no network, no sudo. Work within that.",
                     "sandbox-net": "\n\nFOLDER ACCESS: sandboxed. Only the working folder is "
                                    "writable; no sudo. Work within that.",
                     "readonly": "\n\nFOLDER ACCESS: read-only. Nothing may be run or changed; "
                                 "investigate and answer."}[acc]
        if self.agent_mode and _programs is not None and "settings" in self.__dict__:
            names = sorted(self.programs())
            n_sub = self.cfg("subagents", 4, 1, 8)
            text += ("\n\nAGENT OS: you run it. The user just says what they want; never ask them to "
                     "type a command, choose for them. One thing to do or answer: do it yourself. "
                     f"Several independent things to find out: one ```task``` each, all at once (up to "
                     f"{n_sub}). A self-contained job that can run while you talk: ```spawn``` (tested "
                     "on its copy; ```wait <pid>``` if you need its result). A goal with several "
                     "parts or files: ```mission <goal>``` (planned, ordered, run in parallel, every "
                     "merge tested; starts by itself if cheap). Review, audit, tests, research: "
                     "```spawn @<program> <focus>``` (" + ", ".join(names) + "). Later or at a time: "
                     "```remind <when>: <what>```. Recurring checks or watching: ```daemon <when>: "
                     "<what>```. Everything else (agents, missions, budget, governor, inbox, watching "
                     "the editor): ```chuck <command>```, e.g. ```chuck top```, ```chuck kill 4```, "
                     "```chuck budget```. ```send <pid> <msg>``` steers a running agent; ```post "
                     "<key>: <fact>``` / ```board``` share facts between agents.")
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
            ph.set_label("Tell Chuck what you want\u2026" + (
                f"  (in {self.workspace.name})" if self.workspace else "")
                + "   \u00b7  Ctrl+P everything  \u00b7  @ files")
        else:
            ph.set_label("Ask anything\u2026   \u00b7  / commands")
        try:
            b = self.entry.get_buffer()
            ph.set_visible(b.get_char_count() == 0)
        except Exception:
            pass

    def _update_subtitle(self):
        model = (self.settings.get("model") or DEFAULT_MODEL).split("/")[-1]
        gov = _governor.mode_of(self.settings) if _governor is not None else "performance"
        parts = [model + ("" if gov == "performance" else f" ({gov})"),
                 "Agent" if self.agent_mode else "Chat"]
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
        wt = getattr(self, "_watch", None)
        if wt is not None and str(p) != wt["root"]:
            self._watch = None                       # /watch watched the old folder
            self._sys_note("\U0001F441 stopped watching: the folder changed (/watch to watch this one).", "dim")
        _WORKDIR["path"] = str(p) if p else None
        _SHELL["cwd"] = None                    # a new folder: the shell starts there
        self._refresh_system()                  # that folder's CHUCK.md / AGENTS.md
        if p:
            rec = [r for r in (self.settings.get("recent_workspaces") or [])
                   if isinstance(r, str) and r != str(p)]
            self.settings["recent_workspaces"] = [str(p)] + rec[:7]
            save_settings(self.settings)
        self._apply_access()
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

    _ISSUED_IDS = set()                         # ids handed out in this process (any window)

    @classmethod
    def _new_chat_id(cls):
        """A timestamp id no other chat has: two chats started in the same
        second used to share one file, and the later save overwrote the first."""
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        cid, n = base, 1
        while cid in cls._ISSUED_IDS or (CHATS_DIR / f"{cid}.json").exists():
            n += 1
            cid = f"{base}-{n}"
        cls._ISSUED_IDS.add(cid)
        return cid

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
        self._checkpoints = []
        self.chat_id = self._new_chat_id()
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

        hit = getattr(self, "_interrupted", None)
        if hit:
            rr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            rr.add_css_class("ws-current")
            rr.set_margin_top(22); rr.set_halign(Gtk.Align.CENTER)
            task = " ".join(hit[1].split())
            short = task if len(task) <= 34 else task[:33].rstrip() + "\u2026"
            rl = Gtk.Label(label=f"Chuck closed mid-task: \u201c{short}\u201d", xalign=0)
            rl.add_css_class("dlg-sub")
            rl.set_tooltip_text(task)
            # wraps instead of setting a minimum width: in a narrow window the
            # banner used to push the whole welcome screen off the edge
            rl.set_wrap(True); rl.set_max_width_chars(72); rl.set_hexpand(True)
            _wrap_anywhere(rl)
            rb = Gtk.Button(label="Resume"); rb.add_css_class("accent-btn")
            rb.connect("clicked", lambda *_a: self.resume_interrupted())
            db = Gtk.Button(label="Dismiss"); db.add_css_class("ghost")
            db.connect("clicked", lambda *_a: self.dismiss_interrupted())
            for b_ in (rb, db):
                b_.set_valign(Gtk.Align.CENTER)
            rr.append(rl); rr.append(rb); rr.append(db)
            box.append(rr)

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
        if self.agent_mode:
            # the agent OS, one click each: what a single chat can't do
            osl = Gtk.Label(label="OR LET IT RUN ON ITS OWN", xalign=0.5)
            osl.add_css_class("empty-keys"); osl.set_margin_top(22)
            box.append(osl)
            row = Gtk.FlowBox()
            row.set_halign(Gtk.Align.CENTER); row.set_margin_top(8)
            try:
                row.set_selection_mode(Gtk.SelectionMode.NONE)
                # a centred FlowBox settles at its narrowest natural width
                # (one per line); asking for three per line spreads them, and
                # they still wrap in a narrow window
                row.set_max_children_per_line(6); row.set_min_children_per_line(3)
                row.set_column_spacing(6); row.set_row_spacing(6)
            except Exception:
                pass
            for label, tip, fn in (
                    ("Mission", "a big goal, planned as tasks run by agents, each tested before merge",
                     lambda: self._prefill("/mission ")),
                    ("Background agent", "a whole job done in a copy of the folder while you go on",
                     lambda: self._prefill("/spawn ")),
                    ("Watch my editor", "AI! comments you save in any editor become tasks",
                     lambda: self.watch_command("on")),
                    ("Daemon", "an agent that wakes on a schedule or an event, for free while it sleeps",
                     lambda: self._prefill("/daemon ")),
                    ("Remind me", "in 20m, at 5pm, every day at 8:30",
                     lambda: self._prefill("/remind ")),
                    ("Everything\u2026  Ctrl+P", "the command palette", self.open_palette)):
                b = Gtk.Button(label=label)
                b.add_css_class("quick"); b.set_tooltip_text(tip)
                b.connect("clicked", lambda _b, f=fn: f())
                row.append(b)
            box.append(row)
        keys = Gtk.Label(label=("Tab  switch Agent / Chat     Ctrl+P  everything     Ctrl+O  folder     "
                                "/  commands     @  files" if self.agent_mode else
                                "Tab  switch Agent / Chat     Ctrl+P  everything     /  commands     "
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
        if not getattr(self, "_batching", False):  # a rebuild windows once, at the end
            self._collapse_window(scroll=True)
        return entry

    def _log_pin(self, widget):
        """Record an interactive widget that must never be torn down. It is
        machine work, so it goes in the work lane."""
        self._drop_hint()
        entry = {"kind": "pinned", "data": None, "w": _lane(widget), "pinned": True}
        self._log.append(entry)
        if not getattr(self, "_batching", False):
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

    def _save_chat(self, running=None, light=False):
        """Write this chat to disk. `running` marks a task in progress, so a
        crash mid-task can be offered for resume at the next start."""
        if running is None:
            running = bool(self._running)
        self._last_autosave = time.time()
        msgs = [m for m in self.history if m["role"] != "system" and isinstance(m.get("content"), str)]
        if not msgs:
            return
        title = next((m["content"][:60] for m in msgs if m["role"] == "user"), "chat")
        try:
            # atomic: a crash mid-write must not corrupt the chat it's saving
            path = CHATS_DIR / f"{self.chat_id}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"title": title, "ts": self.chat_id, "history": self.history,
                 "workspace": str(self.workspace) if self.workspace else "",
                 "usage": self._usage, "compact": self._compact,
                 "checkpoints": self._checkpoints[-200:],
                 "running": running, "pid": os.getpid()}, indent=None if light else 2))
            os.replace(tmp, path)
            self._disk_running = (self.chat_id, running)   # what the file on disk says now
        except Exception:
            pass
        if not light:
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
        current = Path(path).stem == str(self.chat_id)
        if current and self._running:
            # stop FIRST: stopping saves the chat, which would bring the
            # deleted file straight back
            self.stop_run(auto=True)
        try:
            Path(path).unlink()
        except OSError:
            pass
        if current:
            # the open chat is gone: start fresh without re-saving it
            _SHELL["cwd"] = None
            self.chat_id = self._new_chat_id()
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
        cps = meta.get("checkpoints")
        self._checkpoints = [c for c in cps if isinstance(c, dict) and isinstance(c.get("sha"), str)
                             and isinstance(c.get("root"), str) and isinstance(c.get("hist"), int)
                             ] if isinstance(cps, list) else []
        c = meta.get("compact")
        self._compact = (c if isinstance(c, dict) and isinstance(c.get("upto"), int)
                         and isinstance(c.get("text"), str) and 1 < c["upto"] <= len(self.history)
                         else None)
        ws = meta.get("workspace") or ""
        _SHELL["cwd"] = None                    # a `cd` in another chat doesn't carry over
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
        self._batching = True                    # build the log, then window it once
        try:
            self._rebuild_entries()
        finally:
            self._batching = False
        if not self._log:
            self._show_empty_hint()
        self._collapse_window(scroll=True)
        self._stick = True
        self._scroll_down(force=True)

    _STEER_HEAD = "[MESSAGE FROM THE USER, sent while you were working"

    def _rebuild_entries(self):
        did = ""                                 # what the last reply asked the machine to do
        spoke = False                            # Chuck has a byline in this turn already
        hist = self.history
        for idx, m in enumerate(hist):
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
                    # a message you typed mid-task rode along with the tool
                    # results: it's yours, so it shows as your bubble again
                    steer = ""
                    at = c.find(self._STEER_HEAD)
                    if at >= 0:
                        steer = c[at:].split("]:\n", 1)[-1].strip()
                        c = c[:at].rstrip()
                    body = c.split("\n\n", 1)[1] if c.startswith("TOOL RESULTS") and "\n\n" in c else c
                    if body.strip():
                        self._log_add("fold", {"text": clip_output(body.strip(), 12000),
                                               "title": did or "Tool results"})
                    if steer:
                        self._user_bubble(steer)
                        spoke = False
                    did = ""
            else:
                raw = m.get("content") if isinstance(m.get("content"), str) else ""
                # was it acting? Only then were its code blocks tools (hidden);
                # a Chat-mode reply's code is the answer, whatever mode is on now
                nxt = hist[idx + 1] if idx + 1 < len(hist) else None
                acted = bool(nxt and nxt.get("role") == "user" and not self._is_real_user(nxt)
                             and str(nxt.get("content", "")).startswith(("TOOL RESULTS",
                                                                         self._STEER_HEAD)))
                shown = self._display_text(raw, agent=self.agent_mode and acted)
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
        self._edit_msg = self.history[i]        # so a switch to another chat can't misapply it
        self.entry.get_buffer().set_text(self._typed_text(self.history[i].get("content")))
        self._toast("Editing your last message \u2014 Enter resends from there, Esc cancels")
        return True

    def _on_close(self, *_):
        self._save_chat(running=False)          # closing mid-task is a choice, not a crash
        self._py_close()
        try:                                    # the mic, the ears and the voice go too
            stop_speaking()                     # first: then Kokoro's exit isn't a "failure"
            if _listen is not None:
                _listen.EARS.cancel()
                _listen.EARS.stop()
            voice.KOKORO.stop()
        except Exception:
            pass
        if getattr(self, "_lsp_m", None) is not None:
            threading.Thread(target=self._lsp_m.stop_all, daemon=True).start()
        # background jobs belong to this session: don't leave a dev server
        # holding a port after the window that started it is gone; nor a
        # build that was running in the foreground (it has its own session)
        try:
            self._stop_all_jobs()
            kill_live_commands()
        except Exception:
            pass
        self._closing = True                    # running agents stay hibernated for next time
        if getattr(self, "_ledger", None) is not None:
            self._ledger.save()
        if self.kernel is not None:             # every agent process ends with the window
            for r in self.kernel.ps():
                if r["pid"] != self._pid1:
                    self.kernel.signal(r["pid"], _kernel.KILL)
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
            if k == getattr(Gdk, "KEY_m", -3):
                self.toggle_listen(); return True
            if k in (getattr(Gdk, "KEY_p", -4), getattr(Gdk, "KEY_k", -5)):
                self.open_palette(); return True
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
        was = self._running
        self._running = False
        self._thinking = False
        if was:
            self._save_chat(running=False)
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
        if self.kernel is not None:             # Stop ends this turn's sub-agents too
            for r in self.kernel.ps():
                if r["ppid"] == self._pid1 and r["kind"] == "subagent":
                    self.kernel.signal(r["pid"], _kernel.KILL)
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

    def _tool_tags(self, agent=None):
        tags = self._WEB_TAGS + "|" + "|".join(_AGENT_TAGS + tuple(_TOOL_REG))
        return tags + "|" + self._CODE_TAGS if (self.agent_mode if agent is None else agent) else tags

    def _display_text(self, text, partial=False, agent=None):
        """A reply with its tool blocks removed: what the user reads. partial=
        True also drops a tool block that is still open (mid-stream, or cut off
        by the output cap) so half a command never flashes up as prose."""
        if not text:
            return ""
        if "```" not in text:
            return text.strip()
        agent = self.agent_mode if agent is None else agent
        tags = self._tool_tags(agent)
        if agent:
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
        self._paint_pending = False
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

    def _account(self, sent_chars, recv_chars, vision=False, usage=None, model=None, pid=None,
                 charged=False):
        """Book one model call into this chat's and this session's totals, and
        into the kernel: to the process that made it and its ancestors."""
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
        if getattr(self, "_ledger", None) is not None:
            # a model with no known price still counts against the limits: at
            # the most expensive suggested model's price (set its real price in
            # Settings to be exact), so an unknown model can't bypass a budget
            self._ledger.add(c if c is not None else _pricing.cost(u, (1.40, None, 4.40)) or 0.0)
        if self.kernel is not None and not charged:
            self.kernel.charge(pid or self._pid1, int(u.get("prompt") or 0),
                               int(u.get("completion") or 0), c or 0.0)
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
        original = cmd
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
        # judged as written AND as rewritten: the worse of the two counts
        tier = max(classify_command(judged), classify_command(original),
                   key=lambda t: ("", "danger", "critical").index(t))
        if gate_text and tier != "critical" and re.match(r"(?:\S*/)?(?:python3?|node)\b", cmd):
            tier = classify_code("python", gate_text) or tier   # a python/node skill's body

        card, head, cbody, foot = self._card_shell("COMMAND", lambda: cmd)
        prompt = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dollar = Gtk.Label(label="$", xalign=0, valign=Gtk.Align.START)
        dollar.add_css_class("cmd-prompt")
        ct = Gtk.Label(label=cmd, xalign=0, wrap=True, selectable=True, hexpand=True)
        ct.add_css_class("cmd-text"); _wrap_anywhere(ct)
        prompt.append(dollar); prompt.append(ct)
        cbody.append(prompt)
        if gate_text:
            # a skill runs as `bash /path/x.sh`: show what is IN it, so an
            # approval is of the real thing
            lines_ = gate_text.splitlines()
            shown = "\n".join(lines_[:40]) + (f"\n\u2026 {len(lines_) - 40} more lines"
                                              if len(lines_) > 40 else "")
            sk = Gtk.Label(label=shown, xalign=0, wrap=True, selectable=True)
            sk.add_css_class("cmd-text"); sk.add_css_class("dim"); _wrap_anywhere(sk)
            cbody.append(sk)
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
        original = body
        if lang in ("bash", "sh"):
            fixed = enforce_syu(body)
            if fixed != body:
                self._sys_note("using -Syu (a plain -S risks a partial upgrade)", "dim")
            body = add_noconfirm(fixed)
        # shell is judged as shell; a program by the commands in its strings
        # and the library calls that delete the same things (rmtree(home))
        tier = (max(classify_command(body), classify_command(original),
                    key=lambda t: ("", "danger", "critical").index(t))
                if lang in ("bash", "sh") else classify_code(lang, body))

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
                cwd=_cwd(), plots_dir=str(PLOTS_DIR), env=dict(os.environ, **_RUN_ENV),
                python=shutil.which("python3") or sys.executable, preexec=_sandbox_preexec,
                wrap=lambda a, e=None: _sandboxed(a, e, persistent=True))
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

    def _beat(self, every=15):
        """While a long tool runs (a test suite, tsc, cargo), tell the stuck-
        watchdog it's alive. -> an Event; set it when the work is done."""
        done = threading.Event()

        def pulse():
            while not done.wait(every):
                GLib.idle_add(lambda: (self._note_progress(), False)[1])
        threading.Thread(target=pulse, daemon=True, name="srv-beat").start()
        return done

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
            beat = self._beat()
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
                beat.set()
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
            # one video, a sane size and length of name: a playlist or channel
            # URL would otherwise download every entry, unasked
            cmd = ["yt-dlp", "--no-playlist", "--max-filesize", "2G",
                   "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--trim-filenames", "150",
                   "-o", str(DL_DIR / "%(title)s.%(ext)s")]
            proxy = (self.settings.get("proxy") or "").strip()
            if proxy:
                cmd += ["--proxy", proxy]
            cmd += ["--", url]
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
        # saving is not running (it used to start at once, unchecked): it runs
        # when asked, through the same gate as any command
        self._tool_feedback.append(f"[skill {name}: {msg}" + (f" \u2014 ```runskill {name}``` runs it]"
                                                             if ok else "]"))

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
        if self.agent_mode and self._folder_access() != "full" and _secret_path(full):
            self._tool_done(f"[read refused: {spec} is in a credentials folder, which this "
                            "folder's access (/access) keeps hidden]")
            return
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
        # the same credential guard as the main agent's read/grep
        hide = self.agent_mode and self._folder_access() != "full"

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
            if hide and _secret_path(full):
                return ("refused: that's in a credentials folder, which this folder's access "
                        "(/access) keeps hidden")
            if os.path.isdir(full):
                return read_file_safe(full)[1]
            ok, txt, foot = _builder.read_lines(full, start, end, max_chars=24000, label=spec)
            return (txt + ("\n" + foot if foot else "")) if ok else txt

        def grep(body):
            r, sub, pat = self._grep_args(body)
            if r is None:
                return "no working folder"
            if hide and _secret_path(r / sub if sub else r):
                return "refused: that's a credentials folder (/access keeps it hidden)"
            out = _builder.grep(r, pat, sub)[1]
            if hide:
                out = "\n".join(ln for ln in out.split("\n")
                                if not _secret_path(Path(r, ln.split(":", 1)[0])))
            return out

        def glob(pat):
            return _builder.glob_files(root, pat)[1] if root else "no working folder"

        def map_(_arg):
            return _builder.repo_map(root, 12000)[1] if root else "no working folder"
        return {"search": search, "fetch": fetch, "read": read, "grep": grep,
                "glob": glob, "map": map_}

    def _sub_call(self, pid=None, independent=False, role="sub", pin=None):
        """A blocking model call for a sub-agent. Each gets its own Backend so
        concurrent calls never overwrite each other's usage record. With a
        kernel pid, each call waits for a scheduler slot (and may be told to
        stop) and is charged to that process."""
        gen = self._gen
        kern = self.kernel if pid is not None else None

        def call(messages):
            if self._over_cap():
                # sub-agents spend real money too: eight of them must not run
                # on past the cap while the main loop waits to ask
                raise RuntimeError("the chat's spending cap is reached")
            over = self._over_budget()
            if over:
                raise RuntimeError(f"the {'daily' if over[0] == 'day' else 'monthly'} budget "
                                   f"({_pricing.fmt_usd(over[1])}) is spent \u2014 /budget")
            if kern is not None:
                kern.acquire(pid)               # raises ProcessKilled when it may not go on
                try:
                    return model_call(messages)
                finally:
                    kern.release(pid)
            return model_call(messages)

        def model_call(messages):
            b = Backend(self.settings) if isinstance(self.backend, Backend) else self.backend
            if isinstance(b, Backend):
                b.role, b.pin = role, pin
            out, err = [], []
            # a background process lives past the turn that started it: only
            # the kernel (Kill, its quota) stops it
            stale = ((lambda: kern.should_stop(pid)) if independent and kern is not None   # noqa: E731
                     else (lambda: self._cancelled or gen != self._gen))
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
            # no usage reported: an ESTIMATE of this call ({}), never the main
            # loop's last usage (which once billed a 400-char call as 120k tokens)
            u, txt = getattr(b, "last_usage", None) or {}, "".join(out)
            # the model that ANSWERED: a fallback is priced as itself
            mdl = getattr(b, "last_model", None) or (b.model() if hasattr(b, "model") else None)
            if kern is not None:
                # booked to the process NOW, on its own thread: its next
                # [kernel] line and its quota check must see this call
                uu = u or {"prompt": self._est_tokens(sent), "completion": self._est_tokens(len(txt))}
                c = _pricing.cost(uu, _pricing.price_for(
                    mdl or self.settings.get("model") or DEFAULT_MODEL, self.settings))
                kern.charge(pid, int(uu.get("prompt") or 0), int(uu.get("completion") or 0), c or 0.0)
            GLib.idle_add(lambda: (self._account(sent, len(txt), usage=u, model=mdl, pid=pid,
                                                 charged=kern is not None), False)[1])
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
        gen = self._gen
        kern = self.kernel
        quota = {"tokens": self.cfg("subagent_tokens", 250000, 5000, 5000000)}
        procs = [kern.spawn(b.strip().splitlines()[0][:60] if b.strip() else "sub-agent",
                            kind="subagent", brief=b, parent=self._pid1, quota=quota,
                            caps={"read", "net"})
                 if kern is not None else None for b in briefs]
        for row, pr in zip(rows, procs):
            if pr is not None:
                row["pid"] = pr.pid
        self._busy(True)

        def one(i, brief):
            pr = procs[i]
            pid = pr.pid if pr is not None else None
            call = self._sub_call(pid)
            ptools = self._guard(pid, tools)
            GLib.idle_add(self._sub_update, rows[i], "running", "starting")

            def ev(txt):
                self._note_progress()
                if kern is not None:
                    kern.note(pid, txt)
                GLib.idle_add(self._sub_update, rows[i], "running", txt)

            def stop():
                return (self._cancelled or gen != self._gen
                        or (kern is not None and kern.should_stop(pid)))
            try:
                rep = _agents.run_subagent(
                    brief, call, ptools, max_steps=steps, on_event=ev, context=ctx,
                    should_stop=stop,
                    # asked to finish, or already past its quota: the next
                    # call (its last) writes the report
                    wrap_up=(lambda: kern.terminating(pid) or bool(kern.over_quota(pid)))
                    if kern is not None else None)
                stopped = stop()
                # stopped part-way: what it has is a partial report, not "done"
                GLib.idle_add(self._sub_update, rows[i], "stopped" if stopped else "done", "", rep)
                if kern is not None:
                    kern.exit(pid, 0 if not stopped else 1, rep)
            except Exception as ex:
                killed = _kernel is not None and isinstance(ex, _kernel.ProcessKilled)
                rep = (f"(stopped: {ex})" if killed else f"(sub-agent failed: {ex})")
                GLib.idle_add(self._sub_update, rows[i], "stopped" if killed else "fail",
                              str(ex)[:80], rep)
                if kern is not None:
                    kern.exit(pid, 1, rep)
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
        elif state == "stopped":
            row["mark"].set_label("\u25a0"); self._set_status(row["pill"], "stopped", "wait")
            row["mark"].remove_css_class("plan-cur")
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
            argv = _sandboxed([_bash_bin(), "-c", cmd], env)
            if argv is None:
                fh.close()
                self._tool_feedback.append("[bg " + SANDBOX_NO_BWRAP[1:-1] + "]")
                return
            proc = subprocess.Popen(argv, cwd=_cwd(), stdin=subprocess.DEVNULL,
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

    def _kill_job(self, name, hard=False):
        """Stop a job and everything it started. The job runs in its own
        session, so its process group id is its pid: signal the GROUP even when
        the leader already exited (`server &` leaves the server running under
        a dead shell). The grace period waits off the UI thread."""
        j = self._jobs.get(name)
        if not j:
            return f"no job '{name}'"
        pgid = j["proc"].pid
        if j["proc"].poll() is not None and not _group_alive(pgid):
            return f"'{name}' had already exited (code {j['proc'].poll()})"
        try:
            os.killpg(pgid, signal.SIGKILL if hard else signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if not hard:
            def finish():
                end = time.time() + 3
                while time.time() < end and _group_alive(pgid):
                    j["proc"].poll()              # reap the leader: a zombie still counts
                    time.sleep(0.1)
                if _group_alive(pgid):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    j["proc"].wait(timeout=2)
                except Exception:
                    pass
                GLib.idle_add(lambda: (self._update_job_card(j), False)[1])
            threading.Thread(target=finish, daemon=True, name="job-stop").start()
        else:
            try:
                j["proc"].wait(timeout=1)
            except Exception:
                pass
        self._update_job_card(j)
        return f"stopped '{name}'"

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
            self._kill_job(name, hard=True)       # closing: no grace, nothing left behind

    _VIEWABLE = re.compile(r"\.(?:html?|png|jpe?g|gif|webp|svg|bmp|pdf|txt|md|csv|json|log|"
                           r"mp4|webm|mkv|mov|mp3|wav|ogg|flac|m4a)$", re.I)

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
            if (self.agent_mode and self._folder_access() != "full" and os.path.isfile(full)
                    and (os.access(full, os.X_OK) or not self._VIEWABLE.search(full))):
                # the desktop opens files OUTSIDE the sandbox: a .desktop file
                # or a script would run unconfined
                self._tool_feedback.append(f"[open: not opened: {full} isn't a page, picture, "
                                           "document or media file, and this folder is "
                                           "restricted (/access)]")
                return
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
        if not path.is_file():
            return path, None
        try:
            return path, _builder.read_keep(path)     # CRLF and all, so undo is exact
        except UnicodeDecodeError:
            try:                                      # not UTF-8: keep the bytes to restore
                raw = path.read_bytes()
                return path, _RawText(raw.decode("latin-1"), raw)
            except OSError:
                return path, None
        except OSError:
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
                raw = getattr(ch["before"], "raw", None)
                if raw is not None:
                    path.write_bytes(raw)
                else:
                    with open(path, "w", encoding="utf-8", newline="") as fh:
                        fh.write(ch["before"])
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
        done, bad = self._apply_file_plan(p, plan)
        self._sys_note(f"\u2713 patched {len(done)} file(s)", "ok" if not bad else "danger")
        return (f"[patch applied to {len(done)} file(s): {', '.join(done)}]"
                + ("\n[" + "\n".join(bad) + "]" if bad else ""))

    def _apply_file_plan(self, p, plan):
        """Write [(rel, before, after)] (after None = delete), each file
        checkpointed with its diff card and verified. -> (done, problems)."""
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
        return done, bad

    # ── code intelligence: def / refs / rename / find ──
    def _lsp_mgr(self):
        if getattr(self, "_lsp_m", None) is None and _lsp is not None:
            self._lsp_m = _lsp.Manager()
        return getattr(self, "_lsp_m", None)

    def _symbol_tool(self, tag, body):
        if _lsp is None:
            return None, f"[{tag}: the lsp module is missing \u2014 reinstall Chuck]"
        root = self._workroot()
        if root is None:
            return None, f"[{tag}: no working folder \u2014 pick one first]"
        args = (body or "").split()
        if not args:
            return None, f"[{tag}: give a name, e.g. ```{tag} save``` or ```{tag} Store.save```]"
        return (root, args), None

    @tool("def", cap=4, bare=True)
    def _t_def(self, body):
        """```def <name>``` (or Class.method): where it's defined, by meaning
        through a language server when one is installed, else by syntax."""
        ok, err = self._symbol_tool("def", body)
        if err:
            return err
        root, (name, *_rest) = ok
        self._sys_note(f"\U0001F50E where is {name} defined", "dim")

        def work():
            try:
                hits, how = _lsp.definitions(self._lsp_mgr(), root, name)
            except _lsp.LSPError as ex:
                return f"[def {name}: {ex}]"
            return _lsp.report_defs(name, hits, how)
        return work

    @tool("refs", cap=3, bare=True)
    def _t_refs(self, body):
        """```refs <name>```: every use of it. With a language server these are
        real uses of THAT symbol; without one, whole-word matches (it says so)."""
        ok, err = self._symbol_tool("refs", body)
        if err:
            return err
        root, (name, *_rest) = ok
        self._sys_note(f"\U0001F50E who uses {name}", "dim")

        def work():
            try:
                refs, how, defs = _lsp.references(self._lsp_mgr(), root, name)
            except _lsp.LSPError as ex:
                return f"[refs {name}: {ex}]"
            return _lsp.report_refs(name, refs, how, defs)
        return work

    @tool("rename", acts=True, cap=1, bare=True)
    def _t_rename(self, body):
        """```rename <old> <new>```: the language server renames the symbol and
        every real use of it, across files, as one checkpointed change."""
        ok, err = self._symbol_tool("rename", body)
        if err:
            return err
        root, args = ok
        if len(args) != 2:
            return "[rename: give the old and the new name: ```rename make_store build_store```]"
        old, new = args
        self._sys_note(f"\u270e renaming {old} \u2192 {new}", "dim")

        def work():
            try:
                plan, how = _lsp.rename(self._lsp_mgr(), root, old, new)
            except _lsp.LSPError as ex:
                return f"[rename NOT done, nothing was changed: {ex}]"
            if not plan:
                return f"[rename {old}: {how} found nothing to change]"
            box, ev = {}, threading.Event()

            def apply():
                try:
                    box["r"] = self._apply_file_plan(root, plan)
                except Exception as ex:                  # report, never strand the worker
                    box["r"] = ([], [str(ex)])
                ev.set()
                return False
            GLib.idle_add(apply)
            ev.wait(120)
            done, bad = box.get("r", ([], ["timed out applying the rename"]))
            n = sum(after.count(new) - before.count(new) for _r, before, after in plan)
            return (f"[renamed {old} \u2192 {new} via {how}: {n} occurrence(s) in "
                    f"{len(done)} file(s): {', '.join(done)}]"
                    + ("\n[" + "\n".join(bad) + "]" if bad else "")
                    + "\nRun the tests: a rename can still miss uses in strings or "
                      "dynamic code.")
        return work

    @tool("find", cap=2, bare=True)
    def _t_find(self, body):
        """```find <what the code does>```: functions, classes and doc sections
        ranked by how well they match the description (BM25 over names,
        docstrings, comments and identifiers)."""
        if _codesearch is None:
            return "[find: the codesearch module is missing \u2014 reinstall Chuck]"
        root = self._workroot()
        if root is None:
            return "[find: no working folder \u2014 pick one first]"
        query = " ".join((body or "").split())
        if not query:
            return "[find: say what you're looking for, e.g. ```find retry failed requests```]"
        self._sys_note(f"\U0001F50E finding \u201c{query[:60]}\u201d", "dim")

        def work():
            return _codesearch.report(root, query, _codesearch.search(root, query, k=8))
        return work

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
            # stopping the old servers can take seconds: never on the UI thread
            threading.Thread(target=self._mcp.start_all, daemon=True, name="mcp-reload").start()
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
        if _lsp is not None:
            have = {lg: _lsp.server_for(lg, self.workspace) for lg in ("python", "typescript")}
            found = [f"{lg}: {os.path.basename(a[0])}" for lg, a in have.items() if a]
            missing = [lg for lg, a in have.items() if not a]
            add("ok" if not missing else "warn", "Language servers",
                ", ".join(found) + ("; " if found and missing else "")
                + ("; ".join(f"no {lg} server \u2014 install {_lsp.INSTALL[lg]} (def/refs fall back "
                             "to syntax; rename needs one)" for lg in missing)))
        add("ok" if shutil.which("node") else "warn", "Node",
            shutil.which("node") or "none \u2014 JavaScript can't be checked or run")
        shot = next((t for t in ("grim", "spectacle", "scrot", "gnome-screenshot") if shutil.which(t)), None)
        add("ok" if shot else "warn", "Screenshots", shot or "no capture tool \u2014 install grim (Wayland) or scrot (X11)")
        pst, pdet = voice.voice_status(S)       # run_doctor then tries a real synthesis
        add(pst, "Voice", pdet)
        if _listen is not None:
            lst, ldet = _listen.status()
            add(lst, "Voice input", ldet)
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
        voice_row = mic_row = None
        for i, (st, name, detail) in enumerate(rows + [("warn", "API", "checking\u2026")]):
            ic, css = icon[st]
            il = Gtk.Label(label=ic); il.add_css_class(css)
            nl = Gtk.Label(label=name, xalign=0); nl.add_css_class("doctor-name")
            dl = Gtk.Label(label=detail, xalign=0, wrap=True, hexpand=True, selectable=True)
            dl.add_css_class("dim"); _wrap_anywhere(dl)
            grid.attach(il, 0, i, 1, 1); grid.attach(nl, 1, i, 1, 1); grid.attach(dl, 2, i, 1, 1)
            if name == "API":
                api_detail, api_icon = dl, il
            if name == "Voice" and st in ("ok", "fail"):
                voice_row = (dl, il)                 # a failed one is re-tested: it may be fine now
            if name == "Voice input" and st == "ok":
                mic_row = (dl, il)                   # installed: prove the microphone gives sound
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
        if mic_row is not None and _listen is not None:
            mdl, mil = mic_row
            mdl.set_text(mdl.get_text() + " \u2014 testing the microphone\u2026")

            def mic_done(res):
                ok, text = res
                try:
                    mdl.set_text(text)
                    mil.set_text(icon["ok" if ok else "fail"][0])
                    for c in ("plan-tick", "doctor-warn", "doctor-fail"):
                        mil.remove_css_class(c)
                    mil.add_css_class(icon["ok" if ok else "fail"][1])
                except Exception:
                    pass
                return False
            threading.Thread(target=lambda: GLib.idle_add(mic_done, _listen.mic_test()),
                             daemon=True, name="mic-test").start()
        if voice_row is not None:
            vdl, vil = voice_row
            vdl.set_text(vdl.get_text() + " \u2014 testing\u2026")

            def voice_done(res):
                ok, text = res
                try:
                    vdl.set_text(text)
                    vil.set_text(icon["ok" if ok else "fail"][0])
                    for c in ("plan-tick", "doctor-warn", "doctor-fail"):
                        vil.remove_css_class(c)
                    vil.add_css_class(icon["ok" if ok else "fail"][1])
                except Exception:
                    pass
                return False
            use_kokoro = voice.kokoro_ready() and (self.settings.get("voice_engine") or "auto") in (
                "auto", "kokoro")
            kvoice = self.settings.get("kokoro_voice")
            threading.Thread(target=lambda: GLib.idle_add(voice_done, voice.test_kokoro(
                kvoice) if use_kokoro else voice.test_piper()),
                daemon=True).start()
        return rows

    # ── background agents (Chuck OS, phase 17) ──
    WORKTREES = DATA_DIR / "worktrees"

    def _prune_worktrees(self, days=14):
        """Copies left by background agents of earlier sessions go after two
        weeks, unless a hibernated run still points at one (unmerged work or
        a run to resume is never deleted from under its record)."""
        try:
            keep = set()
            if _hibernate is not None:
                keep = {os.path.realpath(r["ws_dir"]) for r in _hibernate.Store(self.PROCS_DIR).load()
                        if r.get("ws_dir")}
            cutoff = time.time() - days * 86400
            for d in self.WORKTREES.iterdir():
                if (d.is_dir() and d.stat().st_mtime < cutoff
                        and os.path.realpath(d) not in keep):
                    self._drop_shadow_of(d)
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass

    def _drop_shadow_of(self, ws_dir):
        """The private snapshot store of a background agent's copy."""
        if _checkpoint is None or not ws_dir:
            return
        try:
            shutil.rmtree(_checkpoint.Shadow(Path(ws_dir) / "work", self.PROC_SHADOWS).git_dir,
                          ignore_errors=True)
        except Exception:
            pass

    def _worker_parse(self, reply):
        """A background agent's tool blocks, in the order it wrote them, with
        the same fence rules as the main agent (a ```bash inside a ````markdown
        example or a file being written is never a command)."""
        reply = reply or ""
        fbs = file_blocks(reply)
        fspans = [(b["start"], b["end"]) for b in fbs]
        spans = _merge_spans(fspans + display_spans(reply, fspans))
        masked = list(reply)
        for a, b in spans:
            masked[a:b] = " " * (b - a)
        masked = "".join(masked)
        calls = [(b["start"], b["tag"], (b["path"], b["body"])) for b in fbs
                 if b["tag"] in ("write", "edit")]
        for m in re.finditer(r"```(read|grep|glob|map|search|fetch|bash|sh|post|board|ask)(?![A-Za-z])"
                             r"[ \t]*\n?(.*?)```", masked, re.DOTALL):
            tag = "bash" if m.group(1) == "sh" else m.group(1)
            arg = reply[m.start(2):m.end(2)]
            if tag not in ("map", "board") and not arg.strip():
                continue
            calls.append((m.start(), tag, arg))
        return [(t, a) for _p, t, a in sorted(calls, key=lambda c: c[0])]

    def _worker_syscalls(self, work, net=True):
        """The syscall table of a background agent, every call bound to its
        own copy of the folder. Commands run jailed there; anything that
        needs the user (sudo, a CRITICAL command) is refused, not queued."""
        work = Path(work)
        web = self._sub_tools()

        def read(spec):
            spec = (spec or "").strip().strip("'\"")
            start = end = None
            m = self._RANGE_RE.match(spec)
            if m and not (work / spec).exists():
                spec, start = m.group(1).strip(), int(m.group(2))
                end = int(m.group(3)) if m.group(3) else None
            full = os.path.expanduser(spec)
            full = full if os.path.isabs(full) else str(work / full)
            if _secret_path(full):
                return "refused: that's in a credentials folder"
            if os.path.isdir(full):
                return read_file_safe(full)[1]
            ok, txt, foot = _builder.read_lines(full, start, end, max_chars=24000, label=spec)
            return (txt + ("\n" + foot if foot else "")) if ok else txt

        def grep(body):
            toks = (body or "").split()
            pat, sub = (body or "").strip(), ""
            if len(toks) > 1 and (work / toks[-1]).exists():
                pat, sub = " ".join(toks[:-1]), toks[-1]
            return _builder.grep(work, pat, sub)[1]

        def write(arg):
            rel, body = arg
            ok, msg, _p = _builder.write_file(work, rel, body)
            return msg

        def edit(arg):
            rel, body = arg
            return _builder.edit_file(work, rel, body)[1]

        def bash(cmd):
            cmd = (cmd or "").strip()
            if classify_command(cmd) == "critical":
                return ("[NOT run: this command needs the user's approval, which a background "
                        "agent can't get. Say in your report what should be run.]")
            if command_needs_sudo(cmd):
                return "[NOT run: no sudo in the background. Say in your report what needs root.]"
            est = estimate_runtime(cmd)
            rc, out = _run_in(work, enforce_syu(cmd), min(600, est["hard_timeout_seconds"]), net=net)
            return f"exit {rc}\n{clip_output(out, 12000)}"
        return {"read": read, "grep": grep,
                "glob": lambda pat: _builder.glob_files(work, (pat or "").strip())[1],
                "map": lambda _a: _builder.repo_map(work, 12000)[1],
                "search": web["search"], "fetch": web["fetch"],
                "write": write, "edit": edit, "bash": bash}

    def spawn_background(self, brief, root=None, daemon=None, why="", program=None, parent=None,
                         mission=None):
        """Start a background agent on `brief` -> its pid (or None). It works
        in a copy of the folder (`root`, else the working folder) while you
        go on; when it's done its report and diff wait for you to merge or
        discard. `daemon`: the daemon that woke it (its report goes to the
        inbox too). `program`: an act program it runs (programs.py)."""
        brief = (brief or "").strip()
        if not brief:
            self._sys_note("/spawn needs a task: /spawn add tests for the parser", "dim")
            return None
        if _cow is None or _worker is None or self.kernel is None:
            self._sys_note("Background agents need the kernel, cow and worker modules "
                           "\u2014 reinstall Chuck.", "danger")
            return None
        root = self._workroot() if root is None else Path(os.path.expanduser(str(root)))
        if root is None or not root.is_dir():
            self._sys_note("Pick a working folder first (/folder): a background agent works "
                           "on a copy of it.", "danger")
            return None
        acc = self.settings.get("folder_access") or {}
        if (acc.get(str(root), "full") if isinstance(acc, dict) else "full") == "readonly":
            self._sys_note("This folder is read-only (/access): nothing may change it, so "
                           "there's nothing to merge a background agent's work into.", "danger")
            return None
        quota = {"tokens": self.cfg("background_tokens", 1500000, 20000, 50000000),
                 "seconds": self.cfg("background_minutes", 60, 1, 1440) * 60}
        caps = {"read", "write", "exec", "net"}
        if program is not None:
            quota = {"tokens": program["tokens"], "seconds": program["minutes"] * 60}
            caps = _programs.caps_of(program)
        # an offline-sandboxed folder keeps its agents offline too
        if (acc.get(str(root), "full") if isinstance(acc, dict) else "full") == "sandbox":
            caps.discard("net")
        name = (f"{program['name']}: " if program else "") + brief.splitlines()[0][:60]
        proc = self.kernel.spawn(name[:60], kind="background", brief=brief,
                                 parent=parent or self._pid1, quota=quota, nice=2, caps=caps,
                                 program=program["name"] if program else "")
        pid = proc.pid
        entry = {"pid": pid, "brief": brief, "name": name, "ws": None, "report": None,
                 "status": "running", "root": root, "changes": {}, "daemon": daemon,
                 "daemon_why": why, "program": program["name"] if program else "",
                 "kind": "background", "mission": mission,
                 "waited": bool(mission)}          # a mission's task reports to its mission
        self._bg[pid] = entry
        if not mission:                        # a mission's tasks live in its card
            self._bg_card(entry)
        self._launch_bg(entry, program)
        if mission:
            return pid
        self._sys_note(f"\U0001F6F0 background agent {pid} started \u2014 it works on a copy of "
                       f"{root.name}; you can keep going (/top to watch it)", "dim")
        return pid

    def _launch_bg(self, entry, program=None, resume=None, ws=None):
        """Run a background agent's process: in a fresh copy of its folder,
        or (resume / fork) in the copy `ws` from the conversation `resume`.
        Every step is checkpointed (hibernate)."""
        pid, root, brief = entry["pid"], entry["root"], entry["brief"]
        entry.setdefault("key", _hibernate.new_key(pid) if _hibernate else str(pid))
        entry.setdefault("steps", [])
        call = self._sub_call(pid, independent=True, role="background", pin=self._pin_for(program))
        slug = re.sub(r"[^a-z0-9]+", "-", entry["name"].lower()).strip("-")[:30] or "task"
        kern = self.kernel

        def run():
            report, status = "", 1
            try:
                if ws is None:
                    kern.note(pid, "copying the folder")
                    # named by the run's unique key, never by pid: pids start
                    # at 1 every session, and a new pid-3 run must not
                    # overwrite an older, unmerged one's copy
                    entry["ws"] = _cow.Workspace.create(root, self.WORKTREES, f"{entry['key']}-{slug}")
                else:
                    entry["ws"] = ws
                wsp = entry["ws"]
                self._shadow_for(entry)
                if not entry["steps"]:            # where this run starts (a fork: its fork point)
                    self._on_step(entry, None, len(_hibernate.steps_of(resume)) if resume and _hibernate else 0)

                def ev(t):
                    kern.note(pid, t)
                    GLib.idle_add(self._bg_paint, entry)
                table = self._worker_syscalls(wsp.work, net=kern.can(pid, "net"))
                if program is not None:
                    table = {t: f for t, f in table.items() if t in program["tools"]}
                table.update(self._os_calls(pid, can_ask=True))
                entry["ended"] = {}
                report, _steps = _worker.run_worker(
                    brief, call, self._worker_parse, self._guard(pid, table),
                    max_steps=self.cfg("background_steps", 60, 5, 400), on_event=ev,
                    outcome=entry["ended"],
                    inbox=self._inbox_for(pid), status_line=self._status_for(pid),
                    should_stop=lambda: kern.should_stop(pid),
                    wrap_up=lambda: kern.terminating(pid) or bool(kern.over_quota(pid)),
                    context=f"{_now_line()}\nProject: {root.name}. Your private copy of it "
                            f"is your working directory ({wsp.work}).",
                    prompt=_programs.act_prompt(program, _worker.WORKER_PROMPT) if program else None,
                    resume=resume, on_step=lambda m, n: self._on_step(entry, m, n))
                status = 1 if kern.should_stop(pid) else 0
            except Exception as ex:
                killed = _kernel is not None and isinstance(ex, _kernel.ProcessKilled)
                report = (f"(stopped: {ex})" if killed else f"(failed: {ex})")
            kern.exit(pid, status, report)
            GLib.idle_add(self._bg_finished, entry, report, status)
        threading.Thread(target=run, daemon=True, name=f"bg-agent-{pid}").start()

    def _bg_card(self, entry):
        title = (f"PROGRAM {entry['program'].upper()}" if entry.get("program")
                 else "BACKGROUND AGENT")
        card, head, body, foot = self._card_shell(f"{title} \u00b7 PID {entry['pid']}",
                                                  lambda e=entry: e.get("report") or e["brief"])
        pill = self._pill("running", "run")
        head.insert_child_after(pill, head.get_first_child())
        bl = Gtk.Label(label=entry["brief"][:600], xalign=0, wrap=True)
        _wrap_anywhere(bl); body.append(bl)
        act = Gtk.Label(label="starting\u2026", xalign=0, wrap=True)
        act.add_css_class("dim"); _wrap_anywhere(act); body.append(act)
        rep = Gtk.Label(label="", xalign=0, wrap=True, selectable=True)
        _wrap_anywhere(rep); rep.set_visible(False); body.append(rep)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        foot.append(btns)
        entry["ui"] = {"pill": pill, "act": act, "rep": rep, "btns": btns}
        self._bg_buttons(entry)
        self._log_pin(card)

    def _bg_buttons(self, entry):
        btns = entry["ui"]["btns"]
        child = btns.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            btns.remove(child)
            child = nxt
        pid = entry["pid"]
        if entry["status"] == "running":
            spec = [("Stop", lambda *_a: self.process_command("kill", str(pid)))]
        elif entry["status"] == "finished":
            spec = []
            if entry["changes"]:
                spec = [("Show diff", lambda *_a: self.show_bg_diff(pid)),
                        ("Merge into folder", lambda *_a: self.join_background(pid))]
            if entry.get("kind") != "report":      # a report program has no copy to discard
                spec.append(("Discard", lambda *_a: self.discard_background(pid)))
            spec.append(("Trace", lambda *_a: self.show_strace(str(pid))))
            if entry.get("msgs"):
                spec.append(("Replay", lambda *_a: self.show_replay(str(pid))))
        else:
            spec = []
        for i, (label, fn) in enumerate(spec):
            b = Gtk.Button(label=label); b.add_css_class("quick")
            if label.startswith("Merge"):
                b.add_css_class("accent-btn")
            b.connect("clicked", fn)
            btns.append(b)

    def _bg_paint(self, entry):
        ui = entry.get("ui")
        p = self.kernel.procs.get(entry["pid"]) if self.kernel else None
        if ui and p is not None and entry["status"] == "running":
            ui["act"].set_text(f"{p.activity or 'working'} \u00b7 "
                               f"{_pricing.fmt_tokens(p.tokens())} tokens \u00b7 "
                               f"{_pricing.fmt_usd(p.total['usd'])}")
        return False

    def _bg_finished(self, entry, report, status, quiet=False):
        if getattr(self, "_closing", False):
            return False                # killed by the window closing: it stays hibernated
        entry["report"] = report
        ws = entry.get("ws")
        try:
            entry["changes"] = ws.changes() if ws is not None else {}
        except Exception:
            entry["changes"] = {}
        entry["status"] = "finished"
        ch = entry["changes"]
        ui = entry.get("ui")
        if ui:
            self._set_status(ui["pill"], "done" if status == 0 else "stopped",
                             "ok" if status == 0 else "wait")
            ui["act"].set_text(f"{len(ch)} file{'' if len(ch) == 1 else 's'} changed in its copy"
                               + (": " + ", ".join(list(ch)[:6]) + (" \u2026" if len(ch) > 6 else "")
                                  if ch else ""))
            ui["rep"].set_text(report[:20000])
            ui["rep"].set_visible(True)
            self._bg_buttons(entry)
        if ch:
            self._hib_save(entry)           # unmerged work survives a restart
        elif self._hib is not None and entry.get("key"):
            self._hib.remove(entry["key"])  # nothing to come back for (replay/fork still work now)
        if quiet:
            return False
        if entry.get("mission"):
            self._mission_task_finished(entry, report, status)
            return False
        if ch and entry.get("ws") is not None and self.settings.get("verify_background", True):
            self._bg_verify(entry)
        if entry.get("daemon"):
            dd = entry["daemon"]
            self._daemon_done(dd["id"], entry.get("daemon_why") or "its trigger fired", report
                              + (f"\n\nChanged in its copy: {', '.join(list(ch)[:20])} \u2014 "
                                 f"merge with /join {entry['pid']}." if ch else ""),
                              entry["pid"], status, bg_pid=entry["pid"] if ch else None)
        what = (f"program {entry['program']} (pid {entry['pid']})" if entry.get("program")
                else f"background agent {entry['pid']}")
        if entry.get("kind") == "report":
            head = (report or "").strip().splitlines()[0][:160] if (report or "").strip() else "no report"
            self._sys_note(f"\u25b6 {what} finished \u2014 {head}", "ok" if status == 0 else "dim")
        else:
            self._sys_note(f"\U0001F6F0 {what} finished \u2014 "
                           + (f"{len(ch)} file(s) changed; review and merge (/join {entry['pid']})"
                              if ch else "no files changed"), "ok" if status == 0 else "dim")
        # the main agent hears about it at its next step (unless it is
        # waiting for exactly this: then the report is its ```wait``` result)
        if not entry.get("waited"):
            self._user_notes.append(
                f"{what} ({entry['name']}) finished. Its report: "
                f"{report[:1500]}" + (f" Files it changed in its copy: {', '.join(list(ch)[:20])}. "
                                      f"They reach the folder only when merged (```join "
                                      f"{entry['pid']}``` or the user's Merge button)." if ch else ""))
        try:
            if not self.is_active():
                n = Gio.Notification.new(f"Background agent {entry['pid']} finished")
                n.set_body(entry["name"][:120])
                self.get_application().send_notification(f"chuck-bg-{entry['pid']}", n)
        except Exception:
            pass
        return False

    def _bg_verify(self, entry):
        """The merge gate: the project's tests run on the agent's copy as soon
        as it finishes, so the card says whether its work passes before you
        merge it."""
        ui = entry.get("ui")
        base = ui["act"].get_text() if ui else ""
        if ui:
            ui["act"].set_text(base + " \u00b7 running the tests on its copy\u2026")

        def work():
            res = self._verify_copy(entry["ws"])
            GLib.idle_add(done, res)

        def done(res):
            state, out = res
            entry["verify"] = state
            if ui:
                ui["act"].set_text(base + {"pass": " \u00b7 \u2713 the tests pass on its copy",
                                           "fail": " \u00b7 \u2717 the tests FAIL on its copy",
                                           "none": " \u00b7 no tests found to check it"}[state])
            if state == "fail":
                self._user_notes.append(f"background agent {entry['pid']}'s copy FAILS the project's "
                                        f"tests (don't merge it as it is):\n{out[-1500:]}")
            return False
        threading.Thread(target=work, daemon=True, name=f"bg-verify-{entry['pid']}").start()

    def _bg_entry(self, pid, verb):
        try:
            pid = int(str(pid).strip())
        except ValueError:
            self._sys_note(f"/{verb} needs a background agent's pid (/top lists them).", "dim")
            return None
        e = self._bg.get(pid)
        if e is None:
            self._sys_note(f"No background agent {pid}.", "danger")
        elif e["status"] == "running":
            self._sys_note(f"Background agent {pid} is still working (/kill {pid} stops it).", "dim")
            return None
        elif e["status"] in ("merged", "discarded"):
            self._sys_note(f"Background agent {pid}'s work was already {e['status']}.", "dim")
            return None
        elif e.get("mission") and not getattr(self, "_mission_merging", False):
            self._sys_note(f"pid {pid} is a mission's task: the mission merges it once its tests "
                           "pass (/missions).", "dim")
            return None
        return e

    def show_bg_diff(self, pid):
        e = self._bg_entry(pid, "diff")
        if e is None or e.get("ws") is None:
            return False
        d = e["ws"].diff()
        card, head, body, foot = self._card_shell(f"DIFF \u00b7 BACKGROUND AGENT {e['pid']}",
                                                  lambda: d)
        lb = Gtk.Label(label=clip_output(d, 60000) or "(no changes)", xalign=0, selectable=True)
        lb.add_css_class("cmd-text"); _wrap_anywhere(lb)
        body.append(lb)
        # choose what to take: a check per file, then merge only those
        ch = e.get("changes") or {}
        if ch:
            pick = Gtk.Label(label="Take these into your folder:", xalign=0)
            pick.add_css_class("dim")
            foot.append(pick)
            checks = {}
            for rel, kind in ch.items():
                cb = Gtk.CheckButton(label=f"{rel}  ({kind})")
                cb.set_active(True)
                checks[rel] = cb
                foot.append(cb)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            mb = Gtk.Button(label="Merge the checked files")
            mb.add_css_class("quick"); mb.add_css_class("accent-btn")

            def merge_checked(*_a):
                chosen = [r for r, c in checks.items() if c.get_active()]
                if not chosen:
                    self._sys_note("Nothing checked: nothing to merge (Discard drops it all).", "dim")
                    return
                mb.set_sensitive(False)
                self.join_background(e["pid"], only=chosen)
            mb.connect("clicked", merge_checked)
            row.append(mb)
            foot.append(row)
            e["_merge_checks"] = checks
        self._log_pin(card)
        return True

    def join_background(self, pid, only=None):
        """Merge a finished background agent's work into the folder: three-way,
        checkpointed first (so /rewind undoes it), conflicts set aside."""
        e = self._bg_entry(pid, "join")
        if e is None or e.get("ws") is None:
            return "nothing to merge"
        if not e["changes"]:
            return "it changed nothing"
        self._checkpoint(f"merge background agent {e['pid']}")
        try:
            r = e["ws"].merge(only=set(only) if only is not None else None)
        except Exception as ex:
            self._sys_note(f"\u2717 couldn't merge: {ex}", "danger")
            return f"merge failed: {ex}"
        e["merge_result"] = r
        if r.get("skipped"):
            # what you left out stays in its copy: Merge or Discard it later
            e["status"] = "finished"
            e["changes"] = {k: v for k, v in (e.get("changes") or {}).items() if k in r["skipped"]}
            self._hib_save(e)
        else:
            e["status"] = "merged"
            e["ws"].remove()
            self._hib_drop(e)
        parts = []
        if r["applied"] or r["merged"]:
            parts.append(f"{len(r['applied']) + len(r['merged'])} file(s) merged")
        if r["merged"]:
            parts.append(f"{len(r['merged'])} combined with your own edits")
        if r.get("skipped"):
            parts.append(f"{len(r['skipped'])} left out as you chose (still in its copy: Merge or "
                         "Discard them later)")
        if r["conflicts"]:
            parts.append(f"{len(r['conflicts'])} CONFLICT(S) not written ({', '.join(r['conflicts'][:5])}"
                         " \u2014 its versions are in .chuck-conflicts/)")
        msg = "; ".join(parts) or "nothing to change"
        self._sys_note(("\u26a0 " if r["conflicts"] else "\u2713 ") + f"background agent {e['pid']}: {msg}",
                       "danger" if r["conflicts"] else "ok")
        if e.get("ui"):
            self._set_status(e["ui"]["pill"], "merged", "ok")
            self._bg_buttons(e)
        self._dirty.update(r["applied"] + r["merged"])
        return msg

    def discard_background(self, pid):
        e = self._bg_entry(pid, "discard")
        if e is None:
            return False
        if e.get("ws") is not None:
            e["ws"].remove()
        e["status"] = "discarded"
        self._hib_drop(e)
        if e.get("ui"):
            self._set_status(e["ui"]["pill"], "discarded", "wait")
            self._bg_buttons(e)
        self._sys_note(f"Background agent {e['pid']}'s work discarded.", "dim")
        return True

    @tool("spawn", cap=3)
    def _t_spawn(self, body):
        """```spawn <brief>``` — a background agent does the whole job in its
        own copy of the folder while the conversation goes on.
        ```spawn @program <input>``` runs an agent program instead."""
        pid = (self.run_program(body) if (body or "").strip().startswith("@")
               else self.spawn_background(body))
        if pid is None:
            return "[spawn: not started (see the note above)]"
        return (f"[spawned background agent {pid}: it works in its own copy of the folder. "
                "Carry on; its report will arrive later, and its changes reach the folder only "
                f"when merged (```join {pid}```).]")

    @tool("join", acts=True, cap=2)
    def _t_join(self, body):
        """```join <pid>``` — merge a finished background agent's work."""
        return f"[join {(body or '').strip()}: {self.join_background(body)}]"

    # ── capabilities, syscalls, programs, the governor (Chuck OS, phase 20) ──
    def _guard(self, pid, table):
        """Every syscall of process `pid` goes through here: checked against
        its capabilities, timed, and logged for /strace. A refused call is a
        result the agent reads, not a crash."""
        kern = self.kernel
        if kern is None or pid is None or _kernel is None:
            return table

        def wrap(tag, fn):
            cap = _kernel.SYSCALL_CAP.get(tag)

            def call(arg):
                if cap and not kern.can(pid, cap):
                    kern.syscall(pid, tag, arg, 0, 0, denied=True)
                    return (f"[refused: this process has no '{cap}' capability — say in your "
                            "report what you would have done]")
                t0, out = time.monotonic(), ""
                try:
                    out = fn(arg)
                    return out
                finally:
                    kern.syscall(pid, tag, arg, (time.monotonic() - t0) * 1000,
                                 len(out) if isinstance(out, str) else 0)
            return call
        return {t: wrap(t, f) for t, f in table.items()}

    def _pin_for(self, program):
        """The model a program asked for: 'fast', 'chat', an id, or nothing
        (the governor decides)."""
        want = ((program or {}).get("model") or "").strip()
        if not want:
            return None
        chat = (self.settings.get("model") or DEFAULT_MODEL).strip()
        if want.lower() == "chat":
            return chat
        if want.lower() == "fast":
            return (_governor.fast_model(self.settings, chat, _pricing.PRICES, _pricing.SUGGESTED,
                                         lambda m: _pricing.price_for(m, self.settings))
                    if _governor is not None else chat)
        return want

    def programs(self):
        """Every agent program: built-in, yours, this project's. -> {name: prog}"""
        if _programs is None:
            return {}
        dirs = [CONFIG_DIR / "programs"]
        ws = getattr(self, "workspace", None)
        if ws:
            dirs.append(Path(ws) / ".chuck" / "programs")
        progs, self._program_problems = _programs.load(dirs)
        return progs

    def run_program(self, spec, parent=None, mission=None):
        """/run <program> [input] -> its pid, or None."""
        if _programs is None or self.kernel is None or _worker is None:
            self._sys_note("Programs need the programs, kernel and worker modules — "
                           "reinstall Chuck.", "danger")
            return None
        name, _sp, inp = (spec or "").strip().lstrip("@").partition(" ")
        name, inp = name.lower(), inp.strip()
        progs = self.programs()
        prog = progs.get(name)
        if prog is None:
            self._sys_note((f"No program {name!r}. " if name else "")
                           + "Programs: " + ", ".join(sorted(progs)) + " (/programs)", "danger")
            return None
        brief = inp or f"Do your job on this {'folder' if self.workspace else 'machine'}."
        if prog["mode"] == "act":
            return self.spawn_background(brief, program=prog, parent=parent, mission=mission)
        root = self._workroot() or Path.home()
        caps = _programs.caps_of(prog)
        if self._folder_access() == "sandbox":
            caps.discard("net")
        proc = self.kernel.spawn(f"{name}: {inp}"[:60] if inp else name, kind="program",
                                 brief=brief, parent=parent or self._pid1, program=name, nice=1,
                                 quota={"tokens": prog["tokens"], "seconds": prog["minutes"] * 60},
                                 caps=caps)
        pid = proc.pid
        entry = {"pid": pid, "brief": brief, "name": f"{name}: {inp}"[:60] if inp else name,
                 "ws": None, "report": None, "status": "running", "root": root, "changes": {},
                 "daemon": None, "program": name, "kind": "report", "mission": mission,
                 "waited": bool(mission)}
        self._bg[pid] = entry
        if not mission:
            self._bg_card(entry)
        self._launch_report(entry, prog)
        if mission:
            return pid
        self._sys_note(f"\u25b6 {name} started as pid {pid} \u2014 it looks and reports; you can "
                       "keep going (/top)", "dim")
        return pid

    def _launch_report(self, entry, prog, resume=None):
        """Run a report program's process (fresh, resumed or forked)."""
        pid, root, brief, kern = entry["pid"], entry["root"], entry["brief"], self.kernel
        entry.setdefault("key", _hibernate.new_key(pid) if _hibernate else str(pid))
        entry.setdefault("steps", [])
        call = self._sub_call(pid, independent=True, role="reader", pin=self._pin_for(prog))
        table = {t: f for t, f in self._daemon_syscalls(root, net=kern.can(pid, "net")).items()
                 if t in prog["tools"]}
        table.update(self._os_calls(pid, can_ask=True))
        syscalls = self._guard(pid, table)

        def run():
            report, status = "", 1
            try:
                def ev(t):
                    kern.note(pid, t)
                    GLib.idle_add(self._bg_paint, entry)
                entry["ended"] = {}
                report, _steps = _worker.run_worker(
                    brief, call, self._worker_parse, syscalls,
                    max_steps=self.cfg("program_steps", 30, 3, 200), on_event=ev,
                    outcome=entry["ended"],
                    inbox=self._inbox_for(pid), status_line=self._status_for(pid),
                    should_stop=lambda: kern.should_stop(pid),
                    wrap_up=lambda: kern.terminating(pid) or bool(kern.over_quota(pid)),
                    context=f"{_now_line()}\nYour working directory: {root}.",
                    prompt=_programs.report_prompt(prog), resume=resume,
                    on_step=lambda m, n: self._on_step(entry, m, n))
                status = 1 if kern.should_stop(pid) else 0
            except Exception as ex:
                killed = _kernel is not None and isinstance(ex, _kernel.ProcessKilled)
                report = f"(stopped: {ex})" if killed else f"(failed: {ex})"
            kern.exit(pid, status, report)
            GLib.idle_add(self._bg_finished, entry, report, status)
        threading.Thread(target=run, daemon=True, name=f"program-{pid}").start()

    def show_programs(self):
        progs = self.programs()
        card, head, body, foot = self._card_shell(
            "PROGRAMS", lambda: "\n".join(f"{n}: {p['description']}" for n, p in sorted(progs.items())))
        if _programs is None:
            body.append(Gtk.Label(label="The programs module is missing — reinstall Chuck.", xalign=0))
            self._log_pin(card)
            return False
        intro = Gtk.Label(label="An agent program is a prompt with a mode, tools, a model and a quota. "
                                "Run one with /run <name> [what to focus on]; the agent runs them with "
                                "```spawn @name```. Add your own as ~/.config/chucknorris/programs/"
                                "<name>.md or .chuck/programs/<name>.md in a project.",
                          xalign=0, wrap=True)
        intro.add_css_class("dim"); _wrap_anywhere(intro)
        body.append(intro)
        for n, p in sorted(progs.items()):
            body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            t = Gtk.Label(label=f"{n} · {'acts in a copy' if p['mode'] == 'act' else 'reports'} · "
                                f"{' '.join(sorted(_programs.caps_of(p)))} · "
                                f"{p['model'] or 'governor'} · {_pricing.fmt_tokens(p['tokens'])} tokens",
                          xalign=0, wrap=True)
            t.add_css_class("slash-cmd"); _wrap_anywhere(t)
            d = Gtk.Label(label=p["description"] + ("" if p["source"] == "built-in"
                                                    else f"  ({p['source']})"), xalign=0, wrap=True)
            _wrap_anywhere(d)
            body.append(t); body.append(d)
        for prob in getattr(self, "_program_problems", []):
            e = Gtk.Label(label="✗ " + prob, xalign=0, wrap=True)
            e.add_css_class("danger"); _wrap_anywhere(e)
            body.append(e)
        self._log_pin(card)
        return True

    def governor_command(self, arg):
        """/governor [performance|balanced|powersave]"""
        if _governor is None:
            self._sys_note("The governor module is missing — reinstall Chuck.", "danger")
            return False
        arg = (arg or "").strip().lower()
        if arg:
            if arg not in _governor.MODES:
                self._sys_note("/governor performance | balanced | powersave", "danger")
                return False
            self.settings["governor"] = arg
            save_settings(self.settings)
        chat = (self.settings.get("model") or DEFAULT_MODEL).strip()
        mode = _governor.mode_of(self.settings)
        po = lambda m: _pricing.price_for(m, self.settings)     # noqa: E731
        rows = _governor.table(self.settings, chat, _pricing.PRICES, _pricing.SUGGESTED, po)
        sv = _governor.saving(self.settings, chat, _pricing.PRICES, _pricing.SUGGESTED, po)
        card, head, body, foot = self._card_shell(f"GOVERNOR · {mode.upper()}")
        lb = Gtk.Label(label={"performance": "Every call on the chat model.",
                              "balanced": "The chat and background writers on the chat model; "
                                          "read-only work on the fast model.",
                              "powersave": "Everything on the fast model."}[mode]
                       + (f" The fast model costs about {sv:.0%} less." if sv else ""),
                       xalign=0, wrap=True)
        lb.add_css_class("dim"); _wrap_anywhere(lb)
        body.append(lb)
        grid = Gtk.Grid(column_spacing=18, row_spacing=4)
        for i, (role, model) in enumerate(rows):
            a = Gtk.Label(label=role, xalign=0); a.add_css_class("dim")
            b = Gtk.Label(label=model.split("/")[-1], xalign=0); b.add_css_class("slash-cmd")
            grid.attach(a, 0, i, 1, 1); grid.attach(b, 1, i, 1, 1)
        body.append(grid)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for m in _governor.MODES:
            bt = Gtk.Button(label=m.capitalize()); bt.add_css_class("quick")
            if m == mode:
                bt.add_css_class("accent-btn")
            bt.connect("clicked", lambda _b, mm=m: self.governor_command(mm))
            btns.append(bt)
        foot.append(btns)
        self._log_pin(card)
        self._update_subtitle()
        return True

    def show_strace(self, arg):
        """/strace <pid>: every syscall a process made, with its latency and
        how much it returned."""
        try:
            pid = int((arg or "").split()[0])
        except (IndexError, ValueError):
            self._sys_note("/strace <pid> — /top lists them.", "dim")
            return False
        p = self.kernel.procs.get(pid) if self.kernel else None
        if p is None:
            self._sys_note(f"No process {pid}.", "danger")
            return False
        calls = self.kernel.strace(pid)
        t0 = calls[0][0] if calls else 0

        def text():
            def size_of(n):
                return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
            return "\n".join(f"{'+%.1fs' % (t - t0):>8}  {tool:<6}  {a}  \u2192 "
                             + ("DENIED" if den else f"{size_of(size)} chars, {ms:.0f} ms")
                             for t, tool, a, ms, size, den in calls)
        card, head, body, foot = self._card_shell(f"STRACE · PID {pid} · {p.name}", text)
        caps = "all" if p.caps is None else " ".join(c for c in _kernel.CAPS if c in p.caps)
        n_den = sum(1 for c in calls if c[5])
        sm = Gtk.Label(label=f"{len(calls)} syscall{'' if len(calls) == 1 else 's'} · "
                             f"{sum(c[3] for c in calls) / 1000:.1f}s in tools · "
                             f"{n_den} denied · capabilities: {caps} · "
                             f"{_pricing.fmt_tokens(p.tokens())} tokens in {p.own['calls']} model calls",
                       xalign=0, wrap=True)
        sm.add_css_class("dim"); _wrap_anywhere(sm)
        body.append(sm)
        lb = Gtk.Label(label=clip_output(text(), 30000) if calls else "(no syscalls yet)", xalign=0,
                       selectable=True)
        lb.add_css_class("cmd-text"); _wrap_anywhere(lb)
        body.append(lb)
        self._log_pin(card)
        return True

    # ── what an agent needs from its OS: talk, ask, share, pace, wait ──
    def _os_calls(self, pid, can_ask=False):
        """The calls every agent process gets, whatever its program: the
        shared board, and (background agents) asking the user."""
        kern = self.kernel

        def post(body):
            key, sep, val = (body or "").partition(":")
            if not sep or not key.strip() or not val.strip():
                return "[post: write it as ```post <key>: <fact>```]"
            kern.post(key.strip()[:120], val.strip()[:4000], by=pid)
            return f"[posted {key.strip()[:120]!r} on the board]"
        calls = {"post": post, "board": lambda _a: self._board_text() or "(the board is empty)"}
        if can_ask:
            calls["ask"] = lambda q: self._bg_ask(pid, q)
        return calls

    def _board_text(self, limit=12000):
        if self.kernel is None:
            return ""
        rows = [f"{k} (pid {by}): {v}" for k, v, by, _t in self.kernel.board_items()]
        return clip_output("\n".join(rows), limit)

    def _inbox_for(self, pid):
        """A process's mail, as the worker hears it."""
        kern = self.kernel

        def take():
            return [("the user" if s == 0 else f"Chuck (pid {s})" if s == self._pid1 else f"pid {s}")
                    + f": {m}" for s, m in kern.drain(pid)]
        return take

    def _status_for(self, pid):
        """The [kernel] line an agent reads with every result: what it has
        spent and what it has left, so it can pace itself."""
        kern = self.kernel

        def line():
            p = kern.procs.get(pid)
            if p is None:
                return ""
            q = p.quota
            parts = [f"tokens {_pricing.fmt_tokens(p.tokens())}"
                     + (f" of {_pricing.fmt_tokens(q['tokens'])}" if q.get("tokens") else "")]
            left = kern._left(p, "seconds")
            if left is not None:
                parts.append(f"{int(left // 60)} min left")
            n = len(kern.board)
            if n:
                parts.append(f"{n} fact{'' if n == 1 else 's'} on the board")
            return " · ".join(parts)
        return line

    def _bg_ask(self, pid, question):
        """A background agent asks the user and waits (WAITING in /top),
        while everything else goes on. Runs on the agent's thread."""
        question = (question or "").strip()
        if not question:
            return "[ask: what's the question?]"
        kern = self.kernel
        ev, box = threading.Event(), {}
        self._asks[pid] = (ev, box, question)
        kern.block(pid, "asking you: " + question[:120])
        GLib.idle_add(self._ask_card, pid, question, box)
        deadline = time.monotonic() + self.cfg("ask_minutes", 30, 1, 1440) * 60
        try:
            while not ev.wait(0.5):
                if kern.should_stop(pid) or time.monotonic() > deadline:
                    break
        finally:
            kern.unblock(pid)
            if self._asks.get(pid, (None, None))[1] is box:
                self._asks.pop(pid, None)
            # its card can't answer a later question from the same agent
            GLib.idle_add(lambda: (box.get("close", lambda: None)(), False)[1])
        if "answer" in box:
            return f"The user answered: {box['answer']}"
        return ("[no answer (timed out or stopped): decide yourself, and say in your report what "
                "you assumed]")

    def _ask_card(self, pid, question, box=None):
        p = self.kernel.procs.get(pid) if self.kernel else None
        card, head, body, foot = self._card_shell(f"PID {pid} ASKS · {(p.name if p else '')[:40]}",
                                                  lambda: question)
        lb = Gtk.Label(label=question[:3000], xalign=0, wrap=True, selectable=True)
        _wrap_anywhere(lb); body.append(lb)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ent = Gtk.Entry(hexpand=True)
        ent.set_placeholder_text("Your answer (it waits; everything else goes on)")
        btn = Gtk.Button(label="Answer"); btn.add_css_class("quick"); btn.add_css_class("accent-btn")

        def send(*_a):
            if self.answer_ask(pid, ent.get_text()):
                ent.set_sensitive(False); btn.set_sensitive(False)
        ent.connect("activate", send)
        btn.connect("clicked", send)
        if box is not None:
            def close():
                ent.set_sensitive(False); btn.set_sensitive(False)
                if "answer" not in box:
                    ent.set_placeholder_text("(no longer waiting)")
            box["close"] = close
            if box.get("done"):
                close()
        row.append(ent); row.append(btn)
        foot.append(row)
        self._log_pin(card)
        self._sys_note(f"❓ pid {pid} is waiting for your answer (or /answer {pid} <text>)", "dim")
        try:
            if not self.is_active():
                n = Gio.Notification.new(f"Agent {pid} has a question")
                n.set_body(question[:200])
                self.get_application().send_notification(f"chuck-ask-{pid}", n)
        except Exception:
            pass
        return False

    def answer_ask(self, pid, text):
        try:
            pid = int(str(pid).strip())
        except ValueError:
            return False
        text = (text or "").strip()
        got = self._asks.get(pid)
        if got is None or not text:
            self._sys_note(f"pid {pid} isn't waiting for an answer." if got is None
                           else "Type an answer first.", "dim")
            return False
        ev, box, _q = got
        box["answer"] = text
        ev.set()
        self._sys_note(f"→ answered pid {pid}", "dim")
        return True

    def send_to(self, pid, text, sender=0):
        """A message to a running agent: it reads it with its next results."""
        try:
            pid = int(str(pid).strip())
        except ValueError:
            return "a pid is a number (/top lists them)"
        text = (text or "").strip()
        if not text:
            return "nothing to send"
        if pid == self._pid1:
            return "pid 1 is this chat"
        if self.kernel is None or not self.kernel.send(pid, text, sender=sender):
            return f"no running process {pid}"
        return f"sent to pid {pid}; it reads it at its next step"

    def show_board(self):
        rows = self.kernel.board_items() if self.kernel else []
        card, head, body, foot = self._card_shell("BOARD", lambda: self._board_text())
        if not rows:
            lb = Gtk.Label(label="Empty. Agents post what they find and what they're changing "
                                 "(```post <key>: <fact>```); every agent can read it.",
                           xalign=0, wrap=True)
            lb.add_css_class("dim"); body.append(lb)
        for k, v, by, t in rows[:60]:
            kl = Gtk.Label(label=f"{k} · pid {by} · {time.strftime('%H:%M', time.localtime(t))}",
                           xalign=0, wrap=True)
            kl.add_css_class("slash-cmd"); _wrap_anywhere(kl)
            vl = Gtk.Label(label=str(v)[:2000], xalign=0, wrap=True, selectable=True)
            _wrap_anywhere(vl)
            body.append(kl); body.append(vl)
        self._log_pin(card)
        return True

    @tool("mission", cap=1)
    def _t_mission(self, body):
        """```mission <goal>``` — plan a multi-agent mission; it starts only
        when the user presses Start."""
        m = self.start_mission(body, autostart="auto")
        if m is None:
            return "[mission: not started (see the note above)]"
        return ("[mission planning: a card shows its tasks. If its estimate is under the user's "
                "auto-start limit it starts by itself; otherwise it waits for their Start. Each "
                "task's work is tested and merged one by one; you'll hear when it completes.]")

    @tool("send", cap=4)
    def _t_send(self, body):
        """```send <pid> <message>``` — tell a running agent something."""
        m = re.match(r"\s*(\d+)\s+(.+)$", body or "", re.DOTALL)
        if not m:
            return "[send: write it as ```send <pid> <message>```]"
        return f"[send {m.group(1)}: {self.send_to(m.group(1), m.group(2), sender=self._pid1)}]"

    @tool("post", cap=6)
    def _t_post(self, body):
        """```post <key>: <fact>``` — share a fact with every agent."""
        if self.kernel is None:
            return "[post: no kernel]"
        return self._os_calls(self._pid1)["post"](body)

    @tool("board", cap=1, bare=True)
    def _t_board(self, _body):
        """```board``` — what the agents have shared."""
        return "[board]\n" + (self._board_text() or "(empty)")

    @tool("wait", cap=2)
    def _t_wait(self, body):
        """```wait <pid> [minutes]``` — wait for a background agent or program
        to finish, and get its report."""
        m = re.match(r"\s*(\d+)(?:\s+(\d+))?", body or "")
        if not m:
            return "[wait: write it as ```wait <pid> [minutes]```]"
        pid, mins = int(m.group(1)), min(60, int(m.group(2) or 10))
        e = self._bg.get(pid)
        if e is None:
            return f"[wait: no background agent or program {pid}]"
        e["waited"] = True                    # its report comes back here, not as a note
        gen = self._gen

        def result():
            if e["status"] == "running":
                e["waited"] = False             # not waited for after all: its report comes as a note
                return (f"[wait {pid}: still running after {mins} min — it reports when done; "
                        "carry on]")
            ch = e.get("changes") or {}
            return (f"[pid {pid} ({e['name']}) finished. Its report:\n{e.get('report')}"
                    + (f"\nFiles it changed in its copy: {', '.join(list(ch)[:30])}. They reach the "
                       f"folder only when merged (```join {pid}```)." if ch else "") + "]")
        if e["status"] != "running":
            return result()

        def work():
            end = time.monotonic() + mins * 60
            while time.monotonic() < end and e["status"] == "running":
                if self._cancelled or gen != self._gen:
                    e["waited"] = False
                    return f"[wait {pid}: stopped]"
                if self.kernel.wait(pid, timeout=0.5) is not None:
                    for _ in range(40):       # its card and report land on the UI thread
                        if e["status"] != "running":
                            break
                        time.sleep(0.05)
            return result()
        return work

    # ── hibernate, replay, fork (Chuck OS, phase 21) ──
    PROCS_DIR = DATA_DIR / "procs"
    PROC_SHADOWS = DATA_DIR / "proc-shadows"

    def _shadow_for(self, entry):
        """A writer's copy of the folder is snapshotted after every step, in
        a private git store, so it can be forked from any step."""
        ws = entry.get("ws")
        if entry.get("shadow") is not None or ws is None or _checkpoint is None:
            return entry.get("shadow")
        try:
            entry["shadow"] = _checkpoint.Shadow(ws.work, self.PROC_SHADOWS)
        except Exception:
            entry["shadow"] = None
        return entry["shadow"]

    def _on_step(self, entry, msgs, step):
        """After every step: snapshot, journal, hibernate. Runs on the
        agent's own thread; a failure here never stops the agent."""
        try:
            if msgs is not None:
                entry["msgs"] = [dict(m) for m in msgs]
            sha = None
            sh = entry.get("shadow")
            if sh is not None:
                try:
                    sha = sh.snapshot(f"step {step}")
                except Exception:
                    sha = None
            p = self.kernel.procs.get(entry["pid"]) if self.kernel else None
            reply = entry["msgs"][-2]["content"] if msgs is not None and len(entry["msgs"]) >= 4 else ""
            if msgs is None and sha and not entry.get("base_sha"):
                entry["base_sha"] = sha             # a fresh run: it starts at the base
            entry["steps"].append({"step": step, "sha": sha, "time": time.time(),
                                   "tokens": p.tokens() if p else 0,
                                   "tools": _hibernate.tool_labels(reply) if _hibernate else ""})
            self._hib_save(entry)
        except Exception:
            pass

    def _hib_save(self, entry):
        if self._hib is None or not entry.get("key"):
            return
        ws = entry.get("ws")
        d = entry.get("daemon") or {}
        self._hib.save({
            "key": entry["key"], "kind": entry.get("kind", "background"), "name": entry["name"],
            "brief": entry["brief"], "root": str(entry["root"]), "ws_dir": str(ws.dir) if ws else "",
            "program": entry.get("program", ""), "daemon_id": d.get("id"),
            "status": entry["status"], "report": entry.get("report"), "msgs": entry.get("msgs") or [],
            "steps": entry.get("steps", []), "changes": entry.get("changes", {}),
            "parent": entry.get("parent", ""), "base_sha": entry.get("base_sha"),
            "mission": entry.get("mission"),
            "time": time.time()})

    def _hib_drop(self, entry):
        if self._hib is not None and entry.get("key"):
            self._hib.remove(entry["key"])
        sh = entry.get("shadow")
        if sh is not None:
            shutil.rmtree(sh.git_dir, ignore_errors=True)
            entry["shadow"] = None

    def _new_entry_proc(self, rec, name=None, kind=None, parent_label="", spent=0, parent=None):
        """A kernel process for a resumed or forked run. A resumed one gets
        what was left of its quota (`spent` tokens are gone), not a fresh
        one: crash-and-resume must never be a way round a budget."""
        prog = self.programs().get(rec.get("program")) if rec.get("program") else None
        kind = kind or rec.get("kind", "background")
        caps = (_programs.caps_of(prog) if prog is not None
                else {"read", "write", "exec", "net"} if kind == "background" else {"read", "exec", "net"})
        quota = ({"tokens": prog["tokens"], "seconds": prog["minutes"] * 60} if prog is not None else
                 {"tokens": self.cfg("background_tokens", 1500000, 20000, 50000000),
                  "seconds": self.cfg("background_minutes", 60, 1, 1440) * 60})
        if spent:
            quota["tokens"] = max(5000, quota["tokens"] - int(spent))
        acc = self.settings.get("folder_access") or {}
        if (acc.get(str(rec["root"]), "full") if isinstance(acc, dict) else "full") == "sandbox":
            caps.discard("net")                # an offline-sandboxed folder stays offline
        proc = self.kernel.spawn((name or rec["name"])[:60],
                                 kind="program" if kind == "report" else "background",
                                 brief=rec["brief"], parent=parent or self._pid1, quota=quota, nice=2,
                                 caps=caps, program=rec.get("program", ""))
        dmn = (self._daemons.get(rec["daemon_id"])
               if rec.get("daemon_id") is not None and self._daemons is not None else None)
        entry = {"pid": proc.pid, "brief": rec["brief"], "name": name or rec["name"], "ws": None,
                 "report": None, "status": "running", "root": Path(rec["root"]), "changes": {},
                 "daemon": dmn if not parent_label else None, "program": rec.get("program", ""),
                 "kind": kind, "parent": parent_label}
        return entry, prog

    def resume_hibernated(self, rec, parent=None, mission=None):
        """Go on with a run that was cut off (Chuck closed, crashed, the
        machine rebooted) from its last checkpoint -> the new pid."""
        if self.kernel is None or _worker is None:
            return None
        kind = rec.get("kind", "background")
        if rec.get("program") and rec["program"] not in self.programs():
            self._sys_note(f"Can't resume: the program {rec['program']!r} is gone.", "danger")
            return None
        ws = None
        if kind == "background":
            if not rec.get("ws_dir") or not Path(rec["ws_dir"], "work").is_dir():
                self._sys_note("Can't resume: its copy of the folder is gone.", "danger")
                self._hib.remove(rec["key"])
                self._drop_shadow_of(rec.get("ws_dir"))
                return None
            ws = _cow.Workspace(rec["root"], rec["ws_dir"])
        st = rec.get("steps") or [{}]
        entry, prog = self._new_entry_proc(rec, spent=st[-1].get("tokens", 0), parent=parent)
        if mission:
            entry["mission"], entry["waited"] = tuple(mission), True
        entry["key"], entry["steps"], entry["msgs"] = rec["key"], list(rec.get("steps") or []), rec["msgs"]
        entry["base_sha"] = rec.get("base_sha")
        self._bg[entry["pid"]] = entry
        self._bg_card(entry)
        if kind == "background":
            self._launch_bg(entry, prog, resume=rec["msgs"], ws=ws)
        else:
            self._launch_report(entry, prog, resume=rec["msgs"])
        n = len(_hibernate.steps_of(rec["msgs"]))
        self._sys_note(f"↻ {rec['name'][:60]} resumed at step {n} as pid {entry['pid']}", "ok")
        return entry["pid"]

    def _restore_finished(self, rec):
        """A finished background agent's unmerged work, after a restart: its
        card comes back with Merge and Discard."""
        if not rec.get("ws_dir") or not Path(rec["ws_dir"], "work").is_dir():
            self._hib.remove(rec["key"])
            return None
        entry, _prog = self._new_entry_proc(rec)
        entry.update(key=rec["key"], steps=list(rec.get("steps") or []), msgs=rec["msgs"],
                     ws=_cow.Workspace(rec["root"], rec["ws_dir"]), base_sha=rec.get("base_sha"))
        self.kernel.exit(entry["pid"], 0, rec.get("report"))
        self._bg[entry["pid"]] = entry
        if not rec.get("mission"):
            self._bg_card(entry)
        self._shadow_for(entry)
        self._bg_finished(entry, rec.get("report") or "(no report)", 0, quiet=True)
        return entry["pid"]

    def offer_hibernated(self):
        """At startup: runs that were cut off are offered for resuming;
        finished work that was never merged comes back as it was."""
        if self._hib is None or self.kernel is None:
            return False
        self._hib.prune()
        cut = []
        boot, self._hib_boot = self._hib_boot, []
        mine = {e.get("key") for e in self._bg.values()}
        mission_recs = [r for r in boot if r.get("mission")]
        for rec in boot:
            if rec["key"] in mine or rec.get("mission"):
                continue
            if rec.get("status") == "running":
                cut.append(rec)
            elif rec.get("kind") == "background" and rec.get("status") == "finished":
                self._restore_finished(rec)
            else:
                self._hib.remove(rec["key"])
        if _missions is not None:
            self._missions_boot(mission_recs)
        if not cut:
            return False
        card, head, body, foot = self._card_shell("INTERRUPTED AGENTS")
        lb = Gtk.Label(label="These were working when Chuck stopped. Each was checkpointed after "
                             "every step: resume it exactly where it was, or drop it.",
                       xalign=0, wrap=True)
        lb.add_css_class("dim"); _wrap_anywhere(lb)
        body.append(lb)
        for rec in cut:
            body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            n = len(_hibernate.steps_of(rec["msgs"]))
            st = rec.get("steps") or [{}]
            t = Gtk.Label(label=f"{rec['name'][:80]} · step {n} · "
                                f"{_pricing.fmt_tokens(st[-1].get('tokens', 0))} tokens so far",
                          xalign=0, wrap=True)
            t.add_css_class("slash-cmd"); _wrap_anywhere(t)
            body.append(t)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            res = Gtk.Button(label="Resume"); res.add_css_class("quick"); res.add_css_class("accent-btn")
            drop = Gtk.Button(label="Drop"); drop.add_css_class("quick")

            def go(_b, r=rec, a=res, d=drop):
                a.set_sensitive(False); d.set_sensitive(False)
                self.resume_hibernated(r)

            def no(_b, r=rec, a=res, d=drop):
                a.set_sensitive(False); d.set_sensitive(False)
                self._hib.remove(r["key"])
                if r.get("ws_dir"):
                    self._drop_shadow_of(r["ws_dir"])
                    shutil.rmtree(r["ws_dir"], ignore_errors=True)
                self._sys_note(f"Dropped {r['name'][:60]}.", "dim")
            res.connect("clicked", go); drop.connect("clicked", no)
            row.append(res); row.append(drop)
            body.append(row)
        self._log_pin(card)
        self._hib_offered = cut
        return True

    def show_replay(self, arg):
        """/replay <pid>: the run step by step: what it did, what it got
        back, with Fork here on every step."""
        try:
            pid = int((arg or "").split()[0])
        except (IndexError, ValueError):
            self._sys_note("/replay <pid> — a background agent or program (/top lists them).", "dim")
            return False
        e = self._bg.get(pid)
        if e is None or not e.get("msgs"):
            self._sys_note(f"No recorded run for pid {pid} (only background agents and programs "
                           "are journalled).", "danger")
            return False
        steps = _hibernate.steps_of(e["msgs"])
        shas = {s["step"]: s for s in e.get("steps", [])}

        def text():
            return "\n\n".join(f"STEP {n}\n{a}\n--- got back ---\n{r}" for n, a, r in steps)
        card, head, body, foot = self._card_shell(f"REPLAY · PID {pid} · {e['name'][:50]}", text)
        sm = Gtk.Label(label=f"{len(steps)} steps" + (" · its files are snapshotted at each one"
                                                      if e.get("shadow") else "")
                       + " · Fork here starts a new agent from that step, with the files as "
                         "they were then", xalign=0, wrap=True)
        sm.add_css_class("dim"); _wrap_anywhere(sm)
        body.append(sm)
        for n, a, r in [(0, e["brief"], "")] + steps:
            body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            st = shas.get(n, {})
            tl = Gtk.Label(label=(f"step {n} · " + (st.get("tools") or "thinking")
                                  + f" · {_pricing.fmt_tokens(st.get('tokens', 0))} tokens")
                           if n else "start · the brief", xalign=0, wrap=True)
            tl.add_css_class("slash-cmd"); _wrap_anywhere(tl)
            body.append(tl)
            al = Gtk.Label(label=clip_output(a, 600), xalign=0, wrap=True, selectable=True)
            _wrap_anywhere(al); body.append(al)
            if r:
                rl = Gtk.Label(label=clip_output(r, 400), xalign=0, wrap=True, selectable=True)
                rl.add_css_class("dim"); _wrap_anywhere(rl); body.append(rl)
            fb = Gtk.Button(label="Fork here"); fb.add_css_class("quick")
            fb.set_halign(Gtk.Align.START)
            fb.connect("clicked", lambda _b, k=n: (
                self.entry.get_buffer().set_text(f"/fork {pid} {k} "), self.entry.grab_focus()))
            body.append(fb)
        self._log_pin(card)
        return True

    def fork_process(self, arg):
        """/fork <pid> <step> [new instruction] -> the new pid. A new agent
        starts from that step of the old one's run: the same conversation up
        to there, its copy of the folder as it was then, and your
        instruction where it picks up. The original is untouched."""
        m = re.match(r"\s*(\d+)\s+(\d+)\s*(.*)$", arg or "", re.DOTALL)
        if not m:
            self._sys_note("/fork <pid> <step> [new instruction] — /replay <pid> shows the steps.", "dim")
            return None
        pid, step, instr = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        e = self._bg.get(pid)
        if e is None or not e.get("msgs") or self.kernel is None:
            self._sys_note(f"No recorded run for pid {pid}.", "danger")
            return None
        total = len(_hibernate.steps_of(e["msgs"]))
        if step > total:
            self._sys_note(f"pid {pid} has {total} steps.", "danger")
            return None
        if e.get("program") and e["program"] not in self.programs():
            self._sys_note(f"Can't fork: the program {e['program']!r} isn't available here "
                           "(a project's program is only found in its folder).", "danger")
            return None
        msgs = _hibernate.fork_messages(e["msgs"], step, instr)
        rec = {"name": f"fork of {pid}@{step}: {e['name']}"[:60], "brief": e["brief"],
               "root": str(e["root"]), "program": e.get("program", ""), "kind": e.get("kind", "background")}
        ws = None
        if rec["kind"] == "background":
            src = e.get("ws")
            sh = e.get("shadow")
            snaps = {s["step"]: s.get("sha") for s in e.get("steps", [])}
            if src is None or not src.base.is_dir() or sh is None or not e.get("base_sha"):
                self._sys_note("Can't fork: that run's copy of the folder or its snapshots are gone "
                               "(merged or discarded).", "danger")
                return None
            if not snaps.get(step):
                first = min(snaps) if snaps else 0
                self._sys_note(f"pid {pid}'s files weren't snapshotted at step {step}"
                               + (f" (it is itself a fork from step {first}: fork its parent for "
                                  "earlier steps)" if first else ""), "danger")
                return None
            entry, prog = self._new_entry_proc(rec, parent_label=f"{pid}@{step}")
            try:
                entry["key"] = _hibernate.new_key(entry["pid"])
                d = self.WORKTREES / f"{entry['key']}-fork-of-{pid}"
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(src.base, d / "base", symlinks=True)
                shutil.copytree(src.base, d / "work", symlinks=True)
                ws = _cow.Workspace(e["root"], d)
                # the fork's own store starts at the base, like any run's,
                # so a fork of the fork replays from the same place
                entry["ws"] = ws
                nsh = self._shadow_for(entry)
                if nsh is not None:
                    entry["base_sha"] = nsh.snapshot("base")
                sh.replay_onto(e["base_sha"], snaps[step], d / "work")
            except Exception as ex:
                self.kernel.exit(entry["pid"], 1, f"fork failed: {ex}")
                self._sys_note(f"✗ couldn't fork: {ex}", "danger")
                return None
        else:
            entry, prog = self._new_entry_proc(rec, parent_label=f"{pid}@{step}")
        entry["msgs"] = msgs
        entry["steps"] = [s for s in e.get("steps", []) if s["step"] <= step and rec["kind"] != "background"]
        self._bg[entry["pid"]] = entry
        self._bg_card(entry)
        if rec["kind"] == "background":
            self._launch_bg(entry, prog, resume=msgs, ws=ws)
        else:
            self._launch_report(entry, prog, resume=msgs)
        self._sys_note(f"⑂ forked pid {pid} at step {step} → pid {entry['pid']}"
                       + (f": “{instr[:80]}”" if instr else ""), "ok")
        return entry["pid"]

    # ── missions (Chuck OS, phase 23): a goal, run as a graph of agents ──
    MISSIONS_DIR = DATA_DIR / "missions"

    def start_mission(self, goal, autostart=None):
        """/mission <goal> -> the mission. A planner turns the goal into a
        graph of tasks; nothing runs until you press Start (or autostart)."""
        goal = (goal or "").strip()
        if not goal:
            self._sys_note("/mission needs a goal: /mission set up CI and make every test pass", "dim")
            return None
        if _missions is None or self.kernel is None or _worker is None or _cow is None:
            self._sys_note("Missions need the missions, kernel, worker and cow modules — "
                           "reinstall Chuck.", "danger")
            return None
        root = self._workroot()
        if root is None:
            self._sys_note("Pick a working folder first (/folder): a mission works on a project.",
                           "danger")
            return None
        if self._folder_access() == "readonly":
            self._sys_note("This folder is read-only (/access): a mission would have nothing to "
                           "merge its work into.", "danger")
            return None
        m = _missions.new_mission(goal, root)
        proc = self.kernel.spawn(f"mission: {goal}"[:60], kind="mission", brief=goal,
                                 parent=self._pid1, nice=1,
                                 quota={"tokens": self.cfg("mission_tokens", 5000000, 100000, 100000000),
                                        "seconds": self.cfg("mission_hours", 4, 1, 72) * 3600})
        m["pid"] = proc.pid
        m["autostart"] = (autostart if autostart == "auto" else
                          bool(self.settings.get("mission_autostart", False) if autostart is None
                               else autostart))
        self._missions[m["id"]] = m
        _missions.log(m, "planning")
        self._mission_save(m)
        self._mission_card(m)
        self.kernel.note(m["pid"], "planning")
        call = self._sub_call(m["pid"], independent=True, role="main")
        rules = project_instructions(self.workspace) or ""
        try:
            repo = _builder.repo_map(root, 8000)[1]
        except Exception:
            repo = ""
        sysmsg = (_missions.PLAN_PROMPT.format(max=self.cfg("mission_max_tasks", 8, 2, 12))
                  + f"\n\n{_now_line()}\nProject: {root.name}\n"
                  + (f"Project rules:\n{rules[:3000]}\n" if rules else "")
                  + (f"Files and what they define:\n{repo}" if repo else ""))

        def plan():
            msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": goal}]
            err = ""
            for _attempt in range(2):
                try:
                    reply = call(msgs)
                    tasks, notes = _missions.parse_plan(reply, self.cfg("mission_max_tasks", 8, 2, 12))
                    GLib.idle_add(self._mission_planned, m["id"], tasks, notes, "")
                    return
                except _missions.MissionError as ex:
                    err = str(ex)
                    msgs += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"That plan can't be used: {err}. Reply "
                                                         "with ONLY the corrected JSON."}]
                except Exception as ex:
                    err = str(ex)
                    break
            GLib.idle_add(self._mission_planned, m["id"], None, "", err)
        threading.Thread(target=plan, daemon=True, name=f"mission-plan-{proc.pid}").start()
        self._sys_note(f"\U0001F3AF mission {m['id'][-5:]}: planning “{goal[:80]}”", "dim")
        return m

    def _mission_planned(self, mid, tasks, notes, err):
        m = self._missions.get(mid)
        if m is None or m["state"] == _missions.CANCELLED:
            return False
        if tasks is None:
            m["state"] = _missions.CANCELLED
            _missions.log(m, f"planning failed: {err}")
            self.kernel.exit(m["pid"], 1, f"planning failed: {err}")
            self._sys_note(f"✗ mission couldn't be planned: {err}", "danger")
        else:
            m["tasks"], m["notes"] = tasks, notes
            m["state"] = _missions.AWAITING
            _missions.log(m, f"planned {len(tasks)} tasks")
            self.kernel.note(m["pid"], "waiting for you to start it")
            if m.get("autostart") == "auto":
                # the agent started it: it runs by itself when it's cheap,
                # and waits for one click when it isn't
                tok, usd = self._mission_estimate(m)
                lim = self.cfg("mission_auto_usd", 0.50, 0.0, 1000.0, cast=float)
                cheap = (usd is not None and usd <= lim) or (usd is None and tok <= 1500000)
                if cheap and not self._over_budget():
                    m["state"] = _missions.ACTIVE
                    m["notes"] = ((m.get("notes") or "") + f"\nStarted by itself: estimated "
                                  f"{_pricing.fmt_usd(usd) if usd is not None else _pricing.fmt_tokens(tok) + ' tokens'}"
                                  " (under your auto-start limit). Pause or Cancel any time.").strip()
                else:
                    m["notes"] = ((m.get("notes") or "") + "\nWaiting for Start: estimated "
                                  f"{_pricing.fmt_usd(usd) if usd is not None else _pricing.fmt_tokens(tok) + ' tokens'}"
                                  f", above the {_pricing.fmt_usd(lim)} it may start by itself.").strip()
            elif m.get("autostart"):
                m["state"] = _missions.ACTIVE
        self._mission_save(m)
        self._mission_paint(m)
        if m["state"] == _missions.ACTIVE:
            self._mission_step(m)
        return False

    def _mission_save(self, m):
        p = self.kernel.procs.get(m.get("pid")) if self.kernel is not None and m.get("pid") else None
        if p is not None and m["state"] not in (_missions.COMPLETE, _missions.CANCELLED):
            m["spent_tokens"] = int(m.get("spent_base", 0)) + p.tokens()   # a restart keeps counting
            m["spent_usd"] = float(m.get("spent_base_usd", 0.0)) + p.total["usd"]
            # its time limit counts while Chuck runs it, not the night it was closed
            m["spent_seconds"] = float(m.get("spent_base_seconds", 0.0)) + (self.kernel.clock() - p.started)
        if self._mstore is not None:
            self._mstore.save({k: v for k, v in m.items() if not k.startswith("_")})

    def mission_action(self, mid, verb, tid=None):
        """start / pause / resume / cancel a mission; retry / skip a task."""
        m = self._missions.get(mid)
        if m is None:
            return False
        t = _missions.task(m, tid) if tid else None
        if verb in ("start", "resume") and m["state"] in (_missions.AWAITING, _missions.PAUSED):
            m["state"] = _missions.ACTIVE
            _missions.log(m, verb)
        elif verb == "pause" and m["state"] == _missions.ACTIVE:
            m["state"] = _missions.PAUSED
            _missions.log(m, "paused (running tasks finish; nothing new starts)")
        elif verb == "cancel" and m["state"] not in (_missions.COMPLETE, _missions.CANCELLED):
            m["state"] = _missions.CANCELLED
            _missions.log(m, "cancelled")
            if self.kernel is not None and m.get("pid"):
                self.kernel.signal(m["pid"], _kernel.KILL)   # every agent in it
        elif verb == "retry" and t is not None and t["state"] in (
                _missions.FAILED, _missions.CONFLICT):
            t["state"], t["attempts"], t["note"] = _missions.PENDING, 0, "retrying"
            if m["state"] == _missions.PAUSED:
                m["state"] = _missions.ACTIVE
        elif verb == "skip" and t is not None and t["state"] in (
                _missions.FAILED, _missions.CONFLICT, _missions.PENDING):
            t["state"], t["note"] = _missions.SKIPPED, "skipped by you"
            if m["state"] == _missions.PAUSED and not _missions.blocked(m):
                m["state"] = _missions.ACTIVE
        else:
            return False
        self._mission_save(m)
        self._mission_paint(m)
        self._mission_step(m)
        return True

    def _mission_step(self, m):
        """The orchestrator: start every task that may start, finish the
        mission when every task is done, pause it when one is stuck."""
        if m["state"] != _missions.ACTIVE:
            return
        if _missions.is_complete(m):
            return self._mission_complete(m)
        over = self.kernel.over_quota(m["pid"]) if self.kernel and m.get("pid") else ""
        if over:
            return self._mission_pause(m, f"the mission's budget is spent ({over})")
        for t in _missions.ready(m, self.cfg("mission_parallel", 3, 1, 8)):
            self._mission_launch(m, t)
        # after launching: a launch that failed is stuck too
        stuck = _missions.blocked(m)
        busy = any(t["state"] in (_missions.RUNNING, _missions.VERIFYING) for t in m["tasks"])
        if stuck and not busy and not _missions.ready(m, 99):
            m["state"] = _missions.PAUSED
            _missions.log(m, f"paused: {', '.join(x['id'] for x in stuck)} needs you")
            self._sys_note(f"⚠ mission {m['id'][-5:]} paused: task {stuck[0]['id']} "
                           f"({stuck[0]['title'][:50]}) {stuck[0]['state']} — Retry or Skip it",
                           "danger")
            self._notify(f"Mission paused: {stuck[0]['title'][:60]}", m["goal"][:120])
        self._mission_save(m)
        self._mission_paint(m)

    def _mission_pause(self, m, why):
        m["state"] = _missions.PAUSED
        _missions.log(m, f"paused: {why}")
        self._sys_note(f"\u26a0 mission {m['id'][-5:]} paused: {why}", "danger")
        self._notify("Mission paused", why[:150])
        self._mission_save(m)
        self._mission_paint(m)

    def _mission_launch(self, m, t):
        brief = f"{t['title']}\n\n" + _missions.context_for(m, t)
        tag = (m["id"], t["id"])
        if t["kind"] == "code":
            pid = self.spawn_background(brief, root=Path(m["folder"]), parent=m["pid"], mission=tag)
        else:
            prog = "reviewer" if t["kind"] == "check" else "investigator"
            pid = self.run_program(f"{prog} {brief}", parent=m["pid"], mission=tag)
        if pid is None:
            t["state"], t["note"] = _missions.FAILED, "couldn't start (see the note above)"
            return False
        t["state"], t["pid"] = _missions.RUNNING, pid
        t["key"] = (self._bg.get(pid) or {}).get("key")      # its checkpoint, after a restart
        t["attempts"] = t.get("attempts", 0) + 1
        _missions.log(m, f"{t['id']} started as pid {pid}")
        return True

    def _mission_task_finished(self, entry, report, status):
        """A mission's agent finished: verify, merge, send back, or fail."""
        mid, tid = entry["mission"]
        m = self._missions.get(mid)
        t = _missions.task(m, tid) if m else None
        if m is None or t is None or m["state"] == _missions.CANCELLED:
            return
        t["report"] = (report or "")[:4000]
        pp = self.kernel.procs.get(entry["pid"]) if self.kernel else None
        if pp is not None:
            t["spent"] = int(t.get("spent", 0)) + pp.tokens()      # every attempt, for the estimates
        ended = (entry.get("ended") or {}).get("ended", "report")
        if status == 0 and ended in ("budget", "steps"):
            status = 1                       # cut off at its quota or step limit: not finished
            report = f"(cut off: {'out of budget' if ended == 'budget' else 'out of steps'}) " + (report or "")
        if status != 0:
            if t["attempts"] < self.cfg("mission_attempts", 2, 1, 5) and m["state"] == _missions.ACTIVE:
                t["state"], t["note"] = _missions.PENDING, "stopped; trying again"
            else:
                t["state"], t["note"] = _missions.FAILED, (report or "stopped")[:200]
            _missions.log(m, f"{tid} stopped")
            return self._mission_step(m)
        if t["kind"] != "code":
            t["state"], t["note"] = _missions.DONE, ""
            _missions.log(m, f"{tid} done")
            return self._mission_step(m)
        if not entry.get("changes"):
            t["state"], t["note"] = _missions.DONE, "changed nothing"
            _missions.log(m, f"{tid} done (no changes)")
            return self._mission_step(m)
        t["state"], t["note"] = _missions.VERIFYING, "running the tests on its copy"
        self._mission_paint(m)
        ws = entry["ws"]

        def verify():
            res = self._verify_copy(ws)
            GLib.idle_add(self._mission_verified, entry, res)
        threading.Thread(target=verify, daemon=True, name=f"mission-verify-{entry['pid']}").start()

    def _mission_verified(self, entry, res):
        mid, tid = entry["mission"]
        m = self._missions.get(mid)
        t = _missions.task(m, tid) if m else None
        if m is None or t is None or m["state"] == _missions.CANCELLED:
            return False
        state, out = res
        t["verify"] = state
        if state == "fail":
            if t["attempts"] < self.cfg("mission_attempts", 2, 1, 5):
                _missions.log(m, f"{tid}: tests fail; sent back to fix them")
                t["note"] = "tests fail on its copy; fixing"
                self._mission_send_back(m, t, entry, out)
            else:
                t["state"], t["note"] = _missions.FAILED, "tests still fail: " + out[-160:]
                _missions.log(m, f"{tid}: tests still fail")
                self._mission_step(m)
            return False
        self._mission_merging = True
        try:
            msg = self.join_background(entry["pid"])
        finally:
            self._mission_merging = False
        r = entry.get("merge_result") or {}
        if r.get("conflicts"):
            t["state"], t["note"] = _missions.CONFLICT, msg[:200]
            _missions.log(m, f"{tid}: merge conflict")
        elif entry.get("status") == "merged":
            t["state"] = _missions.MERGED
            t["note"] = ("tests pass" if state == "pass" else "no tests to run") + "; merged"
            _missions.log(m, f"{tid} merged")
        else:
            t["state"], t["note"] = _missions.FAILED, f"couldn't merge: {msg}"[:200]
        self._mission_step(m)
        return False

    def _verify_copy(self, ws):
        """Run the project's tests on an agent's copy -> ("pass"|"fail"|"none",
        output). Never raises: whatever goes wrong is a "fail" with the reason
        (a verify that died silently left a mission waiting forever)."""
        try:
            return self._verify_copy_inner(ws)
        except Exception as ex:
            return "fail", f"(the tests couldn't be run: {ex})"

    def _verify_copy_inner(self, ws):
        """The project's own check (.chuck/test, a shell script: every line
        must pass) wins over discovery. It is read from YOUR folder, never
        from the agent's copy: an agent can't rewrite its own exam."""
        cmd = ""
        custom = Path(ws.source) / ".chuck" / "test"
        if custom.is_file():
            body = custom.read_bytes().decode("utf-8", "replace").strip()
            if body:
                cmd = "set -e\n" + body
        if not cmd:
            try:
                cmd = _builder.test_command_str(ws.work) or ""
            except Exception:
                cmd = ""
        if not cmd:
            return "none", ""
        rc, out = _run_in(ws.work, cmd, self.cfg("verify_timeout", 600, 30, 3600))
        return ("pass" if rc == 0 else "fail"), f"$ {cmd}\nexit {rc}\n{clip_output(out, 6000)}"

    def _mission_send_back(self, m, t, entry, output):
        """Its tests fail: the same agent goes on in the same copy, told what
        failed (its conversation continues; it isn't a fresh start)."""
        msgs = (entry.get("msgs") or [])[:]
        if not msgs:
            t["state"], t["note"] = _missions.PENDING, "tests failed; starting over"
            return self._mission_step(m)
        msgs += [{"role": "assistant", "content": entry.get("report") or "REPORT"},
                 {"role": "user", "content": "The project's tests FAIL on your copy (this is how "
                                             "your work is checked before it is merged):\n\n"
                                             + output + "\n\nFix it, run the tests until they "
                                             "pass, then write your REPORT."}]
        rec = {"name": entry["name"], "brief": entry["brief"], "root": str(entry["root"]),
               "program": "", "kind": "background"}
        new, _prog = self._new_entry_proc(rec, parent=m["pid"])
        new.update(ws=entry["ws"], msgs=msgs, steps=list(entry.get("steps") or []),
                   key=entry.get("key"), shadow=entry.get("shadow"), base_sha=entry.get("base_sha"),
                   mission=entry["mission"], waited=True)
        entry["status"] = "superseded"
        if entry.get("ui"):
            self._set_status(entry["ui"]["pill"], f"continued as {new['pid']}", "wait")
            self._bg_buttons(entry)
        self._bg[new["pid"]] = new
        t["state"], t["pid"] = _missions.RUNNING, new["pid"]
        t["attempts"] = t.get("attempts", 0) + 1
        self._launch_bg(new, None, resume=msgs, ws=entry["ws"])
        self._mission_save(m)
        self._mission_paint(m)

    def _mission_complete(self, m):
        m["state"] = _missions.COMPLETE
        p = self.kernel.procs.get(m["pid"]) if self.kernel else None
        if p is not None:
            m["spent_tokens"] = int(m.get("spent_base", 0)) + p.tokens()
            m["spent_usd"] = float(m.get("spent_base_usd", 0.0)) + p.total["usd"]
        lines = [f"- {t['id']} {t['title']}: {t['state']}"
                 + (f" ({t['note']})" if t.get("note") else "")
                 + (f" — {t['report'].strip().splitlines()[0][:140]}" if t.get("report", "").strip() else "")
                 for t in m["tasks"]]
        try:
            self._mission_learn(m)
        except Exception:
            pass
        summary = (f"Mission complete: {m['goal']}\n" + "\n".join(lines)
                   + f"\nSpent {_pricing.fmt_tokens(m['spent_tokens'])} tokens, "
                     f"{_pricing.fmt_usd(m['spent_usd'])}.")
        m["summary"] = summary
        _missions.log(m, "complete")
        if self.kernel is not None and m.get("pid"):
            self.kernel.exit(m["pid"], 0, summary)
        self._mission_save(m)
        self._mission_paint(m)
        self._sys_note(f"\U0001F3C1 mission complete: {m['goal'][:80]} — "
                       f"{len(m['tasks'])} tasks, {_pricing.fmt_usd(m['spent_usd'])}", "ok")
        self._user_notes.append(summary[:3000])
        self._notify("Mission complete", m["goal"][:120])

    def _notify(self, title, body=""):
        try:
            if not self.is_active():
                n = Gio.Notification.new(title[:80])
                n.set_body(body[:200])
                self.get_application().send_notification(None, n)
        except Exception:
            pass

    _MISSION_ICON = {"pending": "○", "running": "◔", "verifying": "◑", "merged": "✓",
                     "done": "✓", "failed": "✗", "skipped": "–", "conflict": "⚠"}

    _TASK_TOKENS = {"code": 180000, "research": 60000, "check": 80000}    # until it has learned

    def _mission_estimate(self, m):
        """-> (tokens, usd or None): what the plan will likely cost, from what
        this machine's past missions' tasks really spent (a default until
        there are some), at the chat model's price."""
        learned = self.settings.get("mission_task_tokens") or {}
        tok = 0
        for t in m["tasks"]:
            k = t.get("kind", "code")
            got = learned.get(k) if isinstance(learned, dict) else None
            tok += int(got[0]) if isinstance(got, list) and got and got[1] >= 2 else self._TASK_TOKENS.get(k, 150000)
        price = _pricing.price_for(self.settings.get("model") or DEFAULT_MODEL, self.settings)
        usd = None
        if price:
            usd = _pricing.cost({"prompt": int(tok * 0.9), "completion": int(tok * 0.1), "cached": 0}, price)
        return tok, usd

    def _mission_estimate_text(self, m):
        tok, usd = self._mission_estimate(m)
        cap = self.cfg("mission_tokens", 5000000, 100000, 100000000)
        return (f"\nEstimated: about {_pricing.fmt_tokens(tok)} tokens"
                + (f" \u2248 {_pricing.fmt_usd(usd)}" if usd is not None else "")
                + f" (its budget caps it at {_pricing.fmt_tokens(cap)})")

    def _mission_learn(self, m):
        """After a mission: what each kind of task really spent, as a running
        average, so the next estimate is closer to this machine's projects."""
        learned = dict(self.settings.get("mission_task_tokens") or {})
        for t in m["tasks"]:
            p = self.kernel.procs.get(t.get("pid")) if self.kernel and t.get("pid") else None
            spent = int(t.get("spent") or 0) or (p.tokens() if p is not None else 0)
            if not spent or t["state"] not in (_missions.MERGED, _missions.DONE):
                continue
            avg, n = (learned.get(t["kind"]) or [0, 0])[:2]
            learned[t["kind"]] = [int((avg * n + spent) / (n + 1)), min(n + 1, 50)]
        self.settings["mission_task_tokens"] = learned
        save_settings(self.settings)

    def _mission_card(self, m):
        card, head, body, foot = self._card_shell(
            f"MISSION · {m['goal'][:70]}", lambda: m.get("summary") or self._mission_text(m))
        pill = self._pill(m["state"], "run")
        head.insert_child_after(pill, head.get_first_child())
        info = Gtk.Label(xalign=0, wrap=True); info.add_css_class("dim"); _wrap_anywhere(info)
        body.append(info)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(rows)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        foot.append(btns)
        m["_ui"] = {"pill": pill, "info": info, "rows": rows, "btns": btns}
        self._log_pin(card)
        self._mission_paint(m)

    def _mission_text(self, m):
        return (f"MISSION: {m['goal']} [{m['state']}]\n"
                + "\n".join(f"{t['id']} [{t['state']}] ({t['kind']}) {t['title']}"
                            + (f" after {', '.join(t['deps'])}" if t["deps"] else "")
                            for t in m["tasks"]))

    def _mission_paint(self, m):
        ui = m.get("_ui")
        if not ui:
            return False

        def clear(box):
            c = box.get_first_child()
            while c is not None:
                n = c.get_next_sibling()
                box.remove(c)
                c = n
        st = m["state"]
        self._set_status(ui["pill"], st, {"running": "run", "complete": "ok", "cancelled": "fail",
                                          "paused": "wait"}.get(st, "wait"))
        done, n = _missions.progress(m)
        p = self.kernel.procs.get(m["pid"]) if self.kernel and m.get("pid") else None
        spend = (f" · {_pricing.fmt_tokens(int(m.get('spent_base', 0)) + p.tokens())} tokens · "
                 f"{_pricing.fmt_usd(float(m.get('spent_base_usd', 0.0)) + p.total['usd'])}"
                 if p is not None else "")
        ui["info"].set_text(("planning the tasks…" if st == _missions.PLANNING else
                             f"{done} of {n} tasks finished{spend}")
                            + (f"\n{m['notes']}" if m.get("notes") else "")
                            + (self._mission_estimate_text(m) if st == _missions.AWAITING else "")
                            + ("\nEach code task runs in its own copy of the folder; its tests must pass "
                               "on that copy before it is merged. Tasks without a dependency between "
                               "them run at once." if st == _missions.AWAITING else ""))
        clear(ui["rows"])
        for t in m["tasks"]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tl = Gtk.Label(label=f"{self._MISSION_ICON.get(t['state'], '?')} {t['id']} · {t['kind']} · "
                                 f"{t['title']}" + (f"  (after {', '.join(t['deps'])})" if t["deps"] else ""),
                           xalign=0, wrap=True)
            tl.add_css_class("slash-cmd"); _wrap_anywhere(tl)
            row.append(tl)
            sub = t["state"] + (f" · pid {t['pid']}" if t.get("pid") else "") + \
                (f" · {t['note']}" if t.get("note") else "")
            sl = Gtk.Label(label=sub, xalign=0, wrap=True); sl.add_css_class("dim"); _wrap_anywhere(sl)
            row.append(sl)
            tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if t["state"] in (_missions.FAILED, _missions.CONFLICT):
                for label, verb in (("Retry", "retry"), ("Skip", "skip")):
                    b = Gtk.Button(label=label); b.add_css_class("quick")
                    b.connect("clicked", lambda _b, v=verb, i=t["id"]: self.mission_action(m["id"], v, i))
                    tb.append(b)
            e = self._bg.get(t.get("pid")) if t.get("pid") else None
            if e is not None:
                if e.get("report") and t["state"] not in (_missions.RUNNING,):
                    b = Gtk.Button(label="Report"); b.add_css_class("quick")
                    b.connect("clicked", lambda _b, ee=e: self._sys_note(
                        f"{ee['name'][:60]}:\n{ee.get('report')}", "dim"))
                    tb.append(b)
                for label, fn in (("Trace", self.show_strace), ("Replay", self.show_replay)):
                    b = Gtk.Button(label=label); b.add_css_class("quick")
                    b.connect("clicked", lambda _b, f=fn, p=t["pid"]: f(str(p)))
                    tb.append(b)
            if tb.get_first_child() is not None:
                row.append(tb)
            ui["rows"].append(row)
        clear(ui["btns"])
        spec = {_missions.AWAITING: [("Start", "start"), ("Cancel", "cancel")],
                _missions.ACTIVE: [("Pause", "pause"), ("Cancel", "cancel")],
                _missions.PAUSED: [("Resume", "resume"), ("Cancel", "cancel")]}.get(st, [])
        for label, verb in spec:
            b = Gtk.Button(label=label); b.add_css_class("quick")
            if verb == "start":
                b.add_css_class("accent-btn")
            b.connect("clicked", lambda _b, v=verb: self.mission_action(m["id"], v))
            ui["btns"].append(b)
        return False

    def show_missions(self):
        items = sorted(self._missions.values(), key=lambda x: -x.get("created", 0))[:12]
        card, head, body, foot = self._card_shell("MISSIONS")
        if not items:
            lb = Gtk.Label(label="No missions yet. A mission is a goal too big for one agent: "
                                 "/mission migrate the API to async, with tests", xalign=0, wrap=True)
            lb.add_css_class("dim"); body.append(lb)
        for m in items:
            done, n = _missions.progress(m)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lb = Gtk.Label(label=f"{m['goal'][:70]} · {m['state']} · {done}/{n}", xalign=0,
                           hexpand=True, wrap=True)
            _wrap_anywhere(lb)
            b = Gtk.Button(label="Show"); b.add_css_class("quick")
            b.connect("clicked", lambda _b, mm=m: self._mission_card(mm))
            row.append(lb); row.append(b)
            body.append(row)
        self._log_pin(card)
        return True

    def _missions_boot(self, recs):
        """At startup: missions that were running go on. Each task is matched
        to ITS checkpoint (by key): one cut off mid-run resumes from its step,
        one that had finished and was being tested is tested again, one with
        no checkpoint starts over. The mission keeps counting what it spent."""
        if self._mstore is None:
            return
        for m in self._mstore.load():
            if m["id"] in self._missions:
                continue                    # started in this session: it's live, not a leftover
            if m["state"] in (_missions.COMPLETE, _missions.CANCELLED):
                if time.time() - m.get("created", 0) < 7 * 86400:
                    self._missions[m["id"]] = m
                continue
            self._missions[m["id"]] = m
            if m["state"] == _missions.PLANNING:
                m["state"] = _missions.CANCELLED          # its planner is gone; ask again
                _missions.log(m, "cancelled: Chuck closed while it was being planned")
                self._mission_save(m)
                continue
            spent = int(m.get("spent_tokens") or 0)
            m["spent_base"], m["spent_base_usd"] = spent, float(m.get("spent_usd") or 0.0)
            hours = self.cfg("mission_hours", 4, 1, 72) * 3600
            used_s = float(m.get("spent_seconds") or 0.0)
            m["spent_base_seconds"] = used_s
            proc = self.kernel.spawn(
                f"mission: {m['goal']}"[:60], kind="mission", brief=m["goal"], parent=self._pid1,
                nice=1, quota={"tokens": max(1, self.cfg("mission_tokens", 5000000, 100000, 100000000) - spent),
                               "seconds": max(60, hours - used_s)})
            m["pid"] = proc.pid
            mine = [r for r in recs if isinstance(r.get("mission"), list) and r["mission"][0] == m["id"]]
            for t in m["tasks"]:
                if t["state"] not in (_missions.RUNNING, _missions.VERIFYING):
                    continue
                cands = [r for r in mine if r["mission"][1] == t["id"]]
                rec = (next((r for r in cands if t.get("key") and r["key"] == t["key"]), None)
                       or next((r for r in reversed(cands) if r.get("status") == "running"), None)
                       or (cands[-1] if cands else None))
                for r in cands:                      # older attempts: their copies are dead weight
                    if r is not rec:
                        self._hib.remove(r["key"])
                        if r.get("ws_dir"):
                            self._drop_shadow_of(r["ws_dir"])
                            shutil.rmtree(r["ws_dir"], ignore_errors=True)
                if rec is not None and rec.get("status") == "running":
                    pid = self.resume_hibernated(rec, parent=m["pid"], mission=(m["id"], t["id"]))
                    t["state"], t["pid"] = ((_missions.RUNNING, pid) if pid else (_missions.PENDING, None))
                elif (rec is not None and rec.get("status") == "finished" and rec.get("ws_dir")
                      and Path(rec["ws_dir"], "work").is_dir()):
                    pid = self._restore_finished(rec)       # its work, back as it was
                    e = self._bg.get(pid) if pid else None
                    if e is not None:
                        e["mission"], e["waited"] = (m["id"], t["id"]), True
                        t["pid"], t["key"] = pid, rec["key"]
                        self._mission_task_finished(e, rec.get("report") or "", 0)   # test it again
                    else:
                        t["state"], t["pid"] = _missions.PENDING, None
                else:
                    t["state"], t["pid"] = _missions.PENDING, None
            self._mission_save(m)
            self._mission_card(m)
            self._mission_step(m)

    # ── the command palette (Ctrl+P): everything Chuck can do, one search away ──
    def palette_items(self):
        """Every action as an item, built fresh (live processes, missions,
        programs, chats, folders, models). An item runs `fn`, or puts
        `compose` in the composer for you to finish (a goal, a brief)."""
        items = []

        def add(group, title, fn=None, keys="", hint="", compose=None, keywords=""):
            items.append({"group": group, "title": title, "fn": fn, "keys": keys, "hint": hint,
                          "compose": compose, "keywords": keywords, "order": len(items)})
        for pid, (_ev, box, q) in list(getattr(self, "_asks", {}).items()):
            add("Waiting for you", f"Answer pid {pid}: {q[:70]}",
                lambda p=pid, qq=q, b=box: self._ask_card(p, qq, b), keywords="question ask")
        add("Agent OS", "Activity: everything waiting and working", self.show_activity,
            hint="/activity", keywords="tray status")
        add("Agent OS", "Processes (top)", self.show_top, hint="/top", keywords="ps kill agents")
        add("Agent OS", "New mission…", compose="/mission ", hint="/mission <goal>",
            keywords="plan big job graph agents")
        add("Agent OS", "Missions", self.show_missions, hint="/missions")
        add("Agent OS", "Background agent…", compose="/spawn ", hint="/spawn <task>",
            keywords="spawn copy worktree")
        add("Agent OS", "New daemon…", compose="/daemon ", hint="/daemon <when>: <what>",
            keywords="schedule watch cron trigger")
        add("Agent OS", "Daemons", self.show_daemons, hint="/daemons")
        add("Agent OS", "New reminder\u2026", compose="/remind ", hint="/remind <when>: <what>",
            keywords="alarm timer later notify")
        add("Agent OS", "Inbox (daemon reports)", self.show_inbox, hint="/inbox")
        add("Agent OS", "Board (what agents share)", self.show_board, hint="/board")
        add("Agent OS", "Budget", lambda: self.budget_command(""), hint="/budget",
            keywords="spend money limit cost")
        if _governor is not None:
            cur = _governor.mode_of(self.settings)
            for mode in _governor.MODES:
                add("Agent OS", f"Governor: {mode}" + (" ✓" if mode == cur else ""),
                    lambda md=mode: self.governor_command(md), hint="/governor",
                    keywords="model cheap fast")
        if self.kernel is not None:
            for r in self.kernel.ps():
                if r["pid"] == self._pid1:
                    continue
                label = f"{r['pid']} · {r['name'][:50]}"
                add("Processes", f"Kill {label}", lambda p=r["pid"]: self.process_command("kill", str(p)),
                    hint=r["state"], keywords="stop end")
                add("Processes", f"Trace {label}", lambda p=r["pid"]: self.show_strace(str(p)),
                    hint="/strace", keywords="strace syscalls")
                if r["pid"] in self._bg:
                    add("Processes", f"Replay {label}", lambda p=r["pid"]: self.show_replay(str(p)),
                        hint="/replay")
                    add("Processes", f"Message {label}…", compose=f"/send {r['pid']} ",
                        hint="/send", keywords="tell steer")
        for m in sorted(getattr(self, "_missions", {}).values(), key=lambda x: -x.get("created", 0))[:8]:
            done, n = _missions.progress(m) if _missions else (0, 0)
            add("Missions", f"Mission: {m['goal'][:60]} ({m['state']}, {done}/{n})",
                lambda mm=m: self._mission_card(mm))
        for name, p in sorted(self.programs().items()):
            add("Programs", f"Run {name}…", compose=f"/run {name} ",
                hint=p["mode"], keywords=p.get("description", ""))
        add("Chat", "New chat", self.new_chat, keys="Ctrl+N")
        add("Chat", "Retry the last answer", self.retry_last, keys="Ctrl+R")
        add("Chat", "Switch Agent / Chat mode", self.toggle_mode, keys="Tab")
        add("Chat", "Plan mode on/off", lambda: self.set_plan_mode(not self.plan_mode),
            hint="/plan", keywords="approve")
        add("Chat", "Context: what the model is sent", self.show_context, hint="/context",
            keywords="memory tokens window ram")
        add("Chat", "Compact the conversation", self.compact_now, hint="/compact",
            keywords="summarise context")
        add("Chat", "Export this chat", self.export_chat, hint="/export", keywords="markdown save")
        add("Chat", "Cost of this chat", lambda: self._slash("/cost"), hint="/cost")
        add("Code", "Show the diff", self.show_git_diff, hint="/diff", keywords="git changes")
        add("Code", "Commit", lambda: self._slash("/commit"), hint="/commit", keywords="git")
        add("Code", "Open a pull request", lambda: self._slash("/pr"), hint="/pr",
            keywords="git github branch push gh")
        add("Code", "Review the uncommitted changes", lambda: self._slash("/review"), hint="/review",
            keywords="bugs")
        add("Code", "Rewind files and chat", self.show_rewind, hint="/rewind", keywords="undo checkpoint")
        add("Code", "Write CHUCK.md for this project", lambda: self._slash("/init"), hint="/init",
            keywords="rules instructions")
        add("Code", "Doctor: check the setup", self.run_doctor, hint="/doctor")
        add("Code", ("Stop watching for AI! comments" if self._watch else
                     "Watch for AI! comments from your editor"),
            lambda: self.watch_command(""), hint="/watch", keywords="editor vscode vim comment")
        add("Folder", "Choose a working folder…", self.open_workspace, keys="Ctrl+O")
        for path in (self.settings.get("recent_workspaces") or [])[:8]:
            if path and os.path.isdir(path):
                add("Folder", f"Open {_short_path(path)}",
                    lambda p=path: self.set_workspace(p), keywords=path)
        for mode, label in (("full", "full access"), ("sandbox-net", "sandboxed"),
                            ("sandbox", "sandboxed, offline"), ("readonly", "read-only")):
            add("Folder", f"Folder access: {label}", lambda md=mode: self.set_access(md),
                hint="/access", keywords="safety bubblewrap")
        cur_model = (self.settings.get("model") or DEFAULT_MODEL)
        for mid in _pricing.SUGGESTED + [mm for mm in (self.settings.get("models") or [])
                                         if mm not in _pricing.SUGGESTED][:8]:
            add("Models", f"Use {mid.split('/')[-1]}" + (" ✓" if mid == cur_model else ""),
                lambda x=mid: self.set_model(x), keywords=mid)
        add("Voice", "Talk to Chuck", self.toggle_listen, keys="Ctrl+M", keywords="mic speech")
        add("Voice", "Read replies aloud on/off", lambda: self.tts_btn.set_active(not self.tts_btn.get_active()),
            keywords="tts speak")
        add("Voice", "Install the natural voice and voice input", lambda: self.offer_voice_install("both"),
            hint="/voice")
        try:
            for f in chat_files()[:12]:
                try:
                    title = (json.loads(f.read_text()).get("title") or "chat").strip().replace("\n", " ")
                except Exception:
                    continue
                add("Chats", f"Open chat: {title[:70]}", lambda p=f: self._load_chat(p))
        except Exception:
            pass
        add("Settings", "Settings", self.open_settings, keys="Ctrl+,")
        add("Settings", "Memory & skills", self.open_memory)
        add("Settings", "Update Chuck", self.run_update, hint="/update")
        add("Settings", "Toggle the chats sidebar", lambda: self.side_btn.set_active(not self.side_btn.get_active()),
            keys="Ctrl+B")
        seen = {it["hint"].split()[0] for it in items if it["hint"].startswith("/")}
        for c, d in self.SLASH_HELP + [(f"/{n}", dsc or "your command")
                                       for n, (_p, dsc) in sorted(self.custom_commands().items())]:
            first = c.split()[0]
            if first not in seen and first.startswith("/"):
                takes = len(c.split()) > 1
                add("Commands", f"{c}  — {d}"[:110], (lambda cc=first: self._slash(cc)) if not takes else None,
                    compose=(first + " ") if takes else None)
        return items

    def palette_run(self, item):
        """Run one palette item (the window is already closed)."""
        if item.get("compose"):
            self.entry.get_buffer().set_text(item["compose"])
            try:
                self.entry.grab_focus()
                buf = self.entry.get_buffer()
                buf.place_cursor(buf.get_end_iter())
            except Exception:
                pass
            return True
        if item.get("fn"):
            item["fn"]()
            return True
        return False

    def open_palette(self, *_a):
        """Ctrl+P: search every action, arrows to move, Enter to run."""
        if _palette is None:
            return False
        if getattr(self, "_palette_win", None) is not None:
            try:
                self._palette_win.present()
                return True
            except Exception:
                pass
        items = self.palette_items()
        win = Gtk.Window(transient_for=self, modal=True, decorated=False)
        win.add_css_class("palette")
        try:
            width = min(720, max(420, int(self.get_width()) - 80))
        except (TypeError, ValueError):
            width = 640
        win.set_default_size(width, 460)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("palette-box")
        search = Gtk.SearchEntry(hexpand=True)
        search.set_placeholder_text("Type a command, an agent, a chat, a model…  (Esc closes)")
        search.add_css_class("palette-search")
        box.append(search)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        lb = Gtk.ListBox()
        lb.add_css_class("palette-list")
        lb.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sc.set_child(lb)
        box.append(sc)
        foot = Gtk.Label(label="↑↓ move · Enter run · Esc close", xalign=0)
        foot.add_css_class("dim"); foot.add_css_class("palette-foot")
        box.append(foot)
        win.set_child(box)
        state = {"rows": []}

        def fill(*_x):
            c = lb.get_first_child()
            while c is not None:
                n = c.get_next_sibling(); lb.remove(c); c = n
            q = search.get_text() if hasattr(search, "get_text") else ""
            shown = _palette.rank(items, q)
            state["rows"] = []
            last_group = None
            for it in shown:
                if not q and it["group"] != last_group:
                    last_group = it["group"]
                    hr = Gtk.ListBoxRow(); hr.set_selectable(False); hr.set_activatable(False)
                    hl = Gtk.Label(label=it["group"].upper(), xalign=0)
                    hl.add_css_class("palette-group")
                    hr.set_child(hl)
                    lb.append(hr)
                row = Gtk.ListBoxRow()
                rb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                t = Gtk.Label(label=it["title"], xalign=0, hexpand=True)
                if Pango is not None:
                    t.set_ellipsize(Pango.EllipsizeMode.END)
                rb.append(t)
                side = it["keys"] or it["hint"] or (it["group"] if q else "")
                if side:
                    s_ = Gtk.Label(label=side, xalign=1)
                    s_.add_css_class("dim"); s_.add_css_class("palette-hint")
                    rb.append(s_)
                row.set_child(rb)
                row._item = it
                lb.append(row)
                state["rows"].append(row)
            if state["rows"]:
                lb.select_row(state["rows"][0])

        def close(*_x):
            self._palette_win = None
            try:
                win.destroy()
            except Exception:
                pass

        def run_row(row):
            it = getattr(row, "_item", None)
            close()
            if it is not None:
                GLib.idle_add(lambda: (self.palette_run(it), False)[1])

        def move(d):
            rows = state["rows"]
            if not rows:
                return
            cur = lb.get_selected_row()
            i = rows.index(cur) if cur in rows else -1
            nxt = rows[max(0, min(len(rows) - 1, i + d))]
            lb.select_row(nxt)
            try:
                nxt.grab_focus(); search.grab_focus()
            except Exception:
                pass

        def on_key(_c, keyval, _code, _state):
            if keyval == Gdk.KEY_Escape:
                close(); return True
            if keyval in (Gdk.KEY_Down, getattr(Gdk, "KEY_KP_Down", -1)):
                move(1); return True
            if keyval in (Gdk.KEY_Up, getattr(Gdk, "KEY_KP_Up", -1)):
                move(-1); return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                enter()
                return True
            return False
        kc = Gtk.EventControllerKey()
        try:
            # before the search entry: it would eat Enter (activate) and Esc
            kc.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        except Exception:
            pass
        kc.connect("key-pressed", on_key)
        win.add_controller(kc)

        def enter(*_x):
            row = lb.get_selected_row()
            if getattr(row, "_item", None) is None:          # nothing selected: the best match
                row = state["rows"][0] if state["rows"] else None
            if row is not None:
                run_row(row)
        try:
            search.connect("activate", enter)
            search.connect("stop-search", close)
        except Exception:
            pass
        search.connect("search-changed", fill)
        lb.connect("row-activated", lambda _l, row: run_row(row))
        win.connect("close-request", lambda *_x: (setattr(self, "_palette_win", None), False)[1])
        fill()
        self._palette_win = win
        self._palette_state = {"items": items, "fill": fill, "search": search, "run": run_row,
                               "rows": state, "close": close}
        win.present()
        try:
            search.grab_focus()
        except Exception:
            pass
        return True

    # ── /watch: talk to Chuck from your own editor (AI! / AI? comments) ──
    def watch_command(self, arg=""):
        """/watch [on|off]: act on AI! comments (and answer AI? ones) as you
        save files in the working folder, from whatever editor you use."""
        if _aiwatch is None:
            self._sys_note("The aiwatch module is missing — reinstall Chuck.", "danger")
            return False
        want = (arg or "").strip().lower()
        on = (self._watch is None) if want not in ("on", "off") else want == "on"
        if not on:
            if self._watch is not None:
                self._watch = None
                self._sys_note("\U0001F441 stopped watching for AI! comments.", "dim")
            return True
        root = self._workroot()
        if root is None:
            self._sys_note("Pick a working folder first (/folder): /watch watches it.", "danger")
            return False
        if self._watch is not None and self._watch["root"] == str(root):
            return True
        w = {"root": str(root), "snap": None, "seen": set(), "busy": True}
        self._watch = w

        def first():
            w["snap"] = _aiwatch.snapshot(w["root"])      # what's there now is the baseline
            w["busy"] = False
        threading.Thread(target=first, daemon=True, name="watch-init").start()
        GLib.timeout_add(1500, self._watch_tick, w)
        if not self.agent_mode:
            self.set_agent_mode(True, announce=False)
        self._sys_note(f"\U0001F441 watching {root.name}: write a comment ending in AI! in any file, "
                       "in any editor, and save; AI? asks a question. /watch off stops.", "ok")
        return True

    def _watch_tick(self, w):
        if self._watch is not w or getattr(self, "_closing", False):
            return False                              # turned off, or another folder
        if w["busy"] or w["snap"] is None:
            return True
        if w.get("pending") and not self._running and not self._get_entry().strip():
            self._watch_found(w, w["snap"], [], [])  # the draft is gone: send what waited
            return True
        w["busy"] = True

        def scan():
            try:
                snap, found, changed = _aiwatch.scan(w["root"], w["snap"])
            except Exception:
                snap, found, changed = w["snap"], [], []
            GLib.idle_add(self._watch_found, w, snap, found, changed)
        threading.Thread(target=scan, daemon=True, name="watch-scan").start()
        return True

    def _watch_found(self, w, snap, found, changed=()):
        w["snap"], w["busy"] = snap, False
        if self._watch is not w:
            return False
        # each comment is acted on once: keyed by file, kind, text and the code
        # around it; a comment that's gone from a changed file is forgotten,
        # so writing it again later works again
        keys = {(f[0], f[2], f[3], f[4]) for f in found}
        gone = set(changed)
        w["seen"] = {k for k in w["seen"] if k[0] not in gone or k in keys}
        new = [f for f in found if (f[0], f[2], f[3], f[4]) not in w["seen"]]
        w["seen"] |= {(f[0], f[2], f[3], f[4]) for f in new}
        new = w.pop("pending", []) + new
        if not new:
            return False
        if not self._running and (self._get_entry().strip() or getattr(self, "_attachments", None)):
            w["pending"] = new                        # you're typing: never overwrite a draft
            if not w.get("told"):
                w["told"] = True
                self._sys_note("\U0001F441 an AI comment is waiting: it's sent once your draft is "
                               "sent or cleared.", "dim")
            return False
        w["told"] = False
        text = _aiwatch.prompt_for(new)
        self._sys_note(f"\U0001F441 {len(new)} AI comment(s) in "
                       + ", ".join(sorted({f'{f[0]}:{f[1]}' for f in new}))[:200], "dim")
        if self._running:
            self._steer.append(text)                  # heard at its next step
        else:
            if not self.agent_mode:
                self.set_agent_mode(True, announce=False)
            self.entry.get_buffer().set_text(text)
            self.on_send()
        return False

    # ── /context: the context window's memory map (`free` for the model's RAM) ──
    def context_map(self):
        """What the next model call would send, by what takes the room ->
        (rows [(label, chars)], total chars, budget chars, paged-out chars)."""
        sent = self._trim_for_send(list(self.history), dry=True) if self.history else []
        rows = {}
        paged_saved = 0

        def add(label, n):
            rows[label] = rows.get(label, 0) + n
        for i, m in enumerate(sent):
            c = m.get("content")
            text = c if isinstance(c, str) else " ".join(
                x.get("text", "") for x in (c or []) if isinstance(x, dict))
            n = len(text) if isinstance(c, str) else 2000   # as the send budget counts it
            if i == 0 and m.get("role") == "system":
                base = len(SYSTEM_PROMPT if self.agent_mode else CHAT_PROMPT)
                add("system prompt", min(n, base))
                rest = text[base:]
                mcp = re.search(r"\n\nMCP TOOLS.*?(?=\n\n[A-Z]|\Z)", rest, re.DOTALL)
                osl = re.search(r"\n\nAGENT OS:.*?(?=\n\n|\Z)", rest, re.DOTALL)
                if mcp:
                    add("MCP tools", len(mcp.group(0)))
                if osl:
                    add("agent OS & programs", len(osl.group(0)))
                add("machine, folder & project rules",
                    max(0, len(rest) - (len(mcp.group(0)) if mcp else 0) - (len(osl.group(0)) if osl else 0)))
            elif text.startswith("CONVERSATION SUMMARY"):
                add("summary of earlier turns", n)
            elif text.startswith("[PAGED OUT"):
                add("page table (paged-out output)", n)
                mm = re.search(r"([\d,]+) chars of earlier", text)
                if mm:
                    paged_saved += int(mm.group(1).replace(",", ""))
            elif m.get("role") == "user" and text.startswith(self._PAGEABLE + ("I ran ",)):
                add("tool results", n)
            elif m.get("role") == "user" and text.startswith(self._INTERNAL):
                add("Chuck's notes to the model", n)
            elif m.get("role") == "user":
                add("your messages", n)
            else:
                add("replies", n)
        total = sum(rows.values())
        budget = self.cfg("context_chars", SEND_CHAR_BUDGET, 40000, 800000)
        return sorted(rows.items(), key=lambda r: -r[1]), total, budget, paged_saved

    def show_context(self):
        rows, total, budget, paged = self.context_map()

        def text():
            return "\n".join(f"{lbl:<34} {n:>9,} chars  ~{n // 4:,} tokens" for lbl, n in rows)
        card, head, body, foot = self._card_shell("CONTEXT", text)
        pct = min(100, round(100 * total / max(1, budget)))
        top = Gtk.Label(xalign=0, wrap=True)
        top.set_text(f"The next model call sends {total:,} characters (~{total // 4:,} tokens): "
                     f"{pct}% of the {budget:,}-character context budget."
                     + (f" {paged:,} characters of old tool output are paged out (```page``` brings "
                        "any back exactly)." if paged else ""))
        body.append(top)
        block = "█"
        width = 30
        lines = []
        for lbl, n in rows:
            bar = block * max(1 if n else 0, round(width * n / max(1, total)))
            lines.append(f"{lbl[:32]:<32} {bar:<{width}} {n // 4:>8,} tk")
        bl = Gtk.Label(label="\n".join(lines) or "(nothing yet)", xalign=0, selectable=True)
        bl.add_css_class("cmd-text")
        body.append(bl)
        tip = Gtk.Label(xalign=0, wrap=True)
        tip.add_css_class("dim"); _wrap_anywhere(tip)
        tip.set_text("Every step of an agent re-sends all of this. /compact summarises old turns; "
                     "the context budget is in Settings › Agent.")
        body.append(tip)
        self._log_pin(card)
        return True

    # ── the shell: drive the running OS from any terminal ──
    REMOTE_HELP = """chucknorris — talk to the running Chuck from a terminal

  chucknorris "fix the failing tests"     send a message (as if typed)
  chucknorris --here "…"                  first make this directory the working folder
  chucknorris --mission "goal"            plan a mission (it waits for Start)
  chucknorris --spawn "task"              a background agent in a copy of the folder
  chucknorris --run "reviewer the parser" run an agent program
  chucknorris --remind "in 20m: tea"      a reminder
  chucknorris --daemon "daily 08:30: …"   a daemon
  chucknorris --top                       print the agent processes here
  chucknorris --budget                    print today's and this month's spend
  chucknorris --inbox                     print unread daemon reports
"""

    def remote_command(self, args, cwd=None, out=print):
        """A command from another `chucknorris` launch (Gio forwards its
        arguments here). `out` prints back to that terminal. -> exit code."""
        args = [a for a in args if a]
        if not args:
            return 0
        if args[0] in ("-h", "--help"):
            out(self.REMOTE_HELP)
            return 0
        if args[0] == "--here":
            if cwd and os.path.isdir(cwd):
                self.set_workspace(cwd, announce=True)
            args = args[1:]
            if not args:
                return 0
        flag, rest = args[0], " ".join(args[1:]).strip()
        if flag == "--top" or flag == "--ps":
            out(self._top_text() or "(no processes)")
            return 0
        if flag == "--budget":
            if self._ledger is None:
                out("budget module missing"); return 1
            out(f"today {_pricing.fmt_usd(self._ledger.today())}, this month "
                f"{_pricing.fmt_usd(self._ledger.month())}")
            return 0
        if flag == "--inbox":
            items = self._inbox.unread() if self._inbox is not None else []
            out("\n\n".join(f"[{i['daemon']}] {i['why']}\n{i['report']}" for i in items) or "(no unread reports)")
            return 0
        actions = {"--mission": lambda: self.start_mission(rest),
                   "--spawn": lambda: self.spawn_background(rest),
                   "--run": lambda: self.run_program(rest),
                   "--remind": lambda: self.remind_command(rest),
                   "--daemon": lambda: self.daemon_command(rest)}
        if flag in actions:
            if not rest:
                out(f"{flag} needs an argument (chucknorris --help)")
                return 2
            ok = actions[flag]()
            out("done" if ok not in (None, False) else "not done: see the note in Chuck's window")
            return 0 if ok not in (None, False) else 1
        if flag.startswith("--"):
            out(f"unknown option {flag} (chucknorris --help)")
            return 2
        text = " ".join(args)
        if self._running:
            self._steer.append(text)
            out("sent: Chuck is working, it hears this at its next step")
        else:
            self.entry.get_buffer().set_text(text)
            self.on_send()
            out("sent")
        return 0

    # ── the agent runs the OS: the user never needs a command ──
    OS_COMMANDS = ("top", "kill <pid>", "stop <pid>", "cont <pid>", "nice <pid> <n>", "missions",
                   "mission start|pause|resume|cancel <id>", "daemons",
                   "daemon run|on|off|rm <id>", "reminders", "inbox", "board",
                   "budget [day|month <$>]", "governor [performance|balanced|powersave]",
                   "watch on|off", "activity", "context", "programs", "strace <pid>",
                   "replay <pid>", "discard <pid>")

    def os_text(self, spec):
        """Run one OS command for the agent -> what it says, as text (the
        user's slash commands, made readable for the model)."""
        words = (spec or "").strip().lstrip("/").split(None, 1)
        if not words:
            return "commands: " + " · ".join(self.OS_COMMANDS)
        cmd, arg = words[0].lower(), (words[1].strip() if len(words) > 1 else "")
        K_ = self.kernel
        if cmd in ("top", "ps", "processes"):
            return self._top_text() or "(no processes besides this chat)"
        if cmd in ("kill", "stop", "cont", "nice"):
            ok = self.process_command(cmd, arg)
            p = K_.procs.get(int(arg.split()[0])) if ok and K_ and arg.split()[0].isdigit() else None
            return f"pid {arg.split()[0] if arg else '?'}: {p.state if p else 'not done (no such process, or pid 1)'}"
        if cmd == "missions":
            if not self._missions:
                return "(no missions)"
            out = []
            for m in sorted(self._missions.values(), key=lambda x: -x.get("created", 0))[:10]:
                done, n = _missions.progress(m)
                out.append(f"{m['id'][-5:]} [{m['state']}] {done}/{n} {m['goal'][:80]}"
                           + "".join(f"\n   {t['id']} {t['state']}: {t['title'][:60]}" for t in m["tasks"]))
            return "\n".join(out)
        if cmd == "mission":
            parts = arg.split()
            if len(parts) < 2 or parts[0] not in ("start", "pause", "resume", "cancel"):
                return "write it as: mission start|pause|resume|cancel <id> (see: missions)"
            m = next((x for x in self._missions.values() if x["id"].endswith(parts[1])), None)
            if m is None:
                return f"no mission {parts[1]}"
            ok = self.mission_action(m["id"], parts[0])
            return f"mission {m['id'][-5:]}: {m['state']}" + ("" if ok else " (nothing to do)")
        if cmd == "daemons" or cmd == "reminders":
            items = [d for d in (self._daemons.items if self._daemons else [])
                     if (d.get("kind") == "remind") == (cmd == "reminders")]
            return "\n".join(f"{d['id']} [{'on' if d.get('enabled') else 'paused'}] "
                             f"{_daemons.describe(d['when'])}: {d['brief'][:80]}" for d in items) or f"(no {cmd})"
        if cmd == "daemon":
            self.daemon_command(arg)
            return self.os_text("daemons")
        if cmd == "inbox":
            items = self._inbox.unread() if self._inbox is not None else []
            if self._inbox is not None:
                self._inbox.mark_read()
            return "\n\n".join(f"[{i['daemon']}] {i['why']}\n{i['report'][:1500]}" for i in items) or "(nothing unread)"
        if cmd == "board":
            return self._board_text() or "(the board is empty)"
        if cmd == "budget":
            m = re.match(r"(day|daily|month|monthly)\s+\$?([\d.]+)$", arg)
            if m:
                key = "budget_day_usd" if m.group(1).startswith("da") else "budget_month_usd"
                new, cur = float(m.group(2)), self.cfg(key, 0.0, 0.0, 1000000.0, cast=float)
                if new == 0 or (cur and new > cur):
                    # a looser limit is the user's call, never the agent's
                    self._confirm_card("BUDGET", f"Chuck wants to set your {m.group(1)} budget to "
                                       f"{'no limit' if new == 0 else _pricing.fmt_usd(new)} (now "
                                       f"{_pricing.fmt_usd(cur) if cur else 'no limit'}).",
                                       "Allow", lambda: self.budget_command(arg))
                    return "asked the user to confirm (raising a limit is theirs to approve)"
                self.budget_command(arg)
            L = self._ledger
            if L is None:
                return "(no budget module)"
            return (f"today {_pricing.fmt_usd(L.today())} (limit "
                    f"{_pricing.fmt_usd(self.cfg('budget_day_usd', 0.0, 0.0, 1e5, cast=float)) or 'none'}), "
                    f"this month {_pricing.fmt_usd(L.month())} (limit "
                    f"{_pricing.fmt_usd(self.cfg('budget_month_usd', 0.0, 0.0, 1e6, cast=float)) or 'none'})")
        if cmd == "governor":
            if arg:
                self.governor_command(arg)
            if _governor is None:
                return "(no governor module)"
            chat = (self.settings.get("model") or DEFAULT_MODEL).strip()
            return f"mode {_governor.mode_of(self.settings)}: " + "; ".join(
                f"{r} -> {mm}" for r, mm in _governor.table(self.settings, chat, _pricing.PRICES, _pricing.SUGGESTED))
        if cmd == "watch":
            self.watch_command(arg or "on")
            return "watching for AI! comments" if self._watch else "not watching"
        if cmd == "activity":
            a = self._activity()
            return (f"{len(a['asks'])} question(s) waiting, {len(a['agents'])} agent(s) working, "
                    f"{len(a['missions'])} mission(s), {a['unread']} unread report(s)")
        if cmd == "context":
            rows, total, budget, paged = self.context_map()
            return (f"{total:,} of {budget:,} chars; " + ", ".join(f"{lb} {n:,}" for lb, n in rows)
                    + (f"; {paged:,} paged out" if paged else ""))
        if cmd == "programs":
            return "\n".join(f"{n} ({p['mode']}): {p['description']}" for n, p in sorted(self.programs().items()))
        if cmd == "strace" and arg.split() and arg.split()[0].isdigit():
            calls = K_.strace(int(arg.split()[0])) if K_ else []
            return "\n".join(f"{tool} {a} {ms:.0f}ms {size}b" + (" DENIED" if den else "")
                             for _t, tool, a, ms, size, den in calls[-40:]) or "(no syscalls)"
        if cmd == "replay" and arg.split() and arg.split()[0].isdigit():
            e = self._bg.get(int(arg.split()[0]))
            if not e or not e.get("msgs"):
                return "(no recorded run)"
            return "\n".join(f"step {n}: {_hibernate.tool_labels(a) or a[:80]}"
                             for n, a, _r in _hibernate.steps_of(e["msgs"]))
        if cmd == "discard":
            return "discarded" if self.discard_background(arg) else "not discarded"
        return f"unknown: {cmd}. commands: " + " · ".join(self.OS_COMMANDS)

    def _confirm_card(self, kind, text, yes_label, on_yes):
        """A one-click question for something only the user may decide."""
        card, head, body, foot = self._card_shell(kind)
        card.add_css_class("ask-card")
        lb = Gtk.Label(label=text, xalign=0, wrap=True); _wrap_anywhere(lb)
        body.append(lb)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        yes = Gtk.Button(label=yes_label); yes.add_css_class("quick"); yes.add_css_class("accent-btn")
        no = Gtk.Button(label="No"); no.add_css_class("ghost")

        def pick(ok):
            yes.set_sensitive(False); no.set_sensitive(False)
            if ok:
                on_yes()
        yes.connect("clicked", lambda *_a: pick(True))
        no.connect("clicked", lambda *_a: pick(False))
        row.append(yes); row.append(no)
        foot.append(row)
        self._log_pin(card)
        self._pending_confirm = (pick, card)
        return card

    @tool("agent", agent_only=False, cap=1)
    def _t_agent(self, body):
        """```agent <request>``` (Chat mode) — this needs to act on the
        machine: one click hands it to Agent mode."""
        req = (body or "").strip()
        if self.agent_mode or not req:
            return "[agent: already in Agent mode \u2014 just do it]" if req else "[agent: what should be done?]"

        def go():
            self.set_agent_mode(True, announce=False)
            self._prefill(req)
            self.on_send()
        self._confirm_card("AGENT MODE", f"This needs Chuck to act on your machine: \u201c{req[:300]}\u201d. "
                           "Agent mode runs commands and edits files (risky ones still ask you).",
                           "Do it in Agent mode", go)
        return "[a button lets the user hand this to Agent mode; don't repeat the request]"

    @tool("chuck", cap=6)
    def _t_chuck(self, body):
        """```chuck <command>``` — the agent drives the OS (what /commands do)
        and reads the result."""
        return f"[chuck {(body or '').strip()[:60]}]\n" + clip_output(self.os_text(body), 8000)

    # ── the system tray: the OS at a glance ──
    def _activity(self):
        """What the OS is doing right now, for the tray and its card."""
        kern = self.kernel
        procs = [r for r in kern.ps() if r["pid"] != self._pid1 and r["kind"] != "mission"] if kern else []
        missions = [m for m in getattr(self, "_missions", {}).values()
                    if m["state"] in ("running", "paused", "awaiting approval", "planning")]
        unread = len(self._inbox.unread()) if getattr(self, "_inbox", None) is not None else 0
        return {"agents": procs, "asks": dict(getattr(self, "_asks", {})), "missions": missions,
                "unread": unread}

    def _tray_tick(self):
        if getattr(self, "_closing", False):
            return False
        try:
            a = self._activity()
            parts, tips = [], []
            if a["asks"]:
                parts.append(f"? {len(a['asks'])}")
                tips.append(f"{len(a['asks'])} agent(s) waiting for your answer")
            if a["agents"]:
                parts.append(f"\u25b6 {len(a['agents'])}")
                tips.append(f"{len(a['agents'])} agent(s) working")
            if a["missions"]:
                parts.append(f"\u25ce {len(a['missions'])}")
                tips.append(f"{len(a['missions'])} mission(s)")
            if getattr(self, "_watch", None) is not None:
                parts.append("\U0001F441")
                tips.append("watching for AI! comments (/watch off)")
            if a["unread"]:
                parts.append(f"\u2709 {a['unread']}")
                tips.append(f"{a['unread']} daemon report(s) unread")
            text = "  ".join(parts)
            if text != getattr(self, "_tray_last", None):
                self._tray_last = text
                self._tray_lbl.set_label(text)
                self.tray_btn.set_visible(bool(text))
                self.tray_btn.set_tooltip_text(" \u00b7 ".join(tips) + " \u2014 click for details")
                if a["asks"]:
                    self.tray_btn.add_css_class("tray-ask")
                else:
                    self.tray_btn.remove_css_class("tray-ask")
        except Exception:
            pass
        return True

    def show_activity(self):
        """One card: everything waiting for you, and everything working."""
        a = self._activity()
        card, head, body, foot = self._card_shell("ACTIVITY")

        def section(title):
            lb = Gtk.Label(label=title, xalign=0); lb.add_css_class("slash-cmd")
            body.append(lb)

        def line(text, button=None, fn=None):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lb = Gtk.Label(label=text, xalign=0, hexpand=True, wrap=True)
            _wrap_anywhere(lb); row.append(lb)
            if button:
                b = Gtk.Button(label=button); b.add_css_class("quick")
                b.connect("clicked", lambda *_x: fn())
                row.append(b)
            body.append(row)
        if a["asks"]:
            section("WAITING FOR YOUR ANSWER")
            for pid, (_ev, box, q) in a["asks"].items():
                line(f"pid {pid}: {q[:200]}", "Answer", lambda p=pid, qq=q, bx=box: self._ask_card(p, qq, bx))
        if a["missions"]:
            section("MISSIONS")
            for m in a["missions"]:
                done, n = _missions.progress(m)
                line(f"{m['goal'][:90]} \u00b7 {m['state']} \u00b7 {done}/{n}", "Show",
                     lambda mm=m: self._mission_card(mm))
        if a["agents"]:
            section("WORKING")
            for r in a["agents"][:12]:
                line(f"{r['pid']} \u00b7 {r['kind']} \u00b7 {r['name'][:60]} \u00b7 {r['state']}"
                     + (f" \u00b7 {r['activity'][:60]}" if r.get("activity") else ""))
            line(f"{len(a['agents'])} running", "Open /top", self.show_top)
        if a["unread"]:
            section("REPORTS")
            line(f"{a['unread']} unread daemon report(s)", "Open inbox", self.show_inbox)
        if getattr(self, "_ledger", None) is not None:
            section("SPENT")
            lim = self.cfg("budget_day_usd", 0.0, 0.0, 100000.0, cast=float)
            line(f"today {_pricing.fmt_usd(self._ledger.today())}"
                 + (f" of {_pricing.fmt_usd(lim)}" if lim else "")
                 + f" \u00b7 this month {_pricing.fmt_usd(self._ledger.month())}", "Budget",
                 lambda: self.budget_command(""))
        if not (a["asks"] or a["missions"] or a["agents"] or a["unread"]):
            line("Nothing running and nothing waiting.")
        self._log_pin(card)
        return True

    # ── daemons (Chuck OS, phase 19) ──
    DAEMONS_FILE = CONFIG_DIR / "daemons.json"
    INBOX_FILE = CONFIG_DIR / "inbox.json"
    # "<when>: <what>", the colon outside any `backticks` and before a space
    _DAEMON_RE = re.compile(r"^((?:[^`:]|`[^`]*`|:(?!\s))+):\s+(.+)$", re.DOTALL)

    class _Probe:
        """What a daemon's trigger looks at: plain code, not a token spent."""

        @staticmethod
        def mtime(path):
            return _daemons.tree_signature(path)

        @staticmethod
        def failed_units():
            if not shutil.which("systemctl"):
                return []
            try:
                p = subprocess.run(["systemctl", "--failed", "--no-legend", "--plain"],
                                   capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                return []
            units = []
            for ln in p.stdout.splitlines():
                words = [w for w in ln.split() if w != "●"]
                if words:
                    units.append(words[0])
            return units

        @staticmethod
        def disk_percent(path):
            try:
                u = shutil.disk_usage(os.path.expanduser(path))
            except OSError:
                return None
            return 100.0 * u.used / u.total if u.total else None

        @staticmethod
        def run(cmd):
            return _run_in(Path.home(), cmd, 20, write=False)[1]

        localtime = staticmethod(time.localtime)

    def _daemon_tick(self):
        """Every 30 s: is any daemon due? Checked off the main thread, by
        plain code; an agent is woken only for one that fired. Whatever
        goes wrong in one tick, the timer lives on."""
        if self._daemons is None:
            return False
        try:
            return self._daemon_tick_once()
        except Exception as ex:
            self._daemon_checking = False
            if _crashlog is not None:
                _crashlog.record("daemon tick", str(ex))
            return True

    def _daemon_tick_once(self):
        if self._daemon_checking or self.settings.get("daemons_paused"):
            return True
        broke = bool(self._over_budget())       # over budget: only reminders (they cost nothing)
        specs = [dict(d) for d in self._daemons.items
                 if d.get("enabled") and d.get("id") not in self._daemon_live
                 and (not broke or d.get("kind") == "remind")]
        if not specs:
            return True
        self._daemon_checking = True
        probe = self._daemon_probe

        def run():
            now, out = time.time(), []
            for d in specs:
                try:
                    due, why, st = _daemons.check(d, now, probe)
                except Exception as ex:
                    due, why, st = False, f"check failed: {ex}", d.get("state") or {}
                out.append((d["id"], due, why, st))
            GLib.idle_add(self._daemon_checked, out, now)
        threading.Thread(target=run, daemon=True, name="daemon-check").start()
        return True

    def _daemon_checked(self, results, now):
        self._daemon_checking = False
        changed = False
        for did, due, why, st in results:
            d = self._daemons.get(did)
            if d is None or not d.get("enabled"):
                continue
            if st != d.get("state"):
                d["state"], changed = st, True
            if due:
                self._wake_daemon(d, why, now)
        if changed:
            self._daemons.save()
        return False

    def _daemon_syscalls(self, root, net=True):
        """A daemon's syscall table: it looks, it never changes anything.
        Its commands run with the whole system read-only (its folder too);
        the system bus is there for `systemctl status`, but nothing that
        changes a service or the power gets through."""
        calls = self._worker_syscalls(root, net=net)
        calls.pop("write", None)
        calls.pop("edit", None)

        def bash(cmd):
            cmd = (cmd or "").strip()
            why = _daemons.read_only_problem(cmd, strict=not shutil.which("bwrap"))
            if not why and command_needs_sudo(cmd):
                why = "it needs root"
            if not why and classify_command(cmd):
                why = "it can change or destroy things"
            if why:
                return (f"[NOT run: {why}. A daemon only looks — put the command in your "
                        "report as a proposal.]")
            rc, out = _run_in(root, cmd, 120, write=False, net=net)
            return f"exit {rc}\n{clip_output(out, 12000)}"
        calls["bash"] = bash
        return calls

    def _wake_daemon(self, d, why, now=None):
        """A daemon fired: wake an agent for it, as a kernel process with the
        daemon's quota. -> its pid, or None."""
        if d["id"] in self._daemon_live:
            return None
        d["last_run"] = now or time.time()
        d["runs"] = d.get("runs", 0) + 1
        self._daemons.save()
        if d.get("kind") == "remind":
            self._remind(d)
            return None
        detail = _daemons.trigger_detail(d)
        brief = d["brief"] + f"\n\n(You were woken because: {why}.)" + (f"\n{detail}" if detail else "")
        if d.get("kind") == "act":
            pid = self.spawn_background(brief, root=Path(d["folder"]), daemon=d, why=why)
            if pid is None:
                self._inbox.add(d["name"], why, "(couldn't start: see the note in the chat)",
                                extra={"daemon_id": d["id"], "status": 1})
            else:
                self._daemon_live[d["id"]] = pid
            return pid
        if self.kernel is None or _worker is None:
            return None
        root = Path(os.path.expanduser(d["folder"])) if d.get("folder") else Path.home()
        if not root.is_dir():
            root = Path.home()
        quota = {"tokens": int(d.get("quota") or 150000),
                 "seconds": self.cfg("daemon_minutes", 15, 1, 240) * 60}
        proc = self.kernel.spawn(f"daemon: {d['name']}"[:60], kind="daemon", brief=brief,
                                 parent=self._pid1, quota=quota, nice=5,
                                 caps={"read", "exec", "net"})
        pid, kern, did = proc.pid, self.kernel, d["id"]
        self._daemon_live[did] = pid
        call = self._sub_call(pid, independent=True, role="daemon")
        syscalls = self._guard(pid, {**self._daemon_syscalls(root), **self._os_calls(pid)})

        def run():
            report, status = "", 1
            try:
                report, _steps = _worker.run_worker(
                    brief, call, self._worker_parse, syscalls,
                    max_steps=self.cfg("daemon_steps", 20, 3, 100),
                    inbox=self._inbox_for(pid), status_line=self._status_for(pid),
                    on_event=lambda t: kern.note(pid, t),
                    should_stop=lambda: kern.should_stop(pid),
                    wrap_up=lambda: kern.terminating(pid) or bool(kern.over_quota(pid)),
                    context=f"{_now_line()}\nYou were woken because: {why}. "
                            f"Your working directory: {root}.",
                    prompt=_daemons.DAEMON_PROMPT)
                status = 1 if kern.should_stop(pid) else 0
            except Exception as ex:
                killed = _kernel is not None and isinstance(ex, _kernel.ProcessKilled)
                report = f"(stopped: {ex})" if killed else f"(failed: {ex})"
            kern.exit(pid, status, report)
            GLib.idle_add(self._daemon_done, did, why, report, pid, status)
        threading.Thread(target=run, daemon=True, name=f"daemon-{pid}").start()
        return pid

    @staticmethod
    def _quiet_report(report):
        """A daemon that found nothing wrong says so in its headline, and is
        filed without a notification: a daemon that cries wolf gets muted."""
        head = (report or "").strip().splitlines()[0].lower() if (report or "").strip() else ""
        return bool(re.match(r"\W*(?:nothing to do|all (?:good|clear|fine)|no (?:issues|problems|"
                             r"changes|action))", head))

    def _daemon_done(self, did, why, report, pid, status, bg_pid=None):
        self._daemon_live.pop(did, None)
        d = self._daemons.get(did) if self._daemons else None
        name = d["name"] if d else f"daemon {did}"
        quiet = status == 0 and self._quiet_report(report)
        extra = {"daemon_id": did, "status": status}
        if bg_pid is not None:
            extra["bg_pid"] = bg_pid
        it = self._inbox.add(name, why, report, pid, extra)
        if quiet:
            it["unread"] = False
            self._inbox.save()
            return False
        head = (report or "").strip().splitlines()[0][:160] if (report or "").strip() else "(no report)"
        self._sys_note(f"\U0001F4EC daemon · {name}: {head}  (/inbox)", "dim")
        try:
            if not self.is_active():
                n = Gio.Notification.new(f"Chuck daemon: {name}"[:80])
                n.set_body(head)
                self.get_application().send_notification(f"chuck-daemon-{it['id']}", n)
        except Exception:
            pass
        return False

    def _remind(self, d):
        """A reminder is due: tell the user (a note, the inbox, a desktop
        notification, aloud if replies are read aloud). No agent, no tokens.
        A one-time reminder is gone once it has fired."""
        text = d["brief"].strip()
        self._sys_note(f"\u23f0 Reminder: {text}", "ok")
        self._inbox.add("reminder", _daemons.describe(d["when"]), text, None,
                        {"daemon_id": d["id"], "status": 0})
        try:
            n = Gio.Notification.new("Reminder")
            n.set_body(text[:200])
            self.get_application().send_notification(f"chuck-remind-{d['id']}", n)
        except Exception:
            pass
        if self.settings.get("tts", True):
            try:
                speak(f"Reminder: {text}", self.settings)
            except Exception:
                pass
        if "at" in d["when"]:
            self._daemons.remove(d["id"])

    def remind_command(self, arg):
        """/remind <when>: <what> -> the reminder (or None)."""
        d, why = self.daemon_add(arg, "remind")
        if d is None:
            self._sys_note(f"\u2717 {why}" if arg.strip() else
                           "/remind in 20m: check the oven \u00b7 /remind at 5pm: call the bank \u00b7 "
                           "/remind every day at 8:30: stand-up", "danger" if arg.strip() else "dim")
            return None
        self._sys_note(f"\u23f0 I'll remind you {_daemons.describe(d['when'])}: {d['brief'][:120]}", "ok")
        return d

    @tool("remind", cap=3, agent_only=False)
    def _t_remind(self, body):
        """```remind <when>: <what>``` — a reminder for the user (no agent,
        no tokens): 'in 20m', 'at 17:30', 'tomorrow at 9am', 'every day at 8:30'."""
        d = self.remind_command(body or "")
        if d is None:
            return "[remind: not set (see the note above); write it as ```remind <when>: <what>```]"
        return f"[reminder set: {_daemons.describe(d['when'])}: {d['brief'][:120]}]"

    def daemon_add(self, spec, kind="report"):
        """'<when>: <what>' -> (the daemon, '') or (None, why not)."""
        if self._daemons is None:
            return None, "the daemons module is missing — reinstall Chuck"
        m = self._DAEMON_RE.match((spec or "").strip())
        if not m:
            return None, ("write it as  /daemon <when>: <what to do>  — e.g. /daemon daily 08:30: "
                          "summarise new errors in the journal")
        when, what = m.group(1).strip(), m.group(2).strip()
        try:
            trig = _daemons.parse_when(when)
        except _daemons.DaemonError as ex:
            return None, str(ex)
        folder = str(self.workspace) if self.workspace else ""
        if kind == "act" and not folder:
            return None, "an acting daemon works on a copy of a folder: pick one first (/folder)"
        if "watch" in trig and not os.path.isabs(trig["watch"]):
            if not folder:
                return None, "a relative path needs a working folder (/folder), or give a full path"
            trig["watch"] = str(Path(folder) / trig["watch"])
        if "disk" in trig and not os.path.exists(trig["disk"]):
            return None, f"{trig['disk']} doesn't exist"
        if "watch" in trig and not os.path.exists(trig["watch"]):
            return None, f"{trig['watch']} doesn't exist"
        if "command" in trig:
            why = (_daemons.read_only_problem(trig["command"], strict=not shutil.which("bwrap"))
                   or ("it can change things" if classify_command(trig["command"]) else ""))
            if why:
                return None, f"a trigger command may only look: {why}"
        d = self._daemons.add("", trig, what, folder, kind,
                              quota=self.cfg("daemon_tokens", 150000, 10000, 5000000))
        if "at" not in trig:
            d["last_run"] = time.time()         # a schedule starts from now, not at once
        self._daemons.save()
        return d, ""

    def daemon_command(self, arg):
        """/daemon <when>: <what> · /daemon act <when>: <what> ·
        /daemon run|on|off|rm <id>"""
        words = arg.split(None, 1)
        verb = words[0].lower() if words else ""
        if not words or verb == "list":
            return self.show_daemons()
        if verb in ("run", "on", "off", "rm", "delete") and len(words) == 2:
            d = self._daemons.get(words[1]) if self._daemons and words[1].strip().isdigit() else None
            if d is None:
                self._sys_note(f"No daemon {words[1].strip()} (/daemons lists them).", "danger")
                return False
            return self._daemon_action(d, verb)
        kind = "report"
        if verb == "act" and len(words) == 2:
            kind, arg = "act", words[1]
        d, why = self.daemon_add(arg, kind)
        if d is None:
            self._sys_note(f"✗ {why}", "danger" if arg else "dim")
            return False
        self._sys_note(f"⏰ daemon {d['id']} · {_daemons.describe(d['when'])}: {d['brief'][:120]}"
                       + (" — it works on a copy of the folder; you merge what it does"
                          if kind == "act" else " — it looks and reports to /inbox"), "ok")
        return True

    def _daemon_action(self, d, verb):
        if verb == "run":
            if d["id"] in self._daemon_live:
                self._sys_note(f"Daemon {d['id']} is already running (pid {self._daemon_live[d['id']]}).", "dim")
                return False
            return self._wake_daemon(d, "run by hand") is not None
        if verb in ("on", "off"):
            d["enabled"] = verb == "on"
            if verb == "on":
                d["state"] = {}                 # watch afresh: no stale edge fires at once
                d["last_run"] = time.time()     # nor a schedule missed while it was paused
            self._daemons.save()
            self._sys_note(f"Daemon {d['id']} {'resumed' if verb == 'on' else 'paused'}.", "dim")
            return True
        pid = self._daemon_live.get(d["id"])
        if pid is not None and self.kernel is not None:
            self.kernel.signal(pid, _kernel.KILL)
        self._daemons.remove(d["id"])
        self._sys_note(f"Daemon {d['id']} deleted.", "dim")
        return True

    def show_daemons(self):
        if self._daemons is None:
            self._sys_note("The daemons module is missing — reinstall Chuck.", "danger")
            return False
        items = self._daemons.items
        card, head, body, foot = self._card_shell(
            "DAEMONS", lambda: "\n".join(f"{d['id']}  {_daemons.describe(d['when'])}: {d['brief']}"
                                         for d in self._daemons.items))
        if not items:
            lb = Gtk.Label(label="No daemons yet. A daemon wakes on a schedule or an event, looks, "
                                 "and reports — its trigger costs nothing while it sleeps:\n"
                                 "/daemon daily 08:30: summarise new errors in the journal\n"
                                 "/daemon when a service fails: find out why and propose a fix\n"
                                 "/daemon when disk / is above 90%: find what's filling it\n"
                                 "/daemon when `checkupdates` changes: say what's worth updating\n"
                                 "/daemon act every day at 3am: update the dependencies and run "
                                 "the tests", xalign=0, wrap=True, selectable=True)
            lb.add_css_class("dim")
            body.append(lb)
        else:
            live = sum(1 for d in items if d.get("enabled"))
            sm = Gtk.Label(label=f"{live} awake of {len(items)} \u00b7 a sleeping daemon costs nothing: "
                                 "its trigger is checked by plain code every 30 s, and an agent "
                                 "is woken only when it fires", xalign=0, wrap=True)
            sm.add_css_class("dim"); _wrap_anywhere(sm)
            body.append(sm)
        for d in items:
            body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            last = (time.strftime("%d %b %H:%M", time.localtime(d["last_run"]))
                    if d.get("runs") else "never")
            t = Gtk.Label(label="", xalign=0, wrap=True)
            t.add_css_class("slash-cmd"); _wrap_anywhere(t)
            row.append(t)
            b = Gtk.Label(label=d["brief"][:300], xalign=0, wrap=True); _wrap_anywhere(b)
            row.append(b)
            meta = Gtk.Label(label=f"ran {d.get('runs', 0)}× · last {last}"
                                   + (f" · {d['folder']}" if d.get("folder") else ""),
                             xalign=0, wrap=True)
            meta.add_css_class("dim"); _wrap_anywhere(meta)
            row.append(meta)
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            run_b, pause_b, del_b = (Gtk.Button(label=x) for x in ("Run now", "Pause", "Delete"))

            def paint(dd=d, t=t, run_b=run_b, pause_b=pause_b, del_b=del_b):
                gone = self._daemons.get(dd["id"]) is None
                state = ("deleted" if gone else "running" if dd["id"] in self._daemon_live
                         else "on" if dd.get("enabled") else "paused")
                t.set_text(f"{dd['id']} \u00b7 {_daemons.describe(dd['when'])} \u00b7 "
                           f"{ {'act': 'acts in a copy', 'remind': 'reminds you'}.get(dd.get('kind'), 'reports')} "
                           f"\u00b7 {state}")
                pause_b.set_label("Pause" if dd.get("enabled") else "Resume")
                for b in (run_b, pause_b, del_b):
                    b.set_sensitive(not gone)

            def act(which, dd=d, paint=paint):
                verb = ("off" if dd.get("enabled") else "on") if which == "pause" else which
                self._daemon_action(dd, verb)
                paint()
            for b, which in ((run_b, "run"), (pause_b, "pause"), (del_b, "rm")):
                b.add_css_class("quick")
                b.connect("clicked", lambda _b, w_=which, a=act: a(w_))
                btns.append(b)
            paint()
            row.append(btns)
            body.append(row)
        self._log_pin(card)
        return True

    def show_inbox(self):
        if self._inbox is None:
            self._sys_note("The daemons module is missing — reinstall Chuck.", "danger")
            return False
        items = list(reversed(self._inbox.items[-20:]))
        card, head, body, foot = self._card_shell(
            "INBOX", lambda: "\n\n".join(f"{i['daemon']} ({i['why']}):\n{i['report']}" for i in items))
        if not items:
            lb = Gtk.Label(label="Nothing yet: your daemons' reports land here (/daemons).",
                           xalign=0, wrap=True)
            lb.add_css_class("dim")
            body.append(lb)
        for n, it in enumerate(items):
            if n:
                body.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            when = time.strftime("%d %b %H:%M", time.localtime(it.get("time") or 0))
            t = Gtk.Label(label=("● " if it.get("unread") else "") + f"{when} · "
                          f"{it['daemon']} · {it['why']}", xalign=0, wrap=True)
            t.add_css_class("slash-cmd"); _wrap_anywhere(t)
            row.append(t)
            rep = Gtk.Label(label=(it.get("report") or "")[:6000], xalign=0, wrap=True,
                            selectable=True)
            _wrap_anywhere(rep)
            row.append(rep)
            btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if it.get("bg_pid") is not None and it["bg_pid"] in self._bg:
                bt = Gtk.Button(label="Show diff"); bt.add_css_class("quick")
                bt.connect("clicked", lambda _b, p=it["bg_pid"]: self.show_bg_diff(p))
                btns.append(bt)
            bt = Gtk.Button(label="Act on it"); bt.add_css_class("quick")
            bt.add_css_class("accent-btn")
            bt.connect("clicked", lambda _b, i=it: self.act_on_report(i))
            btns.append(bt)
            row.append(btns)
            body.append(row)
        self._inbox.mark_read()
        self._log_pin(card)
        return True

    def act_on_report(self, it):
        """Hand a daemon's report to the agent: the user reads it in the
        composer and sends it (nothing runs by itself)."""
        self.set_agent_mode(True, announce=False)
        self.entry.get_buffer().set_text(
            f"My daemon “{it['daemon']}” ({it['why']}) reported:\n\n"
            f"{(it.get('report') or '')[:6000]}\n\nLook into it and fix what needs fixing.")
        try:
            self.entry.grab_focus()
        except Exception:
            pass
        return True

    def _inbox_greeting(self):
        """At startup: what the daemons found while the window was closed."""
        n = len(self._inbox.unread()) if self._inbox is not None else 0
        if n:
            self._sys_note(f"\U0001F4EC {n} daemon report{'' if n == 1 else 's'} waiting (/inbox)", "dim")
        return False

    @tool("daemon", cap=2)
    def _t_daemon(self, body):
        """```daemon <when>: <what>``` — propose a daemon; it exists only
        once the user allows it."""
        spec = (body or "").strip()
        kind = "report"
        if spec.lower().startswith("act "):
            kind, spec = "act", spec[4:].strip()
        m = self._DAEMON_RE.match(spec)
        if self._daemons is None or not m:
            return "[daemon: write it as ```daemon <when>: <what>``` (when: every 2h, daily 08:30, " \
                   "when <path> changes, when a service fails, when disk / is above 90%)]"
        try:
            trig = _daemons.parse_when(m.group(1).strip())
        except _daemons.DaemonError as ex:
            return f"[daemon: {ex}]"
        if kind == "report":
            # one that only looks and reports can't change anything: it is
            # created at once, with an Undo; one that acts still asks first
            d, why = self.daemon_add(spec, kind)
            if d is None:
                return f"[daemon: not created: {why}]"
            card, head, cbody, foot = self._card_shell("DAEMON")
            lb = Gtk.Label(label=f"{_daemons.describe(d['when'])}: {d['brief'][:600]}\nIt looks and "
                                 "reports to the inbox; it changes nothing, and costs nothing "
                                 "while it waits.", xalign=0, wrap=True)
            _wrap_anywhere(lb); cbody.append(lb)
            undo = Gtk.Button(label="Undo"); undo.add_css_class("quick")

            def take_back(*_a, did=d["id"]):
                undo.set_sensitive(False)
                self._daemon_action(self._daemons.get(did) or {"id": did}, "rm")
                self._user_notes.append(f"the user removed daemon {did}")
            undo.connect("clicked", take_back)
            foot.append(undo)
            self._log_pin(card)
            self._daemon_undo = take_back
            return (f"[daemon {d['id']} created: {_daemons.describe(d['when'])}. It reports to the "
                    "inbox; the user can undo it.]")
        card, head, cbody, foot = self._card_shell("DAEMON · PROPOSED")
        lb = Gtk.Label(label=f"{_daemons.describe(trig)}: {m.group(2).strip()[:600]}\n"
                             + ("It will work on a copy of the folder; you merge what it does."
                                if kind == "act" else "It will look and report to /inbox; it "
                                "changes nothing."), xalign=0, wrap=True)
        _wrap_anywhere(lb); cbody.append(lb)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status = Gtk.Label(label="", xalign=0); status.add_css_class("dim")

        def decide(ok):
            for c in (allow, no):
                c.set_sensitive(False)
            if not ok:
                status.set_text("declined")
                self._user_notes.append("the user declined the proposed daemon")
                return
            d, why = self.daemon_add(spec, kind)
            status.set_text(f"daemon {d['id']} created" if d else f"✗ {why}")
            self._user_notes.append(f"the user allowed the daemon: it is daemon {d['id']}" if d
                                    else f"the daemon couldn't be created: {why}")
        allow = Gtk.Button(label="Allow"); allow.add_css_class("quick"); allow.add_css_class("accent-btn")
        allow.connect("clicked", lambda *_a: decide(True))
        no = Gtk.Button(label="Decline"); no.add_css_class("quick")
        no.connect("clicked", lambda *_a: decide(False))
        btns.append(allow); btns.append(no); btns.append(status)
        foot.append(btns)
        self._log_pin(card)
        self._pending_daemon = (decide, card)
        return ("[daemon proposed: a card asks the user to allow it. It exists only once they "
                "do; carry on.]")

    # ── the kernel: /top and process control ──
    _SIG = {"kill": "KILL", "stop": "STOP", "cont": "CONT", "term": "TERM"}

    def process_command(self, cmd, arg):
        if self.kernel is None:
            self._sys_note("The kernel module is missing \u2014 reinstall Chuck.", "danger")
            return False
        parts = arg.split()
        try:
            pid = int(parts[0])
        except (IndexError, ValueError):
            self._sys_note(f"/{cmd} needs a process id \u2014 /top lists them.", "dim")
            return False
        if pid == self._pid1 and cmd != "nice":
            self._sys_note("PID 1 is this chat \u2014 use Stop (Esc) to stop a turn.", "dim")
            return False
        if cmd == "nice":
            try:
                ok = self.kernel.renice(pid, int(parts[1]))
            except (IndexError, ValueError):
                self._sys_note("/nice <pid> <priority from -10 to 10>", "dim")
                return False
        else:
            ok = self.kernel.signal(pid, self._SIG[cmd])
        p = self.kernel.procs.get(pid)
        self._sys_note((f"\u25a0 {cmd} {pid} ({p.name}) \u2192 {p.state}" if ok and p
                        else f"No process {pid}."), "dim" if ok else "danger")
        return bool(ok)

    def show_top(self):
        """/top: every agent process, live: state, priority, tokens, spend,
        rate, run time and what it is doing, with Stop / Kill per process."""
        if self.kernel is None:
            self._sys_note("The kernel module is missing \u2014 reinstall Chuck.", "danger")
            return False
        card, head, body, foot = self._card_shell("TOP", lambda: self._top_text())
        summary = Gtk.Label(xalign=0, wrap=True); summary.add_css_class("dim")
        body.append(summary)
        grid = Gtk.Grid(column_spacing=14, row_spacing=4)
        body.append(grid)
        self._log_pin(card)
        live = {"rows": []}

        def paint():
            if self.kernel is None:
                return False
            rows = self.kernel.ps(all=True)
            rows = [r for r in rows if r["state"] in _kernel.LIVE] + \
                [r for r in rows if r["state"] not in _kernel.LIVE][-12:]
            running = sum(1 for r in rows if r["state"] == "running")
            k = self.kernel
            tot = k.procs[self._pid1].info(k.clock()) if self._pid1 in k.procs else {}
            summary.set_text(f"{sum(1 for r in rows if r['state'] in _kernel.LIVE)} live \u00b7 "
                             f"{running} running \u00b7 model slots {len(k._running)}/{k.slots} "
                             f"\u00b7 governor {_governor.mode_of(self.settings) if _governor else '-'} "
                             f"\u00b7 {_pricing.fmt_tokens(tot.get('tokens', 0))} tokens \u00b7 "
                             f"{_pricing.fmt_usd(tot.get('usd', 0))} this session")
            child = grid.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                grid.remove(child)
                child = nxt
            for c, h in enumerate(("PID", "NAME", "STATE", "NI", "TOKENS", "COST", "TOK/S",
                                   "TIME", "DOING", "")):
                hl = Gtk.Label(label=h, xalign=0); hl.add_css_class("card-kind")
                grid.attach(hl, c, 0, 1, 1)
            for i, r in enumerate(rows, 1):
                cells = [str(r["pid"]), r["name"], r["state"] + (" \u23f3" if r["terminating"]
                                                                     else ""),
                         str(r["nice"]), _pricing.fmt_tokens(r["tokens"]),
                         _pricing.fmt_usd(r["usd"]), f"{r['rate']:.0f}",
                         f"{int(r['seconds']) // 60}:{int(r['seconds']) % 60:02d}",
                         r["activity"] or ""]
                for c, txt in enumerate(cells):
                    lb = Gtk.Label(label=txt, xalign=0)
                    lb.add_css_class("dim" if r["state"] not in _kernel.LIVE else "mono")
                    if c == 2:                  # the state, at a glance
                        lb.add_css_class({"running": "ok", "killed": "danger",
                                          "failed": "danger"}.get(r["state"], "dim"))
                    if c in (1, 8):             # long names and activities never widen the card
                        try:
                            lb.set_ellipsize(Pango.EllipsizeMode.END)
                            lb.set_max_width_chars(22 if c == 1 else 30)
                            lb.set_tooltip_text(r["name"] if c == 1 else r["activity"])
                        except Exception:
                            pass
                        if c == 8:
                            lb.set_hexpand(True)
                    grid.attach(lb, c, i, 1, 1)
                if r["state"] in _kernel.LIVE and r["pid"] != self._pid1:
                    acts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                    paused = r["state"] == "stopped"
                    for lab, cmd_ in (("Continue" if paused else "Pause",
                                       "cont" if paused else "stop"), ("Kill", "kill")):
                        b = Gtk.Button(label=lab); b.add_css_class("quick")
                        b.connect("clicked", lambda _b, p=r["pid"], c_=cmd_: (
                            self.process_command(c_, str(p)), paint()))
                        acts.append(b)
                    grid.attach(acts, 9, i, 1, 1)
            live["rows"] = rows
            return True

        def tick():
            # live while any agent is: then one last paint, and it rests
            paint()
            return any(r["state"] in _kernel.LIVE and r["pid"] != self._pid1
                       for r in live["rows"])
        paint()
        GLib.timeout_add(1000, tick)
        return True

    def _top_text(self):
        if self.kernel is None:
            return ""
        return "\n".join(f"{r['pid']:>4} {r['name'][:24]:<24} {r['state']:<8} {r['tokens']:>8} "
                         f"${r['usd']:.4f} {r['activity'][:40]}" for r in self.kernel.ps(all=True))

    # ── voice input: talk to Chuck ──
    def toggle_listen(self):
        if _listen is None:
            self._sys_note("Voice input is unavailable (module missing) \u2014 reinstall Chuck.", "danger")
            return False
        if self._listening:
            _listen.EARS.cancel()
            return True
        if not _listen.ready():
            self.offer_voice_install("input")
            return False
        stop_speaking()                          # talking over Chuck interrupts him
        self._listening = True
        self._mic_state("starting")
        if not self.settings.get("mic_used"):
            self.settings["mic_used"] = True
            save_settings(self.settings)

        def on_event(ev):
            GLib.idle_add(self._on_hear, ev)

        _listen.EARS.prepare()                  # a second tap from here on is a cancel

        def go():
            try:
                _listen.EARS.listen(on_event, max_wait=float(self.settings.get("mic_wait", 8)),
                                    prepared=True)
            except Exception as ex:
                GLib.idle_add(self._on_hear, {"event": "error", "error": str(ex)})
        threading.Thread(target=go, daemon=True, name="listen").start()
        return True

    def _mic_state(self, state, level=0.0):
        b = getattr(self, "mic_btn", None)
        if b is None:
            return
        for c in ("mic-on", "mic-speech", "mic-wait"):
            b.remove_css_class(c)
        tips = {"starting": "Getting ready to listen\u2026", "listening": "Listening \u2014 talk; "
                "it stops when you pause (Ctrl+M cancels)", "speech": "Hearing you\u2026",
                "transcribing": "Writing down what you said\u2026"}
        if state in ("starting", "transcribing"):
            b.add_css_class("mic-wait")
        elif state == "listening":
            b.add_css_class("mic-on")
        elif state == "speech":
            b.add_css_class("mic-on"); b.add_css_class("mic-speech")
        b.set_tooltip_text(tips.get(state, "Talk to Chuck  (Ctrl+M) \u2014 heard on this machine, "
                                           "never uploaded"))
        try:
            b.set_opacity(1.0 if state != "speech" else 0.75 + min(0.25, level * 4))
        except Exception:
            pass

    def _on_hear(self, ev):
        kind = ev.get("event")
        if kind == "listening":
            self._mic_state("listening")
        elif kind == "speech":
            self._heard_speech = True
            self._mic_state("speech")
        elif kind == "level":
            if getattr(self, "_heard_speech", False):
                self._mic_state("speech", ev.get("rms", 0.0))
        elif kind in ("final", "nothing", "cancelled", "error"):
            self._listening = False
            self._heard_speech = False
            self._mic_state("idle")
            if kind == "final":
                text = (ev.get("text") or "").strip()
                if not text:
                    self._toast("Didn't catch that \u2014 try again (Ctrl+M)")
                    return False
                cur = self._get_entry().rstrip()
                self.entry.get_buffer().set_text((cur + " " if cur else "") + text)
                try:
                    self.entry.grab_focus()
                except Exception:
                    pass
                if self.settings.get("voice_autosend", False) and not self._running:
                    self.on_send()
            elif kind == "nothing":
                self._toast(f"Didn't hear anything in {int(float(self.settings.get('mic_wait', 8)))} s "
                            "\u2014 is the right microphone selected? (/doctor tests it)")
            elif kind == "error":
                self._sys_note(f"\U0001F399 voice input: {ev.get('error')}", "danger")
                if _crashlog is not None:
                    _crashlog.record("voice input", str(ev.get("error")))
        return False

    def offer_voice_install(self, what="both"):
        """A card that installs the natural voice and/or voice input, with
        progress, without leaving the chat."""
        if getattr(self, "_voice_installing", False):
            return
        need_voice = what in ("voice", "both") and not voice.kokoro_ready()
        need_input = what in ("input", "both") and _listen is not None and not _listen.ready()
        if not (need_voice or need_input):
            self._sys_note("The natural voice and voice input are already installed.", "ok")
            return
        card, head, body, foot = self._card_shell("VOICE")
        parts = (["the natural voice (Kokoro, about 350 MB)"] if need_voice else []) + \
                (["voice input (Moonshine, about 250 MB)"] if need_input else [])
        lab = Gtk.Label(label="Install " + " and ".join(parts) + "? " + (
            "Both run" if len(parts) > 1 else "It runs") + " entirely on this machine: nothing "
            "you say or hear is uploaded.", xalign=0, wrap=True)
        lab.add_css_class("dim"); _wrap_anywhere(lab)
        body.append(lab)
        prog = Gtk.Label(label="", xalign=0, wrap=True); prog.add_css_class("cmd-text")
        prog.set_visible(False)                  # until there's progress to show
        body.append(prog)
        go = Gtk.Button(label="Install"); go.add_css_class("accent-btn"); go.set_halign(Gtk.Align.START)
        foot.append(go)

        def start(*_a):
            go.set_sensitive(False)
            self._voice_installing = True

            def say(m):
                GLib.idle_add(lambda: (prog.set_visible(True), prog.set_text(m + "\u2026"),
                                       False)[2])

            def work():
                msgs, ok_all = [], True
                if need_voice:
                    ok, m = voice.install_kokoro(say)
                    msgs.append(m); ok_all &= ok
                    if ok and (self.settings.get("voice_engine") or "auto") in ("auto", "piper"):
                        self.settings["voice_engine"] = "auto"
                        save_settings(self.settings)
                if need_input:
                    ok, m = _listen.install(say)
                    msgs.append(m); ok_all &= ok

                def fin():
                    self._voice_installing = False
                    prog.set_text("\n".join(msgs))
                    head.insert_child_after(self._pill("ready" if ok_all else "failed",
                                                       "ok" if ok_all else "fail"),
                                            head.get_first_child())
                    go.set_visible(False)
                    if ok_all and need_voice:
                        speak("Natural voice ready. This is how I sound now.", self.settings)
                    if not ok_all and _crashlog is not None:
                        _crashlog.record("voice install", "\n".join(msgs))
                    return False
                GLib.idle_add(fin)
            threading.Thread(target=work, daemon=True, name="voice-install").start()
        go.connect("clicked", start)
        self._log_pin(card)

    # ── resume after a crash ──
    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _find_interrupted(self):
        """The newest saved chat that was mid-task when its Chuck died."""
        best = None
        try:
            files = sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return None
        for f in files[:40]:
            try:
                meta = json.loads(f.read_text())
            except Exception:
                continue
            if meta.get("running") and meta.get("pid") != os.getpid() and not self._pid_alive(
                    meta.get("pid")):
                best = (f, meta.get("title") or "a task")
                break
        return best

    def _set_interrupted_flag(self, path, value):
        try:
            meta = json.loads(Path(path).read_text())
            meta["running"] = value
            tmp = Path(str(path) + ".tmp")
            tmp.write_text(json.dumps(meta, indent=2))
            os.replace(tmp, path)               # a crash here must not leave half a chat
        except Exception:
            pass

    RESUME_NOTE = ("CONTINUE: Chuck closed unexpectedly in the middle of this task (a crash or "
                   "power loss); anything that was running was stopped. Check the current state "
                   "first (files, running processes, the plan), then finish the task.")

    def resume_interrupted(self):
        hit = self._interrupted
        if not hit:
            return False
        path, _title = hit
        self._interrupted = None
        self._set_interrupted_flag(path, False)
        self._load_chat(path)
        last = self.history[-1] if len(self.history) > 1 else None
        if last is None:
            return False
        if last.get("role") == "assistant":
            self.history.append({"role": "user", "content": self.RESUME_NOTE})
        self._sys_note("\u21bb resuming the interrupted task", "dim")
        self._reset_turn_state()
        self._start_run()
        self._ask_model()
        return True

    def dismiss_interrupted(self):
        if self._interrupted:
            self._set_interrupted_flag(self._interrupted[0], False)
        self._interrupted = None
        self._show_empty_hint()

    def _voice_fail_note(self, engine, why):
        last = (why or "").strip().splitlines()[-1][:180] if why else "unknown error"
        self._sys_note(f"\U0001F50A {engine} failed ({last}), so another voice is reading this "
                       "for now \u2014 /doctor or Settings \u203a Voice to fix it.", "danger")
        if _crashlog is not None:
            _crashlog.record(engine.lower(), why or "")
        return False

    def _piper_note(self, why):
        last = (why or "").strip().splitlines()[-1][:180] if why else "unknown error"
        self._sys_note(f"\U0001F50A Piper failed, so the robotic fallback voice is speaking: {last} "
                       "\u2014 /doctor or Settings \u203a Voice to fix it.", "danger")
        if _crashlog is not None:
            _crashlog.record("piper", why or "")
        return False

    # ── folder access: full / sandboxed / read-only ──
    ACCESS = [("full", "Full access", "commands run as you, anywhere you can"),
              ("sandbox-net", "Sandboxed", "only this folder is writable; network allowed"),
              ("sandbox", "Sandboxed, offline", "only this folder is writable; no network"),
              ("readonly", "Read-only", "nothing is run or changed; Chuck only reads")]

    def _folder_access(self):
        acc = self.settings.get("folder_access") or {}
        return (acc.get(str(self.workspace), "full") if self.workspace and isinstance(acc, dict)
                else "full")

    def _apply_access(self):
        """Make the module-level sandbox policy match this folder's access."""
        mode = self._folder_access()
        was = (_SANDBOX["on"], _SANDBOX["net"], _SANDBOX["root"], _SANDBOX.get("mode"))
        _SANDBOX.update(on=mode.startswith("sandbox"), net=mode == "sandbox-net",
                        root=str(self.workspace) if self.workspace else None,
                        writable=[str(PLOTS_DIR)], mode=mode)
        wrap = None if mode == "full" else _sandboxed_look
        if _browser is not None and hasattr(_browser, "FILES_ROOT"):
            _browser.FILES_ROOT[0] = None if mode == "full" or not self.workspace else str(self.workspace)
            _browser.HIDDEN[0] = tuple(os.path.realpath(os.path.join(str(Path.home()), r))
                                       for r in _SANDBOX_HIDE + (".netrc",))
        for mod in (_builder, _diagnose, _lsp):  # tests, linters and language servers too
            if mod is not None and hasattr(mod, "RUN_WRAP"):
                mod.RUN_WRAP[0] = wrap if wrap is None or mod is not _lsp else (
                    lambda a: _sandboxed_look(a, persistent=True))
        if getattr(self, "_lsp_m", None) is not None and was != (
                _SANDBOX["on"], _SANDBOX["net"], _SANDBOX["root"], mode):
            threading.Thread(target=self._lsp_m.stop_all, daemon=True).start()
        try:
            PLOTS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if was[:3] != (_SANDBOX["on"], _SANDBOX["net"], _SANDBOX["root"]) and getattr(self, "_py", None):
            self._py_close()                     # the Python session restarts under the new rules
        try:
            label = dict((k, l) for k, l, _d in self.ACCESS)[mode]
            short = {"sandbox-net": "sandbox", "sandbox": "offline", "readonly": "read-only"}
            self._ws_lbl.set_label((self.workspace.name if self.workspace else "No folder")
                                   + (f" \u00b7 {short[mode]}" if mode in short else ""))
            self.ws_btn.set_tooltip_text(f"Working folder \u2014 access: {label} (/access)")
        except Exception:
            pass
        return mode

    def set_access(self, mode):
        if not self.workspace:
            self._sys_note("Pick a working folder first: access is set per folder.", "danger")
            return False
        if mode not in [k for k, _l, _d in self.ACCESS]:
            return False
        if mode.startswith("sandbox") and not shutil.which("bwrap"):
            self._sys_note("Sandboxing needs bubblewrap (pacman -Syu bubblewrap / apt install "
                           "bubblewrap). Access left unchanged.", "danger")
            return False
        acc = dict(self.settings.get("folder_access") or {})
        if mode == "full":
            acc.pop(str(self.workspace), None)
        else:
            acc[str(self.workspace)] = mode
        self.settings["folder_access"] = acc
        save_settings(self.settings)
        self._apply_access()
        self._refresh_system()
        label = dict((k, l) for k, l, _d in self.ACCESS)[mode]
        self._sys_note(f"\U0001F512 {self.workspace.name}: {label}" if mode != "full"
                       else f"{self.workspace.name}: Full access", "ok")
        return True

    def show_access(self):
        """/access: choose this folder's access, one click."""
        if not self.workspace:
            self._sys_note("Pick a working folder first: access is set per folder.", "dim")
            return False
        cur = self._folder_access()
        card, head, body, foot = self._card_shell("ACCESS")
        head.insert_child_after(self._pill(self.workspace.name, "run"), head.get_first_child())
        for key, label, desc in self.ACCESS:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = Gtk.Label(label=label, xalign=0); t.add_css_class("doctor-name")
            d = Gtk.Label(label=desc + ("" if key == "full" or not key.startswith("sandbox")
                                        or shutil.which("bwrap") else " (needs bubblewrap)"),
                          xalign=0, wrap=True)
            d.add_css_class("dim")
            col.append(t); col.append(d)
            row.append(col)
            if key == cur:
                row.append(self._pill("current", "ok"))
            else:
                b = Gtk.Button(label="Use"); b.add_css_class("quick"); b.set_valign(Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, k=key: (self.set_access(k), self.show_access()))
                row.append(b)
            body.append(row)
        self._log_pin(card)
        return True

    # ── checkpoints: /rewind ──
    CHECKPOINT_STORE = DATA_DIR / "checkpoints"

    def _checkpoint(self, label):
        """Snapshot the working folder before a step that may change it.
        Synchronous on purpose: the step must not start changing files while
        the snapshot is still being read."""
        root = self._workroot()
        if (_checkpoint is None or root is None or not self.settings.get("checkpoints", True)
                or str(root) in self._cp_off):
            return None
        try:
            sha = _checkpoint.Shadow(root, self.CHECKPOINT_STORE).snapshot(label)
        except _checkpoint.CheckpointError as ex:
            self._cp_off.add(str(root))
            self._sys_note(f"/rewind is off for this folder: {ex}", "dim")
            return None
        ask = next((self._typed_text(m.get("content")) for m in reversed(self.history)
                    if self._is_real_user(m)), "")
        cp = {"sha": sha, "root": str(root), "hist": len(self.history) - 1, "label": label,
              "ask": (ask or "")[:80], "t": time.time()}
        self._checkpoints = (self._checkpoints + [cp])[-200:]
        return cp

    def show_rewind(self):
        """/rewind: the checkpoints, newest first, each with what changed since."""
        cps = [c for c in self._checkpoints if c["hist"] <= len(self.history) or c.get("history")]
        if not cps:
            self._sys_note("No checkpoints yet \u2014 Chuck snapshots the folder before each step "
                           "that changes it.", "dim")
            return False
        card, head, body, foot = self._card_shell("REWIND")
        hint = Gtk.Label(label="Puts the files AND the conversation back to before that step. "
                               "Rewinding is itself undoable.", xalign=0, wrap=True)
        hint.add_css_class("dim"); body.append(hint)
        try:
            nested = _checkpoint.Shadow(cps[-1]["root"], self.CHECKPOINT_STORE).nested()
        except Exception:
            nested = []
        if nested:
            w = Gtk.Label(label="\u26a0 " + ", ".join(nested[:4]) + (" \u2026" if len(nested) > 4 else "")
                          + (" is its own git repository" if len(nested) == 1 else
                             " are their own git repositories")
                          + ": what's inside isn't snapshotted, so a rewind leaves it as it is.",
                          xalign=0, wrap=True)
            w.add_css_class("dim"); _wrap_anywhere(w); body.append(w)
        now = time.time()
        for cp in reversed(cps[-12:]):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = Gtk.Label(label=cp["label"] if cp.get("history") else f"before  {cp['label']}",
                          xalign=0)
            t.add_css_class("doctor-name")
            try:
                t.set_ellipsize(Pango.EllipsizeMode.END)
            except Exception:
                pass
            try:
                n = len(_checkpoint.Shadow(cp["root"], self.CHECKPOINT_STORE)
                        .changed_since(cp["sha"]))
            except Exception:
                n = None
            ago = int(now - cp.get("t", now))
            ago = (f"{ago // 3600} h ago" if ago >= 3600 else f"{ago // 60} min ago" if ago >= 60
                   else "just now")
            meta = " \u00b7 ".join(x for x in (
                f"\u201c{cp['ask']}\u201d" if cp.get("ask") else "", ago,
                "" if n is None else ("no files differ now" if n == 0 else
                                      "1 file differs now" if n == 1 else f"{n} files differ now")) if x)
            m = Gtk.Label(label=meta, xalign=0, wrap=True); m.add_css_class("dim")
            _wrap_anywhere(m)
            col.append(t); col.append(m)
            b = Gtk.Button(label="Rewind"); b.add_css_class("quick"); b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, c=cp: self.rewind_to(c))
            row.append(col); row.append(b)
            body.append(row)
        self._log_pin(card)
        return True

    def rewind_to(self, cp):
        """Restore the folder to a checkpoint and the chat to before that step."""
        if self._running:
            self._sys_note("Stop the current run first (Esc), then rewind.", "danger")
            return False
        if _checkpoint is None:
            return False
        def undo_of(sha):
            return {"sha": sha, "root": cp["root"], "hist": len(self.history),
                    "label": "undo the rewind", "ask": "", "t": time.time(),
                    "history": [dict(m) for m in self.history],
                    "later": [c for c in self._checkpoints if not c.get("history")]}
        try:
            before, changed = _checkpoint.Shadow(cp["root"], self.CHECKPOINT_STORE).restore(cp["sha"])
        except _checkpoint.CheckpointError as ex:
            if ex.before:                 # files may be part-way back: keep the way home
                self._checkpoints = (self._checkpoints + [undo_of(ex.before)])[-200:]
            self._sys_note(f"\u2717 couldn't rewind: {ex}", "danger")
            return False
        undo = undo_of(before)
        if cp.get("history"):                     # undoing a rewind: everything comes back
            self.history = [dict(m) for m in cp["history"]]
            self._rebuild_transcript()
            self._checkpoints = list(cp.get("later") or []) + [undo]
        else:
            self._rewind_to(cp["hist"])
            self._checkpoints = [c for c in self._checkpoints if c is not cp
                                 and not c.get("history") and c["hist"] < len(self.history)] + [undo]
        self._changes = []              # file checkpoints for `undo` describe a state that's gone
        self._reset_turn_state()
        last = self.history[-1] if len(self.history) > 1 else None
        if last is not None and self._is_real_user(last) and not cp.get("history"):
            self.history.pop()          # your message, back in the composer to send again
            self._rebuild_transcript()
            if not self._get_entry():
                self.entry.get_buffer().set_text(self._typed_text(last.get("content")))
        self._sys_note(f"\u27f2 rewound {len(changed)} file{'' if len(changed) == 1 else 's'} and "
                       f"the chat to before: {cp['label']}" + ("" if cp.get("history") else
                                                               "  (/rewind again to undo)"), "ok")
        self._save_chat()
        return True

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
        def guarded():
            try:
                res = work()
            except Exception as ex:              # never leave the updater stuck "checking…"
                res = ("fail", f"the update failed ({ex.__class__.__name__}: {ex})", None)
            GLib.idle_add(done, res)
        threading.Thread(target=guarded, daemon=True, name="update").start()

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
        # a repo's own config can run commands (core.fsmonitor, diff drivers,
        # filters): outside a fully trusted folder git runs in the sandbox too
        argv = _sandboxed_look(["git", "-c", "core.fsmonitor=false", *args])
        if argv is None:
            return 126, SANDBOX_NO_BWRAP
        try:
            p = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
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
    PR_ASK = ("Open a pull request for this work. Check `git status`, the current branch and "
              "`git remote -v`. If on the default branch, create a branch with a short descriptive "
              "name. Stage only what belongs (never secrets or build output), commit with a "
              "message that says why, run the project's tests, push with `git push -u origin "
              "<branch>`, then open the PR with `gh pr create` (title: what it does; body: what "
              "changed and why, how it was tested, anything the reviewer should look at). If `gh` "
              "isn't installed or isn't logged in, stop after the push and give the exact "
              "compare URL. Show the PR URL at the end.")

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

        hide = self.agent_mode and self._folder_access() != "full"
        if hide and _secret_path(root / sub if sub else root):
            self._busy(False)
            self._tool_done("[grep refused: that's a credentials folder, which this folder's "
                            "access (/access) keeps hidden]")
            return

        def work():
            ok, out = _builder.grep(root, pattern, sub)
            if ok and hide:                 # searching ~ in a restricted folder: skip secrets
                out = "\n".join(ln for ln in out.split("\n")
                                if not _secret_path(Path(root, ln.split(":", 1)[0])))
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
                 "read-only tools only (map, glob, grep, read, def, refs, find, search, fetch, "
                 "browse, diagnose, task). Change NOTHING: no bash/python, write, edit, patch, "
                 "rename, bg or rmfile. When "
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
            beat = self._beat()                 # a long suite isn't "stuck"
            try:
                rc, out = _builder.run_tests(p)
            finally:
                beat.set()

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
        ("/pr [what it's about]", "a branch, a commit, a push and a pull request with a written "
                                  "description (via gh)"),
        ("/review", "Chuck reviews the uncommitted changes for bugs"),
        ("/undo", "revert the last file change"),
        ("/rewind", "put the folder AND the chat back to before any step"),
        ("/access", "this folder: full access, sandboxed, or read-only"),
        ("/voice", "install the natural voice and voice input (all local)"),
        ("/retry", "ask the last question again  (Ctrl+R; \u2191 edits it)"),
        ("/plan", "plan mode on/off: propose first, act after you approve"),
        ("/agent  /chat", "switch mode"),
        ("/folder [path]", "set the working folder (no path: choose one)"),
        ("/cost", "what this chat has cost"),
        ("/memory", "open the memory panel"),
        ("/export", "save this chat as Markdown"),
        ("/mcp [reload]", "MCP servers and their tools"),
        ("/doctor", "check your setup: key, model, browser, tools, errors"),
        ("/top", "every agent process: state, tokens, spend, what it's doing"),
        ("/kill /stop /cont <pid>", "end, pause or resume an agent process"),
        ("/spawn <task>", "a background agent does it in a copy of the folder while you go on"),
        ("/join /discard <pid>", "merge a background agent's work into the folder, or drop it"),
        ("/nice <pid> <-10..10>", "an agent's priority for the model (lower runs first)"),
        ("/daemon <when>: <task>", "an agent that wakes on a schedule or event (daily 08:30, "
                                   "when a service fails\u2026), looks and reports"),
        ("/daemons  /inbox", "your daemons (run, pause, delete) and their reports"),
        ("/run <program> [focus]", "run an agent program: reviewer, tester, auditor, checkup, "
                                   "researcher, explainer, or your own (/programs)"),
        ("/governor [mode]", "performance, balanced or powersave: which model each agent runs on"),
        ("/strace <pid>", "every tool call a process made, its latency and size"),
        ("/replay <pid>", "a background agent's run, step by step"),
        ("/mission <goal>", "a goal too big for one agent: planned as tasks, run by agents in "
                            "parallel, each tested before it's merged"),
        ("/missions", "your missions and how far they got"),
        ("/budget [day|month <$>]", "what everything spent today and this month, and the limits"),
        ("/activity", "everything waiting for you and everything working (the tray in the header)"),
        ("Ctrl+P", "the command palette: every action, agent, chat and model, one search away"),
        ("/watch [off]", "act on AI! comments you save in any editor (AI? asks a question)"),
        ("/context", "what the next model call sends, by what takes the room"),
        ("/remind <when>: <what>", "a reminder: in 20m, at 5pm, tomorrow at 9am, every day at 8:30"),
        ("/send <pid> <message>", "tell a running agent something; it hears it at its next step"),
        ("/answer <pid> <text>", "answer an agent that asked you a question"),
        ("/board", "the facts agents share with each other"),
        ("/fork <pid> <step> [instruction]", "start a new agent from any step of a run, its "
                                             "files as they were then"),
        ("/update", "install the newest Chuck (then restart)"),
    ]

    def _slash(self, text):
        """Run a /command typed in the composer. -> True if it was one. Anything
        that merely starts with a slash (/etc/fstab is broken) is a message."""
        m = re.match(r"^/([a-z][a-z0-9_-]*)(?:\s+(.*))?$", (text or "").strip(), re.DOTALL)
        if not m:
            return False
        cmd, arg = m.group(1), (m.group(2) or "").strip()
        if cmd == "help":
            card, head, body, foot = self._card_shell("COMMANDS")
            grid = Gtk.Grid(column_spacing=18, row_spacing=6)
            custom = [(f"/{n}", d or "your command") for n, (_p, d) in
                      sorted(self.custom_commands().items())]
            for i, (c, d) in enumerate(self.SLASH_HELP + custom
                                       + [("@path", "attach a file from the working folder"),
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
        elif cmd in ("top", "ps"):
            self.show_top()
        elif cmd in ("kill", "stop", "cont", "nice"):
            self.process_command(cmd, arg)
        elif cmd == "spawn":
            self.run_program(arg) if arg.startswith("@") else self.spawn_background(arg)
        elif cmd == "join":
            self.join_background(arg)
        elif cmd == "discard":
            self.discard_background(arg)
        elif cmd == "daemon":
            self.daemon_command(arg)
        elif cmd == "run":
            self.run_program(arg)
        elif cmd == "programs":
            self.show_programs()
        elif cmd == "governor":
            self.governor_command(arg)
        elif cmd == "strace":
            self.show_strace(arg)
        elif cmd == "replay":
            self.show_replay(arg)
        elif cmd == "mission":
            self.start_mission(arg)
        elif cmd == "budget":
            self.budget_command(arg)
        elif cmd == "activity":
            self.show_activity()
        elif cmd == "watch":
            self.watch_command(arg)
        elif cmd == "remind":
            self.remind_command(arg)
        elif cmd in ("context", "mem"):
            self.show_context()
        elif cmd == "missions":
            self.show_missions()
        elif cmd == "send":
            m = re.match(r"(\d+)\s+(.+)$", arg, re.DOTALL)
            self._sys_note(self.send_to(m.group(1), m.group(2)) if m else "/send <pid> <message>", "dim")
        elif cmd == "answer":
            m = re.match(r"(\d+)\s+(.+)$", arg, re.DOTALL)
            if m:
                self.answer_ask(m.group(1), m.group(2))
            else:
                self._sys_note("/answer <pid> <your answer>", "dim")
        elif cmd == "board":
            self.show_board()
        elif cmd == "fork":
            self.fork_process(arg)
        elif cmd == "daemons":
            self.show_daemons()
        elif cmd == "inbox":
            self.show_inbox()
        elif cmd == "rewind":
            self.show_rewind()
        elif cmd == "access":
            self.show_access()
        elif cmd == "voice":
            self.offer_voice_install("both")
        elif cmd == "update":
            self.run_update()
        elif cmd in ("commit", "review", "pr"):
            if not self.workspace:
                self._sys_note("Pick a working folder first (/folder).", "danger")
                return True
            self.set_agent_mode(True, announce=False)
            ask = {"commit": self.COMMIT_ASK, "review": self.REVIEW_ASK, "pr": self.PR_ASK}[cmd]
            if cmd == "pr" and arg:
                ask += f" What this PR is about, in the user's words: {arg}"
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
        elif cmd in self.custom_commands():
            self.run_custom_command(cmd, arg)
        else:
            return False
        return True

    # ── custom commands: .chuck/commands/<name>.md -> /name ──
    def custom_commands(self):
        """{name: (path, description)}: the project's .chuck/commands/*.md
        over the config folder's commands/*.md. Built-ins can't be replaced."""
        out = {}
        builtin = {part[1:] for c, _d in self.SLASH_HELP for part in c.split()
                   if part.startswith("/")}
        dirs = [CONFIG_DIR / "commands"]
        if self.workspace:
            dirs.append(Path(self.workspace) / ".chuck" / "commands")
        for d in dirs:                                   # later (the project's) wins
            try:
                files = sorted(d.glob("*.md"))
            except OSError:
                continue
            for f in files:
                name = f.stem.lower()
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", name) or name in builtin:
                    continue
                desc = ""
                try:
                    head = f.read_text(encoding="utf-8", errors="replace")[:600]
                    m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
                    dm = (re.search(r"(?mi)^description:\s*(.+)$", m.group(1)) if m
                          else re.match(r"\s*description:\s*(.+)", head, re.I))
                    desc = dm.group(1).strip() if dm else next(
                        (ln.strip("# ").strip() for ln in head.splitlines()
                         if ln.strip() and not ln.startswith("---")), "")
                except OSError:
                    continue
                out[name] = (f, desc[:80])
        return out

    def run_custom_command(self, name, arg):
        path, _desc = self.custom_commands()[name]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as ex:
            self._sys_note(f"/{name}: couldn't read {path}: {ex}", "danger")
            return
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S).strip()
        text = re.sub(r"\A\s*description:[^\n]*\n?", "", text, flags=re.I).strip()   # metadata, not prompt
        if "$ARGUMENTS" in text:
            text = text.replace("$ARGUMENTS", arg)
        elif arg:
            text += "\n\n" + arg
        if not text:
            self._sys_note(f"/{name} is empty ({_short_path(path)}).", "danger")
            return
        if text.startswith("/"):
            self._sys_note(f"/{name} expands to another command; write the prompt itself in "
                           f"{_short_path(path)}.", "danger")
            return
        self.entry.get_buffer().set_text(text)
        self.on_send()

    def on_send(self, *_):
        text = self._get_entry()
        if text and not self._attachments and self._slash(text):
            self.entry.get_buffer().set_text("")
            return
        if self._running and (text or self._attachments):
            self._attach_mentions(text)
            files = [a for a in self._attachments if a["kind"] != "image"]
            if not text and not files:
                return                      # only pictures: they go with the next message
            self._attachments = [a for a in self._attachments if a["kind"] == "image"]
            self._refresh_tray()
            self._queue_steer(text, files)
            if self._attachments:
                self._sys_note("\U0001F5BC the picture goes with your next message, once this "
                               "task is done", "dim")
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
        stop_speaking()                         # the last answer stops reading over the new one
        self.entry.get_buffer().set_text("")
        self._attachments = []
        self._refresh_tray()
        if self._edit_at is not None:
            # an edited message replaces the original and everything after it
            at, self._edit_at = self._edit_at, None
            # only if it's still THAT message: after a chat switch the index
            # would cut someone else's conversation
            if at < len(self.history) and self.history[at] is getattr(self, "_edit_msg", None):
                self._rewind_to(at)
            self._edit_msg = None
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
        body += self._att_text(atts)
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

    def _over_budget(self):
        """The OS-wide daily or monthly limit, if reached -> (scope, limit,
        spent), else None."""
        if getattr(self, "_ledger", None) is None:
            return None
        return self._ledger.over(self.cfg("budget_day_usd", 0.0, 0.0, 100000.0, cast=float),
                                 self.cfg("budget_month_usd", 0.0, 0.0, 1000000.0, cast=float))

    def _budget_pause(self, over, vision):
        """Stop before the next model call and ask: the limit is the user's."""
        scope, lim, spent = over
        base = self.cfg("budget_day_usd" if scope == "day" else "budget_month_usd", 0.0, 0.0,
                        1000000.0, cast=float)
        card, head, cbody, foot = self._card_shell("BUDGET")
        card.add_css_class("ask-card")
        ql = Gtk.Label(xalign=0, wrap=True, hexpand=True)
        ql.add_css_class("prose")
        ql.set_text(f"{'Today' if scope == 'day' else 'This month'} everything Chuck ran (every chat, "
                    f"agent, mission and daemon) has spent {_pricing.fmt_usd(spent)}: your "
                    f"{'daily' if scope == 'day' else 'monthly'} limit is {_pricing.fmt_usd(lim)}. "
                    "Paused before the next model call; agents in the background are paused too.")
        cbody.append(ql)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        go = Gtk.Button(label=f"Allow {_pricing.fmt_usd(base)} more {'today' if scope == 'day' else 'this month'}")
        go.add_css_class("quick")
        stop = Gtk.Button(label="Stop here"); stop.add_css_class("ghost")
        row.append(go); row.append(stop)
        foot.append(row)
        gen_, chat_ = self._gen, self.chat_id

        def cont(_b):
            go.set_sensitive(False); stop.set_sensitive(False)
            if self._running or gen_ != self._gen or chat_ != self.chat_id:
                return                          # an old card: it changes nothing
            # `base` more from what has been spent now, not from the old limit
            self._ledger.allow_more(scope, max(0.0, spent - lim) + base)
            self._start_run()
            self._ask_model(vision)

        def halt(_b):
            go.set_sensitive(False); stop.set_sensitive(False)
            self._sys_note("Stopped at the budget (/budget to change it).", "dim")
        go.connect("clicked", cont)
        stop.connect("clicked", halt)
        self._log_pin(card)
        self._end_run()

    def budget_command(self, arg):
        """/budget [day|month <usd>]: what everything has spent, and the limits."""
        if self._ledger is None:
            self._sys_note("The budget module is missing \u2014 reinstall Chuck.", "danger")
            return False
        m = re.match(r"\s*(day|daily|month|monthly)\s+\$?([\d.]+)\s*$", arg or "")
        if m:
            key = "budget_day_usd" if m.group(1).startswith("da") else "budget_month_usd"
            try:
                self.settings[key] = max(0.0, float(m.group(2)))
            except ValueError:
                self._sys_note("/budget day 2.50  \u00b7  /budget month 40  (0 = no limit)", "dim")
                return False
            save_settings(self.settings)
        elif (arg or "").strip():
            self._sys_note("/budget day 2.50  \u00b7  /budget month 40  (0 = no limit)", "dim")
            return False
        L = self._ledger
        day_l = self.cfg("budget_day_usd", 0.0, 0.0, 100000.0, cast=float)
        mon_l = self.cfg("budget_month_usd", 0.0, 0.0, 1000000.0, cast=float)
        hist = L.history(14)
        card, head, body, foot = self._card_shell(
            "BUDGET", lambda: "\n".join(f"{d}  {_pricing.fmt_usd(v)}" for d, v in hist))
        top = Gtk.Label(xalign=0, wrap=True)
        top.set_text(f"Today {_pricing.fmt_usd(L.today())}"
                     + (f" of {_pricing.fmt_usd(day_l)}" if day_l else " (no daily limit)")
                     + f"  \u00b7  this month {_pricing.fmt_usd(L.month())}"
                     + (f" of {_pricing.fmt_usd(mon_l)}" if mon_l else " (no monthly limit)"))
        body.append(top)
        sub = Gtk.Label(xalign=0, wrap=True)
        sub.add_css_class("dim"); _wrap_anywhere(sub)
        sub.set_text("Everything counts: every chat, sub-agent, background agent, program, mission and "
                     "daemon. Over a limit, the chat asks before going on and agents are refused "
                     "their next call. Set them with /budget day 2.50 or /budget month 40 "
                     "(0 = no limit), or in Settings.")
        body.append(sub)
        peak = max([v for _d, v in hist] + [1e-9])
        block = "\u2588"
        bars = "\n".join(f"{d[5:]}  {block * max(0, round(v / peak * 24)):<24}  {_pricing.fmt_usd(v)}"
                         for d, v in hist)
        bl = Gtk.Label(label=bars, xalign=0, selectable=True)
        bl.add_css_class("cmd-text")
        body.append(bl)
        self._log_pin(card)
        return True

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

        # only the newest cap card can continue: an older one would start a
        # second model stream beside the run the newer one started
        for old_btns in getattr(self, "_cap_btns", ()):
            for b_ in old_btns:
                try:
                    b_.set_sensitive(False)
                except Exception:
                    pass
        self._cap_btns = [(go, stop)]
        gen_, chat_ = self._gen, self.chat_id

        def cont(_b):
            go.set_sensitive(False); stop.set_sensitive(False)
            if self._running or gen_ != self._gen or chat_ != self.chat_id:
                return                          # a run is going, or this chat moved on
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

    @staticmethod
    def _att_text(atts):
        """Attachments as they go into a message's text."""
        body = ""
        for a in atts:
            if a["kind"] == "text":
                note = (f" (first {len(a['text']):,} characters of {a['size']:,} \u2014 the "
                        f"full file is at {a['path']})" if a.get("cut") else "")
                body += (f"\n\n--- attached file: {a['name']}{note} ---\n{a['text']}"
                         "\n--- end ---")
            elif a["kind"] == "path":
                body += (f"\n\n[attached file: {a['path']} ({a['size']:,} bytes, binary) \u2014 "
                         "work with it by path]")
            else:
                body += f"\n[image attached: {a['name']}]"
        return body

    def _queue_steer(self, text, files=()):
        """You typed while it works: show it now, deliver it at the next step.
        Files you attached (or @mentioned) go with it; pictures wait in the
        tray for your next message, since they can't ride along mid-task."""
        self.entry.get_buffer().set_text("")
        atts = [a for a in files if a["kind"] != "image"]
        self._steer.append((text or "Look at the attached file(s).") + self._att_text(atts))
        self._user_bubble(text or "(attached files)", files=[a["name"] for a in atts])
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
        if (getattr(self, "_disk_running", None) != (self.chat_id, True)
                or time.time() - getattr(self, "_last_autosave", 0) > 2):
            self._save_chat(running=True, light=True)   # a crash from here on can be resumed
        self._deliver_steer()
        cap = self._over_cap()
        if cap:
            self._cost_pause(cap, vision)
            return
        over = self._over_budget()
        if over:
            self._budget_pause(over, vision)
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
        first = next((m for m in self.history if self._is_real_user(m)), None)
        title = (self._typed_text(first.get("content")) if first else "").strip()
        if not title and first is not None:    # only a file: name the chat after it
            att = re.search(r"--- attached file: (.+?)(?: \(| ---)", str(first.get("content")))
            title = att.group(1) if att else ""
        title = (title or "Chat")[:80]
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
                                     "CONTINUE:", "FILE ", self._STEER_HEAD))
            steer = ""
            if role == "user" and internal and self._STEER_HEAD in c:
                at = c.find(self._STEER_HEAD)       # your mid-task message is yours
                steer = c[at:].split("]:\n", 1)[-1].strip()
                c = c[:at].rstrip()
            if role == "user" and internal:
                if c.strip():
                    body = clip_output(c, 6000).replace("</details>", "&lt;/details&gt;")
                    # a fence longer than any backtick run inside, so a README
                    # in the output can't close it early
                    fence = "`" * max(4, max((len(r) for r in re.findall(r"`+", body)),
                                             default=0) + 1)
                    lines += ["<details><summary>Tool results</summary>", "", fence + "text",
                              body, fence, "", "</details>", ""]
                if steer:
                    lines += ["## You", "", steer, ""]
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
        if (self._compacting or not self.settings.get("auto_compact", True) or self._over_cap()
                or self._over_budget()):
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
        if not auto and (self._over_cap() or self._over_budget()):
            self._sys_note("Not compacting: the spending cap or the budget is reached (/budget).", "danger")
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
        # the very messages it summarises: a retry, an edit or a rewind cuts
        # history in place and the run regrows it, so a length check can't
        # tell that these turns are gone
        anchor, first = self.history[upto - 1], self.history[start]
        self._sys_note("\U0001F5DC compacting earlier turns\u2026", "dim")

        def work():
            out, err = [], []
            b = Backend(self.settings) if isinstance(self.backend, Backend) else self.backend
            if isinstance(b, Backend):
                b.role = "summary"              # the governor may give it the fast model
            try:
                b.stream(msgs, out.append, lambda: None, err.append)
            except Exception as ex:
                err.append(str(ex))
            text = "".join(out).strip()

            def land():
                self._compacting = False
                if not err:                      # it was paid for, whether it lands or not
                    self._account(len(transcript), len(text), usage=getattr(b, "last_usage", None),
                                  model=getattr(b, "last_model", None))
                if (chat != self.chat_id or upto > len(self.history) - 1
                        or self.history[upto - 1] is not anchor or self.history[start] is not first):
                    return False                 # another chat, or history changed under it
                if err or len(text) < 40:
                    self._sys_note("\u26a0 couldn't compact: " + (err[0] if err else
                                                                   "empty summary"), "danger")
                    return False
                self._compact = {"upto": upto, "text": text, "anchor": _msg_key(anchor)}
                self._sys_note(f"\U0001F5DC compacted {upto - start} earlier messages into a "
                               f"{len(text) // 1000 or 1}k summary", "dim")
                self._save_chat()
                return False
            GLib.idle_add(land)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _trim_for_send(self, messages, dry=False):
        """Bound the conversation actually sent to the model.

        Never mutates self.history — the user keeps their whole transcript on
        disk. This only decides what rides along this turn: the system prompt,
        then as much recent conversation as fits the budget, with stale research
        blobs dropped first (they're huge, and the answer that followed them
        already contains their conclusions).
        """
        if not messages:
            return messages
        messages = self._page_out(messages)     # virtual memory: lossless, before anything lossy
        c = self._compact
        if c and c.get("anchor") and not (1 < c["upto"] <= len(self.history)
                                         and _msg_key(self.history[c["upto"] - 1]) == c["anchor"]):
            c = None                             # what it summarised is no longer there
            if not dry:
                self._compact = None
        if c and 1 < c["upto"] < len(messages) - 1:
            # the summary stands in for every turn it covers — but never for
            # the request being worked on, or its plan: those go verbatim
            extra = ""
            li = self._last_user_index()
            if li is not None and li < c["upto"]:
                extra += ("\n\nTHE USER'S CURRENT REQUEST (verbatim):\n"
                          + self._typed_text(self.history[li].get("content")))
            newer_plan = any(m.get("role") == "assistant" and "```plan" in str(m.get("content"))
                             for m in self.history[c["upto"]:])
            for m in ([] if newer_plan else reversed(self.history[:c["upto"]])):
                mc = m.get("content")
                if m.get("role") == "assistant" and isinstance(mc, str) and "```plan" in mc:
                    pm = re.findall(r"```plan[ \t]*\n?(.*?)```", mc, re.DOTALL)
                    if pm:
                        extra += "\n\nTHE CURRENT PLAN:\n" + pm[-1].strip()
                    break
                if self._is_real_user(m):
                    break                        # a plan from an earlier request
            table = self._page_table(1, c["upto"])
            if table:
                extra += ("\n\nPAGE TABLE (the exact tool output of the summarised turns is still "
                          "there: ```page <id>``` brings any of it back):\n" + table)
            messages = (messages[:1] + [{"role": "user", "content":
                        "CONVERSATION SUMMARY (earlier turns, compacted — the work so far):\n"
                        + c["text"] + extra}] + messages[c["upto"]:])
        if not dry:                              # /context only looks: never compacts (a paid call)
            self._maybe_auto_compact(messages)
        # Compress BEFORE deciding what fits. The old order dropped whole blobs
        # the moment the budget was tight, which threw away the middle of a
        # research chain entirely; squeezing them first means more of the chain
        # survives in the same number of characters. Extractive only — head,
        # tail and any error lines are kept verbatim, nothing is paraphrased.
        if _compress is not None:
            messages, saved = _compress.compress_history(
                messages, keep_recent=4, budget_chars=4000)
            if saved > 2000 and not dry:
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

    # ── virtual memory for the context (Chuck OS, phase 18) ──
    PAGE_MIN = 3000                 # a tool result smaller than this stays inline
    _PAGEABLE = ("TOOL RESULTS", "FILE ", "SUB-AGENT REPORTS")

    def _pageable(self, m):
        c = m.get("content")
        return (m.get("role") == "user" and isinstance(c, str) and len(c) >= self.PAGE_MIN
                and c.startswith(self._PAGEABLE))

    @staticmethod
    def _gist(text, limit=420):
        """What a paged-out result was, in one line: the tools that produced it
        and the lines that matter (errors, failures, exit codes)."""
        heads = re.findall(r"(?m)^\[([a-z]+ [^\]\n]{0,70})\]", text)[:6]
        hot = [ln.strip() for ln in text.splitlines()
               if re.search(r"(?i)\b(error|failed|failure|exception|traceback|exit [1-9]|"
                            r"not found|denied|warning)\b", ln)][:4]
        g = "; ".join(heads) + (" \u2014 " + " | ".join(h[:120] for h in hot) if hot else "")
        return (g or text.split("\n", 2)[-1][:200]).replace("\n", " ")[:limit]

    def _page_entry(self, idx, text):
        return (f"[PAGED OUT p{idx}: {len(text):,} chars of earlier tool output \u2014 ```page p{idx}``` "
                f"brings it back exactly (```page p{idx} 40-120``` for lines, ```page p{idx} "
                f"/regex/``` to search it)]\nGist: {self._gist(text)}")

    def _page_out(self, messages):
        """Context is RAM. Once the conversation is big, older tool output is
        swapped out of what is SENT (history keeps every byte) and replaced by
        a one-line page-table entry; ```page``` swaps it back in, exactly.
        Lossless, unlike trimming or compaction, and cheaper on every call
        that follows."""
        if not self.settings.get("paging", True):
            return messages
        size = sum(len(m["content"]) for m in messages[1:] if isinstance(m.get("content"), str))
        if size < self.cfg("page_after_chars", 24000, 4000, 2000000):
            return messages
        where = {id(m): i for i, m in enumerate(self.history)}
        pageable = [j for j, m in enumerate(messages) if self._pageable(m) and id(m) in where]
        keep = set(pageable[-self.cfg("page_keep", 3, 1, 20):])   # the newest stay inline
        out = list(messages)
        for j in pageable:
            if j not in keep:
                m = out[j]
                out[j] = {"role": m["role"], "content": self._page_entry(where[id(m)], m["content"])}
        return out

    def _page_table(self, lo, hi, limit=25):
        rows = [f"p{i}: {self._gist(m['content'], 160)}" for i, m in enumerate(self.history[lo:hi], lo)
                if self._pageable(m)]
        return "\n".join(rows[-limit:])

    @tool("page", agent_only=False, cap=4)
    def _t_page(self, body):
        """```page p17``` (whole, in pieces), ```page p17 40-120``` (lines),
        ```page p17 /regex/``` (matching lines): exact tool output that was
        paged out of the context, swapped back in."""
        m = re.match(r"^\s*p?(\d+)\s*(.*)$", body or "", re.S)
        if not m:
            return "[page: give a page id, e.g. ```page p17```]"
        idx, rest = int(m.group(1)), m.group(2).strip()
        if not (0 < idx < len(self.history)) or not isinstance(self.history[idx].get("content"), str):
            return f"[page p{idx}: no such page]"
        lines = self.history[idx]["content"].split("\n")
        rx = re.match(r"^/(.+)/$", rest)
        if rx:
            try:
                pat = re.compile(rx.group(1), re.I)
            except re.error as ex:
                return f"[page p{idx}: bad regex: {ex}]"
            hits = [f"{n}: {ln[:300]}" for n, ln in enumerate(lines, 1) if pat.search(ln)]
            return (f"[page p{idx} /{rx.group(1)}/: {len(hits)} matching line(s)]\n"
                    + "\n".join(hits[:200]) + ("\n\u2026" if len(hits) > 200 else ""))
        rng = re.match(r"^(\d+)\s*-\s*(\d*)$", rest)
        a, b = (int(rng.group(1)), int(rng.group(2) or len(lines))) if rng else (1, len(lines))
        a, b = max(1, a), min(len(lines), b)
        chunk, n = [], 0
        for i in range(a, b + 1):
            ln = lines[i - 1]
            if n + len(ln) > 24000:
                chunk.append(f"\u2026 (more: ```page p{idx} {i}-{b}```)")
                break
            chunk.append(f"{i}: {ln}" if rng else ln)     # a range is numbered, to navigate by
            n += len(ln) + 1
        return f"[page p{idx}, lines {a}-{b} of {len(lines)}]\n" + "\n".join(chunk)

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

        got = {"chars": 0, "opened": False, "ended": False}

        def on_delta(chunk):
            got["chars"] += len(chunk or "")
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
            if now - self._last_paint < _STREAM_REPAINT or self._paint_pending:
                return                  # one repaint queued at a time: a slow one can't pile up
            self._last_paint = now
            self._paint_pending = True
            GLib.idle_add(self._paint_stream)

        def on_done():
            got["ended"] = True
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
            got["ended"] = True
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

        def opened(*_a):
            got["opened"] = True
            self._note_progress()

        def run_stream():
            self.backend.stream(send_msgs, on_delta, on_done, on_error, vision,
                                should_stop=stale, on_open=opened)
            if not got["ended"] and (got["opened"] or got["chars"]):
                # stopped mid-reply (Stop, the watchdog, another chat): the
                # request was still billed, so the meter and the cap count it
                used = getattr(self.backend, "last_model", None)
                GLib.idle_add(lambda: (self._account(sent_chars, got["chars"], vision,
                                                     usage={}, model=used), False)[1])
        threading.Thread(target=run_stream, daemon=True).start()

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
            # a restart repeats whole lines; a short mid-line overlap is
            # more likely a coincidence than a repeat
            if prefix.endswith(piece[:k]) and (k >= 40 or piece[k - 1] == "\n"):
                # a long overlap is a restart; but text that repeats itself
                # (rows of a CSV, a table) overlaps by nature: keep it
                ov = piece[:k]
                if not any(ov[n:] == ov[:-n] for n in range(1, k // 2 + 1)):
                    piece = piece[k:]
                break
        last = prefix.rsplit("\n", 1)[-1].strip()
        if piece and not piece.startswith("\n") and re.fullmatch(r"`{3,4}", last or "-"):
            # a fence that CLOSES a block ends its line; one that OPENS a
            # block ("```" + "write app.py") continues on the same line
            fences = sum(1 for ln in prefix.split("\n") if ln.strip().startswith("```"))
            if fences % 2 == 0:
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
        fspans = [(b["start"], b["end"]) for b in file_blocks(text)]
        outside = _cut_spans(text, _merge_spans(fspans + display_spans(text, fspans)))

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
        over_tools = []

        def capped(tag, n):
            got = grab(tag)
            if len(got) > n:
                over_tools.append(f"{len(got) - n} more ```{tag}```")
            return got[:n]
        searches = capped("search", CAP)
        fetches = capped("fetch", CAP)
        images = capped("images", CAP)
        vid_searches = capped("videos", CAP)     # search-and-show videos
        videos = capped("video", CAP)            # download by URL
        junk = bool(re.search(r"```junk", outside))
        skill_blocks = capped("skill", CAP)      # save a smart file
        runskills = capped("runskill", CAP)      # run a saved smart file
        reads = capped("read", CAP)              # read a file from disk by path
        greps = capped("grep", CAP)              # regex search in the working folder
        globs = capped("glob", CAP)              # find files by pattern
        asks = capped("ask", 1)                  # a question only the user can answer
        self._asked = bool(asks)
        briefs = capped("task", 8)               # parallel read-only sub-agents
        bgs = capped("bg", 3)                    # background jobs
        logss = capped("logs", 4)
        kills = capped("kill", 4)
        undo_req = has("undo")
        undo_arg = (grab("undo") or [""])[0]
        opens = capped("open", 3)
        shots = has("screenshot")               # see the screen
        looks = capped("look", 3)                # see an image file
        maps = has("map")                       # the repo map: files + what they define
        plans = grab("plan")[-1:]               # the live checklist (latest wins)
        remembers = capped("remember", CAP)      # store a durable fact
        forgets = capped("forget", CAP)          # prune a fact
        projects = capped("project", 2)        # open / switch a build workspace
        trees = has("tree")                    # show what's been built
        rmfiles = capped("rmfile", 4)           # remove a file from the project
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
        writes = [(b["path"], b["body"]) for b in fblocks if b["tag"] == "write"]
        edits = [(b["path"], b["body"]) for b in fblocks if b["tag"] == "edit"]
        patches_all = [b for b in fblocks if b["tag"] == "patch"]
        # what a reply asked for beyond the caps is reported, never silently dropped
        over_cap = ([f"write {p_}" for p_, _b in writes[12:]] + [f"edit {p_}" for p_, _b in edits[12:]]
                    + ["patch"] * max(0, len(patches_all) - 4))
        writes, edits = writes[:12], edits[:12]
        over_cap += over_tools
        fence_clash = [b["path"] for b in fblocks if b.get("ambiguous")]
        # what's left once file contents are cut out: code INSIDE a file being
        # written is never mistaken for a block to run
        fspans = [(b["start"], b["end"]) for b in fblocks]
        rest = _cut_spans(text, _merge_spans(fspans + display_spans(text, fspans)))
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
        readonly = self.agent_mode and not self.plan_mode and self._folder_access() == "readonly"
        if self.agent_mode and (self.plan_mode or readonly):
            # nothing changes until the user approves the plan
            plan_blocked = ([f"```{lang}```" for lang, _ in codes] + [f"write {p}" for p, _ in writes]
                            + [f"edit {p}" for p, _ in edits] + [f"bg {b[:30]}" for b in bgs]
                            + [f"rmfile {r}" for r in rmfiles]
                            + [t for t, _ in reg_calls if _TOOL_REG[t]["acts"]]
                            + (["runskill"] if (skill_blocks or runskills) else [])
                            + (["runtests"] if runtests else []) + (["package"] if packages else [])
                            + (["undo"] if undo_req else []) + (["kill"] if kills else []))
            if videos:
                plan_blocked.append("video download")
                videos = []
            codes, writes, edits, bgs, rmfiles, skill_blocks, runskills = [], [], [], [], [], [], []
            runtests = packages = junk = undo_req = False
            kills, projects = [], []
            reg_calls = [c for c in reg_calls if not _TOOL_REG[c[0]]["acts"]]
            if plans and not readonly:
                self._asked = True               # a proposed plan waits for the user
        proj_refused = bool(projects and self.agent_mode and self._folder_access() != "full")
        if proj_refused:
            projects = []                       # leaving would leave this folder's sandbox behind
        if fence_clash or self._step_forced:
            reg_calls = [c for c in reg_calls if not _TOOL_REG[c[0]]["acts"]]
        acting = bool(searches or fetches or images or vid_searches or videos or junk
                      or skill_blocks or runskills or reads or greps or codes or checks
                      or projects or writes or edits or trees or packages or runtests
                      or rmfiles or ledger_req or plans or globs or maps or shots or looks
                      or briefs or bgs or logss or kills or undo_req or opens or asks
                      or reg_calls or proj_refused)
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
        if (self.agent_mode and not blocked_agent and not self.plan_mode and not fence_clash
                and (codes or writes or edits or rmfiles or bgs or runskills or packages
                     or runtests or projects or undo_req or any(_TOOL_REG[t]["acts"] for t, _ in reg_calls))):
            self._checkpoint(self._actions_of(text) or "a step")
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
        # file actions in the order the reply wrote them: "undo, then edit"
        # must not undo the new edit
        seq = ([(b["start"], "write", b) for b in [x for x in fblocks if x["tag"] == "write"]
                [:len(writes)]]
               + [(b["start"], "edit", b) for b in [x for x in fblocks if x["tag"] == "edit"]
                  [:len(edits)]]
               + list(zip([m.start() for m in re.finditer(r"```rmfile(?![A-Za-z])", text)],
                          ["rmfile"] * len(rmfiles), rmfiles)))
        if undo_req:
            m_undo = re.search(r"```undo(?![A-Za-z])", text)
            seq.append((m_undo.start() if m_undo else len(text), "undo", undo_arg))
        for _pos, kind, arg in sorted(seq, key=lambda t: t[0]):
            if kind == "write":
                self._do_write(arg["path"], arg["body"])
            elif kind == "edit":
                self._do_edit(arg["path"], arg["body"])
            elif kind == "rmfile":
                self._do_rmfile(arg.strip())
            else:
                self._do_undo(arg)
        if over_cap and self.agent_mode:
            self._tool_feedback.append("[NOT applied (over the per-reply limit): "
                                       + ", ".join(over_cap[:12]) + " \u2014 send them in the "
                                       "next reply]")
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
        if plan_blocked and readonly:
            self._sys_note("\U0001F512 read-only folder \u2014 nothing was run or changed", "dim")
            self._tool_feedback.append(
                "[READ-ONLY FOLDER: not run: " + ", ".join(plan_blocked[:8]) + ". The user made "
                "this folder read-only: investigate with read-only tools and answer; say what "
                "you would change, or ask them to change its access (/access).]")
        elif plan_blocked:
            self._sys_note("\U0001F4CB plan mode \u2014 nothing was run or changed", "dim")
            self._tool_feedback.append(
                "[PLAN MODE: not run: " + ", ".join(plan_blocked[:8]) + ". Investigate with "
                "read-only tools, then post the ```plan```; the user approves it before you act.]")
        if proj_refused:
            self._tool_feedback.append(
                "[project NOT opened: this folder's access is restricted (/access), so Chuck "
                "can't move itself to another folder. Work here, or ask the user to open it.]")
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
        if feedback and self._asked:
            # it asked the user something: the turn waits for the answer, and
            # what the other tools found rides along with it (instead of going
            # straight back to the model, which then answers its own question)
            self._user_notes.append("results of the tools you ran with your question:\n"
                                    + _clip_feedback(feedback, 8000))
            feedback = []
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
        if not self._BROWSER_SLOTS.acquire(timeout=90):
            return "", "blocked; the browser was busy"
        try:
            if cancelled():
                return "", ""
            try:
                text, eng, still = _browser.read_page(
                    url, engine=self.settings.get("browser") or "auto", timeout=45)
            except Exception as ex:                       # no browser, or it failed
                return "", f"blocked; browser: {str(ex)[:80]}"
        finally:
            self._BROWSER_SLOTS.release()
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
                    try:
                        for fut in as_completed(futs, timeout=150):
                            if self._cancelled:
                                break
                            try:
                                r = fut.result()
                            except Exception:
                                r = None
                            if r:
                                out.append(r); got += 1
                    except TimeoutError:
                        pass                      # a page that never ends: answer with the rest
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
            # each part keeps its head and tail: a flat [:16000] dropped the
            # pages whenever a command's output came first
            msg = note + _clip_feedback(out, 16000)

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
        if _governor is not None:
            gv = Adw.ComboRow(); gv.set_title("Governor")
            gv.set_subtitle("Which model each agent runs on: performance (the chat model "
                            "everywhere), balanced (read-only work on the fast model), "
                            "powersave (the fast model everywhere)")
            gv.set_model(Gtk.StringList.new([m.capitalize() for m in _governor.MODES]))
            gv.set_selected(_governor.MODES.index(_governor.mode_of(S)))
            gv.connect("notify::selected",
                       lambda *_a: (changed("governor", _governor.MODES[gv.get_selected()]),
                                    self._update_subtitle()))
            g.add(gv)
            entry(g, "Fast model (blank: the cheapest suggested)", "fast_model", "")

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
        spin(g, "Daily budget for everything ($)", "budget_day_usd", 0, 10000, 0.5, 0, digits=2,
             sub="Every chat, agent, mission and daemon together. Over it, the chat asks and "
                 "agents stop. 0 = no limit. /budget shows the spend.")
        spin(g, "Monthly budget for everything ($)", "budget_month_usd", 0, 100000, 5, 0, digits=2,
             sub="The same, per calendar month. 0 = no limit.")
        spin(g, "Context sent per request (characters)", "context_chars", 40000, 800000,
             10000, SEND_CHAR_BUDGET,
             sub="How much conversation and file content rides along. More sees more "
                 "of a big repo at once; less is cheaper. ~4 characters per token.")

        g = group(pg, "Hooks",
                  "Your own commands around Chuck's work. Their failures go back to him to fix.")
        entry(g, "After each file write  ({path} = the file)", "hook_after_write", "")
        entry(g, "  \u2026only for files matching (e.g. *.py, *.ts)", "hook_after_write_glob", "*")
        entry(g, "Before he may finish  (e.g. pytest -q)", "hook_before_done", "")
        if _checkpoint is not None:
            switch(g, "Snapshot the folder before each step", "checkpoints", True,
                   sub="So /rewind can put files and the chat back, even after a command "
                       "changed them. Kept in a private repository; yours is never touched.")

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
        eng_keys = ["auto", "kokoro", "piper", "espeak"]
        eng.set_model(Gtk.StringList.new(["Best installed (Kokoro, then Piper, then espeak-ng)",
                                          "Kokoro \u2014 natural", "Piper", "espeak-ng (robotic)"]))
        eng.set_selected(eng_keys.index((S.get("voice_engine") or "auto").lower())
                         if (S.get("voice_engine") or "auto").lower() in eng_keys else 0)
        eng.connect("notify::selected", lambda *_a: changed(
            "voice_engine", eng_keys[int(eng.get_selected() or 0)]))
        g.add(eng)
        kv = Adw.ComboRow(); kv.set_title("Voice (Kokoro)")
        kv_keys = [k for k, _l in voice.KOKORO_CHOICES]
        kv.set_model(Gtk.StringList.new([lbl for _k, lbl in voice.KOKORO_CHOICES]))
        cur_v = S.get("kokoro_voice") or voice.DEFAULT_KOKORO_VOICE
        kv.set_selected(kv_keys.index(cur_v) if cur_v in kv_keys else 0)

        def pick_voice(*_a):
            changed("kokoro_voice", kv_keys[int(kv.get_selected() or 0)])
            if voice.kokoro_ready():
                speak("Here's how I sound. Ready when you are.", dict(S, voice_engine="kokoro"))
        kv.connect("notify::selected", pick_voice)
        g.add(kv)
        spin(g, "Speed", "voice_speed", 0.5, 2.0, 0.05, 1.0, digits=2)
        spin(g, "Pitch (espeak-ng)", "voice_pitch", 0, 99, 1, 28)
        spin(g, "Max characters read per reply", "voice_max_chars", 2000, 40000, 1000, 20000)
        entry(g, "Piper voice model (blank = installed voice)", "piper_model")
        def here(detail):
            # /doctor's pointers ("— Settings › Voice › Install …") make no
            # sense on the page that has the button
            return re.sub(r"\s*\u2014\s*(?:Settings \u203a .*|Install natural voice.*)$", "", detail)
        nv = Adw.ActionRow(); nv.set_title("Natural voice")
        nv.set_subtitle(_html.escape(
            here(voice.voice_status(S)[1]) + ("" if voice.kokoro_ready() else
                                               " \u00b7 Install adds Kokoro, a human-sounding "
                                               "voice (about 350 MB, runs on this machine)")))
        ib = Gtk.Button(label="Install" if not voice.kokoro_ready() else "Reinstall")
        ib.add_css_class("quick"); ib.set_valign(Gtk.Align.CENTER)
        vt = Gtk.Button(label="Test"); vt.add_css_class("quick"); vt.set_valign(Gtk.Align.CENTER)

        def voice_install(*_a):
            ib.set_sensitive(False); vt.set_sensitive(False)

            def prog(msg):
                GLib.idle_add(lambda: (nv.set_subtitle(_html.escape(msg + "\u2026")), False)[1])

            def work():
                ok, msg = voice.install_kokoro(prog)

                def fin():
                    nv.set_subtitle(_html.escape(msg))
                    ib.set_sensitive(True); vt.set_sensitive(True)
                    if ok:
                        speak("Natural voice ready. This is how I sound now.", S)
                    return False
                GLib.idle_add(fin)
            threading.Thread(target=work, daemon=True).start()

        def voice_test(*_a):
            vt.set_sensitive(False)
            nv.set_subtitle("testing\u2026")

            def work():
                eng_now = voice.engine_for(S)
                ok, msg = (voice.test_kokoro(S.get("kokoro_voice")) if eng_now == "kokoro"
                           else voice.test_piper() if eng_now == "piper"
                           else (bool(eng_now), voice.voice_status(S)[1]))

                def fin():
                    nv.set_subtitle(_html.escape(msg))
                    vt.set_sensitive(True)
                    if ok:
                        speak("Chuck Norris does not adjust settings. Settings adjust to "
                              "Chuck Norris.", S)
                    return False
                GLib.idle_add(fin)
            threading.Thread(target=work, daemon=True).start()
        ib.connect("clicked", voice_install); vt.connect("clicked", voice_test)
        nv.add_suffix(ib); nv.add_suffix(vt); g.add(nv)
        if _listen is not None:
            g2 = group(pg, "Voice input", "Talk to Chuck with the mic button or Ctrl+M. Speech is "
                       "recognised on this machine (Moonshine); nothing is uploaded.")
            li = Adw.ActionRow(); li.set_title("Speech recognition")
            li.set_subtitle(_html.escape(
                "not installed \u00b7 about 250 MB, runs on this machine" if not _listen.ready()
                else here(_listen.status()[1])))
            lb = Gtk.Button(label="Install" if not _listen.ready() else "Reinstall")
            lb.add_css_class("quick"); lb.set_valign(Gtk.Align.CENTER)

            def input_install(*_a):
                lb.set_sensitive(False)

                def prog(msg):
                    GLib.idle_add(lambda: (li.set_subtitle(_html.escape(msg + "\u2026")), False)[1])

                def work():
                    ok, msg = _listen.install(prog)
                    GLib.idle_add(lambda: (li.set_subtitle(_html.escape(msg)),
                                           lb.set_sensitive(True), False)[2])
                threading.Thread(target=work, daemon=True).start()
            lb.connect("clicked", input_install)
            li.add_suffix(lb); g2.add(li)
            switch(g2, "Send what I say right away", "voice_autosend", False,
                   sub="Off: what you said lands in the composer for a look first.")
            spin(g2, "Give up if nothing is said within (s)", "mic_wait", 3, 30, 1, 8)
            entry(g2, "Microphone command (blank: automatic \u2014 parecord, pw-record, arecord)",
                  "mic_command", "")
            mt = Adw.ActionRow(); mt.set_title("Test the microphone")
            mt.set_subtitle("Records a second and says whether sound is coming in")
            mb = Gtk.Button(label="Test"); mb.add_css_class("quick"); mb.set_valign(Gtk.Align.CENTER)

            def mic_check(*_a):
                mb.set_sensitive(False)
                mt.set_subtitle("listening for a second\u2026")

                def work():
                    ok, text = _listen.mic_test()
                    GLib.idle_add(lambda: (mt.set_subtitle(_html.escape(("\u2713 " if ok else "\u2717 ") + text)),
                                           mb.set_sensitive(True), False)[2])
                threading.Thread(target=work, daemon=True, name="mic-test").start()
            mb.connect("clicked", mic_check)
            mt.add_suffix(mb); g2.add(mt)
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
                      "piper_model", "temperature", "max_tokens",
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
        # a second `chucknorris …` hands its arguments to this instance
        # (remote_command) and gets the answer printed in its own terminal
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        try:
            flags |= Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        except (AttributeError, TypeError):
            pass
        super().__init__(application_id=APP_ID, flags=flags)

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

    def do_command_line(self, cmdline):
        args = list(cmdline.get_arguments() or [])[1:]
        self.activate()
        win = self.props.active_window
        if win is None or not args:
            return 0
        try:
            cwd = cmdline.get_cwd()
        except Exception:
            cwd = None

        def out(text):
            try:
                cmdline.print_literal(str(text).rstrip("\n") + "\n")
            except Exception:
                pass
        try:
            return int(win.remote_command(args, cwd, out) or 0)
        except Exception as ex:
            out(f"error: {ex}")
            return 1

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
