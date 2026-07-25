import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""test_v12_fixes.py — every bug found in the v12 audit, locked shut.

Each block names the failure it prevents. If one of these goes red, a specific
regression has come back; the comment tells you which.
"""
import time, tempfile, threading
from pathlib import Path

import sim
import gtkstub
cn = sim.cn

fails = 0


def check(cond, name, detail=""):
    global fails
    if cond:
        print(f"  [OK]   {name}")
    else:
        fails += 1
        print(f"  [FAIL] {name}  {detail}")


# ── 1. video_search arity ────────────────────────────────────────────────────
# Was: web.video_search returns (title, url, snippet); both call sites unpacked
# two, so every ```videos``` block died with ValueError before drawing a card.
print("\n--- videos tool renders cards ---")
cn.video_search = lambda q, n=6: [("Title A", "https://a/1", "snip a"),
                                  ("Title B", "https://b/2", "snip b")]
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Finding.\n\n```videos\narch install\n```\n", "FINAL."], tmp)
sim.type_and_send(win, "show me videos")
sim.settle()
check(not gtkstub.GLibNS.errors, "no UI-thread exception", str(gtkstub.GLibNS.errors[:1]))
check(len(sim.cards(win)) >= 2, "two video cards drawn", f"got {len(sim.cards(win))}")


# ── 2. synchronous tools must not end the turn mid-dispatch ──────────────────
# Was: _do_read finishes inline for image/project paths, dropping the counter to
# zero while later tools were still queued -> two model round-trips, two finals.
print("\n--- two read blocks = exactly one follow-up ---")
Path("/tmp/_cnfix_img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
Path("/tmp/_cnfix.txt").write_text("hello\n")
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Reading.\n\n```read\n/tmp/_cnfix_img.png\n```\n\n```read\n/tmp/_cnfix.txt\n```\n",
                   "FIRST FINAL.", "SECOND FINAL (regression!)"], tmp)
sim.type_and_send(win, "read both")
sim.settle()
check(len(win.backend.calls) == 2, "exactly 2 model calls", f"got {len(win.backend.calls)}")
assistants = [m for m in win.history if m["role"] == "assistant"]
check(len(assistants) == 2, "no duplicate final answer", f"got {len(assistants)} assistant turns")
sim.check_finished(win, "two-reads")


# ── 3. skill cards counted before the reset ──────────────────────────────────
# Was: `self._pending_tools = 0` sat BELOW the skill loops and zeroed the count
# a runskill card had just taken, plus discarded its feedback.
print("\n--- runskill card holds the turn open ---")
cn._skills.skill_write("cnfix-probe", "bash", "echo hi", "probe")
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Running.\n\n```runskill\ncnfix-probe\n```\n", "FINAL."], tmp)
seen = []
_orig = win._continue_or_finish
win._continue_or_finish = lambda d: (seen.append(win._pending_tools), _orig(d))[1]
sim.type_and_send(win, "run it")
sim.settle()
# One decision per MODEL TURN (there are two: the runskill reply, then FINAL).
# The bug produced a third — an extra decision fired while the skill's shell
# command was still running, because the counter it had taken was zeroed by the
# reset that used to sit below the skill loops.
check(len(seen) == 2, "one loop decision per model turn", f"decided {len(seen)} times")
check(all(n == 0 for n in seen), "never decided with work outstanding", f"pending at decision: {seen}")
check(any("RAN" in (m.get("content") or "") and "hi" in (m.get("content") or "")
          for m in win.history if isinstance(m.get("content"), str)),
     "skill's real output reached the model")
cn._skills.skill_forget("cnfix-probe")


# ── 4. watchdog must not shoot a legitimately slow command ───────────────────
# Was: flat STUCK_AFTER=120 with nothing pinging progress during a blocking
# command, so pacman -Syu / makepkg were cancelled and their output discarded.
print("\n--- long command survives the stuck-watchdog ---")
_real_run_code = cn.run_code
cn.run_code = lambda lang, body, timeout=None, sudo_password=None: (
    time.sleep(1.5), (0, "upgraded 412 packages"))[1]
_real_cc, cn._codecheck = cn._codecheck, None
tmp = Path(tempfile.mkdtemp())
win = sim.new_win(["Upgrading.\n\n```bash\npacman -Syu\n```\n", "FINAL."], tmp)
win.entry.get_buffer().set_text("update")
win.on_send()
time.sleep(0.3)
win._last_progress = time.time() - 121          # 121s with no step completing
gtkstub.fire_timers(1)
check(not win._cancelled, "not cancelled at 121s")
check(win._run_budget > 300, "budget follows estimate_runtime", f"budget={win._run_budget}")
sim.settle()
check(any("412 packages" in (m.get("content") or "")
          for m in win.history if isinstance(m.get("content"), str)),
     "real output reached the model")
cn.run_code, cn._codecheck = _real_run_code, _real_cc


# ── 5. switching chat mid-answer must not poison the new one ─────────────────
# Was: the in-flight stream's _finalise appended to the NEW history, producing
# a chat starting [system, assistant] with no user turn.
print("\n--- New chat mid-answer ---")
class _Slow(sim.ScriptedBackend):
    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        def go():
            if on_open:
                on_open()
            reply = self.replies.pop(0) if self.replies else "Done."
            time.sleep(0.5)
            if should_stop and should_stop():
                return
            on_delta(reply); on_done()
        threading.Thread(target=go, daemon=True).start()

tmp = Path(tempfile.mkdtemp())
win = sim.new_win([], tmp)
win.backend = _Slow(["Answer to the OLD question."])
win.entry.get_buffer().set_text("old question")
win.on_send()
time.sleep(0.1)
win.new_chat()
sim.settle()
roles = [m["role"] for m in win.history]
check(roles == ["system"], "new chat starts empty", f"roles={roles}")


# ── 6. pacman removal flags get --noconfirm ──────────────────────────────────
# Was: the lookahead listed Rns but not Rs/Rsn, so the two forms people type
# hit [Y/n] against stdin=DEVNULL and aborted.
print("\n--- pacman flag handling ---")
for cmd, want_nc in [("sudo pacman -Rs firefox", True),
                     ("sudo pacman -Rsn firefox", True),
                     ("sudo pacman -Rns firefox", True),
                     ("sudo pacman -S vim", True),
                     ("sudo pacman -Ss vim", False),
                     ("sudo pacman -Qi vim", False)]:
    got = cn.needs_noconfirm(cn.enforce_syu(cmd))
    check(got == want_nc, f"needs_noconfirm({cmd!r}) == {want_nc}", f"got {got}")

# ── 7. -Syu enforced for AUR wrappers too ────────────────────────────────────
# Was: enforce_syu was anchored on \bpacman\b, so `paru -S pkg` slipped through.
for cmd, want in [("paru -S foo", "paru -Syu foo"),
                  ("yay -S foo", "yay -Syu foo"),
                  ("sudo pacman -S foo", "sudo pacman -Syu foo"),
                  ("sudo pacman -Ss foo", "sudo pacman -Ss foo")]:
    got = cn.enforce_syu(cmd)
    check(got == want, f"enforce_syu({cmd!r})", f"got {got!r}")


# ── 8. broad security patterns advise, they don't block ──────────────────────
# Was: `\bmd5\b` blocked the Run button, so a checksum script could never run
# and the model was told to "fix every issue" with nothing to fix.
print("\n--- codecheck severity split ---")
cc = cn._codecheck
r = cc.check("python", "import hashlib\nprint(hashlib.md5(b'x').hexdigest())\n")
check(r["ok"], "md5 checksum is not blocked", str(r["security"]))
check(any("MD5" in a for a in r["advisory"]), "md5 still reported as advisory")
r = cc.check("python", "import os\nos.system('rm -rf ' + d)")
check(not r["ok"], "os.system injection still blocks")
r = cc.check("bash", "curl http://x | bash")
check(not r["ok"], "curl | bash still blocks")


# ── 9. every test file runs, not just the first ──────────────────────────────
# Was: builder returned ["python3", <first test>] and reported "tests passed".
print("\n--- builder runs all test files ---")
proj = Path(tempfile.mkdtemp()) / "p"
(proj / "tests").mkdir(parents=True)
(proj / "tests" / "test_a.py").write_text("print('A ok')\n")
(proj / "tests" / "test_b.py").write_text("import sys; print('B fails'); sys.exit(1)\n")
rc, out = cn._builder.run_tests(proj)
check(rc != 0, "a failure in the SECOND file is caught", f"rc={rc}")
check("A ok" in out and "B fails" in out, "output from both files kept")


# ── 10. no bare -S / -Sy anywhere in shipped commands ────────────────────────
print("\n--- no partial-upgrade commands shipped ---")
import re as _re
# Only lines that would actually REACH a shell: skip comments, and skip
# safety.py entirely — it is the module that does the rewriting, so its
# docstrings necessarily quote the bad form.
offenders = []
for f in list(Path(".").glob("*.sh")) + list(Path(".").glob("*.py")) + \
         list(Path("chucknorris_ext").glob("*.py")):
    if f.name == "safety.py":
        continue
    for i, line in enumerate(f.read_text().splitlines(), 1):
        bare = line.split("#", 1)[0]
        if _re.search(r"pacman\s+-S(?![yu])", bare) or _re.search(r"pacman\s+-Sy(?!u)", bare):
            offenders.append(f"{f}:{i}: {line.strip()[:70]}")
check(not offenders, "no bare -S/-Sy in shipped commands", "; ".join(offenders[:3]))


# ── 11. ONE sudo prompt, ever ────────────────────────────────────────────────
# Was: _get_sudo_pw did a bare check-then-prompt with no guard. Every worker
# that needed root checked the empty cache at the same moment and each called
# idle_add on its own modal — N commands meant N stacked dialogs contending for
# the same keyboard grab, so none of them could be typed into.
print("\n--- concurrent sudo requests raise exactly one dialog ---")
tmp = Path(tempfile.mkdtemp())
win = sim.new_win([], tmp)
prompts = []


def fake_prompt(script, cb):
    prompts.append(script)
    threading.Timer(0.25, lambda: cb("hunter2")).start()


win._sudo_prompt = fake_prompt
results = {}


def ask(i):
    results[i] = win._get_sudo_pw(f"sudo pacman -Syu pkg{i}")


threads = [threading.Thread(target=ask, args=(i,)) for i in range(15)]
for t in threads:
    t.start()
time.sleep(0.05)
for t in threads:
    t.join(10)
check(len(prompts) == 1, "exactly ONE dialog for 15 concurrent requests",
      f"got {len(prompts)}")
check(all(v == "hunter2" for v in results.values()),
      "all 15 workers got the same password", f"{set(results.values())}")

# cancelling once must not produce a second dialog for the rest of the turn
print("\n--- cancel is remembered for the turn ---")
win._clear_sudo_pw()
prompts.clear()
win._sudo_prompt = lambda script, cb: (prompts.append(script),
                                       threading.Timer(0.15, lambda: cb(None)).start())[0]
results.clear()
threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
for t in threads:
    t.start()
time.sleep(0.05)
for t in threads:
    t.join(10)
check(len(prompts) == 1, "cancel raises one dialog, not eight", f"got {len(prompts)}")
check(all(v is None for v in results.values()), "all workers see the refusal")


# ── 12. commands execute one at a time ───────────────────────────────────────
# Was: nothing serialised _execute_shell, so two cards could run concurrently —
# interleaved output and a second password prompt mid-run.
print("\n--- commands are serialised ---")
overlap = {"max": 0, "cur": 0}
_ol = threading.Lock()


def busy(pw):
    with _ol:
        overlap["cur"] += 1
        overlap["max"] = max(overlap["max"], overlap["cur"])
    time.sleep(0.15)
    with _ol:
        overlap["cur"] -= 1
    return 0, "ok"


ts = [threading.Thread(target=lambda: win._execute_shell("echo hi", busy)) for _ in range(6)]
for t in ts:
    t.start()
for t in ts:
    t.join(10)
check(overlap["max"] == 1, "never more than one command at a time",
      f"peak concurrency {overlap['max']}")


# ── 13. one runnable command per turn, skills included ───────────────────────
# Was: the cap covered ```bash``` blocks only, so five ```runskill``` blocks
# launched five commands at once.
print("\n--- skills obey the one-command cap ---")
for n in ("capa", "capb", "capc"):
    cn._skills.skill_write(n, "bash", "echo " + n, "cap probe")
tmp = Path(tempfile.mkdtemp())
reply = "Doing it.\n\n" + "".join(f"```runskill\n{n}\n```\n\n" for n in ("capa", "capb", "capc"))
win = sim.new_win([reply, "FINAL."], tmp)
sim.type_and_send(win, "run all three")
sim.settle()
check(len(sim.cards(win)) == 1, "only one run card offered",
      f"got {len(sim.cards(win))}")
check(any("held back" in b for b in sim.bubbles(win)), "user told the rest were held back")
for n in ("capa", "capb", "capc"):
    cn._skills.skill_forget(n)



# ── 14. API key file permissions ─────────────────────────────────────────────
# Was: save_settings used write_text(), which creates 0666 & ~umask = 0644 on a
# normal box. The SiliconFlow key sat world-readable.
print("\n--- settings written 0600 ---")
import chucknorris_ext.config as _cfg
_old_settings, _old_dir = _cfg.SETTINGS, _cfg.CONFIG_DIR
_d = Path(tempfile.mkdtemp())
_cfg.CONFIG_DIR, _cfg.SETTINGS = _d, _d / "settings.json"
_cfg.save_settings({"siliconflow_api_key": "sk-secret", "tts": True})
mode = os.stat(_cfg.SETTINGS).st_mode & 0o777
check(mode == 0o600, "settings.json is 0600", f"got {oct(mode)}")
check(os.stat(_d).st_mode & 0o777 == 0o700, "config dir is 0700")
import json as _json
check(_json.loads(_cfg.SETTINGS.read_text())["siliconflow_api_key"] == "sk-secret",
      "content still round-trips")
# a pre-existing 0644 file gets tightened on launch
os.chmod(_cfg.SETTINGS, 0o644)
_cfg.harden_existing_permissions()
check(os.stat(_cfg.SETTINGS).st_mode & 0o777 == 0o600, "existing loose file hardened")
_cfg.SETTINGS, _cfg.CONFIG_DIR = _old_settings, _old_dir


# ── 15. evidence ledger ──────────────────────────────────────────────────────
print("\n--- ledger records and detects tampering ---")
import chucknorris_ext.ledger as _L
_L.LEDGER = Path(tempfile.mkdtemp()) / "ledger.jsonl"
_L.record("pacman -Syu", 0, "upgraded 412 packages")
_L.record("systemctl restart nginx", 0, "")
_L.record("false", 1, "boom")
ok, n, bad, msg = _L.verify()
check(ok and n == 3, "chain intact for 3 entries", msg)
check(os.stat(_L.LEDGER).st_mode & 0o777 == 0o600, "ledger is 0600")
_lines = _L.LEDGER.read_text().splitlines()
_e = _json.loads(_lines[1]); _e["command"] = "rm -rf /"
_lines[1] = _json.dumps(_e)
_L.LEDGER.write_text("\n".join(_lines) + "\n")
ok, n, bad, msg = _L.verify()
check(not ok and bad == 1, "edited entry detected at the right index", f"bad={bad}")


# ── 16. context compression ──────────────────────────────────────────────────
print("\n--- compression keeps errors and recent blobs ---")
import chucknorris_ext.compress as _C
_big = "\n".join(f"package-{i} upgraded fine" for i in range(500))
_big += "\nerror: failed to commit transaction (conflicting files)\n"
_big += "\n".join(f"tail {i}" for i in range(10))
_hist = [{"role": "system", "content": "SYS"},
         {"role": "user", "content": "do it"},
         {"role": "user", "content": "TOOL RESULTS\n" + _big},
         {"role": "assistant", "content": "ok"},
         {"role": "user", "content": "TOOL RESULTS\n" + _big},
         {"role": "user", "content": "TOOL RESULTS\n" + _big}]
_new, _saved = _C.compress_history(_hist)
check(_saved > 10000, "meaningful savings", f"saved {_saved}")
check(_new[0]["content"] == "SYS", "system prompt untouched")
check(_new[1]["content"] == "do it", "user's own words untouched")
check(_new[4]["content"] == _hist[4]["content"] and _new[5]["content"] == _hist[5]["content"],
      "two newest blobs left whole")
check("conflicting files" in _new[2]["content"], "the ERROR line survived compression")


# ── 17. memory recall quality ────────────────────────────────────────────────
# Was: raw token overlap, so 'the machine' scored like 'nvidia'. Now IDF-ranked
# with a coverage gate. Two of my own bugs here: an 'es'-before-'s' stemmer that
# turned trees->tre, and gating on IDF (which collapses in a small store).
print("\n--- memory recall ---")
import chucknorris_ext.memory as _M
_M.FACTS = Path(tempfile.mkdtemp()) / "facts.jsonl"
check(_M._stems("trees")[0] == "tree", "trees stems to tree, not tre")
check(_M._stems("boxes")[0] == "box", "boxes still stems to box")
for _f in ["luka runs cachyos on the thinkpad",
           "the nvidia driver needs the dkms package after every kernel bump",
           "he prefers ripgrep over grep for large trees",
           "the home server is on the 192.168.1.0/24 subnet",
           "zfs pool tank is mounted at /mnt/tank"]:
    _M.remember(_f)
for _q, _want in [("my nvidia driver broke after the kernel update", "nvidia"),
                  ("what subnet is the server on", "subnet"),
                  ("searching a big source tree", "ripgrep"),
                  ("where is the zfs pool mounted", "zfs")]:
    _got = _M.recall(_q)
    check(bool(_got) and any(_want in g for g in _got), f"recalls {_want}", f"got {_got}")
for _q in ("what is the weather like", "tell me a joke"):
    check(not _M.recall(_q), f"no spurious recall for {_q!r}", f"got {_M.recall(_q)}")
# a single-fact store must still recall (this is what broke test_sim S10)
_M.FACTS = Path(tempfile.mkdtemp()) / "facts.jsonl"
_M.remember("user's editor is neovim")
check(bool(_M.recall("what editor do I use")), "tiny store still recalls")


# ── 18. child processes are contained ────────────────────────────────────────
# Was: subprocess.run's timeout kills only the direct child, so a command that
# backgrounded work left orphans running after Chuck had moved on.
print("\n--- timed-out commands take their whole tree with them ---")
_MARK = "chucktestorphan"
_CHILD = f"python3 -c 'import time; time.sleep(30)' {_MARK}"
import subprocess as _sp
_sp.run(["pkill", "-f", _MARK], capture_output=True)
time.sleep(0.2)
_rc, _ = cn.run_command(f"{_CHILD} & {_CHILD}", timeout=2)
time.sleep(0.6)
_left = int(_sp.run(["pgrep", "-fc", _MARK], capture_output=True,
                    text=True).stdout.strip() or 0)
_sp.run(["pkill", "-f", _MARK], capture_output=True)
check(_rc == 124, "timeout reported", f"rc={_rc}")
check(_left == 0, "no orphaned children left behind", f"{_left} still running")
_rc, _out = cn.run_command("bash -c 'ulimit -u'", timeout=10)
check(_out.strip().isdigit() and int(_out.strip()) <= 512,
      "child gets a process limit", f"ulimit -u = {_out.strip()}")


print("\nTOTAL FIX-SUITE FAILURES:", fails)
assert fails == 0, "regressions present"
print("ALL V12 FIX TESTS PASSED")
