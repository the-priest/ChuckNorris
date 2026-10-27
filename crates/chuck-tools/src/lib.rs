//! chuck-tools — what agents do on the machine: read their blocks, change
//! files safely, run commands, and work in isolated copies.

pub mod blocks;
pub mod cow;
pub mod fs;
pub mod patch;
pub mod run;
pub mod search;

pub use blocks::{clean_head, edit_pairs, file_blocks, prose, tool_blocks, unclosed, FileBlock, ToolBlock};
pub use cow::{changes_between, diff_between, ChangeKind, MergeResult, Snapshot, Workspace};
pub use fs::{delete_file, edit_file, read_page, safe, unified, write_file, Change, FsError};
pub use run::{bash, kill_all, running, RunOpts, RunResult, Service, Wrap};
pub use search::{fingerprint, glob, grep};
