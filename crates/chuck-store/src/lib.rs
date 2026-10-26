//! chuck-store — what Chuck keeps on disk: settings (shared with the Python
//! app), and, as the port continues, chats, the audit ledger and checkpoints.

pub mod settings;

pub use settings::{config_dir, write_private, Settings, SettingsError};
