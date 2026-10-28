//! chuck-tools — what agents do on the machine: read their blocks, change
//! files safely, run commands, and keep the folder as a job found it.

pub mod blocks;
pub mod native;
pub mod snapshot;
pub mod fs;
pub mod patch;
pub mod run;
pub mod search;

pub use native::normalize_calls;
pub use blocks::{clean_head, edit_pairs, file_blocks, prose, native_tool_markup, tool_blocks, unclosed, without_made_up_turns, FileBlock, ToolBlock};
pub use snapshot::{changes_between, diff_between, ChangeKind, Snapshot};
pub use fs::{append_file, delete_file, edit_file, read_page, safe, unified, write_file, Change, FsError};
pub use run::{bash, kill_all, kill_env, kill_session, pids_with_env, running, session, session_pids, tag, RunOpts, RunResult, Service, Wrap,
    SESSION_VAR};
pub use search::{fingerprint, glob, grep, listing};
