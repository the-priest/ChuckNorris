//! The fleet in a terminal: what the Lead says, what each agent does, the
//! gate, and an honest ending (done, or INCOMPLETE with what's missing).

use chuck_core::{AgentId, Role};
use chuck_model::{fmt_tokens, fmt_usd};
use chuck_runtime::{Event, Outcome};
use std::collections::HashMap;
use std::io::Write;

pub struct Style {
    pub color: bool,
}

impl Style {
    fn paint(&self, code: &str, s: &str) -> String {
        if self.color { format!("\x1b[{code}m{s}\x1b[0m") } else { s.to_string() }
    }
    pub fn dim(&self, s: &str) -> String {
        self.paint("38;5;245", s)
    }
    pub fn bold(&self, s: &str) -> String {
        self.paint("1", s)
    }
    pub fn good(&self, s: &str) -> String {
        self.paint("38;5;42", s)
    }
    pub fn bad(&self, s: &str) -> String {
        self.paint("38;5;203", s)
    }
    pub fn accent(&self, s: &str) -> String {
        self.paint("38;5;141", s)
    }
    pub fn warn(&self, s: &str) -> String {
        self.paint("38;5;178", s)
    }
}

pub struct View {
    pub st: Style,
    pub verbose: bool,
    names: HashMap<AgentId, (String, Role)>,
    tokens: u64,
    usd: f64,
    prompt: u64,
    cached: u64,
    last_gate: Option<(String, usize)>,
    out: Box<dyn Write>,
}

fn one_line(s: &str, n: usize) -> String {
    let flat: String = s.split_whitespace().collect::<Vec<_>>().join(" ");
    if flat.chars().count() <= n { flat } else { format!("{}…", flat.chars().take(n - 1).collect::<String>()) }
}

impl View {
    pub fn new(color: bool, verbose: bool, out: Box<dyn Write>) -> View {
        View { st: Style { color }, verbose, names: HashMap::new(), tokens: 0, usd: 0.0, prompt: 0, cached: 0, last_gate: None, out }
    }

    pub fn header(&mut self, text: &str) {
        let line = format!("{} {}", self.st.accent(&self.st.bold("chuck")), self.st.dim(text));
        self.line(line);
    }

    fn who(&self, id: AgentId) -> String {
        self.names.get(&id).map(|(n, _)| n.clone()).unwrap_or_else(|| format!("#{id}"))
    }

    fn is_lead(&self, id: AgentId) -> bool {
        self.names.get(&id).is_some_and(|(n, r)| *r == Role::Lead && (n == "lead" || n == "chuck"))
    }

    fn line(&mut self, s: String) {
        let _ = writeln!(self.out, "{s}");
        let _ = self.out.flush();
    }

    pub fn show(&mut self, e: &Event) {
        let st = &self.st;
        let text = match e {
            Event::AgentStarted { id, role, name, model, task } => {
                self.names.insert(*id, (name.clone(), *role));
                if name == "lead" || name == "chuck" {
                    return;
                }
                let doing = match task {
                    Some(t) => format!(" on {t}"),
                    None if name == "critic" => " reviewing the whole result".into(),
                    None => String::new(),
                };
                st.dim(&format!("  + {name} ({}){doing}", short_model(model)))
            }
            Event::Said { id, text } => {
                if self.is_lead(*id) {
                    format!("\n{}", text.trim())
                } else if self.verbose {
                    let who = self.who(*id);
                    st.dim(&format!("  {who} │ {}", one_line(text, 140)))
                } else {
                    return;
                }
            }
            Event::Action { id, tool, arg, ok, summary } => {
                let who = self.who(*id);
                let mark = if *ok { st.good("✓") } else { st.bad("✗") };
                let what = match tool.as_str() {
                    "bash" | "test" => format!("{} → {}", one_line(arg, 70), one_line(summary, 40)),
                    "search" => format!("“{}” → {}", one_line(arg, 60), one_line(summary, 40)),
                    "fetch" => format!("{} → {}", one_line(arg, 70), one_line(summary, 60)),
                    _ if summary.is_empty() => one_line(arg, 90),
                    _ => one_line(summary, 110),
                };
                format!("  {mark} {} {} {}", st.dim(&format!("{who:<6}")), tool, st.dim(&what))
            }
            Event::Team { from, to, text } => st.dim(&format!("  ✉ {from} → {}: {}", to.as_deref().unwrap_or("everyone"),
                                                           one_line(text, 140))),
            Event::Gallery { query, items } => {
                let mut s = format!("  {} {query}", st.accent(&format!("◉ {} pictures", items.len())));
                for p in items {
                    s.push_str(&format!("\n      {} {}", p.path.display(), st.dim(&format!("({})", p.page))));
                }
                s
            }
            Event::Videos { query, items } => {
                let mut s = format!("  {} {query}", st.accent(&format!("◉ {} videos", items.len())));
                for v in items {
                    s.push_str(&format!("\n      {}{} {}", v.title, v.length.as_ref().map(|l| format!(" ({l})")).unwrap_or_default(),
                                        st.dim(&v.url)));
                }
                s
            }
            Event::Browsed { url, errors, failed, shot, look, .. } => {
                let mut s = format!("  {} {url}: {} JS error{}, {} failed request{}", st.accent("◉ browser"), errors.len(),
                                    if errors.len() == 1 { "" } else { "s" }, failed.len(), if failed.len() == 1 { "" } else { "s" });
                for e in errors.iter().take(5) {
                    s.push_str(&format!("\n      {}", st.bad(&one_line(e, 140))));
                }
                if let Some(l) = look {
                    s.push_str(&format!("\n      {}", st.dim(&one_line(l, 200))));
                }
                if let Some(p) = shot {
                    s.push_str(&format!("\n      {}", st.dim(&format!("screenshot: {}", p.display()))));
                }
                s
            }
            Event::Output { chunk, .. } => {
                if !self.verbose {
                    return;
                }
                st.dim(chunk.trim_end())
            }
            Event::Assigned { task, title, worker, role } => {
                let who = self.who(*worker);
                st.accent(&format!("  ◆ {task} \"{}\" → {who} ({})", one_line(title, 60), role.label()))
            }
            Event::Submitted { task, worker, lines } => {
                let who = self.who(*worker);
                st.accent(&format!("  ◆ {task} submitted by {who} ({lines} lines changed)"))
            }
            Event::Reviewed { task, accepted, defects } => {
                let what = if task == "job" { "independent review".to_string() } else { task.clone() };
                if *accepted {
                    st.good(&format!("  ✓ {what}: accepted"))
                } else {
                    st.warn(&format!("  ↺ {what}: sent back — {}", one_line(&defects.join("; "), 140)))
                }
            }
            Event::Merged { task, applied, merged, conflicts, refused } => {
                let mut s = format!("  ⇣ {task} merged into your folder: {applied} file(s) applied, {merged} merged");
                if !conflicts.is_empty() {
                    s.push_str(&format!(", CONFLICTS: {}", conflicts.join(", ")));
                }
                if !refused.is_empty() {
                    s.push_str(&format!(", refused: {}", refused.join(", ")));
                }
                if conflicts.is_empty() { st.good(&s) } else { st.warn(&s) }
            }
            Event::Gate { id, task: None, phase, unmet } => {
                if !self.is_lead(*id) {
                    return;
                }
                let key = (phase.clone(), unmet.len());
                if self.last_gate.as_ref() == Some(&key) {
                    return;
                }
                self.last_gate = Some(key);
                let st = &self.st;
                if unmet.is_empty() {
                    st.dim(&format!("  gate: {phase} — nothing missing"))
                } else {
                    st.dim(&format!("  gate: {phase} — {} open: {}", unmet.len(), one_line(&unmet.join("; "), 120)))
                }
            }
            Event::Plan { steps } => {
                let done = steps.iter().filter(|(_, d)| *d).count();
                let now = steps.iter().find(|(_, d)| !d).map(|(s, _)| one_line(s, 80))
                    .unwrap_or_else(|| "every step done".into());
                st.accent(&format!("  ▤ plan {done}/{} — now: {now}", steps.len()))
            }
            Event::Running { id, cmd, .. } => {
                if !self.verbose {
                    return;
                }
                let who = self.who(*id);
                st.dim(&format!("  ⏵ {who:<6} {}", one_line(cmd, 100)))
            }
            Event::Gate { .. } | Event::Board { .. } | Event::Tasks(_) | Event::Thinking { .. } | Event::Delta { .. } => return,
            Event::Notice { id, text } => {
                let who = self.who(*id);
                st.warn(&format!("  ! {who}: {text}"))
            }
            Event::Ask { question, .. } => format!("\n{} {}", st.bold("? the lead asks:"), question.trim()),
            Event::Confirm { command, why, outside, .. } => format!("\n{} {}\n  {}\n  {}{}",
                if outside.is_empty() { st.bad(&st.bold("! CRITICAL")) } else { st.warn(&st.bold("! OUTSIDE THE FOLDER")) },
                st.bold("the lead wants to run:"), command.trim(), st.dim(why),
                if outside.is_empty() { String::new() } else { format!("\n  {}", st.dim(&format!("it reaches: {}", outside.join(", ")))) }),
            Event::Usage { tokens, usd, prompt, cached, .. } => {
                self.tokens += tokens;
                self.usd += usd;
                self.prompt += prompt;
                self.cached += cached;
                return;
            }
            Event::AgentEnded { id, status } => {
                if self.is_lead(*id) || !self.verbose {
                    return;
                }
                let who = self.who(*id);
                st.dim(&format!("  − {who} {status}"))
            }
            Event::Schedule { kind, when, what } => format!("{} {}: {}", st.bold(if kind == "remind" { "⏰ reminder set," }
                else { "◎ daemon set," }), chuck_daemon::describe(when), what),
            Event::Handoff { request } => format!("\n{}\n  {}\n{}", st.bold("→ for the fleet (nothing has run):"),
                request.trim().replace('\n', "\n  "),
                st.dim(&format!("  to send it:  chuck run \"{}\"", one_line(request, 200).replace('"', "\\\"")))),
            Event::Finished(o) => self.ending(o),
        };
        self.line(text);
    }

    fn ending(&self, o: &Outcome) -> String {
        let st = &self.st;
        let cache = if self.prompt > 0 && self.cached > 0 {
            format!(" · {:.0}% of the prompt from cache", 100.0 * self.cached as f64 / self.prompt as f64)
        } else {
            String::new()
        };
        let spend = st.dim(&format!("{} tokens · {}{cache}", fmt_tokens(self.tokens), fmt_usd(self.usd)));
        match o {
            Outcome::Done { summary } => format!("\n{}  {spend}\n{}", st.good(&st.bold("DONE")), summary.trim()),
            Outcome::Answered { .. } => format!("\n{spend}"),
            Outcome::Paused { gaps } => {
                let mut s = format!("\n{}  {spend}\nNot finished. What is still missing:", st.warn(&st.bold("INCOMPLETE")));
                for g in gaps {
                    s.push_str(&format!("\n  - {g}"));
                }
                s
            }
            Outcome::Failed { error } => format!("\n{}  {error}", st.bad(&st.bold("FAILED"))),
        }
    }
}

pub fn short_model(m: &str) -> &str {
    m.rsplit('/').next().unwrap_or(m)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[derive(Clone, Default)]
    struct Buf(Arc<Mutex<Vec<u8>>>);
    impl Write for Buf {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn renders_the_story_and_an_honest_ending() {
        let buf = Buf::default();
        let mut v = View::new(false, false, Box::new(buf.clone()));
        let lead = AgentId(1);
        let w = AgentId(2);
        v.show(&Event::AgentStarted { id: lead, role: Role::Lead, name: "lead".into(), model: "m".into(), task: None });
        v.show(&Event::AgentStarted { id: w, role: Role::GlmWorker, name: "glm-1".into(),
                                      model: "zai-org/GLM-5.3-Flash".into(), task: Some("t1".into()) });
        v.show(&Event::Said { id: lead, text: "On it.".into() });
        v.show(&Event::Said { id: w, text: "worker chatter".into() });
        v.show(&Event::Action { id: w, tool: "write".into(), arg: "a.rs".into(), ok: true, summary: "created a.rs (+3 −0)".into() });
        v.show(&Event::Usage { id: w, tokens: 1500, usd: 0.001, prompt: 1200, cached: 600 });
        v.show(&Event::Finished(Outcome::Paused { gaps: vec!["c1 has no evidence".into()] }));
        let s = String::from_utf8(buf.0.lock().unwrap().clone()).unwrap();
        assert!(s.contains("+ glm-1 (GLM-5.3-Flash) on t1"));
        assert!(s.contains("On it."));
        assert!(!s.contains("worker chatter"), "workers' prose only with --verbose");
        assert!(s.contains("✓ glm-1  write created a.rs (+3 −0)"));
        assert!(s.contains("INCOMPLETE"));
        assert!(s.contains("- c1 has no evidence"));
        assert!(!s.contains('\x1b'));
    }

    #[test]
    fn chat_mode_reads_as_an_answer_and_a_proposal() {
        let buf = Buf::default();
        let mut v = View::new(false, false, Box::new(buf.clone()));
        let c = AgentId(1);
        v.show(&Event::AgentStarted { id: c, role: Role::Lead, name: "chuck".into(), model: "m".into(), task: None });
        v.show(&Event::Action { id: c, tool: "search".into(), arg: "rg smart case".into(), ok: true, summary: "5 results".into() });
        v.show(&Event::Said { id: c, text: "Use `rg -S`.".into() });
        v.show(&Event::Handoff { request: "Add alias rg=\"rg -S\" to ~/.bashrc".into() });
        v.show(&Event::Finished(Outcome::Answered { text: "Use `rg -S`.".into() }));
        let s = String::from_utf8(buf.0.lock().unwrap().clone()).unwrap();
        assert!(!s.contains("+ chuck"), "{s}");
        assert!(s.contains("✓ chuck  search “rg smart case” → 5 results"), "{s}");
        assert!(s.contains("\nUse `rg -S`."));
        assert!(s.contains("→ for the fleet (nothing has run):\n  Add alias"));
        assert!(s.contains(r#"chuck run "Add alias rg=\"rg -S\" to ~/.bashrc""#), "{s}");
    }
}
