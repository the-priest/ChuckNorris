"""palette.py — the command palette's matching (Ctrl+P). Pure: no GTK.

Every action Chuck has is an item: a title, a group, a few keywords and a
shortcut. Typing ranks them: a title that starts with what you typed beats
one that contains it, which beats one whose words you typed out of order,
which beats one whose letters merely appear in order ("nmis" finds "New
mission"). An empty query shows everything, grouped.
"""
import re

GROUPS = ("Waiting for you", "Agent OS", "Processes", "Missions", "Programs", "Chat", "Code",
          "Folder", "Models", "Voice", "Chats", "Settings", "Commands")


def score(query, item):
    """How well `query` matches an item -> 0 (no match) .. ~1000."""
    q = " ".join((query or "").lower().split())
    if not q:
        return 1
    title = item["title"].lower()
    hay = " ".join([title, item.get("group", "").lower(), item.get("keywords", "").lower()])
    if title.startswith(q):
        return 1000 - len(title)
    words = re.findall(r"[a-z0-9/]+", title)
    if any(w.startswith(q) for w in words):
        return 900 - len(title)
    i = title.find(q)
    if i >= 0:
        return 800 - i - len(title) // 4
    if q in hay:
        return 700 - len(hay) // 8
    parts = q.split()
    if len(parts) > 1 and all(p in hay for p in parts):
        return 600 - len(hay) // 8
    # the letters in order, in the title (a few gaps are fine)
    pos, gaps = 0, 0
    for ch in q.replace(" ", ""):
        j = title.find(ch, pos)
        if j < 0:
            return 0
        gaps += j - pos
        pos = j + 1
    return max(1, 400 - gaps * 8 - len(title) // 4)


def rank(items, query, limit=60):
    """The items that match, best first; with no query, in group order."""
    q = (query or "").strip()
    if not q:
        order = {g: i for i, g in enumerate(GROUPS)}
        return sorted(items, key=lambda it: (order.get(it.get("group"), 99), it.get("order", 0)))[:400]
    scored = [(score(q, it), n, it) for n, it in enumerate(items)]
    return [it for s, _n, it in sorted((x for x in scored if x[0] > 0),
                                       key=lambda x: (-x[0], x[1]))][:limit]
