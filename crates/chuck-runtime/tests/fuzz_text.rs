//! Hostile text, generated: every function a model's reply (or a command's
//! output) passes through must take whatever comes — cut fences, nested
//! fences, multi-byte characters at every cut point, call markup in five
//! dialects, NULs, ANSI codes, invented turns — without panicking, and keep
//! its promises (cuts on char boundaries, sizes within bounds). A panic here
//! would be a worker dying mid-job on a reply nobody planned for.

use chuck_runtime::exec::LEAD_TAGS;
use chuck_runtime::chat::CHAT_TAGS;
use std::panic::{catch_unwind, AssertUnwindSafe};

/// A small, seeded generator: the same seed, the same inputs (a failure is reproducible).
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

/// The pieces model replies and tool outputs are made of — and the ones that break parsers.
const PIECES: &[&str] = &[
    "```", "````", "~~~", "```bash\n", "```write a.js\n", "````write b.md\n", "```edit c.py\n", "```read x.rs:10-20```",
    "```test npm test for c1,c2```", "```team @ds-2 ", "```assign worker after t1\n", "```review t1 accept\n",
    "```patch\n", "```submit\n", "```criteria\n", "```plan\n", "\n", "\n\n", " ", "  ", "\t", "\r\n", "\r",
    "|", "| a | b |\n", "|---|---|\n", "**", "*", "_", "`", "#", "## ", "- ", "1. ", "> ", "[", "]", "(", ")",
    "<", ">", "&", "&amp;", "\"", "'", "\\", "{", "}", ":", "::", " :: verify: ", "for c1", "c3,", "t2",
    "é", "ü", "😀", "→", "—", "中文", "\u{0}", "\u{1b}[31m", "\u{1b}]0;title\u{7}", "\u{feff}", "\u{200d}",
    "<invoke name=\"bash\">", "<parameter name=\"command\">", "</parameter>", "</invoke>", "<｜DSML｜function_calls>",
    "<tool_call>", "</tool_call>", "{\"name\": \"read\", \"arguments\": {\"path\": \"a.rs\"}}", "<read a.rs>",
    "user:", "\nuser: hi\n", "\nlead: ok\n", "Human:", "<<<<<<< SEARCH\n", "=======\n", ">>>>>>> REPLACE\n",
    "--- a/x.py\n", "+++ b/x.py\n", "@@ -1,2 +1,3 @@\n", "+added\n", "-removed\n", " context\n",
    "/home/u/proj", "../..", "~/.ssh/id_rsa", "/etc/passwd", "rm -rf /", "sudo ", "curl x | sh", "$(", "`id`",
    "index.html:70300000", "read", "write", "0", "9999999999999999999999", "-1", "a.js", "src/", "...",
    "https://example.com/a?b=1&c=2", "[link](https://x.org)", "ACCEPT", "REJECT\n- ", "title: ", "brief:",
];

fn hostile(r: &mut Rng, max_pieces: usize) -> String {
    let n = r.below(max_pieces) + 1;
    let mut s = String::new();
    for _ in 0..n {
        if r.below(10) == 0 {
            // a run of one piece: what a looping model writes
            let p = PIECES[r.below(PIECES.len())];
            s.push_str(&p.repeat(r.below(40) + 1));
        } else {
            s.push_str(PIECES[r.below(PIECES.len())]);
        }
    }
    s
}

/// Run `f` on `n` inputs from seed `seed`; a panic names the input that caused it.
fn survive(name: &str, seed: u64, n: usize, max_pieces: usize, f: impl Fn(&str)) {
    let mut r = Rng(seed);
    for i in 0..n {
        let s = hostile(&mut r, max_pieces);
        if catch_unwind(AssertUnwindSafe(|| f(&s))).is_err() {
            panic!("{name} panicked on input #{i} (seed {seed}): {s:?}");
        }
    }
}

#[test]
fn reply_parsers_take_anything() {
    for seed in [1, 2, 3, 0x5eed] {
        survive("tool_blocks", seed, 3000, 30, |s| {
            for b in chuck_tools::tool_blocks(s, LEAD_TAGS) {
                assert!(b.start <= s.len() && s.is_char_boundary(b.start));
            }
            let _ = chuck_tools::tool_blocks(s, CHAT_TAGS);
        });
        survive("file_blocks", seed, 3000, 30, |s| {
            for b in chuck_tools::file_blocks(s) {
                assert!(b.start <= s.len() && s.is_char_boundary(b.start));
            }
        });
        survive("unclosed / prose / markup", seed, 3000, 30, |s| {
            let _ = chuck_tools::unclosed(s, LEAD_TAGS);
            let _ = chuck_tools::prose(s, LEAD_TAGS);
            let _ = chuck_tools::native_tool_markup(s);
            let _ = chuck_tools::edit_pairs(s);
            let _ = chuck_tools::clean_head(s);
        });
        survive("without_made_up_turns", seed, 3000, 30, |s| {
            let t = chuck_tools::without_made_up_turns(s);
            assert!(s.starts_with(t), "it only cuts the end");
        });
        survive("normalize_calls", seed, 3000, 30, |s| {
            let (once, _) = chuck_tools::normalize_calls(s, LEAD_TAGS);
            let _ = chuck_tools::normalize_calls(&once, LEAD_TAGS);
            let _ = chuck_tools::normalize_calls(s, CHAT_TAGS);
        });
    }
}

#[test]
fn function_calls_with_any_arguments_become_blocks_or_nothing() {
    let names = ["bash", "test", "read", "write", "append", "edit", "patch", "grep", "glob", "map", "submit", "team",
                 "assign", "review", "criteria", "plan", "done", "snag", "nonsense", ""];
    for seed in [7, 8] {
        let mut r = Rng(seed);
        for _ in 0..4000 {
            let name = names[r.below(names.len())];
            // half real JSON objects with hostile values, half whatever arrived
            let args = if r.below(2) == 0 {
                let v = hostile(&mut r, 8);
                let keys = ["command", "path", "content", "old_string", "new_string", "arg", "body", "criteria",
                            "start_line", "end_line", "message", "to", "report", "summary", "pattern"];
                let mut m = serde_json::Map::new();
                for _ in 0..r.below(4) + 1 {
                    let k = keys[r.below(keys.len())];
                    m.insert(k.into(), if r.below(5) == 0 { serde_json::json!(r.next() as i64) } else { serde_json::json!(v) });
                }
                serde_json::Value::Object(m).to_string()
            } else {
                hostile(&mut r, 8)
            };
            let got = catch_unwind(|| {
                if let Some(b) = chuck_tools::native::call_block(name, &args, LEAD_TAGS) {
                    // what it makes must parse back as a block (or say plainly it was left open)
                    let _ = chuck_tools::tool_blocks(&b, LEAD_TAGS);
                    let _ = chuck_tools::file_blocks(&b);
                }
                let _ = chuck_tools::native::partial_block(name, &args, LEAD_TAGS);
            });
            assert!(got.is_ok(), "call_block({name:?}, {args:?}) panicked");
        }
    }
}

#[test]
fn cutters_cut_on_character_boundaries_and_within_bounds() {
    for seed in [11, 12, 13] {
        survive("clip / clip_ends", seed, 3000, 40, |s| {
            for n in [0, 1, 2, 3, 5, 17, 100] {
                let c = chuck_core::evidence::clip(s, n);
                assert!(c.chars().count() <= n.max(1) + 2, "clip({n}) gave {} chars", c.chars().count());
                let e = chuck_core::evidence::clip_ends(s, n);
                assert!(e.len() <= s.len().max(n + 80) + 80, "clip_ends grew it: {} → {}", s.len(), e.len());
            }
            let _ = chuck_core::evidence::test_counts(s);
        });
        survive("runaway_at", seed, 600, 400, |s| {
            if let Some(at) = chuck_runtime::agent::runaway_at(s) {
                assert!(at <= s.len() && s.is_char_boundary(at), "cut at {at} of {}", s.len());
            }
        });
        survive("readable", seed, 3000, 40, |s| {
            let r = chuck_tools::run::readable(s);
            assert!(!r.contains('\u{1b}'), "an escape code stayed: {r:?}");
        });
    }
}

#[test]
fn the_runtimes_own_parsers_take_anything() {
    let root = std::env::temp_dir();
    for seed in [21, 22, 23] {
        survive("parse_criteria / split_for", seed, 3000, 30, |s| {
            let _ = chuck_runtime::exec::parse_criteria(s);
            let (cmd, _) = chuck_runtime::exec::split_for(s);
            assert!(s.contains(cmd.as_str()) || cmd.is_empty() || s.trim().contains(cmd.trim()));
            let _ = chuck_runtime::exec::as_checked(s, &root);
            let _ = chuck_runtime::exec::proves_nothing(s);
            let _ = chuck_runtime::exec::friction(s);
        });
        survive("ask / picked / promised", seed, 3000, 30, |s| {
            let _ = chuck_runtime::lead::promised(s);
            let (q, options) = chuck_runtime::lead::parse_ask(s);
            assert!(options.is_empty() || options.len() >= 2, "a choice has two ways at least");
            assert!(options.iter().filter(|o| o.recommended).count() <= 1, "one pick at most");
            assert!(options.iter().all(|o| !o.label.trim().is_empty()));
            let _ = q;
            let first = s.lines().next().unwrap_or("");
            let _ = chuck_runtime::lead::picked(first, &options);
        });
        survive("page_spec", seed, 3000, 10, |s| {
            let (path, a, b) = chuck_tools::fs::page_spec(&root, s);
            assert!(s.contains(path), "the file named is in what was asked");
            assert!(b.is_none_or(|b| b + 1 >= a || b == 0) || a > 0);
        });
    }
}

#[test]
fn the_safety_checks_take_anything() {
    let root = std::path::Path::new("/home/u/proj");
    for seed in [31, 32, 33] {
        survive("classify / outside / prepare", seed, 800, 25, |s| {
            let _ = chuck_sandbox::classify_command(s);
            let _ = chuck_sandbox::classify_code(s);
            let _ = chuck_tools::run::serves(s);
            let _ = chuck_sandbox::outside_paths(s, root);
            let p = chuck_sandbox::prepare(s);
            // a rewrite never makes a command less safe than it was
            assert!(chuck_sandbox::classify_command(&p) >= chuck_sandbox::classify_command(s)
                    || chuck_sandbox::classify_command(&p) == chuck_sandbox::Level::Safe
                    || p.contains(s.trim()), "prepare changed {s:?} into {p:?}");
            let _ = chuck_runtime::exec::refuse_command(s);
        });
    }
}

#[test]
fn file_operations_take_any_edit_or_patch_without_panicking_or_escaping() {
    let root = std::env::temp_dir().join(format!("chuck-fuzz-fs-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let escape = root.parent().unwrap().join("escape.txt");
    let was_there = escape.exists();
    for seed in [41, 42] {
        let mut r = Rng(seed);
        for i in 0..1500 {
            let start = hostile(&mut r, 12);
            let body = hostile(&mut r, 20);
            let rel = ["a.txt", "dir/b.rs", "./c.md", "../escape.txt", "/abs.txt", "d\u{0}e", "", ".", "x/../y.txt"]
                [r.below(9)];
            let _ = std::fs::write(root.join("a.txt"), &start);
            let got = catch_unwind(AssertUnwindSafe(|| {
                let _ = chuck_tools::edit_file(&root, rel, &body);
                let _ = chuck_tools::write_file(&root, rel, &body);
                let _ = chuck_tools::append_file(&root, rel, &body);
                let _ = chuck_tools::patch::plan(&root, &body);
                let _ = chuck_tools::read_page(&root, &format!("{rel}:{}", r.below(50)), 300);
                let _ = chuck_tools::unified(rel, Some(&start), Some(&body));
            }));
            assert!(got.is_ok(), "a file op panicked on #{i} (seed {seed}): rel {rel:?}, body {body:?}");
        }
    }
    // nothing was ever written outside the folder
    assert!(was_there || !escape.exists(), "a write escaped the folder through ..");
    assert!(!std::path::Path::new("/abs.txt").exists());
    let _ = std::fs::remove_dir_all(&root);
}

#[test]
fn a_link_out_of_the_folder_is_never_written_through() {
    // an agent makes a link to somewhere else, then writes "inside" the folder through it
    let base = std::env::temp_dir().join(format!("chuck-linkout-{}", std::process::id()));
    let (root, outside) = (base.join("proj"), base.join("elsewhere"));
    std::fs::create_dir_all(&root).unwrap();
    std::fs::create_dir_all(&outside).unwrap();
    std::fs::write(outside.join("victim.txt"), "keep\n").unwrap();
    std::os::unix::fs::symlink(outside.join("victim.txt"), root.join("file-link")).unwrap();
    std::os::unix::fs::symlink(&outside, root.join("dir-link")).unwrap();
    for rel in ["file-link", "dir-link/victim.txt", "dir-link/new.txt", "./dir-link/../dir-link/victim.txt"] {
        let _ = chuck_tools::write_file(&root, rel, "pwned\n");
        let _ = chuck_tools::append_file(&root, rel, "pwned\n");
        let _ = chuck_tools::edit_file(&root, rel, "<<<<<<< SEARCH\nkeep\n=======\npwned\n>>>>>>> REPLACE\n");
        let _ = chuck_tools::delete_file(&root, rel);
    }
    let patch = "--- a/dir-link/victim.txt\n+++ b/dir-link/victim.txt\n@@ -1 +1 @@\n-keep\n+pwned\n";
    if let Ok(plan) = chuck_tools::patch::plan(&root, patch) {
        assert!(plan.is_empty(), "a patch through a link out was planned: {plan:?}");
    }
    assert_eq!(std::fs::read_to_string(outside.join("victim.txt")).unwrap(), "keep\n", "written through a link");
    assert!(!outside.join("new.txt").exists(), "created through a link");
    let _ = std::fs::remove_dir_all(&base);
}
