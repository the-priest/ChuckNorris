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
import builtins as _builtins
import shutil
import subprocess
import tempfile

# ── security footgun patterns (language-tagged) ──────────────────────────────
# Each entry: (pattern, message, severity). Patterns match case-insensitively.
#
# SEVERITY MATTERS, and getting it wrong is expensive in both directions:
#   BLOCK  — a genuine defect with an obvious fix (injection, RCE, a shell built
#            from a string). The Run button is withheld and the model is told to
#            fix it. Only patterns precise enough that "fix it" is always the
#            right answer belong here.
#   ADVISE — a real footgun, but the pattern is broad enough to fire on correct
#            code. `\bmd5\b` hits `hashlib.md5(chunk)` used as a content
#            checksum; `verify=False` hits recon tooling pointed at a box with a
#            self-signed cert. These used to block, which meant the model was
#            handed "CODE VERIFICATION FAILED — fix every issue" for code with
#            nothing to fix, and it looped until the hop budget ran out. They are
#            now surfaced on the card and in the report without withholding it.
BLOCK, ADVISE = "block", "advise"

_SEC = {
    "python": [
        (r"\beval\s*\(", "eval() on dynamic input is RCE-prone — use ast.literal_eval", BLOCK),
        (r"\bexec\s*\(", "exec() executes arbitrary code — avoid on untrusted input", BLOCK),
        (r"subprocess\.(?:call|run|Popen|check_output)\([^)]*shell\s*=\s*True",
         "shell=True with a built string is command-injection-prone — pass a list", BLOCK),
        (r"os\.system\s*\(", "os.system() is injection-prone — use subprocess with a list", BLOCK),
        (r"yaml\.load\s*\((?![^)]*Loader\s*=\s*yaml\.SafeLoader)",
         "yaml.load without SafeLoader is unsafe — use yaml.safe_load", BLOCK),
        (r"(password|secret|api[_-]?key|token)\s*=\s*['\"][^'\"]{6,}['\"]",
         "hard-coded secret — load from env/secret store instead", BLOCK),
        (r"\bpickle\.loads?\s*\(", "pickle on untrusted data is RCE — use json", ADVISE),
        (r"\bmd5\b|\bsha1\b", "MD5/SHA1 are weak for security — fine for checksums, "
                              "use SHA-256+ if this is security-relevant", ADVISE),
        (r"verify\s*=\s*False", "TLS verify=False disables cert checking", ADVISE),
        (r"random\.(?:random|randint|choice)\b.*(token|password|secret|key)",
         "random module isn't cryptographically secure — use secrets", ADVISE),
    ],
    "js": [
        (r"\beval\s*\(", "eval() is RCE-prone", BLOCK),
        (r"child_process\.exec\s*\(",
         "child_process.exec runs a shell — use execFile/spawn with args", BLOCK),
        (r"(password|secret|api[_-]?key|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]",
         "hard-coded secret", BLOCK),
        (r"innerHTML\s*=", "innerHTML with dynamic data is XSS-prone — use textContent", ADVISE),
        (r"Math\.random\(\).*(token|password|secret)",
         "Math.random isn't cryptographically secure", ADVISE),
    ],
    "bash": [
        (r"\beval\s+", "eval in bash is injection-prone", BLOCK),
        (r"rm\s+-rf\s+\$", "rm -rf on an unquoted/var path — guard it", BLOCK),
        (r"curl\s+[^|]*\|\s*(?:sudo\s+)?(?:bash|sh)\b",
         "curl | bash executes remote code unread", BLOCK),
        (r"chmod\s+777", "chmod 777 is world-writable — tighten permissions", ADVISE),
    ],
}

# Compiled once at import. security_scan() runs every pattern against every
# line of every block; re-resolving them through re's cache on each call was
# pure overhead on the hottest loop in the verifier.
_SEC_COMPILED = {lang: [(re.compile(pat, re.IGNORECASE), msg, sev)
                        for pat, msg, sev in pats]
                 for lang, pats in _SEC.items()}

_LANG_NORM = {"py": "python", "python": "python", "python3": "python",
              "js": "js", "javascript": "js", "node": "js",
              "sh": "bash", "bash": "bash"}


_WHICH_CACHE = {}


def _which(*names):
    """First of these that exists on PATH, memoised.

    shutil.which() stats every PATH entry. This is called several times per
    verification and a verification happens on every code block Chuck writes,
    so the lookups were being redone constantly for an answer that cannot
    change while the app is running.
    """
    hit = _WHICH_CACHE.get(names)
    if hit is not None:
        return hit or None
    for n in names:
        if shutil.which(n):
            _WHICH_CACHE[names] = n
            return n
    _WHICH_CACHE[names] = ""
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



# ── stdlib static analysis (the no-linter-installed path) ────────────────────
# Before this existed, a box without ruff or pyflakes got a "lint" step that
# called compile() — which only repeats the syntax check that already ran. So
# the verifier's whole middle stage was silently a no-op on a clean install, and
# `x = undefined_name` sailed through to a Run button. The README's "no runtime
# dependency beyond the stdlib" was true of the code and false of the guarantee.
#
# The design rule here is asymmetric on purpose: a MISSED defect costs one
# round-trip, a FALSE defect withholds working code and sends the model into a
# fix loop with nothing to fix — the exact failure that made md5 unrunnable in
# v12.0.1. So every check below is deliberately conservative, and where a
# construct is ambiguous the analyzer stays quiet.

# NB: `dir(__builtins__)` is wrong inside an imported module — there
# __builtins__ is the builtins *dict*, so dir() returns dict's own methods and
# every real builtin goes missing. `print` then reads as an undefined name and
# the verifier blocks every script that prints anything. Import the module.
_ALWAYS_DEFINED = frozenset(dir(_builtins)) | frozenset((
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__all__", "__version__",
    "WindowsError", "reveal_type", "_",
))


class _Bindings(ast.NodeVisitor):
    """Every name this module binds ANYWHERE, flattened across all scopes.

    Flattening is the conservative choice. Real scope analysis would catch more
    (a local used outside its function), but it also has to model closures,
    comprehension scopes, class bodies, global/nonlocal, star-imports and
    conditional definitions correctly — and every corner it gets wrong is a
    false positive on working code. A flat set can only ever under-report.
    """

    def __init__(self):
        self.bound = set()
        self.star_import = False
        self.imported = {}          # name -> (lineno, module_text)
        self.dynamic = False        # globals()/locals()/exec seen: stop guessing

    # -- definitions
    def visit_FunctionDef(self, node):
        self.bound.add(node.name)
        for a in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs):
            self.bound.add(a.arg)
        if node.args.vararg:
            self.bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            self.bound.add(node.args.kwarg.arg)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for a in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs):
            self.bound.add(a.arg)
        if node.args.vararg:
            self.bound.add(node.args.vararg.arg)
        if node.args.kwarg:
            self.bound.add(node.args.kwarg.arg)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        elif node.id in ("globals", "locals", "vars", "eval", "exec"):
            self.dynamic = True
        self.generic_visit(node)

    def visit_Global(self, node):
        self.bound.update(node.names)
        self.generic_visit(node)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            local = a.asname or a.name.split(".")[0]
            self.bound.add(local)
            self.imported.setdefault(local, (node.lineno, a.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        for a in node.names:
            if a.name == "*":
                # A star import can bind anything at all. Any undefined-name
                # claim after this is a guess, so we stop making them.
                self.star_import = True
                continue
            local = a.asname or a.name
            self.bound.add(local)
            self.imported.setdefault(local, (node.lineno,
                                             f"{node.module or ''}.{a.name}".strip(".")))
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchMapping(self, node):
        if node.rest:
            self.bound.add(node.rest)
        self.generic_visit(node)


def _loaded_names(tree):
    """Names read (not written) anywhere, plus every attribute base."""
    used = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            base = n
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
    return used


def _string_mentions(tree):
    """Words appearing inside string literals — annotations like `x: "Foo"`,
    __all__ entries, and TYPE_CHECKING-guarded names all live there. Treating
    them as uses is crude, and that is the point: it cannot produce a false
    'unused import', only miss a real one."""
    words = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", n.value))
    return words


def _analyze_python(body):
    """Real findings from the stdlib alone. Returns a list of strings."""
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []                      # syntax is reported by its own stage
    b = _Bindings()
    try:
        b.visit(tree)
        used = _loaded_names(tree)
        strings = _string_mentions(tree)
    except Exception:
        return []                      # never fail a verification on our own bug

    findings = []

    # 1. undefined names — the one that matters. A typo'd variable is a crash
    #    the user would otherwise discover by pressing Run.
    if not (b.star_import or b.dynamic):
        known = b.bound | _ALWAYS_DEFINED
        seen = set()
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                continue
            if n.id in known or n.id in seen:
                continue
            seen.add(n.id)
            findings.append(f"code.py:{n.lineno}: undefined name '{n.id}'")
            if len(findings) >= 10:
                break

    # 2. unused imports — never blocking-worthy on their own, but they are a
    #    reliable sign the model pasted something it then rewrote, and they are
    #    free to detect.
    for name, (line, mod) in sorted(b.imported.items(), key=lambda kv: kv[1][0]):
        if name in ("annotations",) or mod.startswith("__future__"):
            continue
        if name in used or name in strings:
            continue
        findings.append(f"code.py:{line}: '{mod}' imported but unused")
        if len(findings) >= 16:
            break

    return findings


# ── lint ─────────────────────────────────────────────────────────────────────
_LINT_NOISE = ("all checks passed", "no issues found", "found 0 errors")


def _clean_lint_output(rc, out, replace_path=None, as_name="code.py"):
    """Turn a linter's raw output into real findings only.

    Linters are not consistent: ruff prints "All checks passed!" to STDOUT and
    exits 0 when the code is fine. Treating any output as a finding marks clean
    code broken — which would withhold the Run button and send the model into a
    pointless fix loop. So: trust the exit code, and drop success banners.
    """
    if rc == 0:
        return []
    text = (out or "").strip()
    if not text:
        return []
    if replace_path:
        text = text.replace(replace_path, as_name)
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not any(n in ln.lower() for n in _LINT_NOISE)]
    return ["\n".join(lines)] if lines else []


def _lint_python(body):
    findings = []
    tool = _which("ruff")
    if tool:
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(body); p = f.name
        try:
            # --isolated: ignore any ruff.toml/pyproject.toml that happens to be
            #   in the working directory, so verification is deterministic and a
            #   user's strict project config can't start blocking code cards.
            # --select E9,F,B: real defects only — syntax errors, undefined names,
            #   unused imports, likely bugs. NOT style rules (import sorting, line
            #   length): withholding working code over formatting taste would trap
            #   the model in a pointless fix loop.
            rc, out = _run([tool, "check", "--isolated", "--select", "E9,F,B",
                            "--output-format", "concise", p])
            findings += _clean_lint_output(rc, out, p, "code.py")
        finally:
            os.unlink(p)
    elif _which("pyflakes"):
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(body); p = f.name
        try:
            rc, out = _run(["pyflakes", p])
            findings += _clean_lint_output(rc, out, p, "code.py")
        finally:
            os.unlink(p)
    else:
        # No external linter: fall back to the stdlib AST analysis above rather
        # than to compile(), which only re-runs the syntax check that already
        # passed and therefore found nothing, ever.
        findings += _analyze_python(body)
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
        return _clean_lint_output(rc, out, p, "script.sh")
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
                return _clean_lint_output(rc, out, p, "code.js")
            finally:
                os.unlink(p)
    return []


# ── security scan ────────────────────────────────────────────────────────────
def security_scan(lang, body):
    """Return (blocking, advisory) lists of 'L<n>: message' findings."""
    lang = _LANG_NORM.get(lang, lang)
    pats = _SEC_COMPILED.get(lang, [])
    block_hits, advise_hits = [], []
    for i, line in enumerate(body.splitlines(), 1):
        for rx, msg, sev in pats:
            if rx.search(line):
                (block_hits if sev == BLOCK else advise_hits).append(f"L{i}: {msg}")
    # de-dup identical messages, keeping the first line number for each
    def _dedup(hits):
        seen, out = set(), []
        for h in hits:
            key = h.split(": ", 1)[-1]
            if key not in seen:
                seen.add(key); out.append(h)
        return out
    return _dedup(block_hits), _dedup(advise_hits)


# ── public: full pipeline ────────────────────────────────────────────────────
def check(lang, body, tests=None):
    """Run the full verify pipeline. Returns a dict:
       {ok, lang, syntax_ok, syntax_err, lint[], security[], tests{...}}

    SAFETY INVARIANT: this function NEVER executes the code it is checking.
    Verification is 100% static — parse, lint, and pattern-scan only. Running
    code is a separate, deliberate act that always goes through the app's
    approve-to-run card, where the user sees it and presses the button.
    The `tests` argument is accepted for API compatibility but is NOT executed;
    a verifier that runs untrusted code would defeat its own purpose.
    """
    lang = _LANG_NORM.get((lang or "").lower(), (lang or "").lower())
    res = {"lang": lang, "syntax_ok": True, "syntax_err": "",
           "lint": [], "security": [], "advisory": [], "tests": None, "ok": True}

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

    # 3. security scan (always — cheap, static). Blocking findings withhold the
    #    Run button; advisory ones are reported but never block (see _SEC).
    res["security"], res["advisory"] = security_scan(lang, body)

    # 4. tests are NOT run here (see the safety invariant above). If the model
    #    supplied tests, statically check them too and fold the findings in.
    if tests:
        t_syntax_ok = True
        if lang == "python":
            t_syntax_ok, t_err = _syntax_python(tests)
            if not t_syntax_ok:
                res["lint"].append(f"test block: {t_err}")
        t_block, t_advise = security_scan(lang, tests)
        res["security"] += t_block
        res["advisory"] += t_advise
        res["tests"] = {"ran": False, "reason": "not executed by design "
                        "(verification is static; run it via the approve-to-run card)"}

    res["ok"] = (res["syntax_ok"] and not res["lint"] and not res["security"])
    return res


def _advisory_lines(res):
    return [f"  NOTE (not blocking): {a}" for a in (res.get("advisory") or [])[:10]]


def report(res):
    """Compact human/model-readable report string from a check() result.

    Advisories are appended to BOTH the clean and the failing report, clearly
    marked as non-blocking, so the model is never told to "fix every issue"
    about something that isn't withholding the card.
    """
    if res.get("ok"):
        head = f"\u2713 {res['lang']} verified: syntax OK, no blocking issues."
        adv = _advisory_lines(res)
        return "\n".join([head] + adv) if adv else head
    lines = [f"Verification of the {res['lang']} code found issues:"]
    if not res["syntax_ok"]:
        lines.append(f"  SYNTAX: {res['syntax_err']}")
    for l in res["lint"][:20]:
        for sub in l.splitlines()[:20]:
            if sub.strip():
                lines.append(f"  LINT: {sub.strip()}")
    for s in res["security"][:20]:
        lines.append(f"  SECURITY: {s}")
    lines.append("Fix these, then re-verify.")
    lines += _advisory_lines(res)
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
