//! `chuck` — Chuck from a terminal.
//!
//!   chuck run [-C DIR] [--max-usd N] [--verbose] "what to do"
//!   chuck models      Chuck's models, checked against your account
//!   chuck key         save your SiliconFlow API key (shared with the window)
//!   chuck version

mod term;

use chuck_model::ChatModel;
use chuck_runtime::{setup, Event, Events, Job, Outcome, Runtime};
use chuck_store::{Ledger, Settings};
use std::io::{BufRead, IsTerminal, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::mpsc::channel;
use std::sync::Arc;
use std::time::Duration;

const VERSION: &str = env!("CARGO_PKG_VERSION");

const HELP: &str = "\
chuck — one fast model on SiliconFlow that builds, runs and checks; work ends only through the Done gate.

USAGE
  chuck             the window (in the folder you start it from)
  chuck run [-C DIR] [--max-usd N] [--verbose] \"what to do\"
  chuck continue [-C DIR] [--max-usd N] [\"what to add\"]   carry on a paused (INCOMPLETE)
                    job exactly where it stopped: criteria, evidence and the Lead's work kept
  chuck ask [--max-usd N] \"a question\"   Chat mode: an answer, researched on the web
                    when it should be; it changes nothing (work goes to `chuck run`)
  chuck models      show Chuck's models, checked against your account
  chuck key         save your SiliconFlow API key (shared with the Chuck window)
  chuck doctor      check everything Chuck depends on (the API, sandbox, search, voice…)
  chuck update      install the newest Chuck
  chuck voice [install]   how speaking and listening stand; `install` sets up the
                    natural voice and voice input (Kokoro + Moonshine, ~600 MB, local)
  chuck daemons [rm N | pause N | resume N]   your daemons and reminders
  chuck inbox [read]   what daemons reported (they run while the window is open)
  chuck ledger [N]  verify the evidence chain and show the last N commands run
  chuck rewind [-C DIR] [--list | N]   put the folder back to before the last job
                    (or checkpoint N); a rewind can itself be undone
  chuck version

  `chucknorris` (and Chuck in your app menu) opens the same window as `chuck`.

RUN
  -C, --dir DIR     the project folder (default: the current folder)
  --max-usd N       spending cap for this job
  -v, --verbose     also show every command's output
  The folder's access (set in the window's Settings), the daily and monthly
  spending limits, and the evidence ledger are the window's own.
  Ctrl-C stops every agent; the job ends INCOMPLETE with what is missing.

EXIT STATUS
  0 done or answered · 2 incomplete (paused, with the gaps) · 1 failed";

static INTERRUPTS: AtomicU32 = AtomicU32::new(0);

/// Only counts (a signal handler must not lock or allocate): the watcher
/// thread stops the job on the first, ends every command and exits on the
/// second; a third exits at once, whatever state things are in.
extern "C" fn on_sigint(_: libc::c_int) {
    if INTERRUPTS.fetch_add(1, Ordering::SeqCst) >= 2 {
        unsafe { libc::_exit(130) };
    }
}

fn fail(msg: &str) -> ! {
    eprintln!("chuck: {msg}");
    std::process::exit(1);
}

fn client(s: &Settings) -> chuck_model::SiliconFlow {
    setup::client(s).unwrap_or_else(|e| fail(&e))
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

fn ago(at: i64) -> String {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let s = (now - at).max(0);
    match s {
        0..=59 => "just now".into(),
        60..=3599 => format!("{} min ago", s / 60),
        3600..=86_399 => format!("{} h ago", s / 3600),
        _ => format!("{} d ago", s / 86_400),
    }
}

/// `chuck rewind [-C DIR] [--list | N | <sha>]`: the folder back to before
/// the last job (or to checkpoint N, newest = 1). Undoable: it says how.
fn cmd_rewind(args: &[String]) -> i32 {
    let mut dir = PathBuf::from(".");
    let mut target: Option<String> = None;
    let mut list = false;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "-C" | "--dir" => {
                i += 1;
                dir = PathBuf::from(args.get(i).unwrap_or_else(|| fail("-C needs a folder")));
            }
            "--list" | "-l" => list = true,
            w => target = Some(w.to_string()),
        }
        i += 1;
    }
    let sh = chuck_store::Shadow::new(&dir, &chuck_store::checkpoint::default_store())
        .unwrap_or_else(|e| fail(&e.to_string()));
    let pts = sh.points(40);
    if pts.is_empty() {
        println!("no checkpoints for {} yet — chuck takes one before every step that changes it", sh.work.display());
        return 0;
    }
    if list {
        for (n, p) in pts.iter().enumerate().take(20) {
            let differ = sh.changed_since(&p.sha).len();
            println!("{:>2}  {}  {:<12}  {}  ({})", n + 1, &p.sha[..10], ago(p.at), p.label,
                     match differ { 0 => "no files differ now".to_string(), 1 => "1 file differs now".into(),
                                    k => format!("{k} files differ now") });
        }
        return 0;
    }
    let pick = match target.as_deref() {
        None => pts.iter().find(|p| p.label.starts_with("chuck: before \"")),
        Some(t) if t.len() <= 3 && t.chars().all(|c| c.is_ascii_digit()) => {
            t.parse::<usize>().ok().and_then(|n| n.checked_sub(1)).and_then(|n| pts.get(n))
        }
        Some(t) => pts.iter().find(|p| p.sha.starts_with(t)),
    };
    let Some(p) = pick else { fail("no such checkpoint — `chuck rewind --list` shows them") };
    match sh.restore(&p.sha) {
        Ok((undo, changed)) => {
            println!("rewound {} to {} ({}): {} file(s) put back", sh.work.display(), &p.sha[..10], p.label, changed.len());
            println!("undo this rewind:  chuck rewind -C {} {}", sh.work.display(), &undo[..10]);
            0
        }
        Err(e) => fail(&e.to_string()),
    }
}

/// `chuck daemons [rm N | pause N | resume N]`
fn cmd_daemons(args: &[String]) -> i32 {
    let mut reg = chuck_daemon::Registry::load(&chuck_daemon::Registry::default_path());
    if let (Some(verb), Some(id)) = (args.first(), args.get(1).and_then(|n| n.parse::<i64>().ok())) {
        let ok = match verb.as_str() {
            "rm" | "remove" => reg.remove(id),
            "pause" | "resume" => match reg.get_mut(id) {
                Some(d) => {
                    d.set("enabled", (verb == "resume").into());
                    if verb == "resume" {
                        d.set("last_run", chuck_daemon::now().into());
                    }
                    reg.save();
                    true
                }
                None => false,
            },
            _ => fail("chuck daemons [rm N | pause N | resume N]"),
        };
        if !ok {
            fail(&format!("no daemon {id}"));
        }
    }
    if reg.items.is_empty() {
        println!("No daemons or reminders. In the window: /daemon every day at 9: check the disk, or ask in Chat.");
        return 0;
    }
    for d in &reg.items {
        println!("{:>3}  {:<8} {:<34} {}{}", d.id(), if d.kind() == "remind" { "reminder" } else { "daemon" },
                 chuck_daemon::describe(d.when()), d.name(), if d.enabled() { String::new() } else { "  (paused)".into() });
    }
    println!("\nThey run while the Chuck window is open: reports go to `chuck inbox`.");
    0
}

/// `chuck inbox [read]`
fn cmd_inbox(arg: Option<&str>) -> i32 {
    let mut ib = chuck_daemon::Inbox::load(&chuck_daemon::Inbox::default_path(), 200);
    if arg == Some("read") {
        ib.mark_read();
        println!("Marked read.");
        return 0;
    }
    if ib.items.is_empty() {
        println!("The inbox is empty.");
        return 0;
    }
    for it in ib.items.iter().rev().take(20) {
        let s = |k: &str| it.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let t = it.get("time").and_then(|v| v.as_f64()).unwrap_or(0.0) as i64;
        let new = if it.get("unread").and_then(|v| v.as_bool()) == Some(true) { "● " } else { "  " };
        println!("{new}{}  {} — {}\n{}\n", chuck_store::ledger::stamp_at(t, "%a %d %b %H:%M"), s("daemon"), s("why"),
                 s("report").trim().lines().map(|l| format!("    {l}")).collect::<Vec<_>>().join("\n"));
    }
    println!("{} unread · `chuck inbox read` marks them read", ib.unread());
    0
}

fn cmd_ledger(n: usize) -> i32 {
    let l = Ledger::at(&Ledger::default_path());
    let v = l.verify();
    println!("evidence ledger — {} entries, {}: {}", v.count, if v.ok { "VERIFIED" } else { "BROKEN" }, v.message);
    println!("({})\n", l.path.display());
    let all = l.read_all();
    for e in all.iter().skip(all.len().saturating_sub(n)) {
        let rc = e.get("rc").and_then(|x| x.as_i64()).unwrap_or(-1);
        let mark = if rc == 0 { "✓".to_string() } else { format!("✗ rc={rc}") };
        let cmd: String = e.get("command").and_then(|x| x.as_str()).unwrap_or("").chars().take(90).collect();
        println!("{}  {mark}  {}", e.get("ts").and_then(|x| x.as_str()).unwrap_or("?"), cmd.replace('\n', " ⏎ "));
    }
    if v.ok { 0 } else { 1 }
}

fn cmd_models() {
    let s = Settings::load();
    let c = client(&s);
    let mut f = setup::models(&s);
    let live = c.list_models().unwrap_or_else(|e| fail(&e.to_string()));
    let changes = f.resolve(&live);
    let have = |m: &str| live.iter().any(|x| x == m);
    let on = |m: &str| if have(m) { "✓ on your account" } else { "✗ not on your account" };
    println!("does the work       {}  {}", f.main, on(&f.main));
    println!("eyes (screenshots)  {}  {}", f.vision, on(&f.vision));
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

fn parse_run(args: &[String], allow_empty: bool) -> Result<RunArgs, String> {
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
    if a.task.is_empty() && !allow_empty {
        return Err("say what to do: chuck run \"fix the failing test in parser.py\"".into());
    }
    Ok(a)
}

fn cmd_run(args: &[String], chat: bool, cont: bool) -> i32 {
    let a = parse_run(args, cont).unwrap_or_else(|e| fail(&e));
    let s = Settings::load();
    let folder = a.dir.canonicalize().unwrap_or_else(|_| a.dir.clone());
    let resume = if cont {
        Some(chuck_runtime::resume::load(&folder).unwrap_or_else(|| fail(&format!(
            "no paused job in {} — a finished job, or one never started here, has nothing to carry on", folder.display()))))
    } else {
        None
    };
    let setup::Setup { model, mut cfg, usd, notes } = setup::from_settings(&s, &folder, a.max_usd)
        .unwrap_or_else(|e| fail(&e));
    for n in notes {
        eprintln!("chuck: {n}");
    }
    // MCP servers, if any are set up: started for this job, ended with it
    let mcp = Arc::new(chuck_mcp::Manager::new(chuck_mcp::config_path()));
    if mcp.configured() {
        mcp.set_roots(vec![folder.clone()]);
        mcp.start_all();
        for st in mcp.status().iter().filter(|st| st.state != "running") {
            eprintln!("chuck: MCP server {} {}: {}", st.name, st.state, st.detail);
        }
        cfg.mcp = Some(mcp.clone());
    }
    let lsp = Arc::new(setup::lsp(&s));
    cfg.lsp = Some(lsp.clone());
    let header = if chat {
        format!("chat · {} · up to {}", term::short_model(&cfg.models.main), chuck_model::fmt_usd(usd))
    } else {
        format!("{} · {} ({}) · up to {}", term::short_model(&cfg.models.main), folder.display(), cfg.access.label(),
                chuck_model::fmt_usd(usd))
    };
    let (tx, rx) = channel();
    let rt = Runtime::new(model, cfg, Events(Some(tx)));
    let mut job = Job::new(a.task, &a.dir);
    if let Some(r) = resume {
        eprintln!("chuck: carrying on \"{}\" ({} so far, paused: {})", chuck_core::evidence::clip(&r.ask, 80),
                  chuck_model::fmt_usd(r.spent_usd), r.why);
        job.resume = Some(r);
    }
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
        // Ctrl-C, a closed terminal, a logout: the job stops and every command it started ends
        for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            libc::signal(sig, on_sigint as *const () as libc::sighandler_t);
        }
    }
    let watcher_stop = stop.clone();
    let done = Arc::new(AtomicBool::new(false));
    let watcher_done = done.clone();
    std::thread::spawn(move || {
        while !watcher_done.load(Ordering::Relaxed) {
            let n = INTERRUPTS.load(Ordering::SeqCst);
            if n > 0 {
                watcher_stop.store(true, Ordering::SeqCst);
            }
            if n > 1 {
                chuck_tools::kill_all();
                std::process::exit(130);
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    });
    let worker = std::thread::spawn(move || if chat { rt.chat(job) } else { rt.run(job) });
    let mut daemons: Option<chuck_daemon::Registry> = None;

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
            Event::Confirm { outside, .. } if asking => {
                print!("{} ", view.st.bold(if outside.is_empty() { "type yes to run it, anything else to refuse >" } else {
                    "yes: allow once · always: for this job · anything else refuses >" }));
                let _ = std::io::stdout().flush();
            }
            Event::Schedule { kind, when, what } => {
                // kept for the window, which runs them while it's open
                let reg = daemons.get_or_insert_with(|| chuck_daemon::Registry::load(&chuck_daemon::Registry::default_path()));
                let f = if kind == "remind" { String::new() } else { folder.display().to_string() };
                reg.add("", when.clone(), what, &f, kind, 150_000);
            }
            Event::Finished(_) => break,
            _ => {}
        }
    }
    let report = worker.join().unwrap_or_else(|_| fail("the job crashed"));
    mcp.stop_all();
    lsp.stop_all();
    chuck_tools::kill_all();
    done.store(true, Ordering::Relaxed);
    if let (Some(cp), false) = (&report.checkpoint, matches!(report.outcome, Outcome::Answered { .. })) {
        println!("{}", view.st.dim(&format!("undo all of it:  chuck rewind -C {} {}", folder.display(), &cp[..10])));
    }
    if report.resume.is_some() {
        println!("{}", view.st.dim(&format!("carry on where it stopped:  chuck continue -C {}", folder.display())));
    }
    match report.outcome {
        Outcome::Done { .. } | Outcome::Answered { .. } => 0,
        Outcome::Paused { .. } => 2,
        Outcome::Failed { .. } => 1,
    }
}

/// How the voice stands, or (`install`) set it up.
fn cmd_voice(arg: Option<&str>) -> i32 {
    let s = Settings::load();
    let status = |s: &Settings| {
        println!("speaking   {}", chuck_voice::Voice::new().status(&chuck_voice::Prefs::from(s)));
        println!("listening  {}", chuck_voice::listen::status(s.str("mic_command").unwrap_or("")));
    };
    match arg {
        None | Some("status") => {
            status(&s);
            0
        }
        Some("install") => {
            let tty = std::io::stdout().is_terminal();
            let say = |m: &str| {
                if tty && m.starts_with("downloading") {
                    print!("\r\x1b[K  {m}");
                } else {
                    println!("{}  {m}", if tty { "\r\x1b[K" } else { "" });
                }
                let _ = std::io::stdout().flush();
            };
            let said = chuck_voice::install::all(&say);
            for r in &said {
                match r {
                    Ok(m) => say(&format!("✓ {m}")),
                    Err(e) => say(&format!("✗ {e}")),
                }
            }
            status(&s);
            i32::from(said.iter().any(|r| r.is_err()))
        }
        Some(other) => fail(&format!("chuck voice: unknown `{other}` (status, install)")),
    }
}

fn main() {
    chuck_store::crashlog::install();
    chuck_runtime::setup::migrate();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        Some("run") => cmd_run(&args[1..], false, false),
        Some("continue") => cmd_run(&args[1..], false, true),
        Some("ask") => cmd_run(&args[1..], true, false),
        Some("models") => {
            cmd_models();
            0
        }
        Some("key") => {
            cmd_key();
            0
        }
        Some("rewind") => cmd_rewind(&args[1..]),
        Some("doctor") => {
            let folder = std::env::current_dir().ok();
            let mcp = chuck_mcp::Manager::new(chuck_mcp::config_path());
            if mcp.configured() {
                mcp.start_all();
            }
            let checks = chuck_ui::doctor::run(folder.as_deref(), Some(&mcp));
            for k in &checks {
                println!("{} {:<16} {}", match k.state { chuck_ui::doctor::State::Ok => "✓", chuck_ui::doctor::State::Warn => "!",
                    chuck_ui::doctor::State::Fail => "✗" }, k.name, k.detail);
            }
            mcp.stop_all();
            chuck_tools::kill_all();
            i32::from(checks.iter().any(|k| k.state == chuck_ui::doctor::State::Fail))
        }
        Some("update") => match chuck_ui::update::check() {
            Err(e) => fail(&format!("couldn't ask GitHub: {e}")),
            Ok(chuck_ui::update::Found::UpToDate(_)) => {
                println!("This is the newest Chuck.");
                0
            }
            Ok(_) => match chuck_ui::update::run(&mut |l| println!("  {l}")) {
                Ok(()) => {
                    println!("Updated. Open Chuck again to use the new version.");
                    0
                }
                Err(e) => fail(&e),
            },
        },
        Some("voice") => cmd_voice(args.get(1).map(String::as_str)),
        Some("daemons") => cmd_daemons(&args[1..]),
        Some("inbox") => cmd_inbox(args.get(1).map(String::as_str)),
        Some("ledger") => cmd_ledger(args.get(1).and_then(|n| n.parse().ok()).unwrap_or(15)),
        Some("version" | "--version" | "-V") => {
            println!("chuck {VERSION}");
            0
        }
        None if std::env::var_os("DISPLAY").is_some() || std::env::var_os("WAYLAND_DISPLAY").is_some() => {
            chuck_ui::run(chuck_ui::Options::default())
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
        let a = parse_run(&v(&["-C", "/tmp/p", "--max-usd", "0.5", "fix", "the", "test"]), false).unwrap();
        assert_eq!(a.dir, PathBuf::from("/tmp/p"));
        assert_eq!(a.max_usd, Some(0.5));
        assert_eq!(a.task, "fix the test");
        assert!(parse_run(&v(&["--max-usd", "-1", "x"]), false).is_err());
        assert!(parse_run(&v(&["--bogus", "x"]), false).is_err());
        assert!(parse_run(&v(&[]), false).is_err());
        assert_eq!(parse_run(&v(&["--", "-v", "means", "verbose"]), false).unwrap().task, "-v means verbose");
    }
}
