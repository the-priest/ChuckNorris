"""gtkstub.py — a GTK4/libadwaita stub faithful enough to construct the REAL
ChuckWindow and drive whole conversations through it.

Not a mock of Chuck's logic: the app's own code runs unmodified. Only the
toolkit is simulated (widget tree, css classes, label text, timers).
"""
import sys, types

_TIMERS = {}
_TIMER_SEQ = [0]


class _WidgetMeta(type):
    """Lets class-level constructors like Gtk.Picture.new_for_filename() work."""
    def __getattr__(cls, name):
        return lambda *a, **k: cls()


class Widget(metaclass=_WidgetMeta):
    def __init__(self, **kw):
        self.kids = []
        self.parent = None
        self.classes = set()
        self._text = kw.get("label", "")
        self._icon = kw.get("icon_name", "")
        self._sensitive = True
        self._visible = True
        self._active = kw.get("active", False)
        self._tooltip = ""
        self._child = None
        self._handlers = {}
        self._buf = Buffer()
        self.props = types.SimpleNamespace(active_window=None)

    # tree
    def append(self, w):
        if isinstance(w, Widget):
            w.parent = self
        self.kids.append(w)

    def prepend(self, w):
        if isinstance(w, Widget):
            w.parent = self
        self.kids.insert(0, w)

    def remove(self, w):
        if w in self.kids:
            self.kids.remove(w)
            if isinstance(w, Widget):
                w.parent = None

    def get_first_child(self):
        return self.kids[0] if self.kids else None

    def get_next_sibling(self):
        p = self.parent
        if p is None:
            return None
        try:
            i = p.kids.index(self)
        except ValueError:
            return None
        return p.kids[i + 1] if i + 1 < len(p.kids) else None

    def set_child(self, c):
        self._child = c
        if isinstance(c, Widget):
            c.parent = self
    def get_child(self): return self._child
    def add_overlay(self, w): self.append(w)

    # text / state
    def set_text(self, t): self._text = t
    def get_text(self): return self._text
    def set_label(self, t): self._text = t
    def get_label(self): return self._text
    def set_markup(self, t): self._text = t
    def set_icon_name(self, n): self._icon = n
    def get_icon_name(self): return self._icon
    def set_sensitive(self, v): self._sensitive = bool(v)
    def get_sensitive(self): return self._sensitive
    def set_visible(self, v): self._visible = bool(v)
    def get_visible(self): return self._visible
    def set_active(self, v):
        self._active = bool(v)
        for cb in self._handlers.get("toggled", []):
            cb(self)
    def get_active(self): return self._active
    def set_tooltip_text(self, t): self._tooltip = t

    # css
    def add_css_class(self, c): self.classes.add(c)
    def remove_css_class(self, c): self.classes.discard(c)
    def has_css_class(self, c): return c in self.classes

    # signals
    def connect(self, sig, cb, *a):
        self._handlers.setdefault(sig, []).append(cb)
        return len(self._handlers[sig])

    def emit(self, sig, *a):
        for cb in self._handlers.get(sig, []):
            cb(self, *a)

    def click(self):
        for cb in self._handlers.get("clicked", []):
            cb(self)

    # text buffer (TextView)
    def get_buffer(self):
        return self._buf

    # everything else is a no-op that returns another widget.
    # CRITICAL: private names must still raise AttributeError, otherwise
    # hasattr(self,"_x") and getattr(self,"_x",None) always succeed — which
    # both breaks this stub and hides real bugs in the app under test.
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def anything(*a, **k):
            return Widget()
        return anything

    # descendant search helper for tests
    def walk(self):
        yield self
        for k in self.kids:
            if isinstance(k, Widget):
                yield from k.walk()
        if isinstance(self._child, Widget):
            yield from self._child.walk()


class Buffer:
    def __init__(self): self.text = ""
    def set_text(self, t, *a): self.text = t
    def get_text(self, *a): return self.text
    def get_start_iter(self): return 0
    def get_end_iter(self): return len(self.text)
    def get_bounds(self): return (0, len(self.text))
    def get_line_count(self): return self.text.count("\n") + 1


class Enum:
    def __getattr__(self, n): return n


class _AnyThing:
    """Callable AND attribute-traversable: satisfies Gtk.ContentFit.COVER,
    Gtk.Picture.new_for_filename(...), Gtk.Whatever(...) alike."""
    def __call__(self, *a, **k):
        return Widget(**k)
    def __getattr__(self, n):
        return _AnyThing()


_widget_factory = _AnyThing()


class GioNS:
    class ApplicationFlags:
        DEFAULT_FLAGS = 0
    def __getattr__(self, n): return _widget_factory


class GtkNS:
    Box = Label = Button = ToggleButton = CheckButton = Revealer = Widget
    ScrolledWindow = TextView = Overlay = Picture = Spinner = Widget
    EventControllerKey = CssProvider = Widget
    Orientation = Enum(); Align = Enum(); WrapMode = Enum()
    RevealerTransitionType = Enum(); PolicyType = Enum()
    STYLE_PROVIDER_PRIORITY_APPLICATION = 600

    class StyleContext:
        @staticmethod
        def add_provider_for_display(*a, **k): pass

    def __getattr__(self, n): return _widget_factory


class AppWindowBase(Widget):
    def __init__(self, *a, **k):
        Widget.__init__(self)
        self._content = None
    def set_content(self, c):
        self._content = c
        if isinstance(c, Widget):
            c.parent = self
    def get_content(self): return self._content
    def present(self): pass
    def set_default_size(self, *a): pass
    def set_title(self, *a): pass


class AdwNS:
    ApplicationWindow = AppWindowBase
    Window = AppWindowBase

    class Application(Widget):
        def __init__(self, *a, **k):
            Widget.__init__(self)
        def do_startup(self, *a): pass
        def run(self, *a): return 0

    HeaderBar = ToolbarView = Widget

    @staticmethod
    def init(): pass

    def __getattr__(self, n): return _widget_factory


class GLibNS:
    @staticmethod
    def idle_add(fn, *a):
        try:
            fn(*a)
        except Exception as e:
            GLibNS.errors.append(("idle_add", fn, e))
        return 0
    errors = []

    @staticmethod
    def timeout_add(ms, fn, *a):
        _TIMER_SEQ[0] += 1
        _TIMERS[_TIMER_SEQ[0]] = fn
        return _TIMER_SEQ[0]

    @staticmethod
    def timeout_add_seconds(s, fn, *a):
        return GLibNS.timeout_add(s * 1000, fn)

    @staticmethod
    def source_remove(i):
        _TIMERS.pop(i, None)
        return True

    def __getattr__(self, n): return _widget_factory


class GdkNS:
    KEY_Return = 65293
    KEY_KP_Enter = 65421

    class ModifierType:
        SHIFT_MASK = 1

    class Display:
        @staticmethod
        def get_default(): return Widget()

    def __getattr__(self, n): return _widget_factory


def install():
    """Install the stub into sys.modules so `import gi` picks it up."""
    gi = types.ModuleType("gi")
    gi.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.Gtk = GtkNS()
    repo.Adw = AdwNS()
    repo.GLib = GLibNS()
    repo.Gdk = GdkNS()
    repo.Gio = GioNS()
    repo.GdkPixbuf = GtkNS()
    gi.repository = repo
    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo
    return repo


def fire_timers(n=1):
    """Run every registered timer callback n times (simulates the GLib loop)."""
    for _ in range(n):
        for tid, fn in list(_TIMERS.items()):
            try:
                if fn() is False:
                    _TIMERS.pop(tid, None)
            except Exception as e:
                GLibNS.errors.append(("timer", fn, e))
