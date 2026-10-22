import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Adversarial checks for the logic-audit fixes. Each block attacks one race
or edge the audit found, through the REAL window on the stub toolkit:

  * a stream from a stopped turn finishing late and landing in the next one;
  * checkpoints: undo, undo all, the Revert button, double reverts;
  * background jobs: logs, kill, duplicates, CRITICAL and sudo refusals,
    jobs that die at once, and jobs outliving the window;
  * Tab: flips the mode from the composer, never inside a dialog or a field;
  * trimming: never opens with an assistant turn, never touches the history;
  * nested fences: a README's own code is written, never executed.
"""
import tempfile
import threading
import time
from pathlib import Path

import gtkstub
from sim import cn, new_win, type_and_send, settle, check_finished, check_history, FAILS, bubbles

fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {label}")
    else:
        fails += 1
        print(f"  [FAIL] {label}  {detail}")


tmp = Path(tempfile.mkdtemp())
cn._builder.PROJECTS = tmp / "projects"
cn._builder.PROJECTS.mkdir()
cn._builder._STATE = cn._builder.PROJECTS / ".current"
cn.ChuckWindow.JOB_DIR = tmp / "jobs"


def hist_text(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


def agent(replies, folder=None):
    w = new_win(replies, tmp)
    w.set_agent_mode(True, announce=False)
    if folder:
        w.set_workspace(folder, announce=False)
    return w


def folder(name):
    d = tmp / name
    d.mkdir()
    return d


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- a late stream from a stopped turn never lands in the next one ---")


class LateBackend:
    """First call blocks until released, then answers; later calls answer at once."""
    def __init__(self, late, now):
        self.late, self.now = late, now
        self.release = threading.Event()
        self.calls = 0

    def key(self): return "test-key"
    def base(self): return "https://api.example.com/v1"

    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        self.calls += 1
        if self.calls == 1:
            self.release.wait(10)
            on_delta(self.late)          # deliberately ignores should_stop
            on_done()
            return
        on_delta(self.now)
        on_done()


for how in ("stop", "new chat"):
    w = agent([])
    be = LateBackend("OLD ANSWER from the stopped turn", "NEW ANSWER")
    w.backend = be
    w.entry.get_buffer().set_text("first question")
    w.on_send()
    if how == "stop":
        w.stop_run()
    else:
        w.new_chat()
    w.entry.get_buffer().set_text("second question")
    w.on_send()
    time.sleep(0.2)
    be.release.set()
    settle()
    h = hist_text(w)
    check("OLD ANSWER" not in h, f"after {how}: the old turn's late answer is dropped")
    check(h.count("NEW ANSWER") == 1, f"after {how}: the new answer arrives exactly once",
          f"{h.count('NEW ANSWER')}")
    check(not any("OLD ANSWER" in (x.get_text() or "") for x in w.msgbox.walk()
                  if isinstance(x.get_text(), str)),
          f"after {how}: and it never appears on screen")
    check_finished(w, f"late stream ({how})")
    check_history(w, f"late stream ({how})")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- checkpoints: undo, undo all, Revert ---")
ws = folder("ckpt")
(ws / "keep.txt").write_text("original\n")
w = agent(["```write new.txt\nfresh\n```",
           "```edit keep.txt\n<<<<<<< SEARCH\noriginal\n=======\nchanged\n>>>>>>> REPLACE\n```",
           "```undo```",
           "Undid the edit."], ws)
type_and_send(w, "make changes, then undo the last")
check((ws / "keep.txt").read_text() == "original\n", "```undo``` restores the edited file")
check((ws / "new.txt").exists(), "and only the LAST change — the new file stays")
check("restored keep.txt" in hist_text(w), "the model is told what was undone")

w.backend.replies = ["```undo all```", "All reverted."]
type_and_send(w, "undo everything")
check((ws / "new.txt").exists(),
      "```undo all``` only covers the current task, not earlier ones")

ws = folder("ckpt_all")
(ws / "a.txt").write_text("A\n")
w = agent(["```write a.txt\nA2\n```", "```write b.txt\nB\n```", "```write a.txt\nA3\n```",
           "```undo all```", "Back to how it was."], ws)
type_and_send(w, "change then undo all")
check((ws / "a.txt").read_text() == "A\n", "undo all unwinds in reverse: a.txt is back to A")
check(not (ws / "b.txt").exists(), "and the file it created is gone")

w.backend.replies = ["```undo```", "Nothing to undo."]
type_and_send(w, "undo again")
check("no file changes to revert" in hist_text(w), "undo with nothing left says so")

ws = folder("ckpt_btn")
w = agent(["```write note.txt\nhello\n```", "Wrote it."], ws)
type_and_send(w, "write a note")
revert = [x for x in w.msgbox.walk() if x.get_label() == "Revert"]
check(len(revert) == 1, "the file card has a Revert button", f"{len(revert)}")
if revert:
    revert[0].click()
    check(not (ws / "note.txt").exists(), "clicking Revert removes the new file")
    check(revert[0].get_label() == "Reverted", "and the button says so")
    check(any("user reverted" in n for n in w._user_notes),
          "the model will hear the user reverted it")
    ch = w._changes[-1]
    check("already reverted" in w._revert(ch), "a second revert is a no-op, not a crash")
check_finished(w, "checkpoints")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- background jobs ---")
ws = folder("jobs")
w = agent(["```bg web\nsleep 30\n```",
           "```bg web\nsleep 5\n```",
           "```logs web```",
           "```kill web```",
           "Started, checked, stopped."], ws)
type_and_send(w, "run a server, check it, stop it")
h = hist_text(w)
check("job 'web' is RUNNING" in h, "a long-running job is reported RUNNING")
check("already running" in h, "a second job with the same name is refused")
check("job 'web' RUNNING" in h, "logs report its state")
check("stopped 'web'" in h, "kill stops it")
check(not w._job_alive("web"), "and it is really gone")
check_finished(w, "jobs")

w = agent(["```bg\nbash -c 'echo boom; exit 3'\n```", "It failed."], ws)
type_and_send(w, "start a broken job")
check("EXITED immediately with code 3" in hist_text(w) and "boom" in hist_text(w),
      "a job that dies at once reports its code and output")

w = agent(["```bg wipe\nrm -rf /\n```", "Refused."], ws)
type_and_send(w, "wipe in the background")
check("bg refused" in hist_text(w) and "CRITICAL" in hist_text(w),
      "a CRITICAL command can't sneak past the gate as a background job")
check("wipe" not in w._jobs, "and no job was started")

w = agent(["```bg upd\nsudo sleep 1\n```", "Refused."], ws)
_ready = cn._sudo_ready
cn._sudo_ready = lambda: False
type_and_send(w, "background sudo")
cn._sudo_ready = _ready
check("needs root" in hist_text(w), "a sudo job with no cached password is refused, not hung")

w = agent(["```logs nope```", "No such job."], ws)
type_and_send(w, "logs of nothing")
check("no job 'nope'" in hist_text(w), "logs for an unknown job lists what exists")

w = agent(["```bg keep\nsleep 30\n```", "Running."], ws)
type_and_send(w, "start and leave")
job = w._jobs.get("keep")
check(job is not None and w._job_alive("keep"), "a job is running before the window closes")
w._on_close()
time.sleep(0.3)
check(job is not None and job["proc"].poll() is not None,
      "closing the window stops its background jobs")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- Tab flips the mode only where it should ---")
gtkstub.GdkNS.ModifierType.CONTROL_MASK = 4
gtkstub.GdkNS.ModifierType.ALT_MASK = 8


class FakeText(gtkstub.Widget):
    pass


gtkstub.GtkNS.Text = FakeText
TAB = cn.Gdk.KEY_Tab
w = agent([])
content = gtkstub.Widget()
w.get_content = lambda: content
w.get_visible_dialog = lambda: None
w.get_focus = lambda: w.entry
before = w.agent_mode
check(w._on_window_key(None, TAB, 0, 0) is True and w.agent_mode != before,
      "Tab in the composer flips Agent/Chat")
w._on_window_key(None, TAB, 0, 0)
check(w.agent_mode == before, "and back")
w.get_visible_dialog = lambda: gtkstub.Widget()
check(not w._on_window_key(None, TAB, 0, 0) and w.agent_mode == before,
      "inside Settings, Tab moves between fields instead")
w.get_visible_dialog = lambda: None
w.get_focus = lambda: FakeText()
check(not w._on_window_key(None, TAB, 0, 0) and w.agent_mode == before,
      "in the chat search field, Tab moves focus instead")
w.get_focus = lambda: w.entry
check(not w._on_window_key(None, TAB, 0, 1) and w.agent_mode == before,
      "Shift+Tab is left alone")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- trimming what is sent ---")
w = agent([])
big = "word " * 3000                                       # 15k characters
w.history = [w.history[0],
             {"role": "user", "content": "A " + big},
             {"role": "assistant", "content": "B " + big},
             {"role": "user", "content": "C " + big}]
snapshot = [dict(m) for m in w.history]
w.settings["context_chars"] = 40000
sent = w._trim_for_send(list(w.history))
w.settings.pop("context_chars", None)
check(sent[0]["role"] == "system", "the system prompt always rides along")
check(len(sent) > 1 and sent[1]["role"] == "user",
      "a cut that lands on an assistant turn drops it: the conversation opens with the user",
      f"{[m['role'] for m in sent]}")
check(w.history == snapshot, "the transcript itself is never trimmed")

w.history = [w.history[0]]
for i in range(10):
    w.history += [{"role": "user", "content": f"TOOL RESULTS #{i}\n" + "x" * 200},
                  {"role": "assistant", "content": f"step {i}"}]
sent = w._trim_for_send(list(w.history))
blobs = [m for m in sent if str(m.get("content", "")).startswith("TOOL RESULTS")]
check(len(blobs) == cn.TOOL_BLOBS_KEPT, "only the newest tool-result blobs are re-sent",
      f"{len(blobs)}")
check("TOOL RESULTS #9" in str(blobs[-1]["content"]), "and they are the newest ones")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- fences inside written files ---")
readme = ("```write README.md\n# Demo\n\nRun it:\n\n```bash\ntouch PWNED_BY_README\n```\n\n"
          "Done.\n```")
blocks = cn.file_blocks(readme)
check(len(blocks) == 1 and "touch PWNED_BY_README" in blocks[0]["body"]
      and blocks[0]["body"].rstrip().endswith("Done."),
      "a README with its own ```bash block is ONE file, whole")
ws = folder("fences")
w = agent([readme, "Wrote the README."], ws)
type_and_send(w, "write a readme")
check((ws / "README.md").exists() and "touch PWNED_BY_README" in (ws / "README.md").read_text(),
      "the README is written with its example intact")
check(not (ws / "PWNED_BY_README").exists() and not (Path.home() / "PWNED_BY_README").exists(),
      "and the example inside it was never executed")

four = "````write GUIDE.md\n# Guide\n```\nplain fence\n```\nend\n````"
b4 = cn.file_blocks(four)
check(len(b4) == 1 and b4[0]["body"] == "# Guide\n```\nplain fence\n```\nend\n",
      "a four-backtick block keeps bare inner fences verbatim")

ambiguous = "```write DOC.md\n# Doc\n```\nrm -rf important\n```\n"
ws = folder("ambiguous")
(ws / "important").mkdir()
w = agent([ambiguous, "I'll resend it with four backticks.",
           "````write DOC.md\n# Doc\n```\nrm -rf important\n```\n````", "Written."], ws)
type_and_send(w, "write the doc")
check((ws / "important").is_dir(), "an ambiguous doc block runs nothing")
check("FOUR backticks" in hist_text(w), "and the model is told how to resend it")
check((ws / "DOC.md").exists() and "rm -rf important" in (ws / "DOC.md").read_text(),
      "the resend with four backticks is written whole")
check_finished(w, "fences")

code = "```write app.py\nprint('a')\n```\n```bash\npython3 app.py\n```"
blocks = cn.file_blocks(code)
check(len(blocks) == 1 and blocks[0]["body"] == "print('a')\n",
      "a plain code write ends at its own fence; the command after it is separate")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- no folder means no folder ---")
old = cn._builder.PROJECTS / "old-project"
old.mkdir()
cn._builder._STATE.write_text(str(old))
w = agent([])
w.set_workspace(None, announce=False)
check(w._workroot() is None, "with 'No folder', file tools don't reuse an old session's project")
w.backend.replies = ["```write hello.txt\nhi\n```", "Wrote it."]
type_and_send(w, "write hello")
check(not (old / "hello.txt").exists(), "nothing is written into the old project")
check(w.workspace is not None and (w.workspace / "hello.txt").exists(),
      "the file goes to a scratch folder, which the chip then shows")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- the tetris run: write a page, screenshot it, look at it ---")
ws = folder("tetris")
page = "<!doctype html>\n<html><body><canvas id=c></canvas><script>let x = 1;</script></body></html>\n"
reply = (f"Building it.\n```write {ws}/tetris.html\n{page}```\n"
         f"```bash\necho shot > {ws}/shot.txt\n```\n```look {ws}/shot.png```")
w = agent([reply, "Now looking.", "Looks right."], ws)
type_and_send(w, "make a html tetris game")
h = hist_text(w)
check("couldn't tell where" not in h and "NOTHING was written" not in h,
      "a page written beside a command and a look is not flagged as ambiguous")
check((ws / "tetris.html").read_text() == page,
      "an absolute path inside the working folder is accepted, file written whole")
check((ws / "shot.txt").exists(), "the command in the same reply ran")
check("held back" in h, "the look that would have raced the command is held, and the model told")
check_finished(w, "tetris run")

print("\n--- a stray fence never dumps a file's code into the chat ---")
page = "<!doctype html>\n<html><body><script>\nfunction loop(t){ drawBoard(); }\n</script></body></html>"
# the model's stray closing fence after its narration: an HTML page has no
# fences of its own, so the page is written and the stray fence is dropped
ws = folder("stray-after")
w = agent([f"Writing it.\n```write tetris.html\n{page}\n```\n\nWritten. Now render it.\n```",
           "Done."], ws)
type_and_send(w, "make tetris")
check((ws / "tetris.html").read_text() == page + "\n", "the page is written whole, first time")
shown = " ".join(bubbles(w))
check("Written. Now render it." in shown and "Now render it.\n```" not in shown,
      "the narration shows, the stray ``` doesn't")
check(all(sg[0] != "code" for b in bubbles(w) if "Written" in b for sg in cn.md_segments(b)),
      "no empty code card")
check_finished(w, "stray after")
# a stray fence right after the header: an EMPTY file is never written, and
# the page between the fences never shows up in the chat as prose
ws = folder("stray-before")
w = agent([f"Writing it.\n```write tetris.html\n```\n{page}\n```\n\nWritten.\n```",
           f"````write tetris.html\n{page}\n````", "Done."], ws)
type_and_send(w, "make tetris")
shown = " ".join(bubbles(w))
prose = [b for b in bubbles(w) if b.startswith("Writing it.")]
check(prose and not any("drawBoard" in b or "<html" in b for b in prose),
      "the page's code is not dumped into the chat")
check("couldn't tell" not in shown and "send it again" in shown, "a calm note, not an alarm")
check((ws / "tetris.html").read_text() == page + "\n", "the resend is written whole")
check_finished(w, "stray before")
print("\n--- the chat reads in order and stays tidy ---")
import struct, zlib


def tiny_png(path):
    raw = b"\x00\xff\x00\x00"
    png = (b"\x89PNG\r\n\x1a\n" + b"".join(
        struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        for t, d in ((b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
                     (b"IDAT", zlib.compress(raw)), (b"IEND", b""))))
    Path(path).write_bytes(png)


ws = folder("layout")
tiny_png(ws / "wide.png"); tiny_png(ws / "narrow.png")
folds = []
w = agent([f"Building it.\n```write page.html\n{page}\n```\nWritten. Now looking at it.",
           "```look wide.png```\n```look narrow.png```", "Both look right."], ws)
real_diff = w._diff_view
w._diff_view = lambda lines, expanded=True, **k: (folds.append(expanded), real_diff(lines, expanded, **k))[1]
type_and_send(w, "make a page")
kinds = [(e["kind"], (e["data"] or {}).get("text", "")[:30]) for e in w._log
         if e["kind"] in ("bot", "pinned", "gallery")]
i_card = next((i for i, k in enumerate(kinds) if k[0] == "pinned"), -1)
i_head = next((i for i, k in enumerate(kinds) if k[1].startswith("Building it.")), -1)
i_tail = next((i for i, k in enumerate(kinds) if k[1].startswith("Written. Now looking")), -1)
check(-1 < i_head < i_card < i_tail, "what Chuck says after a file block shows UNDER its card",
      str(kinds))
check(not any("Written. Now" in t for k, t in kinds[:max(i_card, 0)]), "and not above it")
gal = [e for e in w._log if e["kind"] == "gallery"]
check(len(gal) == 1 and [it["label"] for it in gal[0]["data"]["items"]] == ["wide.png", "narrow.png"],
      "two looks share ONE labelled gallery card", str([e["data"] for e in gal]))
check(not any(e["kind"] == "note" and "looking at" in str(e["data"].get("text", "")) for e in w._log),
      "no separate 'looking at' lines")
check(folds and folds[0] is False, "a NEW file's diff starts folded (no wall of code)")
check_finished(w, "layout")
e = w._log_add("bot", {"text": "Thinking\u2026", "rich": False, "meta": "", "cont": True})
w._collapse_window(scroll=False)
check(e.get("tools_w") is not None and not e["tools_w"].get_visible(),
      "no play/copy buttons under a 'Thinking…' placeholder")
w._bot_entry = e
w._set_bot_text("A real answer.", rich=True)
check(e["tools_w"].get_visible(), "they come back once there is a real answer")
ws = folder("edit-miss")
(ws / "a.py").write_text("x = 1\n")
w = agent(["```edit a.py\n<<<<<<< SEARCH\ny = 2\n=======\ny = 3\n>>>>>>> REPLACE\n```", "Done."], ws)
type_and_send(w, "fix it")
shown = " ".join(bubbles(w))
check("he'll re-read it and retry" in shown and "quote the current lines" not in shown,
      "a missed edit reads as plain English; the model still gets the exact reason")
check("quote the current lines" in hist_text(w), "(the model's feedback is unchanged)")

print("\n--- a restricted folder can't plant code for git to run later ---")
import shutil as _sh
import subprocess as _sp
check(cn.open_in_brave("--renderer-cmd-prefix=touch /tmp/x") is False
      and cn.open_in_brave("file:///etc/passwd") is False, "the browser is only ever handed a web address")
if _sh.which("bwrap") and _sh.which("git"):
    ws = folder("gitguard")
    _sp.run(["git", "init", "-q", str(ws)], check=True)
    cfg0 = (ws / ".git" / "config").read_text()
    fresh = ws / "fresh"
    w = agent([
        "```bash\nprintf '#!/bin/sh\\necho pwn\\n' > .git/hooks/pre-commit; "
        "git config core.fsmonitor 'touch PWNED'; echo done\n```",
        "```bash\nmkdir fresh && cd fresh && git init -q && printf '#!/bin/sh\\ntouch PWNED\\n' > "
        ".git/hooks/pre-commit && chmod +x .git/hooks/pre-commit && git config core.fsmonitor 'touch PWNED'"
        " && git config alias.st '!touch PWNED' && git config user.name Chuck\n```",
        "```write .git/hooks/post-checkout\n#!/bin/sh\ntouch PWNED\n```",
        "```bash\ncat ~/.git-credentials; ls ~/.mozilla; echo end\n```",
        "Done."], ws)
    (Path.home() / ".git-credentials").write_text("https://me:TOKEN-SECRET@github.com\n")
    (Path.home() / ".mozilla" / "firefox").mkdir(parents=True, exist_ok=True)
    (Path.home() / ".mozilla" / "firefox" / "cookies.sqlite").write_text("COOKIE-SECRET")
    check(w.set_access("sandbox"), "sandboxed")
    type_and_send(w, "try it")
    h = hist_text(w)
    check(not (ws / ".git" / "hooks" / "pre-commit").exists(), "an existing repo's hooks are read-only")
    check((ws / ".git" / "config").read_text() == cfg0, "and so is its config")
    hk = fresh / ".git" / "hooks"
    check(not (hk / "pre-commit").exists() and (hk / "pre-commit.disabled-by-chuck").exists(),
          "a hook planted in a repo made during the run is disarmed")
    fc = (fresh / ".git" / "config").read_text()
    check("# disabled by Chuck: fsmonitor" in fc and "# disabled by Chuck: st" in fc
          and "name = Chuck" in fc, "program-running git settings are switched off; plain ones stay", fc)
    check("Chuck disarmed" in h, "and the model is told why")
    check(not (ws / ".git" / "hooks" / "post-checkout").exists() and "inside .git" in h,
          "Chuck's own write tool refuses .git in a restricted folder")
    check("TOKEN-SECRET" not in h and "cookies.sqlite" not in h and "firefox" not in h,
          "token files and browser profiles are hidden from sandboxed commands")
    check_finished(w, "git guard")

print("\n--- a damaged chat file opens safely and never poisons later saves ---")
import json as _json
w = agent(["Hi."])
cd_ = cn.CHATS_DIR
for _nm, _data in (("list", [1, 2]), ("null", None), ("hist-str", {"history": "nope"}),
                   ("bad-items", {"title": 5, "history": [None, 3, {"role": "user"}, {"content": "x"},
                                                         {"role": "assistant", "content": "fine"}]}),
                   ("escape", {"ts": "../../evil", "history": [{"role": "user", "content": "hi"}]})):
    _p = cd_ / f"bad-{_nm}.json"
    _p.write_text(_json.dumps(_data))
    w._load_chat(_p)
    check(all(isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") for m in w.history),
          f"chat file '{_nm}' loads as clean history")
_mem = cn._memory if hasattr(cn, "_memory") else None
if _mem is not None:
    _mem.FACTS.parent.mkdir(parents=True, exist_ok=True)
    _mem.FACTS.write_text('5\n["x"]\n{"no": "text"}\nnot json\n{"text": "a fact with nothing else"}\n')
    _mem.remember("the build uses meson")
    check(any("meson" in str(f) for f in _mem.all_facts())
          and isinstance(_mem.recall("how do we build it"), (str, list)),
          "a damaged memory line is skipped; memory still saves and recalls")
type_and_send(w, "still works?")
check(not (cd_.parent.parent / "evil.json").exists() and not list(cd_.parent.glob("*evil*")),
      "a chat's id from the file can't point a save outside the chats folder")
check_finished(w, "damaged chats")

print("\n--- open: a picture's name can't smuggle a launcher ---")
ws = folder("openlink")
(ws / "evil.desktop").write_text("[Desktop Entry]\nExec=touch PWNED\n")
os.symlink("evil.desktop", ws / "photo.png")
tiny_png(ws / "real.png")
opened = []
_real_open = cn._open_path
cn._open_path = lambda p: (opened.append(p), True)[1]
w = agent(["```open photo.png```", "```open real.png```", "Done."], ws)
w.set_access("sandbox")
type_and_send(w, "show me")
cn._open_path = _real_open
check(not any("photo.png" in str(p) or "evil" in str(p) for p in opened),
      "a .png that links to a .desktop launcher isn't opened in a restricted folder", opened)
check(any("real.png" in str(p) for p in opened), "a real picture still opens")
check_finished(w, "open link")

print("\n--- privacy: the crash log and Chuck's folders ---")
from chucknorris_ext import crashlog as _cl
_cl.record("api", "HTTP 401 with Authorization: Bearer sk-abcdefghijklmnop1234 at /v1?api_key=SECRET1")
_logtxt = _cl.log_path().read_text()
check("sk-abcdefghijklmnop1234" not in _logtxt and "SECRET1" not in _logtxt and "[redacted]" in _logtxt,
      "a key or token in an error never reaches the crash log")
check(os.stat(_cl.log_path()).st_mode & 0o077 == 0, "and the log is readable by you only")
os.chmod(cn.DATA_DIR, 0o755)
cn._private_dirs()
check(os.stat(cn.DATA_DIR).st_mode & 0o077 == 0 and os.stat(cn.CONFIG_DIR).st_mode & 0o077 == 0,
      "Chuck's data and config folders (chats, memory, checkpoints) are private at startup")

# a markdown doc stays strict: its own bare fences may be content
amb = cn.file_blocks("```write DOC.md\n# Doc\n```\nrm -rf x\n```\nmore\n```")
check(len(amb) == 1 and amb[0]["ambiguous"] and amb[0]["end"] == len(
    "```write DOC.md\n# Doc\n```\nrm -rf x\n```\nmore\n```"),
      "an ambiguous doc block hides everything up to its last loose fence")

w = agent([f"```write {tmp}/outside.txt\nno\n```", "Refused."], folder("abs_out"))
type_and_send(w, "write outside")
check(not (tmp / "outside.txt").exists() and "give a path inside the project" in hist_text(w),
      "an absolute path OUTSIDE the working folder is still refused")

print("\n--- commands run like they would in a terminal ---")
rc, out = cn.run_command("ulimit -v; ulimit -u; ulimit -f; python3 -c 'import threading;"
                         "[threading.Thread(target=lambda:None).start() for _ in range(600)];"
                         "print(\"spawned\")'")
lines = out.split()
check(rc == 0 and "spawned" in out, "a command may start 600 threads (the old cap was 512 total)",
      out[-200:])
hard_u = __import__("resource").getrlimit(__import__("resource").RLIMIT_NPROC)[0]
want_u = "unlimited" if hard_u == __import__("resource").RLIM_INFINITY else str(hard_u)
check(lines[:1] == ["unlimited"], "no address-space cap that would kill Chromium or Node",
      lines[:1])
check(lines[1:2] == [want_u], "no process cap of our own (the desktop's tasks count against it)",
      f"{lines[1:2]} vs inherited {want_u}")
check(lines[2:3] == ["unlimited"], "no file-size cap that kills big downloads", lines[2:3])

# ═══════════════════════════════════════════════════════════════════════════
print(f"\nTOTAL AUDIT FAILURES: {fails + len(FAILS)}")
assert fails == 0 and not FAILS, "audit regressions present"
print("ALL AUDIT TESTS PASSED")
