import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Chuck builds real things: project → write → verify → test → package → deliver.

The bar here is not "he emitted some code". It is: files exist on disk, the
tests he wrote actually ran, the zip contains the right things, and the tool he
built genuinely works when executed.
"""
import tempfile, subprocess, zipfile
from pathlib import Path
from sim import cn, new_win, type_and_send, check_history, check_finished, fail, FAILS
import chucknorris_ext.builder as B

tmp = Path(tempfile.mkdtemp())
B.PROJECTS = tmp / "proj"; B.PROJECTS.mkdir(parents=True)
B._STATE = B.PROJECTS / ".current"

print("--- sandbox: hostile paths are refused, not rewritten ---")
p, _ = B.project_open("sandbox")
for evil in ["../../../../tmp/pwned.txt", "/etc/passwd", "a/../../../escape.py",
             "....//....//x", "~/.ssh/id_rsa", "C:\\win\\x", "a/./b", ""]:
    ok, msg, path = B.write_file(p, evil, "X")
    if ok:
        fail(f"accepted hostile path {evil!r}")
    if path and not str(Path(path).resolve()).startswith(str(Path(p).resolve())):
        fail(f"ESCAPED the project with {evil!r}")
if Path("/tmp/pwned.txt").exists():
    fail("wrote outside the sandbox")
for good in ["main.py", "lib/util.py", "a/b/c/deep.txt", "run.sh", "docs/README.md"]:
    ok, msg, _ = B.write_file(p, good, "content\n")
    if not ok:
        fail(f"rejected a legitimate path {good!r}: {msg}")
junk = [f for f in Path(p).rglob("*") if "etc" in str(f) or "~" in str(f)]
if junk:
    fail(f"junk directories created: {junk}")
print("  escapes refused, normal paths fine, tree clean")

print("--- shell scripts come out executable ---")
B.write_file(p, "go.sh", "#!/usr/bin/env bash\necho hi\n")
if not os.stat(Path(p) / "go.sh").st_mode & 0o111:
    fail("shell script not made executable")
print("  chmod +x applied")

print("--- FULL BUILD CHAIN through the real app ---")
replies = [
    "```project\nwordcount\n```",
    "```write\nwordcount.py\nimport sys\n\n\ndef count_words(text):\n"
    "    return len(text.split())\n\n\ndef main(argv):\n    if len(argv) < 2:\n"
    "        print('usage: wordcount.py <file>')\n        return 2\n    try:\n"
    "        with open(argv[1], encoding='utf-8') as fh:\n"
    "            print(count_words(fh.read()))\n    except OSError as e:\n"
    "        print('error: %s' % e)\n        return 1\n    return 0\n\n\n"
    "if __name__ == '__main__':\n    sys.exit(main(sys.argv))\n```\n"
    "```write\ntests/test_wordcount.py\nimport sys, os\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
    "from wordcount import count_words\nassert count_words('a b c') == 3\n"
    "assert count_words('') == 0\nprint('PASSED')\n```\n"
    "```write\nREADME.md\n# wordcount\n\nRun: `python3 wordcount.py FILE`\n```",
    "```runtests\n```",
    "```tree\n```\n```package\n```",
    "Done.",
]
w = new_win(replies, tmp)
type_and_send(w, "build me a cli tool that counts words in a file")
proj = B.current_project()
if proj is None:
    fail("no project was created")
else:
    for need in ("wordcount.py", "tests/test_wordcount.py", "README.md"):
        if not (proj / need).is_file():
            fail(f"missing file: {need}")
    print("  files on disk:", sorted(str(f.relative_to(proj))
                                     for f in proj.rglob("*")
                                     if f.is_file() and "__pycache__" not in str(f)))

print("--- the tests he wrote were actually RUN ---")
tr = [m for m in w.history if isinstance(m.get("content"), str) and "project tests" in m["content"]]
if not tr or "PASSED" not in tr[0]["content"]:
    fail("project tests did not really run")
else:
    print("  test output came back from a real subprocess")

print("--- packaged into a clean deliverable ---")
zips = list(B.PROJECTS.glob("wordcount-*.zip"))
if not zips:
    fail("no zip produced")
else:
    names = zipfile.ZipFile(zips[0]).namelist()
    inner = sorted(n.split("/", 1)[1] for n in names if "/" in n)
    if any("__pycache__" in n or n.endswith(".pyc") for n in names):
        fail("build artefacts leaked into the zip")
    if "wordcount.py" not in inner:
        fail(f"zip missing the tool: {inner}")
    print("  zip contents:", inner)

print("--- THE BUILT TOOL ACTUALLY WORKS ---")
if proj and (proj / "wordcount.py").is_file():
    sample = tmp / "s.txt"; sample.write_text("one two three four five")
    r = subprocess.run([sys.executable, str(proj / "wordcount.py"), str(sample)],
                       capture_output=True, text=True)
    if r.stdout.strip() != "5":
        fail(f"built tool gave {r.stdout.strip()!r}, expected '5'")
    r2 = subprocess.run([sys.executable, str(proj / "wordcount.py")],
                        capture_output=True, text=True)
    if r2.returncode != 2:
        fail(f"usage path returned {r2.returncode}, expected 2")
    print("  counts correctly, and exits 2 with no args")

print("--- a deliverable card was pinned in the chat ---")
if not [e for e in w._log if e["pinned"]]:
    fail("no deliverable card shown to the user")
check_history(w, "build"); check_finished(w, "build")

print("--- broken code is refused on write, with a fix request ---")
w2 = new_win(["```project\nbroken\n```\n```write\nbad.py\ndef oops(:\n    pass\n```", "ok"], tmp)
type_and_send(w2, "build something")
bad = [m for m in w2.history if isinstance(m.get("content"), str)
       and "does not verify" in m["content"]]
if not bad:
    fail("a syntactically broken file was written without complaint")
else:
    print("  verifier caught it and asked for a rewrite")

print("--- failing project tests are reported honestly ---")
p3, _ = B.project_open("failing")
B.write_file(p3, "tests/test_x.py", "assert 1 == 2, 'boom'\n")
rc, out = B.run_tests(p3)
if rc == 0 or ("boom" not in out and "AssertionError" not in out):
    fail(f"failing tests not reported (rc={rc})")
print("  failure surfaced with the real assertion message")

print("--- a project with no tests says so rather than pretending ---")
p4, _ = B.project_open("notests")
B.write_file(p4, "main.py", "print(1)\n")
rc4, out4 = B.run_tests(p4)
if rc4 != -1:
    fail(f"expected 'no tests' sentinel, got rc={rc4}")
print(" ", out4)

print()
print("TOTAL BUILDER FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "builder regressions"
print("ALL BUILDER TESTS PASSED")
