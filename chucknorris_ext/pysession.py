"""pysession.py — one Python interpreter that lives as long as the chat.

```py``` blocks run here, notebook-style: a dataset loaded in one block is
still loaded in the next, a function defined once can be called later, and a
matplotlib figure comes back as a PNG. (```python``` blocks still run as a
fresh, one-shot script.)

The kernel is a child process speaking a line protocol: each request is one
JSON line on its stdin; its output streams back on stdout/stderr (merged, so
the order is what the code printed), and the run ends with a marker line that
carries a per-run nonce, so nothing the code prints can fake the end of a run.

Stop and timeouts send SIGINT first, so a long loop is interrupted the way
Ctrl+C would and the session survives; only a kernel that ignores that is
killed, and the next block starts a fresh one (and says so).
"""
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time

KERNEL = r'''
import ast, builtins, io, json, linecache, os, sys, traceback
ctl = os.fdopen(os.dup(0), "r", encoding="utf-8")
_null = os.open(os.devnull, os.O_RDONLY)
os.dup2(_null, 0)                                  # child processes can't read the protocol
sys.stdin = io.StringIO("")


def _no_input(prompt=""):
    raise EOFError("this Python session has no keyboard: input() can't be answered "
                   "(pass the value in the code instead)")


builtins.input = _no_input
G = {"__name__": "__main__", "__builtins__": builtins}
PLOTS = os.environ.get("CHUCK_PLOTS", "")


def _run(code, name):
    linecache.cache[name] = (len(code), None, code.splitlines(True), name)
    tree = ast.parse(code, name, "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, name, "exec"), G)
    if last is not None:
        v = eval(compile(last, name, "eval"), G)
        mod = type(v).__module__ or ""
        if isinstance(v, list) and v:
            mod = type(v[0]).__module__ or ""         # plt.plot() returns [Line2D]
        if v is not None and not mod.startswith("matplotlib"):   # the figure is shown instead
            G["_"] = v
            r = repr(v)
            print(r if len(r) <= 20000 else r[:20000] + " ...")


def _plots(seq):
    out = []
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is None or not PLOTS:
        return out
    for n in plt.get_fignums():
        p = os.path.join(PLOTS, ".plot_%d_%d_%d.png" % (os.getpid(), seq, n))
        try:
            plt.figure(n).savefig(p, dpi=110, bbox_inches="tight")
            out.append(p)
        except Exception as ex:
            print("couldn't save figure %d: %s" % (n, ex), file=sys.stderr)
    plt.close("all")
    return out


def _trace(et, ev, tb):
    frames = [f for f in traceback.extract_tb(tb) if not f.filename.startswith(("<string>", sys.prefix,
                                                                                    sys.base_prefix))
              or f.filename.startswith("<py ")]
    if et is SyntaxError or issubclass(et, SyntaxError):
        frames = []                                # where it failed is in the message
    lines = ["Traceback (most recent call last):\n"] if frames else []
    sys.stderr.write("".join(lines + traceback.format_list(frames)
                             + traceback.format_exception_only(et, ev)))


seq = 0
while True:
    try:
        line = ctl.readline()
    except KeyboardInterrupt:
        continue                                   # Stop between runs: nothing to stop
    if not line:
        break
    try:
        req = json.loads(line)
    except ValueError:
        continue
    seq += 1
    st = {"ok": True}
    try:
        _run(req["code"], "<py %d>" % seq)
    except SystemExit as e:
        code = e.code
        st = {"ok": code in (None, 0), "exit": code if isinstance(code, int) else (0 if code is None else 1)}
        if code not in (None, 0) and not isinstance(code, int):
            print(code, file=sys.stderr)
    except KeyboardInterrupt:
        st = {"ok": False, "interrupted": True}
        print("KeyboardInterrupt", file=sys.stderr)
    except BaseException:
        st = {"ok": False}
        _trace(*sys.exc_info())
    try:
        st["plots"] = _plots(seq)
    except BaseException:
        st["plots"] = []
    st["names"] = sorted(k for k in G if not k.startswith("_") and k != "In")[:200]
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    sys.stdout.write("\x1eCHUCK %s %s\x1e\n" % (req.get("id", ""), json.dumps(st)))
    sys.stdout.flush()
'''



class PySession:
    """A persistent interpreter. Thread-safe: runs are serialised."""

    def __init__(self, cwd=None, plots_dir=None, env=None, python=None, preexec=None,
                 wrap=None):
        self.cwd = cwd
        self.plots_dir = plots_dir
        self.env = dict(env or os.environ)
        self.python = python or sys.executable or "python3"
        self.preexec = preexec
        self.wrap = wrap                 # argv -> argv (a sandbox), or None to refuse
        self.proc = None
        self.q = None
        self.names = []
        self.runs = 0
        self.started = 0
        self.lock = threading.Lock()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        env = dict(self.env)
        env.update({"PYTHONUNBUFFERED": "1", "MPLBACKEND": env.get("MPLBACKEND") or "Agg",
                    "PYTHONIOENCODING": "utf-8"})
        if self.plots_dir:
            os.makedirs(self.plots_dir, exist_ok=True)
            env["CHUCK_PLOTS"] = str(self.plots_dir)
        kw = {"start_new_session": True} if self.preexec is None else {"preexec_fn": self.preexec}
        argv = [self.python, "-u", "-c", KERNEL]
        if self.wrap is not None:
            argv = self.wrap(argv, env)
            if argv is None:
                raise OSError("this folder is Sandboxed but bubblewrap isn't installed")
        self.proc = subprocess.Popen(
            argv, cwd=self.cwd or None, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
        self.q = q = queue.Queue()
        fd = self.proc.stdout.fileno()

        def reader():
            import codecs
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                q.put(dec.decode(data))
            q.put(None)                                  # the kernel is gone
        threading.Thread(target=reader, daemon=True, name="pysession-reader").start()
        self.names = []
        self.started += 1

    def _signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except (OSError, AttributeError):
            try:
                self.proc.send_signal(sig)
            except Exception:
                pass

    def interrupt(self):
        """Ctrl+C the running block (the session survives)."""
        if self.alive():
            self._signal(signal.SIGINT)

    def close(self):
        if self.proc is not None:
            if self.proc.poll() is None:
                self._signal(signal.SIGKILL)
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f.close()
                except Exception:
                    pass
        self.proc = None
        self.names = []

    def run(self, code, timeout=300, on_chunk=None, should_stop=None):
        """Run code -> dict(ok, out, plots, names, restarted, interrupted,
        timed_out, died, exit)."""
        with self.lock:
            return self._run(code, timeout, on_chunk, should_stop or (lambda: False))

    def _run(self, code, timeout, on_chunk, should_stop):
        restarted = False
        if not self.alive():
            restarted = self.started > 0                 # it existed and is gone
            self.close()
            try:
                self._start()
            except OSError as ex:
                return {"ok": False, "out": f"(the Python session couldn't start: {ex})",
                        "plots": [], "names": [], "restarted": False, "interrupted": False,
                        "timed_out": False, "died": True, "exit": None}
        nonce = secrets.token_hex(8)
        res = {"ok": False, "out": "", "plots": [], "names": self.names, "restarted": restarted,
               "interrupted": False, "timed_out": False, "died": False, "exit": None}
        try:
            self.proc.stdin.write((json.dumps({"id": nonce, "code": code}) + "\n").encode())
            self.proc.stdin.flush()
        except OSError:
            res.update(died=True, out="(the Python session could not be started)")
            self.close()
            return res
        head = f"\x1eCHUCK {nonce} "
        end_re = re.compile(re.escape(head) + r"(\{.*?\})\x1e\n?", re.S)
        buf, shown = "", 0
        deadline = time.time() + timeout
        hard = None                                      # set once SIGINT was sent
        while True:
            try:
                chunk = self.q.get(timeout=0.2)
            except queue.Empty:
                chunk = ""
            if chunk is None:                            # kernel exited
                res["died"] = True
                res["exit"] = self.proc.wait(timeout=5) if self.proc else None
                break
            buf += chunk
            m = end_re.search(buf, max(0, shown - len(head)))
            if m:
                body = buf[:m.start()]
                if on_chunk and len(body) > shown:
                    on_chunk(body[shown:])
                try:
                    st = json.loads(m.group(1))
                except ValueError:
                    st = {"ok": False}
                res.update(ok=bool(st.get("ok")), plots=st.get("plots") or [],
                           interrupted=bool(st.get("interrupted")), exit=st.get("exit"))
                self.names = res["names"] = st.get("names") or []
                buf = body
                break
            if on_chunk:
                # stream everything except a tail that could be the start of
                # this run's end marker
                cut = buf.rfind("\x1e")
                tail = buf[cut:] if cut >= 0 else ""
                safe = cut if tail and (head.startswith(tail) or tail.startswith(head)) else len(buf)
                if safe > shown:
                    on_chunk(buf[shown:safe])
                    shown = safe
            now = time.time()
            if hard is None and (now > deadline or should_stop()):
                res["timed_out"] = now > deadline
                self.interrupt()
                hard = now + 3
            elif hard is not None and now > hard:
                self.close()                             # ignored Ctrl+C: kill it
                res.update(died=True, exit=-9)
                break
        self.runs += 1
        out = buf
        if res["died"]:
            code = res["exit"]
            out += ("\n(the Python session " + ("exited" if code is None or code >= 0
                                                  else "was stopped (it ignored Ctrl+C)")
                    + ("" if code is None or code < 0 else f" with code {code}")
                    + " — its variables are gone; the next block starts a fresh one)")
        res["out"] = out
        return res
