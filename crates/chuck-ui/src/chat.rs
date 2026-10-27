//! Conversations kept on disk, one per project folder, so nothing you and
//! the fleet said disappears when the window closes. Everything shown in the
//! conversation is an entry here, and is drawn from the entry both live and
//! when it's opened again.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

const KEEP: usize = 4000;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "k", rename_all = "snake_case")]
pub enum Entry {
    User { text: String },
    /// A message typed to the Lead while it worked.
    Steer { text: String },
    Lead {
        text: String,
        model: String,
        /// Said in Chat mode (by Chuck, not the fleet's Lead).
        #[serde(default)]
        chat: bool,
    },
    /// Chat mode proposed work for the fleet: a button sends it.
    Handoff { request: String },
    /// One line of the work lane (Pango markup).
    Act { markup: String },
    /// A card: DONE, INCOMPLETE, a question and its answer, a rewind.
    Card { kind: String, class: String, text: String },
}

/// A task, as last shown.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct TaskSnap {
    pub id: String,
    pub title: String,
    pub state: String,
    pub workers: Vec<String>,
    pub deps: Vec<String>,
    pub criteria: Vec<String>,
    pub note: String,
}

/// Where the last job got to, so the panel shows it when you come back.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Progress {
    pub plan: Vec<(String, bool)>,
    pub finished: bool,
    pub tasks: Vec<TaskSnap>,
    pub phase: String,
    pub unmet: Vec<String>,
    pub board: Vec<(String, String, Option<String>, Vec<String>)>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Saved {
    pub folder: String,
    /// What the Lead is told of earlier turns: (role, text).
    pub history: Vec<(String, String)>,
    pub entries: Vec<Entry>,
    #[serde(default)]
    pub progress: Progress,
}

/// The key Chat-mode conversations without a project folder are kept under.
pub fn loose() -> PathBuf {
    PathBuf::from("chuck:chat")
}

fn dir() -> PathBuf {
    chuck_store::ledger::data_dir().join("fleet-chats")
}

pub fn path_for(folder: &Path) -> PathBuf {
    let key = chuck_store::checkpoint::sha1_hex(folder.to_string_lossy().as_bytes());
    dir().join(format!("{}.json", &key[..16]))
}

pub fn load(folder: &Path) -> Option<Saved> {
    let text = std::fs::read_to_string(path_for(folder)).ok()?;
    serde_json::from_str(&text).ok()
}

/// Written whole and renamed into place, readable only by you.
pub fn save(folder: &Path, s: &Saved) {
    let mut s = s.clone();
    if s.entries.len() > KEEP {
        let cut = s.entries.len() - KEEP;
        s.entries.drain(..cut);
    }
    let p = path_for(folder);
    let _ = std::fs::create_dir_all(dir());
    if let Ok(text) = serde_json::to_string(&s) {
        let tmp = p.with_extension("tmp");
        if chuck_store::write_private(&tmp, text.as_bytes()).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

/// Start over: the old conversation is kept beside, dated.
pub fn archive(folder: &Path) {
    let p = path_for(folder);
    if p.exists() {
        let stamp = chuck_store::ledger::local_stamp("%Y%m%d-%H%M%S");
        let _ = std::fs::rename(&p, p.with_extension(format!("{stamp}.json")));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_conversation_comes_back_as_it_was() {
        let home = std::env::temp_dir().join(format!("chuck-chat-{}", std::process::id()));
        std::fs::create_dir_all(&home).unwrap();
        std::env::set_var("HOME", &home);
        let folder = home.join("proj");
        let s = Saved { folder: folder.display().to_string(), history: vec![("user".into(), "hi".into())],
                        entries: vec![Entry::User { text: "hi".into() },
                                      Entry::Lead { text: "**hello**".into(), model: "m".into(), chat: true },
                                      Entry::Handoff { request: "install rg".into() },
                                      Entry::Act { markup: "<b>x</b>".into() },
                                      Entry::Card { kind: "DONE".into(), class: "done-card".into(), text: "ok".into() }],
                        progress: Progress { plan: vec![("a".into(), true)], finished: true, ..Default::default() } };
        save(&folder, &s);
        assert_eq!(load(&folder), Some(s));
        archive(&folder);
        assert_eq!(load(&folder), None);
        // an old file, from before Chat mode, still opens
        std::fs::write(path_for(&folder), r#"{"folder":"x","history":[],"entries":[{"k":"lead","text":"t","model":"m"}]}"#).unwrap();
        assert_eq!(load(&folder).unwrap().entries, vec![Entry::Lead { text: "t".into(), model: "m".into(), chat: false }]);
        let _ = std::fs::remove_dir_all(&home);
    }
}
