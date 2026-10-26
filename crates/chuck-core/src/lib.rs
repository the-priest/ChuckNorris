//! chuck-core — the agent OS without I/O: who agents are, what they may do
//! and spend, the kernel that schedules them, the fleet, and the Genuine Done
//! gate that decides when work is finished.

pub mod donegate;
pub mod evidence;
pub mod fleet;
pub mod kernel;
pub mod types;

pub use donegate::{DoneGate, Phase, Progress, Scale, Stall, Unmet};
pub use evidence::{Criterion, CriterionId, Evidence, EvidenceKind, Verdict};
pub use fleet::{FleetConfig, Submission, Task, DEEPSEEK_MODEL, GLM_MODEL};
pub use kernel::{Kernel, ProcInfo, ProcState, Signal, SpawnSpec};
pub use types::{tool_cap, AgentId, Caps, ModelFamily, Quota, Role, Spend};
