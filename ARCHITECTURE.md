# Chuck in Rust — architecture

Chuck is a Linux desktop coding agent in Rust. Two things are central and
enforced in code:

1. **One fast model does the whole job.** DeepSeek-V4.1-Flash writes the
   criteria, the code and the checks, usually all in its first reply. Nothing
   waits on hand-offs. (Chuck 18 started with a Lead and up to eight workers in
   copies of the project; one model turned out faster, so the fleet is gone.)
2. **Genuine Done.** A job can't be reported finished without written
   acceptance criteria, recorded evidence for each one, and a verification run
   after the last change.

Remote models only think. The local Rust runtime runs tools, isolates
commands, records evidence and enforces the gates.

---

## 1. The models (SiliconFlow only)

All model traffic goes to SiliconFlow (`https://api.siliconflow.com/v1`, or
the user's configured SiliconFlow base URL) with the key from Settings. The
client refuses a base URL that isn't SiliconFlow unless the user explicitly
overrides it.

| Role | Model (intended id) |
|---|---|
| **Chuck**: talks to the user, writes acceptance criteria, builds, verifies, owns *done* (also Chat and the daemons) | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| **Eyes**: describe screenshots — pages Chuck checks with `browse`, pictures the user pastes | `zai-org/GLM-5.3-Flash` |

Model ids are resolved against the account's live model list at startup
(`/models`, `chuck_core::Models::resolve`). The intended id is used if present;
otherwise the closest live id of the same family, never a pricier `-Pro`/`-Max`
sibling while another exists. `chuck models` shows the resolution.

### The job loop — `chuck-runtime::lead`

```
 user ──► first turn (thinking on): criteria, every file, the checks, done
            │
            ▼
       Exec runs the reply's calls in order — criteria first, wherever they sit
            │   (files written before any criteria are kept and written once they exist)
            ▼
       feedback: what ran, its output, the gate's line
            │
            ├─ a check failed / done refused → next turn thinks, fixes with `edit`
            ├─ fine → next turn goes on without thinking
            └─ ```done``` through the gate → Done
```

- Every turn gets the full reply budget (`max_tokens`). A reply cut by the
  length limit is continued, a cut file call kept as the open block it was
  becoming.
- `ask` puts a decision to the user with 2–4 explained options; with no answer
  in three minutes Chuck goes with its own pick (`parse_ask`, `picked`).
- Tools are also offered as API functions (`chuck-runtime::schemas`). A call
  comes back parsed and is turned into the fenced block it means
  (`chuck_tools::native::call_block`). Text calls in any format are normalized
  the same way, so the gate, the jail and the evidence see one thing; a call
  that can't be read is reported, never dropped.
- `edit` matches exactly, then ignoring trailing spaces, then at a uniformly
  wrong indentation (re-indenting the replacement), always only when unique.
- Pasted screenshots (`Job::images`) go to the eyes first; Chuck gets what they
  saw and the file's path (`browse::describe_pasted`).
- Budgets: the job is a kernel process with a quota; every model call is
  charged to it, and day/month limits stop every call past them.

---

## 2. Genuine Done — the gate

`DoneGate::check` refuses completion — returning the exact unmet items — unless
**all** of these hold, each backed by recorded `Evidence`:

1. **Acceptance criteria** were written *before* the first change. A
   write/edit/patch, or a command that would change files, before criteria
   exist is refused (commands run with the folder read-only).
2. **Every criterion has evidence**: a command with its exit code and output,
   a diff, a test run, or a look at a screenshot — linked to the criterion.
3. **Verification after the last change**: each criterion's check ran *after*
   the last mutation and passed. Any later change resets this.
4. **Craftsmanship checks** on the real diff (the folder against a snapshot
   taken when the job began, so shell edits count): no `TODO`/`FIXME`/
   `unimplemented!`/stubs in changed code.

A check that can't fail (`true`, `echo ok`) proves nothing. A reply that says
it's finished in prose with every criterion verified is its `done`; one that
isn't verified is told only `done` ends defined work, and a second time pauses
the job INCOMPLETE. **Budget exhaustion is never "done"**: the job pauses with
an honest list of what's missing, and *Keep going* carries it on.

Shallow-loop detection: turns with no change and no passing check, or the same
failing action repeated, make Chuck replan; being stuck after that pauses it.

The window's **Checks** section shows each criterion and its evidence.

---

## 3. Chuck OS semantics preserved

| Python module | Rust home | Notes |
|---|---|---|
| `kernel.py` | `chuck-core::kernel` | processes, quotas over ancestors, signals, mailboxes, blackboard, syscall log, slot scheduler with priority + ageing |
| `worker.py`, `agents.py` | `chuck-runtime::{agent, lead}` | the job loop: one model, its calls run through the guard and the gate |
| `daemons.py` | `chuck-daemon` | triggers checked by plain code for free; read-only shell policy; the inbox (same files) |
| `budget.py`, `pricing.py`, `governor.py` | `chuck-core::budget`, `chuck-model::pricing` | day/month ledger, per-model prices |
| `hibernate.py`, `checkpoint.py` | `chuck-store` | per-step checkpoints, shadow git, replay / fork, rewind |
| `ledger.py`, `crashlog.py` | `chuck-store::ledger`, `chuck-store::crashlog` | hash-chained audit log; redacted crash log |
| `safety.py` | `chuck-sandbox::safety` | critical / danger classifier (same rules) |
| bubblewrap code in `chucknorris.py` | `chuck-sandbox::{jail, outside}` | modes Guarded (the folder freely, a path outside it only with the user's yes, then mounted alone) / Full / Sandboxed / Offline / Read-only; hidden secrets, read-only `.git` hooks/config, git disarm |
| `cow.py` | `chuck-tools::snapshot` | the folder as the job found it, for the real diff; links never followed |
| `builder.py`, file blocks | `chuck-tools` | read/write/edit/patch/grep/glob/bash, fenced-block parser, all-or-nothing `patch` |
| `net.py`, `web.py` | `chuck-web` | search (SearXNG, DuckDuckGo), page reads that never reach private addresses, images and videos |
| `browser.py` | `chuck-web::browser` | a DevTools-protocol client for Brave/Chromium/Chrome; every request judged (public only, offline, folder files) |
| `lsp.py`, `codesearch.py` | `chuck-code` | language-server def/refs/rename, syntax fallback, repo map, BM25 `find` |
| `mcp.py` | `chuck-mcp` | stdio MCP client; tool annotations decide what may run when |
| `palette.py` | `chuck-ui::palette` | Ctrl+P, the same ranking |
| `config.py`, `chats.py`, `memory.py` | `chuck-store` | settings 0600, chats, memory |
| GTK window | `chuck-ui` (gtk4-rs + libadwaita) | chat, work lane, side panel, Checks, settings, pasted screenshots |
| `voice.py`, `listen.py` | `chuck-voice` | Rust drives the existing Kokoro / Moonshine servers (they need Python's ONNX runtime); players probed, never silent |

## 4. Crates

```
crates/
  chuck-core      types, kernel, models, DoneGate, evidence (no I/O)
  chuck-runtime   the job loop, Chat, daemons' agents, guarded execution of every block
  chuck-model     SiliconFlow client: SSE streaming, reasoning budget, stall timeout, retries,
                  pricing
  chuck-sandbox   bubblewrap argv, secrets, command safety classifier, git guard
  chuck-tools     tools + evidence capture, fenced-block parser, the folder snapshot
  chuck-store     settings, chats, audit ledger, checkpoints, memory, spend, crash log
  chuck-web       search, page reads, the headless browser, pictures and videos
  chuck-code      language servers, syntax answers, repo map, find
  chuck-mcp       MCP servers over stdio
  chuck-daemon    triggers, the read-only shell policy, the inbox
  chuck-voice     TTS/STT sidecar control, audio player probe
  chuck-ui        GTK4 + libadwaita: conversation, work lane, side panel, Checks, settings
  chuck           the binary: `chuck` (window), `chuck run "task"` (headless), `chuck continue`,
                  `chuck ask`, `chuck rewind`, `chuck ledger`, `chuck doctor`…
```

Concurrency model: the job on its own OS thread, channels to the window, no
async runtime — a blocking HTTP stream is the simplest correct design and keeps
idle cost at zero (daemons are a single timer thread that checks triggers in
plain code).

## 5. Performance

Measured, not estimated — see `docs/rust-benchmarks.md` for how and on what:

- First painted window frame: about 210 ms for the Rust window vs about 430 ms
  for the Python one, on the same machine (most of both is GTK's own start).
- The harness's own overhead per turn, with an instant scripted model
  (`cargo run -p chuck-runtime --example overhead`): about 30 ms — the model's
  latency is everything else.


