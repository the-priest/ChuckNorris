//! The right side: everything the fleet is doing, in sections you open when
//! you want them. Each section shows a one-line summary while it's closed
//! (the plan also keeps its progress bar and current step in view); click a
//! header to see the detail. What you opened is remembered.

use crate::md::escape;
use chuck_core::{AgentId, ProcInfo, ProcState, Role};
use chuck_model::{fmt_tokens, fmt_usd};
use chuck_runtime::TaskView;
use gtk::prelude::*;
use gtk::pango;
use std::collections::{HashMap, VecDeque};
use std::rc::Rc;
use std::time::Instant;

const LOG_KEEP: usize = 200;
const RECENT_KEEP: usize = 60;
const TAIL_BYTES: usize = 6_000;

fn label(text: &str, classes: &[&str]) -> gtk::Label {
    let l = gtk::Label::builder().label(text).xalign(0.0).wrap(true).wrap_mode(pango::WrapMode::WordChar).build();
    for c in classes {
        l.add_css_class(c);
    }
    l
}

fn mono(text: &str) -> gtk::Label {
    let l = label(text, &["panel-mono"]);
    l.set_selectable(true);
    l
}

fn one_line(s: &str, n: usize) -> String {
    let flat: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= n { flat } else { format!("{}…", flat.chars().take(n.saturating_sub(1)).collect::<String>()) }
}

fn tail(s: &str, n: usize) -> String {
    if s.len() <= n {
        return s.to_string();
    }
    let mut at = s.len() - n;
    while !s.is_char_boundary(at) {
        at += 1;
    }
    format!("…{}", &s[at..])
}

fn clear(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

fn ago(t: Instant) -> String {
    let s = t.elapsed().as_secs();
    if s < 60 { format!("{s}s") } else { format!("{}m{:02}s", s / 60, s % 60) }
}

/// A collapsible section: a header you click, a summary that's always
/// there, an optional always-visible strip, and the detail.
pub struct Section {
    pub root: gtk::Box,
    pub summary: gtk::Label,
    pub strip: gtk::Box,
    pub body: gtk::Box,
    revealer: gtk::Revealer,
}

impl Section {
    pub fn new(name: &'static str, title: &str, open: bool, on_toggle: Rc<dyn Fn(&'static str, bool)>) -> Section {
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        root.add_css_class("sec");
        let head = gtk::Button::new();
        head.add_css_class("sec-head");
        head.add_css_class("flat");
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let chevron = gtk::Image::from_icon_name(if open { "chuck-down-symbolic" } else { "chuck-right-symbolic" });
        chevron.add_css_class("sec-chevron");
        row.append(&chevron);
        let t = label(title, &["sec-title"]);
        t.set_wrap(false);
        row.append(&t);
        let summary = label("", &["sec-summary"]);
        summary.set_wrap(false);
        summary.set_hexpand(true);
        summary.set_xalign(1.0);
        summary.set_ellipsize(pango::EllipsizeMode::End);
        row.append(&summary);
        head.set_child(Some(&row));
        root.append(&head);
        let strip = gtk::Box::new(gtk::Orientation::Vertical, 4);
        strip.add_css_class("sec-strip");
        strip.set_visible(false);
        root.append(&strip);
        let body = gtk::Box::new(gtk::Orientation::Vertical, 2);
        body.add_css_class("sec-body");
        let revealer = gtk::Revealer::builder().transition_type(gtk::RevealerTransitionType::SlideDown)
            .transition_duration(150).reveal_child(open).child(&body).build();
        root.append(&revealer);
        let r = revealer.clone();
        head.connect_clicked(move |_| {
            let open = !r.reveals_child();
            r.set_reveal_child(open);
            chevron.set_icon_name(Some(if open { "chuck-down-symbolic" } else { "chuck-right-symbolic" }));
            on_toggle(name, open);
        });
        Section { root, summary, strip, body, revealer }
    }

    pub fn open(&self) -> bool {
        self.revealer.reveals_child()
    }
}

#[derive(Clone)]
struct Cmd {
    who: String,
    tool: String,
    cmd: String,
    started: Instant,
    out: String,
    result: Option<(bool, String)>,
}

/// One row of a list that expands to show more.
fn expander(head: &gtk::Widget, child: &gtk::Widget) -> gtk::Expander {
    let e = gtk::Expander::new(None);
    e.set_label_widget(Some(head));
    e.set_child(Some(child));
    e.add_css_class("panel-row");
    e
}

pub struct Panel {
    pub root: gtk::ScrolledWindow,
    plan: Section,
    plan_bar: gtk::ProgressBar,
    plan_now: gtk::Label,
    tasks: Section,
    team: Section,
    team_posts: usize,
    agents: Section,
    commands: Section,
    running_box: gtk::Box,
    recent_box: gtk::Box,
    research: Section,
    searches: usize,
    pages: usize,
    gate: Section,
    gate_phase: gtk::Label,
    gate_unmet: gtk::Box,
    gate_criteria: gtk::Box,
    spend: Section,
    pub checkpoints: Section,
    inbox: Section,
    daemons: Section,
    memory: Section,
    // what's shown
    steps: Vec<(String, bool)>,
    finished: bool,
    logs: HashMap<String, VecDeque<String>>,
    running: HashMap<AgentId, Cmd>,
    recent: VecDeque<Cmd>,
    recent_dirty: bool,
    agents_sig: String,
}

impl Panel {
    pub fn new(open: &[String], on_toggle: Rc<dyn Fn(&'static str, bool)>) -> Panel {
        let is = |n: &str| open.iter().any(|o| o == n);
        let side = gtk::Box::new(gtk::Orientation::Vertical, 0);
        side.add_css_class("side");
        let mk = |n: &'static str, t: &str| Section::new(n, t, is(n), on_toggle.clone());

        let plan = mk("plan", "PLAN");
        let plan_bar = gtk::ProgressBar::new();
        plan_bar.add_css_class("plan-bar");
        let plan_now = label("", &["plan-now"]);
        plan.strip.append(&plan_bar);
        plan.strip.append(&plan_now);
        plan.body.append(&label("The Lead writes a plan for larger work; its progress shows here.", &["panel-dim"]));

        let tasks = mk("tasks", "TASKS");
        tasks.body.append(&label("Work the Lead hands to the fleet appears here.", &["panel-dim"]));
        let team = mk("team", "TEAM TALK");
        team.body.append(&label("What the workers and the Lead say to each other appears here.", &["panel-dim"]));
        let agents = mk("agents", "AGENTS");
        agents.body.append(&label("Agents appear here while a job runs.", &["panel-dim"]));

        let commands = mk("commands", "COMMANDS");
        let running_box = gtk::Box::new(gtk::Orientation::Vertical, 2);
        let recent_box = gtk::Box::new(gtk::Orientation::Vertical, 2);
        commands.body.append(&label("RUNNING NOW", &["panel-sub"]));
        commands.body.append(&running_box);
        commands.body.append(&label("RECENT", &["panel-sub"]));
        commands.body.append(&recent_box);
        running_box.append(&label("nothing running", &["panel-dim"]));

        let research = mk("research", "RESEARCH");
        research.body.append(&label("Searches and the pages agents read appear here, with links.", &["panel-dim"]));

        let gate = mk("gate", "DONE GATE");
        let gate_phase = label("No job yet", &["gate-phase"]);
        let gate_unmet = gtk::Box::new(gtk::Orientation::Vertical, 1);
        let gate_criteria = gtk::Box::new(gtk::Orientation::Vertical, 4);
        gate.body.append(&gate_phase);
        gate.body.append(&gate_unmet);
        gate.body.append(&gate_criteria);

        let spend = mk("spend", "SPEND");
        let checkpoints = mk("checkpoints", "CHECKPOINTS");
        let inbox = mk("inbox", "INBOX");
        let daemons = mk("daemons", "DAEMONS & REMINDERS");
        let memory = mk("memory", "MEMORY");

        for s in [&plan, &tasks, &team, &agents, &commands, &research, &gate, &spend, &checkpoints, &inbox, &daemons, &memory] {
            side.append(&s.root);
        }
        let root = gtk::ScrolledWindow::builder().child(&side).hscrollbar_policy(gtk::PolicyType::Never).build();
        let p = Panel { root, plan, plan_bar, plan_now, tasks, team, team_posts: 0, agents, commands, running_box, recent_box,
                        research, searches: 0, pages: 0, gate,
                        gate_phase, gate_unmet, gate_criteria, spend, checkpoints, inbox, daemons, memory, steps: Vec::new(), finished: false,
                        logs: HashMap::new(), running: HashMap::new(), recent: VecDeque::new(), recent_dirty: false,
                        agents_sig: String::new() };
        p.plan.summary.set_label("no plan yet");
        p.tasks.summary.set_label("none");
        p.team.summary.set_label("quiet");
        p.agents.summary.set_label("idle");
        p.commands.summary.set_label("none yet");
        p.research.summary.set_label("none");
        p.gate.summary.set_label("no job");
        p
    }

    /// A new job: what belonged to the last one goes.
    pub fn reset(&mut self) {
        self.steps.clear();
        self.finished = false;
        self.logs.clear();
        self.running.clear();
        self.recent.clear();
        self.recent_dirty = true;
        self.agents_sig.clear();
        self.set_plan(Vec::new());
        self.set_tasks(&[]);
        clear(&self.team.body);
        self.team_posts = 0;
        self.team.summary.set_label("quiet");
        self.team.body.append(&label("What the workers and the Lead say to each other appears here.", &["panel-dim"]));
        clear(&self.research.body);
        (self.searches, self.pages) = (0, 0);
        self.research.summary.set_label("none");
        clear(&self.gate_unmet);
        clear(&self.gate_criteria);
        self.gate_phase.set_label("Phase: intake");
        self.gate.summary.set_label("intake");
    }

    // ── plan ──

    pub fn set_plan(&mut self, steps: Vec<(String, bool)>) {
        self.steps = steps;
        self.draw_plan();
    }

    /// Show where a job got to (a conversation opened again).
    pub fn restore(&mut self, p: &crate::chat::Progress) {
        self.steps = p.plan.clone();
        self.finished = p.finished;
        self.draw_plan();
        let tasks: Vec<TaskView> = p.tasks.iter().map(|t| TaskView { id: t.id.clone(), title: t.title.clone(),
            state: t.state.clone(), workers: t.workers.clone(), deps: t.deps.clone(), criteria: t.criteria.clone(),
            note: t.note.clone() }).collect();
        self.set_tasks(&tasks);
        if !p.phase.is_empty() {
            self.set_gate(&p.phase, &p.unmet);
        }
        self.set_board(&p.board);
    }

    pub fn job_finished(&mut self, done: bool) {
        self.finished = done;
        self.draw_plan();
        clear(&self.running_box);
        self.running_box.append(&label("nothing running", &["panel-dim"]));
        self.running.clear();
    }

    fn draw_plan(&self) {
        clear(&self.plan.body);
        let n = self.steps.len();
        if n == 0 {
            self.plan.summary.set_label("no plan yet");
            self.plan.strip.set_visible(false);
            self.plan.body.append(&label("The Lead writes a plan for larger work; its progress shows here.", &["panel-dim"]));
            return;
        }
        let done_flags: Vec<bool> = self.steps.iter().map(|(_, d)| *d || self.finished).collect();
        let done = done_flags.iter().filter(|d| **d).count();
        let current = done_flags.iter().position(|d| !d);
        self.plan.summary.set_label(&format!("{done} / {n} steps"));
        self.plan.strip.set_visible(true);
        self.plan_bar.set_fraction(done as f64 / n as f64);
        self.plan_now.set_label(&match current {
            Some(i) => format!("Now: {}", one_line(&self.steps[i].0, 80)),
            None => "Every step done".into(),
        });
        for (i, (step, _)) in self.steps.iter().enumerate() {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            row.add_css_class("plan-step");
            let (mark, class) = if done_flags[i] {
                ("✓", "step-done")
            } else if Some(i) == current {
                ("▶", "step-now")
            } else {
                ("○", "step-todo")
            };
            let m = label(mark, &["step-mark", class]);
            m.set_valign(gtk::Align::Start);
            row.append(&m);
            let t = label(step, &["step-text", class]);
            t.set_hexpand(true);
            row.append(&t);
            self.plan.body.append(&row);
        }
    }

    // ── tasks ──

    pub fn set_tasks(&self, tasks: &[TaskView]) {
        clear(&self.tasks.body);
        if tasks.is_empty() {
            self.tasks.summary.set_label("none");
            self.tasks.body.append(&label("Work the Lead hands to the fleet appears here.", &["panel-dim"]));
            return;
        }
        let count = |s: &str| tasks.iter().filter(|t| t.state == s).count();
        let mut parts = Vec::new();
        for (s, word) in [("running", "running"), ("review", "in review"), ("queued", "queued"), ("merged", "merged"),
                          ("failed", "failed")] {
            let c = count(s);
            if c > 0 {
                parts.push(format!("{c} {word}"));
            }
        }
        self.tasks.summary.set_label(&parts.join(" · "));
        for t in tasks {
            let head = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            head.append(&label(&t.id, &["task-id"]));
            let title = label(&t.title, &["task-title"]);
            title.set_hexpand(true);
            title.set_wrap(false);
            title.set_ellipsize(pango::EllipsizeMode::End);
            head.append(&title);
            let badge = label(&t.state, &["badge", &format!("badge-{}", t.state)]);
            badge.set_wrap(false);
            head.append(&badge);
            let body = gtk::Box::new(gtk::Orientation::Vertical, 3);
            body.add_css_class("panel-detail");
            if !t.workers.is_empty() {
                body.append(&label(&format!("worker: {}", t.workers.join(" vs ")), &["panel-meta"]));
            }
            if !t.deps.is_empty() {
                body.append(&label(&format!("after: {}", t.deps.join(", ")), &["panel-meta"]));
            }
            for (i, c) in t.criteria.iter().enumerate() {
                body.append(&label(&format!("c{}  {c}", i + 1), &["panel-text"]));
            }
            if !t.note.is_empty() {
                body.append(&label(&t.note, &["panel-warn"]));
            }
            self.tasks.body.append(&expander(head.upcast_ref(), body.upcast_ref()));
        }
    }

    // ── the team's talk ──

    /// A message on the team channel: who to whom, and what.
    pub fn team_post(&mut self, from: &str, to: Option<&str>, text: &str) {
        if self.team_posts == 0 {
            clear(&self.team.body);
        }
        self.team_posts += 1;
        let head = label("", &["team-head"]);
        head.set_markup(&format!("<b>{}</b> <span foreground=\"#5F5F6E\">→ {}</span>", escape(from),
                                 escape(to.unwrap_or("everyone"))));
        head.set_wrap(false);
        self.team.body.append(&head);
        self.team.body.append(&label(&one_line(text, 400), &["panel-text"]));
        while self.team.body.observe_children().n_items() > 160 {
            if let Some(first) = self.team.body.first_child() {
                self.team.body.remove(&first);
            }
        }
        self.team.summary.set_label(&format!("{} message{} · last: {from}", self.team_posts,
                                             if self.team_posts == 1 { "" } else { "s" }));
    }

    // ── research ──

    /// A search or a page read: who, what, how it went. Pages link out.
    pub fn research(&mut self, who: &str, tool: &str, what: &str, ok: bool, summary: &str) {
        if self.searches + self.pages == 0 {
            clear(&self.research.body);
        }
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        row.add_css_class("panel-row");
        let mark = gtk::Image::from_icon_name(match tool {
            "search" => "chuck-search-symbolic",
            "images" => "chuck-image-symbolic",
            "videos" => "chuck-video-symbolic",
            _ => "chuck-page-symbolic",
        });
        mark.add_css_class(if ok { "research-ok" } else { "research-fail" });
        mark.set_valign(gtk::Align::Start);
        mark.set_margin_top(2);
        row.append(&mark);
        let text = gtk::Box::new(gtk::Orientation::Vertical, 1);
        text.set_hexpand(true);
        let url = what.split_whitespace().next().unwrap_or("");
        let head = if tool != "fetch" {
            self.searches += 1;
            format!("<b>{}</b>{}", escape(what), if tool == "search" { String::new() } else { format!(" · {tool}") })
        } else {
            self.pages += 1;
            let shown = if summary.is_empty() || !ok { url.to_string() } else { summary.to_string() };
            if ok && (url.starts_with("http://") || url.starts_with("https://")) {
                format!("<a href=\"{}\">{}</a>", escape(url), escape(&shown))
            } else {
                escape(&shown)
            }
        };
        let h = label("", &["research-head"]);
        h.set_markup(&head);
        h.set_ellipsize(pango::EllipsizeMode::End);
        h.set_wrap(false);
        h.set_tooltip_text(Some(url));
        text.append(&h);
        let sub = if tool != "fetch" { format!("{who} · {summary}") } else if ok { format!("{who} · {url}") }
            else { format!("{who} · {summary}") };
        let d = label(&sub, &["panel-dim"]);
        d.set_ellipsize(pango::EllipsizeMode::End);
        d.set_wrap(false);
        text.append(&d);
        row.append(&text);
        self.research.body.append(&row);
        while self.research.body.observe_children().n_items() > 80 {
            if let Some(first) = self.research.body.first_child() {
                self.research.body.remove(&first);
            }
        }
        let (s, p) = (self.searches, self.pages);
        self.research.summary.set_label(&format!("{s} search{} · {p} page{}", if s == 1 { "" } else { "es" },
                                                 if p == 1 { "" } else { "s" }));
    }

    // ── agents ──

    /// A line of what an agent did, for its own history.
    pub fn log(&mut self, who: &str, line: String) {
        let l = self.logs.entry(who.to_string()).or_default();
        l.push_back(line);
        while l.len() > LOG_KEEP {
            l.pop_front();
        }
        self.agents_sig.clear(); // redraw on the next tick
    }

    /// The agents at work now, each with what it is doing; the ones that
    /// finished are a count (their work shows in the tasks and the log).
    pub fn set_agents(&mut self, ps: &[ProcInfo], models: &HashMap<AgentId, String>) {
        let usd: f64 = ps.iter().filter(|p| p.ppid.is_none()).map(|p| p.usd).sum::<f64>().max(0.0);
        let (active, ended): (Vec<&ProcInfo>, Vec<&ProcInfo>) = ps.iter().partition(|p| p.state.alive());
        self.agents.summary.set_label(&if ps.is_empty() { "idle".to_string() } else if active.is_empty() {
            format!("none active · {} finished · {}", ended.len(), fmt_usd(usd))
        } else {
            format!("{} active · {} finished · {}", active.len(), ended.len(), fmt_usd(usd))
        });
        // only rebuild when something visible changed (and only while open)
        let sig: String = active.iter().map(|p| format!("{}{:?}{}{}{}", p.pid, p.state, p.activity, p.tokens,
            self.logs.get(&p.name).map_or(0, |l| l.len()))).chain(std::iter::once(ended.len().to_string())).collect();
        if !self.agents.open() || sig == self.agents_sig {
            if !self.agents.open() {
                self.agents_sig.clear();
            }
            return;
        }
        self.agents_sig = sig;
        clear(&self.agents.body);
        if active.is_empty() {
            self.agents.body.append(&label(if ps.is_empty() { "Agents appear here while a job runs." } else {
                "Nobody is working right now." }, &["panel-dim"]));
        }
        for p in active {
            let head = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            let dot = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            dot.add_css_class("dot");
            dot.add_css_class(match p.state {
                ProcState::Running => "dot-run",
                ProcState::Waiting | ProcState::Ready | ProcState::Stopped => "dot-wait",
                ProcState::Done => "dot-done",
                ProcState::Failed | ProcState::Killed => "dot-fail",
            });
            dot.set_valign(gtk::Align::Center);
            head.append(&dot);
            let col = gtk::Box::new(gtk::Orientation::Vertical, 0);
            col.set_hexpand(true);
            let top = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            top.append(&label(&p.name, &["agent-name"]));
            let model = models.get(&p.pid).cloned().unwrap_or_else(|| match p.role {
                Role::GlmWorker => "GLM".into(),
                _ => "DeepSeek".into(),
            });
            let m = label(&model, &["panel-meta"]);
            m.set_wrap(false);
            m.set_ellipsize(pango::EllipsizeMode::End);
            top.append(&m);
            col.append(&top);
            let doing = if p.activity.is_empty() { p.state.label().to_string() } else { p.activity.clone() };
            // a parent is charged what its children spend: its own share, then the team's
            let kids: (u64, f64) = ps.iter().filter(|c| c.ppid == Some(p.pid))
                .fold((0, 0.0), |(t, u), c| (t + c.tokens, u + c.usd));
            let spent = if kids.1 > 0.0 || kids.0 > 0 {
                format!("own {} · {} · team {}", fmt_tokens(p.tokens.saturating_sub(kids.0)),
                        fmt_usd((p.usd - kids.1).max(0.0)), fmt_usd(p.usd))
            } else {
                format!("{} · {}", fmt_tokens(p.tokens), fmt_usd(p.usd))
            };
            col.append(&label(&format!("{} · {spent}", one_line(&doing, 40)), &["panel-meta"]));
            if !p.brief.is_empty() && p.role != Role::Lead {
                col.append(&label(&one_line(&p.brief, 48), &["panel-meta"]));
            }
            // what it did last: the line that says what it is on
            if let Some(last) = self.logs.get(&p.name).and_then(|l| l.back()) {
                let l = label(&one_line(last, 60), &["panel-dim"]);
                l.set_wrap(false);
                l.set_ellipsize(pango::EllipsizeMode::End);
                col.append(&l);
            }
            head.append(&col);
            let body = gtk::Box::new(gtk::Orientation::Vertical, 1);
            body.add_css_class("panel-detail");
            match self.logs.get(&p.name) {
                Some(l) if !l.is_empty() => {
                    let shown: Vec<&String> = l.iter().rev().take(40).collect();
                    if l.len() > shown.len() {
                        body.append(&label(&format!("{} earlier lines", l.len() - shown.len()), &["panel-dim"]));
                    }
                    for line in shown.into_iter().rev() {
                        body.append(&mono(line));
                    }
                }
                _ => body.append(&label("nothing yet", &["panel-dim"])),
            }
            self.agents.body.append(&expander(head.upcast_ref(), body.upcast_ref()));
        }
        if !ended.is_empty() {
            // "18 finished: eyes ×14, ds-1, ds-2, critic …" — counted by name
            let mut by: Vec<(String, usize)> = Vec::new();
            for p in &ended {
                match by.iter_mut().find(|(n, _)| *n == p.name) {
                    Some((_, c)) => *c += 1,
                    None => by.push((p.name.clone(), 1)),
                }
            }
            let names: Vec<String> = by.iter().map(|(n, c)| if *c > 1 { format!("{n} ×{c}") } else { n.clone() }).collect();
            let spent: f64 = ended.iter().map(|p| p.usd).sum();
            self.agents.body.append(&label(&format!("{} finished ({}): {}", ended.len(), fmt_usd(spent),
                                                    one_line(&names.join(", "), 200)), &["panel-dim"]));
        }
    }

    // ── commands ──

    pub fn command_started(&mut self, id: AgentId, who: &str, tool: &str, cmd: &str) {
        self.running.insert(id, Cmd { who: who.into(), tool: tool.into(), cmd: cmd.into(), started: Instant::now(),
                                      out: String::new(), result: None });
        self.draw_running();
    }

    pub fn output(&mut self, id: AgentId, chunk: &str) {
        if let Some(c) = self.running.get_mut(&id) {
            c.out.push_str(chunk);
            if c.out.len() > TAIL_BYTES * 2 {
                c.out = tail(&c.out, TAIL_BYTES);
            }
        }
    }

    pub fn command_finished(&mut self, id: AgentId, ok: bool, summary: &str) {
        if let Some(mut c) = self.running.remove(&id) {
            c.result = Some((ok, format!("{summary} · {}", ago(c.started))));
            self.recent.push_front(c);
            while self.recent.len() > RECENT_KEEP {
                self.recent.pop_back();
            }
            self.recent_dirty = true;
        }
        self.draw_running();
    }

    fn draw_running(&self) {
        clear(&self.running_box);
        if self.running.is_empty() {
            self.running_box.append(&label("nothing running", &["panel-dim"]));
        }
        let mut cmds: Vec<&Cmd> = self.running.values().collect();
        cmds.sort_by_key(|c| c.started);
        for c in cmds {
            let head = label(&format!("⏵ {}  {}  {}", c.who, ago(c.started), one_line(&c.cmd, 60)), &["cmd-live"]);
            let body = mono(if c.out.trim().is_empty() { "(no output yet)" } else { c.out.trim_end() });
            self.running_box.append(&expander(head.upcast_ref(), body.upcast_ref()));
        }
    }

    /// Once a second: elapsed times, the running list, the summary.
    pub fn tick(&mut self) {
        let total = self.recent.len() + self.running.len();
        self.commands.summary.set_label(&match (self.running.len(), total) {
            (_, 0) => "none yet".to_string(),
            (0, t) => format!("{t} run"),
            (r, t) => format!("{r} running · {t} run"),
        });
        if !self.commands.open() {
            return;
        }
        if !self.running.is_empty() {
            self.draw_running();
        }
        if self.recent_dirty {
            self.recent_dirty = false;
            clear(&self.recent_box);
            if self.recent.is_empty() {
                self.recent_box.append(&label("none yet", &["panel-dim"]));
            }
            for c in &self.recent {
                let (ok, sum) = c.result.clone().unwrap_or((true, String::new()));
                let mark = if ok { "<span foreground=\"#4ADE9A\">✓</span>" } else { "<span foreground=\"#F87171\">✗</span>" };
                let head = label("", &["cmd-done"]);
                head.set_markup(&format!("{mark} {} <span foreground=\"#5F5F6E\">{}</span>  {}",
                                         escape(&c.who), escape(&c.tool), escape(&one_line(&c.cmd, 54))));
                let body = gtk::Box::new(gtk::Orientation::Vertical, 2);
                body.add_css_class("panel-detail");
                body.append(&mono(&c.cmd));
                body.append(&label(&sum, &["panel-meta"]));
                body.append(&mono(if c.out.trim().is_empty() { "(no output)" } else { c.out.trim_end() }));
                self.recent_box.append(&expander(head.upcast_ref(), body.upcast_ref()));
            }
        }
    }

    // ── the Done gate ──

    pub fn set_gate(&self, phase: &str, unmet: &[String]) {
        self.gate_phase.set_label(&format!("Phase: {phase}"));
        self.gate_phase.remove_css_class("phase-incomplete");
        self.gate_phase.remove_css_class("phase-done");
        match phase {
            "INCOMPLETE" => self.gate_phase.add_css_class("phase-incomplete"),
            "done" => self.gate_phase.add_css_class("phase-done"),
            _ => {}
        }
        self.gate.summary.set_label(&if unmet.is_empty() { phase.to_string() } else {
            format!("{phase} · {} open", unmet.len())
        });
        clear(&self.gate_unmet);
        for u in unmet.iter().take(10) {
            self.gate_unmet.append(&label(&format!("· {u}"), &["panel-warn"]));
        }
    }

    pub fn set_board(&self, criteria: &[(String, String, Option<String>, Vec<String>)]) {
        clear(&self.gate_criteria);
        for (id, text, verify, ev) in criteria {
            let head = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            head.append(&label(id, &["crit-id"]));
            let t = label(text, &["panel-text"]);
            t.set_hexpand(true);
            head.append(&t);
            let proven = ev.iter().any(|e| e.contains("pass"));
            let badge = label(if proven { "evidence" } else if ev.is_empty() { "none" } else { "partial" },
                              &["badge", if proven { "badge-merged" } else { "badge-failed" }]);
            badge.set_wrap(false);
            head.append(&badge);
            let body = gtk::Box::new(gtk::Orientation::Vertical, 2);
            body.add_css_class("panel-detail");
            if let Some(v) = verify {
                body.append(&label(&format!("verify: {v}"), &["panel-meta"]));
            }
            if ev.is_empty() {
                body.append(&label("no evidence yet", &["panel-dim"]));
            }
            for e in ev.iter().rev().take(12) {
                body.append(&mono(e));
            }
            self.gate_criteria.append(&expander(head.upcast_ref(), body.upcast_ref()));
        }
    }

    // ── spend ──

    pub fn set_spend(&self, ps: &[ProcInfo], today: f64, month: f64, day_limit: f64, month_limit: f64,
                     cache: Option<f64>) {
        let job_usd: f64 = ps.iter().filter(|p| p.ppid.is_none()).map(|p| p.usd).sum::<f64>().max(0.0);
        let job_tok: u64 = ps.iter().filter(|p| p.ppid.is_none()).map(|p| p.tokens).sum();
        let lim = |x: f64, l: f64| if l > 0.0 { format!("{} of {}", fmt_usd(x), fmt_usd(l)) } else { fmt_usd(x) };
        self.spend.summary.set_label(&format!("{} this job · {} today", fmt_usd(job_usd), fmt_usd(today)));
        if !self.spend.open() {
            return;
        }
        clear(&self.spend.body);
        self.spend.body.append(&label(&format!("This job: {} · {} tokens", fmt_usd(job_usd), fmt_tokens(job_tok)), &["panel-text"]));
        self.spend.body.append(&label(&format!("Today: {}", lim(today, day_limit)), &["panel-text"]));
        self.spend.body.append(&label(&format!("This month: {}", lim(month, month_limit)), &["panel-text"]));
        if let Some(c) = cache {
            self.spend.body.append(&label(&format!("Prompt cache: {:.0}% of what was sent was billed at the cached \
                price", c * 100.0), &["panel-meta"]));
        }
        if !ps.is_empty() {
            self.spend.body.append(&label("BY AGENT", &["panel-sub"]));
            let mut rows: Vec<&ProcInfo> = ps.iter().collect();
            rows.sort_by(|a, b| b.usd.partial_cmp(&a.usd).unwrap_or(std::cmp::Ordering::Equal));
            for p in rows {
                // own spend: the lead's total includes its children, so subtract them
                let own = if p.ppid.is_none() {
                    p.usd - ps.iter().filter(|c| c.ppid == Some(p.pid)).map(|c| c.usd).sum::<f64>()
                } else {
                    p.usd
                };
                self.spend.body.append(&mono(&format!("{:<8} {:>9}  {} calls", p.name, fmt_usd(own.max(0.0)),
                                                      p.calls)));
            }
        }
    }

    // ── checkpoints ──

    /// Daemon reports, newest first; unread ones marked. `on_read` marks
    /// them all read.
    pub fn set_inbox(&self, items: &[serde_json::Map<String, serde_json::Value>], on_read: Rc<dyn Fn()>) {
        clear(&self.inbox.body);
        let unread = items.iter().filter(|i| i.get("unread").and_then(|v| v.as_bool()) == Some(true)).count();
        self.inbox.summary.set_label(&match (items.len(), unread) {
            (0, _) => "empty".into(),
            (n, 0) => format!("{n} report{}", if n == 1 { "" } else { "s" }),
            (_, u) => format!("{u} new"),
        });
        if unread > 0 {
            self.inbox.summary.add_css_class("badge-new");
        } else {
            self.inbox.summary.remove_css_class("badge-new");
        }
        if items.is_empty() {
            self.inbox.body.append(&label("Daemons' reports and your reminders land here.", &["panel-dim"]));
            return;
        }
        let now = chuck_daemon::now();
        for it in items.iter().rev().take(40) {
            let s = |k: &str| it.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
            let report = s("report");
            let head_line = report.trim().lines().next().unwrap_or("(no report)").to_string();
            let new = it.get("unread").and_then(|v| v.as_bool()) == Some(true);
            let ago = (now - it.get("time").and_then(|v| v.as_f64()).unwrap_or(now)).max(0.0) as u64;
            let when = if ago < 60 { "just now".into() } else if ago < 3600 { format!("{} min ago", ago / 60) }
                else if ago < 86400 { format!("{} h ago", ago / 3600) } else { format!("{} d ago", ago / 86400) };
            let head = gtk::Box::new(gtk::Orientation::Vertical, 0);
            let h = label(&head_line, &[if new { "inbox-new" } else { "panel-text" }]);
            h.set_ellipsize(pango::EllipsizeMode::End);
            h.set_wrap(false);
            head.append(&h);
            head.append(&label(&format!("{} · {} · {when}", s("daemon"), s("why")), &["panel-meta"]));
            let body = label(&report, &["panel-text"]);
            body.set_selectable(true);
            body.set_margin_start(6);
            self.inbox.body.append(&expander(head.upcast_ref(), body.upcast_ref()));
        }
        if unread > 0 {
            let b = gtk::Button::with_label("Mark all read");
            b.add_css_class("pill-sm");
            b.set_halign(gtk::Align::Start);
            b.set_margin_top(6);
            b.connect_clicked(move |_| on_read());
            self.inbox.body.append(&b);
        }
    }

    /// The daemons and reminders: what wakes each, how often it has, and
    /// buttons to pause or remove it.
    pub fn set_daemons(&self, items: &[chuck_daemon::Daemon], live: &[i64], on_toggle: Rc<dyn Fn(i64)>,
                       on_remove: Rc<dyn Fn(i64)>) {
        clear(&self.daemons.body);
        let on = items.iter().filter(|d| d.enabled()).count();
        self.daemons.summary.set_label(&match (items.len(), live.len()) {
            (0, _) => "none".into(),
            (n, 0) => format!("{on} of {n} on"),
            (_, l) => format!("{l} looking now"),
        });
        if items.is_empty() {
            self.daemons.body.append(&label("Type /remind in 20m: check the oven, or /daemon every day at 9: \
                check the disk and the failed services. Or ask in Chat. They run while Chuck is open.", &["panel-dim"]));
            return;
        }
        for d in items {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            row.add_css_class("panel-row");
            let col = gtk::Box::new(gtk::Orientation::Vertical, 0);
            col.set_hexpand(true);
            let name = label(d.name(), &[if d.enabled() { "panel-text" } else { "panel-dim" }]);
            name.set_ellipsize(pango::EllipsizeMode::End);
            name.set_wrap(false);
            name.set_tooltip_text(Some(d.brief()));
            col.append(&name);
            let kind = if d.kind() == "remind" { "reminder" } else { "daemon" };
            let state = if live.contains(&d.id()) { " · looking now".to_string() } else if !d.enabled() { " · paused".into() }
                else if d.runs() > 0 { format!(" · ran {}×", d.runs()) } else { String::new() };
            col.append(&label(&format!("{kind} · {}{state}", chuck_daemon::describe(d.when())), &["panel-meta"]));
            row.append(&col);
            let t = gtk::Button::from_icon_name(if d.enabled() { "chuck-pause-symbolic" } else { "chuck-play-symbolic" });
            t.add_css_class("flat");
            t.set_valign(gtk::Align::Center);
            t.set_tooltip_text(Some(if d.enabled() { "Pause it" } else { "Resume it" }));
            let (id, cb) = (d.id(), on_toggle.clone());
            t.connect_clicked(move |_| cb(id));
            row.append(&t);
            let x = gtk::Button::from_icon_name("chuck-trash-symbolic");
            x.add_css_class("flat");
            x.set_valign(gtk::Align::Center);
            x.set_tooltip_text(Some("Remove it"));
            let (id, cb) = (d.id(), on_remove.clone());
            x.connect_clicked(move |_| cb(id));
            row.append(&x);
            self.daemons.body.append(&row);
        }
    }

    /// What Chuck remembers, newest first, each with a button to forget it.
    /// `folder`: the open project (its own facts are marked).
    pub fn set_memory(&self, facts: &[chuck_store::memory::Fact], folder: &str, on_forget: Rc<dyn Fn(serde_json::Value)>) {
        clear(&self.memory.body);
        let here = facts.iter().filter(|f| f.scope().is_empty() || f.scope() == folder).count();
        self.memory.summary.set_label(&match facts.len() {
            0 => "nothing yet".into(),
            n if here == n => format!("{n} fact{}", if n == 1 { "" } else { "s" }),
            n => format!("{here} here · {n} in all"),
        });
        if facts.is_empty() {
            self.memory.body.append(&label("Facts Chuck keeps for later chats appear here (it says when it \
                remembers something). Remove any you don't want kept.", &["panel-dim"]));
            return;
        }
        for f in facts.iter().take(200) {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
            row.add_css_class("panel-row");
            let col = gtk::Box::new(gtk::Orientation::Vertical, 0);
            col.set_hexpand(true);
            col.append(&label(f.text(), &["panel-text"]));
            let scope = if f.scope().is_empty() { "about you".to_string() } else {
                format!("project · {}", std::path::Path::new(f.scope()).file_name().map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_else(|| f.scope().to_string()))
            };
            col.append(&label(&format!("{}{scope}", if f.kind() == "core" { "always · " } else { "" }), &["panel-meta"]));
            row.append(&col);
            let x = gtk::Button::from_icon_name("chuck-trash-symbolic");
            x.add_css_class("flat");
            x.set_valign(gtk::Align::Center);
            x.set_tooltip_text(Some("Forget this"));
            let (id, cb) = (f.id(), on_forget.clone());
            x.connect_clicked(move |_| cb(id.clone()));
            row.append(&x);
            self.memory.body.append(&row);
        }
    }

    pub fn set_checkpoints(&self, points: &[chuck_store::Point], busy: bool, on_rewind: Rc<dyn Fn(String)>) {
        clear(&self.checkpoints.body);
        self.checkpoints.summary.set_label(&match points.len() {
            0 => "none yet".into(),
            1 => "1".into(),
            n => format!("{n}"),
        });
        if points.is_empty() {
            self.checkpoints.body.append(&label("Chuck takes one before every step that changes your folder.", &["panel-dim"]));
            return;
        }
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
        for p in points.iter().take(15) {
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            row.add_css_class("panel-row");
            let col = gtk::Box::new(gtk::Orientation::Vertical, 0);
            col.set_hexpand(true);
            col.append(&label(p.label.trim_start_matches("chuck: "), &["panel-text"]));
            let s = (now - p.at).max(0);
            let when = if s < 60 { "just now".into() } else if s < 3600 { format!("{} min ago", s / 60) } else {
                format!("{} h ago", s / 3600)
            };
            col.append(&label(&format!("{} · {}", &p.sha[..10.min(p.sha.len())], when), &["panel-meta"]));
            row.append(&col);
            let b = gtk::Button::with_label("Rewind");
            b.add_css_class("pill-sm");
            b.set_valign(gtk::Align::Center);
            b.set_sensitive(!busy);
            b.set_tooltip_text(Some("Put the folder back to this point (undoable)"));
            let (sha, cb) = (p.sha.clone(), on_rewind.clone());
            b.connect_clicked(move |_| cb(sha.clone()));
            row.append(&b);
            self.checkpoints.body.append(&row);
        }
    }
}
