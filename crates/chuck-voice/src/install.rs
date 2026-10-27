//! Setting the voice up: Kokoro (speaking) and Moonshine (listening), both
//! in one private Python environment with their models next to it. Safe to
//! run again: what is already here and whole is never downloaded twice, a
//! cut-off download is never kept, and Kokoro's files are checked against
//! their published checksums.

use crate::speak::{data_dir, kokoro_files, voice_dir, which};
use sha2::{Digest, Sha256};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const KOKORO_BASE: &str = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/";
/// (file, size, sha256): the release assets are immutable, so a file that
/// matches is whole and one that doesn't is damaged.
const KOKORO_SUMS: &[(&str, u64, &str)] = &[
    ("kokoro-v1.0.onnx", 325_505_369, "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a"),
    ("voices-v1.0.bin", 28_214_398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
];
const STT_MODEL_URL: &str =
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-moonshine-base-en-int8.tar.bz2";
const VAD_URL: &str = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx";

/// One install at a time: they share the environment.
static LOCK: Mutex<()> = Mutex::new(());

pub fn venv() -> PathBuf {
    data_dir().join("voice")
}

fn mb(n: u64) -> String {
    if n >= 1 << 20 { format!("{} MB", n >> 20) } else { format!("{} KB", n >> 10) }
}

/// The environment at `venv` with `packages` in it (checked by importing
/// `module`): made if missing (or made without pip), else just checked.
pub fn ensure_venv(venv: &Path, packages: &[&str], module: &str, say: &dyn Fn(&str)) -> Result<(), String> {
    let py = venv.join("bin/python");
    let imports = || Command::new(&py).args(["-c", &format!("import {module}")]).stdin(Stdio::null())
        .stdout(Stdio::null()).stderr(Stdio::null()).status().is_ok_and(|s| s.success());
    if py.exists() && imports() {
        return Ok(());
    }
    if !py.exists() || !venv.join("bin/pip").exists() {
        say("creating the voice's own Python environment");
        let base = which("python3").ok_or("python3 isn't installed")?;
        let o = Command::new(base).args(["-m", "venv", "--clear"]).arg(venv).stdin(Stdio::null()).output()
            .map_err(|e| e.to_string())?;
        if !o.status.success() {
            return Err(format!("couldn't create a Python environment (on Debian/Ubuntu: apt install python3-venv): {}",
                               tail(&o.stderr, &o.stdout, 200)));
        }
    }
    if !packages.is_empty() {
        say(&format!("installing {} (pip)", packages.join(", ")));
        let o = Command::new(venv.join("bin/pip")).args(["install", "-q", "-U"]).args(packages)
            .env("PIP_NO_INPUT", "1").stdin(Stdio::null()).output().map_err(|e| e.to_string())?;
        if !o.status.success() {
            return Err(format!("pip install {} failed: {}", packages.join(" "), tail(&o.stderr, &o.stdout, 300)));
        }
    }
    if imports() { Ok(()) } else { Err(format!("{module} was installed but doesn't load")) }
}

fn tail(err: &[u8], out: &[u8], n: usize) -> String {
    let t = String::from_utf8_lossy(if err.iter().any(|b| !b.is_ascii_whitespace()) { err } else { out }).trim().to_string();
    let k = t.char_indices().rev().nth(n.saturating_sub(1)).map_or(0, |(i, _)| i);
    t[k..].to_string()
}

/// `url` to `dest`, through `dest.part`: a download that stops short is
/// thrown away, never kept as if it were whole.
pub fn download(url: &str, dest: &Path, label: &str, say: &dyn Fn(&str)) -> Result<(), String> {
    let agent: ureq::Agent = ureq::Agent::config_builder().timeout_connect(Some(Duration::from_secs(30)))
        .timeout_recv_response(Some(Duration::from_secs(60))).http_status_as_error(false)
        .tls_config(ureq::tls::TlsConfig::builder().root_certs(ureq::tls::RootCerts::PlatformVerifier).build())
        .build().into();
    let mut r = agent.get(url).header("User-Agent", "chuck").call().map_err(|e| format!("{label}: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("{label}: the server answered {}", r.status()));
    }
    let total: u64 = r.headers().get("content-length").and_then(|v| v.to_str().ok()?.parse().ok()).unwrap_or(0);
    let tmp = PathBuf::from(format!("{}.part", dest.display()));
    let res = (|| -> Result<u64, String> {
        let mut f = std::fs::File::create(&tmp).map_err(|e| e.to_string())?;
        let mut body = r.body_mut().as_reader();
        let (mut got, mut last) = (0u64, Instant::now() - Duration::from_secs(1));
        let mut buf = vec![0u8; 1 << 20];
        loop {
            let n = body.read(&mut buf).map_err(|e| format!("{label}: {e}"))?;
            if n == 0 {
                break;
            }
            f.write_all(&buf[..n]).map_err(|e| e.to_string())?;
            got += n as u64;
            if last.elapsed() > Duration::from_millis(500) {
                last = Instant::now();
                say(&format!("downloading {label} — {}{}", mb(got), if total > 0 { format!(" of {}", mb(total)) } else { String::new() }));
            }
        }
        f.sync_all().map_err(|e| e.to_string())?;
        Ok(got)
    })();
    match res {
        Ok(got) if total == 0 || got == total => std::fs::rename(&tmp, dest).map_err(|e| e.to_string()),
        Ok(got) => {
            let _ = std::fs::remove_file(&tmp);
            Err(format!("the download of {label} was cut off ({} of {}) — try again", mb(got), mb(total)))
        }
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(e)
        }
    }
}

/// Is `p` the file `name` should be? Its size always; its checksum too with
/// `full` (about a second for the 310 MB model).
fn whole(p: &Path, name: &str, full: bool) -> bool {
    let Some(&(_, size, sum)) = KOKORO_SUMS.iter().find(|s| s.0 == name) else { return p.exists() };
    if !p.metadata().is_ok_and(|m| m.len() == size) {
        return false;
    }
    !full || sha256(p).is_some_and(|h| h == sum)
}

fn sha256(p: &Path) -> Option<String> {
    let mut f = std::fs::File::open(p).ok()?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Some(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// Set up the natural voice (about 350 MB).
pub fn kokoro(say: &dyn Fn(&str)) -> Result<String, String> {
    let _one = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    ensure_venv(&venv(), &["kokoro-onnx"], "kokoro_onnx", say)?;
    let (model, voices) = kokoro_files();
    std::fs::create_dir_all(model.parent().unwrap_or(&model)).map_err(|e| e.to_string())?;
    for (dest, label) in [(&voices, "the voices"), (&model, "the voice model")] {
        let name = dest.file_name().unwrap_or_default().to_string_lossy().into_owned();
        if whole(dest, &name, false) {
            continue; // here and whole: never downloaded twice
        }
        download(&format!("{KOKORO_BASE}{name}"), dest, label, say)?;
        say(&format!("checking {label}"));
        if !whole(dest, &name, true) {
            let _ = std::fs::remove_file(dest);
            return Err(format!("{label} arrived damaged (checksum mismatch) — try again"));
        }
    }
    Ok("natural voice ready (Kokoro)".into())
}

/// Set up voice input (about 250 MB): sherpa-onnx, the Moonshine model and
/// the speech detector.
pub fn listening(say: &dyn Fn(&str)) -> Result<String, String> {
    let _one = LOCK.lock().unwrap_or_else(|e| e.into_inner());
    ensure_venv(&venv(), &["sherpa-onnx"], "sherpa_onnx", say)?;
    let stt = voice_dir().join("stt");
    std::fs::create_dir_all(&stt).map_err(|e| e.to_string())?;
    let vad = stt.join("silero_vad.onnx");
    if !vad.exists() {
        download(VAD_URL, &vad, "the speech detector", say)?;
    }
    let model = stt.join("moonshine-base-en");
    if !crate::listen::MODEL_FILES.iter().all(|f| model.join(f).exists()) {
        let arc = stt.join("moonshine.tar.bz2");
        if !arc.exists() {
            download(STT_MODEL_URL, &arc, "the speech model", say)?;
        }
        say("unpacking the speech model");
        let r = unpack(&arc, &model, crate::listen::MODEL_FILES);
        let _ = std::fs::remove_file(&arc); // only the model files are kept; a bad archive isn't reused
        r?;
    }
    Ok("voice input ready (Moonshine, local)".into())
}

/// The files named `keep` out of the archive `arc`, wherever in it they
/// are, into `to`.
fn unpack(arc: &Path, to: &Path, keep: &[&str]) -> Result<(), String> {
    let tmp = to.with_extension("unpack");
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp).map_err(|e| e.to_string())?;
    let o = Command::new("tar").arg("-xaf").arg(arc).arg("-C").arg(&tmp).arg("--no-same-owner")
        .stdin(Stdio::null()).output().map_err(|e| format!("tar: {e}"))?;
    let res = (|| {
        if !o.status.success() {
            return Err(format!("the speech model didn't unpack: {}", tail(&o.stderr, &o.stdout, 200)));
        }
        std::fs::create_dir_all(to).map_err(|e| e.to_string())?;
        let mut stack = vec![tmp.clone()];
        while let Some(d) = stack.pop() {
            for e in std::fs::read_dir(&d).map_err(|e| e.to_string())?.flatten() {
                let ft = e.file_type().map_err(|e| e.to_string())?;
                if ft.is_dir() {
                    stack.push(e.path());
                } else if ft.is_file() && keep.iter().any(|k| e.file_name() == **k) {
                    std::fs::rename(e.path(), to.join(e.file_name())).map_err(|e| e.to_string())?;
                }
            }
        }
        let missing: Vec<&str> = keep.iter().copied().filter(|f| !to.join(f).exists()).collect();
        if missing.is_empty() { Ok(()) } else { Err(format!("the speech model was incomplete: {}", missing.join(", "))) }
    })();
    let _ = std::fs::remove_dir_all(&tmp);
    res
}

/// Both, as the installer and Settings run it: what each came to.
pub fn all(say: &dyn Fn(&str)) -> Vec<Result<String, String>> {
    vec![kokoro(say), listening(say)]
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    /// A server that answers one request with `body`, saying it is `claim` bytes long.
    fn serve(body: Vec<u8>, claim: usize) -> String {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let at = l.local_addr().unwrap();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut req = [0u8; 1024];
                let _ = s.read(&mut req);
                let _ = write!(s, "HTTP/1.1 200 OK\r\nContent-Length: {claim}\r\nConnection: close\r\n\r\n");
                let _ = s.write_all(&body);
            }
        });
        format!("http://{at}/f")
    }

    #[test]
    fn a_download_is_kept_only_when_whole() {
        let d = std::env::temp_dir().join(format!("chuck-dl-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let dest = d.join("model.bin");
        download(&serve(b"0123456789".to_vec(), 10), &dest, "a file", &|_| {}).unwrap();
        assert_eq!(std::fs::read(&dest).unwrap(), b"0123456789");
        let cut = d.join("cut.bin");
        let e = download(&serve(b"01234".to_vec(), 10), &cut, "a file", &|_| {}).unwrap_err();
        assert!(!cut.exists() && !d.join("cut.bin.part").exists(), "{e}");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_damaged_model_is_told_apart() {
        let d = std::env::temp_dir().join(format!("chuck-sum-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("voices-v1.0.bin");
        std::fs::write(&p, b"short").unwrap();
        assert!(!whole(&p, "voices-v1.0.bin", false), "the wrong size");
        assert!(whole(&p, "anything-else", true), "a file without a checksum: there is enough");
        assert_eq!(sha256(&p).unwrap(), "f9b0078b5df596d2ea19010c001bbd009e651de2c57e8fb7e355f31eb9d3f739");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn the_model_files_come_out_of_the_archive_wherever_they_are() {
        let d = std::env::temp_dir().join(format!("chuck-tar-{}", std::process::id()));
        let src = d.join("src/sherpa-moonshine");
        std::fs::create_dir_all(&src).unwrap();
        for f in ["a.onnx", "tokens.txt", "README.md"] {
            std::fs::write(src.join(f), f).unwrap();
        }
        let arc = d.join("m.tar.bz2");
        if !Command::new("tar").arg("-cjf").arg(&arc).arg("-C").arg(d.join("src")).arg(".").status()
            .is_ok_and(|s| s.success()) {
            return; // no bzip2 here
        }
        let to = d.join("model");
        unpack(&arc, &to, &["a.onnx", "tokens.txt"]).unwrap();
        assert!(to.join("a.onnx").exists() && to.join("tokens.txt").exists() && !to.join("README.md").exists());
        assert!(unpack(&arc, &d.join("m2"), &["a.onnx", "missing.onnx"]).unwrap_err().contains("missing.onnx"));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn an_environment_is_made_once_and_then_only_checked() {
        if which("python3").is_none() {
            return;
        }
        let v = std::env::temp_dir().join(format!("chuck-venv-{}", std::process::id()));
        let said = std::cell::RefCell::new(Vec::new());
        ensure_venv(&v, &[], "json", &|m| said.borrow_mut().push(m.to_string())).unwrap();
        if !v.join("bin/pip").exists() {
            let _ = std::fs::remove_dir_all(&v);
            return; // a Python without ensurepip (Debian without python3-venv)
        }
        assert_eq!(said.borrow().len(), 1);
        ensure_venv(&v, &[], "json", &|m| said.borrow_mut().push(m.to_string())).unwrap();
        assert_eq!(said.borrow().len(), 1, "already there: nothing done");
        assert!(ensure_venv(&v, &[], "no_such_module_here", &|_| {}).is_err());
        let _ = std::fs::remove_dir_all(&v);
    }
}
