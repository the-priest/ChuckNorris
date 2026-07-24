import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Windowed transcript — long sessions must not pile widgets up in RAM.

Only the newest RENDER_KEEP messages stay materialised; scrolling to the top
reveals RENDER_PAGE more; returning to the bottom frees them again. Text must
survive the round trip, and interactive cards must never be torn down (their
run state can't be rebuilt).
"""
import tempfile, json, time
from pathlib import Path
from sim import cn, new_win, type_and_send, fail, FAILS
import gtkstub

tmp = Path(tempfile.mkdtemp())


class Adj:
    """Stand-in scroll adjustment so we can drive scroll positions."""
    def __init__(s, v, u, p): s.v, s.u, s.p = v, u, p
    def get_value(s): return s.v
    def get_upper(s): return s.u
    def get_page_size(s): return s.p
    def set_value(s, x): s.v = x


def live(w):
    return [e for e in w._log if e["w"] is not None]


print("--- long session keeps only the newest slice alive ---")
w = new_win(["reply %d" % i for i in range(200)], tmp)
for i in range(60):
    type_and_send(w, f"question {i}")
print(f"  log={len(w._log)} alive={len(live(w))} cap={cn.RENDER_KEEP}")
if len(live(w)) > cn.RENDER_KEEP + 2:
    fail(f"{len(live(w))} widgets alive after 60 turns")

print("--- scrolling to the top loads a page at a time ---")
start = w._win_start
w._on_scroll(Adj(0, 5000, 600)); gtkstub.fire_timers(1)
if w._win_start != max(0, start - cn.RENDER_PAGE):
    fail(f"page 1 not loaded: {start} -> {w._win_start}")
after1 = len(live(w))
s2 = w._win_start
w._on_scroll(Adj(0, 6000, 600)); gtkstub.fire_timers(1)
if w._win_start != max(0, s2 - cn.RENDER_PAGE):
    fail(f"page 2 not loaded: {s2} -> {w._win_start}")
print(f"  {start} -> {s2} -> {w._win_start} | alive {after1} -> {len(live(w))}")
if len(live(w)) <= after1:
    fail("second page materialised nothing")

print("--- returning to the bottom frees them again ---")
w._on_scroll(Adj(5400, 6000, 600))
print(f"  window start {w._win_start} | alive {len(live(w))}")
if len(live(w)) > cn.RENDER_KEEP + 2:
    fail(f"did not offload on return to bottom: {len(live(w))} alive")

print("--- text survives unload and rebuild ---")
w._win_start = 0
w._apply_window()
users = [e["data"]["text"] for e in w._log if e["kind"] == "user"]
bots = [e["data"]["text"] for e in w._log if e["kind"] == "bot"]
if users[0] != "question 0" or users[-1] != "question 59":
    fail(f"user text corrupted: {users[0]!r} .. {users[-1]!r}")
if not bots or not bots[0].startswith("reply"):
    fail(f"bot text corrupted: {bots[:1]}")
print(f"  {len(users)} user + {len(bots)} bot messages intact after rebuild")

print("--- interactive cards are pinned, never freed ---")
w2 = new_win(["```bash\nls -la\n```", "done"], tmp)
type_and_send(w2, "list files")
pins = [e for e in w2._log if e["pinned"]]
for i in range(30):
    type_and_send(w2, f"filler {i}")
alive_pins = [e for e in pins if e["w"] is not None]
print(f"  pinned {len(pins)}, still alive {len(alive_pins)}")
if len(alive_pins) != len(pins):
    fail("a card was freed — its run state would be lost")

print("--- a huge saved chat opens without materialising it all ---")
hist = [{"role": "system", "content": "sys"}]
for i in range(150):
    hist.append({"role": "user", "content": f"old question {i}"})
    hist.append({"role": "assistant", "content": f"old answer {i}"})
w3 = new_win(["x"], tmp)
f = cn.CHATS_DIR / "20260723-101010.json"
f.write_text(json.dumps({"title": "big", "ts": "20260723-101010", "history": hist}))
t0 = time.time(); w3._load_chat(f); el = time.time() - t0
print(f"  {len(hist)} history entries -> {len(live(w3))} widgets in {el:.3f}s")
if len(live(w3)) > cn.RENDER_KEEP + 2:
    fail(f"load materialised {len(live(w3))} widgets")
w3._win_start = 0; w3._apply_window()
u3 = [e["data"]["text"] for e in w3._log if e["kind"] == "user"]
if u3[0] != "old question 0" or u3[-1] != "old question 149":
    fail("reloaded chat text wrong after scroll-back")

print("--- the streaming reply is never freed mid-flight ---")
w4 = new_win(["A" * 3000], tmp)
type_and_send(w4, "long one")
be = w4._bot_entry
if be is None or be["w"] is None:
    fail("streaming bubble freed while live")
elif len(be["data"]["text"]) == 0:
    fail("streamed text not recorded in the log")
else:
    print(f"  live bubble holds {len(be['data']['text'])} chars")
    for i in range(40):
        type_and_send(w4, f"more {i}")
    if be["w"] is not None:
        fail("old reply never freed")
    w4._win_start = 0; w4._apply_window()
    if be["w"] is None or not be["data"]["text"]:
        fail("old reply did not rebuild with its text")
    else:
        print("  freed later, then rebuilt intact")

print("--- window size is tunable ---")
w5 = new_win(["r"] * 80, tmp)
w5.settings["render_keep"] = 4
for i in range(20):
    type_and_send(w5, f"q{i}")
if len(live(w5)) > 6:
    fail(f"render_keep=4 ignored: {len(live(w5))} alive")
print(f"  render_keep=4 -> {len(live(w5))} alive")

print("--- no exceptions on the UI thread ---")
if gtkstub.GLibNS.errors:
    fail(f"UI errors: {gtkstub.GLibNS.errors[:2]}")

print()
print("TOTAL TRANSCRIPT FAILURES:", len(FAILS))
for x in FAILS:
    print("  ", x)
assert not FAILS, "windowed transcript regressions"
print("ALL TRANSCRIPT MEMORY TESTS PASSED")
