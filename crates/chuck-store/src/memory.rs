//! Chuck's long-term memory: durable, relevance-scoped, prompt-safe — the
//! Python app's memory, in its own file (~/.local/share/chucknorris/memory/
//! facts.jsonl), so what it remembered carries over.
//!
//! Two goals: real facts (the user's hardware, preferences, projects,
//! standing instructions) survive across chats and come back when relevant;
//! and the store is NEVER dumped into a prompt — each turn gets a few
//! relevance-scored lines plus a small always-on core.
//!
//! Recall: IDF-weighted overlap of crude stems, gated on how much of the
//! question matched, a small category→instance bridge ("distro" finds
//! "CachyOS"), weight, hits and recency. Nothing is stored silently: agents
//! use ```remember``` and ```forget```, and you can see every fact.

use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

const STOP: &str = "the a an and or but if then of to in on at for with by is are was were be been \
    being it its this that these those i you he she they we me my your his her their \
    our as so do does did has have had will would can could should he's i'm you're \
    what when where who why how which not no yes get got make made use used \
    like likes liked want wants need needs thing things stuff really very just \
    about into from over under after before some any all one two more most much \
    than there here now new old good bad best better also only even still \
    please thanks ok okay sure fine yeah nah lot lots bit";
const MAX_INJECT: usize = 6;
const MAX_CORE: usize = 4;
const COVERAGE_FLOOR: f64 = 0.34;
const RARE_TERM_SCORE: f64 = 1.2;
const MAX_FACTS: usize = 400;
const HIT_FLUSH_SECS: f64 = 120.0;
const BRIDGE_WEIGHT: f64 = 0.5;

const RELATED: &[(&str, &str)] = &[
    ("distro", "cachyos arch linux endeavouros manjaro fedora debian ubuntu nixos"),
    ("os", "cachyos arch linux distro kernel"),
    ("laptop", "thinkpad x395 framework macbook notebook dell lenovo asus"),
    ("machine", "laptop desktop thinkpad server box rig"),
    ("computer", "laptop desktop thinkpad machine box"),
    ("pc", "laptop desktop machine rig"),
    ("gpu", "nvidia amdgpu radeon intel graphics driver dkms nouveau"),
    ("graphics", "nvidia amdgpu radeon gpu driver"),
    ("cpu", "ryzen intel amd processor core"),
    ("network", "subnet ip vlan router wifi ethernet dns gateway lan"),
    ("wifi", "wireless wlan ssid network"),
    ("shell", "bash zsh fish terminal prompt"),
    ("editor", "vim neovim nvim emacs helix nano vscode"),
    ("browser", "firefox brave chromium librewolf"),
    ("terminal", "kitty alacritty foot wezterm konsole shell"),
    ("desktop", "kde plasma gnome hyprland sway i3 xfce wayland"),
    ("package", "pacman paru yay aur repo"),
    ("disk", "nvme ssd hdd btrfs ext4 zfs partition filesystem"),
    ("filesystem", "btrfs ext4 zfs xfs disk partition"),
    ("language", "python rust go bash javascript"),
    ("handle", "username nickname github alias"),
    ("repo", "github gitlab repository git"),
    ("project", "repo tool app build"),
    ("job", "work role employer"),
    ("phone", "android pixel samsung iphone"),
    ("vpn", "mullvad wireguard proxy tailscale"),
    ("firewall", "iptables nftables ufw firewalld"),
];

fn stop() -> &'static HashSet<&'static str> {
    static S: OnceLock<HashSet<&'static str>> = OnceLock::new();
    S.get_or_init(|| STOP.split_whitespace().collect())
}

/// Content words, in order of first appearance.
fn ordered_tokens(text: &str) -> Vec<String> {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    let r = R.get_or_init(|| regex::Regex::new(r"[a-z0-9][a-z0-9_+.-]{1,}").unwrap());
    let low = text.to_lowercase();
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for m in r.find_iter(&low) {
        let w = m.as_str();
        if !stop().contains(w) && w.chars().count() > 2 && seen.insert(w.to_string()) {
            out.push(w.to_string());
        }
    }
    out
}

fn tokens(text: &str) -> HashSet<String> {
    ordered_tokens(text).into_iter().collect()
}

/// Crude suffix stripping, deliberately conservative ("installing" finds
/// "install"; "trees" isn't cut to "tre").
pub fn stems(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    for mut t in tokens(text) {
        let n = t.chars().count();
        let ends = |s: &str| t.ends_with(s);
        if n > 5 && ends("ing") {
            t.truncate(t.len() - 3);
        } else if n > 4 && (ends("ed") || ends("es") && t.chars().rev().nth(2).is_some_and(|c| "sxzhi".contains(c))) {
            // 'es' only after a sibilant: "boxes" -> "box", "trees" stays "tree"
            t.truncate(t.len() - 2);
        } else if n > 3 && ends("s") && !ends("ss") {
            t.truncate(t.len() - 1);
        }
        out.push(t);
    }
    out
}

fn related() -> &'static HashMap<String, HashSet<String>> {
    static R: OnceLock<HashMap<String, HashSet<String>>> = OnceLock::new();
    R.get_or_init(|| {
        let mut m: HashMap<String, HashSet<String>> = HashMap::new();
        for (k, v) in RELATED {
            let vs: HashSet<String> = stems(v).into_iter().collect();
            for kk in stems(k) {
                m.entry(kk).or_default().extend(vs.iter().cloned());
            }
        }
        m
    })
}

fn bridge(cstem: &HashSet<String>) -> HashSet<String> {
    let mut out = HashSet::new();
    for t in cstem {
        if let Some(r) = related().get(t) {
            out.extend(r.iter().cloned());
        }
    }
    out.retain(|t| !cstem.contains(t));
    out
}

fn now() -> f64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())
}

fn norm(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// One fact as stored (fields the Python app wrote are all kept).
#[derive(Clone, Debug, PartialEq)]
pub struct Fact(pub Map<String, Value>);

impl Fact {
    pub fn text(&self) -> &str {
        self.0.get("text").and_then(Value::as_str).unwrap_or("")
    }
    pub fn kind(&self) -> &str {
        self.0.get("kind").and_then(Value::as_str).unwrap_or("fact")
    }
    pub fn scope(&self) -> &str {
        self.0.get("scope").and_then(Value::as_str).unwrap_or("")
    }
    fn num(&self, k: &str, d: f64) -> f64 {
        self.0.get(k).and_then(Value::as_f64).unwrap_or(d)
    }
    pub fn ts(&self) -> f64 {
        self.num("ts", 0.0)
    }
    pub fn id(&self) -> Value {
        self.0.get("id").cloned().unwrap_or(Value::Null)
    }
}

#[derive(Default)]
struct Cache {
    sig: Option<(u128, u64)>,
    facts: Vec<Fact>,
    last_flush: f64,
}

/// The store. One per process is enough; it's safe to share.
pub struct Memory {
    pub path: PathBuf,
    cache: Mutex<Cache>,
}

impl Memory {
    pub fn default_path() -> PathBuf {
        crate::ledger::data_dir().join("memory").join("facts.jsonl")
    }

    pub fn at(path: &Path) -> Memory {
        Memory { path: path.to_path_buf(), cache: Mutex::default() }
    }

    fn sig(&self) -> Option<(u128, u64)> {
        let m = self.path.metadata().ok()?;
        Some((m.modified().ok()?.duration_since(std::time::UNIX_EPOCH).ok()?.as_nanos(), m.len()))
    }

    /// The facts, read again only when the file changed. A damaged line is
    /// skipped; a partial one is completed.
    fn load(&self, c: &mut Cache) -> Vec<Fact> {
        let sig = self.sig();
        if sig.is_some() && c.sig == sig {
            return c.facts.clone();
        }
        let mut out = Vec::new();
        if let Ok(text) = std::fs::read_to_string(&self.path) {
            for line in text.lines().map(str::trim).filter(|l| !l.is_empty()) {
                let Ok(Value::Object(mut f)) = serde_json::from_str::<Value>(line) else { continue };
                let Some(t) = f.get("text").and_then(Value::as_str).map(String::from) else { continue };
                let id_ok = f.get("id").is_some_and(|i| i.is_i64() || i.is_u64() || i.as_str().is_some_and(|s| !s.is_empty()));
                if !id_ok {
                    let h = crate::checkpoint::sha1_hex(t.as_bytes());
                    f.insert("id".into(), json!(u64::from_str_radix(&h[..8], 16).unwrap_or(0) % 1_000_000_000));
                }
                let kind = f.get("kind").and_then(Value::as_str).filter(|k| !k.is_empty()).unwrap_or("fact").to_string();
                f.insert("kind".into(), json!(kind));
                for (k, d) in [("ts", 0.0), ("weight", 1.0), ("hits", 0.0)] {
                    if !f.get(k).is_some_and(|v| v.is_number()) {
                        f.insert(k.into(), json!(d));
                    }
                }
                out.push(Fact(f));
            }
        }
        c.sig = sig;
        c.facts = out.clone();
        out
    }

    fn save_all(&self, c: &mut Cache, facts: Vec<Fact>) {
        if let Some(d) = self.path.parent() {
            let _ = std::fs::create_dir_all(d);
        }
        let text = facts.iter().map(|f| serde_json::to_string(&Value::Object(f.0.clone())).unwrap_or_default())
            .collect::<Vec<_>>().join("\n");
        let tmp = self.path.with_extension("tmp");
        if std::fs::write(&tmp, text).is_ok() && std::fs::rename(&tmp, &self.path).is_ok() {
            c.sig = self.sig();
            c.facts = facts;
        }
    }

    fn archive(&self, old: &[Fact]) {
        use std::io::Write;
        let p = self.path.with_file_name("superseded.jsonl");
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(p) {
            for o in old {
                let mut m = o.0.clone();
                m.insert("superseded_at".into(), json!(now()));
                let _ = writeln!(f, "{}", Value::Object(m));
            }
        }
    }

    /// Store a durable fact. A near-identical one is refreshed, a corrected
    /// one replaced, and an older fact about the same subject superseded
    /// (archived, never deleted). `scope`: "" for the user, a folder for a
    /// project (recalled only there). -> (stored, what happened).
    pub fn remember(&self, text: &str, kind: &str, weight: f64, scope: &str) -> (bool, String) {
        let mut text = norm(text);
        if text.chars().count() < 3 {
            return (false, "nothing to remember".into());
        }
        if text.chars().count() > 400 {
            text = text.chars().take(400).collect();
        }
        let mut c = self.cache.lock().unwrap();
        let mut facts = self.load(&mut c);
        let ntok = tokens(&text);
        for i in 0..facts.len() {
            if facts[i].scope() != scope {
                continue;
            }
            let ft = tokens(facts[i].text());
            if ntok.is_empty() || ft.is_empty() {
                continue;
            }
            let overlap = ntok.intersection(&ft).count() as f64 / ntok.union(&ft).count().max(1) as f64;
            let w = facts[i].num("weight", 1.0).max(weight);
            if facts[i].text().to_lowercase() == text.to_lowercase() || ntok.is_subset(&ft) {
                facts[i].0.insert("ts".into(), json!(now()));
                facts[i].0.insert("weight".into(), json!(w));
                self.save_all(&mut c, facts);
                return (true, "already knew that (refreshed)".into());
            }
            if overlap > 0.8 {
                self.archive(&[facts[i].clone()]);
                facts[i].0.insert("text".into(), json!(text));
                facts[i].0.insert("ts".into(), json!(now()));
                facts[i].0.insert("weight".into(), json!(w));
                self.save_all(&mut c, facts);
                return (true, "updated what I knew".into());
            }
        }
        let old: Vec<Fact> = facts.iter().filter(|f| f.scope() == scope && f.kind() != "core" && supersedes(&text, f.text()))
            .cloned().collect();
        if !old.is_empty() {
            self.archive(&old);
            let ids: Vec<Value> = old.iter().map(Fact::id).collect();
            facts.retain(|f| !ids.contains(&f.id()));
        }
        let used: HashSet<String> = facts.iter().map(|f| f.id().to_string()).collect();
        let mut id = (now() * 1000.0) as u64 % 1_000_000_000;
        while used.contains(&id.to_string()) {
            id = (id + 1) % 1_000_000_000;
        }
        facts.push(Fact(json!({"id": id, "text": text, "kind": if kind == "core" { "core" } else { "fact" },
            "weight": weight, "ts": now(), "hits": 0, "scope": scope}).as_object().unwrap().clone()));
        if facts.len() > MAX_FACTS {
            let (core, mut rest): (Vec<Fact>, Vec<Fact>) = facts.into_iter().partition(|f| f.kind() == "core");
            rest.sort_by(|a, b| {
                let ka = (a.num("weight", 1.0) + a.num("hits", 0.0), a.ts());
                let kb = (b.num("weight", 1.0) + b.num("hits", 0.0), b.ts());
                kb.partial_cmp(&ka).unwrap_or(std::cmp::Ordering::Equal)
            });
            rest.truncate(MAX_FACTS.saturating_sub(core.len()));
            facts = core.into_iter().chain(rest).collect();
        }
        self.save_all(&mut c, facts);
        (true, if old.is_empty() { "noted".into() } else { "updated".into() })
    }

    /// Remove facts matching `query` (a substring, or most of its words).
    pub fn forget(&self, query: &str) -> (bool, String) {
        let mut c = self.cache.lock().unwrap();
        let facts = self.load(&mut c);
        if facts.is_empty() {
            return (false, "nothing stored".into());
        }
        let q = query.trim().to_lowercase();
        let qtok = tokens(query);
        let (gone, kept): (Vec<Fact>, Vec<Fact>) = facts.into_iter().partition(|f| {
            let ft = tokens(f.text());
            let overlap = if qtok.is_empty() { 0.0 } else { qtok.intersection(&ft).count() as f64 / qtok.len() as f64 };
            !q.is_empty() && (f.text().to_lowercase().contains(&q) || overlap >= 0.6)
        });
        if gone.is_empty() {
            return (false, "no match to forget".into());
        }
        self.save_all(&mut c, kept);
        (true, format!("forgot {} fact(s)", gone.len()))
    }

    /// Forget one fact by its id (the panel's remove button).
    pub fn forget_id(&self, id: &Value) -> bool {
        let mut c = self.cache.lock().unwrap();
        let mut facts = self.load(&mut c);
        let n = facts.len();
        facts.retain(|f| &f.id() != id);
        let gone = facts.len() < n;
        if gone {
            self.save_all(&mut c, facts);
        }
        gone
    }

    /// A short list of facts relevant to `context`, plus the core ones.
    /// Never the whole store.
    pub fn recall(&self, context: &str, scope: &str) -> Vec<String> {
        let mut c = self.cache.lock().unwrap();
        let facts: Vec<Fact> = self.load(&mut c).into_iter()
            .filter(|f| f.scope().is_empty() || (!scope.is_empty() && f.scope() == scope)).collect();
        if facts.is_empty() {
            return Vec::new();
        }
        let cstem: HashSet<String> = stems(context).into_iter().collect();
        let bridged = bridge(&cstem);
        let fstems: Vec<HashSet<String>> = facts.iter().map(|f| stems(f.text()).into_iter().collect()).collect();
        let n = facts.len().max(1) as f64;
        let mut df: HashMap<&str, usize> = HashMap::new();
        for s in &fstems {
            for t in s {
                *df.entry(t.as_str()).or_default() += 1;
            }
        }
        let idf: HashMap<&str, f64> = df.iter().map(|(t, c)| (*t, (1.0 + n / (1.0 + *c as f64)).ln())).collect();
        let answerable = {
            let a = cstem.iter().filter(|t| idf.contains_key(t.as_str()) || related().contains_key(*t)).count();
            (if a > 0 { a } else { cstem.len() }).max(1) as f64
        };
        let core: Vec<&Fact> = facts.iter().filter(|f| f.kind() == "core").take(MAX_CORE).collect();
        let t_now = now();
        let mut scored: Vec<(f64, usize)> = Vec::new();
        for (i, f) in facts.iter().enumerate() {
            if f.kind() == "core" {
                continue;
            }
            let ft = &fstems[i];
            let shared: HashSet<&String> = cstem.intersection(ft).collect();
            let soft: HashSet<&String> = bridged.intersection(ft).filter(|t| !shared.contains(t)).collect();
            if shared.is_empty() && soft.is_empty() {
                continue;
            }
            let w = |t: &&String| idf.get(t.as_str()).copied().unwrap_or(0.5);
            let weighted = shared.iter().map(w).sum::<f64>() + BRIDGE_WEIGHT * soft.iter().map(w).sum::<f64>();
            let coverage = (shared.len() as f64 + BRIDGE_WEIGHT * soft.len() as f64) / answerable;
            if coverage < COVERAGE_FLOOR && weighted < RARE_TERM_SCORE {
                continue;
            }
            let overlap = weighted / (1.0 + (1.0 + ft.len() as f64).ln());
            let age_days = ((t_now - f.0.get("ts").and_then(Value::as_f64).unwrap_or(t_now)) / 86400.0).max(0.0);
            let score = overlap + coverage * 0.5 + f.num("weight", 1.0) * 0.15 + (1.0 + f.num("hits", 0.0)).ln() * 0.2
                + (0.3 - age_days / 365.0).max(0.0);
            scored.push((score, i));
        }
        // stable, like Python's sort: equal scores keep their order
        scored.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        let picked: Vec<&Fact> = core.into_iter().chain(scored.iter().take(MAX_INJECT).map(|(_, i)| &facts[*i])).collect();
        // useful facts survive pruning: hits counted now, written at most every two minutes
        if !picked.is_empty() {
            let ids: Vec<Value> = picked.iter().map(|f| f.id()).collect();
            let mut all = c.facts.clone();
            for f in &mut all {
                if ids.contains(&f.id()) {
                    let h = f.num("hits", 0.0) + 1.0;
                    f.0.insert("hits".into(), json!(h as i64));
                }
            }
            if t_now - c.last_flush >= HIT_FLUSH_SECS {
                c.last_flush = t_now;
                self.save_all(&mut c, all);
            } else {
                c.facts = all;
            }
        }
        let mut seen = HashSet::new();
        picked.into_iter().map(|f| f.text().to_string()).filter(|t| seen.insert(t.clone()))
            .take(MAX_INJECT + MAX_CORE).collect()
    }

    /// What goes into a prompt ("" when nothing is relevant).
    pub fn block(&self, context: &str, scope: &str) -> String {
        let hits = self.recall(context, scope);
        if hits.is_empty() {
            return String::new();
        }
        format!("What you remember about this user (use if relevant, don't force it):\n- {}", hits.join("\n- "))
    }

    /// Every fact, newest first (for the panel).
    pub fn all(&self) -> Vec<Fact> {
        let mut c = self.cache.lock().unwrap();
        let mut f = self.load(&mut c);
        f.sort_by(|a, b| b.ts().partial_cmp(&a.ts()).unwrap_or(std::cmp::Ordering::Equal));
        f
    }

    /// Write the hit counts kept in memory (when the app closes).
    pub fn flush(&self) {
        let mut c = self.cache.lock().unwrap();
        let facts = c.facts.clone();
        if c.sig.is_some() && !facts.is_empty() {
            self.save_all(&mut c, facts);
        }
    }
}

/// Is `new` an update of `old` (same subject, much the same words), not a
/// separate fact? Conservative: a false positive drops a true fact.
fn supersedes(new: &str, old: &str) -> bool {
    let (a, b) = (ordered_tokens(new), ordered_tokens(old));
    if a.len() < 3 || b.len() < 3 || a[..2] != b[..2] {
        return false;
    }
    let (sa, sb): (HashSet<&String>, HashSet<&String>) = (a.iter().collect(), b.iter().collect());
    sa.intersection(&sb).count() as f64 / sa.union(&sb).count().max(1) as f64 >= 0.5
}
