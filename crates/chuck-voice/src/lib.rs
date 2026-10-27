//! Chuck's voice: replies read aloud (Kokoro, else Piper, else espeak-ng)
//! and talking to Chuck (Moonshine, on this machine). The engines are the
//! ones the installer set up for the Python app; nothing leaves the machine.

pub mod clean;
pub mod listen;
pub mod speak;

pub use listen::{Ears, Heard};
pub use speak::{Prefs, Voice};
