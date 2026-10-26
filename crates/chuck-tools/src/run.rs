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
    c.args(&argv[1..]).current_dir(o.cwd).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .env("CI", "1").env("PAGER", "cat").env("GIT_PAGER", "cat").env("TERM", "dumb")
        .env("DEBIAN_FRONTEND", "noninteractive");
    for (k, v) in &o.env {
        c.env(k, v);
    }
    use std::os::unix::process::CommandExt;
    c.process_group(0);
    let mut child = match c.spawn() {
        Ok(ch) => ch,
        Err(e) => return RunResult { exit: 127, output: format!("couldn't start: {e}"), timed_out: false,
                                     stopped: false, ms: 0 },
    };
    let pgid = child.id() as i32;
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
    let mut head = Vec::<u8>::new();
    let mut tail = std::collections::VecDeque::<u8>::new();
    let mut total = 0usize;
    let (mut timed_out, mut stopped) = (false, false);
    let deadline = t0 + o.timeout;
    loop {
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(chunk) => {
                if let Some(f) = o.on_chunk.as_mut() {
                    f(&String::from_utf8_lossy(&chunk));
                }
                total += chunk.len();
                for b in chunk {
                    if head.len() < MAX_OUTPUT / 2 {
                        head.push(b);
                    } else {
                        tail.push_back(b);
                        if tail.len() > MAX_OUTPUT / 2 {
                            tail.pop_front();
                        }
                    }
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        if o.stop.as_ref().is_some_and(|s| s.load(Ordering::Relaxed)) {
            stopped = true;
        } else if Instant::now() > deadline {
            timed_out = true;
        }
        if stopped || timed_out {
            kill_group(pgid);
            break;
        }
    }
    let status = child.wait();
    for r in readers {
        let _ = r.join();
    }
    while let Ok(chunk) = rx.try_recv() {
        for b in chunk {
            if head.len() < MAX_OUTPUT / 2 { head.push(b) } else { tail.push_back(b) }
        }
    }
    let mut output = String::from_utf8_lossy(&head).into_owned();
    if !tail.is_empty() {
        let elided = total.saturating_sub(head.len() + tail.len());
        if elided > 0 {
            output.push_str(&format!("\n… {elided} bytes elided …\n"));
        }
        output.push_str(&String::from_utf8_lossy(&tail.into_iter().collect::<Vec<_>>()));
    }
    if timed_out {
        output.push_str(&format!("\n(timed out after {} s — the whole process tree was stopped)", o.timeout.as_secs()));
    }
    let exit = match status {
        Ok(s) => s.code().unwrap_or(if timed_out || stopped { 124 } else { 128 }),
        Err(_) => 128,
    };
    RunResult { exit, output, timed_out, stopped, ms: t0.elapsed().as_millis() as u64 }
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
