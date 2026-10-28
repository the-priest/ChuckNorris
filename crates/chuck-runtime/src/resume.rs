//! A job that paused (out of budget, stuck, stopped, an outage too long)
//! can carry on exactly where it stopped: the Lead's conversation, the
//! criteria and every piece of evidence, and the checkpoint the
//! whole job started from. Kept on disk per folder, so "Keep going" works
//! after Chuck was closed, too; a job that finishes clears it.

use chuck_core::DoneGate;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Resume {
    /// The request, as first asked.
    pub ask: String,
    pub folder: String,
    /// The Lead's conversation so far.
    pub convo: Vec<Value>,
    /// Where the task is in it (after any earlier turns of the chat).
    #[serde(default = "one")]
    pub head: usize,
    pub gate: DoneGate,
    /// The job had changed the folder.
    pub mutated: bool,
    /// The checkpoint taken before the job touched anything.
    pub checkpoint: Option<String>,
    /// Why it paused, and what was missing then.
    pub why: String,
    pub gaps: Vec<String>,
    /// Spent on this job across every run of it.
    pub spent_usd: f64,
    /// How many times it has been continued.
    pub runs: u32,
    pub saved_at: f64,
}

fn one() -> usize {
    1
}

impl Resume {
    /// Paused only because its money ran out (not stuck, not stopped).
    pub fn out_of_budget(&self) -> bool {
        self.why.contains("budget")
    }
}

/// Where paused jobs are kept (the window's and `chuck`'s).
pub fn default_dir() -> PathBuf {
    chuck_store::ledger::data_dir().join("resume")
}

pub fn path_in(dir: &Path, folder: &Path) -> PathBuf {
    let f = folder.canonicalize().unwrap_or_else(|_| folder.to_path_buf());
    let key = chuck_store::checkpoint::sha1_hex(f.to_string_lossy().as_bytes());
    dir.join(format!("{}.json", &key[..16]))
}

pub fn save_in(dir: &Path, r: &Resume) {
    let p = path_in(dir, Path::new(&r.folder));
    let _ = std::fs::create_dir_all(dir);
    if let Ok(t) = serde_json::to_vec(r) {
        let tmp = p.with_extension("tmp");
        if chuck_store::write_private(&tmp, &t).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

pub fn load_in(dir: &Path, folder: &Path) -> Option<Resume> {
    serde_json::from_slice(&std::fs::read(path_in(dir, folder)).ok()?).ok()
}

pub fn clear_in(dir: &Path, folder: &Path) {
    let _ = std::fs::remove_file(path_in(dir, folder));
}

/// The paused job in `folder`, if there is one.
pub fn load(folder: &Path) -> Option<Resume> {
    load_in(&default_dir(), folder)
}
