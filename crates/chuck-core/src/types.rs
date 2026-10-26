//! The vocabulary every crate shares: who an agent is, what it may do, what
//! it may spend.

use serde::{Deserialize, Serialize};
use std::fmt;

/// A process id in the agent kernel. PID 1 is the user's chat (the Lead).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct AgentId(pub u32);

impl fmt::Display for AgentId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

/// The model family an agent thinks with. SiliconFlow only.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ModelFamily {
    /// DeepSeek-V4.1-Flash-1017: the Lead and four workers.
    DeepSeek,
    /// GLM-5.3-Flash: four workers.
    Glm,
}

/// What an agent is for. The rules that make the fleet a team rather than
/// nine chatbots live on this type, not in prompts.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Role {
    /// Talks to the user, plans, writes acceptance criteria, assigns work,
    /// develops the critical path, reviews every result, owns "done".
    Lead,
    /// Implementation-heavy work: code, refactors, tests, tooling.
    DeepSeekWorker,
    /// Parallel implementation, exploration, alternatives, review, docs.
    GlmWorker,
    /// A daemon woken by its trigger: looks and reports, never changes.
    Daemon,
}

impl Role {
    pub fn family(self) -> ModelFamily {
        match self {
            Role::GlmWorker => ModelFamily::Glm,
            Role::Lead | Role::DeepSeekWorker | Role::Daemon => ModelFamily::DeepSeek,
        }
    }

    pub fn is_worker(self) -> bool {
        matches!(self, Role::DeepSeekWorker | Role::GlmWorker)
    }

    /// Only the Lead may declare a job done. Workers submit; the Lead decides.
    pub fn may_declare_done(self) -> bool {
        matches!(self, Role::Lead)
    }

    /// Only the Lead may bring work into the user's folder (through the gates).
    pub fn may_merge(self) -> bool {
        matches!(self, Role::Lead)
    }

    /// Only the Lead talks to the user as the answer.
    pub fn talks_to_user(self) -> bool {
        matches!(self, Role::Lead)
    }

    pub fn default_caps(self) -> Caps {
        match self {
            Role::Lead => Caps::ALL,
            Role::DeepSeekWorker | Role::GlmWorker => Caps::READ | Caps::WRITE | Caps::EXEC | Caps::NET,
            Role::Daemon => Caps::READ | Caps::EXEC,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Role::Lead => "lead",
            Role::DeepSeekWorker => "deepseek",
            Role::GlmWorker => "glm",
            Role::Daemon => "daemon",
        }
    }
}

/// Capabilities, clipped to the parent's on spawn: a child can never do more
/// than the agent that started it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Caps(u8);

impl Caps {
    pub const NONE: Caps = Caps(0);
    pub const READ: Caps = Caps(1);
    pub const WRITE: Caps = Caps(2);
    pub const EXEC: Caps = Caps(4);
    pub const NET: Caps = Caps(8);
    pub const SPAWN: Caps = Caps(16);
    pub const ALL: Caps = Caps(31);

    pub fn has(self, c: Caps) -> bool {
        self.0 & c.0 == c.0
    }

    pub fn clip(self, parent: Caps) -> Caps {
        Caps(self.0 & parent.0)
    }

    pub fn without(self, c: Caps) -> Caps {
        Caps(self.0 & !c.0)
    }

    pub fn names(self) -> Vec<&'static str> {
        [(Caps::READ, "read"), (Caps::WRITE, "write"), (Caps::EXEC, "exec"), (Caps::NET, "net"),
         (Caps::SPAWN, "spawn")]
            .into_iter()
            .filter(|(c, _)| self.has(*c))
            .map(|(_, n)| n)
            .collect()
    }

    pub fn parse(names: &str) -> Caps {
        let mut c = Caps::NONE;
        for w in names.split(|ch: char| ch == ',' || ch.is_whitespace()) {
            c = c | match w.trim() {
                "read" => Caps::READ,
                "write" => Caps::WRITE,
                "exec" => Caps::EXEC,
                "net" => Caps::NET,
                "spawn" => Caps::SPAWN,
                "all" => Caps::ALL,
                _ => Caps::NONE,
            };
        }
        c
    }
}

impl std::ops::BitOr for Caps {
    type Output = Caps;
    fn bitor(self, o: Caps) -> Caps {
        Caps(self.0 | o.0)
    }
}

/// The capability each tool needs. A tool not listed needs none.
pub fn tool_cap(tool: &str) -> Option<Caps> {
    Some(match tool {
        "read" | "grep" | "glob" | "map" | "page" | "find" | "def" | "refs" | "look" => Caps::READ,
        "write" | "edit" | "patch" | "rename" | "rmfile" => Caps::WRITE,
        "bash" | "sh" | "python" | "py" | "node" | "runtests" | "diagnose" => Caps::EXEC,
        "search" | "fetch" | "browse" => Caps::NET,
        "spawn" | "task" | "assign" => Caps::SPAWN,
        _ => return None,
    })
}

/// Limits on what a process may spend. `None` means no limit of that kind.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Quota {
    pub tokens: Option<u64>,
    pub usd: Option<f64>,
    pub seconds: Option<f64>,
}

impl Quota {
    pub fn tokens(n: u64) -> Quota {
        Quota { tokens: Some(n), ..Quota::default() }
    }
}

/// What a process (or a process and all its descendants) has spent.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Spend {
    pub prompt: u64,
    pub completion: u64,
    pub usd: f64,
    pub calls: u64,
}

impl Spend {
    pub fn tokens(&self) -> u64 {
        self.prompt + self.completion
    }
}
