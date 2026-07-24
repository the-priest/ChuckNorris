import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Chuck ACTS. He runs commands and verifies they worked.

He is an agent, not a suggestion box: a shell block executes immediately and
the real exit code and output come back. A non-zero exit is reported as a
failure he must fix before taking another step — he never gets to claim a
success he hasn't seen. The single exception is the CRITICAL tier, which still
waits for a deliberate confirmation.
"""
import tempfile
from pathlib import Path
from sim import cn, new_win, type_and_send, check_finished, fail, FAILS

tmp = Path(tempfile.mkdtemp())


def hist(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


print("--- a normal command runs with no click ---")
w = new_win(["Checking.\n```bash\nuname -r\n```", "That's your kernel."], tmp)
type_and_send(w, "what kernel am i on")
if "SUCCEEDED (exit 0)" not in hist(w):
    fail("a plain command did not auto-execute")
if len(w.backend.calls) != 2:
    fail(f"expected the result to drive a follow-up turn, got {len(w.backend.calls)} calls")
check_finished(w, "autorun")
print("  ran, got exit 0 back, and continued on its own")

print("--- the real stdout comes back, not an invention ---")
w = new_win(["```bash\necho CHUCK_WAS_HERE\n```", "done"], tmp)
type_and_send(w, "echo")
if "CHUCK_WAS_HERE" not in hist(w):
    fail("real stdout was not fed back")
print("  actual process output reached the model")

print("--- a failing command stops the march ---")
w = new_win(["```bash\nexit 3\n```", "I see."], tmp)
type_and_send(w, "run something broken")
h = hist(w)
if "FAILED (exit 3)" not in h:
    fail("a non-zero exit was not reported as a failure")
if "Do NOT move on" not in h:
    fail("he wasn't told to fix it before continuing")
print("  exit 3 surfaced as FAILED, with an explicit stop")

print("--- a command that writes is actually observable on disk ---")
probe = tmp / "probe.txt"
w = new_win([f"```bash\necho written > {probe}\n```", "done"], tmp)
type_and_send(w, "write a file")
if not probe.exists():
    fail("the command did not really run on the filesystem")
else:
    print(f"  {probe.name} exists with: {probe.read_text().strip()!r}")

print("--- python blocks run too ---")
w = new_win(["```python\nprint(6*7)\n```", "42."], tmp)
type_and_send(w, "6*7?")
if "42" not in hist(w) or "SUCCEEDED" not in hist(w):
    fail("python block did not execute")
print("  code executed and returned its real output")

print("--- CRITICAL still waits for a deliberate tick ---")
w = new_win(["```bash\nrm -rf / --no-preserve-root\n```", "ok"], tmp)
type_and_send(w, "wipe it")
h = hist(w)
if "RAN `rm -rf /" in h or "SUCCEEDED (exit 0)" in h:
    fail("A CRITICAL COMMAND AUTO-EXECUTED")
btns = [x for e in w._log if e["pinned"] for x in e["w"].walk() if x.get_label() == "Run"]
if not btns:
    fail("no Run button offered for the critical command")
elif btns[0].get_sensitive():
    fail("the critical command's Run button was armed")
else:
    print("  held back, button disarmed, model told it is waiting")
if "CRITICAL" not in h:
    fail("the model was not told the command is pending confirmation")

print("--- installs are still forced through -Syu when auto-running ---")
w = new_win(["```bash\nsudo pacman -S cowsay\n```", "done"], tmp)
type_and_send(w, "install cowsay")
if "-Syu" not in hist(w):
    fail("a bare -S was auto-run without being corrected")
print("  bare -S rewritten before it executed")

print("--- one command per reply still holds ---")
w = new_win(["```bash\necho one\n```\n```bash\necho two\n```\n```bash\necho three\n```", "done"], tmp)
type_and_send(w, "do three things")
# count real EXECUTIONS, not the model's own text (which lists all three)
executed = sum(str(m.get("content", "")).count("SUCCEEDED (exit 0)")
               for m in w.history)
if executed != 1:
    fail(f"{executed} commands ran from one reply; expected exactly 1")
else:
    print("  exactly one of three executed; the rest were held back")

print()
print("TOTAL AUTONOMY FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "autonomous execution regressions"
print("ALL AUTONOMY TESTS PASSED")
