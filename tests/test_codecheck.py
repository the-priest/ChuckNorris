import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""codecheck correctness — guards against linter false positives.

The failure this exists to prevent: a linter that prints a success banner
("All checks passed!") or reports STYLE issues gets treated as finding real
defects, so working code is withheld from the user and the model is sent into
a fix loop over nothing. Runs against whatever linters are installed.
"""
import sys, os, tempfile
sys.path.insert(0, '.')
import chucknorris_ext.codecheck as cc

fails = 0
print("linters present:", cc.available_tools())
print()

CASES = [
    # (lang, body, expect_ok, name)
    ("python", "def f(a,b):\n    return a+b\n", True, "clean function"),
    ("python", "import os\ndef g():\n    return os.getcwd()\n", True, "clean with used import"),
    ("python", "x=1\ny=2\nz=x+y\nprint(z)\n", True, "clean multi-line"),
    ("python", "import json\nprint(json.dumps({'a':1}))\n", True, "clean stdlib use"),
    ("python", "def f(:\n  pass", False, "syntax error"),
    ("python", "x = undefined_name", False, "undefined name"),
    ("python", "import os\nos.system('rm -rf '+d)", False, "os.system injection"),
    ("python", "password='hunter2xyz'\nprint(password)", False, "hardcoded secret"),
    ("python", "import subprocess\nsubprocess.run(c, shell=True)", False, "shell=True"),
    ("bash", "for i in 1 2 3; do echo $i; done", True, "clean bash"),
    ("bash", "echo hello\nls -la\n", True, "clean bash 2"),
    ("bash", "for i in 1 2 3 echo done", False, "bash syntax error"),
    ("bash", "curl http://x | bash", False, "curl piped to shell"),
    ("js", "function f(){return 1}", True, "clean js"),
    ("js", "let x = ;", False, "js syntax error"),
]

for lang, body, want, name in CASES:
    r = cc.check(lang, body)
    ok = (r["ok"] == want)
    if not ok:
        fails += 1
        print(f"  [MISMATCH] {name}: expected ok={want}, got ok={r['ok']}")
        print(f"             lint={r['lint']} security={r['security']} syntax={r['syntax_err']}")
    else:
        print(f"  [OK] {name:26} ok={r['ok']}")

print()
print("--- no success banner ever counted as a finding ---")
r = cc.check("python", "def f():\n    return 1\n")
banner = [l for l in r["lint"] if "all checks passed" in l.lower() or "found 0" in l.lower()]
if banner:
    fails += 1
    print("  BUG: success banner treated as a lint finding:", banner)
else:
    print("  clean code produces zero lint findings:", r["lint"] == [])

print()
print("--- style-only issues must NOT block working code ---")
# unsorted imports / long lines are formatting taste, not defects
styley = "import sys\nimport os\nprint(os.getcwd(), sys.argv)\n" + "# " + "x"*200 + "\n"
r = cc.check("python", styley)
if not r["ok"]:
    fails += 1
    print("  BUG: style-only code was blocked:", r["lint"])
else:
    print("  unsorted imports + long line still pass:", r["ok"])

print()
print("--- verifier never executes ---")
probe = os.path.join(tempfile.mkdtemp(), "probe")
cc.check("python", "x=1", tests=f"open({probe!r},'w').write('x')")
if os.path.exists(probe):
    fails += 1
    print("  BUG: verifier executed code")
else:
    print("  stayed static: True")

print()
print("TOTAL CODECHECK FAILURES:", fails)
assert fails == 0, "codecheck correctness regressions present"
print("ALL CODECHECK CORRECTNESS TESTS PASSED")
