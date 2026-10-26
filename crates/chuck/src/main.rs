//! `chuck` — the fleet from a terminal.
//!
//!   chuck run [-C DIR] [--max-usd N] [--verbose] "what to do"
//!   chuck models      the fleet's models, checked against your account
//!   chuck key         save your SiliconFlow API key (shared with the window)
//!   chuck version

mod term;

use chuck_core::{FleetConfig, Quota};
use chuck_model::{is_siliconflow, ChatModel, Reasoning, SiliconFlow, DEFAULT_BASE};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use chuck_sandbox::Access;
use chuck_store::Settings;
use std::io::{BufRead, IsTerminal, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Duration;

const VERSION: &str = env!("CARGO_PKG_VERSION");

const HELP: &str = "\
chuck — one Lead and eight workers on SiliconFlow; work ends only through the Done gate.

USAGE
  chuck run [-C DIR] [--max-usd N] [--verbose] \"what to do\"
  chuck models      show the fleet's models, checked against your account
  chuck key         save your SiliconFlow API key (shared with the Chuck window)
  chuck version

  The window is `chucknorris` while the Rust window is being built.

RUN
  -C, --dir DIR     the project folder (default: the current folder)
  --max-usd N       spending cap for this job, Lead and workers together
  -v, --verbose     also show what workers say and every command's output
  Ctrl-C stops every agent; the job ends INCOMPLETE with what is missing.

EXIT STATUS
  0 done or answered · 2 incomplete (paused, with the gaps) · 1 failed";

static INTERRUPTS: AtomicU32 = AtomicU32::new(0);

extern "C" fn on_sigint(_: libc::c_int) {
    if INTERRUPTS.fetch_add(1, Ordering::SeqCst) >= 1 {
        unsafe { libc::_exit(130) };
    }
}

fn fail(msg: &str) -> ! {
    eprintln!("chuck: {msg}");
    std::process::exit(1);
}

fn is_local(base: &str) -> bool {
    let rest = base.split("://").nth(1).unwrap_or("");
    let host = rest.split('/').next().unwrap_or("");
    let host = host.rsplit_once(':').map_or(host, |(h, p)| if p.chars().all(|c| c.is_ascii_digit()) { h } else { host });
    matches!(host, "127.0.0.1" | "localhost" | "[::1]")
}

/// The one client every agent shares. SiliconFlow only (a server on this
/// machine is allowed for testing).
fn client(s: &Settings) -> SiliconFlow {
    let Some(key) = s.api_key() else {
        fail("no SiliconFlow API key yet — run `chuck key`, or add it in the window's Settings")
    };
    let base = s.str("siliconflow_base_url").unwrap_or(DEFAULT_BASE).to_string();
    if !is_siliconflow(&base) && !is_local(&base) {
        fail(&format!("{base} isn't a SiliconFlow endpoint: Chuck's fleet talks to SiliconFlow only \
            (clear \"API base URL\" in Settings)"));
    }
    let stall = Duration::from_secs(s.num("stall_seconds", 90.0, 20.0, 600.0) as u64);
    SiliconFlow::new(&base, key, stall, is_local(&base)).unwrap_or_else(|e| fail(&e.to_string()))
}

/// The folder's access as the window stores it (`folder_access`: path → mode).
fn access_of(s: &Settings, folder: &std::path::Path) -> Access {
    let Some(map) = s.map.get("folder_access").and_then(|v| v.as_object()) else { return Access::Full };
    map.iter().find(|(k, _)| {
        let p = std::path::Path::new(k.as_str());
        p == folder || p.canonicalize().is_ok_and(|c| c == folder)
    }).and_then(|(_, v)| v.as_str()).map_or(Access::Full, Access::parse)
}

fn fleet(s: &Settings) -> FleetConfig {
    let mut f = FleetConfig::default();
    if let Some(m) = s.str("fleet_deepseek_model") {
        f.deepseek_model = m.to_string();
    }
    if let Some(m) = s.str("fleet_glm_model") {
        f.glm_model = m.to_string();
    }
    f.deepseek_workers = s.num("fleet_deepseek_workers", 4.0, 0.0, 4.0) as u8;
    f.glm_workers = s.num("fleet_glm_workers", 4.0, 0.0, 4.0) as u8;
    f
}

fn cmd_key() {
    let mut s = Settings::load();
    let tty = std::io::stdin().is_terminal();
    if tty {
        eprint!("Paste your SiliconFlow API key (input hidden): ");
        let _ = std::io::stderr().flush();
    }
    let key = read_secret(tty);
    let key = key.trim();
    if key.is_empty() {
        fail("no key given — nothing was changed");
    }
    s.set("siliconflow_api_key", serde_json::Value::String(key.to_string()));
    if let Err(e) = s.save() {
        fail(&e.to_string());
    }
    eprintln!("saved to {} (only you can read it)", s.path.display());
}

fn read_secret(tty: bool) -> String {
    let mut line = String::new();
    if !tty {
        let _ = std::io::stdin().lock().read_line(&mut line);
        return line;
    }
    unsafe {
        let mut t: libc::termios = std::mem::zeroed();
        let have = libc::tcgetattr(0, &mut t) == 0;
        let old = t;
        if have {
            t.c_lflag &= !libc::ECHO;
            libc::tcsetattr(0, libc::TCSANOW, &t);
        }
        let _ = std::io::stdin().lock().read_line(&mut line);
        if have {
            libc::tcsetattr(0, libc::TCSANOW, &old);
        }
    }
    eprintln!();
    line
}

fn cmd_models() {
    let s = Settings::load();
    let c = client(&s);
    let mut f = fleet(&s);
    let live = c.list_models().unwrap_or_else(|e| fail(&e.to_string()));
    let changes = f.resolve(&live);
    let have = |m: &str| live.iter().any(|x| x == m);
    println!("lead + {} DeepSeek workers  {}  {}", f.deepseek_workers, f.deepseek_model,
             if have(&f.deepseek_model) { "✓ on your account" } else { "✗ not on your account" });
    println!("{} GLM workers             {}  {}", f.glm_workers, f.glm_model,
             if have(&f.glm_model) { "✓ on your account" } else { "✗ not on your account" });
    for (from, to) in changes {
        println!("  {from} isn't listed; using {to}");
    }
}

struct RunArgs {
    dir: PathBuf,
    max_usd: Option<f64>,
    verbose: bool,
    task: String,
}

fn parse_run(args: &[String]) -> Result<RunArgs, String> {
    let mut a = RunArgs { dir: PathBuf::from("."), max_usd: None, verbose: false, task: String::new() };
    let mut words = Vec::new();
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "-C" | "--dir" => {
                i += 1;
                a.dir = PathBuf::from(args.get(i).ok_or("-C needs a folder")?);
            }
            "--max-usd" => {
                i += 1;
                let v: f64 = args.get(i).and_then(|v| v.parse().ok()).filter(|v: &f64| v.is_finite() && *v > 0.0)
                    .ok_or("--max-usd needs a positive number")?;
                a.max_usd = Some(v);
            }
            "-v" | "--verbose" => a.verbose = true,
            "--" => {
                words.extend(args[i + 1..].iter().cloned());
                break;
            }
            w if w.starts_with('-') && w.len() > 1 => return Err(format!("unknown option {w}")),
            w => words.push(w.to_string()),
        }
        i += 1;
    }
    a.task = words.join(" ").trim().to_string();
    if a.task.is_empty() {
        return Err("say what to do: chuck run \"fix the failing test in parser.py\"".into());
    }
    Ok(a)
}

fn cmd_run(args: &[String]) -> i32 {
    let a = parse_run(args).unwrap_or_else(|e| fail(&e));
    let s = Settings::load();
    if let Some(p) = &s.problem {
        eprintln!("chuck: {p}");
    }
    let model = client(&s);
    let mut cfg = RuntimeConfig { fleet: fleet(&s), ..RuntimeConfig::default() };
    if let Ok(live) = model.list_models() {
        for (from, to) in cfg.fleet.resolve(&live) {
            eprintln!("chuck: {from} isn't on your account; using {to}");
        }
    }
    cfg.reasoning = Reasoning {
        budget: s.num("reasoning_budget", 4096.0, 0.0, 32768.0) as u32,
        max_think_secs: s.num("max_think_seconds", 120.0, 0.0, 1800.0) as u32,
    };
    let usd = a.max_usd.unwrap_or_else(|| s.num("fleet_job_usd", 2.0, 0.01, 1000.0));
    cfg.job_quota = Quota { usd: Some(usd), ..cfg.job_quota };

    let folder = a.dir.canonicalize().unwrap_or_else(|_| a.dir.clone());
    cfg.access = access_of(&s, &folder);
    if cfg.access != Access::Full && !cfg.bwrap {
        eprintln!("chuck: this folder is {}, which needs bubblewrap to run commands — install it \
            (pacman -S bubblewrap / apt install bubblewrap); until then commands are refused", cfg.access.label());
    }
    let header = format!("lead + {} DeepSeek + {} GLM · {} ({}) · up to {}", cfg.fleet.deepseek_workers,
                         cfg.fleet.glm_workers, folder.display(), cfg.access.label(), chuck_model::fmt_usd(usd));
    let (tx, rx) = channel();
    let rt = Runtime::new(Arc::new(model), cfg, Events(Some(tx)));
    let mut job = Job::new(a.task, &a.dir);
    // the Lead's questions are answered from the keyboard; stdin is read on
    // its own thread so Ctrl-C still stops the job while a question is open
    if std::io::stdin().is_terminal() {
        let (atx, arx) = channel();
        job.answers = Some(arx);
        std::thread::spawn(move || {
            for line in std::io::stdin().lock().lines() {
                let Ok(line) = line else { break };
                if atx.send(line.trim().to_string()).is_err() {
                    break;
                }
            }
        });
    }
    let asking = job.answers.is_some();
    let stop = job.stop.clone();
    unsafe {
        libc::signal(libc::SIGINT, on_sigint as *const () as libc::sighandler_t);
    }
    let watcher_stop = stop.clone();
    let done = Arc::new(AtomicBool::new(false));
    let watcher_done = done.clone();
    std::thread::spawn(move || {
        while !watcher_done.load(Ordering::Relaxed) {
            if INTERRUPTS.load(Ordering::SeqCst) > 0 {
                watcher_stop.store(true, Ordering::SeqCst);
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    });
    let worker = std::thread::spawn(move || rt.run(job));

    let color = std::io::stdout().is_terminal() && std::env::var_os("NO_COLOR").is_none();
    let mut view = term::View::new(color, a.verbose, Box::new(std::io::stdout()));
    view.header(&header);
    for e in rx.iter() {
        view.show(&e);
        match &e {
            Event::Ask { .. } if asking => {
                print!("{} ", view.st.bold(">"));
                let _ = std::io::stdout().flush();
            }
            Event::Confirm { .. } if asking => {
                print!("{} ", view.st.bold("type yes to run it, anything else to refuse >"));
                let _ = std::io::stdout().flush();
            }
            Event::Finished(_) => break,
            _ => {}
        }
    }
    let report = worker.join().unwrap_or_else(|_| fail("the job crashed"));
    done.store(true, Ordering::Relaxed);
    match report.outcome {
        Outcome::Done { .. } | Outcome::Answered { .. } => 0,
        Outcome::Paused { .. } => 2,
        Outcome::Failed { .. } => 1,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("run") => cmd_run(&args[1..]),
        Some("models") => {
            cmd_models();
            0
        }
        Some("key") => {
            cmd_key();
            0
        }
        Some("version" | "--version" | "-V") => {
            println!("chuck {VERSION}");
            0
        }
        None | Some("help" | "--help" | "-h") => {
            println!("{HELP}");
            0
        }
        Some(other) => {
            eprintln!("chuck: unknown command `{other}`\n\n{HELP}");
            1
        }
    };
    std::process::exit(code);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v(s: &[&str]) -> Vec<String> {
        s.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn run_arguments() {
        let a = parse_run(&v(&["-C", "/tmp/p", "--max-usd", "0.5", "fix", "the", "test"])).unwrap();
        assert_eq!(a.dir, PathBuf::from("/tmp/p"));
        assert_eq!(a.max_usd, Some(0.5));
        assert_eq!(a.task, "fix the test");
        assert!(parse_run(&v(&["--max-usd", "-1", "x"])).is_err());
        assert!(parse_run(&v(&["--bogus", "x"])).is_err());
        assert!(parse_run(&v(&[])).is_err());
        assert_eq!(parse_run(&v(&["--", "-v", "means", "verbose"])).unwrap().task, "-v means verbose");
    }

    #[test]
    fn only_siliconflow_or_this_machine() {
        assert!(is_local("http://127.0.0.1:8080/v1"));
        assert!(is_local("http://localhost/v1"));
        assert!(!is_local("https://api.openai.com/v1"));
        assert!(!is_local("https://localhost.evil.com/v1"));
    }
}
