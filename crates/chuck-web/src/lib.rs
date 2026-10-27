//! The web, for research: search across engines and read pages as text.
//!
//! - Search asks SearXNG (which aggregates Brave, Google, DuckDuckGo and
//!   Bing): the user's own instance first, then up to four public ones AT
//!   ONCE, the first usable answer winning. DuckDuckGo's HTML page is the
//!   fallback. Results are deduplicated and capped at two per site, so
//!   "cross-check" means genuinely different outlets.
//! - A fetch reads http and https only, never a local or private address
//!   (checked on every redirect, too), at most a few megabytes, and says
//!   honestly why a page read nothing: a bot wall, a page that is empty
//!   until its scripts run, not a page at all, or unreachable.
//! - Identical reads within ten minutes are served from memory, so paging
//!   through a long page or re-reading a source costs nothing.

pub mod browser;
pub mod camoufox;
pub mod html;
pub mod media;

use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::net::{IpAddr, ToSocketAddrs};
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

pub const UA: &str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

/// Nothing a reader wants is bigger than this; a larger "page" is a download.
pub const MAX_PAGE_BYTES: usize = 3_000_000;
const MAX_SEARCH_BYTES: usize = 4_000_000;
/// Readable text kept per page (it is paged out to agents a window at a time).
const MAX_TEXT: usize = 400_000;
const CACHE_TTL: Duration = Duration::from_secs(600);
const CACHE_MAX: usize = 64;
const MAX_REDIRECTS: usize = 6;

/// Public SearXNG instances (JSON API).
pub const SEARX_INSTANCES: &[&str] = &[
    "https://searx.be", "https://search.inetol.net", "https://priv.au", "https://searx.tiekoetter.com",
    "https://search.rhscz.eu", "https://searxng.site", "https://opnxng.com", "https://search.bus-hit.me",
];
pub const DDG_HTML: &[&str] = &["https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"];

#[derive(Clone, Debug, PartialEq)]
pub enum Proxy {
    /// HTTPS_PROXY / HTTP_PROXY / NO_PROXY from the environment.
    Env,
    None,
    Url(String),
}

#[derive(Clone, Debug)]
pub struct Config {
    /// The user's own SearXNG: asked first, alone.
    pub searx_own: Option<String>,
    pub searx: Vec<String>,
    pub ddg: Vec<String>,
    pub proxy: Proxy,
    /// Let fetches reach loopback and private addresses (tests only: an
    /// agent reading the router's admin page or a local service is how a
    /// prompt injected into a web page reaches inside the network).
    pub allow_private: bool,
    pub timeout: Duration,
    /// A page a plain read can't (empty until its scripts run, or behind a
    /// bot check) is read in a real browser: which one ("auto", "brave",
    /// "chromium", "chrome" or a path). None: never.
    pub browser: Option<String>,
}

impl Default for Config {
    fn default() -> Config {
        Config {
            searx_own: None,
            searx: SEARX_INSTANCES.iter().map(|s| s.to_string()).collect(),
            ddg: DDG_HTML.iter().map(|s| s.to_string()).collect(),
            proxy: Proxy::Env,
            allow_private: false,
            timeout: Duration::from_secs(8),
            browser: Some("auto".into()),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Hit {
    pub title: String,
    pub url: String,
    pub snippet: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Page {
    /// Where the page ended up, after redirects.
    pub url: String,
    pub title: Option<String>,
    pub text: String,
    /// Read in a real browser ("Chromium"), not by a plain fetch.
    pub via: Option<String>,
}

/// Why a fetch read nothing.
#[derive(Clone, Debug, PartialEq)]
pub enum Why {
    /// Not something this may read: another scheme, a local address.
    Refused(String),
    /// A bot wall or a refusal status (401, 403, 429…).
    Blocked(u16),
    /// The page is empty until its JavaScript runs.
    Script,
    /// Not page text: a PDF, an image, an archive.
    NotText(String),
    Unreachable(String),
}

impl std::fmt::Display for Why {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            Why::Refused(r) => write!(f, "refused: {r}"),
            Why::Blocked(0) => write!(f, "blocked: the site shows a bot check instead of the page"),
            Why::Blocked(s) => write!(f, "blocked: the site refused the request (HTTP {s})"),
            Why::Script => write!(f, "the page is empty until its JavaScript runs, so a plain read sees nothing"),
            Why::NotText(t) => write!(f, "not a page of text ({t}) — download it with a command if you need it"),
            Why::Unreachable(e) => write!(f, "unreachable: {e}"),
        }
    }
}

/// A read, and when it was made.
type Cached = (Instant, Result<Page, Why>);

pub struct Web {
    cfg: Config,
    agent: ureq::Agent,
    cache: Mutex<HashMap<String, Cached>>,
    /// Browsers open at once for reads (each is a few hundred MB).
    slots: Arc<(Mutex<usize>, std::sync::Condvar)>,
}

const BROWSER_SLOTS: usize = 2;

fn agent(cfg: &Config, timeout: Duration) -> ureq::Agent {
    let proxy = match &cfg.proxy {
        Proxy::Env => ureq::Proxy::try_from_env(),
        Proxy::None => None,
        Proxy::Url(u) => ureq::Proxy::new(u).ok(),
    };
    ureq::Agent::config_builder()
        .http_status_as_error(false)
        .max_redirects(0)
        .user_agent(UA)
        .timeout_global(Some(timeout))
        .timeout_connect(Some(timeout.min(Duration::from_secs(6))))
        .proxy(proxy)
        // the system's trust store, as every other program here uses (and a
        // company's inspecting proxy needs), not a bundled copy of Mozilla's
        .tls_config(ureq::tls::TlsConfig::builder().root_certs(ureq::tls::RootCerts::PlatformVerifier).build())
        .build()
        .new_agent()
}

impl Web {
    pub fn new(cfg: Config) -> Web {
        let agent = agent(&cfg, cfg.timeout);
        Web { cfg, agent, cache: Mutex::new(HashMap::new()), slots: Arc::default() }
    }

    pub fn config(&self) -> &Config {
        &self.cfg
    }

    // ── search ──────────────────────────────────────────────────────────

    /// Up to `n` results, at most two per site.
    pub fn search(&self, query: &str, n: usize) -> Result<Vec<Hit>, String> {
        let query = query.trim();
        if query.is_empty() {
            return Err("no query given".into());
        }
        let mut pool = self.searx(query, n + 6, "general");
        if pool.len() < 3 {
            let seen: HashSet<String> = pool.iter().map(|h| h.url.clone()).collect();
            pool.extend(self.ddg(query, n + 6).into_iter().filter(|h| !seen.contains(&h.url)));
        }
        if pool.is_empty() {
            return Err("no search engine answered (offline, or every engine refused)".into());
        }
        Ok(diverse(pool, n))
    }

    fn searx(&self, query: &str, n: usize, categories: &str) -> Vec<Hit> {
        self.searx_with(query, n, categories, hits_of)
    }

    /// SearXNG, the user's own instance first, then four public ones at
    /// once; `parse` turns an answer into results (empty: ask the next).
    pub(crate) fn searx_with<T: Send + 'static>(&self, query: &str, n: usize, categories: &str,
                                                parse: fn(&serde_json::Value, usize) -> Vec<T>) -> Vec<T> {
        let searcher = Searcher { agent: self.agent.clone() };
        if let Some(own) = self.cfg.searx_own.as_deref().map(str::trim).filter(|s| !s.is_empty()) {
            if let Ok(h) = searcher.searx_json(own, query, categories).map(|v| parse(&v, n)) {
                if !h.is_empty() {
                    return h;
                }
            }
        }
        // the public list rotates, so one host isn't asked every time
        let mut hosts = self.cfg.searx.clone();
        if !hosts.is_empty() {
            let k = rotation() % hosts.len();
            hosts.rotate_left(k);
        }
        hosts.truncate(4);
        if hosts.is_empty() {
            return Vec::new();
        }
        let (tx, rx) = channel();
        let me = Arc::new(searcher);
        for h in hosts {
            let (tx, me, q, c) = (tx.clone(), me.clone(), query.to_string(), categories.to_string());
            // Not joined: a straggler finishes within its own timeout and its
            // answer is dropped. Waiting for it would undo the whole point.
            std::thread::Builder::new().name("searx".into()).spawn(move || {
                let _ = tx.send(me.searx_json(&h, &q, &c).map(|v| parse(&v, n)).unwrap_or_default());
            }).ok();
        }
        drop(tx);
        let deadline = Instant::now() + self.cfg.timeout + Duration::from_secs(1);
        while let Some(left) = deadline.checked_duration_since(Instant::now()) {
            match rx.recv_timeout(left) {
                Ok(h) if !h.is_empty() => return h,
                Ok(_) => continue,
                Err(_) => break,
            }
        }
        Vec::new()
    }

    fn ddg(&self, query: &str, n: usize) -> Vec<Hit> {
        for host in &self.cfg.ddg {
            let resp = self.agent.post(host).header("Accept", "text/html").send_form([("q", query)]);
            let Ok(mut resp) = resp else { continue };
            if resp.status().as_u16() != 200 {
                continue;
            }
            let mut body = Vec::new();
            if resp.body_mut().as_reader().take(MAX_SEARCH_BYTES as u64).read_to_end(&mut body).is_err() {
                continue;
            }
            let hits = parse_ddg(&String::from_utf8_lossy(&body), n);
            if !hits.is_empty() {
                return hits;
            }
        }
        Vec::new()
    }

    // ── fetch ───────────────────────────────────────────────────────────

    /// A page's readable text. Cached for ten minutes, failures included,
    /// so an agent paging through a long article reads it from the network
    /// once.
    pub fn fetch(&self, url: &str) -> Result<Page, Why> {
        let url = normalize(url)?;
        {
            let mut c = self.cache.lock().unwrap();
            c.retain(|_, (t, _)| t.elapsed() < CACHE_TTL);
            if let Some((_, r)) = c.get(&url) {
                return r.clone();
            }
        }
        let r = match self.fetch_now(&url) {
            Err(why @ (Why::Script | Why::Blocked(_))) => self.in_browser(&url).unwrap_or(Err(why)),
            r => r,
        };
        let mut c = self.cache.lock().unwrap();
        if c.len() >= CACHE_MAX {
            if let Some(oldest) = c.iter().min_by_key(|(_, (t, _))| *t).map(|(k, _)| k.clone()) {
                c.remove(&oldest);
            }
        }
        // a transient failure is worth retrying later; a page or a refusal isn't
        if !matches!(r, Err(Why::Unreachable(_))) {
            c.insert(url, (Instant::now(), r.clone()));
        }
        r
    }

    /// The page read in a real browser. None: no browser to read it with
    /// (the plain read's answer stands).
    fn in_browser(&self, url: &str) -> Option<Result<Page, Why>> {
        let engine = browser::pick(self.cfg.browser.as_deref()?, browser::Purpose::Read).ok()?;
        let (lock, cv) = &*self.slots;
        {
            let mut n = lock.lock().unwrap();
            let end = Instant::now() + Duration::from_secs(90);
            while *n >= BROWSER_SLOTS {
                let left = end.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    return None;
                }
                n = cv.wait_timeout(n, left).unwrap().0;
            }
            *n += 1;
        }
        let mut o = browser::Options::new(engine);
        o.net = if self.cfg.allow_private { browser::Net::Any } else { browser::Net::Public(self.cfg.proxy.clone()) };
        o.proxy = Some(self.cfg.proxy.clone()).filter(|p| *p != Proxy::Env);
        o.files = browser::Files { root: Some(PathBuf::from("/nonexistent")), hidden: Vec::new() };
        o.timeout = Duration::from_secs(45);
        let r = browser::read_page(url, &o);
        *lock.lock().unwrap() -= 1;
        cv.notify_one();
        let (r, blocked) = r.ok()?;
        let name = r.engine.clone();
        if blocked {
            return Some(Err(Why::Blocked(0)));
        }
        match r.status {
            Some(st) if BLOCK_CODES.contains(&st) => return Some(Err(Why::Blocked(st))),
            Some(st) if st >= 400 => return Some(Err(Why::Unreachable(format!("HTTP {st}")))),
            _ => {}
        }
        if r.text.trim().is_empty() {
            return None;
        }
        let text: String = r.text.chars().take(MAX_TEXT).collect();
        Some(Ok(Page { url: if r.final_url.starts_with("http") { r.final_url } else { url.into() },
                       title: Some(r.title).filter(|t| !t.trim().is_empty()), text, via: Some(name) }))
    }

    fn fetch_now(&self, start: &str) -> Result<Page, Why> {
        let mut url = start.to_string();
        for _ in 0..=MAX_REDIRECTS {
            self.address_ok(&url)?;
            let mut resp = self.agent.get(&url)
                .header("Accept", "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5")
                .header("Accept-Language", "en-US,en;q=0.8")
                .call().map_err(|e| Why::Unreachable(short_err(&e)))?;
            let status = resp.status().as_u16();
            if (300..400).contains(&status) {
                let loc = resp.headers().get("location").and_then(|v| v.to_str().ok())
                    .ok_or_else(|| Why::Unreachable(format!("HTTP {status} without a Location")))?;
                url = normalize(&join(&url, loc))?;
                continue;
            }
            if BLOCK_CODES.contains(&status) {
                return Err(Why::Blocked(status));
            }
            if status >= 400 {
                return Err(Why::Unreachable(format!("HTTP {status}")));
            }
            let ctype = resp.headers().get("content-type").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
            let mime = ctype.split(';').next().unwrap_or("").trim().to_ascii_lowercase();
            if !mime.is_empty() && !texty(&mime) {
                return Err(Why::NotText(mime));
            }
            let mut body = Vec::new();
            resp.body_mut().as_reader().take(MAX_PAGE_BYTES as u64).read_to_end(&mut body)
                .map_err(|e| Why::Unreachable(e.to_string()))?;
            let raw = html::decode(&body, &ctype);
            return read(&url, &raw, &mime);
        }
        Err(Why::Unreachable(format!("more than {MAX_REDIRECTS} redirects")))
    }

    fn address_ok(&self, url: &str) -> Result<(), Why> {
        if self.cfg.allow_private {
            return Ok(());
        }
        public_address(url, &self.cfg.proxy)
    }
}

/// Never a local or private address, by name or by what the name resolves
/// to. Behind a proxy the proxy resolves names, so only the literal ones can
/// be judged here.
pub fn public_address(url: &str, proxy: &Proxy) -> Result<(), Why> {
    let (host, port) = host_port(url).ok_or_else(|| Why::Refused("no host in the address".into()))?;
    let bare = host.trim_start_matches('[').trim_end_matches(']');
    let refuse = || Why::Refused(format!("{host} is a local or private address; web reads stay on the public internet"));
    if let Ok(ip) = bare.parse::<IpAddr>() {
        return if private(ip) { Err(refuse()) } else { Ok(()) };
    }
    let name = bare.to_ascii_lowercase();
    let name = name.trim_end_matches('.');
    if name == "localhost" || [".localhost", ".local", ".internal", ".lan", ".home.arpa"].iter().any(|s| name.ends_with(s))
        || !name.contains('.')
    {
        return Err(refuse());
    }
    if *proxy == Proxy::None || ureq::Proxy::try_from_env().is_none() {
        if let Ok(addrs) = (name, port).to_socket_addrs() {
            if addrs.into_iter().any(|a| private(a.ip())) {
                return Err(refuse());
            }
        }
    }
    Ok(())
}

/// The search half, shareable with the threads that ask instances at once.
struct Searcher {
    agent: ureq::Agent,
}

/// Web results from a SearXNG answer.
fn hits_of(v: &serde_json::Value, n: usize) -> Vec<Hit> {
    v.get("results").and_then(|r| r.as_array()).map(|rs| {
        rs.iter().take(n * 2).filter_map(|r| {
            let u = r.get("url")?.as_str()?;
            u.starts_with("http").then(|| Hit {
                title: html::strip(r.get("title").and_then(|t| t.as_str()).unwrap_or("")),
                url: u.to_string(),
                snippet: html::strip(r.get("content").and_then(|t| t.as_str()).unwrap_or("")),
            })
        }).collect()
    }).unwrap_or_default()
}

impl Searcher {
    fn searx_json(&self, host: &str, query: &str, categories: &str) -> Result<serde_json::Value, String> {
        let url = format!("{}/search?format=json&safesearch=0&categories={}&q={}", host.trim_end_matches('/'),
                          enc(categories), enc(query));
        let mut resp = self.agent.get(&url).header("Accept", "application/json").call().map_err(|e| e.to_string())?;
        if resp.status().as_u16() != 200 {
            return Err(format!("HTTP {}", resp.status().as_u16()));
        }
        let mut body = Vec::new();
        resp.body_mut().as_reader().take(MAX_SEARCH_BYTES as u64).read_to_end(&mut body).map_err(|e| e.to_string())?;
        serde_json::from_slice(&body).map_err(|e| e.to_string())
    }
}

const BLOCK_CODES: &[u16] = &[401, 403, 406, 429, 451, 503];

fn texty(mime: &str) -> bool {
    mime.starts_with("text/") || matches!(mime, "application/xhtml+xml" | "application/xml" | "application/json"
        | "application/ld+json" | "application/rss+xml" | "application/atom+xml")
}

/// What a fetched body says, as a page or as why it isn't one.
fn read(url: &str, raw: &str, mime: &str) -> Result<Page, Why> {
    static WALL: OnceLock<regex::Regex> = OnceLock::new();
    static SHELL: OnceLock<regex::Regex> = OnceLock::new();
    let markup = mime.is_empty() && raw.trim_start().starts_with('<') || mime.contains("html") || mime.contains("xml");
    if !markup {
        let text = html::cut(raw, MAX_TEXT * 4).chars().take(MAX_TEXT).collect::<String>();
        return Ok(Page { url: url.into(), title: None, text: text.trim().to_string(), via: None });
    }
    let text = html::readable(raw, MAX_TEXT);
    // a plain string: `\` at a line's end continues it (in a raw string it wouldn't, and the
    // alternatives after it could only match after a line break)
    let wall = WALL.get_or_init(|| regex::Regex::new(
        "(?i)<title>\\s*(?:just a moment\\.\\.\\.|attention required|access denied|are you a robot|security check|please wait)\
         |cf-browser-verification|challenge-platform|cf_chl_opt|captcha-delivery\\.com|px-captcha|_incapsula_resource\
         |enable javascript and cookies to continue").unwrap());
    if wall.is_match(html::cut(raw, 200_000)) && text.chars().count() < 1500 {
        return Err(Why::Blocked(0));
    }
    let shell = SHELL.get_or_init(|| regex::Regex::new(
        r#"(?i)<div id="(?:root|app|__next|__nuxt|svelte)"\s*>\s*</div>|<noscript>[^<]{0,200}(?:enable|requires?) javascript"#).unwrap());
    if text.chars().count() < 200 && (shell.is_match(raw) || raw.matches("<script").count() >= 3) {
        return Err(Why::Script);
    }
    Ok(Page { url: url.into(), title: html::title(raw), text, via: None })
}

/// http(s) only; a bare "example.com/x" is read as https.
pub fn normalize(url: &str) -> Result<String, Why> {
    let u = url.trim().trim_matches(['<', '>', '"', '\'', '`']);
    let lower = u.to_ascii_lowercase();
    if lower.starts_with("http://") || lower.starts_with("https://") {
        return Ok(u.split('#').next().unwrap_or(u).to_string());
    }
    if let Some((scheme, _)) = u.split_once("://") {
        return Err(Why::Refused(format!("only http and https addresses are read, not {scheme}:")));
    }
    if let Some(scheme) = ["file:", "ftp:", "data:", "javascript:", "gopher:", "mailto:", "about:", "blob:"]
        .iter().find(|s| lower.starts_with(*s))
    {
        return Err(Why::Refused(format!("only http and https addresses are read, not {scheme}")));
    }
    if u.is_empty() || u.contains(char::is_whitespace) || !u.split('/').next().unwrap_or("").contains('.') {
        return Err(Why::Refused(format!("`{}` isn't a web address", html::cut(u, 80))));
    }
    Ok(format!("https://{}", u.split('#').next().unwrap_or(u)))
}

/// (host, port) of an http(s) URL.
pub fn host_port(url: &str) -> Option<(String, u16)> {
    let (scheme, rest) = url.split_once("://")?;
    let auth = rest.split(['/', '?', '#']).next()?;
    let auth = auth.rsplit('@').next()?; // user:pass@ never decides the host
    let default = if scheme.eq_ignore_ascii_case("https") { 443 } else { 80 };
    if let Some(v6) = auth.strip_prefix('[') {
        let (h, tail) = v6.split_once(']')?;
        let port = tail.strip_prefix(':').and_then(|p| p.parse().ok()).unwrap_or(default);
        return Some((format!("[{h}]"), port));
    }
    match auth.rsplit_once(':') {
        Some((h, p)) if p.chars().all(|c| c.is_ascii_digit()) && !p.is_empty() => Some((h.to_string(), p.parse().ok()?)),
        _ => (!auth.is_empty()).then(|| (auth.to_string(), default)),
    }
}

/// Loopback, private, link-local, carrier-grade NAT, unspecified, multicast.
pub fn private(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            let o = v.octets();
            v.is_loopback() || v.is_private() || v.is_link_local() || v.is_unspecified() || v.is_broadcast()
                || v.is_multicast() || o[0] == 0 || (o[0] == 100 && (64..128).contains(&o[1]))
                || (o[0] == 192 && o[1] == 0 && o[2] == 0) || o[0] >= 240
        }
        IpAddr::V6(v) => {
            if let Some(v4) = v.to_ipv4_mapped() {
                return private(IpAddr::V4(v4));
            }
            let s = v.segments();
            v.is_loopback() || v.is_unspecified() || v.is_multicast() || (s[0] & 0xfe00) == 0xfc00
                || (s[0] & 0xffc0) == 0xfe80
        }
    }
}

/// A Location header resolved against the page it came from.
pub fn join(base: &str, loc: &str) -> String {
    let loc = loc.trim();
    if loc.contains("://") {
        return loc.to_string();
    }
    let (scheme, rest) = base.split_once("://").unwrap_or(("https", base));
    if let Some(r) = loc.strip_prefix("//") {
        return format!("{scheme}://{r}");
    }
    let auth = rest.split(['/', '?', '#']).next().unwrap_or("");
    if loc.starts_with('/') {
        return format!("{scheme}://{auth}{loc}");
    }
    let path = &rest[auth.len()..];
    let path = path.split(['?', '#']).next().unwrap_or("");
    let dir = path.rsplit_once('/').map_or("", |(d, _)| d);
    format!("{scheme}://{auth}{dir}/{loc}")
}

/// Percent-encoding for a query parameter.
pub fn enc(s: &str) -> String {
    let mut o = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => o.push(b as char),
            _ => o.push_str(&format!("%{b:02X}")),
        }
    }
    o
}

pub fn dec(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() && b[i + 1].is_ascii_hexdigit() && b[i + 2].is_ascii_hexdigit() => {
                let hex = |c: u8| (c as char).to_digit(16).unwrap_or(0) as u8;
                out.push(hex(b[i + 1]) * 16 + hex(b[i + 2]));
                i += 3;
                continue;
            }
            b'+' => out.push(b' '),
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

pub fn domain(url: &str) -> String {
    host_port(url).map(|(h, _)| h.to_ascii_lowercase().trim_start_matches("www.").to_string()).unwrap_or_default()
}

/// Deduplicated, at most two per site.
pub fn diverse(pool: Vec<Hit>, n: usize) -> Vec<Hit> {
    let mut per: HashMap<String, usize> = HashMap::new();
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for h in pool {
        let key = h.url.trim_end_matches('/').to_string();
        if !seen.insert(key) {
            continue;
        }
        let d = per.entry(domain(&h.url)).or_insert(0);
        if *d >= 2 {
            continue;
        }
        *d += 1;
        out.push(h);
        if out.len() >= n {
            break;
        }
    }
    out
}

/// DuckDuckGo's HTML and lite result pages. Each link takes the snippet
/// that follows it and comes before the next link, so a result without a
/// snippet never shifts every later snippet onto the wrong result.
pub fn parse_ddg(page: &str, n: usize) -> Vec<Hit> {
    static A: OnceLock<regex::Regex> = OnceLock::new();
    static SN: OnceLock<regex::Regex> = OnceLock::new();
    static HREF: OnceLock<regex::Regex> = OnceLock::new();
    let a = A.get_or_init(|| regex::Regex::new(
        r#"(?is)<a\b([^>]*\bclass=["'](?:result__a|result-link)["'][^>]*)>(.*?)</a>"#).unwrap());
    let sn = SN.get_or_init(|| regex::Regex::new(
        r#"(?is)class=["']result(?:__|-)snippet["'][^>]*>(.*?)</(?:a|td|div)>"#).unwrap());
    let href = HREF.get_or_init(|| regex::Regex::new(r#"(?i)href=["']([^"']+)["']"#).unwrap());
    let links: Vec<_> = a.captures_iter(page).collect();
    let mut out = Vec::new();
    for (i, c) in links.iter().enumerate() {
        let Some(h) = href.captures(&c[1]) else { continue };
        let end = links.get(i + 1).map_or(page.len(), |nx| nx.get(0).unwrap().start());
        let between = &page[c.get(0).unwrap().end()..end];
        let snippet = sn.captures(between).map(|s| html::strip(&s[1])).unwrap_or_default();
        let raw = html::unescape(&h[1]);
        let mut real = raw.split_once("uddg=").map(|(_, r)| dec(r.split('&').next().unwrap_or(""))).unwrap_or(raw);
        if real.starts_with("//") {
            real = format!("https:{real}");
        }
        if !real.starts_with("http") || domain(&real).ends_with("duckduckgo.com") {
            continue; // an ad, or a link back into the engine
        }
        out.push(Hit { title: html::strip(&c[2]), url: real, snippet });
        if out.len() >= n {
            break;
        }
    }
    out
}

fn rotation() -> usize {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.subsec_nanos() as usize ^ d.as_secs() as usize)
}

fn short_err(e: &ureq::Error) -> String {
    let s = e.to_string();
    html::cut(&s, 200).to_string()
}

/// One window of a page's text, for an agent: `page` counts from 1.
pub fn window(text: &str, page: usize, per: usize) -> (String, usize) {
    let chars: Vec<char> = text.chars().collect();
    let pages = chars.len().div_ceil(per).max(1);
    let page = page.clamp(1, pages);
    let from = (page - 1) * per;
    (chars[from..(from + per).min(chars.len())].iter().collect(), pages)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn addresses() {
        assert_eq!(normalize("example.com/a#frag").unwrap(), "https://example.com/a");
        assert_eq!(normalize(" <https://x.org/p?q=1> ").unwrap(), "https://x.org/p?q=1");
        for bad in ["file:///etc/passwd", "ftp://x.org/f", "javascript:alert(1)", "data:text/html,hi", "hello world", "nodots"] {
            assert!(matches!(normalize(bad), Err(Why::Refused(_))), "{bad}");
        }
        assert_eq!(host_port("https://u:p@Example.com:8443/x"), Some(("Example.com".into(), 8443)));
        assert_eq!(host_port("http://[::1]/"), Some(("[::1]".into(), 80)));
        assert_eq!(join("https://a.org/x/y.html?q", "z.html"), "https://a.org/x/z.html");
        assert_eq!(join("https://a.org/x/y", "/r"), "https://a.org/r");
        assert_eq!(join("https://a.org/x", "//b.org/c"), "https://b.org/c");
        assert_eq!(dec("https%3A%2F%2Fa.org%2Fx%3Fy%3D1+2"), "https://a.org/x?y=1 2");
        assert_eq!(dec("100%"), "100%");
        assert_eq!(enc("a b&c=d/é"), "a%20b%26c%3Dd%2F%C3%A9");
    }

    #[test]
    fn bot_walls_are_seen_whatever_the_marker() {
        for raw in ["<html><title>Just a moment...</title></html>", "<div class=\"cf-browser-verification\"></div>",
                    "<p>Enable JavaScript and cookies to continue</p>", "<script src=/_Incapsula_Resource></script>"] {
            assert!(matches!(read("https://x.org/", raw, "text/html"), Err(Why::Blocked(0))), "{raw}");
        }
        assert!(read("https://x.org/", "<title>Moments</title><p>just a moment of your time</p>", "text/html").is_ok());
    }

    #[test]
    fn private_addresses_are_never_read() {
        let w = Web::new(Config { proxy: Proxy::None, ..Config::default() });
        for u in ["http://127.0.0.1:8080/", "http://localhost/", "http://10.0.0.1/", "http://192.168.1.1/admin",
                  "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[fd00::1]/", "http://router/",
                  "http://printer.local/", "http://0.0.0.0/", "http://100.64.0.1/", "http://[::ffff:127.0.0.1]/",
                  "https://me@127.0.0.1/", "http://metadata.google.internal/"] {
            assert!(matches!(w.fetch(u), Err(Why::Refused(_))), "{u}");
        }
        assert!(!private("93.184.216.34".parse().unwrap()));
        assert!(!private("2606:4700::1111".parse().unwrap()));
    }

    #[test]
    fn two_per_site() {
        let h = |u: &str| Hit { title: u.into(), url: u.into(), snippet: String::new() };
        let got = diverse(vec![h("https://a.org/1"), h("https://www.a.org/2"), h("https://a.org/3"), h("https://b.org/"),
                               h("https://b.org"), h("https://c.org/")], 10);
        let urls: Vec<_> = got.iter().map(|h| h.url.as_str()).collect();
        assert_eq!(urls, ["https://a.org/1", "https://www.a.org/2", "https://b.org/", "https://c.org/"]);
    }

    #[test]
    fn duckduckgo_results_keep_their_own_snippets() {
        let page = r##"
          <div class="result results_links"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fone.org%2Fa&amp;rut=x">One &amp; only</a>
            <a class="result__snippet" href="#">first snippet</a></div>
          <div class="result"><a class="result__a" href="https://two.org/b">Two</a></div>
          <div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a><a class="result__snippet">ad</a></div>
          <div class="result"><a href="https://three.org/c" class="result__a">Three</a><div class="result__snippet">third <b>snippet</b></div></div>"##;
        let got = parse_ddg(page, 10);
        assert_eq!(got, vec![
            Hit { title: "One & only".into(), url: "https://one.org/a".into(), snippet: "first snippet".into() },
            Hit { title: "Two".into(), url: "https://two.org/b".into(), snippet: "".into() },
            Hit { title: "Three".into(), url: "https://three.org/c".into(), snippet: "third snippet".into() },
        ]);
    }

    #[test]
    fn pages_in_windows() {
        let (w, n) = window("abcdefghij", 2, 4);
        assert_eq!((w.as_str(), n), ("efgh", 3));
        assert_eq!(window("abc", 9, 4), ("abc".into(), 1));
        assert_eq!(window("", 1, 4), ("".into(), 1));
    }
}
