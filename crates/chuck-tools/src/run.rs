//! Running commands: one process group per command, so a timeout or a Stop
//! kills the whole tree; output streamed live and capped.

use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::Arc;
use std::time::{Duration, Instant};

/// Output kept per command: head and tail, the middle elided (errors live at
/// the end; what ran lives at the start).
pub const MAX_OUTPUT: usize = 24_000;

#[derive(Clone, Debug, PartialEq)]
pub struct RunResult {
    pub exit: i32,
    pub output: String,
    pub timed_out: bool,
    pub stopped: bool,
    pub ms: u64,
}

impl RunResult {
    pub fn ok(&self) -> bool {
        self.exit == 0 && !self.timed_out && !self.stopped
    }
}

/// Turns an argv into the argv that actually runs (the sandbox), or None
/// when it must not run (sandboxed folder, no bubblewrap).
pub type Wrap = Arc<dyn Fn(Vec<String>) -> Option<Vec<String>> + Send + Sync>;

pub struct RunOpts<'a> {
    pub cwd: &'a Path,
    pub timeout: Duration,
    pub env: Vec<(String, String)>,
    pub wrap: Option<Wrap>,
    pub stop: Option<Arc<AtomicBool>>,
    pub on_chunk: Option<&'a mut dyn FnMut(&str)>,
}

impl<'a> RunOpts<'a> {
    pub fn new(cwd: &'a Path) -> RunOpts<'a> {
        RunOpts { cwd, timeout: Duration::from_secs(300), env: Vec::new(), wrap: None, stop: None, on_chunk: None }
    }
}

pub fn bash(cmd: &str, mut o: RunOpts<'_>) -> RunResult {
    let t0 = Instant::now();
    let argv = vec!["bash".to_string(), "-c".to_string(), cmd.to_string()];
    let argv = match &o.wrap {
        Some(w) => match w(argv) {
            Some(a) => a,
            None => return RunResult { exit: 126, output: "(not run: this folder is sandboxed but bubblewrap \
                isn't installed — install bubblewrap, or give the folder full access)".into(),
                timed_out: false, stopped: false, ms: 0 },
        },
        None => argv,
    };
    let mut c = Command::new(&argv[0]);
    tag(&mut c);
    c.args(&argv[1..]).current_dir(o.cwd).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .env("CI", "1").env("PAGER", "cat").env("GIT_PAGER", "cat").env("TERM", "dumb")
        .env("DEBIAN_FRONTEND", "noninteractive");
    for (k, v) in &o.env {
        c.env(k, v);
    }
    use std::os::unix::process::CommandExt;
    c.process_group(0);
    // if Chuck itself dies (even SIGKILL), the command's shell dies with it
    unsafe {
        c.pre_exec(|| {
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            Ok(())
        });
    }
    let mut child = match c.spawn() {
        Ok(ch) => ch,
        Err(e) => return RunResult { exit: 127, output: format!("couldn't start: {e}"), timed_out: false,
                                     stopped: false, ms: 0 },
    };
    let pgid = child.id() as i32;
    let _live = Live::register(pgid);
    let (tx, rx) = mpsc::channel::<Vec<u8>>();
    let mut readers = Vec::new();
    for pipe in [child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>),
                 child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>)].into_iter().flatten() {
        let tx = tx.clone();
        readers.push(std::thread::spawn(move || {
            let mut pipe = pipe;
            let mut buf = [0u8; 8192];
            while let Ok(n) = pipe.read(&mut buf) {
                if n == 0 || tx.send(buf[..n].to_vec()).is_err() {
                    break;
                }
            }
        }));
    }
    drop(tx);
    let mut cap = Capped::default();
    let (mut timed_out, mut stopped, mut lingered) = (false, false, false);
    let deadline = t0 + o.timeout;
    let mut exited: Option<(Instant, std::process::ExitStatus)> = None;
    loop {
        match rx.recv_timeout(Duration::from_millis(50)) {
            Ok(chunk) => {
                if let Some(f) = o.on_chunk.as_mut() {
                    f(&String::from_utf8_lossy(&chunk));
                }
                cap.push(&chunk);
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        if exited.is_none() {
            if let Ok(Some(st)) = child.try_wait() {
                exited = Some((Instant::now(), st));
            }
        }
        if o.stop.as_ref().is_some_and(|s| s.load(Ordering::Relaxed)) {
            stopped = true;
        } else if Instant::now() > deadline {
            timed_out = true;
        } else if exited.is_some_and(|(at, _)| at.elapsed() > LINGER) {
            // the command is over but something it started in the background
            // still holds its output open: it would hang the agent until the timeout
            lingered = true;
        }
        if stopped || timed_out || lingered {
            kill_group(pgid);
            break;
        }
    }
    let status = match exited {
        Some((_, st)) => Ok(st),
        None => child.wait(),
    };
    // the group is dead or done: the pipes close and the readers end
    for r in readers {
        let _ = r.join();
    }
    while let Ok(chunk) = rx.try_recv() {
        cap.push(&chunk);
    }
    let mut output = cap.text();
    if timed_out {
        output.push_str(&format!("\n(timed out after {} s — the whole process tree was stopped)", o.timeout.as_secs()));
    }
    if lingered {
        output.push_str("\n(the command finished, but processes it started in the background kept running and were \
            stopped — to try a server, start it and test it in ONE command, e.g. `./serve & sleep 2; curl -s localhost:8000`)");
    }
    use std::os::unix::process::ExitStatusExt;
    let exit = match status {
        Ok(s) => s.code().or_else(|| s.signal().map(|n| 128 + n)).unwrap_or(128),
        Err(_) => 128,
    };
    let exit = if (timed_out || stopped) && exit == 0 { 124 } else { exit };
    RunResult { exit, output, timed_out, stopped, ms: t0.elapsed().as_millis() as u64 }
}

/// How long background stragglers may keep the output open after the
/// command itself has exited.
const LINGER: Duration = Duration::from_millis(1500);

/// Output kept as head + tail, the middle counted and elided.
#[derive(Default)]
struct Capped {
    head: Vec<u8>,
    tail: std::collections::VecDeque<u8>,
    total: usize,
}

impl Capped {
    fn push(&mut self, chunk: &[u8]) {
        self.total += chunk.len();
        for &b in chunk {
            if self.head.len() < MAX_OUTPUT / 2 {
                self.head.push(b);
            } else {
                self.tail.push_back(b);
                if self.tail.len() > MAX_OUTPUT / 2 {
                    self.tail.pop_front();
                }
            }
        }
    }

    fn text(self) -> String {
        let mut out = String::from_utf8_lossy(&self.head).into_owned();
        if !self.tail.is_empty() {
            let elided = self.total.saturating_sub(self.head.len() + self.tail.len());
            if elided > 0 {
                out.push_str(&format!("\n… {elided} bytes elided …\n"));
            }
            out.push_str(&String::from_utf8_lossy(&self.tail.into_iter().collect::<Vec<_>>()));
        }
        out
    }
}

/// Every command's process group while it runs, so Chuck can end them all
/// when it quits (nothing an agent started may outlive the app).
fn live() -> &'static std::sync::Mutex<std::collections::HashSet<i32>> {
    static LIVE: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<i32>>> = std::sync::OnceLock::new();
    LIVE.get_or_init(Default::default)
}

struct Live(i32);

impl Live {
    fn register(pgid: i32) -> Live {
        live().lock().unwrap_or_else(|e| e.into_inner()).insert(pgid);
        Live(pgid)
    }
}

impl Drop for Live {
    fn drop(&mut self) {
        live().lock().unwrap_or_else(|e| e.into_inner()).remove(&self.0);
    }
}

/// Long-lived helpers Chuck starts (MCP servers): not commands, so not
/// counted as running ones, but ended with everything else on quit.
fn services() -> &'static std::sync::Mutex<std::collections::HashSet<i32>> {
    static S: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<i32>>> = std::sync::OnceLock::new();
    S.get_or_init(Default::default)
}

/// A helper's process group, registered while the guard lives.
pub struct Service(i32);

impl Service {
    pub fn register(pgid: i32) -> Service {
        services().lock().unwrap_or_else(|e| e.into_inner()).insert(pgid);
        Service(pgid)
    }
}

impl Drop for Service {
    fn drop(&mut self) {
        services().lock().unwrap_or_else(|e| e.into_inner()).remove(&self.0);
    }
}

/// How many commands are running right now (any agent, any thread).
pub fn running() -> usize {
    live().lock().unwrap_or_else(|e| e.into_inner()).len()
}

/// Every process Chuck starts carries this in its environment, and so does
/// everything those start in turn — through `nohup … &`, setsid and
/// daemons that detach (an emulator, an adb server) — so quitting can end
/// all of it, wherever it went. What Chuck opens for you (a link in your
/// browser, a restarted Chuck) is never tagged.
pub const SESSION_VAR: &str = "CHUCK_SESSION";

/// This run's tag.
pub fn session() -> &'static str {
    static S: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    S.get_or_init(|| {
        let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        format!("{}-{t:x}", std::process::id())
    })
}

/// Tag a command Chuck is about to start.
pub fn tag(c: &mut Command) {
    c.env(SESSION_VAR, session());
}

/// Processes carrying this run's tag (never Chuck itself).
pub fn session_pids() -> Vec<i32> {
    let want = format!("{SESSION_VAR}={}", session());
    let me = std::process::id() as i32;
    let Ok(rd) = std::fs::read_dir("/proc") else { return Vec::new() };
    rd.flatten().filter_map(|e| e.file_name().to_str().and_then(|n| n.parse::<i32>().ok()))
        .filter(|&pid| pid != me)
        .filter(|pid| std::fs::read(format!("/proc/{pid}/environ"))
            .is_ok_and(|env| env.split(|b| *b == 0).any(|kv| kv == want.as_bytes())))
        .collect()
}

/// End everything this run started that is still alive, TERM then KILL;
/// twice, for what was being started meanwhile.
pub fn kill_session() {
    for _ in 0..2 {
        let pids = session_pids();
        if pids.is_empty() {
            return;
        }
        for p in &pids {
            unsafe {
                libc::kill(*p, libc::SIGTERM);
            }
        }
        std::thread::sleep(Duration::from_millis(300));
        for p in session_pids() {
            unsafe {
                libc::kill(p, libc::SIGKILL);
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// End every command still running — the whole process group of each,
/// TERM then KILL — and everything they left behind. Called when the app quits.
pub fn kill_all() {
    let mut groups: Vec<i32> = live().lock().unwrap_or_else(|e| e.into_inner()).iter().copied().collect();
    groups.extend(services().lock().unwrap_or_else(|e| e.into_inner()).iter().copied());
    for g in &groups {
        unsafe {
            libc::killpg(*g, libc::SIGTERM);
        }
    }
    if !groups.is_empty() {
        std::thread::sleep(Duration::from_millis(200));
    }
    for g in &groups {
        unsafe {
            libc::killpg(*g, libc::SIGKILL);
        }
    }
    kill_session();
}

fn kill_group(pgid: i32) {
    unsafe {
        libc::killpg(pgid, libc::SIGTERM);
    }
    std::thread::sleep(Duration::from_millis(150));
    unsafe {
        libc::killpg(pgid, libc::SIGKILL);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runs_with_exit_code_and_output() {
        let d = std::env::temp_dir();
        let r = bash("echo out; echo err >&2; exit 3", RunOpts::new(&d));
        assert_eq!(r.exit, 3);
        assert!(r.output.contains("out") && r.output.contains("err"));
    }

    #[test]
    fn a_timeout_kills_the_whole_tree() {
        let d = std::env::temp_dir();
        let mut o = RunOpts::new(&d);
        o.timeout = Duration::from_millis(400);
        let t0 = Instant::now();
        let r = bash("sleep 30 & sleep 30; echo never", o);
        assert!(r.timed_out && t0.elapsed() < Duration::from_secs(5));
        assert!(!r.output.contains("never"));
    }

    #[test]
    fn huge_output_is_capped_head_and_tail() {
        let d = std::env::temp_dir();
        let r = bash("seq 1 200000", RunOpts::new(&d));
        assert!(r.output.len() < MAX_OUTPUT + 200);
        assert!(r.output.starts_with("1\n") && r.output.trim_end().ends_with("200000"));
        assert!(r.output.contains("elided"));
    }

    #[test]
    fn stop_ends_it() {
        let d = std::env::temp_dir();
        let stop = Arc::new(AtomicBool::new(false));
        let s2 = stop.clone();
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(200));
            s2.store(true, Ordering::Relaxed);
        });
        let mut o = RunOpts::new(&d);
        o.stop = Some(stop);
        let r = bash("sleep 20", o);
        assert!(r.stopped && !r.ok());
    }

    #[test]
    fn a_background_straggler_doesnt_hang_the_agent() {
        let d = std::env::temp_dir();
        let t0 = Instant::now();
        let r = bash("sleep 60 & echo started", RunOpts::new(&d));
        assert!(t0.elapsed() < Duration::from_secs(5), "{:?}", t0.elapsed());
        assert_eq!(r.exit, 0);
        assert!(!r.timed_out);
        assert!(r.output.starts_with("started") && r.output.contains("background"));
    }

    #[test]
    fn a_signal_death_is_reported_as_128_plus_the_signal() {
        let d = std::env::temp_dir();
        let r = bash("kill -9 $$", RunOpts::new(&d));
        assert_eq!(r.exit, 137);
    }

    #[test]
    fn live_chunks_arrive() {
        let d = std::env::temp_dir();
        let mut got = String::new();
        let mut f = |c: &str| got.push_str(c);
        let mut o = RunOpts::new(&d);
        o.on_chunk = Some(&mut f);
        bash("echo a; sleep 0.1; echo b", o);
        assert_eq!(got, "a\nb\n");
    }
}
