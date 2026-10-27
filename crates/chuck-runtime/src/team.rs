//! The team's channel: workers and the Lead write to each other while a job
//! runs, so parallel work stays one piece. A worker says what it exports
//! and how it is called, asks who owns a file, or warns about a decision
//! others depend on; the Lead hears what is addressed to it.
//!
//! A post goes to everyone, or to one name (`@ds-2`, `@lead`). Each reader
//! gets what it hasn't seen yet, never its own posts. The Lead's lifecycle
//! notes (a task started, merged) are posts too, so every worker knows who
//! is on what.

use std::sync::Mutex;

#[derive(Clone, Debug, PartialEq)]
pub struct Post {
    pub from: String,
    /// One reader (a worker's name or "lead"), or everyone.
    pub to: Option<String>,
    pub text: String,
}

/// Posts kept per job; past this the oldest go (readers have seen them).
const KEEP: usize = 2000;

#[derive(Default)]
pub struct Team {
    /// (sequence number, post)
    posts: Mutex<(u64, Vec<(u64, Post)>)>,
}

impl Team {
    pub fn new() -> Team {
        Team::default()
    }

    /// Post; returns its number.
    pub fn post(&self, from: &str, to: Option<&str>, text: &str) -> u64 {
        let mut p = self.posts.lock().unwrap_or_else(|e| e.into_inner());
        p.0 += 1;
        let n = p.0;
        p.1.push((n, Post { from: from.into(), to: to.map(|t| t.trim_start_matches('@').to_ascii_lowercase()),
                            text: text.trim().into() }));
        if p.1.len() > KEEP {
            let drop = p.1.len() - KEEP;
            p.1.drain(..drop);
        }
        n
    }

    /// What `reader` hasn't seen since `cursor` (and the new cursor).
    pub fn since(&self, reader: &str, cursor: u64) -> (Vec<Post>, u64) {
        let p = self.posts.lock().unwrap_or_else(|e| e.into_inner());
        let me = reader.to_ascii_lowercase();
        let new: Vec<Post> = p.1.iter().filter(|(n, post)| *n > cursor && post.from.to_ascii_lowercase() != me
            && post.to.as_deref().is_none_or(|t| t == me)).map(|(_, post)| post.clone()).collect();
        (new, p.0)
    }

    /// Every post since `cursor` but `reader`'s own, whoever it was for:
    /// how the Lead follows the team.
    pub fn overheard(&self, reader: &str, cursor: u64) -> (Vec<Post>, u64) {
        let p = self.posts.lock().unwrap_or_else(|e| e.into_inner());
        let me = reader.to_ascii_lowercase();
        let new = p.1.iter().filter(|(n, post)| *n > cursor && post.from.to_ascii_lowercase() != me)
            .map(|(_, post)| post.clone()).collect();
        (new, p.0)
    }

    /// The newest post's number: a reader starting now reads from here.
    pub fn cursor(&self) -> u64 {
        self.posts.lock().unwrap_or_else(|e| e.into_inner()).0
    }

    /// The last `n` posts everyone was sent: what a newcomer should know.
    pub fn recent(&self, n: usize) -> Vec<Post> {
        let p = self.posts.lock().unwrap_or_else(|e| e.into_inner());
        let all: Vec<Post> = p.1.iter().filter(|(_, post)| post.to.is_none()).map(|(_, post)| post.clone()).collect();
        all[all.len().saturating_sub(n)..].to_vec()
    }

    /// Is anything waiting for `reader` that was sent to it by name?
    pub fn addressed(&self, reader: &str, cursor: u64) -> bool {
        let me = reader.to_ascii_lowercase();
        let p = self.posts.lock().unwrap_or_else(|e| e.into_inner());
        p.1.iter().any(|(n, post)| *n > cursor && post.to.as_deref() == Some(me.as_str()))
    }
}

/// What one turn may carry of the team's talk: the newest posts win.
const RENDER_CHARS: usize = 6000;

/// Posts as an agent reads them (the newest, when there are many).
pub fn render(posts: &[Post]) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut used = 0;
    for p in posts.iter().rev() {
        let line = format!("- {}{}: {}", p.from, p.to.as_ref().map(|t| format!(" → {t}")).unwrap_or_default(),
                           chuck_core::evidence::clip(&p.text, 1500));
        used += line.len();
        if used > RENDER_CHARS && !lines.is_empty() {
            lines.push(format!("- ({} older message(s) not shown)", posts.len() - lines.len()));
            break;
        }
        lines.push(line);
    }
    lines.reverse();
    format!("TEAM (new messages from your teammates):\n{}", lines.join("\n"))
}

/// "@ds-2 the rest" → (Some("ds-2"), "the rest"); no name: everyone.
pub fn addressee(arg: &str, body: &str) -> (Option<String>, String) {
    let arg = arg.trim();
    let (to, rest) = match arg.strip_prefix('@') {
        Some(r) => {
            let (name, rest) = r.split_once(char::is_whitespace).unwrap_or((r, ""));
            (Some(name.trim_end_matches([':', ',']).to_string()), rest.trim().to_string())
        }
        None => (None, arg.to_string()),
    };
    let text = [rest.as_str(), body.trim()].iter().filter(|s| !s.is_empty()).copied().collect::<Vec<_>>().join("\n");
    (to.filter(|t| !t.is_empty() && !t.eq_ignore_ascii_case("all") && !t.eq_ignore_ascii_case("team")), text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_reader_gets_what_is_for_it_once() {
        let t = Team::new();
        t.post("ds-1", None, "engine.js exports create(seed) -> game");
        t.post("ds-2", Some("ds-1"), "does create() take options?");
        t.post("ds-1", Some("lead"), "t1 needs the board size from you");
        let (a, c) = t.since("ds-2", 0);
        assert_eq!(a.len(), 1, "everyone's post, not its own, not someone else's");
        let (b, cb) = t.since("ds-1", 0);
        assert_eq!(b.iter().map(|p| p.from.as_str()).collect::<Vec<_>>(), ["ds-2"]);
        assert!(t.addressed("lead", 0) && !t.addressed("ds-3", 0));
        assert!(t.since("ds-2", c).0.is_empty() && t.since("ds-1", cb).0.is_empty(), "nothing twice");
        assert_eq!(t.overheard("lead", 0).0.len(), 3, "the Lead follows it all but its own");
        assert!(render(&b).contains("- ds-2 → ds-1: does create() take options?"));
        assert_eq!(t.cursor(), 3);
        assert_eq!(t.recent(5).len(), 1, "only what everyone was sent");
    }

    #[test]
    fn a_flood_shows_the_newest() {
        let posts: Vec<Post> = (0..50).map(|i| Post { from: "ds-1".into(), to: None, text: format!("{i} {}", "x".repeat(400)) }).collect();
        let r = render(&posts);
        assert!(r.len() < RENDER_CHARS + 1000 && r.contains("- ds-1: 49 ") && r.contains("older message(s) not shown"));
        assert!(!r.contains("- ds-1: 0 "));
    }

    #[test]
    fn who_a_post_is_for() {
        assert_eq!(addressee("@ds-2", "use u32"), (Some("ds-2".into()), "use u32".into()));
        assert_eq!(addressee("@lead: need the spec", ""), (Some("lead".into()), "need the spec".into()));
        assert_eq!(addressee("", "everyone: renderer owns style.css"), (None, "everyone: renderer owns style.css".into()));
        assert_eq!(addressee("@all", "hi"), (None, "hi".into()));
    }
}
