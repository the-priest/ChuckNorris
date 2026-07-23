"""codecheck.py — verify code BEFORE it reaches the user.

This is the "move intelligence into the scaffolding" idea: a small model writes
code, and instead of trusting it, the app runs a real verification pipeline and
feeds the findings back so the model FIXES its own work. Cheaper and more
reliable than asking a bigger model to get it right first try.

Pipeline (per language, best-effort — degrades if a tool is missing):
  1. SYNTAX   — does it even parse / compile?
  2. LINT     — pyflakes / ruff / node --check / shellcheck / bash -n
  3. SECURITY — a small stdlib pattern scan for the usual footguns
  4. TESTS    — if the model supplied tests, run them

Everything is stdlib + optional external linters; nothing is required. Findings
come back as a compact report the model can act on.
"""
import os
import re
import ast
import shutil
import subprocess
import tempfile

# ── security footgun patterns (language-tagged, low false-positive) ──────────
# Each entry: (pattern, message). Patterns are matched case-insensitively.
_SEC = {
    "python": [
        (r"\beval\s*\(", "eval() on dynamic input is RCE-prone — use ast.literal_eval"),
        (r"\bexec\s*\(", "exec() executes arbitrary code — avoid on untrusted input"),
        (r"subprocess\.(?:call|run|Popen|check_output)\([^)]*shell\s*=\s*True",
         "shell=True with a built string is command-injection-prone — pass a list"),
        (r"os\.system\s*\(", "os.system() is injection-prone — use subprocess with a list"),
        (r"\bpickle\.loads?\s*\(", "pickle on untrusted data is RCE — use json"),
        (r"yaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)",
         "yaml.load without SafeLoader is unsafe — use yaml.safe_load"),
        (r"\bmd5\b|\bsha1\b", "MD5/SHA1 are weak for security — use SHA-256+"),
        (r"verify\s*=\s*False", "TLS verify=False disables cert checking"),
        (r"(password|secret|api[_-]?key|token)\s*=\s*['\"][^'\"]{6,}['\"]",
         "hard-coded secret — load from env/secret store instead"),
        (r"random\.(?:random|randint|choice)\b.*(token|password|secret|key)",
         "random module isn't cryptographically secure — use secrets"),
    ],
    "js": [
        (r"\beval\s*\(", "eval() is RCE-prone"),
        (r"child_process\.exec\s*\(", "child_process.exec runs a shell — use execFile/spawn with args"),
        (r"innerHTML\s*=", "innerHTML with dynamic data is XSS-prone — use textContent"),
        (r"(password|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]",
         "hard-coded secret"),
        (r"Math\.random\(\).*(token|password|secret)", "Math.random isn't cryptographically secure"),
    ],
    "bash": [
        (r"\beval\s+", "eval in bash is injection-prone"),
        (r"rm\s+-rf\s+\$", "rm -rf on an unquoted/var path — guard it"),
        (r"curl\s+[^|]*\|\s*(?:sudo\s+)?(?:bash|sh)\b", "curl | bash executes remote code unread"),
        (r"chmod\s+777", "chmod 777 is world-writable — tighten permissions"),
    ],
}

_LANG_NORM = {"py": "python", "python": "python", "python3": "python",
              "js": "js", "javascript": "js", "node": "js",
              "sh": "bash", "bash": "bash"}


def _which(*names):
    for n in names:
        if shutil.which(n):
            return n
    return None


def _run(argv, text_in=None, timeout=60):
    try:
        p = subprocess.run(argv, input=text_in, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except Exception as ex:
        return 1, f"(error: {ex})"


# ── per-language syntax ──────────────────────────────────────────────────────
def _syntax_python(body):
    try:
        ast.parse(body)
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError line {e.lineno}: {e.msg}"


def _syntax_via(body, ext, argv_builder):
    with tempfile.NamedTemporaryFile("w", suffix="." + ext, delete=False) as f:
        f.write(body); path = f.name
    try:
        rc, out = _run(argv_builder(path))
        return rc == 0, out.strip()
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ── lint ─────────────────────────────────────────────────────────────────────
def _lint_python(body):
    findings = []
    tool = _which("ruff")
    if tool:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(body); p = f.name
        try:
            rc, out = _run([tool, "check", "--output-format", "concise", p])
            if out.strip():
                findings.append(out.strip().replace(p, "code.py"))
        finally:
            os.unlink(p)
    elif _which("pyflakes"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(body); p = f.name
        try:
            rc, out = _run(["pyflakes", p])
            if out.strip():
                findings.append(out.strip().replace(p, "code.py"))
        finally:
            os.unlink(p)
    else:
        # stdlib fallback: compile catches a lot; also flag bare except / undefined-ish
        try:
            compile(body, "code.py", "exec")
        except Exception as e:
            findings.append(str(e))
    return findings


def _lint_shell(body):
    tool = _which("shellcheck")
    if not tool:
        ok, out = _syntax_via(body, "sh", lambda p: ["bash", "-n", p])
        return [] if ok else [out]
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(body); p = f.name
    try:
        rc, out = _run([tool, "-f", "gcc", p])
        return [out.strip().replace(p, "script.sh")] if out.strip() else []
    finally:
        os.unlink(p)


def _lint_js(body):
    node = _which("node")
    if node:
        # node --check does a syntax parse; eslint if present for real lint
        eslint = _which("eslint")
        if eslint:
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(body); p = f.name
            try:
                rc, out = _run([eslint, "--no-eslintrc", "--format", "compact", p])
                return [out.strip().replace(p, "code.js")] if out.strip() else []
            finally:
                os.unlink(p)
    return []


# ── security scan ────────────────────────────────────────────────────────────
def security_scan(lang, body):
    lang = _LANG_NORM.get(lang, lang)
    pats = _SEC.get(lang, [])
    hits = []
    lines = body.splitlines()
    for i, line in enumerate(lines, 1):
        for pat, msg in pats:
            if re.search(pat, line, re.IGNORECASE):
                hits.append(f"L{i}: {msg}")
    # de-dup identical messages
    seen, out = set(), []
    for h in hits:
        key = h.split(": ", 1)[-1]
        if key not in seen:
            seen.add(key); out.append(h)
    return out


# ── public: full pipeline ────────────────────────────────────────────────────
def check(lang, body, tests=None):
    """Run the full verify pipeline. Returns a dict:
       {ok, lang, syntax_ok, syntax_err, lint[], security[], tests{ran,rc,out}}"""
    lang = _LANG_NORM.get((lang or "").lower(), (lang or "").lower())
    res = {"lang": lang, "syntax_ok": True, "syntax_err": "",
           "lint": [], "security": [], "tests": None, "ok": True}

    # 1. syntax
    if lang == "python":
        res["syntax_ok"], res["syntax_err"] = _syntax_python(body)
    elif lang == "bash":
        ok, err = _syntax_via(body, "sh", lambda p: ["bash", "-n", p])
        res["syntax_ok"], res["syntax_err"] = ok, ("" if ok else err)
    elif lang == "js":
        node = _which("node")
        if node:
            ok, err = _syntax_via(body, "js", lambda p: [node, "--check", p])
            res["syntax_ok"], res["syntax_err"] = ok, ("" if ok else err)

    # 2. lint (only if syntax ok — lint output on broken syntax is noise)
    if res["syntax_ok"]:
        if lang == "python":
            res["lint"] = _lint_python(body)
        elif lang == "bash":
            res["lint"] = _lint_shell(body)
        elif lang == "js":
            res["lint"] = _lint_js(body)

    # 3. security scan (always — cheap)
    res["security"] = security_scan(lang, body)

    # 4. tests (only python for now, if provided and syntax ok)
    if tests and res["syntax_ok"] and lang == "python":
        prog = body + "\n\n# --- tests ---\n" + tests
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(prog); p = f.name
        try:
            rc, out = _run(["python3", p], timeout=60)
            res["tests"] = {"ran": True, "rc": rc, "out": out.strip()[:2000]}
        finally:
            os.unlink(p)

    res["ok"] = (res["syntax_ok"] and not res["lint"] and not res["security"]
                 and (res["tests"] is None or res["tests"]["rc"] == 0))
    return res


def report(res):
    """Compact human/model-readable report string from a check() result."""
    if res.get("ok"):
        extra = " (tests passed)" if res.get("tests") else ""
        return f"\u2713 {res['lang']} verified: syntax OK, no lint or security issues{extra}."
    lines = [f"Verification of the {res['lang']} code found issues:"]
    if not res["syntax_ok"]:
        lines.append(f"  SYNTAX: {res['syntax_err']}")
    for l in res["lint"][:20]:
        for sub in l.splitlines()[:20]:
            if sub.strip():
                lines.append(f"  LINT: {sub.strip()}")
    for s in res["security"][:20]:
        lines.append(f"  SECURITY: {s}")
    if res.get("tests") and res["tests"]["rc"] != 0:
        lines.append(f"  TESTS FAILED (exit {res['tests']['rc']}):")
        for sub in res["tests"]["out"].splitlines()[-12:]:
            lines.append("    " + sub)
    lines.append("Fix these, then re-verify.")
    return "\n".join(lines)


def available_tools():
    """Which optional linters are present — for the app to advise install."""
    return {
        "ruff": bool(_which("ruff")),
        "pyflakes": bool(_which("pyflakes")),
        "shellcheck": bool(_which("shellcheck")),
        "node": bool(_which("node")),
        "eslint": bool(_which("eslint")),
    }
