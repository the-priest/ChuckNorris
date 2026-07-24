import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Real system work: no hangs, read the whole output, react to failure.

Three bugs this locks shut, all of which only bite on a real machine:
  - a command that asks a question (pacman's "Proceed? [Y/n]") had stdin
    inherited, so it could block until the 30-minute timeout;
  - command output was truncated from the FRONT, throwing away the error at
    the end of a failing build — he'd read a failure as a success;
  - a non-zero exit was reported as a bare number, easy to sail past.
"""
import time, tempfile
from pathlib import Path
from sim import cn, new_win, type_and_send, check_finished, fail, FAILS
import chucknorris_ext.specs as specs

print("--- a command that asks a question cannot hang the app ---")
t0 = time.time()
rc, out = cn.run_command('echo -n "Proceed with installation? [Y/n] "; read a; echo "[$a]"')
el = time.time() - t0
print(f"  rc={rc} in {el:.2f}s -> {out[:48]!r}")
if el > 5:
    fail(f"an interactive prompt blocked for {el:.0f}s")

print("--- a pager cannot stall a command ---")
t0 = time.time()
rc, out = cn.run_command("printf 'line\\n%.0s' $(seq 1 400) | cat")
if time.time() - t0 > 5:
    fail("output through a pager stalled")
print(f"  400 lines in {time.time() - t0:.2f}s, {len(out)} chars")

print("--- non-interactive env is set for the tools that need it ---")
rc, out = cn.run_command("echo PAGER=$PAGER TERM=$TERM GIT_TERMINAL_PROMPT=$GIT_TERMINAL_PROMPT")
for want in ("PAGER=cat", "TERM=dumb", "GIT_TERMINAL_PROMPT=0"):
    if want not in out:
        fail(f"{want} not set for commands")
print(" ", out.strip())

print("--- the ERROR at the end of a long build survives truncation ---")
big = "\n".join(f"compiling module_{i}.c ... ok" for i in range(800))
big += "\nmodule.c:42: error: undefined reference to 'foo'\nmake: *** [Makefile:17] Error 1"
clip = cn.clip_output(big)
print(f"  {len(big)} chars -> {len(clip)} kept")
if "undefined reference" not in clip or "Error 1" not in clip:
    fail("the error at the end was truncated away")
if not clip.startswith("compiling module_0"):
    fail("the start of the output was lost")
if "trimmed from the middle" not in clip:
    fail("truncation isn't declared, so he can't tell something was dropped")

print("--- short output is passed through untouched ---")
if cn.clip_output("all good") != "all good":
    fail("short output was mangled")

print("--- a failure is reported as a FAILURE, not a number ---")
rep = cn._run_report("`make`", 2, "fatal: nope")
if "FAILED" not in rep or "do not carry on" not in rep:
    fail("a non-zero exit isn't clearly flagged as a failure")
print(" ", rep.split("\n")[0][:78])
if "SUCCEEDED" not in cn._run_report("`ls`", 0, "file"):
    fail("success isn't stated clearly")
for code, word in ((127, "isn't installed"), (126, "executable"), (124, "needs input")):
    if word not in cn._run_report("`x`", code, ""):
        fail(f"exit {code} has no useful hint")
print("  exit 124/126/127 each get a specific hint")

print("--- package operations get --noconfirm; queries never do ---")
cases = [("sudo pacman -Syu firefox", True), ("pacman -S neovim", True),
         ("paru -S aurpkg", True), ("sudo pacman -Rns oldpkg", True),
         ("pacman -Ss firefox", False), ("pacman -Qtdq", False),
         ("pacman -Si vim", False), ("pacman -Syu --noconfirm x", False),
         ("ls -la", False), ("echo pacman", False)]
for c, want in cases:
    if (cn.add_noconfirm(c) != c) != want:
        fail(f"--noconfirm wrong for {c!r} -> {cn.add_noconfirm(c)!r}")
print(f"  {len(cases)}/{len(cases)} correct")

print("--- installs are forced through -Syu ---")
for src, want in [("sudo pacman -S firefox", "sudo pacman -Syu firefox"),
                  ("sudo pacman -Sy firefox", "sudo pacman -Syu firefox"),
                  ("pacman -Ss x", "pacman -Ss x"),
                  ("pacman -Rns x", "pacman -Rns x")]:
    if cn.enforce_syu(src) != want:
        fail(f"-Syu wrong: {src!r} -> {cn.enforce_syu(src)!r}")
print("  bare -S and -Sy both corrected; queries and removals untouched")

print("--- the careful-work playbooks trigger on real phrasings ---")
for q, group in [("my wifi stopped working", "fixit"),
                 ("sound is broken", "fixit"),
                 ("it wont boot after the update", "fixit"),
                 ("nvidia driver no longer loads", "fixit"),
                 ("install firefox", "sysadmin"),
                 ("upgrade the kernel", "sysadmin"),
                 ("enable the ssh service", "sysadmin")]:
    got = [g for g, _ in specs.specs_for(q)]
    if group not in got:
        fail(f"{q!r} didn't load the {group} playbook (got {got})")
for q in ("whats the weather", "write me a poem"):
    if any(g in ("fixit", "sysadmin") for g, _ in specs.specs_for(q)):
        fail(f"{q!r} wrongly loaded a system playbook")
print("  fixit/sysadmin fire on real problems, stay quiet otherwise")

print("--- the playbooks actually teach the discipline ---")
text = " ".join(t for g, t in specs.specs_for("my system is broken and wont boot"))
for must in ("DIAGNOSE FIRST", "ONE CHANGE AT A TIME", "undo", "verify"):
    if must.lower() not in text.lower():
        fail(f"the fix playbook never mentions {must!r}")
sysd = " ".join(t for g, t in specs.specs_for("install a package and edit fstab"))
for must in ("-Syu", "back it up", "already installed"):
    if must.lower() not in sysd.lower():
        fail(f"the sysadmin playbook never mentions {must!r}")
print("  diagnose-first, one-change-at-a-time, backup and verify are all in there")

print("--- a failing command still ends the turn cleanly ---")
_tmp = Path(tempfile.mkdtemp())
w = new_win(["```bash\nexit 3\n```", "That failed — here's why."], _tmp)
type_and_send(w, "run something that fails")
cards = [e for e in w._log if e["pinned"]]
if not cards:
    fail("no card for a command that will fail")
check_finished(w, "failing command")

print()
print("TOTAL SYSTEM-WORK FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "system-work regressions"
print("ALL SYSTEM WORK TESTS PASSED")
