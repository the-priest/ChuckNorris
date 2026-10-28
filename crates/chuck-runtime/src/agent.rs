//! An agent's thinking: a model call through the kernel's scheduler, its
//! conversation kept within the context.

use crate::events::{Event, Events};
use crate::RuntimeConfig;
use chuck_core::{AgentId, Kernel};
use chuck_model::{price_for, ChatModel, ChatRequest, ModelError, Notice, Sink, Usage};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

/// What every agent of a job shares.
#[derive(Clone)]
pub struct Ctx {
    pub kernel: Arc<Kernel>,
    pub model: Arc<dyn ChatModel>,
    pub cfg: RuntimeConfig,
    pub events: Events,
    /// The user's Stop: every agent of the job, and the commands they run.
    pub stop: Arc<AtomicBool>,
}

/// Why an agent's thinking ended early.
#[derive(Clone, Debug, PartialEq)]
pub enum Halt {
    /// The kernel ended it: a quota (its own or an ancestor's), or a KILL.
    Killed(String),
    /// The user pressed Stop.
    Stopped,
    /// The API failed after its own retries.
    Model(String),
}

impl Halt {
    pub fn describe(&self) -> String {
        match self {
            Halt::Killed(why) => format!("stopped by the kernel: {why}"),
            Halt::Stopped => "stopped by you".into(),
            Halt::Model(e) => e.clone(),
        }
    }
}

/// A conversation that stays inside the context window: the system prompt
/// and the first message (the task) are kept; the oldest exchanges after
/// them go first.
#[derive(Clone, Debug, Default)]
pub struct Convo {
    pub msgs: Vec<Value>,
    /// Characters of old output folded away to keep calls cheap.
    pub elided: usize,
    /// The task as given, kept apart so trimming notes never touch it.
    first: String,
    trimmed: usize,
    /// Where the task is: after the system prompt and any earlier turns of
    /// the conversation it continues. Nothing at or before it is trimmed.
    head: usize,
}

/// Turns kept whole at the end of a conversation.
const KEEP_RECENT: usize = 6;
const ELIDED: &str = "elided to keep the context small";
const TRIM_NOTE_END: &str = "earlier messages were trimmed to fit the context; the gate keeps every piece of evidence, so \
    re-read files rather than relying on memory]";

/// Keep the head and the tail of an over-long message (errors live at the
/// end, what was asked lives at the start).
fn squeeze(s: &str, max: usize) -> String {
    if s.len() <= max {
        return s.to_string();
    }
    let keep = max.saturating_sub(80) / 2;
    let mut a = keep;
    while !s.is_char_boundary(a) {
        a -= 1;
    }
    let mut b = s.len() - keep;
    while !s.is_char_boundary(b) {
        b += 1;
    }
    format!("{}\n… ({} characters left out to fit the context) …\n{}", &s[..a], b - a, &s[b..])
}

impl Convo {
    pub fn new(system: &str, first: &str) -> Convo {
        Convo::with_history(system, &[], first)
    }

    /// The task as the next turn of a conversation: the earlier turns go in
    /// as the real messages they were (a transcript pasted into one message
    /// gets imitated: the model writes the user's next line itself).
    pub fn with_history(system: &str, history: &[(bool, String)], first: &str) -> Convo {
        let mut msgs = vec![json!({"role": "system", "content": system})];
        for (user, text) in history {
            msgs.push(json!({"role": if *user { "user" } else { "assistant" }, "content": text}));
        }
        msgs.push(json!({"role": "user", "content": first}));
        let head = msgs.len() - 1;
        Convo { msgs, elided: 0, first: first.to_string(), trimmed: 0, head }
    }

    /// A conversation as it was saved (a paused job carrying on); `head`:
    /// where its task is.
    pub fn restore(msgs: Vec<Value>, head: usize) -> Convo {
        let head = head.clamp(1, msgs.len().saturating_sub(1).max(1));
        let mut first = msgs.get(head).and_then(|m| m["content"].as_str()).unwrap_or("").to_string();
        // the trimming note is the conversation's, not the task's: taken off,
        // and its count kept, so a later trim says the total once
        let mut trimmed = 0;
        if let Some(at) = first.rfind("\n\n[") {
            let note = &first[at + 3..];
            if let Some(n) = note.strip_suffix(TRIM_NOTE_END).and_then(|n| n.split(' ').next()).and_then(|n| n.parse().ok()) {
                trimmed = n;
                first.truncate(at);
            }
        }
        Convo { msgs, elided: 0, first, trimmed, head }
    }

    /// Where the task is (see `restore`).
    pub fn head(&self) -> usize {
        self.head
    }

    pub fn user(&mut self, text: &str) {
        self.push("user", text);
    }

    pub fn assistant(&mut self, text: &str) {
        self.push("assistant", text);
    }

    /// Turns alternate (a strict API refuses two of one side in a row, and
    /// a model reads them as one anyway): a second note from the same side
    /// joins the one before it; an empty one is left out.
    fn push(&mut self, role: &str, text: &str) {
        let text = if text.trim().is_empty() {
            if role == "user" { return } else { "(no reply)" }
        } else {
            text
        };
        let last = self.msgs.len() - 1;
        if last > 0 && self.msgs[last]["role"] == role {
            if let Some(prev) = self.msgs[last]["content"].as_str() {
                let joined = format!("{prev}\n\n{text}");
                if last == self.head {
                    self.first = format!("{}\n\n{text}", self.first);
                }
                self.msgs[last]["content"] = Value::String(joined);
                return;
            }
        }
        self.msgs.push(json!({"role": role, "content": text}));
    }

    fn chars(&self) -> usize {
        self.msgs.iter().map(|m| m["content"].as_str().map_or(0, str::len)).sum()
    }

    /// Keep the context cheap without losing what matters: past half the
    /// limit, long tool output and long replies older than the last few
    /// turns shrink to their head and a note — all in one go, so the API's
    /// prompt cache (which bills a repeated prefix at a fraction) is broken
    /// only once per compaction, not on every call. Recent turns, the system
    /// prompt and the task are never touched; files are on disk to re-read.
    fn compact(&mut self, soft: usize) -> usize {
        let from = self.head + 1;
        if self.chars() <= soft || self.msgs.len() <= from + KEEP_RECENT {
            return 0;
        }
        let mut saved = 0;
        let end = self.msgs.len() - KEEP_RECENT;
        for m in &mut self.msgs[from..end] {
            let text = m["content"].as_str().unwrap_or("").to_string();
            let limit = if m["role"] == "assistant" { 3000 } else { 1500 };
            if text.len() <= limit || text.contains(ELIDED) {
                continue;
            }
            let mut cut = 600;
            while !text.is_char_boundary(cut) {
                cut -= 1;
            }
            let what = if m["role"] == "assistant" { "your earlier reply (what it wrote is on disk)" } else {
                "tool output — re-read or re-run if you need it again" };
            let short = format!("{}\n[{ELIDED}: {} more characters of {what}]", &text[..cut], text.len() - cut);
            saved += text.len() - short.len();
            m["content"] = Value::String(short);
        }
        self.elided += saved;
        saved
    }

    /// Fit the context: compact old output first (see `compact`); then, if
    /// it's still too big, drop the oldest assistant/user pairs (never the
    /// system prompt, the task, or the latest exchange) down to three
    /// quarters of the limit, so this too happens rarely; if one message is
    /// still too big on its own, keep its head and tail.
    pub fn fit(&mut self, max_chars: usize) {
        self.compact(max_chars / 2);
        // pairs after the task go, oldest first (the roles keep alternating)
        let (a, b) = (self.head + 1, self.head + 3);
        if self.chars() > max_chars {
            while self.chars() > max_chars * 3 / 4 && self.msgs.len() > b {
                self.msgs.drain(a..b);
                self.trimmed += 2;
            }
        }
        while self.chars() > max_chars && self.msgs.len() > b {
            self.msgs.drain(a..b);
            self.trimmed += 2;
        }
        if self.trimmed > 0 {
            let note = format!("[{} {TRIM_NOTE_END}", self.trimmed);
            self.msgs[self.head]["content"] = Value::String(format!("{}\n\n{note}", self.first));
        }
        let total = self.chars();
        if total > max_chars {
            let over = total - max_chars;
            if let Some(big) = self.msgs.iter_mut().skip(1)
                .max_by_key(|m| m["content"].as_str().map_or(0, str::len))
            {
                let text = big["content"].as_str().unwrap_or("").to_string();
                let target = text.len().saturating_sub(over).max(2000);
                big["content"] = Value::String(squeeze(&text, target));
            }
        }
    }
}

/// Where a reply that has started repeating itself (a model stuck in a
/// loop, writing the same line thousands of times) should be cut: after its
/// first copy of what repeats. None: it isn't. A short unit must repeat 30
/// times and a long one 8 (a 22-row board literal is code, not a loop).
pub fn runaway_at(s: &str) -> Option<usize> {
    let n = s.len();
    if n < 2000 {
        return None;
    }
    let base = n.saturating_sub(24_000);
    let b = &s.as_bytes()[base..];
    let m = b.len();
    // a unit up to a long one-liner (a `node -e "…"` check written again and again)
    for l in 1..=1200usize {
        if l * 8 > m {
            break;
        }
        let unit = &b[m - l..];
        let mut k = 1;
        while (k + 1) * l <= m && &b[m - (k + 1) * l..m - k * l] == unit {
            k += 1;
        }
        let span = k * l;
        let looping = if l <= 64 { k >= 30 && span >= 2000 } else { k >= 8 && span >= 3000 };
        if looping {
            // where it really began: back through the whole reply, past the window looked at
            let full = s.as_bytes();
            let mut start = n - span;
            while start >= l && &full[start - l..start] == unit {
                start -= l;
            }
            let mut at = start + l;
            while at < n && !s.is_char_boundary(at) {
                at += 1;
            }
            return Some(at);
        }
    }
    None
}

struct AgentSink<'a> {
    id: AgentId,
    ctx: &'a Ctx,
    /// The reply as it streams in, and where it began repeating itself (the
    /// call is stopped there).
    got: String,
    checked: usize,
    runaway: Option<usize>,
    /// When the call began, what has streamed in so far (characters of
    /// reasoning, of reply), and when the agent's line last said so.
    t0: std::time::Instant,
    thought: usize,
    wrote: usize,
    shown: Option<std::time::Instant>,
    /// Function calls as their arguments stream in: (name, the JSON so far).
    calls: Vec<(String, String)>,
    /// The file being written, as last shown: (path, lines, the newest lines), and when.
    live: Option<(String, usize, String)>,
    live_at: Option<std::time::Instant>,
    /// The reply has a ```write``` / ```append``` block in it (looked for in what's new only).
    fenced_write: bool,
}

/// A fenced ```write``` / ```append``` block still being written at the end
/// of `text` (or the last one, closed): (path, its content so far).
fn open_write(text: &str) -> Option<(String, String)> {
    static RX: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let rx = RX.get_or_init(|| regex::Regex::new(r"(?m)^(`{3,4})(?:write|append)[ \t]+(\S+)[^\n]*\n").unwrap());
    let c = rx.captures_iter(text).last()?;
    let body = &text[c.get(0)?.end()..];
    let fence = format!("\n{}", &c[1]);
    let body = if body.starts_with(&c[1]) { "" } else { body.find(&fence).map_or(body, |e| &body[..e]) };
    Some((c[2].to_string(), body.to_string()))
}

/// The string value of `key` in JSON that may still be arriving (decoded as
/// far as it goes).
fn partial_field(json: &str, key: &str) -> Option<String> {
    let at = json.find(&format!("\"{key}\""))? + key.len() + 2;
    let rest = json[at..].trim_start().strip_prefix(':')?.trim_start().strip_prefix('"')?;
    let mut out = String::new();
    let mut chars = rest.chars();
    while let Some(c) = chars.next() {
        match c {
            '"' => break,
            '\\' => match chars.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => {}
                Some('u') => {
                    let hex: String = chars.by_ref().take(4).collect();
                    if let Some(ch) = u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32) {
                        out.push(ch);
                    }
                }
                Some(o) => out.push(o),
                None => break,
            },
            _ => out.push(c),
        }
    }
    Some(out)
}

impl AgentSink<'_> {
    /// The file being written, shown as it grows (about three times a second;
    /// `last`: the reply is complete).
    fn live_file(&mut self, last: bool) {
        if !last && self.live_at.is_some_and(|t| t.elapsed() < Duration::from_millis(300)) {
            return;
        }
        let from_call = self.calls.iter().rev().filter(|(n, _)| n == "write" || n == "append")
            .find_map(|(_, a)| Some((partial_field(a, "path")?, partial_field(a, "content").unwrap_or_default())))
            .filter(|(p, _)| !p.is_empty());
        let Some((path, content)) = from_call.or_else(|| open_write(&self.got)) else { return };
        self.live_at = Some(std::time::Instant::now());
        let lines = content.lines().count();
        let all: Vec<&str> = content.lines().collect();
        let tail = all[all.len().saturating_sub(14)..].iter()
            .map(|l| l.chars().take(160).collect::<String>()).collect::<Vec<_>>().join("\n");
        if !last && self.live.as_ref().is_some_and(|(p, n, _)| *p == path && *n == lines) {
            return; // nothing new to show
        }
        self.ctx.events.emit(Event::Writing { id: self.id, path: path.clone(), lines, tail: tail.clone(), done: last });
        self.live = Some((path, lines, tail));
    }

    /// What the call is doing, on the agent's line, about once a second: a
    /// long reply is seen growing, never a silent "thinking · 0".
    fn progress(&mut self) {
        if self.shown.is_some_and(|t| t.elapsed() < Duration::from_secs(1)) {
            return;
        }
        self.shown = Some(std::time::Instant::now());
        let secs = self.t0.elapsed().as_secs();
        let what = if self.wrote > 0 {
            format!("writing · ~{} tokens · {secs}s", (self.wrote / 4).max(1))
        } else {
            format!("thinking · ~{} tokens · {secs}s", (self.thought / 4).max(1))
        };
        self.ctx.kernel.progress(self.id, &what);
    }
}

impl Sink for AgentSink<'_> {
    fn think(&mut self, c: &str) {
        self.thought += c.len();
        self.progress();
        self.ctx.events.emit(Event::Thinking { id: self.id, chunk: c.into() });
    }
    fn delta(&mut self, c: &str) {
        self.got.push_str(c);
        if self.runaway.is_none() && self.got.len() >= self.checked + 512 {
            self.checked = self.got.len();
            self.runaway = runaway_at(&self.got);
        }
        self.wrote += c.len();
        self.progress();
        self.ctx.events.emit(Event::Delta { id: self.id, chunk: c.into() });
        if !self.fenced_write {
            let mut from = self.got.len().saturating_sub(c.len() + 24);
            while !self.got.is_char_boundary(from) {
                from -= 1;
            }
            let new = &self.got[from..];
            self.fenced_write = new.contains("```write") || new.contains("```append");
        }
        if self.fenced_write {
            self.live_file(false);
        }
    }
    fn call(&mut self, i: usize, name: &str, piece: &str) {
        let i = i.min(255);
        while self.calls.len() <= i {
            self.calls.push((String::new(), String::new()));
        }
        if self.calls[i].0.is_empty() {
            self.calls[i].0 = name.to_string();
        }
        self.calls[i].1.push_str(piece);
        self.wrote += piece.len();
        self.progress();
        if name == "write" || name == "append" || self.calls[i].0 == "write" || self.calls[i].0 == "append" {
            self.live_file(false);
        }
    }
    fn notice(&mut self, n: Notice) {
        let text = match n {
            Notice::ThinkCut { secs } => format!("thought for {secs}s without answering — asked again with thinking off"),
            Notice::Retrying { attempt, why } => format!("retrying ({attempt}): {why}"),
            Notice::OptionDropped(o) => format!("the API refused `{o}`; sent without it"),
        };
        let who = self.ctx.kernel.ps(true).into_iter().find(|p| p.pid == self.id).map(|p| p.name).unwrap_or_default();
        crate::snag(&self.ctx.events, self.id, &who, "api", &text);
        self.ctx.events.emit(Event::Notice { id: self.id, text });
    }
    fn should_stop(&self) -> bool {
        self.runaway.is_some() || self.ctx.stop.load(Ordering::Relaxed) || self.ctx.kernel.should_stop(self.id)
    }
}

/// One model call, scheduled and metered by the kernel. A reply the API cut
/// at its length limit is continued (twice at most) so a half-written block
/// is never executed.
pub fn think(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo) -> Result<String, Halt> {
    think_with(ctx, id, model, convo, &[])
}

/// [`think`], offering the agent's `tags` as functions it can call: a call
/// comes back as the fenced block it means, appended to the reply, so the
/// rest of the loop reads it as if it had been written that way.
pub fn think_with(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo, tags: &[&str]) -> Result<String, Halt> {
    think_as(ctx, id, model, convo, tags, None, None)
}

/// [`think_with`], with this call's reasoning and reply budget set (None
/// each: the agent's own, the full `max_tokens`).
pub fn think_as(ctx: &Ctx, id: AgentId, model: &str, convo: &Convo, tags: &[&str],
                reasoning: Option<chuck_model::Reasoning>, max_tokens: Option<u32>) -> Result<String, Halt> {
    let tools = (ctx.cfg.native_tools && !tags.is_empty()).then(|| crate::schemas::tools(tags));
    let mut msgs = convo.msgs.clone();
    let mut text = String::new();
    // how long the API has been unreachable (reset by any answer)
    let mut outage = Duration::ZERO;
    let mut round = 0;
    while round < 3 {
        // the limits as they are now: one changed in Settings mid-job applies to the next call
        let (day, month) = if ctx.cfg.live_limits {
            let s = chuck_store::Settings::load();
            (s.num("budget_day_usd", 0.0, 0.0, 100_000.0), s.num("budget_month_usd", 0.0, 0.0, 1_000_000.0))
        } else {
            (ctx.cfg.day_limit, ctx.cfg.month_limit)
        };
        if let Some(over) = ctx.cfg.spend.as_ref().and_then(|s| s.over(day, month)) {
            return Err(Halt::Killed(format!("the {} spending limit ({}) is reached — only you can raise it, in \
                Settings", if over.scope == "day" { "daily" } else { "monthly" }, chuck_model::fmt_usd(over.limit))));
        }
        loop {
            if ctx.stop.load(Ordering::Relaxed) {
                return Err(Halt::Stopped);
            }
            match ctx.kernel.acquire(id, Some(Duration::from_millis(300))) {
                Ok(true) => break,
                Ok(false) => continue,
                Err(k) => return Err(Halt::Killed(k.0)),
            }
        }
        ctx.kernel.note(id, "waiting for the model's first word");
        let mut req = ChatRequest::new(model, msgs.clone());
        req.reasoning = reasoning.unwrap_or(ctx.cfg.reasoning);
        // a continuation gets the full budget: the reply was cut, so it IS a long one
        req.max_tokens = if round == 0 { max_tokens.unwrap_or(ctx.cfg.max_tokens) } else { ctx.cfg.max_tokens };
        // a continuation goes on as text (a cut function call can't be continued as one)
        req.tools = if round == 0 { tools.clone() } else { None };
        let mut sink = AgentSink { id, ctx, got: String::new(), checked: 0, runaway: None,
                                   t0: std::time::Instant::now(), thought: 0, wrote: 0, shown: None, calls: Vec::new(),
                                   live: None, live_at: None, fenced_write: false };
        let r = chuck_model::chat_interruptible(&ctx.model, &req, &mut sink);
        if sink.live.is_some() {
            sink.live_file(true); // the file as the reply left it
        }
        ctx.kernel.release(id);
        // a reply caught in a loop: stopped at once, what came before the loop kept, the agent told
        let r = match (r, sink.runaway) {
            (Ok(_) | Err(ModelError::Stopped), Some(at)) if !ctx.stop.load(Ordering::Relaxed) && !ctx.kernel.should_stop(id) => {
                let who = ctx.kernel.ps(true).into_iter().find(|p| p.pid == id).map(|p| p.name).unwrap_or_default();
                crate::snag(&ctx.events, id, &who, "runaway reply", &format!("it began repeating itself after {} \
                    characters: {}", at, chuck_core::evidence::clip(&sink.got[at..], 120)));
                let kept = format!("{}\n\n[cut: this reply began repeating the same text over and over, so it was \
                    stopped there]", &sink.got[..at]);
                let usage = Usage::estimate(req.sent_chars(), sink.got.len());
                Ok(chuck_model::Outcome { text: kept, usage: Some(usage), finish: Some("stop".into()),
                                          model: req.model.clone(), ..Default::default() })
            }
            (r, _) => r,
        };
        let out = match r {
            Ok(o) => {
                outage = Duration::ZERO;
                o
            }
            Err(ModelError::Stopped) => {
                return Err(if ctx.stop.load(Ordering::Relaxed) { Halt::Stopped } else {
                    Halt::Killed("killed".into())
                });
            }
            // the network or the API is down: a long job waits it out rather than giving up
            Err(e) if e.transient() && outage < ctx.cfg.outage_wait => {
                let pause = Duration::from_secs(match outage.as_secs() { 0..=59 => 15, 60..=599 => 60, _ => 300 })
                    .min((ctx.cfg.outage_wait / 10).max(Duration::from_millis(100)));
                ctx.events.emit(Event::Notice { id, text: format!("the API isn't answering ({}) — trying again in {:.0}s; \
                    the job waits (Stop ends it)", chuck_core::evidence::clip(&e.to_string(), 120), pause.as_secs_f64()) });
                ctx.kernel.note(id, "waiting for the API");
                let until = std::time::Instant::now() + pause;
                while std::time::Instant::now() < until {
                    if ctx.stop.load(Ordering::Relaxed) {
                        return Err(Halt::Stopped);
                    }
                    if ctx.kernel.should_stop(id) {
                        return Err(Halt::Killed("killed".into()));
                    }
                    std::thread::sleep(Duration::from_millis(200));
                }
                outage += pause;
                continue;
            }
            Err(e) => return Err(Halt::Model(e.to_string())),
        };
        let usage = out.usage.unwrap_or_else(|| Usage::estimate(req.sent_chars(), out.text.len()));
        let usd = usage.cost(price_for(&out.model)).unwrap_or(0.0);
        ctx.kernel.charge(id, usage.prompt, usage.completion, usd);
        if let Some(s) = &ctx.cfg.spend {
            s.add(usd);
        }
        ctx.events.emit(Event::Usage { id, tokens: usage.prompt + usage.completion, usd, prompt: usage.prompt,
                                       cached: usage.cached });
        text.push_str(&out.text);
        // what it called, as the blocks they mean (one a text block already carries isn't run twice)
        let n = out.calls.len();
        for (i, (name, args)) in out.calls.iter().enumerate() {
            if let Some(b) = chuck_tools::native::call_block(name, args, tags) {
                if !text.contains(&b) {
                    text.push_str(&format!("\n{b}\n"));
                }
                continue;
            }
            // the last call, cut by the length limit mid-way (a big file): what arrived stays, as the
            // open block it was becoming, and the next round continues it in text
            if out.was_cut() && i + 1 == n {
                if let Some(open) = chuck_tools::native::partial_block(name, args, tags) {
                    text.push_str(&format!("\n{open}"));
                    continue;
                }
            }
            // a call that can't be made into a block: kept as a call, so the agent hears it didn't run
            text.push_str(&format!("\n<tool_call>{}</tool_call>\n", json!({"name": name, "arguments": args})));
        }
        if !out.was_cut() || round == 2 {
            if out.was_cut() {
                crate::snag(&ctx.events, id, &ctx.kernel.ps(true).into_iter().find(|p| p.pid == id).map(|p| p.name)
                    .unwrap_or_default(), "reply cut", "a reply ran past the length limit three times: what it wrote was \
                    cut short");
            }
            break;
        }
        msgs.push(json!({"role": "assistant", "content": text.clone()}));
        msgs.push(json!({"role": "user", "content":
            "[your reply was cut off by the length limit — continue EXACTLY where it stopped, no repetition]"}));
        round += 1;
    }
    ctx.kernel.note(id, "acting");
    Ok(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reply_caught_in_a_loop_is_cut_and_code_is_not() {
        // the reply in a real job: one call, written thousands of times
        let good = "Let me read the hook section.\n```read index.html:760-800```\n";
        let mut looped = good.to_string();
        for _ in 0..400 {
            looped.push_str("```read index.html:70300000```\n");
        }
        let at = runaway_at(&looped).expect("a loop");
        assert!(looped[..at].starts_with(good) && looped[..at].matches("70300000").count() == 1, "{}", &looped[..at]);
        assert!(runaway_at(&"0".repeat(5000)).is_some());
        // a 22-row board literal and a long ordinary file are code
        let board = format!("const board = [\n{}];\n", "  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],\n".repeat(22));
        assert!(runaway_at(&board).is_none());
        let code: String = (0..400).map(|i| format!("function f{i}(x) {{ return x + {i}; }}\n")).collect();
        assert!(runaway_at(&code).is_none());
    }

    #[test]
    fn a_file_being_written_is_read_as_it_arrives() {
        // a function call's arguments, cut anywhere
        let json = r#"{"path": "tetris.html", "content": "<canvas>\n<script>\nconst ROWS = 20;\nconst p = \"é\u00e9"#;
        assert_eq!(partial_field(json, "path").as_deref(), Some("tetris.html"));
        assert_eq!(partial_field(json, "content").as_deref(), Some("<canvas>\n<script>\nconst ROWS = 20;\nconst p = \"éé"));
        assert_eq!(partial_field(r#"{"pa"#, "path"), None);
        assert_eq!(partial_field(r#"{"path": "a.js", "content": "x\"#, "content").as_deref(), Some("x"), "a cut escape");
        // a fenced block still open, then closed
        let open = "Here it is.\n```write game.js\nlet a = 1;\nlet b = 2;\n";
        assert_eq!(open_write(open), Some(("game.js".into(), "let a = 1;\nlet b = 2;\n".into())));
        let closed = format!("{open}```\n```test node --check game.js```");
        assert_eq!(open_write(&closed), Some(("game.js".into(), "let a = 1;\nlet b = 2;".into())));
        assert_eq!(open_write("```bash\nls\n```"), None);
        assert_eq!(open_write("````write r.md\n```js\nx\n```\n").map(|w| w.0), Some("r.md".into()));
    }

    #[test]
    fn convo_trims_oldest_pairs_and_keeps_the_task() {
        let mut c = Convo::new("sys", "the task");
        for i in 0..10 {
            c.assistant(&format!("reply {i} {}", "x".repeat(100)));
            c.user(&format!("result {i} {}", "y".repeat(100)));
        }
        c.fit(1000);
        assert!(c.chars() <= 1300);
        assert_eq!(c.msgs[0]["content"], "sys");
        assert!(c.msgs[1]["content"].as_str().unwrap().starts_with("the task\n\n["));
        assert_eq!(c.msgs[2]["role"], "assistant");
        assert!(c.msgs.last().unwrap()["content"].as_str().unwrap().starts_with("result 9"));
        c.fit(1000);
        assert_eq!(c.msgs[1]["content"].as_str().unwrap().matches("trimmed").count(), 1);
    }

    #[test]
    fn a_task_after_earlier_turns_is_kept_and_found_again_after_a_pause() {
        let hist = vec![(true, "earlier question".to_string()), (false, "earlier answer".to_string())];
        let mut c = Convo::with_history("sys", &hist, "the task");
        assert_eq!(c.head(), 3);
        // notes from one side in a row become one turn; an empty one is left out
        c.user("note a");
        c.user("  ");
        assert_eq!(c.msgs.len(), 4);
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("the task\n\nnote a"));
        for i in 0..10 {
            c.assistant(&format!("reply {i} {}", "x".repeat(100)));
            c.user(&format!("result {i} {}", "y".repeat(100)));
        }
        c.fit(1200);
        let roles: Vec<&str> = c.msgs.iter().map(|m| m["role"].as_str().unwrap()).collect();
        assert_eq!(&roles[..5], ["system", "user", "assistant", "user", "assistant"], "the earlier turns and the task stay");
        assert!(roles.windows(2).skip(1).all(|w| w[0] != w[1]), "{roles:?}");
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("the task\n\nnote a\n\n["));
        // paused and carried on: the task is where it was
        let r = Convo::restore(c.msgs.clone(), c.head());
        assert_eq!((r.head(), r.first.as_str(), r.trimmed), (3, "the task\n\nnote a", 14));
        let mut r = r;
        for i in 0..10 {
            r.assistant(&format!("more {i} {}", "x".repeat(100)));
            r.user(&format!("out {i} {}", "y".repeat(100)));
        }
        r.fit(1200);
        let task = r.msgs[3]["content"].as_str().unwrap();
        assert_eq!(task.matches("trimmed").count(), 1, "{task}");
        assert!(task.contains("[34 earlier messages"), "{task}");
        // a resume saved before there was a head (older files) still works
        assert_eq!(Convo::restore(c.msgs.clone(), 1).head(), 1);
        assert_eq!(Convo::restore(vec![], 9).head(), 1);
    }

    #[test]
    fn the_task_survives_trimming_even_with_brackets_in_it() {
        let task = "fix it\n\n[see the log] below";
        let mut c = Convo::new("sys", task);
        for i in 0..10 {
            c.assistant(&"a".repeat(200));
            c.user(&format!("r{i}"));
        }
        c.fit(1500);
        c.fit(1500);
        let first = c.msgs[1]["content"].as_str().unwrap();
        assert!(first.starts_with(task), "{first}");
        assert_eq!(first.matches("trimmed").count(), 1);
    }

    #[test]
    fn old_output_is_folded_in_one_batch_and_recent_turns_stay_whole() {
        let mut c = Convo::new("sys", "the task");
        for i in 0..10 {
            c.assistant(&format!("reply {i}"));
            c.user(&format!("OUTPUT{i} {}", "o".repeat(5000)));
        }
        let before = c.chars();
        c.fit(60_000); // soft limit 30k: compaction, no dropping
        assert!(c.chars() < before / 2, "{} vs {before}", c.chars()); // the last 3 outputs stay whole
        assert_eq!(c.msgs.len(), 22, "nothing dropped");
        assert!(c.msgs[3]["content"].as_str().unwrap().starts_with("OUTPUT0") &&
                c.msgs[3]["content"].as_str().unwrap().contains("elided"));
        let last = c.msgs.last().unwrap()["content"].as_str().unwrap();
        assert!(last.len() > 5000 && !last.contains("elided"), "the latest output is whole");
        // a second fit with nothing new changes nothing (the cached prefix stays the same)
        let snapshot = c.msgs.clone();
        c.fit(60_000);
        assert_eq!(c.msgs, snapshot);
    }

    #[test]
    fn one_huge_message_is_squeezed_not_sent_whole() {
        let mut c = Convo::new("sys", "task");
        c.assistant("ok");
        c.user(&format!("HEAD{}TAIL", "x".repeat(50_000)));
        c.fit(10_000);
        let last = c.msgs.last().unwrap()["content"].as_str().unwrap();
        assert!(c.chars() <= 10_200, "{}", c.chars());
        assert!(last.starts_with("HEAD") && last.ends_with("TAIL"));
    }
}
