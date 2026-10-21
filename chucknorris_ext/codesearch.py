"""codesearch.py — find code by what it does, not by an exact string.

"where do we retry failed requests?" in a repo you've never seen: grep needs
the word the author happened to use; this ranks every function, class and
doc section by how well its NAME, docstring, comments and identifiers match
the question (BM25, the ranking classic search engines are built on), with
camelCase and snake_case split into words and a light stemmer so "retrying",
"retries" and "retry" meet. Stdlib only; the index is cached per folder and
only changed files are re-read.
"""
import ast
import math
import re
from pathlib import Path

from . import builder as _builder

_TEXT_EXT = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".c",
             ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".cs", ".swift", ".rb", ".php", ".lua",
             ".sh", ".bash", ".zsh", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json",
             ".html", ".css", ".scss", ".vue", ".svelte", ".sql", ".ex", ".exs", ".scala", ".dart"}
_STOP = set("""a an and are as at be but by for from has have if in into is it its of on or
that the their then there these this to was were will with without you your we our not no
self cls def return import from class function const let var new none null true false
else elif try except finally pass do done fi end""".split())
_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WINDOW = 60


def _stem(w):
    """A small suffix stripper. It only has to send a word's forms to the same
    stem (parse, parses, parsed, parsing, parser -> pars), not make real words."""
    for suf in ("ization", "ations", "ation", "ings", "ing", "edly", "ers", "ies", "ied", "ure",
                "ed", "es", "er", "s"):
        if not w.endswith(suf) or len(w) - len(suf) < 3:
            continue
        base = w[:-len(suf)]
        if suf in ("ies", "ied"):
            return base + "y"
        if suf == "es" and not base.endswith(("s", "x", "z", "ch", "sh")):
            base = w[:-1]                       # files -> file, not fil·es
        if suf == "s" and base.endswith(("s", "u", "i")):
            return w                            # process, status, analysis keep their s
        if suf in ("ing", "ings", "ed", "er", "ers") and len(base) > 3 and \
                base[-1] == base[-2] and base[-1] not in "lsz":
            base = base[:-1]                    # running -> run, stopped -> stop
        w = base
        break
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]                              # parse and parsing meet at pars
    return w


def words(text):
    out = []
    for ident in _IDENT.findall(text or ""):
        for part in _PART.findall(ident):
            w = part.lower()
            if len(w) > 1 and w not in _STOP:
                st = _stem(w)
                if st not in _STOP:             # classes -> class, returns -> return
                    out.append(st)
    return out


def _py_chunks(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    out = []

    def walk(nodes, prefix):
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = (n.decorator_list[0].lineno if n.decorator_list else n.lineno)
                end = getattr(n, "end_lineno", None) or start
                name = prefix + n.name
                if isinstance(n, ast.ClassDef):
                    # the class head (docstring, attributes) is its own chunk;
                    # its methods are found on their own
                    first = next((b.lineno for b in n.body if isinstance(
                        b, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))), end + 1)
                    out.append((start, max(start, first - 1), name, ast.get_docstring(n) or ""))
                    walk(n.body, name + ".")
                else:
                    out.append((start, end, name, ast.get_docstring(n) or ""))
    walk(tree.body, "")
    covered = set()
    for s, e, _n, _d in out:
        covered.update(range(s, e + 1))
    # module-level code that isn't in a def (scripts, config) in windows
    loose = [i for i in range(1, len(lines) + 1) if i not in covered]
    for k in range(0, len(loose), WINDOW):
        seg = loose[k:k + WINDOW]
        if any(lines[i - 1].strip() for i in seg):
            out.append((seg[0], seg[-1], "", ""))
    return out


def _pattern_chunks(text, ext):
    rx = next((r for exts, r in _builder._DEF_PATTERNS.items() if ext in exts), None)
    lines = text.splitlines()
    starts = []
    if rx is not None:
        for i, ln in enumerate(lines, 1):
            m = rx.match(ln)
            if m:
                nm = next((g for g in m.groups() if g), "")
                if nm not in ("if", "for", "while", "switch", "return"):
                    starts.append((i, nm))
    if ext in (".md", ".rst"):
        starts = [(i, ln.lstrip("#").strip()) for i, ln in enumerate(lines, 1)
                  if ln.startswith("#")]
    out = []
    if starts:
        if starts[0][0] > 1:
            out.append((1, starts[0][0] - 1, "", ""))
        for k, (i, nm) in enumerate(starts):
            end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines)
            for a in range(i, end + 1, WINDOW * 2):          # a 900-line function in pieces
                out.append((a, min(end, a + WINDOW * 2 - 1), nm, ""))
    else:
        for a in range(1, len(lines) + 1, WINDOW):
            out.append((a, min(len(lines), a + WINDOW - 1), "", ""))
    return out


class Index:
    """BM25 over a folder's code chunks, refreshed incrementally."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = {}          # rel -> (mtime, size, [chunk])
        # chunk: dict(rel, start, end, name, tf{term: n}, length)

    def refresh(self, limit=4000):
        seen = set()
        for rel in _builder._project_files(self.root, limit=limit):
            p = self.root / rel
            ext = p.suffix.lower()
            if ext not in _TEXT_EXT:
                continue
            try:
                st = p.stat()
                if st.st_size > _builder.MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            seen.add(rel)
            have = self.files.get(rel)
            if have and have[0] == st.st_mtime and have[1] == st.st_size:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue
            self.files[rel] = (st.st_mtime, st.st_size, self._chunk(rel, text, ext))
        for rel in list(self.files):
            if rel not in seen:
                del self.files[rel]

    @staticmethod
    def _chunk(rel, text, ext):
        lines = text.splitlines()
        spans = (_py_chunks(text) if ext in (".py", ".pyi") else None) or _pattern_chunks(text, ext)
        path_words = words(rel.replace("/", " "))
        out = []
        for start, end, name, doc in spans:
            body = "\n".join(lines[start - 1:end])
            terms = words(body) + path_words
            # the name and docstring say what it IS: they count three times
            terms += (words(name) + words(doc)) * 2
            if not terms:
                continue
            tf = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1
            out.append({"rel": rel, "start": start, "end": end, "name": name, "tf": tf,
                        "len": len(terms)})
        return out

    def search(self, query, k=8):
        q = list(dict.fromkeys(words(query)))
        chunks = [c for _m, _s, cs in self.files.values() for c in cs]
        if not q or not chunks:
            return []
        n = len(chunks)
        avg = sum(c["len"] for c in chunks) / n
        df = {t: sum(1 for c in chunks if t in c["tf"]) for t in q}
        k1, b = 1.2, 0.75
        want_tests = bool({"test", "spec"} & set(q))
        want_docs = bool({"doc", "readme", "changelog", "roadmap"} & set(q))
        scored = []
        for c in chunks:
            s, hit = 0.0, 0
            for t in q:
                f = c["tf"].get(t)
                if not f:
                    continue
                hit += 1
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * c["len"] / avg))
            if s > 0:
                s *= 1 + 0.5 * (hit - 1) / max(1, len(q))      # matching more of the question
                rel = c["rel"].lower()
                if not want_tests and re.search(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_|_test\.|\.spec\.", rel):
                    s *= 0.6                 # the code, before the code that tests it
                if not want_docs and rel.endswith((".md", ".rst", ".txt")):
                    s *= 0.75
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


_CACHE = {}


def search(root, query, k=8):
    root = str(Path(root).resolve())
    idx = _CACHE.get(root)
    if idx is None:
        idx = _CACHE[root] = Index(root)
    idx.refresh()
    return idx.search(query, k)


def report(root, query, results):
    if not results:
        return (f"FIND \"{query}\": nothing matched. Try other words for it, or grep for a "
                "name you expect.")
    q = set(words(query))
    rows = []
    for score, c in results:
        try:
            lines = (Path(root) / c["rel"]).read_text(encoding="utf-8",
                                                     errors="replace").splitlines()
        except OSError:
            continue
        seg = lines[c["start"] - 1:c["end"]]
        # the line that matches the question best, as the preview
        best = max(range(len(seg)), key=lambda i: (len(q & set(words(seg[i]))), -i)) if seg else 0
        head = seg[0].strip() if seg else ""
        prev = seg[best].strip() if seg else ""
        name = f"  {c['name']}" if c["name"] else ""
        rows.append(f"{c['rel']}:{c['start']}-{c['end']}{name}  (score {score:.1f})\n"
                    f"    {head[:150]}" + (f"\n    … {prev[:150]}" if best > 0 and prev else ""))
    return (f"FIND \"{query}\" — best matches by meaning (read them to confirm):\n"
            + "\n".join(rows))
