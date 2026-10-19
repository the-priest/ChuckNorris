import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""v14 — the harness release. Every block names the failure it prevents.

Agent/Chat modes, the working folder, edit/grep/glob/map/paged reads, the step
budget, reply continuation, parallel sub-agents, vision, attachments, real
usage pricing, memory updates and scopes — each driven through the REAL window
on the stub toolkit, or through the module that owns it.
"""
import tempfile
import threading
import urllib.error
from pathlib import Path

from sim import cn, new_win, type_and_send, settle, check_finished, FAILS

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


def hist_text(w):
    return " ".join(str(m.get("content", "")) for m in w.history)


# ═══════════════════════════════════════════════════════════════════════════
print("\n--- prompts: small, and still carrying the rules the safety suite guards ---")
check(len(cn.SYSTEM_PROMPT) < 3700, "agent prompt is far below v13's 5,708 chars",
      f"{len(cn.SYSTEM_PROMPT)}")
check(len(cn.CHAT_PROMPT) < 900, "chat prompt is tiny", f"{len(cn.CHAT_PROMPT)}")
for must in ("YOU ACT, YOU DON'T SUGGEST", "ONE command per reply", "```edit", "```task",
             "```screenshot", "```map"):
    check(must in cn.SYSTEM_PROMPT, f"agent prompt documents {must!r}")
check("```bash" not in cn.CHAT_PROMPT, "chat prompt offers no way to run anything")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- modes: Chat never touches the machine, Agent does ---")
w = new_win(["Here you go:\n```bash\necho CHAT_SHOULD_NOT_RUN\n```"], tmp)
w.set_agent_mode(False, announce=False)
type_and_send(w, "how do I list files")
check("CHAT_SHOULD_NOT_RUN" not in hist_text(w).replace("echo CHAT_SHOULD_NOT_RUN", ""),
      "a bash fence in Chat mode did not execute")
check(w.history[0]["content"].startswith(cn.CHAT_PROMPT[:40]), "system prompt swapped to chat")
bot = [e for e in w._log if e["kind"] == "bot"][-1]
check("echo CHAT_SHOULD_NOT_RUN" in bot["data"]["text"],
      "in Chat mode the code block is SHOWN as code, not stripped")
check_finished(w, "chat mode")
w.toggle_mode()
check(w.agent_mode and w.history[0]["content"].startswith(cn.SYSTEM_PROMPT[:40]),
      "Tab toggle flips to Agent and swaps the prompt back")
w2 = new_win(["```bash\necho AGENT_RAN_$((6*7))\n```", "done"], tmp)
w2.set_agent_mode(True, announce=False)
type_and_send(w2, "go")
check("AGENT_RAN_42" in hist_text(w2), "Agent mode runs the same block for real")
check_finished(w2, "agent mode")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- working folder: commands run there, file tools resolve there ---")
ws = tmp / "repo"; ws.mkdir()
(ws / "app.py").write_text("def hello():\n    return 'hi'\n\nprint(hello())\n")
w = new_win(["```bash\npwd\n```", "ok"], tmp)
w.set_agent_mode(True, announce=False)
w.set_workspace(ws, announce=False)
type_and_send(w, "where am I")
check(str(ws) in hist_text(w), "bash ran in the working folder", "")
w.set_workspace(None, announce=False)
check(cn._cwd() is None, "clearing the folder clears the command cwd")

w = new_win(["```edit app.py\n<<<<<<< SEARCH\n    return 'hi'\n=======\n    return 'hello'\n"
             ">>>>>>> REPLACE\n```", "done"], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
type_and_send(w, "change the greeting")
check("return 'hello'" in (ws / "app.py").read_text(), "```edit applied a SEARCH/REPLACE in place")
check("edited app.py" in hist_text(w), "and reported it back")

w = new_win(["```edit app.py\n<<<<<<< SEARCH\nnot in the file\n=======\nx\n>>>>>>> REPLACE\n```", "ok"], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
before = (ws / "app.py").read_text()
type_and_send(w, "bad edit")
check((ws / "app.py").read_text() == before, "a non-matching edit changes nothing")
check("nothing was changed" in hist_text(w), "and says so to the model")

w = new_win(["```grep return\n```\n```glob *.py\n```\n```map\n```\n```read app.py:1-2\n```", "ok"], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
type_and_send(w, "look around")
h = hist_text(w)
check("app.py:2:" in h, "grep returned file:line hits")
check("GLOB" in h and "app.py" in h, "glob listed the file")
check("REPO MAP" in h and "hello()" in h, "map listed the file with its definitions")
check("FILE app.py:1-2" in h, "read honoured a line range")
check(len(w.backend.calls) == 2, "four read-only tools batched into ONE round trip",
      f"{len(w.backend.calls)} calls")
check_finished(w, "read-only batch")

# a big file arrives in pages, never silently cut
big = ws / "big.py"
big.write_text("".join(f"x_{i} = {i}\n" for i in range(1, 12001)))
w = new_win(["```read big.py\n```", "ok"], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
type_and_send(w, "read it")
h = hist_text(w)
check("continue with ```read big.py:" in h, "a big read ends with an exact pointer to the next page")
ok, txt, foot = cn._builder.read_lines(big, 11990)
check(ok and "x_12000 = 12000" in txt, "the last page is reachable by range")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- step budget: agents don't quit at step five ---")
w = new_win(["```bash\necho step\n```"] * 9 + ["all done"], tmp)
w.set_agent_mode(True, announce=False)
type_and_send(w, "do nine things")
ran = hist_text(w).count("SUCCEEDED (exit 0)")
check(ran == 9, "nine sequential commands all ran (old shared cap stopped at 4)", f"{ran}")
w = new_win(["```bash\necho again\n```"] * 60, tmp)
w.set_agent_mode(True, announce=False)
w.settings["agent_steps"] = 5
type_and_send(w, "loop forever")
w.settings.pop("agent_steps", None)
check(len(w.backend.calls) <= 8, "a runaway is still stopped by the step budget",
      f"{len(w.backend.calls)} calls")
check("STEP BUDGET SPENT" in hist_text(w), "and asked for a final report")
check_finished(w, "step budget")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- continuation: a reply cut at max_tokens is continued, not lost ---")


class CutBackend(sim_backend := __import__("sim").ScriptedBackend):
    def __init__(self, pieces):
        super().__init__(pieces)
        self.finishes = ["length"] * (len(pieces) - 1) + ["stop"]
        self.last_finish = None
        self.last_usage = None

    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        self.last_finish = self.finishes.pop(0) if self.finishes else "stop"
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


body_a = "".join(f"line_{i} = {i}\n" for i in range(0, 300))
body_b = "".join(f"line_{i} = {i}\n" for i in range(300, 600))
w = new_win([], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
w.backend = CutBackend(["Writing it.\n```write long.py\n" + body_a,
                        body_b + "```", "Written."])
type_and_send(w, "write a long file")
lp = ws / "long.py"
check(lp.exists() and lp.read_text().count("\n") == 600,
      "a file split across two replies was stitched and written whole",
      f"{lp.read_text().count(chr(10)) if lp.exists() else 'missing'} lines")
check(not any(m.get("content", "").startswith("CONTINUE:") for m in w.history
              if isinstance(m.get("content"), str)),
      "the CONTINUE scaffolding was collapsed out of history")
check(cn.ChuckWindow._join_overlap("x = 1\nresult = compute(x)\n", "result = compute(x)\nprint(result)\n")
      == "x = 1\nresult = compute(x)\nprint(result)\n",
      "a line the model repeated when continuing is trimmed when stitching")
check(cn.ChuckWindow._join_overlap("the end of the", " the next") == "the end of the the next",
      "a short coincidental overlap is NOT eaten (12-char minimum)")
check_finished(w, "continuation")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- sub-agents: parallel, read-only, reports come back ---")
from chucknorris_ext import agents as A
order = []


def fake_model(msgs):
    brief = msgs[1]["content"]
    if not any(m["role"] == "assistant" for m in msgs):
        return f"```grep {brief}\n```"
    return f"REPORT for {brief}"


tools = {"grep": lambda a: (order.append(a), f"hits for {a}")[1]}
out = A.run_parallel(["alpha", "beta", "gamma"],
                     lambda i, b: A.run_subagent(b, fake_model, tools), max_workers=3)
check(out == ["REPORT for alpha", "REPORT for beta", "REPORT for gamma"],
      "reports come back in brief order", str(out))
check(sorted(order) == ["alpha", "beta", "gamma"], "each sub-agent used its tool")
check("```bash" not in A.SUB_PROMPT and "write" not in A.TOOL_RE.pattern,
      "sub-agents have no shell and no write tool")

w = new_win([], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(ws, announce=False)
script = ["```task\nfind hello\n```\n```task\nfind print\n```"]


class SubBackend(__import__("sim").ScriptedBackend):
    lock = threading.Lock()

    def stream(self, messages, on_delta, on_done, on_error, vision=False,
               should_stop=None, attempts=3, on_open=None):
        sysmsg = messages[0]["content"] if messages else ""
        if sysmsg.startswith("You are a sub-agent"):
            on_delta("REPORT: found it in app.py"); on_done(); return
        super().stream(messages, on_delta, on_done, on_error, vision, should_stop,
                       attempts, on_open)


w.backend = SubBackend(script + ["Both sub-agents reported."])
type_and_send(w, "survey in parallel")
h = hist_text(w)
check("SUB-AGENT REPORTS" in h and h.count("REPORT: found it in app.py") >= 2,
      "two sub-agents ran and their reports reached the model")
check_finished(w, "sub-agents")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- vision + attachments ---")
png = tmp / "shot.png"
png.write_bytes(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"))
txtf = tmp / "notes.txt"; txtf.write_text("the deploy key rotates on fridays")
binf = tmp / "blob.bin"; binf.write_bytes(b"\x00\x01\x02" * 100)
w = new_win(["I can see it."], tmp)
seen = {}
orig = w.backend.stream


def spy(messages, *a, **k):
    seen["last"] = messages[-2] if messages[-1]["role"] == "system" else messages[-1]
    seen["vision"] = a[3] if len(a) > 3 else k.get("vision")
    return orig(messages, *a, **k)


w.backend.stream = spy
for f in (png, txtf, binf):
    w.add_attachment(f)
check(len(w._attachments) == 3, "image, text and binary files all attach")
w.entry.get_buffer().set_text("what is this?")
w.on_send(); settle()
last = seen.get("last") or {}
parts = last.get("content") if isinstance(last.get("content"), list) else []
check(any(p.get("type") == "image_url" for p in parts), "the image went to the model AS an image")
check(seen.get("vision") is True, "and the request was flagged as a vision request")
text_part = next((p["text"] for p in parts if p.get("type") == "text"), "")
check("deploy key rotates" in text_part, "the text file's contents rode along")
check(str(binf) in text_part, "the binary file was passed by path")
check(all(isinstance(m.get("content"), str) for m in w.history),
      "history keeps text only — the image is paid for once, not every hop")
check(not w._attachments, "the tray empties after sending")
check_finished(w, "attachments")

w = new_win(["Looking.\n```look shot.png\n```", "It is a single pixel."], tmp)
w.set_agent_mode(True, announce=False); w.set_workspace(tmp, announce=False)
calls = []
orig2 = w.backend.stream
w.backend.stream = lambda messages, *a, **k: (calls.append(messages), orig2(messages, *a, **k))[1]
type_and_send(w, "check the render")
second = calls[1] if len(calls) > 1 else []
check(any(isinstance(m.get("content"), list) for m in second),
      "```look attached the image to the NEXT request")
check_finished(w, "look")

b = cn.Backend({"model": "deepseek-ai/DeepSeek-V4.1-Flash", "vision_model": "zai-org/GLM-5.3-Flash"})
check(b.model(vision=True) == "zai-org/GLM-5.3-Flash", "images route to the vision model "
      "when the chat model can't see")
b = cn.Backend({"model": "zai-org/GLM-5.3-Flash"})
check(b.model(vision=True) == "zai-org/GLM-5.3-Flash", "a multimodal chat model sees its own screenshots")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- pricing: real usage, real dollars ---")
P = cn._pricing
u = P.usage_from_chunk({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000,
                                  "prompt_cache_hit_tokens": 400_000}})
check(u == {"prompt": 1_000_000, "completion": 1_000_000, "cached": 400_000, "reasoning": 0},
      "SiliconFlow-style usage parsed", str(u))
c = P.cost(u, P.price_for("deepseek-ai/DeepSeek-V3.2"))
check(abs(c - (600_000 * 0.27 + 400_000 * 0.135 + 1_000_000 * 0.42) / 1e6) < 1e-9,
      "cache hits billed at the cache rate", f"{c}")
check(P.cost(u, P.price_for("Qwen/Qwen3-8B")) == 0.0, "free models cost nothing")
check(P.price_for("unknown/model") is None, "an unknown model has no invented price")
check(P.price_for("unknown/model", {"price_in": 1, "price_out": 2}) == (1.0, None, 2.0),
      "a custom price in Settings wins")
w = new_win(["hi"], tmp)
w.backend.last_usage = {"prompt": 2000, "completion": 100, "cached": 1000, "reasoning": 0}
type_and_send(w, "hi")
check(w._usage["prompt"] == 2000 and w._usage["cached"] == 1000 and w._usage["estimated"] == 0,
      "the meter booked the API's numbers, not an estimate", str(w._usage))


class Fake400(Exception):
    pass


def http400(body):
    import io
    return urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(body))


check(cn._mentions_stream_options(http400(b'{"error":"unknown field stream_options"}')),
      "a 400 naming stream_options triggers the fallback")
check(not cn._mentions_stream_options(http400(b'{"error":"model not found"}')),
      "any other 400 does not (and is not retried)")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- memory: updates supersede, project facts stay in their folder ---")
M = cn._memory
M.FACTS = tmp / "facts.jsonl"; M.SUPERSEDED = tmp / "superseded.jsonl"
M._CACHE.update({"sig": None, "facts": [], "stems": {}, "idf": None})
M.remember("User's GPU is an RTX 3080")
ok, msg = M.remember("User's GPU is an RTX 4090")
facts = [t for t, _k in M.all_facts()]
check(msg == "updated" and facts == ["User's GPU is an RTX 4090"],
      "a newer fact about the same subject replaced the old one", str(facts))
check("3080" in M.SUPERSEDED.read_text(), "the superseded fact was archived, not destroyed")
M.remember("User prefers vim"); M.remember("User prefers dark themes")
check(len(M.all_facts()) == 3, "different facts about one subject both stay")
M.remember("Tests run with pytest -q here", scope="/w/proj")
check(M.recall("how do tests run") == [] or all("pytest" not in f for f in M.recall("how do tests run")),
      "a project fact is not recalled outside its folder")
check(any("pytest" in f for f in M.recall("how do tests run", "/w/proj")),
      "and is recalled inside it")

# ═══════════════════════════════════════════════════════════════════════════
print("\n--- rendering: segments, display text, no stray widgets ---")
segs = cn.md_segments("a\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```py\nprint(1)\n```\nb")
check([s[0] for s in segs] == ["md", "table", "code", "md"],
      "md/table/code segments (blank prose dropped)",
      str([s[0] for s in segs]))
w = new_win([], tmp)
w.set_agent_mode(True, announce=False)
check(w._display_text("say\n```bash\nls\n```\nmore") == "say\n\nmore",
      "agent mode hides tool blocks from the reply text")
w.set_agent_mode(False, announce=False)
check("```bash" in w._display_text("say\n```bash\nls\n```"),
      "chat mode keeps code blocks visible")
w.set_agent_mode(True, announce=False)
check(w._display_text("text ```bash\nhalf a comm", partial=True) == "text",
      "a half-streamed tool block never flashes up as prose")
field = cn.GlyphField()
field.poke(10, 10)
check(True, "the glyph field constructs and ignores pokes safely without cairo/GTK")
check("main-window" not in cn.CSS_TMPL or "chucknorris-bg" not in open("chucknorris.py").read(),
      "the old background image is gone from the app")

print()
print("TOTAL V14 FAILURES:", fails + len(FAILS))
for f in FAILS:
    print("  ", f)
assert fails == 0 and not FAILS, "v14 regressions present"
print("ALL V14 TESTS PASSED")
