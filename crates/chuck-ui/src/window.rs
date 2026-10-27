//! The Chuck window: a conversation with the Lead, the fleet's work under
//! each reply, the fleet itself (/top) and the Done gate beside it.

use crate::md::{self, Part};
use adw::prelude::*;
use chuck_core::{AgentId, Kernel, ProcState, Role};
use chuck_model::{fmt_tokens, fmt_usd, ChatModel};
use chuck_runtime::{setup, Event, Events, Job, JobReport, Outcome, Runtime};
use chuck_store::Settings;
use gtk::{gdk, glib, pango};
use std::cell::RefCell;
use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::Arc;
use std::time::Duration;

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
}

struct Running {
    stop: Arc<AtomicBool>,
    answers: Sender<String>,
    events: Receiver<Event>,
    kernel: Arc<Kernel>,
    done: Receiver<JobReport>,
}

struct Ui {
    window: adw::ApplicationWindow,
    stack: gtk::Stack,
    transcript: gtk::Box,
    input: gtk::TextView,
    send: gtk::Button,
    stop: gtk::Button,
    status: gtk::Label,
    subtitle: gtk::Label,
    meter: gtk::Label,
    fleet: gtk::ListBox,
    phase: gtk::Label,
    unmet: gtk::Box,
    criteria: gtk::Box,
    folder_btn: gtk::Button,
}

struct State {
    ui: Ui,
    opts: Options,
    folder: Option<PathBuf>,
    history: Vec<(String, String)>,
    running: Option<Running>,
    names: HashMap<AgentId, (String, Role)>,
    models: HashMap<AgentId, String>,
    lane: Option<gtk::Box>,
    spent: f64,
    tokens: u64,
    last_lead_text: String,
}

type Shared = Rc<RefCell<State>>;

fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let l = gtk::Label::builder().label(text).xalign(0.0).wrap(true).wrap_mode(pango::WrapMode::WordChar).build();
    for c in classes {
        l.add_css_class(c);
    }
    l
}

fn markup(m: &str, classes: &[&str]) -> gtk::Label {
    let l = label("", classes);
    l.set_markup(m);
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
        }
    }
    b
}

fn card(kind: &str, class: &str) -> (gtk::Box, gtk::Box, gtk::Box) {
    let c = gtk::Box::new(gtk::Orientation::Vertical, 0);
    c.add_css_class("card");
    c.add_css_class(class);
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

impl State {
    fn push(&mut self, w: &impl IsA<gtk::Widget>) {
        self.ui.transcript.append(w);
        self.ui.stack.set_visible_child_name("chat");
    }

    fn user_says(&mut self, text: &str) {
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        row.add_css_class("user-row");
        row.set_halign(gtk::Align::End);
        let bubble = gtk::Box::new(gtk::Orientation::Vertical, 0);
        bubble.add_css_class("user-bubble");
        let l = label(text, &[]);
        l.set_selectable(true);
        l.set_max_width_chars(70);
        bubble.append(&l);
        row.append(&bubble);
        self.push(&row);
        self.lane = None;
    }

    fn lead_says(&mut self, text: &str) {
        let col = gtk::Box::new(gtk::Orientation::Vertical, 6);
        col.add_css_class("bot-row");
        let by = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let av = label("C", &["avatar"]);
        av.set_xalign(0.5);
        by.append(&av);
        by.append(&label("Lead", &["byline"]));
        let model = self.names.iter().find(|(_, (n, _))| n == "lead").and_then(|(id, _)| self.models.get(id))
            .cloned().unwrap_or_default();
        by.append(&label(&model, &["byline-meta"]));
        col.append(&by);
        let body = render(text);
        body.set_margin_start(28);
        col.append(&body);
        self.push(&col);
        self.lane = None;
        self.last_lead_text = text.to_string();
    }

    fn lane_row(&mut self, markup_text: &str) {
        if self.lane.is_none() {
            let lane = gtk::Box::new(gtk::Orientation::Vertical, 3);
            lane.add_css_class("lane");
            lane.set_margin_top(8);
            self.push(&lane);
            self.lane = Some(lane);
        }
        let l = markup(markup_text, &["act"]);
        l.set_wrap(true);
        self.lane.as_ref().unwrap().append(&l);
    }

    fn who(&self, id: AgentId) -> String {
        self.names.get(&id).map(|(n, _)| n.clone()).unwrap_or_else(|| format!("#{id}"))
    }

    fn set_running(&mut self, on: bool) {
        self.ui.send.set_visible(!on);
        self.ui.stop.set_visible(on);
        self.ui.status.set_label(if on { "the fleet is working — Esc stops it" } else { "" });
    }

    fn refresh_header(&self) {
        let folder = self.folder.as_ref().map(|f| f.display().to_string()).unwrap_or_else(|| "no folder".into());
        let access = match &self.folder {
            Some(f) => setup::access_of(&Settings::load(), f).label(),
            None => "",
        };
        self.ui.subtitle.set_label(&if access.is_empty() { folder } else { format!("{folder} · {access}") });
        self.ui.folder_btn.set_label(&self.folder.as_ref().and_then(|f| f.file_name())
            .map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "Choose a folder".into()));
        self.ui.meter.set_markup(&format!("{} · <span foreground=\"#A78BFA\" weight=\"bold\">{}</span>",
                                          fmt_tokens(self.tokens), fmt_usd(self.spent)));
    }
}

fn on_event(st: &mut State, e: Event) {
    let esc = md::escape;
    match e {
        Event::AgentStarted { id, role, name, model, task } => {
            st.names.insert(id, (name.clone(), role));
            st.models.insert(id, short(&model).to_string());
            if name != "lead" {
                let what = match (&task, name.as_str()) {
                    (Some(t), _) => format!(" on {t}"),
                    (None, "critic") => " — reviewing the whole result".into(),
                    _ => String::new(),
                };
                st.lane_row(&format!("<span foreground=\"#5F5F6E\">+ {} ({}){}</span>", esc(&name), esc(short(&model)), esc(&what)));
            }
        }
        Event::Said { id, text } => {
            if st.names.get(&id).is_some_and(|(n, r)| *r == Role::Lead && n == "lead") {
                st.lead_says(&text);
            } else {
                let who = st.who(id);
                st.lane_row(&format!("<span foreground=\"#5F5F6E\">{:<6} │ {}</span>", esc(&who), esc(&one_line(&text, 160))));
            }
        }
        Event::Action { id, tool, arg, ok, summary } => {
            let who = st.who(id);
            let what = match tool.as_str() {
                "bash" | "test" => format!("{} → {}", one_line(&arg, 80), one_line(&summary, 40)),
                _ if summary.is_empty() => one_line(&arg, 100),
                _ => one_line(&summary, 120),
            };
            let mark = if ok { "<span foreground=\"#4ADE9A\">✓</span>" } else { "<span foreground=\"#F87171\">✗</span>" };
            st.lane_row(&format!("{mark} <span foreground=\"#5F5F6E\">{:<6}</span> {} <span foreground=\"#8E8E9E\">{}</span>",
                                 esc(&who), esc(&tool), esc(&what)));
        }
        Event::Assigned { task, title, worker, role } => {
            let who = st.who(worker);
            st.lane_row(&format!("<span foreground=\"#A78BFA\">◆ {} “{}” → {} ({})</span>", esc(&task),
                                 esc(&one_line(&title, 60)), esc(&who), role.label()));
        }
        Event::Submitted { task, worker, lines } => {
            let who = st.who(worker);
            st.lane_row(&format!("<span foreground=\"#A78BFA\">◆ {} submitted by {} ({lines} lines)</span>", esc(&task), esc(&who)));
        }
        Event::Reviewed { task, accepted, defects } => {
            let what = if task == "job" { "independent review".to_string() } else { task };
            st.lane_row(&if accepted {
                format!("<span foreground=\"#4ADE9A\">✓ {}: accepted</span>", esc(&what))
            } else {
                format!("<span foreground=\"#F5B454\">↺ {}: sent back — {}</span>", esc(&what), esc(&one_line(&defects.join("; "), 160)))
            });
        }
        Event::Merged { task, applied, merged, conflicts, refused } => {
            let mut s = format!("⇣ {task} merged into your folder: {applied} applied, {merged} merged");
            if !conflicts.is_empty() {
                s.push_str(&format!(", CONFLICTS: {}", conflicts.join(", ")));
            }
            if !refused.is_empty() {
                s.push_str(&format!(", refused: {}", refused.join(", ")));
            }
            let color = if conflicts.is_empty() { "#4ADE9A" } else { "#F5B454" };
            st.lane_row(&format!("<span foreground=\"{color}\">{}</span>", esc(&s)));
        }
        Event::Notice { id, text } => {
            let who = st.who(id);
            st.lane_row(&format!("<span foreground=\"#F5B454\">! {}: {}</span>", esc(&who), esc(&text)));
        }
        Event::Usage { tokens, usd, .. } => {
            st.tokens += tokens;
            st.spent += usd;
            st.refresh_header();
        }
        Event::Gate { id, task: None, phase, unmet } => {
            if st.names.get(&id).is_some_and(|(n, _)| n == "lead") {
                st.ui.phase.set_label(&format!("Phase: {phase}"));
                st.ui.phase.remove_css_class("phase-incomplete");
                if phase == "INCOMPLETE" {
                    st.ui.phase.add_css_class("phase-incomplete");
                }
                while let Some(c) = st.ui.unmet.first_child() {
                    st.ui.unmet.remove(&c);
                }
                for u in unmet.iter().take(8) {
                    st.ui.unmet.append(&label(&format!("· {u}"), &["unmet"]));
                }
            }
        }
        Event::Board { criteria } => {
            while let Some(c) = st.ui.criteria.first_child() {
                st.ui.criteria.remove(&c);
            }
            for (id, text, verify, ev) in criteria {
                let row = gtk::Box::new(gtk::Orientation::Vertical, 3);
                row.add_css_class("crit-row");
                let top = gtk::Box::new(gtk::Orientation::Horizontal, 8);
                top.append(&label(&id, &["crit-id"]));
                let t = label(&text, &["crit-text"]);
                t.set_hexpand(true);
                top.append(&t);
                row.append(&top);
                if let Some(v) = verify {
                    row.append(&label(&format!("verify: {v}"), &["fleet-meta"]));
                }
                let chips = gtk::FlowBox::builder().selection_mode(gtk::SelectionMode::None).max_children_per_line(3).build();
                if ev.is_empty() {
                    chips.insert(&label("no evidence yet", &["chip", "chip-bad"]), -1);
                }
                for e in ev.iter().rev().take(4) {
                    let cls = if e.contains("FAIL") || e.contains("→ exit 1") { "chip-bad" } else if e.contains("pass") { "chip-ok" } else { "chip" };
                    chips.insert(&label(&one_line(e, 40), &["chip", cls]), -1);
                }
                row.append(&chips);
                st.ui.criteria.append(&row);
            }
        }
        Event::Ask { question, .. } => {
            let (c, body, foot) = card("THE LEAD ASKS", "ask-card");
            body.append(&label(&question, &["prose"]));
            let entry = gtk::Entry::builder().hexpand(true).placeholder_text("Your answer").build();
            let btn = gtk::Button::with_label("Answer");
            btn.add_css_class("suggested-action");
            foot.append(&entry);
            foot.append(&btn);
            let tx = st.running.as_ref().map(|r| r.answers.clone());
            let (e2, f2) = (entry.clone(), foot.clone());
            let answer = move || {
                if let Some(tx) = &tx {
                    let _ = tx.send(e2.text().to_string());
                }
                f2.set_sensitive(false);
            };
            let a2 = answer.clone();
            btn.connect_clicked(move |_| a2());
            entry.connect_activate(move |_| answer());
            st.push(&c);
            st.lane = None;
        }
        Event::Confirm { command, why, .. } => {
            let (c, body, foot) = card("CRITICAL — NEEDS YOUR YES", "crit-card");
            body.append(&label("The lead wants to run:", &["prose"]));
            let cmd = label(&command, &["cmd-text"]);
            cmd.set_selectable(true);
            body.append(&cmd);
            body.append(&label(&why, &["sub"]));
            let run = gtk::Button::with_label("Run it");
            run.add_css_class("destructive-action");
            let no = gtk::Button::with_label("Refuse");
            foot.append(&no);
            foot.append(&run);
            let tx = st.running.as_ref().map(|r| r.answers.clone());
            for (b, word) in [(run, "yes"), (no, "no")] {
                let (tx, f) = (tx.clone(), foot.clone());
                b.connect_clicked(move |_| {
                    if let Some(tx) = &tx {
                        let _ = tx.send(word.into());
                    }
                    f.set_sensitive(false);
                });
            }
            st.push(&c);
            st.lane = None;
        }
        Event::Finished(_) | Event::AgentEnded { .. } | Event::Output { .. } | Event::Thinking { .. }
        | Event::Delta { .. } | Event::Gate { .. } => {}
    }
}

fn show_gate(st: &State, gate: &chuck_core::DoneGate) {
    let phase = gate.phase.label();
    st.ui.phase.set_label(&format!("Phase: {phase}"));
    st.ui.phase.remove_css_class("phase-incomplete");
    if phase == "INCOMPLETE" {
        st.ui.phase.add_css_class("phase-incomplete");
    }
    while let Some(c) = st.ui.unmet.first_child() {
        st.ui.unmet.remove(&c);
    }
    if !gate.is_done() {
        for u in gate.check().iter().take(8) {
            st.ui.unmet.append(&label(&format!("· {}", u.describe()), &["unmet"]));
        }
    }
}

fn finished(st: &mut State, report: JobReport) {
    if !report.gate.criteria.is_empty() || report.gate.is_done() {
        show_gate(st, &report.gate);
        on_event(st, Event::Board { criteria: report.gate.board().into_iter()
            .map(|(c, ev)| (c.id.to_string(), c.text, c.verify, ev)).collect() });
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
        Outcome::Paused { gaps } => format!("Not finished. Still missing: {}", gaps.join("; ")),
        Outcome::Failed { error } => error.clone(),
    };
    if !kind.is_empty() {
        let (c, body, foot) = card(&format!("{kind} · {} tokens · {}", fmt_tokens(report.tokens), fmt_usd(report.usd)), class);
        match &report.outcome {
            Outcome::Done { summary } => body.append(&render(summary)),
            Outcome::Paused { gaps } => {
                body.append(&label("Not finished. What is still missing:", &["prose"]));
                for g in gaps {
                    body.append(&label(&format!("·  {g}"), &["prose"]));
                }
            }
            Outcome::Failed { error } => body.append(&label(error, &["prose"])),
            Outcome::Answered { .. } => {}
        }
        if let (Some(cp), Some(folder)) = (report.checkpoint.clone(), st.folder.clone()) {
            let b = gtk::Button::with_label("Rewind this job");
            b.add_css_class("pill");
            let st_note = label("", &["sub"]);
            let note = st_note.clone();
            b.connect_clicked(move |b| {
                let r = chuck_store::Shadow::new(&folder, &chuck_store::checkpoint::default_store())
                    .and_then(|sh| sh.restore(&cp));
                match r {
                    Ok((undo, changed)) => {
                        note.set_label(&format!("{} file(s) put back · undo: chuck rewind {}", changed.len(), &undo[..10]));
                        b.set_sensitive(false);
                    }
                    Err(e) => note.set_label(&e.to_string()),
                }
            });
            foot.append(&b);
            foot.append(&st_note);
        }
        st.push(&c);
    }
    st.history.push(("lead".into(), reply));
    st.lane = None;
    st.set_running(false);
}

fn start(shared: &Shared, text: String) {
    let mut st = shared.borrow_mut();
    if st.running.is_some() || text.trim().is_empty() {
        return;
    }
    st.user_says(&text);
    let Some(folder) = st.folder.clone() else {
        st.lead_says("Pick a project folder first (the button at the top): the fleet works on a folder.");
        return;
    };
    let settings = Settings::load();
    let (model, cfg) = match (&st.opts.model, setup::from_settings(&settings, &folder, None)) {
        (Some(m), _) => {
            let cfg = chuck_runtime::RuntimeConfig { access: setup::access_of(&settings, &folder),
                                                     ..Default::default() };
            (m.clone(), cfg)
        }
        (None, Ok(s)) => {
            for n in &s.notes {
                st.lane_row(&format!("<span foreground=\"#F5B454\">! {}</span>", md::escape(n)));
            }
            (s.model, s.cfg)
        }
        (None, Err(e)) => {
            st.lead_says(&format!("I can't start: {e}"));
            return;
        }
    };
    let (tx, rx) = channel();
    let rt = Runtime::new(model, cfg, Events(Some(tx)));
    let kernel = rt.kernel.clone();
    let mut job = Job::new(text.clone(), &folder);
    job.history = st.history.clone();
    let (atx, arx) = channel();
    job.answers = Some(arx);
    let stop = job.stop.clone();
    let (dtx, drx) = channel();
    std::thread::Builder::new().name("chuck-job".into()).spawn(move || {
        let _ = dtx.send(rt.run(job));
    }).expect("start the job thread");
    st.history.push(("user".into(), text));
    st.running = Some(Running { stop, answers: atx, events: rx, kernel, done: drx });
    st.set_running(true);
}

/// Drain the fleet's events into the window; refresh /top.
fn pump(shared: &Shared) {
    let mut st = shared.borrow_mut();
    let Some(run) = &st.running else { return };
    let events: Vec<Event> = run.events.try_iter().take(400).collect();
    let report = run.done.try_recv().ok();
    let ps = run.kernel.ps(true);
    for e in events {
        on_event(&mut st, e);
    }
    // the fleet panel: every agent of this job, as the kernel sees it (/top)
    while let Some(c) = st.ui.fleet.first_child() {
        st.ui.fleet.remove(&c);
    }
    if ps.is_empty() {
        st.ui.fleet.append(&label("Agents appear here while a job runs.", &["unmet"]));
    }
    for p in &ps {
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 10);
        row.add_css_class("fleet-row");
        let dot = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        dot.add_css_class("dot");
        dot.add_css_class(match p.state {
            ProcState::Running => "dot-run",
            ProcState::Waiting | ProcState::Ready | ProcState::Stopped => "dot-wait",
            ProcState::Done => "dot-done",
            ProcState::Failed | ProcState::Killed => "dot-fail",
        });
        dot.set_valign(gtk::Align::Center);
        row.append(&dot);
        let col = gtk::Box::new(gtk::Orientation::Vertical, 1);
        col.set_hexpand(true);
        let model = st.models.get(&p.pid).cloned().unwrap_or_else(|| p.role.label().into());
        let top = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        top.append(&label(&p.name, &["fleet-name"]));
        top.append(&label(&model, &["fleet-meta"]));
        col.append(&top);
        let doing = if p.activity.is_empty() { p.state.label().to_string() } else { p.activity.clone() };
        col.append(&label(&format!("{} · {} tok · {}", one_line(&doing, 34), fmt_tokens(p.tokens), fmt_usd(p.usd)), &["fleet-meta"]));
        if !p.brief.is_empty() && p.role != Role::Lead {
            col.append(&label(&one_line(&p.brief, 44), &["fleet-meta"]));
        }
        row.append(&col);
        st.ui.fleet.append(&row);
    }
    if let Some(r) = report {
        // the last events of the job may still be queued
        let rest: Vec<Event> = st.running.as_ref().map(|r| r.events.try_iter().collect()).unwrap_or_default();
        for e in rest {
            on_event(&mut st, e);
        }
        st.running = None;
        finished(&mut st, r);
    }
}

fn settings_dialog(shared: &Shared) {
    let s = Settings::load();
    let page = adw::PreferencesPage::new();
    let api = adw::PreferencesGroup::builder().title("SiliconFlow")
        .description("The whole fleet thinks on SiliconFlow, with your key. Shared with the Chuck window.").build();
    let key = adw::PasswordEntryRow::builder().title("API key").build();
    key.set_text(s.api_key().unwrap_or(""));
    api.add(&key);
    page.add(&api);

    let fleet = adw::PreferencesGroup::builder().title("Fleet")
        .description("One Lead (DeepSeek-V4.1-Flash) plus up to four workers of each family.").build();
    let ds = adw::SpinRow::with_range(0.0, 4.0, 1.0);
    ds.set_title("DeepSeek workers");
    ds.set_value(s.num("fleet_deepseek_workers", 4.0, 0.0, 4.0));
    let glm = adw::SpinRow::with_range(0.0, 4.0, 1.0);
    glm.set_title("GLM-5.3-Flash workers");
    glm.set_value(s.num("fleet_glm_workers", 4.0, 0.0, 4.0));
    let cap = adw::SpinRow::with_range(0.05, 100.0, 0.05);
    cap.set_title("Spending cap per job ($)");
    cap.set_digits(2);
    cap.set_value(s.num("fleet_job_usd", 2.0, 0.01, 1000.0));
    fleet.add(&ds);
    fleet.add(&glm);
    fleet.add(&cap);
    page.add(&fleet);

    let reason = adw::PreferencesGroup::builder().title("Reasoning").build();
    let levels = [("Off — answer straight away", 0.0), ("Light (1k tokens)", 1024.0), ("Normal (4k)", 4096.0),
                  ("Deep (8k)", 8192.0), ("Deeper (16k)", 16384.0), ("Max (32k)", 32768.0)];
    let names: Vec<&str> = levels.iter().map(|(n, _)| *n).collect();
    let rz = adw::ComboRow::builder().title("Max reasoning").model(&gtk::StringList::new(&names)).build();
    let cur = s.num("reasoning_budget", 4096.0, 0.0, 32768.0);
    rz.set_selected(levels.iter().position(|(_, v)| *v >= cur).unwrap_or(2) as u32);
    let think = adw::SpinRow::with_range(0.0, 1800.0, 10.0);
    think.set_title("Max thinking time (s)");
    think.set_value(s.num("max_think_seconds", 120.0, 0.0, 1800.0));
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
    let modes = [("full", "Full access — commands run as you"), ("sandbox-net", "Sandboxed — only this folder is writable"),
                 ("sandbox", "Sandboxed, offline — no network"), ("readonly", "Read-only — nothing is changed")];
    let mnames: Vec<&str> = modes.iter().map(|(_, n)| *n).collect();
    let acc = adw::ComboRow::builder().title("Access").model(&gtk::StringList::new(&mnames)).build();
    let current = folder.as_ref().map(|f| setup::access_of(&s, f)).unwrap_or(chuck_sandbox::Access::Full);
    acc.set_selected(match current {
        chuck_sandbox::Access::Full => 0,
        chuck_sandbox::Access::SandboxNet => 1,
        chuck_sandbox::Access::Sandbox => 2,
        chuck_sandbox::Access::ReadOnly => 3,
    });
    acc.set_sensitive(folder.is_some());
    access_group.add(&acc);
    page.add(&access_group);

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
        s.set("fleet_deepseek_workers", (ds.value() as i64).into());
        s.set("fleet_glm_workers", (glm.value() as i64).into());
        s.set("fleet_job_usd", cap.value().into());
        s.set("reasoning_budget", (levels[rz.selected() as usize].1 as i64).into());
        s.set("max_think_seconds", (think.value() as i64).into());
        s.set("stall_seconds", (stall.value() as i64).into());
        s.set("budget_day_usd", day.value().into());
        s.set("budget_month_usd", month.value().into());
        if let Some(f) = &folder {
            let mode = modes[acc.selected() as usize].0;
            let mut map = s.map.get("folder_access").and_then(|v| v.as_object()).cloned().unwrap_or_default();
            if mode == "full" {
                map.remove(&f.display().to_string());
            } else if mode.starts_with("sandbox") && chuck_sandbox::bwrap().is_none() {
                // never claim a jail that isn't there
            } else {
                map.insert(f.display().to_string(), mode.into());
            }
            s.set("folder_access", serde_json::Value::Object(map));
        }
        let _ = s.save();
        sh.borrow().refresh_header();
    });
    let win = shared.borrow().ui.window.clone();
    dialog.present(Some(&win));
}

fn welcome(shared_slot: Rc<RefCell<Option<Shared>>>) -> gtk::Box {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 14);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Center);
    let mark = label("C", &["mark", "mark-lg"]);
    mark.set_xalign(0.5);
    mark.set_halign(gtk::Align::Center);
    b.append(&mark);
    let hero = label("Tell the fleet what you want.", &["hero"]);
    hero.set_xalign(0.5);
    b.append(&hero);
    let lede = label("A Lead and up to eight workers on SiliconFlow. Criteria first, evidence for every claim, \
                      and nothing is done until the gate says so.", &["lede"]);
    lede.set_xalign(0.5);
    lede.set_justify(gtk::Justification::Center);
    lede.set_max_width_chars(60);
    b.append(&lede);
    let flow = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    flow.set_halign(gtk::Align::Center);
    flow.set_margin_top(10);
    for ex in ["Fix the failing tests", "Add input validation and tests", "Explain how this project is built"] {
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
    b
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

pub fn build(app: &adw::Application, opts: Options) {
    let provider = gtk::CssProvider::new();
    provider.load_from_string(crate::style::CSS);
    if let Some(d) = gdk::Display::default() {
        gtk::style_context_add_provider_for_display(&d, &provider, gtk::STYLE_PROVIDER_PRIORITY_APPLICATION);
    }
    adw::StyleManager::default().set_color_scheme(adw::ColorScheme::ForceDark);

    let window = adw::ApplicationWindow::builder().application(app).title("Chuck")
        .default_width(if opts.width > 0 { opts.width } else { 1180 })
        .default_height(if opts.height > 0 { opts.height } else { 780 }).build();
    window.add_css_class("main-window");
    let slot: Rc<RefCell<Option<Shared>>> = Rc::new(RefCell::new(None));

    // header: the mark, where we are, what it costs, the fleet, settings
    let header = adw::HeaderBar::new();
    header.set_title_widget(Some(&gtk::Box::new(gtk::Orientation::Horizontal, 0)));
    let titlebox = gtk::Box::new(gtk::Orientation::Horizontal, 10);
    let mark = label("C", &["mark"]);
    mark.set_xalign(0.5);
    mark.set_valign(gtk::Align::Center);
    titlebox.append(&mark);
    let tcol = gtk::Box::new(gtk::Orientation::Vertical, 0);
    tcol.append(&label("Chuck", &["title"]));
    let subtitle = label("", &["sub"]);
    subtitle.set_ellipsize(pango::EllipsizeMode::Middle);
    subtitle.set_wrap(false);
    subtitle.set_max_width_chars(60);
    tcol.append(&subtitle);
    titlebox.append(&tcol);
    header.pack_start(&titlebox);
    let folder_btn = gtk::Button::with_label("Choose a folder");
    folder_btn.add_css_class("pill");
    header.pack_start(&folder_btn);
    let gear = gtk::Button::from_icon_name("emblem-system-symbolic");
    gear.add_css_class("headerbtn");
    gear.set_tooltip_text(Some("Settings"));
    header.pack_end(&gear);
    let side_btn = gtk::ToggleButton::builder().icon_name("view-dual-symbolic").active(true).build();
    side_btn.add_css_class("headerbtn");
    side_btn.set_tooltip_text(Some("The fleet and the Done gate"));
    header.pack_end(&side_btn);
    let meter = label("", &["meter"]);
    meter.set_wrap(false);
    header.pack_end(&meter);

    // the conversation
    let transcript = gtk::Box::new(gtk::Orientation::Vertical, 0);
    transcript.set_margin_start(28);
    transcript.set_margin_end(28);
    transcript.set_margin_bottom(18);
    let clamp = adw::Clamp::builder().maximum_size(860).child(&transcript).build();
    let scroller = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).child(&clamp).vexpand(true).build();
    scroller.add_css_class("chat-scroll");
    // follow the conversation while you're at the bottom; stay put when you scrolled up to read
    let stick = Rc::new(std::cell::Cell::new(true));
    let adj = scroller.vadjustment();
    let s1 = stick.clone();
    adj.connect_value_changed(move |a| s1.set(a.value() + a.page_size() >= a.upper() - 40.0));
    let s2 = stick.clone();
    adj.connect_upper_notify(move |a| {
        if s2.get() {
            // after this layout pass, when the page size is final too
            let a = a.clone();
            glib::idle_add_local_once(move || a.set_value(a.upper() - a.page_size()));
        }
    });
    let stack = gtk::Stack::new();
    stack.add_named(&welcome(slot.clone()), Some("welcome"));
    stack.add_named(&scroller, Some("chat"));
    stack.set_vexpand(true);

    // the composer
    let input = gtk::TextView::builder().wrap_mode(gtk::WrapMode::WordChar).accepts_tab(false).hexpand(true)
        .top_margin(4).bottom_margin(4).build();
    let comp = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    comp.add_css_class("composer");
    let insc = gtk::ScrolledWindow::builder().child(&input).hscrollbar_policy(gtk::PolicyType::Never)
        .propagate_natural_height(true).max_content_height(180).hexpand(true).build();
    comp.append(&insc);
    let send = gtk::Button::from_icon_name("go-up-symbolic");
    send.add_css_class("suggested-action");
    send.add_css_class("sendbtn");
    send.set_valign(gtk::Align::End);
    send.set_tooltip_text(Some("Send (Enter) · new line: Shift+Enter"));
    let stop = gtk::Button::from_icon_name("media-playback-stop-symbolic");
    stop.add_css_class("destructive-action");
    stop.add_css_class("sendbtn");
    stop.set_valign(gtk::Align::End);
    stop.set_visible(false);
    stop.set_tooltip_text(Some("Stop every agent (Esc)"));
    comp.append(&send);
    comp.append(&stop);
    let status = label("", &["statusbar"]);
    let bottom = gtk::Box::new(gtk::Orientation::Vertical, 4);
    bottom.set_margin_start(28);
    bottom.set_margin_end(28);
    bottom.set_margin_bottom(14);
    bottom.append(&comp);
    bottom.append(&status);
    let bclamp = adw::Clamp::builder().maximum_size(860).child(&bottom).build();
    let main = gtk::Box::new(gtk::Orientation::Vertical, 0);
    main.append(&stack);
    main.append(&bclamp);

    // the side: the fleet (/top) and the Done gate
    let side = gtk::Box::new(gtk::Orientation::Vertical, 0);
    side.add_css_class("side");
    side.append(&label("FLEET", &["side-head"]));
    let fleet = gtk::ListBox::new();
    fleet.set_selection_mode(gtk::SelectionMode::None);
    fleet.add_css_class("navigation-sidebar");
    fleet.append(&label("Agents appear here while a job runs.", &["unmet"]));
    side.append(&fleet);
    side.append(&label("DONE GATE", &["side-head"]));
    let phase = label("No job yet", &["phase"]);
    side.append(&phase);
    let unmet = gtk::Box::new(gtk::Orientation::Vertical, 0);
    side.append(&unmet);
    let criteria = gtk::Box::new(gtk::Orientation::Vertical, 0);
    side.append(&criteria);
    let side_scroll = gtk::ScrolledWindow::builder().child(&side).hscrollbar_policy(gtk::PolicyType::Never).build();
    let split = adw::OverlaySplitView::builder().sidebar_position(gtk::PackType::End).sidebar(&side_scroll)
        .content(&main).show_sidebar(true).min_sidebar_width(290.0).max_sidebar_width(340.0).build();
    side_btn.bind_property("active", &split, "show-sidebar").bidirectional().build();

    let view = adw::ToolbarView::new();
    view.add_top_bar(&header);
    view.set_content(Some(&split));
    window.set_content(Some(&view));

    let folder = opts.folder.clone().or_else(|| {
        std::env::current_dir().ok().filter(|d| {
            let home = std::env::var_os("HOME").map(PathBuf::from);
            Some(d) != home.as_ref() && d != std::path::Path::new("/")
        })
    }).and_then(|f| f.canonicalize().ok());
    let ui = Ui { window: window.clone(), stack, transcript, input: input.clone(), send: send.clone(),
                  stop: stop.clone(), status, subtitle, meter, fleet, phase, unmet, criteria, folder_btn: folder_btn.clone() };
    let shared: Shared = Rc::new(RefCell::new(State {
        ui, opts: opts.clone(), folder, history: Vec::new(), running: None, names: HashMap::new(),
        models: HashMap::new(), lane: None,
        spent: 0.0, tokens: 0, last_lead_text: String::new(),
    }));
    *slot.borrow_mut() = Some(shared.clone());
    shared.borrow().refresh_header();

    let submit = {
        let sh = shared.clone();
        move || {
            let buf = sh.borrow().ui.input.buffer();
            let text = buf.text(&buf.start_iter(), &buf.end_iter(), false).to_string();
            if sh.borrow().running.is_some() || text.trim().is_empty() {
                return;
            }
            buf.set_text("");
            start(&sh, text.trim().to_string());
        }
    };
    let s1 = submit.clone();
    send.connect_clicked(move |_| s1());
    let sh = shared.clone();
    stop.connect_clicked(move |_| {
        if let Some(r) = &sh.borrow().running {
            r.stop.store(true, Ordering::SeqCst);
        }
    });
    let keys = gtk::EventControllerKey::new();
    let (s2, sh2) = (submit.clone(), shared.clone());
    keys.connect_key_pressed(move |_, key, _, mods| {
        if (key == gdk::Key::Return || key == gdk::Key::KP_Enter) && !mods.contains(gdk::ModifierType::SHIFT_MASK) {
            s2();
            return glib::Propagation::Stop;
        }
        if key == gdk::Key::Escape {
            if let Some(r) = &sh2.borrow().running {
                r.stop.store(true, Ordering::SeqCst);
            }
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    input.add_controller(keys);
    let sh = shared.clone();
    gear.connect_clicked(move |_| settings_dialog(&sh));
    let sh = shared.clone();
    folder_btn.connect_clicked(move |_| {
        let dialog = gtk::FileDialog::builder().title("The project folder").modal(true).build();
        let sh = sh.clone();
        let win = sh.borrow().ui.window.clone();
        dialog.select_folder(Some(&win), gtk::gio::Cancellable::NONE, move |r| {
            if let Ok(f) = r {
                if let Some(p) = f.path() {
                    let mut st = sh.borrow_mut();
                    st.folder = p.canonicalize().ok();
                    st.refresh_header();
                }
            }
        });
    });
    let sh = shared.clone();
    glib::timeout_add_local(Duration::from_millis(60), move || {
        pump(&sh);
        glib::ControlFlow::Continue
    });

    // tools/bench.sh: report the first painted frame (against the shell's clock) and memory, then quit
    if let Ok(t0) = std::env::var("CHUCK_BENCH_FRAME") {
        let app = app.clone();
        window.connect_map(move |w| {
            let (app, t0) = (app.clone(), t0.clone());
            if let Some(clock) = w.frame_clock() {
                let fired = Rc::new(std::cell::Cell::new(false));
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
    if let Some(text) = opts.send.clone() {
        let sh = shared.clone();
        glib::timeout_add_local_once(Duration::from_millis(150), move || start(&sh, text));
    }
    if let Some((path, ms)) = opts.shot.clone() {
        let w = window.clone();
        let app = app.clone();
        glib::timeout_add_local_once(Duration::from_millis(ms), move || {
            match save_shot(&w, &path) {
                Ok(()) => eprintln!("saved {}", path.display()),
                Err(e) => eprintln!("screenshot failed: {e}"),
            }
            app.quit();
        });
    }
}
