//! Speaking: Kokoro (natural, kept warm in its own process), else Piper,
//! else espeak-ng. A reply is cleaned, split into chunks, synthesised one or
//! two chunks ahead of playback, and — for Kokoro — streamed into ONE player
//! so there are no gaps between sentences. `stop()` silences it at once.

use crate::clean;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{sync_channel, Receiver};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const KOKORO_RATE: u32 = 24_000;
pub const DEFAULT_KOKORO_VOICE: &str = "am_michael:0.6,am_fenrir:0.4";
/// (voice, what it sounds like), for Settings.
pub const KOKORO_VOICES: &[(&str, &str)] = &[
    (DEFAULT_KOKORO_VOICE, "Chuck — deep, steady (Michael + Fenrir)"),
    ("af_heart", "Heart — warm, the most natural"), ("af_bella", "Bella — bright, expressive"),
    ("am_michael", "Michael — calm American"), ("am_fenrir", "Fenrir — deeper American"),
    ("am_puck", "Puck — lively American"), ("am_onyx", "Onyx — low American"), ("af_nicole", "Nicole — soft, close"),
    ("af_sarah", "Sarah — clear American"), ("bm_george", "George — British"),
    ("bm_fable", "Fable — British storyteller"), ("bf_emma", "Emma — British"),
];

const KOKORO_SERVER: &str = include_str!("kokoro_server.py");
pub(crate) const STT_SERVER: &str = include_str!("stt_server.py");

// ── where things are (the installer's places, shared with the Python app) ──

pub fn data_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| "/".into()).join(".local/share/chucknorris")
}

pub fn voice_dir() -> PathBuf {
    data_dir().join("voices")
}

/// The voice environment's Python (kokoro-onnx, sherpa-onnx, numpy).
pub fn voice_python() -> Option<PathBuf> {
    Some(data_dir().join("voice/bin/python")).filter(|p| p.exists())
}

pub fn kokoro_files() -> (PathBuf, PathBuf) {
    let d = voice_dir().join("kokoro");
    (d.join("kokoro-v1.0.onnx"), d.join("voices-v1.0.bin"))
}

pub fn kokoro_ready() -> bool {
    let (m, v) = kokoro_files();
    voice_python().is_some() && m.exists() && v.exists()
}

/// A sidecar script, written where it can be run from: the one built into
/// this binary, so it always matches.
pub(crate) fn sidecar(name: &str, text: &str) -> std::io::Result<PathBuf> {
    let dir = data_dir().join("rust-sidecars");
    std::fs::create_dir_all(&dir)?;
    let p = dir.join(name);
    if std::fs::read_to_string(&p).ok().as_deref() != Some(text) {
        let tmp = dir.join(format!(".{name}.{}", std::process::id()));
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, &p)?;
    }
    Ok(p)
}

pub fn which(cmd: &str) -> Option<PathBuf> {
    if cmd.contains('/') {
        return Some(PathBuf::from(cmd)).filter(|p| p.is_file());
    }
    std::env::split_paths(&std::env::var_os("PATH")?).map(|d| d.join(cmd)).find(|p| {
        use std::os::unix::fs::PermissionsExt;
        p.metadata().is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
    })
}

/// A command in its own process group, ended if Chuck dies, ended with
/// everything else when Chuck quits.
fn spawn(argv: &[String], stdin: Stdio, stdout: Stdio) -> std::io::Result<(Child, chuck_tools::Service)> {
    let mut c = Command::new(&argv[0]);
    chuck_tools::tag(&mut c);
    c.args(&argv[1..]).stdin(stdin).stdout(stdout).stderr(Stdio::null());
    unsafe {
        c.pre_exec(|| {
            libc::setsid();
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
            Ok(())
        });
    }
    let child = c.spawn()?;
    let guard = chuck_tools::Service::register(child.id() as i32);
    Ok((child, guard))
}

/// A long-lived helper (Kokoro, the listener): started on a thread that
/// lives exactly as long as it — the parent-death signal follows the thread
/// that started a process, so one started from a passing thread would die
/// with that thread. -> (stdin, stdout, pid, exited flag, its group's guard).
pub(crate) struct Lasting {
    pub stdin: ChildStdin,
    /// Taken by whoever reads it.
    pub stdout: Option<std::process::ChildStdout>,
    pub pid: i32,
    pub exited: Arc<std::sync::atomic::AtomicBool>,
    pub _guard: chuck_tools::Service,
}

impl Lasting {
    pub fn start(argv: &[String], cwd: &Path) -> Result<Lasting, String> {
        let (tx, rx) = std::sync::mpsc::channel();
        let exited = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let (argv, cwd, ex) = (argv.to_vec(), cwd.to_path_buf(), exited.clone());
        std::thread::Builder::new().name("voice-helper".into()).spawn(move || {
            let mut c = Command::new(&argv[0]);
            chuck_tools::tag(&mut c);
            c.args(&argv[1..]).current_dir(&cwd).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
            unsafe {
                c.pre_exec(|| {
                    libc::setsid();
                    libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL);
                    Ok(())
                });
            }
            match c.spawn() {
                Ok(mut child) => {
                    let _ = tx.send(Ok((child.stdin.take(), child.stdout.take(), child.id() as i32)));
                    let _ = child.wait();
                    ex.store(true, Ordering::SeqCst);
                }
                Err(e) => {
                    let _ = tx.send(Err(e.to_string()));
                }
            }
        }).map_err(|e| e.to_string())?;
        let (stdin, stdout, pid) = rx.recv_timeout(Duration::from_secs(10)).map_err(|_| "it didn't start".to_string())??;
        let (Some(stdin), Some(stdout)) = (stdin, stdout) else { return Err("no pipes to it".into()) };
        Ok(Lasting { stdin, stdout: Some(stdout), pid, exited, _guard: chuck_tools::Service::register(pid) })
    }

    pub fn alive(&self) -> bool {
        !self.exited.load(Ordering::SeqCst)
    }

    pub fn kill(&self) {
        unsafe {
            libc::killpg(self.pid, libc::SIGKILL);
        }
    }
}

/// Not the OTHER piper (libratbag's mouse configurator, a Python script).
fn is_tts_piper(p: &Path) -> bool {
    let Ok(head) = std::fs::read(p).map(|b| b.into_iter().take(8192).collect::<Vec<u8>>()) else { return true };
    if head.starts_with(b"\x7fELF") {
        return true;
    }
    let t = String::from_utf8_lossy(&head);
    !["piper.application", "gi.require_version", "ratbag"].iter().any(|b| t.contains(b))
}

/// What the settings say about speaking.
#[derive(Clone, Debug)]
pub struct Prefs {
    /// "auto", "kokoro", "piper" or "espeak".
    pub engine: String,
    pub kokoro_voice: String,
    pub speed: f64,
    pub pitch: i32,
    pub piper_model: String,
    pub piper_bin: String,
    /// The user's own player command for raw 24 kHz s16 mono (else probed).
    pub player: String,
    pub max_chars: usize,
}

impl Prefs {
    pub fn from(s: &chuck_store::Settings) -> Prefs {
        let st = |k: &str| s.str(k).unwrap_or("").trim().to_string();
        Prefs {
            engine: Some(st("voice_engine")).filter(|e| !e.is_empty()).unwrap_or_else(|| "auto".into()).to_lowercase(),
            kokoro_voice: Some(st("kokoro_voice")).filter(|v| !v.is_empty()).unwrap_or_else(|| DEFAULT_KOKORO_VOICE.into()),
            speed: s.num("voice_speed", 1.0, 0.5, 2.0),
            pitch: s.num("voice_pitch", 28.0, 0.0, 99.0) as i32,
            piper_model: st("piper_model"),
            piper_bin: st("piper_bin"),
            player: st("voice_player"),
            max_chars: s.num("voice_max_chars", 20_000.0, 200.0, 200_000.0) as usize,
        }
    }
}

impl Default for Prefs {
    fn default() -> Prefs {
        Prefs { engine: "auto".into(), kokoro_voice: DEFAULT_KOKORO_VOICE.into(), speed: 1.0, pitch: 28,
                piper_model: String::new(), piper_bin: String::new(), player: String::new(), max_chars: 20_000 }
    }
}

pub fn piper_bin(p: &Prefs) -> Option<PathBuf> {
    let mut cands: Vec<PathBuf> = Vec::new();
    if !p.piper_bin.is_empty() {
        cands.push(PathBuf::from(&p.piper_bin));
    }
    cands.push(data_dir().join("piper/bin/piper"));
    if let Some(h) = std::env::var_os("HOME") {
        cands.push(PathBuf::from(h).join(".local/bin/piper"));
    }
    if let Some(w) = which("piper") {
        cands.push(w);
    }
    cands.into_iter().find(|c| c.is_file() && is_tts_piper(c))
}

/// A Piper voice model with its .onnx.json beside it.
pub fn piper_model(p: &Prefs) -> Option<PathBuf> {
    let has_json = |m: &Path| PathBuf::from(format!("{}.json", m.display())).exists();
    if !p.piper_model.is_empty() && Path::new(&p.piper_model).exists() && has_json(Path::new(&p.piper_model)) {
        return Some(PathBuf::from(&p.piper_model));
    }
    let mut models: Vec<PathBuf> = std::fs::read_dir(voice_dir()).ok()?.flatten().map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|e| e == "onnx") && has_json(p)).collect();
    models.sort();
    models.into_iter().next()
}

// ── Kokoro, kept warm ───────────────────────────────────────────────────

struct Kokoro {
    proc: Lasting,
    lines: Receiver<String>,
    seq: u64,
}

impl Kokoro {
    fn start() -> Result<Kokoro, String> {
        let py = voice_python().ok_or("Kokoro isn't installed")?;
        let (model, voices) = kokoro_files();
        let script = sidecar("kokoro_server.py", KOKORO_SERVER).map_err(|e| e.to_string())?;
        let argv = vec![py.display().to_string(), "-u".into(), script.display().to_string(),
                        model.display().to_string(), voices.display().to_string()];
        let mut proc = Lasting::start(&argv, &data_dir())?;
        let out = proc.stdout.take().ok_or("no stdout")?;
        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::Builder::new().name("kokoro-read".into()).spawn(move || {
            for l in BufReader::new(out).lines() {
                let Ok(l) = l else { break };
                if tx.send(l).is_err() {
                    break;
                }
            }
        }).map_err(|e| e.to_string())?;
        let mut k = Kokoro { proc, lines: rx, seq: 0 };
        let first: Value = k.reply(Duration::from_secs(90))?;
        if first.get("ready").and_then(Value::as_bool) != Some(true) {
            k.proc.kill();
            return Err(first.get("error").and_then(Value::as_str).unwrap_or("Kokoro didn't start").to_string());
        }
        Ok(k)
    }

    fn reply(&mut self, timeout: Duration) -> Result<Value, String> {
        let l = self.lines.recv_timeout(timeout).map_err(|e| match e {
            std::sync::mpsc::RecvTimeoutError::Timeout => format!("Kokoro didn't answer within {}s", timeout.as_secs()),
            _ => "Kokoro stopped".to_string(),
        })?;
        serde_json::from_str(&l).map_err(|e| format!("Kokoro said something unreadable: {e}"))
    }

    fn alive(&mut self) -> bool {
        self.proc.alive()
    }

    /// Raw 24 kHz 16-bit mono PCM for `text`.
    fn synth(&mut self, text: &str, voice: &str, speed: f64, scratch: &Path) -> Result<Vec<u8>, String> {
        self.seq += 1;
        let id = self.seq.to_string();
        let out = scratch.join(format!("k-{id}.pcm"));
        let req = json!({"id": id, "text": text, "voice": voice, "speed": speed, "out": out});
        writeln!(self.proc.stdin, "{req}").and_then(|_| self.proc.stdin.flush()).map_err(|e| format!("Kokoro is gone ({e})"))?;
        loop {
            let m = self.reply(Duration::from_secs(120))?;
            if m.get("id").and_then(Value::as_str) == Some(&id) {
                if m.get("ok").and_then(Value::as_bool) != Some(true) {
                    return Err(m.get("error").and_then(Value::as_str).unwrap_or("synthesis failed").to_string());
                }
                let pcm = std::fs::read(&out).map_err(|e| e.to_string());
                let _ = std::fs::remove_file(&out);
                return pcm;
            }
        }
    }
}

impl Drop for Kokoro {
    fn drop(&mut self) {
        self.proc.kill();
    }
}

// ── players ──────────────────────────────────────────────────────────────

/// Raw 16-bit PCM on stdin, best first. Which works is checked, not
/// assumed: PipeWire 1.2+ reads raw stdin only with --raw; 1.0 has no --raw.
fn raw_players() -> Vec<Vec<String>> {
    let r = KOKORO_RATE.to_string();
    let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    vec![
        v(&["paplay", "--raw", &format!("--rate={r}"), "--channels=1", "--format=s16le"]),
        v(&["pw-play", "--raw", "--rate", &r, "--channels", "1", "--format", "s16", "-"]),
        v(&["pw-play", "--rate", &r, "--channels", "1", "--format", "s16", "-"]),
        v(&["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", &r, "-c", "1"]),
        v(&["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", "-f", "s16le", "-ar", &r, "-ac", "1", "-i", "-"]),
    ]
}

/// 0.1 s of silence through it: does it really play?
fn probe(argv: &[String]) -> Result<(), String> {
    let (mut child, _g) = spawn(argv, Stdio::piped(), Stdio::null()).map_err(|e| e.to_string())?;
    if let Some(mut i) = child.stdin.take() {
        let _ = i.write_all(&vec![0u8; (KOKORO_RATE / 10 * 2) as usize]);
    }
    let t0 = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(s)) if s.success() => return Ok(()),
            Ok(Some(s)) => return Err(format!("exit {}", s.code().unwrap_or(-1))),
            Ok(None) if t0.elapsed() > Duration::from_secs(8) => {
                let _ = child.kill();
                return Err("it hung".into());
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(e) => return Err(e.to_string()),
        }
    }
}

/// One player for a whole reply: chunks go into it back to back.
struct Stream {
    child: Option<(Child, chuck_tools::Service)>,
}

impl Stream {
    fn write(&mut self, argv: &[String], pcm: &[u8], current: &dyn Fn() -> bool) -> bool {
        if self.child.is_none() {
            match spawn(argv, Stdio::piped(), Stdio::null()) {
                Ok(c) => self.child = Some(c),
                Err(_) => return false,
            }
        }
        let (child, _) = self.child.as_mut().unwrap();
        let Some(stdin) = child.stdin.as_mut() else { return false };
        // 0.2 s at a time: Stop is never far off
        for part in pcm.chunks((KOKORO_RATE / 5 * 2) as usize) {
            if !current() {
                self.kill();
                return false;
            }
            if stdin.write_all(part).is_err() {
                return false;
            }
        }
        stdin.flush().is_ok()
    }

    /// Let the tail play out (unless stopped).
    fn finish(&mut self, current: &dyn Fn() -> bool) {
        let Some((child, _)) = self.child.as_mut() else { return };
        drop(child.stdin.take());
        loop {
            match child.try_wait() {
                Ok(None) if current() => std::thread::sleep(Duration::from_millis(50)),
                Ok(None) => {
                    self.kill();
                    return;
                }
                _ => return,
            }
        }
    }

    fn kill(&mut self) {
        if let Some((mut c, _)) = self.child.take() {
            let _ = c.kill();
            let _ = c.wait();
        }
    }
}

/// Play a wav file, stoppable. True if it played to the end.
fn play_wav(path: &Path, current: &dyn Fn() -> bool) -> bool {
    let p = path.display().to_string();
    for argv in [vec!["paplay".to_string(), p.clone()], vec!["aplay".into(), "-q".into(), p.clone()],
                 vec!["ffplay".into(), "-nodisp".into(), "-autoexit".into(), "-loglevel".into(), "quiet".into(), p.clone()]] {
        if which(&argv[0]).is_none() {
            continue;
        }
        let Ok((mut child, _g)) = spawn(&argv, Stdio::null(), Stdio::null()) else { continue };
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return true,
                Ok(None) if !current() => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(40)),
                Err(_) => return false,
            }
        }
    }
    false
}

// ── the voice ────────────────────────────────────────────────────────────

#[derive(Default)]
struct Inner {
    kokoro: Option<Kokoro>,
    /// Kokoro broke mid-reply: the other voices speak until then.
    kokoro_down_until: Option<Instant>,
    player: Option<Vec<String>>,
    bad_players: Vec<Vec<String>>,
    last_error: String,
}

/// Chuck's voice. Cheap to clone; one per app.
#[derive(Clone, Default)]
pub struct Voice {
    gen: Arc<AtomicU64>,
    /// The newest speech that has ended (finished, or stopped).
    ended: Arc<AtomicU64>,
    inner: Arc<Mutex<Inner>>,
    /// Kokoro is used by one reply at a time.
    synth_lock: Arc<Mutex<()>>,
}

impl Voice {
    pub fn new() -> Voice {
        Voice::default()
    }

    /// Which engine will speak: kokoro, piper or espeak (None: none can).
    pub fn engine_for(&self, p: &Prefs) -> Option<&'static str> {
        let down = self.inner.lock().unwrap().kokoro_down_until.is_some_and(|t| Instant::now() < t);
        let want = p.engine.as_str();
        if matches!(want, "auto" | "kokoro") && kokoro_ready() && !down {
            return Some("kokoro");
        }
        if matches!(want, "auto" | "piper" | "kokoro") && piper_bin(p).is_some() && piper_model(p).is_some() {
            return Some("piper");
        }
        which("espeak-ng").map(|_| "espeak")
    }

    /// A line on how speaking stands, for Settings.
    pub fn status(&self, p: &Prefs) -> String {
        let err = self.inner.lock().unwrap().last_error.clone();
        let base = match self.engine_for(p) {
            Some("kokoro") => format!("Kokoro · {}", p.kokoro_voice),
            Some("piper") => format!("Piper · {}", piper_model(p).and_then(|m| m.file_stem().map(|s| s.to_string_lossy().into_owned()))
                .unwrap_or_default()),
            Some(_) => "espeak-ng (robotic: Settings › Voice › Set up, or `chuck voice install`, gets the natural one)".into(),
            None => "no voice installed — Settings › Voice › Set up, or `chuck voice install` (Kokoro), or install espeak-ng".into(),
        };
        if err.is_empty() { base } else { format!("{base} — last problem: {err}") }
    }

    /// Start Kokoro now, so the first reply speaks at once.
    pub fn warm(&self, p: &Prefs) {
        if self.engine_for(p) != Some("kokoro") {
            return;
        }
        let me = self.clone();
        std::thread::Builder::new().name("kokoro-warm".into()).spawn(move || {
            let _g = me.synth_lock.lock().unwrap();
            me.player();
            let mut i = me.inner.lock().unwrap();
            if i.kokoro.as_mut().is_none_or(|k| !k.alive()) {
                match Kokoro::start() {
                    Ok(k) => i.kokoro = Some(k),
                    Err(e) => i.last_error = e,
                }
            }
        }).ok();
    }

    /// Silence, now.
    pub fn stop(&self) {
        let g = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
        self.ended.fetch_max(g, Ordering::SeqCst);
    }

    /// Is speech `gen` (what [`Voice::speak`] returned) still being spoken?
    pub fn speaking(&self, gen: u64) -> bool {
        gen != 0 && self.gen.load(Ordering::SeqCst) == gen && self.ended.load(Ordering::SeqCst) < gen
    }

    /// Stop and end Kokoro (the app is closing).
    pub fn shutdown(&self) {
        self.stop();
        if let Ok(mut i) = self.inner.lock() {
            i.kokoro = None;
        }
    }

    pub fn speaking_gen(&self) -> u64 {
        self.gen.load(Ordering::SeqCst)
    }

    /// The raw player that works here, found once (None: why is in last_error).
    fn player(&self) -> Option<Vec<String>> {
        let mut i = self.inner.lock().unwrap();
        if let Some(p) = &i.player {
            return Some(p.clone());
        }
        let mut tried = Vec::new();
        for argv in raw_players() {
            if which(&argv[0]).is_none() || i.bad_players.contains(&argv) {
                continue;
            }
            match probe(&argv) {
                Ok(()) => {
                    i.player = Some(argv.clone());
                    return Some(argv);
                }
                Err(why) => {
                    tried.push(format!("{}: {why}", argv[0]));
                    i.bad_players.push(argv);
                }
            }
        }
        i.last_error = if tried.is_empty() {
            "no audio player found (install pipewire-pulse or pulseaudio-utils for paplay, or alsa-utils for aplay)".into()
        } else {
            format!("no audio player could play sound — {}", tried.join("; "))
        };
        None
    }

    /// Speak `text` in full; returns at once, with the speech's number for
    /// [`Voice::speaking`] (0: nothing to say). Anything already speaking stops.
    pub fn speak(&self, text: &str, p: &Prefs) -> u64 {
        let clean: String = clean::clean(text).chars().take(p.max_chars).collect();
        if clean.is_empty() {
            return 0;
        }
        let gen = self.gen.fetch_add(1, Ordering::SeqCst) + 1;
        let me = self.clone();
        let p = p.clone();
        let started = std::thread::Builder::new().name("speak".into()).spawn(move || {
            match me.engine_for(&p) {
                Some("kokoro") => me.speak_kokoro(&clean, gen, &p),
                Some(e) => me.speak_files(&clean, gen, &p, e),
                None => {}
            }
            me.ended.fetch_max(gen, Ordering::SeqCst);
        });
        if started.is_err() {
            self.ended.fetch_max(gen, Ordering::SeqCst);
        }
        gen
    }

    fn scratch() -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-voice-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&d);
        d
    }

    fn speak_kokoro(&self, clean: &str, gen: u64, p: &Prefs) {
        let chunks = clean::chunks(clean, 420, Some(60));
        let current = || self.gen.load(Ordering::SeqCst) == gen;
        let Some(player) = (if p.player.is_empty() { self.player() } else { Some(p.player.split_whitespace().map(String::from).collect()) })
        else {
            return self.speak_files(clean, gen, p, if piper_bin(p).is_some() && piper_model(p).is_some() { "piper" } else { "espeak" });
        };
        let (tx, rx) = sync_channel::<Option<Vec<u8>>>(2);
        let me = self.clone();
        let (voice, speed, chunks2) = (p.kokoro_voice.clone(), p.speed, chunks.clone());
        let producer = std::thread::Builder::new().name("kokoro-synth".into()).spawn(move || {
            let _g = me.synth_lock.lock().unwrap();
            let scratch = Voice::scratch();
            for ch in &chunks2 {
                if me.gen.load(Ordering::SeqCst) != gen {
                    break;
                }
                let r = {
                    let mut i = me.inner.lock().unwrap();
                    if i.kokoro.as_mut().is_none_or(|k| !k.alive()) {
                        match Kokoro::start() {
                            Ok(k) => i.kokoro = Some(k),
                            Err(e) => {
                                i.last_error = e;
                                drop(i);
                                let _ = tx.send(None);
                                return;
                            }
                        }
                    }
                    i.kokoro.as_mut().unwrap().synth(ch, &voice, speed, &scratch)
                };
                match r {
                    Ok(pcm) => {
                        if tx.send(Some(pcm)).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        let mut i = me.inner.lock().unwrap();
                        i.last_error = e;
                        i.kokoro = None;
                        // broken: the other voices speak for a few minutes
                        i.kokoro_down_until = Some(Instant::now() + Duration::from_secs(300));
                        drop(i);
                        let _ = tx.send(None);
                        break;
                    }
                }
            }
        });
        let gap = vec![0u8; (KOKORO_RATE as f64 * 0.16) as usize * 2]; // a breath between chunks
        let mut stream = Stream { child: None };
        let mut spoken = 0;
        let mut broke = false;
        while current() {
            match rx.recv_timeout(Duration::from_secs(180)) {
                Ok(Some(mut pcm)) => {
                    pcm.extend_from_slice(&gap);
                    if !stream.write(&player, &pcm, &current) {
                        if current() {
                            broke = true;
                            let mut i = self.inner.lock().unwrap();
                            i.player = None;
                            i.bad_players.push(player.clone());
                        }
                        break;
                    }
                    spoken += 1;
                }
                Ok(None) => {
                    broke = true;
                    break;
                }
                Err(_) => break,
            }
        }
        stream.finish(&current);
        drop(rx);
        if let Ok(h) = producer {
            let _ = h.join();
        }
        if broke && current() {
            // Kokoro (or its player) broke: another voice reads the rest
            let rest = chunks[spoken.min(chunks.len())..].join(" ");
            if !rest.is_empty() {
                let e = if piper_bin(p).is_some() && piper_model(p).is_some() { "piper" } else { "espeak" };
                self.speak_files(&rest, gen, p, e);
            }
        }
    }

    /// Piper or espeak-ng, a wav per chunk, two chunks ahead of playback.
    fn speak_files(&self, clean: &str, gen: u64, p: &Prefs, engine: &str) {
        let chunks = clean::chunks(clean, 260, None);
        let current = || self.gen.load(Ordering::SeqCst) == gen;
        let (tx, rx) = sync_channel::<Option<PathBuf>>(2);
        let (p2, engine2) = (p.clone(), engine.to_string());
        let g = self.gen.clone();
        let producer = std::thread::Builder::new().name("synth".into()).spawn(move || {
            let scratch = Voice::scratch();
            for (n, ch) in chunks.iter().enumerate() {
                if g.load(Ordering::SeqCst) != gen {
                    break;
                }
                let out = scratch.join(format!("say-{gen}-{n}.wav"));
                let ok = synth_file(ch, &out, &p2, &engine2);
                if tx.send(ok.then_some(out)).is_err() {
                    break;
                }
            }
        });
        while current() {
            match rx.recv_timeout(Duration::from_secs(120)) {
                Ok(Some(wav)) => {
                    play_wav(&wav, &current);
                    let _ = std::fs::remove_file(&wav);
                }
                Ok(None) => continue, // one chunk failed: skip it, speak the rest
                Err(_) => break,
            }
        }
        drop(rx);
        if let Ok(h) = producer {
            let _ = h.join();
        }
        if let Ok(rd) = std::fs::read_dir(Voice::scratch()) {
            for e in rd.flatten().filter(|e| e.file_name().to_string_lossy().starts_with(&format!("say-{gen}-"))) {
                let _ = std::fs::remove_file(e.path());
            }
        }
    }
}

/// One chunk to a wav file (Piper, else espeak-ng). False: it couldn't.
fn synth_file(chunk: &str, out: &Path, p: &Prefs, engine: &str) -> bool {
    let timeout = Duration::from_secs((8 + chunk.len() as u64 / 8).clamp(20, 90));
    let run = |argv: Vec<String>| -> bool {
        let Ok((mut child, _g)) = spawn(&argv, Stdio::piped(), Stdio::null()) else { return false };
        if let Some(mut i) = child.stdin.take() {
            let _ = i.write_all(chunk.as_bytes());
        }
        let t0 = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if t0.elapsed() > timeout => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return false;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(20)),
                Err(_) => return false,
            }
        }
        out.metadata().is_ok_and(|m| m.len() > 64)
    };
    if engine == "piper" {
        if let (Some(b), Some(m)) = (piper_bin(p), piper_model(p)) {
            let mut argv = vec![b.display().to_string(), "-m".into(), m.display().to_string(), "-f".into(), out.display().to_string()];
            if (p.speed - 1.0).abs() > 0.01 {
                argv.extend(["--length-scale".into(), format!("{:.3}", 1.0 / p.speed)]);
            }
            if run(argv) {
                return true;
            }
        }
    }
    if which("espeak-ng").is_some() {
        let rate = ((p.speed * 150.0) as i32).clamp(80, 400);
        return run(vec!["espeak-ng".into(), "-v".into(), "en-us".into(), "-p".into(), p.pitch.clamp(0, 99).to_string(),
                        "-s".into(), rate.to_string(), "-g".into(), "3".into(), "-w".into(), out.display().to_string()]);
    }
    false
}
