# Chuck in Rust — architecture

This branch (`rust-rewrite`) replaces the Python harness with a Rust one. The
product stays the same — a Linux desktop operating system for AI agents —
with two things made central and enforced in code:

1. **A fleet, not a chatbot.** One lead agent plans, assigns, develops and
   quality-checks; eight workers build in parallel in isolated copies.
2. **Genuine Done.** Nothing — not a worker, not the lead — can report a job
   finished without written acceptance criteria, recorded evidence for each
   one, a verification run after the last change, and the lead's review.

Remote models only think. The local Rust runtime schedules, isolates, runs
tools, records evidence, enforces the gates and merges.

---

## 1. The model fleet (SiliconFlow only)

All model traffic goes to SiliconFlow (`https://api.siliconflow.com/v1`, or
the user's configured SiliconFlow base URL) with the key from Settings. No
other provider is in the default path, and the client refuses a base URL
that isn't SiliconFlow unless the user explicitly overrides it.

| Role | Count | Model (intended id) |
|---|---:|---|
| **Lead** — talks to the user, plans, writes acceptance criteria, assigns, is main developer on the critical path, reviews every result, owns *done* | 1 | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| **DeepSeek worker** — implementation, refactors, tests, tooling, hard logic | 4 | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| **GLM worker** — parallel implementation, exploration, alternate approaches, review and critique, docs, long-context sweeps | 4 | `zai-org/GLM-5.3-Flash` |

A full team is **9 concurrent model-backed agents**. Model ids are resolved
against the account's live model list at startup (`/models`): the intended
id is used if present, otherwise the closest live id of the same family
(`DeepSeek-V4.1-Flash*`, `GLM-5.3-Flash*`), and the resolution is shown in
Settings and `/top`.

```
                         ┌──────────────────────────────┐
              user  ◄──► │  LEAD  (DeepSeek-V4.1-Flash)  │  plan · criteria · assign
                         │  main developer · QA · DONE   │  review · accept / reject
                         └──────────────┬───────────────┘
                 assigns tasks          │          evidence + diffs back
        ┌──────────┬──────────┬─────────┼─────────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼         ▼         ▼          ▼          ▼          ▼
     DS-1       DS-2       DS-3      DS-4      GLM-1      GLM-2      GLM-3      GLM-4
   (copy 1)   (copy 2)   (copy 3)  (copy 4)  (copy 5)   (copy 6)   (copy 7)   (copy 8)
        └────────── each in its own copy-on-write folder, sandboxed ───────────┘
                         merge into the user's folder only through the Lead
                         + the test gate + the DoneGate
```

### Role rules — enforced in `chuck-core::fleet`

- `Role::Lead` is the only role whose output reaches the user as the answer,
  and the only role that may call `DoneGate::declare_done`.
- Workers (`Role::DeepSeekWorker`, `Role::GlmWorker`) receive a `Task` with
  acceptance criteria from the Lead, work in an isolated `Workspace`, and
  finish by **submitting** a `Submission` (report + evidence + diff). A
  submission is never a merge.
- `Lead::review(submission)` runs the critic pass: checks every criterion has
  evidence, re-runs the verification commands on the worker's copy, and
  either `Accept`s (the merge is then attempted through the test gate) or
  `Reject`s with concrete defects, which become the worker's next turn.
- The Lead may give the same subproblem to a DeepSeek and a GLM worker
  (`Assignment::Race`) and accept the better verified result.
- The Lead scales the team to the job: a one-line fix uses no workers; the
  default architecture always supports the full 1 + 4 + 4.
- Budgets: every agent is a process with a quota; spend is charged to it and
  every ancestor; a child can never spend more than its parent has left.

---

## 2. Genuine Done — the state machine

```
            ┌───────────┐  criteria written   ┌────────────┐  plan accepted   ┌─────────┐
  task ───► │  Intake   │ ──────────────────► │  Criteria  │ ───────────────► │ Planned │
            └───────────┘                     └────────────┘                  └────┬────┘
                 ▲ mutation before criteria = REFUSED                              │ first change
                 │                                                                 ▼
            ┌────┴──────┐  verify fails / new change   ┌───────────┐  change  ┌─────────┐
            │ Verifying │ ◄─────────────────────────── │  Working  │ ◄─────── │         │
            └────┬──────┘ ──────────────────────────►  └───────────┘          └─────────┘
                 │ all verification green, every criterion has evidence
                 ▼
            ┌───────────┐  Lead rejects (defects)   ┌───────────┐
            │  Review   │ ────────────────────────► │  Working  │
            └────┬──────┘                           └───────────┘
                 │ Lead accepts + test gate passes + merge clean
                 ▼
            ┌───────────┐                  budget exhausted anywhere ┌──────────────┐
            │   Done    │                  ─────────────────────────►│ Paused(gaps) │
            └───────────┘                                            └──────────────┘
```

`DoneGate::check` refuses completion — returning the exact unmet items — unless
**all** of these hold, each backed by recorded `Evidence`:

1. **Acceptance criteria** were written (and accepted by the Lead) *before*
   the first mutating tool call. A write/edit/patch/command that changes
   files before criteria exist is refused by the tool guard.
2. **A plan** exists for non-trivial work (more than one criterion or more
   than one file); large work is a mission graph spread across the fleet.
3. **Every criterion has evidence**: a command with its exit code and
   output, a diff, a test run, or a look at a screenshot — linked to the
   criterion by id.
4. **Verification after the last change**: the project's tests / build /
   linters (or the criterion's own verify command) ran *after* the last
   mutation and passed. Any later change resets this.
5. **Craftsmanship checks** on the diff: no `TODO`/`FIXME`/`unimplemented!`/
   `pass  # stub` in changed code paths, no placeholder strings, error
   handling present where commands can fail — flagged items must be fixed or
   explicitly justified to the Lead.
6. **Proportion**: an empty or tiny diff for a large ask is rejected unless
   the agent justifies it and the Lead accepts the justification.
7. **The Lead's review** accepted it (workers can never self-certify).
8. **Merges are gated**: a mission or background merge is blocked while its
   tests fail or a criterion is unmet.

**Budget exhaustion is never "done."** It moves the job to
`Paused(gaps)` with an honest list of unmet criteria and what remains.

Shallow-loop detection: a turn with lots of talk, no mutation and no
verification (or the same failing action repeated) makes the Lead replan with
tighter criteria instead of continuing to spin.

The UI shows the gate: each criterion with its evidence, verification state,
the Lead's verdict, and an **INCOMPLETE** label on anything not through it.

---

## 3. Chuck OS semantics preserved

| Python module | Rust home | Notes |
|---|---|---|
| `kernel.py` | `chuck-core::kernel` | processes, quotas over ancestors, signals, mailboxes, blackboard, syscall log, slot scheduler with priority + ageing |
| `worker.py`, `agents.py` | `chuck-runtime::{agent, lead}` | the worker loop and the Lead's orchestration; real threads, one per agent |
| `missions.py` | `chuck-core::mission` | task DAG (Kahn), ready/blocked, test-gated merges |
| `daemons.py` | `chuck-core::daemon` | triggers checked by plain code for free; read-only policy |
| `budget.py`, `pricing.py`, `governor.py` | `chuck-core::budget`, `chuck-model::pricing` | day/month ledger, per-model prices |
| `hibernate.py`, `checkpoint.py` | `chuck-store` | per-step checkpoints, shadow git, replay / fork, rewind |
| `ledger.py`, `crashlog.py` | `chuck-store::ledger`, `chuck-store::crashlog` | hash-chained audit log; redacted crash log |
| `safety.py` | `chuck-sandbox::safety` | critical / danger classifier (same rules) |
| bubblewrap code in `chucknorris.py` | `chuck-sandbox::jail` | modes Full / Sandboxed / Offline / Read-only, hidden secrets, read-only `.git` hooks/config, git disarm |
| `cow.py` | `chuck-tools::cow` | copy-on-write workspaces, three-way merge, links never followed |
| `builder.py`, file blocks | `chuck-tools` | read/write/edit/patch/grep/glob/map/bash, fenced-block parser |
| `net.py`, `web.py` | `chuck-model::http`, `chuck-tools::web` | one HTTP layer, proxy-aware, capped bodies |
| `config.py`, `chats.py`, `memory.py` | `chuck-store` | settings 0600, chats, memory |
| GTK window | `chuck-ui` (gtk4-rs + libadwaita) | chat, work lane, fleet panel, DoneGate view, settings |
| `voice.py`, `listen.py` | `chuck-voice` | Rust drives the existing Kokoro / Moonshine servers (they need Python's ONNX runtime); players probed, never silent |

## 4. Crates

```
crates/
  chuck-core      types, kernel, fleet, DoneGate, evidence, missions, daemons, budget (no I/O)
  chuck-runtime   the fleet at work: worker loop, Lead orchestration (assign, race, review,
                  re-verify, merge), the critic pass, guarded execution of every block
  chuck-model     SiliconFlow client: SSE streaming, reasoning budget, stall timeout, retries,
                  fleet model resolution, pricing
  chuck-sandbox   bubblewrap argv, secrets, command safety classifier, git guard
  chuck-tools     tools + evidence capture, fenced-block parser, cow workspaces, checkpoints
  chuck-store     settings, chats, audit ledger, missions/hibernate stores, crash log
  chuck-voice     TTS/STT sidecar control, audio player probe
  chuck-ui        GTK4 + libadwaita front end
  chuck           the binary: `chuck` (window), `chuck run "task"` (headless), `chuck top`
```

Concurrency model: one OS thread per agent (nine at most by default), channels
between them, no async runtime — a blocking HTTP stream per agent is the
simplest correct design at this scale and keeps idle cost at zero (daemons are
a single timer thread that checks triggers in plain code).

## 5. Performance targets

- Cold start of the binary to a ready window: under 150 ms on a laptop
  (Python: ~1.2 s import + GTK init).
- Per-agent overhead: one thread + its message history; under 2 MB RSS per idle
  agent (Python: an interpreter-wide GIL and ~30 MB baseline).
- Tool dispatch (parse a reply, guard, run read-only tools): microseconds,
  not milliseconds.

Measured numbers are recorded in `docs/rust-benchmarks.md` as they land.
