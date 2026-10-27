//! Talking to Chuck: speech to text on this machine, nothing uploaded.
//! Moonshine (through sherpa-onnx) with the Silero voice-activity detector,
//! kept loaded in stt_server.py (in the voice environment), so what you say
//! is transcribed the moment you pause. The microphone is read with
//! parecord, else pw-record, else arecord, as raw 16 kHz mono.

use crate::speak::{data_dir, sidecar, voice_dir, voice_python, which, Lasting, STT_SERVER};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub(crate) const MODEL_FILES: &[&str] = &["preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx",
    "cached_decode.int8.onnx", "tokens.txt"];

fn model_dir() -> PathBuf {
    voice_dir().join("stt/moonshine-base-en")
}

fn vad() -> PathBuf {
    voice_dir().join("stt/silero_vad.onnx")
}

pub fn ready() -> bool {
    voice_python().is_some() && MODEL_FILES.iter().all(|f| model_dir().join(f).exists()) && vad().exists()
}

/// What the ears report.
#[derive(Clone, Debug, PartialEq)]
pub enum Heard {
    Listening,
    /// You started talking.
    Speech,
    /// The input level (for a meter), about ten a second.
    Level(f32),
    /// What you said.
    Final(String),
    /// No speech before the wait ran out.
    Nothing,
    Cancelled,
    Error(String),
}

/// Recorders to try, best first (or the user's own command alone).
pub fn recorders(custom: &str) -> Vec<Vec<String>> {
    let custom = custom.trim();
    if !custom.is_empty() {
        return vec![match custom.strip_prefix("sh -c ") {
            Some(rest) => vec!["sh".into(), "-c".into(), rest.into()],
            None => custom.split_whitespace().map(String::from).collect(),
        }];
    }
    let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
    // --latency-msec: else pipewire-pulse hands audio over in 2 s fragments
    [v(&["parecord", "--raw", "--rate=16000", "--channels=1", "--format=s16le", "--latency-msec=60"]),
     v(&["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "--raw", "-"]),
     v(&["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", "-"]),
     v(&["arecord", "-q", "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"])]
        .into_iter().filter(|a| which(&a[0]).is_some()).collect()
}

pub fn status(custom: &str) -> String {
    if !ready() {
        return "not installed — Settings › Voice › Set up, or `chuck voice install` (Moonshine, about 250 MB)".into();
    }
    match recorders(custom).first() {
        Some(r) => format!("Moonshine, on this machine · microphone via {}", r[0]),
        None => "no recorder: install pipewire (pw-record), pulseaudio-utils or alsa-utils".into(),
    }
}

struct Server {
    proc: Lasting,
}

impl Drop for Server {
    fn drop(&mut self) {
        self.proc.kill();
    }
}

#[derive(Default)]
struct Inner {
    server: Option<Server>,
    /// Where the current listen's events go.
    to: Option<Sender<Heard>>,
    busy: bool,
    /// The recorder that worked last time: tried first.
    good: Option<Vec<String>>,
}

/// The warm listener. Cheap to clone; one per app.
#[derive(Clone, Default)]
pub struct Ears {
    inner: Arc<Mutex<Inner>>,
}

impl Ears {
    pub fn new() -> Ears {
        Ears::default()
    }

    pub fn busy(&self) -> bool {
        self.inner.lock().unwrap().busy
    }

    fn start(&self) -> Result<(), String> {
        if self.inner.lock().unwrap().server.as_ref().is_some_and(|s| s.proc.alive()) {
            return Ok(());
        }
        let py = voice_python().ok_or("voice input isn't installed")?;
        let script = sidecar("stt_server.py", STT_SERVER).map_err(|e| e.to_string())?;
        let _ = std::fs::create_dir_all(data_dir());
        let argv = [py.display().to_string(), "-u".into(), script.display().to_string(), model_dir().display().to_string(),
                    vad().display().to_string()];
        let mut proc = Lasting::start(&argv, &data_dir())?;
        let out = proc.stdout.take().ok_or("no stdout")?;
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let inner = self.inner.clone();
        std::thread::Builder::new().name("stt-read".into()).spawn(move || {
            let mut first = Some(ready_tx);
            for l in BufReader::new(out).lines() {
                let Ok(l) = l else { break };
                let Ok(ev) = serde_json::from_str::<Value>(&l) else { continue };
                if let Some(tx) = first.take() {
                    let _ = tx.send(ev);
                    continue;
                }
                let kind = ev.get("event").and_then(Value::as_str).unwrap_or("");
                let mut i = inner.lock().unwrap();
                if kind == "recorder" {
                    i.good = ev.get("argv").and_then(|a| serde_json::from_value(a.clone()).ok());
                    continue;
                }
                let heard = match kind {
                    "listening" => Heard::Listening,
                    "speech" => Heard::Speech,
                    "level" => Heard::Level(ev.get("rms").and_then(Value::as_f64).unwrap_or(0.0) as f32),
                    "final" => Heard::Final(ev.get("text").and_then(Value::as_str).unwrap_or("").trim().to_string()),
                    "nothing" => Heard::Nothing,
                    "cancelled" => Heard::Cancelled,
                    "error" => Heard::Error(ev.get("error").and_then(Value::as_str).unwrap_or("error").to_string()),
                    _ => continue,
                };
                let end = matches!(heard, Heard::Final(_) | Heard::Nothing | Heard::Cancelled | Heard::Error(_));
                if let Some(to) = &i.to {
                    let _ = to.send(heard);
                }
                if end {
                    i.busy = false;
                    i.to = None;
                }
            }
            // the server is gone: whoever was listening must hear so
            let mut i = inner.lock().unwrap();
            if i.busy {
                if let Some(to) = i.to.take() {
                    let _ = to.send(Heard::Error("the listener stopped".into()));
                }
                i.busy = false;
            }
            i.server = None;
        }).map_err(|e| e.to_string())?;
        let first = ready_rx.recv_timeout(Duration::from_secs(90))
            .map_err(|_| "the speech model didn't load within 90 s".to_string())?;
        if first.get("event").and_then(Value::as_str) != Some("ready") {
            proc.kill();
            return Err(first.get("error").and_then(Value::as_str).unwrap_or("the speech model didn't load").to_string());
        }
        self.inner.lock().unwrap().server = Some(Server { proc });
        Ok(())
    }

    /// Load the model in the background, so the first listen is quick.
    pub fn warm(&self) {
        if ready() {
            let me = self.clone();
            std::thread::Builder::new().name("stt-warm".into()).spawn(move || {
                let _ = me.start();
            }).ok();
        }
    }

    /// Listen for one utterance; events go to `to`. Returns at once.
    pub fn listen(&self, to: Sender<Heard>, custom_mic: &str, max_wait: f64, max_len: f64) {
        {
            let mut i = self.inner.lock().unwrap();
            if i.busy {
                return;
            }
            i.busy = true;
            i.to = Some(to.clone());
        }
        let me = self.clone();
        let custom = custom_mic.to_string();
        std::thread::Builder::new().name("stt-listen".into()).spawn(move || {
            let fail = |why: String| {
                let mut i = me.inner.lock().unwrap();
                i.busy = false;
                i.to = None;
                let _ = to.send(Heard::Error(why));
            };
            let mut chain = recorders(&custom);
            if chain.is_empty() {
                return fail(status(&custom));
            }
            if let Err(e) = me.start() {
                return fail(e);
            }
            let mut i = me.inner.lock().unwrap();
            if !i.busy {
                return; // cancelled while the model loaded
            }
            if let Some(g) = i.good.clone() {
                chain.retain(|a| *a != g);
                chain.insert(0, g);
            }
            let msg = json!({"cmd": "listen", "recorders": chain, "max_wait": max_wait, "max_len": max_len});
            let sent = i.server.as_mut().map(|s| writeln!(s.proc.stdin, "{msg}").and_then(|_| s.proc.stdin.flush()));
            if !matches!(sent, Some(Ok(()))) {
                drop(i);
                fail("the listener stopped".into());
            }
        }).ok();
    }

    /// Stop listening (what was heard so far is dropped).
    pub fn cancel(&self) {
        let mut i = self.inner.lock().unwrap();
        if !i.busy {
            return;
        }
        let sent = i.server.as_mut().map(|s| writeln!(s.proc.stdin, "{}", json!({"cmd": "cancel"})).and_then(|_| s.proc.stdin.flush()));
        if !matches!(sent, Some(Ok(()))) {
            // still loading: the listen thread sees busy=false and doesn't start
            i.busy = false;
            if let Some(to) = i.to.take() {
                let _ = to.send(Heard::Cancelled);
            }
        }
    }

    pub fn shutdown(&self) {
        let mut i = self.inner.lock().unwrap();
        i.server = None;
        i.busy = false;
        i.to = None;
    }
}
