//! Code intelligence: where a name is defined, who uses it, renaming it
//! everywhere it's really used, and the repo map — by meaning through a
//! language server when one is installed, by syntax when not (and saying so).

pub mod lsp;
pub mod syntax;

pub use lsp::{definitions, references, rename, LspError, Manager};
pub use syntax::{repo_map, Def, Ref};
