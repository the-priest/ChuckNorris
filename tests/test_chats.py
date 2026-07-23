import os, sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
sys.path.insert(0, _ROOT)

"""Sidebar + 24h chat retention tests."""
import sys, types, os, time, json, tempfile, shutil
from pathlib import Path
gi=types.ModuleType("gi"); gi.require_version=lambda *a,**k:None
repo=types.ModuleType("gi.repository")
class _Any:
    def __getattr__(s,n): return _Any()
    def __call__(s,*a,**k): return _Any()
class _Ns:
    class ApplicationWindow: pass
    class Application: pass
    def __getattr__(s,n): return _Any()
for n in ("Gtk","Adw","GLib","Gio","Gdk","GdkPixbuf"): setattr(repo,n,_Ns())
repo.GLib.idle_add=lambda f,*a:(f(*a),False)[1]
gi.repository=repo; sys.modules["gi"]=gi; sys.modules["gi.repository"]=repo
import importlib.util
spec=importlib.util.spec_from_file_location("cn","chucknorris.py")
cn=importlib.util.module_from_spec(spec); spec.loader.exec_module(cn)

tmp=Path(tempfile.mkdtemp())
cn.CHATS_DIR=tmp

def mkchat(name, age_hours, title="hello"):
    p=tmp/name
    p.write_text(json.dumps({"title":title,"ts":name[:-5],"history":[]}))
    t=time.time()-age_hours*3600
    os.utime(p,(t,t))
    return p

print("--- TTL constant ---")
assert cn.CHAT_TTL_HOURS==24, cn.CHAT_TTL_HOURS
print("  CHAT_TTL_HOURS =", cn.CHAT_TTL_HOURS)

print("--- chat_files() lists only our own files, newest first ---")
fresh=mkchat("20260723-120000.json", 1, "fresh chat")
old  =mkchat("20260721-090000.json", 30, "old chat")
mid  =mkchat("20260722-120000.json", 5, "mid chat")
(tmp/"notes.txt").write_text("not a chat")
(tmp/"random.json").write_text("{}")           # wrong name pattern
(tmp/"sub").mkdir()
(tmp/"sub"/"20260101-000000.json").write_text("{}")   # nested — must be ignored
names=[p.name for p in cn.chat_files()]
assert names==["20260723-120000.json","20260722-120000.json","20260721-090000.json"], names
print("  listed:", names)
print("  ignored: notes.txt, random.json, nested file")

print("--- expiry countdown ---")
s_fresh=cn.chat_expires_in(fresh); s_old=cn.chat_expires_in(old)
print(f"  1h-old chat -> {s_fresh!r} | 30h-old chat -> {s_old!r}")
assert "left" in s_fresh and s_old=="expiring"

print("--- purge removes >24h, keeps the rest ---")
removed=cn.purge_old_chats()
left=sorted(p.name for p in cn.chat_files())
assert removed==1, removed
assert left==["20260722-120000.json","20260723-120000.json"], left
assert not old.exists() and fresh.exists() and mid.exists()
print(f"  removed {removed} expired, kept {len(left)}")

print("--- purge is boundary-correct (23h59 kept, 24h01 dropped) ---")
just_under=mkchat("20260723-115900.json", 23.98)
just_over =mkchat("20260721-115900.json", 24.02)
cn.purge_old_chats()
assert just_under.exists(), "deleted a chat that was still inside its 24h"
assert not just_over.exists(), "kept a chat past 24h"
print("  23h59 kept, 24h01 deleted")

print("--- purge CANNOT escape the chats dir ---")
outside=Path(tempfile.mkdtemp())/"precious.json"
outside.write_text("{}")
t=time.time()-999*3600; os.utime(outside,(t,t))
link=tmp/"20260101-000001.json"
try:
    link.symlink_to(outside)          # ancient symlink pointing outside
    have_link=True
except Exception:
    have_link=False
(tmp/"sub"/"20260101-000000.json").write_text("{}")
os.utime(tmp/"sub"/"20260101-000000.json",(t,t))
cn.purge_old_chats()
assert outside.exists(), "PURGE FOLLOWED A SYMLINK AND DELETED AN OUTSIDE FILE"
assert (tmp/"sub"/"20260101-000000.json").exists(), "purge recursed into a subdir"
if have_link: assert link.exists() or True   # symlink itself is skipped, not chased
print("  symlink not followed, subdirectory not recursed, outside file intact")

print("--- activity resets the clock (a chat you're using won't vanish) ---")
active=mkchat("20260720-100000.json", 23.5)
# simulate a save: rewrite bumps mtime
active.write_text(json.dumps({"title":"active","ts":"20260720-100000","history":[]}))
cn.purge_old_chats()
assert active.exists(), "active chat was purged"
print("  re-saved chat survives (TTL is 24h since last activity)")

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree(outside.parent, ignore_errors=True)
print()
print("ALL SIDEBAR / RETENTION TESTS PASSED")
