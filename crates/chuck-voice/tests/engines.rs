//! The whole voice pipeline with stand-ins for the engines and the sound
//! server (a private HOME and PATH): Kokoro streamed gaplessly in order, a
//! broken Kokoro handing over to another voice, Stop, and listening.

use chuck_voice::{Ears, Heard, Prefs, Voice};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

fn exe(p: &Path, body: &str) {
    std::fs::write(p, body).unwrap();
    std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o755)).unwrap();
}

/// A private HOME with a "voice environment" and fake players, once.
fn home() -> &'static PathBuf {
    static H: OnceLock<PathBuf> = OnceLock::new();
    H.get_or_init(|| {
        let h = std::env::temp_dir().join(format!("chuck-voice-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&h);
        let data = h.join(".local/share/chucknorris");
        std::fs::create_dir_all(data.join("voice/bin")).unwrap();
        std::fs::create_dir_all(data.join("voices/kokoro")).unwrap();
        std::fs::create_dir_all(data.join("voices/stt/moonshine-base-en")).unwrap();
        for f in ["voices/kokoro/kokoro-v1.0.onnx", "voices/kokoro/voices-v1.0.bin", "voices/stt/silero_vad.onnx"] {
            std::fs::write(data.join(f), "x").unwrap();
        }
        for f in ["preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt"] {
            std::fs::write(data.join("voices/stt/moonshine-base-en").join(f), "x").unwrap();
        }
        exe(&data.join("voice/bin/python"), &format!("#!/bin/sh\nexec python3 {}/tests/fake_voice.py \"$@\"\n",
                                                     env!("CARGO_MANIFEST_DIR")));
        let bin = h.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        // a raw player that keeps what it's given; a wav player that notes the file
        exe(&bin.join("paplay"), "#!/bin/sh\nif [ \"$1\" = --raw ]; then cat >> \"$HOME/played.raw\"; \
            else echo \"$1\" >> \"$HOME/played.wav.log\"; fi\n");
        exe(&bin.join("espeak-ng"), "#!/bin/sh\nwhile [ $# -gt 1 ]; do [ \"$1\" = -w ] && out=\"$2\"; shift; done\n\
            cat > /dev/null; head -c 200 /dev/zero > \"$out\"; echo spoken >> \"$HOME/espeak.log\"\n");
        std::env::set_var("HOME", &h);
        std::env::set_var("PATH", format!("{}:{}", bin.display(), std::env::var("PATH").unwrap_or_default()));
        h
    })
}

fn wait_for(cond: impl Fn() -> bool, secs: u64) -> bool {
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(secs) {
        if cond() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    cond()
}

#[test]
fn speaking_listening_and_stopping() {
    if chuck_voice::speak::which("python3").is_none() {
        return;
    }
    let h = home();
    let voice = Voice::new();
    let p = Prefs::default();
    assert_eq!(voice.engine_for(&p), Some("kokoro"));

    // Kokoro: every chunk, in order, into one player stream, with a breath between
    let text = "Hello there. ".to_string() + &"This is a longer sentence that keeps the voice busy for a while. ".repeat(12);
    let want_chunks = chuck_voice::clean::chunks(&chuck_voice::clean::clean(&text), 420, Some(60));
    voice.speak(&text, &p);
    let raw = h.join("played.raw");
    let gap = (24_000.0 * 0.16) as usize * 2;
    let per = 4800 + gap;
    assert!(wait_for(|| std::fs::metadata(&raw).is_ok_and(|m| m.len() as usize >= per * want_chunks.len()), 20),
            "every chunk played");
    std::thread::sleep(Duration::from_millis(300));
    let pcm = std::fs::read(&raw).unwrap();
    // the 0.1 s probe of silence comes first, then chunk 1, 2, 3… in order
    let probe = 2400 * 2;
    let firsts: Vec<u8> = (0..want_chunks.len()).map(|i| pcm[probe + i * per]).collect();
    assert_eq!(firsts, (1..=want_chunks.len() as u8).collect::<Vec<_>>(), "in order, no chunk lost");
    assert_eq!(pcm.len(), probe + per * want_chunks.len());
    let log = std::fs::read_to_string(h.join("kokoro.log")).unwrap();
    assert_eq!(log.lines().next(), Some("Hello there."), "a short first chunk: speech starts sooner");
    // a second reply uses the same warm Kokoro (loading the model costs seconds)
    voice.speak("And one more thing.", &p);
    assert!(wait_for(|| std::fs::read_to_string(h.join("kokoro.log")).unwrap_or_default().contains("And one more thing."), 20));
    assert_eq!(std::fs::read_to_string(h.join("kokoro.starts")).unwrap().lines().count(), 1, "Kokoro stays warm");
    std::thread::sleep(Duration::from_millis(500));

    // Stop silences it before the end
    std::fs::remove_file(&raw).unwrap();
    std::env::set_var("FAKE_SLOW", "1");
    let voice2 = Voice::new();
    voice2.speak(&text, &p);
    assert!(wait_for(|| std::fs::metadata(&raw).is_ok_and(|m| m.len() > 0), 20));
    voice2.stop();
    std::thread::sleep(Duration::from_millis(1500));
    let played = std::fs::metadata(&raw).map(|m| m.len() as usize).unwrap_or(0);
    assert!(played < probe + per * want_chunks.len() / 2, "stopped early: {played}");
    std::env::remove_var("FAKE_SLOW");

    // a Kokoro that breaks mid-reply: another voice reads the rest
    let voice3 = Voice::new();
    voice3.speak("First part is fine. BREAK here, and the rest must still be heard.", &Prefs::default());
    assert!(wait_for(|| h.join("espeak.log").exists(), 20), "espeak-ng took over");
    assert_eq!(voice3.engine_for(&p), Some("espeak"), "Kokoro rests for a while after breaking");

    // listening: what was said comes back, trimmed
    let ears = Ears::new();
    let (tx, rx) = std::sync::mpsc::channel();
    ears.listen(tx, "", 8.0, 30.0);
    let mut got = Vec::new();
    while let Ok(ev) = rx.recv_timeout(Duration::from_secs(20)) {
        let end = matches!(ev, Heard::Final(_) | Heard::Error(_) | Heard::Nothing);
        got.push(ev);
        if end {
            break;
        }
    }
    assert_eq!(got, vec![Heard::Listening, Heard::Speech, Heard::Level(0.25), Heard::Final("hello chuck".into())]);
    assert!(!ears.busy());

    // nothing outlives the app
    voice.shutdown();
    voice2.shutdown();
    voice3.shutdown();
    ears.shutdown();
    chuck_tools::kill_all();
    std::thread::sleep(Duration::from_millis(300));
    let left = std::fs::read_dir("/proc").unwrap().flatten().filter(|e| {
        let cmd = String::from_utf8_lossy(&std::fs::read(e.path().join("cmdline")).unwrap_or_default()).into_owned();
        cmd.contains("fake_voice.py") && cmd.contains(&*h.display().to_string())
    }).count();
    assert_eq!(left, 0);
}
