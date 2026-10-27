//! chuck-sandbox — what keeps agents from hurting the machine: the
//! destructive-command classifier and bubblewrap jails.

pub mod jail;
pub mod outside;
pub mod safety;

pub use jail::{bwrap, git_disarm, now_secs, secret_path, Access, Jail};
pub use outside::{free_to_reach, mount_point, outside_paths, resolve};
pub use safety::{add_noconfirm, classify_code, classify_command, enforce_syu, prepare, Level};
