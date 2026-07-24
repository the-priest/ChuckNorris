import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Images in chat, and search that keeps working when the web doesn't.

Two bugs this locks shut:
  - image downloads used to run inside the idle callback, freezing the whole
    window for up to 25s per picture;
  - SearXNG instances were probed one at a time at 15s each, so two dead hosts
    cost 30s before the fallback even started.
"""
import tempfile, time, threading, io
from pathlib import Path
from sim import cn, new_win, type_and_send, check_finished, fail, FAILS
import gtkstub

tmp = Path(tempfile.mkdtemp())
UI = threading.current_thread()


def _png(p):
    Path(p).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 80)
    return str(p)


print("--- image downloads stay off the UI thread and run in parallel ---")
where = []
def fake_dl(u, timeout=25):
    where.append(threading.current_thread() is UI)
    time.sleep(0.05)
    return _png(tmp / f"i{len(where)}.png")
cn.image_search = lambda q, n=6: [f"https://x.com/{i}.jpg" for i in range(5)]
cn.download_image = fake_dl
cn._pic_from_file = lambda p, w=-1, h=-1: (gtkstub.Widget(),
                                           type("PB", (), {"get_height": lambda s: 100})())
w = new_win(["```images\ncats\n```", "done"], tmp)
t0 = time.time(); type_and_send(w, "show me cats"); el = time.time() - t0
imgs = [e for e in w._log if e["kind"] == "image"]
print(f"  downloads={len(where)} on-ui-thread={any(where)} bubbles={len(imgs)} in {el:.2f}s")
if any(where):
    fail("image downloads ran on the UI thread — the window would freeze")
if len(imgs) != 5:
    fail(f"expected 5 image bubbles, got {len(imgs)}")
if el > 0.25:
    fail(f"downloads look serial ({el:.2f}s for 5 x 0.05s)")
check_finished(w, "images")

print("--- images unload with the window and rebuild on scroll-back ---")
for i in range(30):
    type_and_send(w, f"filler {i}")
if any(e["w"] is not None for e in imgs):
    fail("image widgets were not freed when scrolled away")
w._win_start = 0; w._apply_window()
if any(e["w"] is None for e in imgs):
    fail("images did not rebuild when scrolled back")
print("  freed when away, rebuilt when back")

print("--- a local image path is shown, not refused as binary ---")
shot = _png(tmp / "screenshot.png")
w2 = new_win([f"```read\n{shot}\n```", "that's it"], tmp)
type_and_send(w2, "show me that screenshot")
if not [e for e in w2._log if e["kind"] == "image"]:
    fail("local image path was not displayed")
if not [m for m in w2.history
        if isinstance(m.get("content"), str) and "showed the image" in m["content"]]:
    fail("model was not told the image was shown")
print("  displayed and reported back")
check_finished(w2, "local image")

print("--- reading a text file still works (no regression) ---")
txt = tmp / "notes.txt"; txt.write_text("hello from a text file")
w3 = new_win([f"```read\n{txt}\n```", "got it"], tmp)
type_and_send(w3, "read my notes")
if not [m for m in w3.history
        if isinstance(m.get("content"), str) and "hello from a text file" in m["content"]]:
    fail("text file reading regressed")
print("  text content still reaches the model")

print("--- non-image binaries are still refused, devices still safe ---")
binf = tmp / "blob.bin"; binf.write_bytes(bytes(range(256)) * 20)
ok, msg = cn.read_file_safe(str(binf))
if ok:
    fail("binary file was read as text")
ok2, _ = cn.read_file_safe("/dev/zero")
if ok2:
    fail("/dev/zero readable again")
print("  binary refused, device refused")

print("--- a dead search instance cannot stall the search ---")
# deterministic: the FIRST host probed is always the dead one, so this exercises
# the straggler path every run rather than depending on the shuffle.
calls = []
DEAD = "https://dead.example"
cn.SEARX_INSTANCES = [DEAD] + ["https://good%d.example" % i for i in range(1, 5)]
def slow_or_fail(url, **kw):
    host = url.split("/search")[0]; calls.append(host)
    if DEAD in host:
        time.sleep(3); raise RuntimeError("dead host")
    return io.BytesIO(b'{"results":[{"url":"https://good.com/a","title":"T","content":"C"}]}')
cn._get = slow_or_fail
cn._SETTINGS = {}
t0 = time.time(); res = cn._searx_search("test", 3); el = time.time() - t0
print(f"  probed {len(set(calls))} hosts in {el:.2f}s, got {len(res)} results")
if el > 2.0:
    fail(f"one dead instance stalled search for {el:.1f}s")
if not res:
    fail("search returned nothing despite a healthy instance")

print("--- search falls back when every instance fails ---")
cn._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("all down"))
res2 = cn._searx_search("test", 3)
if res2 != []:
    fail("expected an empty list when every instance is down")
try:
    out = cn.web_search("test", 3)
    print("  web_search survived a total outage ->", out)
except Exception as e:
    fail(f"web_search crashed on total outage: {e}")

print("--- results are always http(s), never file:/javascript: ---")
cn._get = lambda *a, **k: io.BytesIO(
    b'{"results":[{"url":"file:///etc/passwd","title":"x","content":"y"},'
    b'{"url":"javascript:alert(1)","title":"x","content":"y"},'
    b'{"url":"https://ok.com/a","title":"ok","content":"c"}]}')
rows = cn._searx_search("q", 5)
bad = [u for _t, u, _s in rows if not u.startswith(("http://", "https://"))]
if bad:
    fail(f"search emitted non-http URLs: {bad}")
print("  only http(s) URLs survive:", [u for _t, u, _s in rows])

print()
print("TOTAL MEDIA/SEARCH FAILURES:", len(FAILS))
for f in FAILS:
    print("  ", f)
assert not FAILS, "media/search regressions"
print("ALL MEDIA AND SEARCH TESTS PASSED")
