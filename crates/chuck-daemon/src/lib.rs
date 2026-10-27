//! Daemons: agents that sleep until something needs them. A loop that polls
//! ("is the disk full yet?") burns tokens on nothing; a daemon costs nothing
//! while it sleeps — its trigger is checked by plain code, and only when it
//! fires is an agent woken, with a quota, to look and report. Reports land
//! in the inbox. Reminders are daemons that wake nobody: they just tell you.
//!
//! The daemons and the inbox are the Python app's own files, so both see
//! the same ones. Daemons run only while Chuck is open.

pub mod probe;
pub mod shell;
pub mod when;

pub use shell::read_only_problem;
pub use when::{check, describe, detail, parse_when, Probe};

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

pub const PROMPT: &str = "\
You are a daemon of Chuck, an engineering agent: woken by a trigger while the user is away, you LOOK and \
REPORT. You change nothing: files, services and packages are read-only to you, and a command that would \
change something is refused.
Tools: put one or several fenced blocks in a reply and you get the results back:
```read <path>[:start-end]``` ```grep <regex> [path]``` ```glob <pattern>``` ```map```
```bash
<read-only command>
``` (journalctl -u <unit>, df, du, git log, pacman -Q, …; services can't be reached, so read their journal)
```search <query>``` ```fetch <url>```
Find out what happened and why, briefly. Then reply with no tool block: your REPORT — the first line a \
one-line headline (\"nothing to do\" when there is nothing to do), then what you found, and the exact \
commands or changes you PROPOSE (the user will decide). Be concrete and brief.";

/// `~/x` as a path.
pub(crate) fn when_expand(p: &str) -> String {
    match (p.strip_prefix('~'), std::env::var("HOME")) {
        (Some(rest), Ok(home)) if rest.is_empty() || rest.starts_with('/') => format!("{home}{rest}"),
        _ => p.to_string(),
    }
}

pub fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

fn write_json(path: &Path, v: &Value) {
    if let Some(d) = path.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    let tmp = path.with_extension("tmp");
    if std::fs::write(&tmp, serde_json::to_vec_pretty(v).unwrap_or_default()).is_ok() {
        let _ = std::fs::rename(&tmp, path);
    }
}

/// One daemon, as stored (unknown fields the Python app wrote are kept).
#[derive(Clone, Debug, PartialEq)]
pub struct Daemon(pub Map<String, Value>);

impl Daemon {
    pub fn id(&self) -> i64 {
        self.0.get("id").and_then(Value::as_i64).unwrap_or(0)
    }
    pub fn name(&self) -> &str {
        self.0.get("name").and_then(Value::as_str).unwrap_or("")
    }
    pub fn when(&self) -> &Value {
        self.0.get("when").unwrap_or(&Value::Null)
    }
    pub fn brief(&self) -> &str {
        self.0.get("brief").and_then(Value::as_str).unwrap_or("")
    }
    pub fn folder(&self) -> &str {
        self.0.get("folder").and_then(Value::as_str).unwrap_or("")
    }
    /// "report" (look and report), "remind" (just tell the user), "act" (the
    /// Python app's: here it looks and proposes, as a report).
    pub fn kind(&self) -> &str {
        self.0.get("kind").and_then(Value::as_str).unwrap_or("report")
    }
    pub fn quota(&self) -> u64 {
        self.0.get("quota").and_then(Value::as_u64).unwrap_or(150_000)
    }
    pub fn enabled(&self) -> bool {
        self.0.get("enabled").and_then(Value::as_bool).unwrap_or(true)
    }
    pub fn last_run(&self) -> f64 {
        self.0.get("last_run").and_then(Value::as_f64).unwrap_or(0.0)
    }
    pub fn runs(&self) -> u64 {
        self.0.get("runs").and_then(Value::as_u64).unwrap_or(0)
    }
    pub fn state(&self) -> &Value {
        self.0.get("state").unwrap_or(&Value::Null)
    }
    pub fn set(&mut self, k: &str, v: Value) {
        self.0.insert(k.into(), v);
    }
}

/// The daemons, on disk (they outlive restarts).
pub struct Registry {
    pub path: PathBuf,
    pub items: Vec<Daemon>,
    next_id: i64,
}

impl Registry {
    pub fn default_path() -> PathBuf {
        chuck_store::settings::config_dir().join("daemons.json")
    }

    /// A damaged file never stops Chuck: what can't be read is left out.
    pub fn load(path: &Path) -> Registry {
        let data: Value = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(json!([]));
        let (mut next_id, list) = match &data {
            Value::Object(o) => (o.get("next_id").and_then(Value::as_i64).unwrap_or(1), o.get("items").cloned().unwrap_or(json!([]))),
            other => (1, other.clone()),
        };
        let items: Vec<Daemon> = list.as_array().into_iter().flatten().filter_map(|d| d.as_object().cloned())
            .filter(|d| d.get("id").is_some_and(Value::is_i64) && d.get("when").is_some_and(Value::is_object)
                    && d.get("brief").is_some_and(Value::is_string))
            .map(Daemon).collect();
        next_id = items.iter().map(|d| d.id() + 1).fold(next_id, i64::max);
        Registry { path: path.to_path_buf(), items, next_id }
    }

    pub fn save(&self) {
        write_json(&self.path, &json!({"next_id": self.next_id,
                                        "items": self.items.iter().map(|d| Value::Object(d.0.clone())).collect::<Vec<_>>()}));
    }

    /// A new daemon. Ids are never reused: a deleted daemon's run can't
    /// report under a new one's name.
    pub fn add(&mut self, name: &str, when: Value, brief: &str, folder: &str, kind: &str, quota: u64) -> Daemon {
        let id = self.next_id;
        self.next_id += 1;
        let name = if name.trim().is_empty() { brief.lines().next().unwrap_or("") } else { name };
        let d = Daemon(json!({"id": id, "name": name.chars().take(60).collect::<String>(), "when": when,
            "brief": brief.trim(), "folder": folder, "kind": kind, "quota": quota, "enabled": true, "last_run": 0,
            "runs": 0, "state": {}, "created": now()}).as_object().unwrap().clone());
        self.items.push(d.clone());
        self.save();
        d
    }

    pub fn get(&self, id: i64) -> Option<&Daemon> {
        self.items.iter().find(|d| d.id() == id)
    }

    pub fn get_mut(&mut self, id: i64) -> Option<&mut Daemon> {
        self.items.iter_mut().find(|d| d.id() == id)
    }

    pub fn remove(&mut self, id: i64) -> bool {
        let n = self.items.len();
        self.items.retain(|d| d.id() != id);
        self.save();
        self.items.len() < n
    }
}

/// "<when>: <what>" -> (trigger, what). Err: how to write it.
pub fn split_spec(arg: &str, now: f64) -> Result<(Value, String), String> {
    let (when, what) = arg.split_once(':').filter(|(w, _)| !w.trim().is_empty())
        // "at 17:30: call the bank" — the first colon may be the time's
        .map(|(w, rest)| {
            if regex::Regex::new(r"\d{1,2}$").unwrap().is_match(w.trim())
                && rest.chars().take(2).all(|c| c.is_ascii_digit())
            {
                let (mm, more) = rest.split_at(2);
                match more.split_once(':') {
                    Some((tail, what)) => (format!("{w}:{mm}{tail}"), what.to_string()),
                    None => (w.to_string(), rest.to_string()),
                }
            } else {
                (w.to_string(), rest.to_string())
            }
        })
        .ok_or("write it as <when>: <what>, e.g. `every day at 9: check the disk`")?;
    let what = what.trim();
    if what.is_empty() {
        return Err("say what, after the colon".into());
    }
    let trig = parse_when(&when, now)?;
    if let Some(c) = when::command(&trig) {
        let why = read_only_problem(c, chuck_sandbox::bwrap().is_none());
        if !why.is_empty() {
            return Err(format!("a trigger's command must only look ({why})"));
        }
    }
    Ok((trig, what.to_string()))
}

/// A report, filed. Newest last; the oldest go past `keep`.
pub struct Inbox {
    pub path: PathBuf,
    pub items: Vec<Map<String, Value>>,
    keep: usize,
}

impl Inbox {
    pub fn default_path() -> PathBuf {
        chuck_store::settings::config_dir().join("inbox.json")
    }

    pub fn load(path: &Path, keep: usize) -> Inbox {
        let data: Value = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(json!([]));
        let items = data.as_array().into_iter().flatten().filter_map(|i| i.as_object().cloned())
            .filter(|i| i.get("id").is_some_and(Value::is_i64)).collect();
        Inbox { path: path.to_path_buf(), items, keep }
    }

    pub fn add(&mut self, daemon: &str, why: &str, report: &str, extra: Map<String, Value>) -> i64 {
        let id = self.items.iter().filter_map(|i| i.get("id").and_then(Value::as_i64)).max().unwrap_or(0) + 1;
        let mut it = json!({"id": id, "daemon": daemon, "why": why, "report": report, "pid": null, "time": now(),
                            "unread": true}).as_object().unwrap().clone();
        it.extend(extra);
        self.items.push(it);
        if self.items.len() > self.keep {
            let cut = self.items.len() - self.keep;
            self.items.drain(..cut);
        }
        self.save();
        id
    }

    pub fn unread(&self) -> usize {
        self.items.iter().filter(|i| i.get("unread").and_then(Value::as_bool) == Some(true)).count()
    }

    pub fn mark_read(&mut self) {
        for i in &mut self.items {
            i.insert("unread".into(), json!(false));
        }
        self.save();
    }

    pub fn save(&self) {
        write_json(&self.path, &Value::Array(self.items.iter().cloned().map(Value::Object).collect()));
    }
}

/// A daemon that found nothing wrong says so in its headline, and is filed
/// without a notification: a daemon that cries wolf gets muted.
pub fn quiet(report: &str) -> bool {
    let head = report.trim().lines().next().unwrap_or("").to_lowercase();
    regex::Regex::new(r"^\W*(?:nothing to do|all (?:good|clear|fine)|no (?:issues|problems|changes|action))").unwrap()
        .is_match(&head)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn daemons_and_reports_persist() {
        let d = std::env::temp_dir().join(format!("chuck-daemon-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let p = d.join("dm.json");
        let mut r = Registry::load(&p);
        let a = r.add("", json!({"every": 3600}), "look at things\nmore", "", "report", 150_000);
        assert_eq!(a.name(), "look at things");
        let b = r.add("two", json!({"every": 3600}), "two", "", "report", 1);
        r.remove(b.id());
        let c = r.add("three", json!({"every": 3600}), "three", "", "report", 1);
        assert_eq!(c.id(), b.id() + 1, "a deleted daemon's id is never reused");
        let back = Registry::load(&p);
        assert_eq!(back.get(a.id()).unwrap().brief(), "look at things\nmore");
        assert_eq!(Registry::load(&p).add("", json!({"every": 60}), "four", "", "report", 1).id(), c.id() + 1);
        // the Python app's list form, and junk, are read without complaint
        std::fs::write(&p, r#"[{"id": 7, "when": {"every": 60}, "brief": "old", "extra": 1}, {"id": "x"}, 3]"#).unwrap();
        let old = Registry::load(&p);
        assert_eq!(old.items.len(), 1);
        assert_eq!(old.items[0].0["extra"], 1, "unknown fields are kept");
        std::fs::write(&p, "{ broken").unwrap();
        assert!(Registry::load(&p).items.is_empty());

        let ip = d.join("ib.json");
        let mut ib = Inbox::load(&ip, 3);
        for i in 0..5 {
            ib.add("d", "why", &format!("r{i}"), Map::new());
        }
        let back = Inbox::load(&ip, 3);
        assert_eq!(back.items.iter().map(|i| i["report"].as_str().unwrap()).collect::<Vec<_>>(), ["r2", "r3", "r4"]);
        assert_eq!(back.unread(), 3);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn when_and_what() {
        let now = when::local_at(2026, 2, 10, 10, 0);
        assert_eq!(split_spec("every day at 9: check the disk", now).unwrap(), (json!({"daily": "09:00"}), "check the disk".into()));
        assert_eq!(split_spec("at 17:30: call the bank", now).unwrap().1, "call the bank");
        assert_eq!(describe(&split_spec("at 17:30: call the bank", now).unwrap().0), "at Tue 10 Mar 17:30");
        assert_eq!(split_spec("in 20m: take the pizza out", now).unwrap(), (json!({"at": now + 1200.0}), "take the pizza out".into()));
        assert!(split_spec("whenever: x", now).unwrap_err().contains("say when"));
        assert!(split_spec("when `rm -rf ~/x` changes: y", now).unwrap_err().contains("must only look"));
        assert!(split_spec("every 1h", now).is_err());
        assert!(quiet("Nothing to do: all services are fine.") && quiet("All good") && !quiet("nginx failed"));
    }
}
