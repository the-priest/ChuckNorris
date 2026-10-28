//! The Chuck window: the conversation on the left, with what Chuck did in a
//! lane under each reply; the job's checks, commands and spend on the right,
//! in sections you open when you want them.

use crate::chat::{self, Entry, Saved};
use crate::md::{self, Part};
use crate::panel::Panel;
use adw::prelude::*;
use chuck_core::{AgentId, Kernel, ProcInfo, Role};
use chuck_model::{fmt_tokens, fmt_usd, ChatModel};
use chuck_runtime::{setup, Event, Events, Job, JobReport, Outcome, Runtime};
use chuck_store::Settings;
use gtk::{gdk, glib, pango};
use std::cell::{Cell, RefCell};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// A lane shows this many of its latest actions; the rest fold into a
/// "N earlier actions" row you can open. Nothing is ever removed.
const LANE_KEEP: usize = 12;

/// How the window is started: normally from the settings; a test or a
/// screenshot can hand it a model and a scripted session.
#[derive(Default, Clone)]
pub struct Options {
    pub model: Option<Arc<dyn ChatModel>>,
    pub folder: Option<PathBuf>,
    /// Send this as soon as the window is up.
    pub send: Option<String>,
    /// Save a render of the window here after this many ms, then quit.
    pub shot: Option<(PathBuf, u64)>,
    pub width: i32,
    pub height: i32,
    /// Open Settings once the window is up.
    pub settings: bool,
    /// Sections of the right panel to open (else: what the user had open).
    pub open: Option<Vec<String>>,
    /// The web for a scripted session (a real one gets it from the settings).
    pub web: Option<Arc<chuck_web::Web>>,
    /// Start in Chat mode (true) or in Build (false); else as last time.
    pub chat: Option<bool>,
    /// Start listening once the window is up (as if the mic was clicked).
    pub listen: bool,
    /// Open the command palette once the window is up, with this typed.
    pub palette: Option<String>,
    /// Strike the welcome screen's code net here (renders of the window).
    pub poke: Option<(f64, f64)>,
    /// Pictures to attach before anything is sent (as if pasted).
    pub attach: Vec<PathBuf>,
}

struct Running {
    stop: Arc<AtomicBool>,
    answers: Sender<String>,
    steer: Sender<String>,
    events: Receiver<Event>,
    kernel: Arc<Kernel>,
    done: Receiver<JobReport>,
    started: Instant,
}

struct Ui {
    window: adw::ApplicationWindow,
    stack: gtk::Stack,
    transcript: gtk::Box,
    input: gtk::TextView,
    hint: gtk::Label,
    send: gtk::Button,
    stop: gtk::Button,
    status: gtk::Label,
    subtitle: gtk::Label,
    meter: gtk::Label,
    folder_btn: gtk::Button,
    new_btn: gtk::Button,
    build_btn: gtk::ToggleButton,
    chat_btn: gtk::ToggleButton,
    mic: gtk::Button,
    speaker: gtk::ToggleButton,
    /// Chuck himself, popping up with what the Lead has to say.
    buddy: crate::buddy::Buddy,
    hero: gtk::Label,
    lede: gtk::Label,
    /// The Recent list on the left, and its search.
    chats: gtk::Box,
    chats_search: gtk::SearchEntry,
    /// The screenshots waiting to go with the next message, above the box you type in.
    attach: gtk::Box,
}

struct Lane {
    older: gtk::Expander,
    older_box: gtk::Box,
    recent: gtk::Box,
    folded: usize,
}

struct State {
    ui: Ui,
    panel: Panel,
    opts: Options,
    folder: Option<PathBuf>,
    saved: Saved,
    running: Option<Running>,
    names: HashMap<AgentId, (String, Role)>,
    models: HashMap<AgentId, String>,
    lane: Option<Lane>,
    /// The file being written live: (who, its path, the card's head, its newest lines).
    writing: Option<(AgentId, String, gtk::Label, gtk::Label)>,
    /// The cards of files being written this reply: gone once the reply is in (its action lines say what was
    /// written), so the conversation keeps only what was said and done.
    live_cards: Vec<gtk::Box>,
    /// Screenshots pasted for the next message.
    pasted: Vec<PathBuf>,
    last_ps: Vec<ProcInfo>,
    /// Chat mode: one model answers, researches, and hands work to Build.
    chat: bool,
    /// What the "do it in Build" button does.
    on_handoff: Option<Rc<dyn Fn(String)>>,
    /// The Keep going button: carry on the paused job.
    on_continue: Option<Rc<dyn Fn()>>,
    /// Open Settings (a paused job's "Spending limits…").
    on_settings: Option<Rc<dyn Fn()>>,
    /// Stop pressed: an automatic "keep going" that was about to start doesn't.
    auto_cancel: Rc<Cell<bool>>,
    /// MCP servers: started with the window, shared by every job, ended with it.
    mcp: Arc<chuck_mcp::Manager>,
    /// Language servers: started on first use, ended with the window.
    lsp: Arc<chuck_code::Manager>,
    /// Daemons and reminders, and the reports they filed.
    daemons: chuck_daemon::Registry,
    inbox: chuck_daemon::Inbox,
    /// Daemons looking right now: their Stop, where their report arrives, why they woke.
    daemon_live: HashMap<i64, (Arc<AtomicBool>, Receiver<chuck_runtime::daemon::DaemonReport>, String)>,
    /// A trigger check in flight (it runs off the main thread).
    daemon_check: Option<Receiver<Vec<Checked>>>,
    /// Replies read aloud, and the microphone.
    voice: chuck_voice::Voice,
    ears: chuck_voice::Ears,
    /// What the microphone hears, while it listens.
    heard: Option<Receiver<chuck_voice::Heard>>,
    /// Long-term memory (shared with every job).
    memory: Arc<chuck_store::memory::Memory>,
    /// Cards still being worked out off the main thread (/doctor, /update).
    later: Vec<Receiver<Entry>>,
    /// A line for the status bar from a background check (an update is out).
    notice: Option<Receiver<String>>,
    tokens: u64,
    spent: f64,
    /// Prompt tokens sent, and how many of them the API's cache served.
    prompt: u64,
    cached: u64,
    unsaved: bool,
}

type Shared = Rc<RefCell<State>>;
/// A daemon's trigger, looked at: (id, due, why, its new state).
type Checked = (i64, bool, String, serde_json::Value);

fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let text = crate::shown(text);
    let l = gtk::Label::builder().label(&*text).xalign(0.0).wrap(true).wrap_mode(pango::WrapMode::WordChar).build();
    for c in classes {
        l.add_css_class(c);
    }
    l
}

/// A button that reads `text` aloud, and stops it while it reads (whether
/// replies are read aloud by themselves or not: this is asked for).
fn read_aloud(voice: &chuck_voice::Voice, status: &gtk::Label, text: &str) -> gtk::Button {
    let b = gtk::Button::from_icon_name("chuck-speaker-symbolic");
    b.add_css_class("flat");
    b.add_css_class("read-aloud");
    b.set_valign(gtk::Align::Center);
    b.set_tooltip_text(Some("Read this aloud"));
    let mine = Rc::new(std::cell::Cell::new(0u64));
    let (voice, status, text) = (voice.clone(), status.clone(), text.to_string());
    b.connect_clicked(move |b| {
        if voice.speaking(mine.get()) {
            voice.stop();
            return;
        }
        let p = chuck_voice::Prefs::from(&Settings::load());
        if voice.engine_for(&p).is_none() {
            status.set_label(&format!("Can't read aloud: {}", voice.status(&p)));
            return;
        }
        mine.set(voice.speak(&text, &p));
        b.set_icon_name("chuck-stop-symbolic");
        b.set_tooltip_text(Some("Stop reading"));
        // back to "read" when it's done (or another one started)
        let (b2, v2, m2) = (b.clone(), voice.clone(), mine.clone());
        glib::timeout_add_local(Duration::from_millis(300), move || {
            if v2.speaking(m2.get()) {
                return glib::ControlFlow::Continue;
            }
            b2.set_icon_name("chuck-speaker-symbolic");
            b2.set_tooltip_text(Some("Read this aloud"));
            glib::ControlFlow::Break
        });
    });
    b
}

fn markup(m: &str, classes: &[&str]) -> gtk::Label {
    let l = label("", classes);
    l.set_markup(&crate::shown(m));
    l
}

fn short(model: &str) -> &str {
    model.rsplit('/').next().unwrap_or(model)
}

fn one_line(s: &str, n: usize) -> String {
    let flat: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= n { flat } else { format!("{}…", flat.chars().take(n - 1).collect::<String>()) }
}

/// A reply's prose and code, as widgets.
fn render(text: &str) -> gtk::Box {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 8);
    for p in md::parse(text) {
        match p {
            Part::Prose(m) => {
                let l = markup(&m, &["prose"]);
                l.set_selectable(true);
                b.append(&l);
            }
            Part::Code { lang, text } => {
                let frame = gtk::Box::new(gtk::Orientation::Vertical, 0);
                frame.add_css_class("code-block");
                if !lang.is_empty() {
                    let k = label(&lang.to_uppercase(), &["card-kind"]);
                    k.set_margin_start(12);
                    k.set_margin_top(6);
                    frame.append(&k);
                }
                let code = label(&text, &["code-body"]);
                code.set_wrap(false);
                code.set_selectable(true);
                let sc = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Automatic)
                    .vscrollbar_policy(gtk::PolicyType::Never).child(&code).build();
                frame.append(&sc);
                b.append(&frame);
            }
            Part::Table { head, rows } => {
                let grid = gtk::Grid::builder().column_spacing(18).row_spacing(0).build();
                grid.add_css_class("md-table");
                let cell = |m: &str, head: bool, row: usize| {
                    let l = markup(&if head { format!("<b>{m}</b>") } else { m.to_string() }, &["prose", "md-cell"]);
                    l.set_selectable(true);
                    l.set_max_width_chars(28);
                    l.set_valign(gtk::Align::Start);
                    if head {
                        l.add_css_class("md-head");
                    } else if row % 2 == 1 {
                        l.add_css_class("md-alt");
                    }
                    l
                };
                for (c, h) in head.iter().enumerate() {
                    grid.attach(&cell(h, true, 0), c as i32, 0, 1, 1);
                }
                for (r, row) in rows.iter().enumerate() {
                    for (c, v) in row.iter().enumerate() {
                        grid.attach(&cell(v, false, r), c as i32, r as i32 + 1, 1, 1);
                    }
                }
                // framed, as wide as it needs up to the chat's width (long cells wrap)
                let frame = gtk::Box::new(gtk::Orientation::Vertical, 0);
                frame.add_css_class("md-table-frame");
                frame.set_halign(gtk::Align::Start);
                frame.append(&grid);
                b.append(&frame);
            }
        }
    }
    b
}

fn card(kind: &str, class: &str) -> (gtk::Box, gtk::Box, gtk::Box) {
    let c = gtk::Box::new(gtk::Orientation::Vertical, 0);
    c.add_css_class("card");
    if !class.is_empty() {
        c.add_css_class(class);
    }
    c.set_margin_top(10);
    let head = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    head.add_css_class("card-head");
    let k = label(kind, &["card-kind"]);
    k.set_wrap(false);
    k.set_hexpand(true);
    head.append(&k);
    let body = gtk::Box::new(gtk::Orientation::Vertical, 6);
    body.add_css_class("card-body");
    let foot = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    foot.add_css_class("card-foot");
    c.append(&head);
    c.append(&body);
    c.append(&foot);
    (c, body, foot)
}

/// What the Lead saw in the browser: errors up top, the page as a picture,
/// what a vision model made of it, and the rest one click away.
fn browser_card(b: &chat::Browsed) -> gtk::Box {
    let n = b.errors.len();
    let (c, body, _foot) = card("BROWSER", if n > 0 || !b.failed.is_empty() { "gap-card" } else { "ask-card" });
    let top = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    let chip = label(&if n > 0 { format!("{n} JS error{}", if n == 1 { "" } else { "s" }) } else { "no JS errors".into() },
                     &["chip", if n > 0 { "chip-bad" } else { "chip-ok" }]);
    chip.set_wrap(false);
    chip.set_valign(gtk::Align::Center);
    top.append(&chip);
    let what = label(&format!("{} · {}×{} · {:.1}s{}{}", b.url, b.size.0, b.size.1, b.ms as f64 / 1000.0,
                              if b.engine.is_empty() { String::new() } else { format!(" · {}", b.engine) },
                              if b.title.is_empty() { String::new() } else { format!(" · {}", b.title) }), &["cmd-text"]);
    what.set_hexpand(true);
    what.set_selectable(true);
    top.append(&what);
    body.append(&top);
    for e in b.errors.iter().take(5) {
        body.append(&markup(&format!("<span foreground=\"#F87171\">✗</span> {}", md::escape(e)), &["cmd-text"]));
    }
    if let Some(p) = b.shot.as_deref().filter(|p| Path::new(p).is_file()) {
        let pic = gtk::Picture::for_filename(p);
        pic.set_content_fit(gtk::ContentFit::Contain);
        pic.set_can_shrink(true);
        pic.set_halign(gtk::Align::Start);
        // as wide as the card allows, never taller than this
        let h = (b.size.1 as f64 * 620.0 / b.size.0.max(1) as f64).clamp(120.0, 320.0);
        pic.set_size_request(-1, h as i32);
        pic.set_tooltip_text(Some(p));
        body.append(&pic);
    }
    if let Some(l) = &b.look {
        body.append(&markup(&format!("<span foreground=\"#8E8E9E\">What it looks like:</span> {}", md::escape(l.trim())),
                            &["prose"]));
    }
    let more = |title: String, items: &[String]| {
        let inner = gtk::Box::new(gtk::Orientation::Vertical, 2);
        for it in items {
            let l = label(it, &["cmd-text"]);
            l.set_selectable(true);
            inner.append(&l);
        }
        let e = gtk::Expander::new(Some(&title));
        e.set_child(Some(&inner));
        e.set_expanded(false);
        e
    };
    for (name, items) in [("JS errors", &b.errors), ("Failed requests", &b.failed), ("Warnings", &b.warnings),
                          ("Actions", &b.steps), ("Not allowed to reach", &b.refused)] {
        if !items.is_empty() && !(name == "JS errors" && items.len() <= 5) {
            body.append(&more(format!("{name} ({})", items.len()), items));
        }
    }
    c
}

impl State {
    fn append(&mut self, w: &impl IsA<gtk::Widget>) {
        self.ui.transcript.append(w);
        self.ui.stack.set_visible_child_name("chat");
    }

    /// Record an entry and draw it.
    fn add(&mut self, e: Entry) {
        self.draw(&e);
        self.saved.entries.push(e);
        self.unsaved = true;
    }

    /// Record an entry that's already on screen (an answered card).
    fn record(&mut self, e: Entry) {
        self.saved.entries.push(e);
        self.unsaved = true;
    }

    fn draw(&mut self, e: &Entry) {
        match e {
            Entry::Pasted { paths } => {
                let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
                row.add_css_class("user-row");
                row.set_halign(gtk::Align::End);
                for p in paths {
                    let pic = gtk::Picture::for_filename(p);
                    pic.set_content_fit(gtk::ContentFit::Cover);
                    pic.set_size_request(180, 112);
                    pic.add_css_class("pasted-shot");
                    pic.set_tooltip_text(Some(p));
                    let b = gtk::Button::builder().child(&pic).build();
                    b.add_css_class("flat");
                    b.add_css_class("pasted-btn");
                    let path = PathBuf::from(p);
                    b.connect_clicked(move |_| launch(path.clone()));
                    row.append(&b);
                }
                self.append(&row);
            }
            Entry::User { text } | Entry::Steer { text } => {
                let steer = matches!(e, Entry::Steer { .. });
                let row = gtk::Box::new(gtk::Orientation::Vertical, 2);
                row.add_css_class("user-row");
                row.set_halign(gtk::Align::End);
                if steer {
                    row.append(&label("to Chuck, while it works", &["steer-note"]));
                }
                let bubble = gtk::Box::new(gtk::Orientation::Vertical, 0);
                bubble.add_css_class("user-bubble");
                if steer {
                    bubble.add_css_class("steer-bubble");
                }
                let l = label(text, &[]);
                l.set_selectable(true);
                l.set_max_width_chars(70);
                bubble.append(&l);
                row.append(&bubble);
                self.append(&row);
                self.lane = None;
            }
            Entry::Lead { text, model, .. } => {
                let col = gtk::Box::new(gtk::Orientation::Vertical, 6);
                col.add_css_class("bot-row");
                let by = gtk::Box::new(gtk::Orientation::Horizontal, 8);
                let av = label("C", &["avatar"]);
                av.set_xalign(0.5);
                by.append(&av);
                by.append(&label("Chuck", &["byline"]));
                let meta = label(model, &["byline-meta"]);
                meta.set_hexpand(true);
                by.append(&meta);
                by.append(&read_aloud(&self.voice, &self.ui.status, text));
                col.append(&by);
                let body = render(text);
                body.set_margin_start(28);
                col.append(&body);
                self.append(&col);
                self.lane = None;
            }
            Entry::Act { markup: m } => {
                if self.lane.is_none() {
                    let root = gtk::Box::new(gtk::Orientation::Vertical, 3);
                    root.add_css_class("lane");
                    root.set_margin_top(8);
                    let older_box = gtk::Box::new(gtk::Orientation::Vertical, 3);
                    let older = gtk::Expander::new(Some(""));
                    older.add_css_class("lane-older");
                    older.set_child(Some(&older_box));
                    older.set_visible(false);
                    root.append(&older);
                    let recent = gtk::Box::new(gtk::Orientation::Vertical, 3);
                    root.append(&recent);
                    self.append(&root);
                    self.lane = Some(Lane { older, older_box, recent, folded: 0 });
                }
                let lane = self.lane.as_mut().unwrap();
                let l = markup(m, &["act"]);
                lane.recent.append(&l);
                // fold the oldest visible line once the lane is long: still there, one click away
                let mut n = 0;
                let mut c = lane.recent.first_child();
                while let Some(w) = c {
                    n += 1;
                    c = w.next_sibling();
                }
                if n > LANE_KEEP {
                    if let Some(first) = lane.recent.first_child() {
                        lane.recent.remove(&first);
                        lane.older_box.append(&first);
                        lane.folded += 1;
                        lane.older.set_label(Some(&format!("{} earlier actions", lane.folded)));
                        lane.older.set_visible(true);
                    }
                }
            }
            Entry::Card { kind, class, text } => {
                let (c, body, foot) = card(kind, class);
                body.append(&render(text));
                if let Some(head) = c.first_child().and_downcast::<gtk::Box>() {
                    head.append(&read_aloud(&self.voice, &self.ui.status, text));
                }
                // a job that can't start over a limit: the way to the limits, right there
                if text.contains("spending limit") {
                    let raise = gtk::Button::with_label("Spending limits…");
                    raise.add_css_class("pill-sm");
                    if let Some(open) = self.on_settings.clone() {
                        raise.connect_clicked(move |_| open());
                    }
                    foot.append(&raise);
                }
                self.append(&c);
                self.lane = None;
            }
            Entry::Paused { head, text } => {
                let (c, body, foot) = card(head, "gap-card");
                body.append(&render(text));
                if let Some(h) = c.first_child().and_downcast::<gtk::Box>() {
                    h.append(&read_aloud(&self.voice, &self.ui.status, text));
                }
                let btn = gtk::Button::with_label("Keep going");
                btn.add_css_class("suggested-action");
                btn.add_css_class("pill-sm");
                btn.set_tooltip_text(Some("Carry on exactly where it stopped, with another job budget (`/continue`)"));
                let can = self.folder.as_ref().and_then(|f| chuck_runtime::resume::load(f)).is_some();
                btn.set_sensitive(can);
                if let Some(go) = self.on_continue.clone() {
                    btn.connect_clicked(move |b| {
                        b.set_sensitive(false);
                        go();
                    });
                }
                foot.append(&btn);
                if text.contains("spending limit") {
                    let raise = gtk::Button::with_label("Spending limits…");
                    raise.add_css_class("pill-sm");
                    if let Some(open) = self.on_settings.clone() {
                        raise.connect_clicked(move |_| open());
                    }
                    foot.append(&raise);
                }
                foot.append(&label(if can { "Criteria, evidence and the conversation are kept." } else {
                    "Nothing left to carry on here." }, &["panel-dim"]));
                self.append(&c);
                self.lane = None;
            }
            Entry::Gallery { query, items } => {
                let (c, body, _foot) = card(&format!("PICTURES · {}", items.len()), "ask-card");
                body.append(&label(query, &["panel-dim"]));
                crate::media::gallery(&body, items);
                self.append(&c);
                self.lane = None;
            }
            Entry::Videos { query, items } => {
                let (c, body, _foot) = card(&format!("VIDEOS · {}", items.len()), "ask-card");
                body.append(&label(query, &["panel-dim"]));
                let proxy = Settings::load().str("proxy").map(String::from);
                crate::media::videos(&body, items, proxy);
                self.append(&c);
                self.lane = None;
            }
            Entry::Browser(b) => {
                let c = browser_card(b);
                self.append(&c);
                self.lane = None;
            }
            Entry::Handoff { request } => {
                let (c, body, foot) = card("TO DO ON YOUR MACHINE", "ask-card");
                body.append(&render(request));
                let btn = gtk::Button::with_label("Do it in Build");
                btn.add_css_class("suggested-action");
                btn.add_css_class("pill-sm");
                btn.set_tooltip_text(Some("Switch to Build and send this — it can act on your machine"));
                if let Some(go) = self.on_handoff.clone() {
                    let r = request.clone();
                    btn.connect_clicked(move |b| {
                        b.set_sensitive(false);
                        go(r.clone());
                    });
                }
                foot.append(&btn);
                foot.append(&label("Nothing runs until you press it.", &["panel-dim"]));
                self.append(&c);
                self.lane = None;
            }
        }
    }

    /// Draw a whole saved conversation.
    fn redraw(&mut self) {
        while let Some(c) = self.ui.transcript.first_child() {
            self.ui.transcript.remove(&c);
        }
        self.lane = None;
        let entries = std::mem::take(&mut self.saved.entries);
        for e in &entries {
            self.draw(e);
        }
        self.saved.entries = entries;
        self.ui.stack.set_visible_child_name(if self.saved.entries.is_empty() { "welcome" } else { "chat" });
    }

    /// Where this conversation is kept: the project folder's, or (in Chat
    /// mode with no folder) the loose one.
    fn key(&self) -> Option<PathBuf> {
        self.folder.clone().or_else(|| self.chat.then(chat::loose))
    }

    fn save(&mut self) {
        if let Some(k) = self.key() {
            if self.unsaved {
                self.saved.folder = self.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_default();
                chat::save(&k, &self.saved);
                self.unsaved = false;
            }
        }
    }

    /// Chat mode on or off: remembered, shown, and (with no folder) its own conversation.
    fn set_mode(&mut self, chat: bool, shared: &Shared) {
        if self.chat == chat {
            return;
        }
        if self.running.is_some() {
            // a job in flight keeps its mode; the switch shows what's true
            self.ui.chat_btn.set_active(self.chat);
            self.ui.build_btn.set_active(!self.chat);
            return;
        }
        if self.folder.is_none() {
            self.save();
        }
        self.chat = chat;
        let mut s = Settings::load();
        s.set("agent_mode", (!chat).into());
        let _ = s.save();
        self.ui.chat_btn.set_active(chat);
        self.ui.build_btn.set_active(!chat);
        if self.folder.is_none() {
            let f = self.folder.clone();
            self.open_folder(f, shared);
        }
        self.set_running(false);
    }

    fn act(&mut self, m: String) {
        self.add(Entry::Act { markup: m });
    }

    fn who(&self, id: AgentId) -> String {
        self.names.get(&id).map(|(n, _)| n.clone()).unwrap_or_else(|| format!("#{id}"))
    }

    fn is_lead(&self, id: AgentId) -> bool {
        self.names.get(&id).is_some_and(|(n, r)| *r == Role::Lead && (n == "lead" || n == "chuck"))
    }

    fn set_running(&mut self, on: bool) {
        self.ui.stop.set_visible(on);
        self.ui.send.set_tooltip_text(Some(if on { "Tell Chuck (it reads it at its next step)" } else {
            "Send (Enter) · new line: Shift+Enter" }));
        self.ui.hint.set_label(match (on, self.chat) {
            (true, false) => "Type to steer — Chuck reads it at its next step · Esc stops",
            (true, true) => "Add to the question — Chuck reads it before its next step · Esc stops",
            (false, false) => "What should Chuck build or fix? · paste a screenshot with Ctrl+V",
            (false, true) => "Ask Chuck anything · paste a screenshot with Ctrl+V",
        });
        self.ui.hero.set_label(if self.chat { "Ask Chuck anything." } else { "What should Chuck build?" });
        self.ui.lede.set_label(if self.chat { CHAT_LEDE } else { BUILD_LEDE });
        self.ui.folder_btn.set_sensitive(!on);
        self.ui.new_btn.set_sensitive(!on);
        if !on {
            self.ui.status.set_label("");
        }
    }

    fn refresh_header(&self) {
        let folder = self.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_else(|| "no folder".into());
        let access = match &self.folder {
            Some(f) => setup::access_of(&Settings::load(), f).label(),
            None => "",
        };
        // the folder's name is on its button (its path in the tooltip): the line under the title says what Chuck may do
        self.ui.subtitle.set_label(if access.is_empty() { "no folder yet" } else { access });
        self.ui.folder_btn.set_label(&self.folder.as_ref().and_then(|f| f.file_name())
            .map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "Choose a folder".into()));
        self.ui.folder_btn.set_tooltip_text(Some(&format!("{folder} — the project Chuck works on (click to change)")));
        self.ui.meter.set_markup(&format!("{} · <span foreground=\"#A78BFA\" weight=\"bold\">{}</span>",
                                          fmt_tokens(self.tokens), fmt_usd(self.spent)));
    }

    /// The folder's conversation and checkpoints.
    fn open_folder(&mut self, folder: Option<PathBuf>, shared: &Shared) {
        self.save();
        self.folder = folder;
        self.mcp.set_roots(self.folder.iter().cloned().collect());
        self.saved = self.key().as_deref().and_then(chat::load).unwrap_or_default();
        self.redraw();
        self.refresh_header();
        self.panel.reset();
        self.panel.restore(&self.saved.progress);
        refresh_checkpoints(self, shared);
        refresh_daemons(self, shared);
        refresh_chats(self, shared);
    }
}

/// The Recent list: every conversation (search narrows it), this one marked.
fn refresh_chats(st: &State, shared: &Shared) {
    let list = &st.ui.chats;
    while let Some(c) = list.first_child() {
        list.remove(&c);
    }
    let q = st.ui.chats_search.text().to_string();
    let here = st.key().map(|k| chat::path_for(&k));
    let all = chat::list(400);
    let mut shown = 0;
    for l in all.iter().filter(|l| l.matches(&q)).take(120) {
        shown += 1;
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        row.add_css_class("chat-row");
        if Some(&l.path) == here.as_ref() {
            row.add_css_class("chat-current");
        }
        let open = gtk::Button::new();
        open.add_css_class("flat");
        open.add_css_class("chat-open");
        open.set_hexpand(true);
        let col = gtk::Box::new(gtk::Orientation::Vertical, 1);
        let t = gtk::Label::builder().label(&l.title).xalign(0.0).ellipsize(pango::EllipsizeMode::End)
            .max_width_chars(26).build();
        t.add_css_class("chat-title");
        col.append(&t);
        let place = if l.folder.is_empty() { "Chat".to_string() } else {
            Path::new(&l.folder).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| l.folder.clone())
        };
        let meta = format!("{place} · {}{}", chat::ago(l.modified), if l.kind == chat::Kind::Python { " · Python app" } else { "" });
        let m = gtk::Label::builder().label(&meta).xalign(0.0).ellipsize(pango::EllipsizeMode::End).max_width_chars(30).build();
        m.add_css_class("chat-meta");
        col.append(&m);
        open.set_child(Some(&col));
        open.set_tooltip_text(Some(&if l.folder.is_empty() { l.title.clone() } else { format!("{}\n{}", l.title, l.folder) }));
        let (sh, item) = (shared.clone(), l.clone());
        open.connect_clicked(move |_| open_chat(&sh, &item));
        row.append(&open);
        let del = gtk::Button::from_icon_name("chuck-trash-symbolic");
        del.add_css_class("flat");
        del.add_css_class("chat-del");
        del.set_valign(gtk::Align::Center);
        del.set_tooltip_text(Some(if l.kind == chat::Kind::Python { "Delete this Python-app chat" } else { "Delete this conversation" }));
        let (sh, item) = (shared.clone(), l.clone());
        del.connect_clicked(move |_| {
            let Ok(mut st) = sh.try_borrow_mut() else { return };
            if st.running.is_some() && st.key().map(|k| chat::path_for(&k)).as_ref() == Some(&item.path) {
                return; // not the one a job is writing to
            }
            if chat::delete(&item).is_ok() && st.key().map(|k| chat::path_for(&k)).as_ref() == Some(&item.path) {
                st.saved = Saved::default();
                st.unsaved = false;
                st.redraw();
                st.panel.reset();
            }
            refresh_chats(&st, &sh);
        });
        row.append(&del);
        list.append(&row);
    }
    if shown == 0 {
        let e = label(if q.trim().is_empty() { "No conversations yet." } else { "Nothing matches." }, &["panel-dim"]);
        e.set_margin_start(8);
        e.set_margin_top(6);
        list.append(&e);
    }
}

/// Open a conversation from the Recent list (in its folder, or in Chat).
fn open_chat(shared: &Shared, l: &chat::Listed) {
    let Ok(mut st) = shared.try_borrow_mut() else { return };
    if st.running.is_some() {
        st.ui.status.set_label("a job is running: stop it before opening another conversation");
        return;
    }
    st.save();
    match chat::reopen(l) {
        Ok(Some(folder)) if folder.is_dir() => {
            if st.chat != l_is_chat(l) {
                st.chat = l_is_chat(l);
                st.ui.chat_btn.set_active(st.chat);
                st.ui.build_btn.set_active(!st.chat);
            }
            st.open_folder(Some(folder), shared);
        }
        Ok(Some(folder)) => {
            st.ui.status.set_label(&format!("{} no longer exists", folder.display()));
        }
        Ok(None) => {
            st.chat = true;
            st.ui.chat_btn.set_active(true);
            st.ui.build_btn.set_active(false);
            st.open_folder(None, shared);
        }
        Err(e) => st.ui.status.set_label(&format!("couldn't open that conversation: {e}")),
    }
    st.set_running(false);
    refresh_chats(&st, shared);
}

/// Was it a Chat-mode conversation (Chuck answering, nothing built)?
fn l_is_chat(l: &chat::Listed) -> bool {
    l.folder.is_empty() || (l.kind != chat::Kind::Python && chat::load_path(&l.path).is_some_and(|s| s.entries.iter()
        .any(|e| matches!(e, Entry::Lead { chat: true, .. }))))
        || l.kind == chat::Kind::Python
}

fn refresh_checkpoints(st: &State, shared: &Shared) {
    let points = st.folder.as_ref().and_then(|f| chuck_store::Shadow::new(f, &chuck_store::checkpoint::default_store()).ok())
        .map(|sh| sh.points(15)).unwrap_or_default();
    let sh = shared.clone();
    st.panel.set_checkpoints(&points, st.running.is_some(), Rc::new(move |sha: String| rewind(&sh, &sha)));
}

fn rewind(shared: &Shared, sha: &str) {
    let mut st = shared.borrow_mut();
    if st.running.is_some() {
        return;
    }
    let Some(folder) = st.folder.clone() else { return };
    let r = chuck_store::Shadow::new(&folder, &chuck_store::checkpoint::default_store()).and_then(|s| s.restore(sha));
    let text = match r {
        Ok((undo, changed)) => format!("The folder is back to checkpoint `{}`: {} file(s) put back. This rewind is \
            itself a checkpoint (`{}`), so it can be undone from the list.", &sha[..10.min(sha.len())], changed.len(),
            &undo[..10.min(undo.len())]),
        Err(e) => format!("Couldn't rewind: {e}"),
    };
    st.add(Entry::Card { kind: "REWOUND".into(), class: "ask-card".into(), text });
    st.save();
    refresh_checkpoints(&st, shared);
    refresh_chats(&st, shared);
}

/// Open a file with the desktop's app for it.
fn launch(p: PathBuf) {
    let f = gtk::gio::File::for_path(&p);
    gtk::FileLauncher::new(Some(&f)).launch(None::<&gtk::Window>, gtk::gio::Cancellable::NONE, |_| {});
}

/// Where pasted screenshots are kept (outside every project).
fn pasted_dir() -> PathBuf {
    std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from).filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".cache")))
        .unwrap_or_else(std::env::temp_dir).join("chuck").join("pasted")
}

fn clear_attach(st: &State) {
    while let Some(c) = st.ui.attach.first_child() {
        st.ui.attach.remove(&c);
    }
    st.ui.attach.set_visible(false);
}

/// A picture to go with the next message: saved as PNG, shown above the box you type in.
fn attach_texture(shared: &Shared, tex: &gdk::Texture) {
    let dir = pasted_dir();
    let _ = std::fs::create_dir_all(&dir);
    static N: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let path = dir.join(format!("paste-{}-{}.png", chuck_sandbox::now_secs() as u64, N.fetch_add(1, Ordering::SeqCst)));
    if let Err(e) = tex.save_to_png(&path) {
        shared.borrow().ui.status.set_label(&format!("Couldn't keep that picture: {e}"));
        return;
    }
    attach_file(shared, path);
}

fn attach_file(shared: &Shared, path: PathBuf) {
    let mut st = shared.borrow_mut();
    if st.pasted.contains(&path) || st.pasted.len() >= 6 {
        return;
    }
    st.pasted.push(path.clone());
    let chip = gtk::Overlay::new();
    let pic = gtk::Picture::for_filename(&path);
    pic.set_content_fit(gtk::ContentFit::Cover);
    pic.set_size_request(88, 56);
    pic.add_css_class("attach-shot");
    pic.set_tooltip_text(Some(&format!("{} — goes with your next message", path.display())));
    chip.set_child(Some(&pic));
    let x = gtk::Button::from_icon_name("chuck-close-symbolic");
    x.add_css_class("attach-x");
    x.set_halign(gtk::Align::End);
    x.set_valign(gtk::Align::Start);
    x.set_tooltip_text(Some("Leave it out"));
    chip.add_overlay(&x);
    let (sh, c2, p2) = (shared.clone(), chip.clone(), path.clone());
    x.connect_clicked(move |_| {
        let mut st = sh.borrow_mut();
        st.pasted.retain(|p| *p != p2);
        st.ui.attach.remove(&c2);
        st.ui.attach.set_visible(!st.pasted.is_empty());
    });
    st.ui.attach.append(&chip);
    st.ui.attach.set_visible(true);
    st.ui.input.grab_focus();
}

/// The cards of files written this reply leave the conversation.
fn clear_live(st: &mut State) {
    for c in std::mem::take(&mut st.live_cards) {
        st.ui.transcript.remove(&c);
    }
    st.writing = None;
}

fn mark(ok: bool) -> &'static str {
    if ok { "<span foreground=\"#4ADE9A\">✓</span>" } else { "<span foreground=\"#F87171\">✗</span>" }
}

fn on_event(st: &mut State, e: Event, shared: &Shared) {
    let esc = md::escape;
    match e {
        Event::AgentStarted { id, role, name, model, task } => {
            st.names.insert(id, (name.clone(), role));
            st.models.insert(id, short(&model).to_string());
            let _ = task;
        }
        Event::Said { id, text } => {
            let who = st.who(id);
            if st.is_lead(id) {
                let model = st.models.get(&id).cloned().unwrap_or_default();
                let chat = st.names.get(&id).is_some_and(|(n, _)| n == "chuck");
                clear_live(st);
                st.add(Entry::Lead { text: text.clone(), model, chat });
                st.save(); // a reply is never lost, even if the app is killed
            } else {
                st.act(format!("<span foreground=\"#5F5F6E\">{:<6} │ {}</span>", esc(&who), esc(&one_line(&text, 160))));
            }
        }
        Event::Running { id, tool, cmd } => {
            st.panel.command_started(id, &tool, &cmd);
        }
        Event::Output { id, chunk } => st.panel.output(id, &chunk),
        Event::Action { id, tool, arg, ok, summary } => {
            let who = st.who(id);
            let what = match tool.as_str() {
                "bash" | "test" => format!("{} → {}", one_line(&arg, 80), one_line(&summary, 40)),
                "search" => format!("“{}” → {}", one_line(&arg, 70), one_line(&summary, 40)),
                "fetch" if ok => format!("{} ({})", one_line(&summary, 70), chuck_web::domain(&arg)),
                "fetch" => format!("{} → {}", one_line(&arg, 70), one_line(&summary, 70)),
                _ if summary.is_empty() => one_line(&arg, 100),
                _ => one_line(&summary, 120),
            };
            if tool == "bash" || tool == "test" || tool == "mcp" || tool == "browse" {
                st.panel.command_finished(id, ok, &summary);
            }
            if matches!(tool.as_str(), "search" | "fetch" | "images" | "videos") {
                st.panel.research(&who, &tool, &arg, ok, &summary);
            }
            st.act(format!("{} {} <span foreground=\"#8E8E9E\">{}</span>", mark(ok), esc(&tool), esc(&what)));
        }
        Event::Notice { id, text } => {
            let _ = id;
            st.act(format!("<span foreground=\"#F5B454\">! {}</span>", esc(&text)));
        }
        Event::Usage { id, tokens, usd, prompt, cached } => {
            let _ = id;
            st.tokens += tokens;
            st.spent += usd;
            st.prompt += prompt;
            st.cached += cached;
            st.refresh_header();
        }
        Event::Gate { id, task: None, phase, unmet } if st.is_lead(id) => {
            st.panel.set_gate(&phase, &unmet);
            st.saved.progress.phase = phase;
            st.saved.progress.unmet = unmet;
        }
        Event::Board { criteria } => {
            st.panel.set_board(&criteria);
            st.saved.progress.board = criteria;
        }
        Event::Browsed { id, url, title, size, ms, engine, errors, failed, warnings, steps, refused, shot, look } => {
            let _ = id;
            st.add(Entry::Browser(Box::new(chat::Browsed { url, title, size, ms, engine, errors, failed, warnings, steps, refused,
                                                           shot: shot.map(|p| p.display().to_string()), look })));
        }
        Event::Gallery { query, items } => {
            st.add(Entry::Gallery { query, items: items.into_iter().map(|p| chat::Picture {
                path: p.path.display().to_string(), src: p.src, page: p.page, title: p.title }).collect() });
        }
        Event::Videos { query, items } => {
            st.add(Entry::Videos { query, items: items.into_iter().map(|v| chat::Video {
                title: v.title, url: v.url, thumb: v.thumb.map(|t| t.display().to_string()), length: v.length }).collect() });
        }
        Event::Ask { question, options, .. } => {
            let speech = speak(st, &question);
            buddy_says(st, &question, speech);
            let (c, body, foot) = card("CHUCK ASKS", "ask-card");
            body.append(&render(&question));
            let entry = gtk::Entry::builder().hexpand(true)
                .placeholder_text(if options.is_empty() { "Your answer" } else { "Or say what you want instead" }).build();
            let btn = gtk::Button::with_label("Answer");
            btn.add_css_class("suggested-action");
            foot.append(&entry);
            foot.append(&btn);
            let tx = st.running.as_ref().map(|r| r.answers.clone());
            let picks = gtk::Box::new(gtk::Orientation::Vertical, 6);
            let (f2, p2, sh, q) = (foot.clone(), picks.clone(), shared.clone(), question.clone());
            let send: Rc<dyn Fn(String)> = Rc::new(move |a: String| {
                if a.trim().is_empty() {
                    return;
                }
                if let Some(tx) = &tx {
                    let _ = tx.send(a.clone());
                }
                f2.set_sensitive(false);
                p2.set_sensitive(false);
                if let Ok(mut st) = sh.try_borrow_mut() {
                    st.record(Entry::Card { kind: "CHUCK ASKED".into(), class: "ask-card".into(),
                                            text: format!("{q}\n\n**You:** {a}") });
                }
            });
            for (i, o) in options.iter().enumerate() {
                let row = gtk::Box::new(gtk::Orientation::Vertical, 2);
                let head = format!("{}. {}{}", i + 1, o.label, if o.recommended { "  · recommended" } else { "" });
                let title = label(&head, &["prose"]);
                title.set_xalign(0.0);
                row.append(&title);
                if !o.why.is_empty() {
                    let why = label(&o.why, &["sub"]);
                    why.set_xalign(0.0);
                    why.set_wrap(true);
                    row.append(&why);
                }
                let b = gtk::Button::builder().child(&row).hexpand(true).build();
                if o.recommended {
                    b.add_css_class("suggested-action");
                }
                b.set_tooltip_text(Some(if o.recommended { "Chuck's pick — what it does if you don't answer" }
                                        else { "Go this way" }));
                let (s2, l) = (send.clone(), o.label.clone());
                b.connect_clicked(move |_| s2(l.clone()));
                picks.append(&b);
            }
            if !options.is_empty() {
                body.append(&picks);
            }
            let (e2, s2) = (entry.clone(), send.clone());
            btn.connect_clicked(move |_| s2(e2.text().to_string()));
            let e3 = entry.clone();
            entry.connect_activate(move |_| send(e3.text().to_string()));
            st.append(&c);
            st.lane = None;
            entry.grab_focus();
        }
        Event::Confirm { command, why, outside, .. } => {
            buddy_says(st, &if outside.is_empty() { format!("I need your yes for a critical command: {why}") } else {
                format!("I need your yes to reach outside the folder: {}", outside.join(", ")) }, None);
            let out = !outside.is_empty();
            let (c, body, foot) = if out { card("OUTSIDE THE FOLDER — MAY IT?", "ask-card") } else {
                card("CRITICAL — NEEDS YOUR YES", "crit-card") };
            body.append(&label("Chuck wants to run:", &["prose"]));
            let cmd = label(&command, &["cmd-text"]);
            cmd.set_selectable(true);
            body.append(&cmd);
            body.append(&label(&why, &["sub"]));
            if out {
                body.append(&label(&format!("It reaches: {}", outside.join(", ")), &["sub"]));
            }
            let run = gtk::Button::with_label(if out { "Allow once" } else { "Run it" });
            run.add_css_class(if out { "suggested-action" } else { "destructive-action" });
            let no = gtk::Button::with_label("Refuse");
            foot.append(&no);
            foot.append(&run);
            let mut choices = vec![(run, "yes"), (no, "no")];
            if out {
                let job = gtk::Button::with_label("Allow for this job");
                job.set_tooltip_text(Some("Those places stay open to this job's commands until it ends"));
                foot.append(&job);
                choices.push((job, "always"));
            }
            let tx = st.running.as_ref().map(|r| r.answers.clone());
            for (b, word) in choices {
                let (tx, f, sh, cmd) = (tx.clone(), foot.clone(), shared.clone(), command.clone());
                b.connect_clicked(move |_| {
                    if let Some(tx) = &tx {
                        let _ = tx.send(word.into());
                    }
                    f.set_sensitive(false);
                    if let Ok(mut st) = sh.try_borrow_mut() {
                        let said = match word { "yes" if out => "allow once", "yes" => "run it", "always" => "allow for this job", _ => "no" };
                        st.record(Entry::Card { kind: if out { "OUTSIDE THE FOLDER".into() } else { "CRITICAL COMMAND".into() },
                            class: if out { "ask-card".into() } else { "crit-card".into() },
                            text: format!("`{cmd}`\n\nYou said **{said}**.") });
                    }
                });
            }
            st.append(&c);
            st.lane = None;
        }
        Event::Schedule { kind, when, what } => {
            let folder = if kind == "remind" { String::new() } else {
                st.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_default()
            };
            st.daemons.add("", when, &what, &folder, &kind, 150_000);
            refresh_daemons(st, shared);
        }
        // written down for Chuck to be improved (chuck_store::snags), not shown
        Event::Snag { .. } => {}
        Event::Handoff { request } => {
            st.add(Entry::Handoff { request });
            st.save();
        }
        // the reply itself arrives whole (Said); a file being written shows live (Writing)
        Event::Thinking { .. } | Event::Delta { .. } => {}
        Event::Writing { id, path, lines, tail, done } => {
            // the file as the model writes it: one card per file, its newest lines rolling in
            let head_text = format!("{} {path} · {lines} line{}", if done { "WROTE" } else { "WRITING" },
                                    if lines == 1 { "" } else { "s" });
            let same = st.writing.as_ref().is_some_and(|(w, p, _, _)| *w == id && *p == path);
            if !same {
                let c = gtk::Box::new(gtk::Orientation::Vertical, 0);
                c.add_css_class("code-block");
                c.add_css_class("live-write");
                c.set_margin_top(8);
                let head = label(&head_text, &["card-kind", "live-head"]);
                head.set_margin_start(12);
                head.set_margin_top(6);
                let code = label(&tail, &["code-body", "live-code"]);
                code.set_wrap(false);
                code.set_ellipsize(pango::EllipsizeMode::End);
                c.append(&head);
                c.append(&code);
                st.append(&c);
                st.live_cards.push(c.clone());
                st.writing = Some((id, path, head, code));
            } else if let Some((_, _, head, code)) = &st.writing {
                head.set_label(&head_text);
                code.set_label(&crate::shown(&tail));
            }
            if done {
                st.writing = None;
            }
        }
        Event::AgentEnded { .. } | Event::Finished(_) | Event::Gate { .. } => {}
    }
}

/// The files a job made or changed that open by themselves (a page, a
/// picture, a document): buttons that open them, and the folder.
fn result_row(folder: &Path, gate: &chuck_core::DoneGate) -> Option<gtk::Box> {
    let mut files: Vec<String> = Vec::new();
    for e in &gate.evidence {
        if let chuck_core::EvidenceKind::Diff { files: f, .. } = &e.kind {
            for x in f {
                if !files.contains(x) {
                    files.push(x.clone());
                }
            }
        }
    }
    let opens = |f: &str| {
        let ext = Path::new(f).extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
        ["html", "htm", "svg", "png", "jpg", "jpeg", "gif", "webp", "pdf"].contains(&ext.as_str())
    };
    let shown: Vec<&String> = files.iter().filter(|f| opens(f) && folder.join(f).is_file()).take(4).collect();
    if files.is_empty() {
        return None;
    }
    let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    row.add_css_class("result-row");
    row.set_margin_top(6);
    for f in shown {
        let b = gtk::Button::with_label(&format!("▶  Open {}", Path::new(f).file_name().map(|n| n.to_string_lossy())
            .unwrap_or_default()));
        b.add_css_class("suggested-action");
        b.add_css_class("pill-sm");
        b.set_tooltip_text(Some(&folder.join(f).display().to_string()));
        let p = folder.join(f);
        b.connect_clicked(move |_| launch(p.clone()));
        row.append(&b);
    }
    let dir = gtk::Button::with_label("Open folder");
    dir.add_css_class("pill-sm");
    let p = folder.to_path_buf();
    dir.connect_clicked(move |_| launch(p.clone()));
    row.append(&dir);
    Some(row)
}

fn finished(st: &mut State, report: JobReport, shared: &Shared) {
    clear_live(st);
    // the job's final state, read before it is let go
    if let Some(run) = &st.running {
        st.last_ps = run.kernel.ps(true);
    }
    if let Outcome::Failed { error } = &report.outcome {
        chuck_store::crashlog::record("job failed", error);
    }
    let (kind, class) = match &report.outcome {
        Outcome::Done { .. } => ("DONE", "done-card"),
        Outcome::Answered { .. } => ("", ""),
        Outcome::Paused { .. } => ("INCOMPLETE", "gap-card"),
        Outcome::Failed { .. } => ("FAILED", "gap-card"),
    };
    let reply = match &report.outcome {
        Outcome::Done { summary } => summary.clone(),
        Outcome::Answered { text } => text.clone(),
        Outcome::Paused { gaps } => paused_text(gaps),
        Outcome::Failed { error } => error.clone(),
    };
    if !kind.is_empty() {
        let head = format!("{kind} · {} tokens · {}", fmt_tokens(report.tokens), fmt_usd(report.usd));
        let mut text = reply.clone();
        if let Some(cp) = &report.checkpoint {
            text.push_str(&format!("\n\nUndo the whole job: `/undo` (or **Rewind** checkpoint `{}` in the right panel); \
                see every change: `/diff`.", &cp[..10.min(cp.len())]));
        }
        if report.resume.is_some() {
            st.add(Entry::Paused { head, text });
        } else {
            st.add(Entry::Card { kind: head, class: class.into(), text });
        }
        // what it made, one click away: a page or a picture opens, the folder too
        if let Some(folder) = st.folder.clone().filter(|_| matches!(report.outcome, Outcome::Done { .. })) {
            if let Some(row) = result_row(&folder, &report.gate) {
                st.append(&row);
            }
        }
    }
    // keep going on its own when only the job's budget ran out, if the user turned that on (never past the daily limit)
    if let Some(r) = report.resume.as_ref().filter(|r| r.out_of_budget()) {
        let s = Settings::load();
        let day = s.num("budget_day_usd", 0.0, 0.0, 1e5);
        if s.map.get("fleet_auto_continue").and_then(|v| v.as_bool()) == Some(true) && day > 0.0 && r.runs < 200 {
            st.act(format!("<span foreground=\"#A78BFA\">↻ the job's budget ran out: carrying on in 5 s with another \
                (Settings › Jobs; the daily limit {} still holds) — Stop to stay paused</span>", fmt_usd(day)));
            let sh = shared.clone();
            let cancelled = st.auto_cancel.clone();
            cancelled.set(false);
            glib::timeout_add_local_once(Duration::from_secs(5), move || {
                if !cancelled.get() && sh.borrow().running.is_none() {
                    start(&sh, "/continue".into());
                }
            });
        }
    }
    let gate = &report.gate;
    if !gate.criteria.is_empty() || gate.is_done() {
        let unmet: Vec<String> = if gate.is_done() { Vec::new() } else { gate.check().iter().map(|u| u.describe()).collect() };
        let board: Vec<_> = gate.board().into_iter().map(|(c, ev)| (c.id.to_string(), c.text, c.verify, ev)).collect();
        st.panel.set_gate(gate.phase.label(), &unmet);
        st.panel.set_board(&board);
        st.saved.progress.phase = gate.phase.label().to_string();
        st.saved.progress.unmet = unmet;
        st.saved.progress.board = board;
    }
    // the reply is right there in the conversation: he only pops up to say it's done, or to read it aloud
    if matches!(report.outcome, Outcome::Done { .. }) {
        let speech = speak(st, &reply);
        buddy_says(st, "Done — its checks pass.", speech);
    } else if matches!(report.outcome, Outcome::Answered { .. }) {
        if let Some(speech) = speak(st, &reply) {
            if Settings::load().map.get("buddy").and_then(|v| v.as_bool()).unwrap_or(true) {
                st.ui.buddy.talk(speech);
            }
        }
    } else if matches!(report.outcome, Outcome::Paused { .. }) {
        buddy_says(st, &format!("I had to stop. {}", reply.lines().next().unwrap_or("")), None);
    }
    if report.checkpoint.is_some() {
        st.saved.progress.checkpoint = report.checkpoint.clone();
    }
    let done = matches!(report.outcome, Outcome::Done { .. });
    st.panel.job_finished();
    refresh_daemons(st, shared);
    st.saved.progress.finished = done;
    st.saved.history.push(("lead".into(), reply));
    st.lane = None;
    st.running = None;
    st.set_running(false);
    st.save();
    refresh_checkpoints(st, shared);
    refresh_chats(st, shared);
}

/// Why a job paused, first and plainly, then what was still open.
fn paused_text(gaps: &[String]) -> String {
    let why = gaps.first().map(String::as_str).unwrap_or("it stopped");
    let why = why.trim_start_matches("stopped by the kernel: ");
    let cause = if why.contains("spending limit") {
        format!("**Paused: {why}.** The work itself wasn't finished — your limit stopped it. Raise the limit in \
            Settings › Spending limits (or set 0 for none), then press **Keep going**: it carries on exactly where it \
            stopped.")
    } else if why.contains("budget") {
        format!("**Paused: {why}.** This job's own budget was used up. **Keep going** gives it another one and carries on \
            where it stopped (Settings › Jobs can do that on its own).")
    } else if why.contains("stuck") {
        format!("**Paused: {why}.** Chuck kept repeating itself after being told to replan. Say what to try differently \
            and press **Keep going**, or type `/continue <your hint>`.")
    } else if why.contains("stopped by you") || why == "stopped" {
        "**Stopped by you.** **Keep going** carries on where it stopped.".to_string()
    } else {
        format!("**Paused: {why}.** **Keep going** carries on where it stopped.")
    };
    let rest: Vec<&String> = gaps.iter().skip(1).collect();
    if rest.is_empty() {
        cause
    } else {
        format!("{cause}\n\nStill open when it paused:\n{}", rest.iter().map(|g| format!("- {g}")).collect::<Vec<_>>().join("\n"))
    }
}

fn start(shared: &Shared, text: String) {
    let mut st = shared.borrow_mut();
    let images = std::mem::take(&mut st.pasted);
    clear_attach(&st);
    let mut text = text.trim().to_string();
    if text.is_empty() && !images.is_empty() {
        text = "Look at this screenshot.".into();
    }
    if text.is_empty() {
        return;
    }
    let shown: Vec<String> = images.iter().map(|p| p.display().to_string()).collect();
    if let Some(r) = &st.running {
        // mid-job: it steers the Lead (a screenshot as its file, which it can open with browse)
        let said = if shown.is_empty() { text.clone() } else {
            format!("{text}\n(screenshot{} pasted: {} — ```browse <that file>``` shows it to you)",
                    if shown.len() == 1 { "" } else { "s" }, shown.join(", "))
        };
        let _ = r.steer.send(said);
        st.add(Entry::Steer { text });
        if !shown.is_empty() {
            st.add(Entry::Pasted { paths: shown });
        }
        return;
    }
    st.add(Entry::User { text: text.clone() });
    if !shown.is_empty() {
        st.add(Entry::Pasted { paths: shown });
    }
    st.voice.stop();
    // carry on the paused job in this folder
    let resume = match text.strip_prefix("/continue").filter(|r| r.is_empty() || r.starts_with(char::is_whitespace)) {
        Some(rest) => match st.folder.as_ref().and_then(|f| chuck_runtime::resume::load(f)) {
            Some(r) => Some((r, rest.trim().to_string())),
            None => {
                st.add(Entry::Card { kind: "NOTHING TO CONTINUE".into(), class: "gap-card".into(), text: "No paused \
                    job in this folder: a job that finished, or one never started here, has nothing to carry on.".into() });
                st.save();
                return;
            }
        },
        None => None,
    };
    if resume.is_none() && text.starts_with('/') && slash(&mut st, shared, &text) {
        st.save();
        return;
    }
    if let Some((cmd, arg)) = ["/remind", "/daemon"].iter().find_map(|c| {
        text.strip_prefix(c).filter(|r| r.is_empty() || r.starts_with(char::is_whitespace)).map(|r| (*c, r.trim()))
    }) {
        let kind = if cmd == "/remind" { "remind" } else { "report" };
        let card = match chuck_daemon::split_spec(arg, chuck_daemon::now()) {
            Ok((when, what)) => {
                let folder = if kind == "remind" { String::new() } else {
                    st.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_default()
                };
                let d = st.daemons.add("", when, &what, &folder, kind, 150_000);
                let say = if kind == "remind" {
                    format!("I'll remind you {}: {what}", chuck_daemon::describe(d.when()))
                } else {
                    format!("A daemon will look {}: {what}\n\nIt costs nothing while it waits; when it wakes it only \
                        looks, and files a report in the Inbox (right panel). It runs while Chuck is open.",
                            chuck_daemon::describe(d.when()))
                };
                Entry::Card { kind: if kind == "remind" { "REMINDER SET".into() } else { "DAEMON SET".into() },
                              class: "ask-card".into(), text: say }
            }
            Err(e) => Entry::Card { kind: "NOT SET".into(), class: "gap-card".into(), text: format!("{e}\n\nFor example: \
                `{cmd} in 20m: check the oven`, `{cmd} every day at 9: check the disk`, \
                `{cmd} when a service fails: find out why`.") },
        };
        st.add(card);
        st.save();
        refresh_daemons(&st, shared);
        return;
    }
    // a paused job is Build's, whatever mode the window is in
    let chat_mode = st.chat && resume.is_none();
    let folder = match st.folder.clone() {
        Some(f) => f,
        // Chat mode works on no folder; nothing it does touches one
        None if chat_mode => std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(std::env::temp_dir),
        None => {
            st.add(Entry::Lead { text: "Pick a project folder first (the button at the top): Build works on a \
                folder. Or switch to **Chat** to just ask.".into(), model: String::new(), chat: false });
            return;
        }
    };
    let settings = Settings::load();
    let (model, cfg) = match (&st.opts.model, setup::from_settings(&settings, &folder, None)) {
        (Some(m), _) => (m.clone(), chuck_runtime::RuntimeConfig { access: setup::access_of(&settings, &folder),
                                                                  models: setup::models(&settings),
                                                                  web: st.opts.web.clone(), mcp: Some(st.mcp.clone()),
                                                                  lsp: Some(st.lsp.clone()), memory: Some(st.memory.clone()),
                                                                  browser: Some(setup::browser_pref(&settings)),
                                                                  resume_dir: Some(chuck_runtime::resume::default_dir()),
                                                                  ..Default::default() }),
        (None, Ok(s)) => {
            for n in &s.notes {
                st.act(format!("<span foreground=\"#F5B454\">! {}</span>", md::escape(n)));
            }
            (s.model, chuck_runtime::RuntimeConfig { mcp: Some(st.mcp.clone()), lsp: Some(st.lsp.clone()),
                                                     memory: Some(st.memory.clone()), ..s.cfg })
        }
        (None, Err(e)) => {
            st.add(Entry::Card { kind: "CAN'T START".into(), class: "gap-card".into(), text: e });
            return;
        }
    };
    st.panel.reset();
    st.saved.progress = chat::Progress::default();
    st.names.clear();
    st.models.clear();
    let (tx, rx) = channel();
    let rt = Runtime::new(model, cfg, Events(Some(tx)));
    let kernel = rt.kernel.clone();
    let mut job = Job::new(text.clone(), &folder);
    job.history = st.saved.history.clone();
    job.images = images;
    if let Some((r, extra)) = resume {
        job.ask = extra;
        job.resume = Some(r);
    }
    let (atx, arx) = channel();
    job.answers = Some(arx);
    let (stx, srx) = channel();
    job.steer = Some(srx);
    let stop = job.stop.clone();
    let (dtx, drx) = channel();
    std::thread::Builder::new().name("chuck-job".into()).spawn(move || {
        let _ = dtx.send(if chat_mode { rt.chat(job) } else { rt.run(job) });
    }).expect("start the job thread");
    st.saved.history.push(("user".into(), text));
    st.running = Some(Running { stop, answers: atx, steer: stx, events: rx, kernel, done: drx, started: Instant::now() });
    st.set_running(true);
    st.save();
    refresh_checkpoints(&st, shared);
    refresh_chats(&st, shared);
}

/// Drain the job's events into the window (every 60 ms).
fn pump(shared: &Shared) {
    let mut st = shared.borrow_mut();
    let Some(run) = &st.running else { return };
    let events: Vec<Event> = run.events.try_iter().take(500).collect();
    let report = run.done.try_recv().ok();
    for e in events {
        on_event(&mut st, e, shared);
    }
    if let Some(r) = report {
        let rest: Vec<Event> = st.running.as_ref().map(|r| r.events.try_iter().collect()).unwrap_or_default();
        for e in rest {
            on_event(&mut st, e, shared);
        }
        finished(&mut st, r, shared);
    }
}

/// Twice a second: the job as the kernel sees it, spend, the status line.
fn tick(shared: &Shared) {
    let mut st = shared.borrow_mut();
    daemon_poll(&mut st, shared);
    let mut arrived = Vec::new();
    st.later.retain(|rx| match rx.try_recv() {
        Ok(e) => {
            arrived.push(e);
            false
        }
        Err(std::sync::mpsc::TryRecvError::Empty) => true,
        Err(_) => false,
    });
    for e in arrived {
        st.add(e);
        st.save();
    }
    if let Some(line) = st.notice.as_ref().and_then(|rx| rx.try_recv().ok()) {
        st.notice = None;
        if st.running.is_none() {
            st.ui.status.set_label(&line);
        }
    }
    if let Some(run) = &st.running {
        let ps = run.kernel.ps(true);
        let elapsed = run.started.elapsed().as_secs();
        let lead = ps.iter().find(|p| p.ppid.is_none());
        let doing = lead.map(|p| match p.state {
            chuck_core::ProcState::Running => "thinking".to_string(),
            _ if !p.activity.is_empty() => p.activity.clone(),
            _ => "working".to_string(),
        }).unwrap_or_default();
        let cmds = chuck_tools::running();
        let mut s = format!("{}:{:02}  ·  {doing}", elapsed / 60, elapsed % 60);
        if cmds > 0 {
            s.push_str(&format!("  ·  {cmds} command{} running", if cmds == 1 { "" } else { "s" }));
        }
        st.ui.status.set_label(&s);
        st.last_ps = ps;
    }
    let spend = chuck_store::Spend::at(&chuck_store::Spend::default_path());
    let s = Settings::load();
    let now = chuck_sandbox::now_secs();
    let ps = st.last_ps.clone();
    let cache = (st.prompt > 0).then(|| st.cached as f64 / st.prompt as f64);
    st.panel.set_spend(&ps, spend.today(now), spend.month(now), s.num("budget_day_usd", 0.0, 0.0, 1e5),
                       s.num("budget_month_usd", 0.0, 0.0, 1e6), cache);
    st.panel.tick();
}

/// Stop the job and everything it started, and wait until it has stopped.
fn shut_down(shared: &Shared) {
    let mut st = shared.borrow_mut();
    if let Some(run) = st.running.take() {
        run.stop.store(true, Ordering::SeqCst);
        chuck_tools::kill_all();
        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(6) {
            if let Ok(r) = run.done.recv_timeout(Duration::from_millis(100)) {
                st.running = Some(run);
                finished(&mut st, r, shared);
                break;
            }
            chuck_tools::kill_all();
        }
    }
    st.save();
    for (stop, _, _) in st.daemon_live.values() {
        stop.store(true, Ordering::SeqCst);
    }
    st.mcp.stop_all();
    st.lsp.stop_all();
    st.voice.shutdown();
    st.ears.shutdown();
    st.memory.flush();
}

fn speaker_look(b: &gtk::ToggleButton, on: bool) {
    b.set_icon_name(if on { "chuck-speaker-symbolic" } else { "chuck-speaker-off-symbolic" });
    b.set_tooltip_text(Some(if on { "Replies are read aloud — click to stop (Esc silences one)" } else {
        "Read replies aloud" }));
}

/// Read a reply aloud, if that's on: the speech, for whoever moves with it.
fn speak(st: &State, text: &str) -> Option<(chuck_voice::Voice, u64)> {
    let s = Settings::load();
    if st.ui.speaker.is_active() && s.map.get("tts").and_then(|v| v.as_bool()).unwrap_or(true) {
        return Some((st.voice.clone(), st.voice.speak(text, &chuck_voice::Prefs::from(&s))));
    }
    None
}

/// Chuck pops up with it (if he's on, in Settings › Voice).
fn buddy_says(st: &State, text: &str, speech: Option<(chuck_voice::Voice, u64)>) {
    if Settings::load().map.get("buddy").and_then(|v| v.as_bool()).unwrap_or(true) {
        st.ui.buddy.say(text, speech);
    }
}

/// The microphone: start listening, or stop.
fn mic_clicked(shared: &Shared) {
    let mut st = shared.borrow_mut();
    if st.heard.is_some() {
        st.ears.cancel();
        return;
    }
    let s = Settings::load();
    if !chuck_voice::listen::ready() {
        st.ui.status.set_label(&format!("Voice input: {}", chuck_voice::listen::status(s.str("mic_command").unwrap_or(""))));
        return;
    }
    st.voice.stop(); // don't transcribe Chuck's own voice
    let (tx, rx) = channel();
    st.ears.listen(tx, s.str("mic_command").unwrap_or(""), s.num("mic_wait", 8.0, 2.0, 60.0), s.num("mic_max", 30.0, 5.0, 120.0));
    st.heard = Some(rx);
    mic_state(&st, "loading", 0.0);
}

fn mic_state(st: &State, state: &str, level: f32) {
    let b = &st.ui.mic;
    for c in ["mic-on", "mic-speech"] {
        b.remove_css_class(c);
    }
    let tip = match state {
        "loading" => "Getting ready to listen… (click to cancel)",
        "listening" => "Listening — say it, then pause (click to cancel)",
        "speech" => "Hearing you…",
        _ => "Talk to Chuck (Ctrl+M) — heard on this machine, never uploaded",
    };
    if state != "idle" {
        b.add_css_class("mic-on");
    }
    if state == "speech" {
        b.add_css_class("mic-speech");
    }
    b.set_opacity(if state == "speech" { 0.75 + (level * 4.0).min(0.25) as f64 } else { 1.0 });
    b.set_tooltip_text(Some(tip));
}

/// What the microphone heard: into the composer (and sent, if you said so).
fn hear(shared: &Shared) {
    let mut st = shared.borrow_mut();
    let Some(rx) = &st.heard else { return };
    let events: Vec<chuck_voice::Heard> = rx.try_iter().collect();
    let mut send = None;
    for ev in events {
        use chuck_voice::Heard;
        match ev {
            Heard::Listening => mic_state(&st, "listening", 0.0),
            Heard::Speech => mic_state(&st, "speech", 0.0),
            Heard::Level(l) => {
                if st.ui.mic.has_css_class("mic-speech") {
                    mic_state(&st, "speech", l);
                }
            }
            Heard::Final(text) => {
                st.heard = None;
                mic_state(&st, "idle", 0.0);
                if text.is_empty() {
                    st.ui.status.set_label("Didn't catch that — try again (Ctrl+M)");
                    break;
                }
                let buf = st.ui.input.buffer();
                let cur = buf.text(&buf.start_iter(), &buf.end_iter(), false).trim_end().to_string();
                let full = if cur.is_empty() { text } else { format!("{cur} {text}") };
                buf.set_text(&full);
                st.ui.input.grab_focus();
                if Settings::load().map.get("voice_autosend").and_then(|v| v.as_bool()) == Some(true) {
                    buf.set_text("");
                    send = Some(full);
                }
                break;
            }
            Heard::Nothing => {
                st.heard = None;
                mic_state(&st, "idle", 0.0);
                st.ui.status.set_label("Didn't hear anything — is the right microphone selected?");
                break;
            }
            Heard::Cancelled => {
                st.heard = None;
                mic_state(&st, "idle", 0.0);
                break;
            }
            Heard::Error(e) => {
                st.heard = None;
                mic_state(&st, "idle", 0.0);
                st.ui.status.set_label(&format!("Voice input: {e}"));
                break;
            }
        }
    }
    drop(st);
    if let Some(text) = send {
        start(shared, text);
    }
}

const HELP: &str = "\
- `/remind <when>: <what>` — a reminder (in 20m, at 17:30, tomorrow at 9am, every day at 8:30)
- `/daemon <when>: <what>` — something to look at when it happens (every 2h, when a service fails, \
when disk / is above 90%, when ~/src changes…); it only looks and reports to the Inbox
- `/continue [what to add]` — carry on a paused job exactly where it stopped
- `/undo` — put the folder back to before the last job · `/diff` — every change the last job made
- `/doctor` — check everything Chuck depends on · `/update` — install the newest Chuck · `/restart`
- `/export` — save this conversation as Markdown · `/cost` — what was spent · `/clear` — a new conversation
- `/snags` — what keeps getting in the agents' way (Settings › Jobs › Debug mode asks it to report its own)
- Ctrl+P finds any command · Ctrl+M talks · Esc stops (agents, commands and speech) · the right panel shows \
everything as it happens";

/// A command typed into the composer. True: it was one (handled here).
fn slash(st: &mut State, shared: &Shared, text: &str) -> bool {
    let (cmd, arg) = text.split_once(char::is_whitespace).map_or((text, ""), |(c, a)| (c, a.trim()));
    let card = |kind: &str, class: &str, text: String| Entry::Card { kind: kind.into(), class: class.into(), text };
    match cmd {
        "/help" | "/?" => st.add(card("COMMANDS", "ask-card", HELP.into())),
        "/clear" | "/new" => {
            st.save();
            if let Some(k) = st.key() {
                chat::archive(&k);
            }
            st.saved = Saved::default();
            st.redraw();
            st.panel.reset();
        }
        "/snags" => {
            // what keeps getting in the agents' way, across every job: to be fixed
            let p = chuck_store::snags::default_path();
            let all = chuck_store::snags::read(&p, 5000);
            let text = if all.is_empty() {
                "No snags recorded yet. They're written down as the agents work (Settings › Debug mode asks them to \
                 report their own too).".to_string()
            } else {
                let mut t = format!("{} snags recorded — most often first:\n\n", all.len());
                for (kind, n, last) in chuck_store::snags::summary(&all).into_iter().take(15) {
                    t.push_str(&format!("- **{kind}** ×{n} — latest: {}\n", md::escape(&one_line(&last, 220))));
                }
                t.push_str(&format!("\nThe whole log: `{}` — send it in to have these fixed.", p.display()));
                t
            };
            st.add(card("SNAGS", "ask-card", text));
        }
        "/cost" => {
            let spend = chuck_store::Spend::at(&chuck_store::Spend::default_path());
            let now = chuck_sandbox::now_secs();
            let s = Settings::load();
            let lim = |v: f64| if v > 0.0 { format!(" of {}", fmt_usd(v)) } else { String::new() };
            st.add(card("SPEND", "ask-card", format!("This window: {} tokens, {}\n\nToday: {}{}\nThis month: {}{}",
                fmt_tokens(st.tokens), fmt_usd(st.spent), fmt_usd(spend.today(now)), lim(s.num("budget_day_usd", 0.0, 0.0, 1e5)),
                fmt_usd(spend.month(now)), lim(s.num("budget_month_usd", 0.0, 0.0, 1e6)))));
        }
        "/export" => {
            let dir = if arg.is_empty() { st.folder.clone().unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default())) }
                else { PathBuf::from(arg) };
            let path = dir.join(format!("chuck-chat-{}.md", chuck_store::ledger::local_stamp("%Y%m%d-%H%M")));
            let out = match std::fs::write(&path, chat::markdown(&st.saved)) {
                Ok(()) => card("EXPORTED", "ask-card", format!("The conversation is in `{}`.", path.display())),
                Err(e) => card("NOT EXPORTED", "gap-card", format!("Couldn't write `{}`: {e}", path.display())),
            };
            st.add(out);
        }
        "/undo" | "/diff" => {
            let (Some(folder), Some(cp)) = (st.folder.clone(), st.saved.progress.checkpoint.clone()) else {
                st.add(card("NOTHING TO DO", "gap-card", "No job has changed this folder yet.".into()));
                return true;
            };
            if cmd == "/undo" {
                if st.running.is_some() {
                    st.add(card("NOT NOW", "gap-card", "A job is running: stop it first (Esc).".into()));
                    return true;
                }
                let sh = shared.clone();
                glib::idle_add_local_once(move || rewind(&sh, &cp));
            } else {
                let diff = chuck_store::Shadow::new(&folder, &chuck_store::checkpoint::default_store())
                    .map(|s| s.diff_since(&cp, 60_000)).unwrap_or_else(|e| format!("(couldn't read the checkpoint: {e})"));
                st.add(card("CHANGES SINCE THE LAST JOB STARTED", "ask-card", if diff.trim().is_empty() {
                    "Nothing has changed.".into() } else { format!("```diff\n{}\n```", diff.trim_end()) }));
            }
        }
        "/doctor" => {
            st.add(card("CHECKING", "ask-card", "Checking everything Chuck depends on…".into()));
            let (tx, rx) = channel();
            let (folder, mcp) = (st.folder.clone(), st.mcp.clone());
            std::thread::Builder::new().name("doctor".into()).spawn(move || {
                let checks = crate::doctor::run(folder.as_deref(), Some(&mcp));
                let bad = checks.iter().filter(|c| c.state != crate::doctor::State::Ok).count();
                let _ = tx.send(Entry::Card { kind: if bad == 0 { "ALL GOOD".into() } else { format!("{bad} TO LOOK AT") },
                                              class: if bad == 0 { "done-card".into() } else { "gap-card".into() },
                                              text: crate::doctor::report(&checks) });
            }).ok();
            st.later.push(rx);
        }
        "/update" => {
            if st.running.is_some() {
                st.add(card("NOT NOW", "gap-card", "A job is running: update when it's done.".into()));
                return true;
            }
            st.add(card("UPDATING", "ask-card", "Looking for a newer Chuck…".into()));
            let (tx, rx) = channel();
            let (ntx, nrx) = channel();
            std::thread::Builder::new().name("update".into()).spawn(move || {
                let done = |kind: &str, class: &str, text: String| Entry::Card { kind: kind.into(), class: class.into(), text };
                let e = match crate::update::check() {
                    Err(e) => done("UPDATE", "gap-card", format!("Couldn't ask GitHub: {e}")),
                    Ok(crate::update::Found::UpToDate(_)) => done("UP TO DATE", "done-card", "This is the newest Chuck.".into()),
                    Ok(_) => match crate::update::run(&mut |l| { let _ = ntx.send(l.chars().take(140).collect()); }) {
                        Ok(()) => done("UPDATED", "done-card", "The newest Chuck is installed. Type `/restart` to open it \
                            (your conversation is saved).".into()),
                        Err(e) => done("UPDATE FAILED", "gap-card", format!("{e}\n\nNothing changed: this Chuck still works. \
                            The full installer is: `curl -fsSL https://raw.githubusercontent.com/{}/{}/install.sh | bash`",
                            crate::update::REPO, crate::update::BRANCH)),
                    },
                };
                let _ = tx.send(e);
            }).ok();
            st.later.push(rx);
            st.notice = Some(nrx);
        }
        "/restart" => {
            if st.running.is_some() {
                st.add(card("NOT NOW", "gap-card", "A job is running: stop it first (Esc).".into()));
                return true;
            }
            let exe = std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/bin/chuck")).filter(|p| p.exists())
                .or_else(|| std::env::current_exe().ok());
            if let Some(exe) = exe {
                use std::os::unix::process::CommandExt;
                let mut c = std::process::Command::new(exe);
                // the new Chuck is not this one's: it must outlive the quit below
                c.env_remove(chuck_tools::SESSION_VAR);
                unsafe {
                    c.pre_exec(|| {
                        libc::setsid();
                        Ok(())
                    });
                }
                if c.spawn().is_ok() {
                    let w = st.ui.window.clone();
                    let sh = shared.clone();
                    glib::idle_add_local_once(move || quit(&w, &sh));
                }
            }
        }
        _ => return false,
    }
    true
}

/// The daemons and the inbox, in the panel.
fn refresh_daemons(st: &State, shared: &Shared) {
    let (w1, w2, w3) = (Rc::downgrade(shared), Rc::downgrade(shared), Rc::downgrade(shared));
    let on_toggle: Rc<dyn Fn(i64)> = Rc::new(move |id| {
        let Some(sh) = w1.upgrade() else { return };
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || {
            let mut st = sh2.borrow_mut();
            if let Some(d) = st.daemons.get_mut(id) {
                let on = !d.enabled();
                d.set("enabled", on.into());
                if on {
                    // resuming doesn't fire it at once for the time it was paused
                    d.set("last_run", chuck_daemon::now().into());
                }
            }
            st.daemons.save();
            refresh_daemons(&st, &sh2);
        });
    });
    let on_remove: Rc<dyn Fn(i64)> = Rc::new(move |id| {
        let Some(sh) = w2.upgrade() else { return };
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || {
            let mut st = sh2.borrow_mut();
            if let Some((stop, _, _)) = st.daemon_live.get(&id) {
                stop.store(true, Ordering::SeqCst);
            }
            st.daemons.remove(id);
            refresh_daemons(&st, &sh2);
        });
    });
    let on_read: Rc<dyn Fn()> = Rc::new(move || {
        let Some(sh) = w3.upgrade() else { return };
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || {
            let mut st = sh2.borrow_mut();
            st.inbox.mark_read();
            refresh_daemons(&st, &sh2);
        });
    });
    let w4 = Rc::downgrade(shared);
    let on_forget: Rc<dyn Fn(serde_json::Value)> = Rc::new(move |id| {
        let Some(sh) = w4.upgrade() else { return };
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || {
            let st = sh2.borrow();
            st.memory.forget_id(&id);
            refresh_daemons(&st, &sh2);
        });
    });
    let folder = st.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_default();
    st.panel.set_memory(&st.memory.all(), &folder, on_forget);
    let live: Vec<i64> = st.daemon_live.keys().copied().collect();
    st.panel.set_daemons(&st.daemons.items, &live, on_toggle, on_remove);
    st.panel.set_inbox(&st.inbox.items, on_read);
}

fn notify(st: &State, id: &str, title: &str, body: &str) {
    let Some(app) = st.ui.window.application() else { return };
    let n = gtk::gio::Notification::new(title);
    n.set_body(Some(body));
    app.send_notification(Some(id), &n);
}

/// Every 30 s: is any daemon due? Checked off the main thread by plain code
/// (the clock, a folder, the disk, a command); nothing is spent unless one fires.
fn daemon_tick(shared: &Shared) {
    let mut st = shared.borrow_mut();
    if st.daemon_check.is_some() || Settings::load().map.get("daemons_paused").and_then(|v| v.as_bool()) == Some(true) {
        return;
    }
    let s = Settings::load();
    let spend = chuck_store::Spend::at(&chuck_store::Spend::default_path());
    // over a spending limit: only reminders (they cost nothing)
    let broke = spend.over(s.num("budget_day_usd", 0.0, 0.0, 1e5), s.num("budget_month_usd", 0.0, 0.0, 1e6)).is_some();
    let specs: Vec<(i64, serde_json::Value, serde_json::Value, f64)> = st.daemons.items.iter()
        .filter(|d| d.enabled() && !st.daemon_live.contains_key(&d.id()) && (!broke || d.kind() == "remind"))
        .map(|d| (d.id(), d.when().clone(), d.state().clone(), d.last_run())).collect();
    if specs.is_empty() {
        return;
    }
    let (tx, rx) = channel();
    st.daemon_check = Some(rx);
    std::thread::Builder::new().name("daemon-check".into()).spawn(move || {
        let now = chuck_daemon::now();
        let probe = chuck_daemon::probe::SystemProbe;
        let out = specs.into_iter().map(|(id, when, state, last)| {
            let (due, why, st) = chuck_daemon::check(&when, &state, last, now, &probe);
            (id, due, why, st)
        }).collect();
        let _ = tx.send(out);
    }).ok();
}

/// Twice a second: trigger results in, reports in.
fn daemon_poll(st: &mut State, shared: &Shared) {
    let mut changed = false;
    if let Some(results) = st.daemon_check.as_ref().and_then(|rx| rx.try_recv().ok()) {
        st.daemon_check = None;
        let now = chuck_daemon::now();
        let mut due = Vec::new();
        for (id, fire, why, state) in results {
            let Some(d) = st.daemons.get_mut(id) else { continue };
            if !d.enabled() {
                continue;
            }
            if d.state() != &state {
                d.set("state", state);
                changed = true;
            }
            if fire {
                due.push((id, why));
            }
        }
        for (id, why) in due {
            wake(st, id, &why, now);
            changed = true;
        }
        if changed {
            st.daemons.save();
        }
    }
    let arrived: Vec<(i64, chuck_runtime::daemon::DaemonReport)> = st.daemon_live.iter()
        .filter_map(|(id, (_, rx, _))| rx.try_recv().ok().map(|r| (*id, r))).collect();
    let done: Vec<i64> = arrived.into_iter()
        .map(|(id, r)| {
            let why = st.daemon_live.get(&id).map(|x| x.2.clone()).unwrap_or_default();
            let name = st.daemons.get(id).map(|d| d.name().to_string()).unwrap_or_else(|| format!("daemon {id}"));
            let mut extra = serde_json::Map::new();
            extra.insert("daemon_id".into(), id.into());
            extra.insert("status".into(), r.status.into());
            extra.insert("tokens".into(), r.tokens.into());
            extra.insert("usd".into(), r.usd.into());
            let quiet = r.status == 0 && chuck_daemon::quiet(&r.report);
            st.inbox.add(&name, &why, &r.report, extra);
            if quiet {
                // nothing wrong: filed, not announced
                if let Some(last) = st.inbox.items.last_mut() {
                    last.insert("unread".into(), false.into());
                }
                st.inbox.save();
            } else {
                let head = r.report.trim().lines().next().unwrap_or("(no report)").chars().take(160).collect::<String>();
                notify(st, &format!("chuck-daemon-{id}"), &format!("Chuck daemon: {name}"), &head);
            }
            id
        }).collect();
    for id in &done {
        st.daemon_live.remove(id);
    }
    if changed || !done.is_empty() {
        refresh_daemons(st, shared);
    }
}

/// A daemon fired: a reminder tells you; a daemon wakes an agent that looks
/// and reports (as its own kernel process, with its own quota).
fn wake(st: &mut State, id: i64, why: &str, now: f64) {
    let Some(d) = st.daemons.get_mut(id) else { return };
    d.set("last_run", now.into());
    let runs = d.runs() + 1;
    d.set("runs", runs.into());
    let d = d.clone();
    if d.kind() == "remind" {
        let text = d.brief().trim().to_string();
        let mut extra = serde_json::Map::new();
        extra.insert("daemon_id".into(), id.into());
        extra.insert("status".into(), 0.into());
        st.inbox.add("reminder", &chuck_daemon::describe(d.when()), &text, extra);
        st.add(Entry::Card { kind: "⏰ REMINDER".into(), class: "ask-card".into(), text: text.clone() });
        speak(st, &format!("Reminder: {text}"));
        st.save();
        notify(st, &format!("chuck-remind-{id}"), "Reminder", &text);
        if d.when().get("at").is_some() {
            st.daemons.remove(id); // a one-time reminder is done
        }
        return;
    }
    let folder = PathBuf::from(d.folder());
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"));
    let root = if folder.is_dir() { folder } else { home };
    let settings = Settings::load();
    let made = match &st.opts.model {
        Some(m) => Ok((m.clone(), chuck_runtime::RuntimeConfig { web: st.opts.web.clone(), ..Default::default() })),
        None => setup::from_settings(&settings, &root, None).map(|s| (s.model, s.cfg)),
    };
    let (model, cfg) = match made {
        Ok(x) => x,
        Err(e) => {
            let mut extra = serde_json::Map::new();
            extra.insert("daemon_id".into(), id.into());
            extra.insert("status".into(), 1.into());
            st.inbox.add(d.name(), why, &format!("(couldn't start: {e})"), extra);
            return;
        }
    };
    let stop = Arc::new(AtomicBool::new(false));
    let (tx, rx) = channel();
    let run = chuck_runtime::daemon::DaemonRun {
        name: d.name().to_string(), brief: d.brief().to_string(), why: why.to_string(),
        detail: chuck_daemon::detail(d.when(), d.state()), folder: root, tokens: d.quota(),
        minutes: settings.num("daemon_minutes", 15.0, 1.0, 240.0) as u64,
        steps: settings.num("daemon_steps", 20.0, 3.0, 100.0) as usize, stop: stop.clone(),
    };
    std::thread::Builder::new().name(format!("daemon-{id}")).spawn(move || {
        let rt = Runtime::new(model, cfg, Events::none());
        let _ = tx.send(rt.daemon(run));
    }).ok();
    st.daemon_live.insert(id, (stop, rx, why.to_string()));
}

fn settings_dialog(shared: &Shared) {
    let s = Settings::load();
    let page = adw::PreferencesPage::new();
    let api = adw::PreferencesGroup::builder().title("SiliconFlow")
        .description("Chuck thinks on SiliconFlow, with your key. `chuck` in a terminal uses it too.").build();
    let key = adw::PasswordEntryRow::builder().title("API key").build();
    key.set_text(s.api_key().unwrap_or(""));
    api.add(&key);
    page.add(&api);

    let jobs = adw::PreferencesGroup::builder().title("Jobs")
        .description("One model (DeepSeek-V4.1-Flash) does the whole job; GLM-5.3-Flash looks at screenshots.").build();
    let cap = adw::SpinRow::with_range(0.05, 100.0, 0.05);
    cap.set_title("Spending cap per job ($)");
    cap.set_digits(2);
    cap.set_value(s.num("fleet_job_usd", 2.0, 0.01, 1000.0));
    let cmd_min = adw::SpinRow::with_range(1.0, 1440.0, 5.0);
    cmd_min.set_title("Longest one command may run (minutes)");
    cmd_min.set_subtitle("A build, a test suite or an emulator boot that takes longer is stopped and reported");
    cmd_min.set_value((s.num("command_timeout", 1800.0, 30.0, 86_400.0) / 60.0).round());
    let auto = adw::SwitchRow::builder().title("Keep going when a job's budget runs out")
        .subtitle("Carries on with another job budget, as often as needed, until it's done — never past the daily \
            limit, which this needs set (Spending limits below)")
        .active(s.map.get("fleet_auto_continue").and_then(|v| v.as_bool()) == Some(true)).build();
    let debug_row = adw::SwitchRow::builder().title("Debug mode")
        .subtitle("Chuck notes whatever slows it down, limits it or goes wrong (/snags) — for it to be fixed")
        .active(s.map.get("debug").and_then(|v| v.as_bool()) == Some(true)).build();
    jobs.add(&cap);
    jobs.add(&cmd_min);
    jobs.add(&auto);
    jobs.add(&debug_row);
    page.add(&jobs);

    let reason = adw::PreferencesGroup::builder().title("Reasoning").build();
    let levels = [("Off — answer straight away", 0.0), ("Light (1k tokens)", 1024.0), ("Quick (2k)", 2048.0), ("Normal (4k)", 4096.0),
                  ("Deep (8k)", 8192.0), ("Deeper (16k)", 16384.0), ("Max (32k)", 32768.0)];
    let names: Vec<&str> = levels.iter().map(|(n, _)| *n).collect();
    let rz = adw::ComboRow::builder().title("Reasoning before it builds")
        .subtitle("It thinks on its first turn and after something fails; the rest of the job goes without").model(&gtk::StringList::new(&names)).build();
    let cur = s.num("reasoning_budget", 2048.0, 0.0, 32768.0);
    rz.set_selected(levels.iter().position(|(_, v)| *v >= cur).unwrap_or(2) as u32);
    let think = adw::SpinRow::with_range(0.0, 1800.0, 10.0);
    think.set_title("Max thinking time (s)");
    think.set_value(s.num("max_think_seconds", 90.0, 0.0, 1800.0));
    let stall = adw::SpinRow::with_range(20.0, 600.0, 10.0);
    stall.set_title("Give up on a silent connection after (s)");
    stall.set_value(s.num("stall_seconds", 90.0, 20.0, 600.0));
    reason.add(&rz);
    reason.add(&think);
    reason.add(&stall);
    page.add(&reason);

    let budget = adw::PreferencesGroup::builder().title("Spending limits")
        .description("Across every chat, agent and job. 0 means no limit. Only you can raise them.").build();
    let day = adw::SpinRow::with_range(0.0, 10_000.0, 0.5);
    day.set_title("Daily limit ($)");
    day.set_digits(2);
    day.set_value(s.num("budget_day_usd", 0.0, 0.0, 100_000.0));
    let month = adw::SpinRow::with_range(0.0, 100_000.0, 5.0);
    month.set_title("Monthly limit ($)");
    month.set_digits(2);
    month.set_value(s.num("budget_month_usd", 0.0, 0.0, 1_000_000.0));
    budget.add(&day);
    budget.add(&month);
    page.add(&budget);

    let folder = shared.borrow().folder.clone();
    let access_group = adw::PreferencesGroup::builder().title("This folder's access")
        .description(folder.as_ref().map(|f| f.display().to_string()).unwrap_or_else(|| "pick a folder first".into()))
        .build();
    let modes = [("guarded", "This folder freely — asks before anything outside it"),
                 ("full", "Full access — commands run as you, anywhere"), ("sandbox-net", "Sandboxed — only this folder is writable"),
                 ("sandbox", "Sandboxed, offline — no network"), ("readonly", "Read-only — nothing is changed")];
    let mnames: Vec<&str> = modes.iter().map(|(_, n)| *n).collect();
    let acc = adw::ComboRow::builder().title("Access").model(&gtk::StringList::new(&mnames)).build();
    let current = folder.as_ref().map(|f| setup::access_of(&s, f)).unwrap_or(chuck_sandbox::Access::Guarded);
    acc.set_selected(match current {
        chuck_sandbox::Access::Guarded => 0,
        chuck_sandbox::Access::Full => 1,
        chuck_sandbox::Access::SandboxNet => 2,
        chuck_sandbox::Access::Sandbox => 3,
        chuck_sandbox::Access::ReadOnly => 4,
    });
    acc.set_sensitive(folder.is_some());
    access_group.add(&acc);
    page.add(&access_group);

    let updates = adw::SwitchRow::builder().title("Check for a new version once a day")
        .subtitle(match crate::update::installed() {
            Some(h) => format!("This is {} from the {} branch ({}) · /update installs the newest", env!("CARGO_PKG_VERSION"),
                               crate::update::BRANCH, &h[..10.min(h.len())]),
            None => format!("This is {} built from source", env!("CARGO_PKG_VERSION")),
        })
        .active(s.map.get("check_updates").and_then(|v| v.as_bool()) != Some(false)).build();
    let about = adw::PreferencesGroup::builder().title("Chuck").build();
    about.add(&updates);
    let research = adw::PreferencesGroup::builder().title("Research")
        .description("How agents search the web and read pages.").build();
    let searx = adw::EntryRow::builder().title("Your own SearXNG (asked first; optional)")
        .text(s.str("searx_url").unwrap_or("")).build();
    let proxy = adw::EntryRow::builder().title("Proxy for web reads (optional, e.g. socks5://127.0.0.1:9050)")
        .text(s.str("proxy").unwrap_or("")).build();
    let sources = adw::SpinRow::with_range(1.0, 10.0, 1.0);
    sources.set_title("Results per search");
    sources.set_value(s.num("research_sources", 6.0, 1.0, 10.0));
    let browsers = [("auto", "Automatic (Camoufox reads, Chromium checks)"), ("camoufox", "Camoufox"), ("brave", "Brave"),
                    ("chromium", "Chromium"), ("chrome", "Chrome")];
    let pref = chuck_runtime::setup::browser_pref(&s);
    use chuck_web::browser::{pick, Purpose};
    let browser = adw::ComboRow::builder().title("Browser")
        .subtitle(md::escape(&match (pick(&pref, Purpose::Read), pick(&pref, Purpose::Check)) {
            (Ok(r), Ok(c)) if r == c => format!("{} — reads pages that need JavaScript or turn bots away, and checks \
                Chuck's pages", r.describe()),
            (Ok(r), Ok(c)) => format!("Reads blocked pages: {} · checks Chuck's pages: {}", r.describe(), c.describe()),
            (Err(e), _) | (_, Err(e)) => e,
        }))
        .model(&gtk::StringList::new(&browsers.iter().map(|b| b.1).collect::<Vec<_>>())).build();
    // a path typed into settings.json stays as it is unless a choice here is made
    let custom = !browsers.iter().any(|b| b.0 == pref);
    browser.set_selected(browsers.iter().position(|b| b.0 == pref).unwrap_or(0) as u32);
    let stealth = adw::SwitchRow::builder().title("Read blocked and script-only pages in the browser")
        .subtitle("When a plain read sees only a bot check or an empty page, the page is loaded in the browser \
            (public addresses only)")
        .active(s.map.get("stealth_fetch").and_then(|v| v.as_bool()) != Some(false)).build();
    research.add(&searx);
    research.add(&proxy);
    research.add(&sources);
    research.add(&browser);
    research.add(&stealth);
    page.add(&research);
    page.add(&about);

    let vp = chuck_voice::Prefs::from(&s);
    let voice = shared.borrow().voice.clone();
    let voice_group = adw::PreferencesGroup::builder().title("Voice")
        .description("Spoken and heard on this machine: nothing is uploaded.").build();
    let tts = adw::SwitchRow::builder().title("Read replies aloud")
        .active(s.map.get("tts").and_then(|v| v.as_bool()).unwrap_or(true)).build();
    let buddy_row = adw::SwitchRow::builder().title("Chuck pops up")
        .subtitle("With what Chuck has to say: its news, its questions, the finished job")
        .active(s.map.get("buddy").and_then(|v| v.as_bool()).unwrap_or(true)).build();
    let engines = [("auto", "Automatic (the most natural installed)"), ("kokoro", "Kokoro — natural"),
                   ("piper", "Piper"), ("espeak", "espeak-ng — robotic")];
    let engine = adw::ComboRow::builder().title("Engine")
        .model(&gtk::StringList::new(&engines.iter().map(|e| e.1).collect::<Vec<_>>())).build();
    engine.set_selected(engines.iter().position(|e| e.0 == vp.engine).unwrap_or(0) as u32);
    let voices = chuck_voice::speak::KOKORO_VOICES;
    let kvoice = adw::ComboRow::builder().title("Kokoro voice")
        .model(&gtk::StringList::new(&voices.iter().map(|v| v.1).collect::<Vec<_>>())).build();
    kvoice.set_selected(voices.iter().position(|v| v.0 == vp.kokoro_voice).unwrap_or(0) as u32);
    let speed = adw::SpinRow::with_range(0.5, 2.0, 0.05);
    speed.set_title("Speed");
    speed.set_digits(2);
    speed.set_value(vp.speed);
    let autosend = adw::SwitchRow::builder().title("Send what I say right away")
        .subtitle("Off: what you say lands in the box to check first")
        .active(s.map.get("voice_autosend").and_then(|v| v.as_bool()) == Some(true)).build();
    let vstatus = adw::ActionRow::builder().title("Speaking").subtitle(md::escape(&voice.status(&vp))).build();
    vstatus.set_subtitle_lines(3);
    let test = gtk::Button::with_label("Test");
    test.add_css_class("pill-sm");
    test.set_valign(gtk::Align::Center);
    {
        let (voice, engine, kvoice, speed) = (voice.clone(), engine.clone(), kvoice.clone(), speed.clone());
        test.connect_clicked(move |_| {
            let mut p = chuck_voice::Prefs::from(&Settings::load());
            p.engine = engines[engine.selected() as usize].0.to_string();
            p.kokoro_voice = voices[kvoice.selected() as usize].0.to_string();
            p.speed = speed.value();
            voice.speak("Chuck Norris approves this voice.", &p);
        });
    }
    vstatus.add_suffix(&test);
    let lstatus = adw::ActionRow::builder().title("Listening")
        .subtitle(md::escape(&chuck_voice::listen::status(s.str("mic_command").unwrap_or("")))).build();
    lstatus.set_subtitle_lines(2);
    // set up (or repair) the natural voice and voice input, here and now
    let setup = adw::ActionRow::builder().title("Natural voice and voice input")
        .subtitle("Kokoro and Moonshine, about 600 MB, all on this machine").build();
    setup.set_subtitle_lines(2);
    let get = gtk::Button::with_label(if chuck_voice::speak::kokoro_ready() && chuck_voice::listen::ready() { "Repair" }
                                      else { "Set up" });
    get.add_css_class("pill-sm");
    get.set_valign(gtk::Align::Center);
    {
        let (setup, vstatus, lstatus, voice) = (setup.clone(), vstatus.clone(), lstatus.clone(), voice.clone());
        get.connect_clicked(move |b| {
            b.set_sensitive(false);
            let (tx, rx) = std::sync::mpsc::channel::<Result<String, Vec<Result<String, String>>>>();
            std::thread::spawn(move || {
                let ptx = tx.clone();
                let said = chuck_voice::install::all(&|m| { let _ = ptx.send(Ok(m.chars().take(140).collect())); });
                let _ = tx.send(Err(said));
            });
            let (b, setup, vstatus, lstatus, voice) = (b.clone(), setup.clone(), vstatus.clone(), lstatus.clone(), voice.clone());
            glib::timeout_add_local(Duration::from_millis(200), move || {
                while let Ok(m) = rx.try_recv() {
                    match m {
                        Ok(line) => setup.set_subtitle(&md::escape(&line)),
                        Err(said) => {
                            let ok = said.iter().all(|r| r.is_ok());
                            setup.set_subtitle(&md::escape(&said.into_iter().map(|r| match r {
                                Ok(m) => format!("✓ {m}"),
                                Err(e) => format!("✗ {e}"),
                            }).collect::<Vec<_>>().join("\n")));
                            let s = Settings::load();
                            vstatus.set_subtitle(&md::escape(&voice.status(&chuck_voice::Prefs::from(&s))));
                            lstatus.set_subtitle(&md::escape(&chuck_voice::listen::status(s.str("mic_command").unwrap_or(""))));
                            b.set_label(if ok { "Repair" } else { "Try again" });
                            b.set_sensitive(true);
                            return glib::ControlFlow::Break;
                        }
                    }
                }
                glib::ControlFlow::Continue
            });
        });
    }
    setup.add_suffix(&get);
    for r in [tts.upcast_ref::<gtk::Widget>(), buddy_row.upcast_ref(), engine.upcast_ref(), kvoice.upcast_ref(), speed.upcast_ref(),
              vstatus.upcast_ref(), autosend.upcast_ref(), lstatus.upcast_ref(), setup.upcast_ref()] {
        voice_group.add(r);
    }
    page.add(&voice_group);

    let mcp = shared.borrow().mcp.clone();
    let mcp_group = adw::PreferencesGroup::builder().title("MCP servers")
        .description(format!("Tools from Model Context Protocol servers, set up in {} (the format Claude Desktop \
            uses). The Lead can use them; Chat mode only the ones that only read. A tool that may change things \
            asks you first unless its server has \"trust\": true.", chuck_mcp::config_path().display())).build();
    let status_box = gtk::ListBox::new();
    status_box.add_css_class("boxed-list");
    status_box.set_selection_mode(gtk::SelectionMode::None);
    let fill = {
        let (status_box, mcp) = (status_box.clone(), mcp.clone());
        move || {
            while let Some(c) = status_box.first_child() {
                status_box.remove(&c);
            }
            let st = mcp.status();
            if st.is_empty() {
                status_box.append(&adw::ActionRow::builder().title("No servers set up")
                    .subtitle("Add one to mcp.json, then Reload").build());
            }
            for x in st {
                let row = adw::ActionRow::builder().title(&x.name)
                    .subtitle(md::escape(&if x.detail.is_empty() { x.state.clone() } else { format!("{} — {}", x.state, x.detail) }))
                    .build();
                row.set_subtitle_lines(2);
                status_box.append(&row);
            }
        }
    };
    fill();
    let reload = gtk::Button::with_label("Reload");
    reload.add_css_class("pill-sm");
    let busy = shared.borrow().running.is_some();
    reload.set_sensitive(!busy);
    reload.set_tooltip_text(Some(if busy { "Not while a job runs" } else { "Restart every server and re-read mcp.json" }));
    {
        let (mcp, fill) = (mcp.clone(), fill.clone());
        reload.connect_clicked(move |b| {
            b.set_sensitive(false);
            let (tx, rx) = std::sync::mpsc::channel();
            let m = mcp.clone();
            std::thread::spawn(move || {
                m.start_all();
                let _ = tx.send(());
            });
            let (b, fill) = (b.clone(), fill.clone());
            glib::timeout_add_local(Duration::from_millis(200), move || {
                if rx.try_recv().is_ok() {
                    fill();
                    b.set_sensitive(true);
                    return glib::ControlFlow::Break;
                }
                glib::ControlFlow::Continue
            });
        });
    }
    let open = gtk::Button::with_label("Open mcp.json");
    open.add_css_class("pill-sm");
    open.connect_clicked(|_| {
        let p = chuck_mcp::config_path();
        if !p.exists() {
            if let Some(d) = p.parent() {
                let _ = std::fs::create_dir_all(d);
            }
            let _ = chuck_store::settings::write_private(&p, b"{\n  \"mcpServers\": {\n  }\n}\n");
        }
        let f = gtk::gio::File::for_path(&p);
        gtk::FileLauncher::new(Some(&f)).launch(None::<&gtk::Window>, gtk::gio::Cancellable::NONE, |_| {});
    });
    let buttons = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    buttons.set_margin_top(8);
    buttons.append(&reload);
    buttons.append(&open);
    let mcp_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    mcp_box.append(&status_box);
    mcp_box.append(&buttons);
    mcp_group.add(&mcp_box);
    page.add(&mcp_group);

    let dialog = adw::PreferencesDialog::new();
    dialog.set_title("Settings");
    dialog.add(&page);
    let sh = shared.clone();
    dialog.connect_closed(move |_| {
        let mut s = Settings::load();
        let k = key.text().trim().to_string();
        if !k.is_empty() {
            s.set("siliconflow_api_key", k.into());
        }
        s.set("fleet_job_usd", cap.value().into());
        s.set("fleet_auto_continue", auto.is_active().into());
        s.set("command_timeout", ((cmd_min.value() * 60.0) as i64).into());
        s.set("reasoning_budget", (levels[rz.selected() as usize].1 as i64).into());
        s.set("max_think_seconds", (think.value() as i64).into());
        s.set("stall_seconds", (stall.value() as i64).into());
        s.set("budget_day_usd", day.value().into());
        s.set("budget_month_usd", month.value().into());
        s.set("searx_url", searx.text().trim().into());
        s.set("proxy", proxy.text().trim().into());
        s.set("research_sources", (sources.value() as i64).into());
        if !custom || browser.selected() != 0 {
            s.set("browser", browsers[browser.selected() as usize].0.into());
        }
        s.set("stealth_fetch", stealth.is_active().into());
        s.set("tts", tts.is_active().into());
        s.set("debug", debug_row.is_active().into());
        s.set("buddy", buddy_row.is_active().into());
        s.set("check_updates", updates.is_active().into());
        s.set("voice_engine", engines[engine.selected() as usize].0.into());
        s.set("kokoro_voice", voices[kvoice.selected() as usize].0.into());
        s.set("voice_speed", speed.value().into());
        s.set("voice_autosend", autosend.is_active().into());
        if let Some(f) = &folder {
            let mode = modes[acc.selected() as usize].0;
            let mut map = s.map.get("folder_access").and_then(|v| v.as_object()).cloned().unwrap_or_default();
            if mode == "guarded" {
                // the default: nothing to keep
                map.remove(&f.display().to_string());
            } else if mode.starts_with("sandbox") && chuck_sandbox::bwrap().is_none() {
                // never claim a jail that isn't there
            } else {
                map.insert(f.display().to_string(), mode.into());
            }
            s.set("folder_access", serde_json::Value::Object(map));
        }
        let _ = s.save();
        let st = sh.borrow();
        st.refresh_header();
        st.ui.speaker.set_active(tts.is_active());
    });
    let win = shared.borrow().ui.window.clone();
    dialog.present(Some(&win));
}

const BUILD_LEDE: &str = "One fast model writes the code, runs it and checks it. It's done when its checks pass, not \
    when it says so.";
const CHAT_LEDE: &str = "Answers straight away, looks things up on the web and cites its sources. Anything to do on \
    your machine goes to Build, on your say-so.";

fn welcome(shared_slot: Rc<RefCell<Option<Shared>>>) -> (gtk::Box, gtk::Label, gtk::Label) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 14);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Center);
    let mark = label("C", &["mark", "mark-lg"]);
    mark.set_xalign(0.5);
    mark.set_halign(gtk::Align::Center);
    b.append(&mark);
    let hero = label("What should Chuck build?", &["hero"]);
    hero.set_xalign(0.5);
    b.append(&hero);
    let lede = label(BUILD_LEDE, &["lede"]);
    lede.set_xalign(0.5);
    lede.set_justify(gtk::Justification::Center);
    lede.set_max_width_chars(60);
    b.append(&lede);
    let flow = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    flow.set_halign(gtk::Align::Center);
    flow.set_margin_top(10);
    for ex in ["Make a Tetris game", "Fix the failing tests", "Explain how this project is built"] {
        let btn = gtk::Button::with_label(ex);
        btn.add_css_class("pill");
        let slot = shared_slot.clone();
        btn.connect_clicked(move |_| {
            if let Some(sh) = slot.borrow().as_ref() {
                let input = sh.borrow().ui.input.clone();
                input.buffer().set_text(ex);
                input.grab_focus();
            }
        });
        flow.append(&btn);
    }
    b.append(&flow);
    (b, hero, lede)
}

/// Quit for good — a signal, a logout: everything is stopped and saved
/// first, and an open dialog can't hold the app open (libadwaita takes a
/// window close with Settings open as "close Settings").
fn quit(w: &adw::ApplicationWindow, shared: &Shared) {
    shut_down(shared);
    if let Some(d) = w.visible_dialog() {
        d.force_close();
    }
    w.close();
    if let Some(app) = w.application() {
        app.quit();
    }
}

fn save_shot(window: &adw::ApplicationWindow, path: &std::path::Path) -> Result<(), String> {
    let paintable = gtk::WidgetPaintable::new(Some(window));
    let (w, h) = (window.width() as f64, window.height() as f64);
    let snap = gtk::Snapshot::new();
    paintable.snapshot(snap.upcast_ref::<gdk::Snapshot>(), w, h);
    let node = snap.to_node().ok_or("nothing to render")?;
    let renderer = window.native().and_then(|n| n.renderer()).ok_or("no renderer")?;
    let tex = renderer.render_texture(node, None);
    tex.save_to_png(path).map_err(|e| e.to_string())
}

/// The folder of your most recent chat in the earlier (Python) Chuck
/// window, so switching over opens where you were.
fn last_python_folder() -> Option<PathBuf> {
    let dir = chuck_store::ledger::data_dir().join("chats");
    let newest = std::fs::read_dir(dir).ok()?.flatten()
        .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
        .max_by_key(|e| e.metadata().and_then(|m| m.modified()).ok())?;
    let v: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(newest.path()).ok()?).ok()?;
    v.get("workspace").and_then(|w| w.as_str()).filter(|w| !w.is_empty()).map(PathBuf::from)
}

fn panel_open_from_settings() -> Vec<String> {
    Settings::load().map.get("fleet_panel_open").and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_else(|| vec!["plan".into()])
}

pub fn build(app: &adw::Application, opts: Options) {
    let provider = gtk::CssProvider::new();
    provider.load_from_string(crate::style::CSS);
    if let Some(d) = gdk::Display::default() {
        gtk::style_context_add_provider_for_display(&d, &provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
    }
    adw::StyleManager::default().set_color_scheme(adw::ColorScheme::ForceDark);
    crate::icons::install();

    let window = adw::ApplicationWindow::builder().application(app).title("Chuck")
        .default_width(if opts.width > 0 { opts.width } else { 1280 })
        .default_height(if opts.height > 0 { opts.height } else { 820 }).build();
    window.add_css_class("main-window");
    let slot: Rc<RefCell<Option<Shared>>> = Rc::new(RefCell::new(None));

    // header: the mark, where we are, what it costs, a new conversation, the panel, settings
    let header = adw::HeaderBar::new();
    header.set_title_widget(Some(&gtk::Box::new(gtk::Orientation::Horizontal, 0)));
    let titlebox = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    let mark = label("C", &["mark"]);
    mark.set_xalign(0.5);
    mark.set_valign(gtk::Align::Center);
    titlebox.append(&mark);
    let tcol = gtk::Box::new(gtk::Orientation::Vertical, 0);
    tcol.set_valign(gtk::Align::Center);
    tcol.append(&label("Chuck", &["title"]));
    let subtitle = label("", &["sub"]);
    subtitle.set_ellipsize(pango::EllipsizeMode::Middle);
    subtitle.set_wrap(false);
    subtitle.set_max_width_chars(56);
    tcol.append(&subtitle);
    titlebox.append(&tcol);
    let chats_btn = gtk::ToggleButton::builder().icon_name("chuck-chats-symbolic").build();
    chats_btn.add_css_class("headerbtn");
    chats_btn.set_tooltip_text(Some("Your conversations"));
    header.pack_start(&chats_btn);
    header.pack_start(&titlebox);
    let folder_btn = gtk::Button::with_label("Choose a folder");
    folder_btn.add_css_class("pill");
    folder_btn.set_tooltip_text(Some("The project Chuck works on"));
    header.pack_start(&folder_btn);
    let new_btn = gtk::Button::from_icon_name("chuck-new-symbolic");
    new_btn.add_css_class("headerbtn");
    new_btn.set_tooltip_text(Some("New conversation (the current one is kept on disk)"));
    header.pack_start(&new_btn);
    // Build: Chuck acts on the folder. Chat: Chuck answers, researches, hands work over.
    let modes = gtk::Box::new(gtk::Orientation::Horizontal, 0);
    modes.add_css_class("linked");
    modes.add_css_class("modes");
    let build_btn = gtk::ToggleButton::with_label("Build");
    build_btn.set_tooltip_text(Some("Chuck builds and fixes things in the project folder"));
    let chat_btn = gtk::ToggleButton::with_label("Chat");
    chat_btn.set_tooltip_text(Some("Chuck answers and looks things up — it can't change anything"));
    chat_btn.set_group(Some(&build_btn));
    modes.append(&build_btn);
    modes.append(&chat_btn);
    header.pack_start(&modes);
    let gear = gtk::Button::from_icon_name("chuck-settings-symbolic");
    gear.add_css_class("headerbtn");
    gear.set_tooltip_text(Some("Settings"));
    header.pack_end(&gear);
    let speaker = gtk::ToggleButton::builder().icon_name("chuck-speaker-symbolic").build();
    speaker.add_css_class("headerbtn");
    header.pack_end(&speaker);
    let side_btn = gtk::ToggleButton::builder().icon_name("chuck-panel-symbolic").active(true).build();
    side_btn.add_css_class("headerbtn");
    side_btn.set_tooltip_text(Some("Show or hide the side panel"));
    header.pack_end(&side_btn);
    let meter = label("", &["meter"]);
    meter.set_wrap(false);
    meter.set_tooltip_text(Some("This window's tokens and spend"));
    header.pack_end(&meter);

    // the conversation
    let transcript = gtk::Box::new(gtk::Orientation::Vertical, 0);
    transcript.set_margin_start(28);
    transcript.set_margin_end(28);
    // room under the newest message for Chuck when he pops up, so he never covers what was just said
    transcript.set_margin_bottom(96);
    let clamp = adw::Clamp::builder().maximum_size(880).child(&transcript).build();
    let scroller = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).child(&clamp).vexpand(true).build();
    scroller.add_css_class("chat-scroll");
    // follow the conversation while you're at the bottom; stay where you are when you scrolled up
    // to read, with a button back down
    let jump = gtk::Button::with_label("↓  new activity");
    jump.add_css_class("jump-btn");
    jump.set_halign(gtk::Align::Center);
    jump.set_valign(gtk::Align::End);
    jump.set_margin_bottom(12);
    jump.set_visible(false);
    let stick = Rc::new(Cell::new(true));
    let adj = scroller.vadjustment();
    let (s1, j1) = (stick.clone(), jump.clone());
    adj.connect_value_changed(move |a| {
        let at_bottom = a.value() + a.page_size() >= a.upper() - 48.0;
        s1.set(at_bottom);
        if at_bottom {
            j1.set_visible(false);
        }
    });
    let (s2, j2) = (stick.clone(), jump.clone());
    adj.connect_upper_notify(move |a| {
        if s2.get() {
            let a = a.clone();
            glib::idle_add_local_once(move || a.set_value(a.upper() - a.page_size()));
        } else if a.upper() > a.page_size() {
            j2.set_visible(true);
        }
    });
    let (a3, s3) = (adj.clone(), stick.clone());
    jump.connect_clicked(move |b| {
        s3.set(true);
        a3.set_value(a3.upper() - a3.page_size());
        b.set_visible(false);
    });
    let overlay = gtk::Overlay::new();
    overlay.set_child(Some(&scroller));
    overlay.add_overlay(&jump);
    let buddy = crate::buddy::Buddy::new();
    overlay.add_overlay(&buddy.root);
    let stack = gtk::Stack::new();
    let (welcome_page, hero, lede) = welcome(slot.clone());
    // behind the welcome: the net of code glyphs that ripples where you click
    let field = crate::glyphs::GlyphField::new();
    let welcome_holder = gtk::Overlay::new();
    welcome_holder.set_child(Some(&field.area));
    welcome_holder.add_overlay(&welcome_page);
    field.listen(&welcome_holder);
    if let Some((x, y)) = opts.poke {
        let f = field.clone();
        glib::timeout_add_local_once(Duration::from_millis(250), move || {
            f.hover(Some((x + 260.0, y + 120.0)));
            f.poke(x, y);
        });
    }
    stack.add_named(&welcome_holder, Some("welcome"));
    stack.add_named(&overlay, Some("chat"));
    stack.set_vexpand(true);

    // the composer
    let input = gtk::TextView::builder().wrap_mode(gtk::WrapMode::WordChar).accepts_tab(false).hexpand(true)
        .top_margin(4).bottom_margin(4).build();
    let hint = label("What should Chuck build or fix? · paste a screenshot with Ctrl+V", &["composer-hint"]);
    hint.set_can_target(false);
    hint.set_valign(gtk::Align::Start);
    hint.set_margin_top(4);
    let in_overlay = gtk::Overlay::new();
    in_overlay.set_child(Some(&input));
    in_overlay.add_overlay(&hint);
    let buf = input.buffer();
    let h2 = hint.clone();
    buf.connect_changed(move |b| h2.set_visible(b.char_count() == 0));
    let comp = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    comp.add_css_class("composer");
    let insc = gtk::ScrolledWindow::builder().child(&in_overlay).hscrollbar_policy(gtk::PolicyType::Never)
        .propagate_natural_height(true).max_content_height(200).hexpand(true).build();
    comp.append(&insc);
    let send = gtk::Button::from_icon_name("chuck-send-symbolic");
    send.add_css_class("suggested-action");
    send.add_css_class("sendbtn");
    send.set_valign(gtk::Align::End);
    send.set_tooltip_text(Some("Send (Enter) · new line: Shift+Enter"));
    let stop = gtk::Button::from_icon_name("chuck-stop-symbolic");
    stop.add_css_class("destructive-action");
    stop.add_css_class("sendbtn");
    stop.set_valign(gtk::Align::End);
    stop.set_visible(false);
    stop.set_tooltip_text(Some("Stop every agent and command (Esc)"));
    // talk instead of typing: heard on this machine, never uploaded
    let mic = gtk::Button::from_icon_name("chuck-mic-symbolic");
    mic.add_css_class("flat");
    mic.add_css_class("micbtn");
    mic.set_valign(gtk::Align::End);
    mic.set_tooltip_text(Some("Talk to Chuck (Ctrl+M) — heard on this machine, never uploaded"));
    comp.append(&mic);
    comp.append(&send);
    comp.append(&stop);
    let status = label("", &["statusbar"]);
    status.set_wrap(false);
    status.set_ellipsize(pango::EllipsizeMode::End);
    // screenshots pasted (Ctrl+V) or dropped, waiting to go with the next message
    let attach = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    attach.add_css_class("attach-row");
    attach.set_visible(false);
    let bottom = gtk::Box::new(gtk::Orientation::Vertical, 4);
    bottom.set_margin_start(28);
    bottom.set_margin_end(28);
    bottom.set_margin_bottom(14);
    bottom.append(&status);
    bottom.append(&attach);
    bottom.append(&comp);
    let bclamp = adw::Clamp::builder().maximum_size(880).child(&bottom).build();
    let main = gtk::Box::new(gtk::Orientation::Vertical, 0);
    main.append(&stack);
    main.append(&bclamp);

    // the right side: what Chuck is doing, in sections you open
    let open = Rc::new(RefCell::new(opts.open.clone().unwrap_or_else(panel_open_from_settings)));
    let o2 = open.clone();
    let remember = opts.open.is_none();
    let on_toggle: Rc<dyn Fn(&'static str, bool)> = Rc::new(move |name, is_open| {
        let mut o = o2.borrow_mut();
        o.retain(|x| x != name);
        if is_open {
            o.push(name.to_string());
        }
        if remember {
            let mut s = Settings::load();
            s.set("fleet_panel_open", serde_json::Value::Array(o.iter().map(|x| x.clone().into()).collect()));
            let _ = s.save();
        }
    });
    let panel = Panel::new(&open.borrow(), on_toggle);
    let split = adw::OverlaySplitView::builder().sidebar_position(gtk::PackType::End).sidebar(&panel.root)
        .content(&main).show_sidebar(true).min_sidebar_width(320.0).max_sidebar_width(380.0)
        .sidebar_width_fraction(0.3).build();
    side_btn.bind_property("active", &split, "show-sidebar").bidirectional().build();

    // the left side: every conversation, newest first
    let chats_box = gtk::Box::new(gtk::Orientation::Vertical, 6);
    chats_box.add_css_class("chats");
    let chats_search = gtk::SearchEntry::builder().placeholder_text("Search chats").build();
    chats_search.set_margin_top(10);
    chats_search.set_margin_start(10);
    chats_search.set_margin_end(10);
    chats_box.append(&chats_search);
    chats_box.append(&label("RECENT", &["side-head"]));
    let chats_list = gtk::Box::new(gtk::Orientation::Vertical, 2);
    chats_list.set_margin_start(6);
    chats_list.set_margin_end(6);
    chats_list.set_margin_bottom(8);
    let chats_scroll = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vexpand(true)
        .child(&chats_list).build();
    chats_box.append(&chats_scroll);
    let show_chats = Settings::load().map.get("chats_sidebar").and_then(|v| v.as_bool()).unwrap_or(true);
    let outer = adw::OverlaySplitView::builder().sidebar_position(gtk::PackType::Start).sidebar(&chats_box)
        .content(&split).show_sidebar(show_chats).min_sidebar_width(220.0).max_sidebar_width(280.0)
        .sidebar_width_fraction(0.2).build();
    chats_btn.set_active(show_chats);
    chats_btn.bind_property("active", &outer, "show-sidebar").bidirectional().build();
    chats_btn.connect_toggled(|b| {
        let mut s = Settings::load();
        s.set("chats_sidebar", b.is_active().into());
        let _ = s.save();
    });

    let view = adw::ToolbarView::new();
    view.add_top_bar(&header);
    view.set_content(Some(&outer));
    window.set_content(Some(&view));

    let folder = opts.folder.clone().or_else(|| {
        std::env::current_dir().ok().filter(|d| {
            let home = std::env::var_os("HOME").map(PathBuf::from);
            Some(d) != home.as_ref() && d != Path::new("/")
        })
    }).or_else(|| Settings::load().str("fleet_last_folder").map(PathBuf::from))
        .or_else(last_python_folder)
        .and_then(|f| f.canonicalize().ok()).filter(|f| f.is_dir());
    let ui = Ui { window: window.clone(), stack, transcript, input: input.clone(), hint, send: send.clone(),
                  stop: stop.clone(), status, subtitle, meter, folder_btn: folder_btn.clone(), new_btn: new_btn.clone(),
                  build_btn: build_btn.clone(), chat_btn: chat_btn.clone(), mic: mic.clone(), speaker: speaker.clone(),
                  hero, lede, chats: chats_list.clone(), chats_search: chats_search.clone(), buddy: buddy.clone(),
                  attach: attach.clone() };
    // MCP servers start in the background: the window doesn't wait for them
    let mcp = Arc::new(chuck_mcp::Manager::new(chuck_mcp::config_path()));
    if mcp.configured() {
        let m = mcp.clone();
        std::thread::Builder::new().name("mcp-start".into()).spawn(move || m.start_all()).ok();
    }
    let chat_mode = opts.chat.unwrap_or_else(|| Settings::load().map.get("agent_mode").and_then(|v| v.as_bool()) == Some(false));
    build_btn.set_active(!chat_mode);
    chat_btn.set_active(chat_mode);
    let shared: Shared = Rc::new(RefCell::new(State {
        ui, panel, opts: opts.clone(), folder: None, saved: Saved::default(), running: None, names: HashMap::new(),
        models: HashMap::new(), lane: None, writing: None, live_cards: Vec::new(), pasted: Vec::new(), last_ps: Vec::new(), chat: chat_mode, on_handoff: None, on_continue: None, on_settings: None, auto_cancel: Rc::new(Cell::new(false)), mcp: mcp.clone(),
        lsp: Arc::new(setup::lsp(&Settings::load())),
        daemons: chuck_daemon::Registry::load(&chuck_daemon::Registry::default_path()),
        inbox: chuck_daemon::Inbox::load(&chuck_daemon::Inbox::default_path(), 200), daemon_live: HashMap::new(),
        daemon_check: None, voice: chuck_voice::Voice::new(), ears: chuck_voice::Ears::new(), heard: None,
        memory: Arc::new(chuck_store::memory::Memory::at(&chuck_store::memory::Memory::default_path())),
        later: Vec::new(), notice: None, tokens: 0,
        spent: 0.0, prompt: 0, cached: 0, unsaved: false,
    }));
    *slot.borrow_mut() = Some(shared.clone());
    // the handoff button: to Build, and send
    let weak = Rc::downgrade(&shared);
    shared.borrow_mut().on_handoff = Some(Rc::new(move |request: String| {
        let Some(sh) = weak.upgrade() else { return };
        if sh.borrow().running.is_some() {
            return;
        }
        sh.borrow_mut().set_mode(false, &sh);
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || start(&sh2, request));
    }));
    let weak = Rc::downgrade(&shared);
    shared.borrow_mut().on_continue = Some(Rc::new(move || {
        let Some(sh) = weak.upgrade() else { return };
        if sh.borrow().running.is_some() {
            return;
        }
        sh.borrow_mut().set_mode(false, &sh);
        let sh2 = sh.clone();
        glib::idle_add_local_once(move || start(&sh2, "/continue".into()));
    }));
    let weak = Rc::downgrade(&shared);
    shared.borrow_mut().on_settings = Some(Rc::new(move || {
        if let Some(sh) = weak.upgrade() {
            glib::idle_add_local_once(move || settings_dialog(&sh));
        }
    }));
    shared.borrow_mut().open_folder(folder, &shared);
    shared.borrow_mut().set_running(false);
    for (btn, chat) in [(build_btn.clone(), false), (chat_btn.clone(), true)] {
        let sh = shared.clone();
        btn.connect_toggled(move |b| {
            if b.is_active() {
                // after the toggle settles: set_mode may set the buttons back
                let sh = sh.clone();
                glib::idle_add_local_once(move || sh.borrow_mut().set_mode(chat, &sh));
            }
        });
    }

    let submit = {
        let sh = shared.clone();
        move || {
            let buf = sh.borrow().ui.input.buffer();
            let text = buf.text(&buf.start_iter(), &buf.end_iter(), false).to_string();
            if text.trim().is_empty() && sh.borrow().pasted.is_empty() {
                return;
            }
            buf.set_text("");
            start(&sh, text);
        }
    };
    let s1 = submit.clone();
    send.connect_clicked(move |_| s1());
    let sh = shared.clone();
    let stop_job = move || {
        let st = sh.borrow();
        st.auto_cancel.set(true);
        st.voice.stop();
        if let Some(r) = &st.running {
            r.stop.store(true, Ordering::SeqCst);
        }
    };
    let sj = stop_job.clone();
    stop.connect_clicked(move |_| sj());
    let keys = gtk::EventControllerKey::new();
    let s2 = submit.clone();
    keys.connect_key_pressed(move |_, key, _, mods| {
        if (key == gdk::Key::Return || key == gdk::Key::KP_Enter) && !mods.contains(gdk::ModifierType::SHIFT_MASK) {
            s2();
            return glib::Propagation::Stop;
        }
        if key == gdk::Key::Escape {
            stop_job();
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    input.add_controller(keys);
    // Ctrl+V of a picture (a screenshot tool's copy): it goes with the next message; text pastes as ever
    let sh = shared.clone();
    input.connect_paste_clipboard(move |tv| {
        let cb = tv.clipboard();
        if !cb.formats().contains_type(gdk::Texture::static_type()) {
            return;
        }
        tv.stop_signal_emission_by_name("paste-clipboard");
        let sh = sh.clone();
        cb.read_texture_async(None::<&gtk::gio::Cancellable>, move |r| match r {
            Ok(Some(tex)) => attach_texture(&sh, &tex),
            Ok(None) => {}
            Err(e) => sh.borrow().ui.status.set_label(&format!("Couldn't paste that picture: {e}")),
        });
    });
    // a picture file dropped on the box you type in: the same
    let drop = gtk::DropTarget::new(gdk::FileList::static_type(), gdk::DragAction::COPY);
    let sh = shared.clone();
    drop.connect_drop(move |_, v, _, _| {
        let Ok(files) = v.get::<gdk::FileList>() else { return false };
        let mut any = false;
        for f in files.files() {
            let Some(p) = f.path() else { continue };
            let ext = p.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
            if !["png", "jpg", "jpeg", "webp", "gif", "bmp"].contains(&ext.as_str()) {
                continue;
            }
            // kept as PNG (what the eyes are sent), whatever it was
            match gdk::Texture::from_filename(&p) {
                Ok(tex) => attach_texture(&sh, &tex),
                Err(e) => sh.borrow().ui.status.set_label(&format!("Couldn't open {}: {e}", p.display())),
            }
            any = true;
        }
        any
    });
    comp.add_controller(drop);
    // Ctrl+M anywhere in the window: talk
    let sh = shared.clone();
    let mic_key = gtk::EventControllerKey::new();
    mic_key.connect_key_pressed(move |_, key, _, mods| {
        if mods.contains(gdk::ModifierType::CONTROL_MASK) && (key == gdk::Key::m || key == gdk::Key::M) {
            mic_clicked(&sh);
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    window.add_controller(mic_key);
    // Ctrl+P (or Ctrl+K) anywhere: the command palette
    let pal_key = gtk::EventControllerKey::new();
    {
        let (sh, win) = (shared.clone(), window.clone());
        let (gear, new_btn, folder_btn, build_btn, chat_btn, speaker, side_btn, stop) = (gear.clone(), new_btn.clone(),
            folder_btn.clone(), build_btn.clone(), chat_btn.clone(), speaker.clone(), side_btn.clone(), stop.clone());
        let open = move |query: &str| {
            use crate::palette::Item;
            let click = |b: &gtk::Button| { let b = b.clone(); Box::new(move || b.emit_clicked()) as Box<dyn Fn()> };
            let run = |cmd: &'static str| { let sh = sh.clone(); Box::new(move || start(&sh, cmd.into())) as Box<dyn Fn()> };
            let type_in = |text: &'static str| {
                let sh = sh.clone();
                Box::new(move || {
                    let input = sh.borrow().ui.input.clone();
                    input.buffer().set_text(text);
                    input.buffer().place_cursor(&input.buffer().end_iter());
                    input.grab_focus();
                }) as Box<dyn Fn()>
            };
            let toggle = |b: &gtk::ToggleButton| { let b = b.clone(); Box::new(move || b.set_active(!b.is_active())) as Box<dyn Fn()> };
            let it = |title: &str, group: &'static str, keywords: &'static str, shortcut: &'static str, run: Box<dyn Fn()>| Item {
                title: title.into(), group, keywords, shortcut, run };
            let (fb, cb) = (build_btn.clone(), chat_btn.clone());
            let sh2 = sh.clone();
            let items = vec![
                it("New conversation", "Chat", "clear fresh reset", "", click(&new_btn)),
                it("Switch to Chat", "Chat", "mode answer ask question", "", Box::new(move || cb.set_active(true))),
                it("Switch to Build", "Chat", "mode build fleet code", "", Box::new(move || fb.set_active(true))),
                it("Stop everything", "Chat", "cancel halt kill agents commands", "Esc", click(&stop)),
                it("/help — every command", "Commands", "commands keys", "", run("/help")),
                it("/doctor — check everything Chuck depends on", "Commands", "health diagnose problems", "", run("/doctor")),
                it("/update — install the newest Chuck", "Commands", "upgrade version", "", run("/update")),
                it("/restart — open the new Chuck", "Commands", "reload", "", run("/restart")),
                it("/cost — what was spent", "Commands", "money budget spend tokens", "", run("/cost")),
                it("/export — save this conversation as Markdown", "Commands", "markdown save file", "", run("/export")),
                it("/remind — a reminder", "Commands", "alarm timer later", "", type_in("/remind in 20m: ")),
                it("/daemon — watch something and report", "Commands", "monitor trigger watch", "", type_in("/daemon every 2h: ")),
                it("Choose a folder", "Folder", "project open directory", "", click(&folder_btn)),
                it("/continue — carry on the paused job", "Folder", "resume keep going incomplete", "", run("/continue")),
                it("/undo — put the folder back to before the last job", "Folder", "rewind revert checkpoint", "", run("/undo")),
                it("/diff — every change the last job made", "Folder", "changes review", "", run("/diff")),
                it("Talk", "Voice", "microphone listen speak dictate", "Ctrl+M", Box::new(move || mic_clicked(&sh2))),
                it("Read replies aloud on or off", "Voice", "speaker speech tts mute", "", toggle(&speaker)),
                it("Show or hide the side panel", "View", "panel right sidebar", "", toggle(&side_btn)),
                it("Settings", "Settings", "preferences key budget models voice research mcp", "", click(&gear)),
            ];
            crate::palette::show(&win, items, query);
        };
        let open = Rc::new(open);
        let o2 = open.clone();
        pal_key.connect_key_pressed(move |_, key, _, mods| {
            if !(mods.contains(gdk::ModifierType::CONTROL_MASK)
                && matches!(key, gdk::Key::p | gdk::Key::P | gdk::Key::k | gdk::Key::K)) {
                return glib::Propagation::Proceed;
            }
            o2("");
            glib::Propagation::Stop
        });
        if let Some(q) = opts.palette.clone() {
            glib::timeout_add_local_once(Duration::from_millis(300), move || open(&q));
        }
    }
    window.add_controller(pal_key);
    let sh = shared.clone();
    mic.connect_clicked(move |_| mic_clicked(&sh));
    {
        let s = Settings::load();
        let on = s.map.get("tts").and_then(|v| v.as_bool()).unwrap_or(true);
        speaker.set_active(on);
        speaker_look(&speaker, on);
        if on {
            shared.borrow().voice.warm(&chuck_voice::Prefs::from(&s));
        }
    }
    let sh = shared.clone();
    speaker.connect_toggled(move |b| {
        let on = b.is_active();
        speaker_look(b, on);
        let mut s = Settings::load();
        s.set("tts", on.into());
        let _ = s.save();
        let st = sh.borrow();
        if on {
            st.voice.warm(&chuck_voice::Prefs::from(&s));
        } else {
            st.voice.stop();
        }
    });
    let sh = shared.clone();
    glib::timeout_add_local(Duration::from_millis(60), move || {
        hear(&sh);
        glib::ControlFlow::Continue
    });
    let sh = shared.clone();
    gear.connect_clicked(move |_| settings_dialog(&sh));
    let sh = shared.clone();
    new_btn.connect_clicked(move |_| {
        let mut st = sh.borrow_mut();
        if st.running.is_some() {
            return;
        }
        st.save();
        // the current conversation (a folder's, or Chat's) is kept in the Recent list
        if let Some(k) = st.key() {
            chat::archive(&k);
        }
        st.saved = Saved::default();
        st.redraw();
        st.panel.reset();
        st.ui.input.grab_focus();
        refresh_chats(&st, &sh);
    });
    let sh = shared.clone();
    shared.borrow().ui.chats_search.connect_search_changed(move |_| {
        if let Ok(st) = sh.try_borrow() {
            refresh_chats(&st, &sh);
        }
    });
    let sh = shared.clone();
    folder_btn.connect_clicked(move |_| {
        let dialog = gtk::FileDialog::builder().title("The project folder").modal(true).build();
        let sh = sh.clone();
        let win = sh.borrow().ui.window.clone();
        dialog.select_folder(Some(&win), gtk::gio::Cancellable::NONE, move |r| {
            let Some(p) = r.ok().and_then(|f| f.path()).and_then(|p| p.canonicalize().ok()) else { return };
            let mut s = Settings::load();
            s.set("fleet_last_folder", p.display().to_string().into());
            let _ = s.save();
            let shared = sh.clone();
            sh.borrow_mut().open_folder(Some(p), &shared);
        });
    });
    let sh = shared.clone();
    glib::timeout_add_local(Duration::from_millis(60), move || {
        pump(&sh);
        glib::ControlFlow::Continue
    });
    let sh = shared.clone();
    glib::timeout_add_local(Duration::from_millis(500), move || {
        tick(&sh);
        glib::ControlFlow::Continue
    });
    // daemons: checked every 30 s while the window is open (and never after it closes)
    refresh_daemons(&shared.borrow(), &shared);
    let sh = shared.clone();
    glib::timeout_add_seconds_local(30, move || {
        daemon_tick(&sh);
        glib::ControlFlow::Continue
    });
    let sh = shared.clone();
    glib::timeout_add_local_once(Duration::from_secs(3), move || daemon_tick(&sh));
    // once a day: is there a newer Chuck? (Settings can turn this off; scripted runs never ask)
    if opts.model.is_none() && crate::update::due() {
        let (tx, rx) = channel();
        std::thread::Builder::new().name("update-check".into()).spawn(move || {
            if let Ok(crate::update::Found::Newer { latest, .. }) = crate::update::check() {
                let _ = tx.send(format!("A newer Chuck is out ({}) — type /update to install it", &latest[..10.min(latest.len())]));
            }
        }).ok();
        shared.borrow_mut().notice = Some(rx);
    }

    // closing — the window, Ctrl-C, a logout's SIGTERM/SIGHUP — stops every agent and command first
    let sh = shared.clone();
    window.connect_close_request(move |_| {
        shut_down(&sh);
        glib::Propagation::Proceed
    });
    for sig in [libc::SIGTERM, libc::SIGHUP, libc::SIGINT] {
        let (w, sh) = (window.clone(), shared.clone());
        glib::unix_signal_add_local(sig, move || {
            quit(&w, &sh);
            glib::ControlFlow::Break
        });
    }

    // tools/bench.sh: report the first painted frame (against the shell's clock) and memory, then quit
    if let Ok(t0) = std::env::var("CHUCK_BENCH_FRAME") {
        let app = app.clone();
        window.connect_map(move |w| {
            let (app, t0) = (app.clone(), t0.clone());
            if let Some(clock) = w.frame_clock() {
                let fired = Rc::new(Cell::new(false));
                clock.connect_after_paint(move |_| {
                    if fired.replace(true) {
                        return;
                    }
                    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs_f64()).unwrap_or(0.0);
                    let start: f64 = t0.parse().unwrap_or(now);
                    let rss = std::fs::read_to_string("/proc/self/status").ok().and_then(|s| s.lines()
                        .find(|l| l.starts_with("VmRSS:")).map(|l| l.split_whitespace().nth(1).unwrap_or("0").to_string()))
                        .unwrap_or_default();
                    println!("first-frame {:.3} rss-kb {rss}", now - start);
                    app.quit();
                });
            }
        });
    }
    window.present();
    input.grab_focus();
    if opts.settings {
        let sh = shared.clone();
        glib::timeout_add_local_once(Duration::from_millis(100), move || settings_dialog(&sh));
    }
    if opts.listen {
        let sh = shared.clone();
        glib::timeout_add_local_once(Duration::from_millis(400), move || mic_clicked(&sh));
    }
    for p in &opts.attach {
        match gdk::Texture::from_filename(p) {
            Ok(tex) => attach_texture(&shared, &tex),
            Err(e) => eprintln!("chuck: couldn't attach {}: {e}", p.display()),
        }
    }
    if let Some(text) = opts.send.clone() {
        let sh = shared.clone();
        glib::timeout_add_local_once(Duration::from_millis(150), move || start(&sh, text));
    }
    if let Some((path, ms)) = opts.shot.clone() {
        let w = window.clone();
        let sh = shared.clone();
        glib::timeout_add_local_once(Duration::from_millis(ms), move || {
            tick(&sh);
            let (w2, sh2) = (w.clone(), sh.clone());
            glib::timeout_add_local_once(Duration::from_millis(250), move || {
                match save_shot(&w2, &path) {
                    Ok(()) => eprintln!("saved {}", path.display()),
                    Err(e) => eprintln!("screenshot failed: {e}"),
                }
                quit(&w2, &sh2);
            });
        });
    }
}
