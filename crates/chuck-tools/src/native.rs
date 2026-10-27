//! Tool calls written in a model's own format, made into Chuck's blocks.
//!
//! Models are trained on function calling, and some reach for it even when
//! told tools are fenced blocks: DeepSeek writes DSML
//! (`<｜DSML｜invoke name="bash"><｜DSML｜parameter name="command" …>`), or its
//! tool-call tokens; others `<invoke name=…><parameter …>` or
//! `<tool_call>{"name": …, "arguments": …}</tool_call>`. Refusing those
//! leaves an agent repeating calls that never run. Instead each one is
//! rewritten as the block it means (```bash … ```, ```read path``` …) when
//! its name maps to a tool the agent has, and the reply then reads as if it
//! had been written that way, history included, so the model sees the
//! format that works. Anything that maps to no tool is left as it is.
//!
//! The same pass puts a block opened in the middle of a line ("Here it is:
//! ```criteria") on a line of its own.

use serde_json::Value;
use std::sync::OnceLock;

fn re(p: &str) -> regex::Regex {
    regex::Regex::new(p).expect("valid regex")
}

/// `<`, an optional `｜DSML｜` (either bar, any spacing), an optional `antml:`.
const PRE: &str = r"(?:[|｜]\s*DSML\s*[|｜]\s*)?(?:antml:)?";

fn invoke_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(&format!(r#"(?s)<\s*{PRE}invoke\s+name\s*=\s*["']([^"']+)["']\s*>(.*?)<\s*/\s*{PRE}invoke\s*>"#)))
}

fn param_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(&format!(r#"(?s)<\s*{PRE}parameter\s+name\s*=\s*["']([^"']+)["'][^>]*>(.*?)<\s*/\s*{PRE}parameter\s*>"#)))
}

fn wrapper_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(&format!(r"<\s*/?\s*{PRE}function_calls\s*>|<｜tool▁calls▁(?:begin|end)｜>|</?tool_calls>")))
}

fn json_call_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(r"(?s)<tool_call>\s*(\{.*?\})\s*</tool_call>"))
}

fn ds_token_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(r"(?s)<｜tool▁call▁begin｜>\s*(?:function\s*<｜tool▁sep｜>\s*)?([\w.\-]+)\s*(?:<｜tool▁sep｜>)?\s*(?:```(?:json)?)?\s*(\{.*?\})\s*(?:```)?\s*<｜tool▁call▁end｜>"))
}

/// Which of `tags` a function name means.
fn tag_for(name: &str, tags: &[&str]) -> Option<String> {
    let n = name.trim().to_ascii_lowercase();
    let n = n.rsplit(['.', ':']).next().unwrap_or(&n).replace('-', "_");
    // files are written by any agent that can act (they are blocks of their own, not tags)
    let has = |t: &str| tags.contains(&t) || (matches!(t, "write" | "edit" | "patch") && tags.contains(&"bash"));
    if has(&n) {
        return Some(n);
    }
    let alias: &[(&str, &[&str])] = &[
        ("bash", &["shell", "run", "execute", "exec", "terminal", "run_command", "run_shell", "command", "cmd",
                   "bash_tool", "execute_command", "shell_command", "run_terminal_cmd"]),
        ("test", &["run_tests", "run_test", "test_runner"]),
        ("read", &["read_file", "open", "view", "cat", "open_file", "view_file", "list_dir", "ls", "list_directory",
                   "list_files", "read_files"]),
        ("write", &["write_file", "create_file", "save_file", "create", "write_to_file"]),
        ("edit", &["edit_file", "str_replace", "replace", "str_replace_editor", "apply_edit", "replace_in_file",
                   "search_replace"]),
        ("grep", &["grep_search", "search_code", "ripgrep", "rg", "code_search"]),
        ("glob", &["find_files", "file_search", "list_glob"]),
        ("search", &["web_search", "search_web", "websearch", "google", "search_internet", "internet_search"]),
        ("fetch", &["fetch_url", "open_url", "browse_url", "get_url", "read_url", "web_fetch", "url_fetch"]),
        ("submit", &["submit_task", "finish", "complete", "task_complete", "submit_work"]),
        ("done", &["finish", "complete", "task_complete", "job_done", "finish_job"]),
        ("team", &["message", "send_message", "post", "notify", "team_message", "tell_team", "broadcast"]),
    ];
    alias.iter().filter(|(t, names)| has(t) && names.contains(&n.as_str())).map(|(t, _)| t.to_string()).next()
}

/// A value as the text a block holds: strings as they are, lists as lines.
fn text_of(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a.iter().map(text_of).collect::<Vec<_>>().join("\n"),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// A parameter's text; a JSON list (criteria, plan steps) becomes its lines.
fn param_text(raw: &str) -> String {
    let t = raw.trim();
    if t.starts_with('[') {
        if let Ok(v @ Value::Array(_)) = serde_json::from_str::<Value>(t) {
            return text_of(&v);
        }
    }
    raw.to_string()
}

struct Params(Vec<(String, String)>);

impl Params {
    fn get(&self, names: &[&str]) -> Option<String> {
        names.iter().find_map(|n| self.0.iter().find(|(k, _)| k.eq_ignore_ascii_case(n)).map(|(_, v)| v.clone()))
            .filter(|v| !v.trim().is_empty())
    }

    /// The one value there is, when there's only one.
    fn only(&self) -> Option<String> {
        (self.0.len() == 1).then(|| self.0[0].1.clone()).filter(|v| !v.trim().is_empty())
    }
}

/// Fences long enough for `body` (a body holding ``` needs ````).
fn fence_for(body: &str) -> &'static str {
    if body.contains("```") { "````" } else { "```" }
}

/// The block a call means, or None when it can't be made into one.
fn block(tag: &str, p: &Params) -> Option<String> {
    let one = |arg: &str| format!("```{tag} {}```", arg.trim());
    let with_body = |arg: &str, body: &str| {
        let f = fence_for(body);
        let arg = arg.trim();
        format!("{f}{tag}{}{}\n{}\n{f}", if arg.is_empty() { "" } else { " " }, arg, body.trim_end_matches('\n'))
    };
    match tag {
        "bash" | "sh" | "test" => {
            let cmd = p.get(&["command", "cmd", "script", "code", "commands", "bash", "shell", "input", "args", "arguments"])
                .or_else(|| p.only())?;
            Some(if cmd.contains('\n') || tag != "test" { with_body("", &cmd) } else { one(&cmd) })
        }
        "read" => {
            let path = p.get(&["path", "file", "file_path", "filepath", "filename", "target", "dir", "directory", "target_file"])
                .or_else(|| p.only()).unwrap_or_else(|| ".".into());
            let start = p.get(&["start_line", "start", "offset", "from", "line"]);
            let end = p.get(&["end_line", "end", "to"]).or_else(|| {
                // offset + limit
                let (s, l) = (start.as_ref()?.trim().parse::<u64>().ok()?, p.get(&["limit", "lines"])?.trim().parse::<u64>().ok()?);
                Some((s + l).to_string())
            });
            Some(match (start, end) {
                (Some(s), Some(e)) => one(&format!("{}:{}-{}", path.trim(), s.trim(), e.trim())),
                (Some(s), None) => one(&format!("{}:{}", path.trim(), s.trim())),
                _ => one(&path),
            })
        }
        "write" => {
            let path = p.get(&["path", "file", "file_path", "filepath", "filename", "target_file"])?;
            // no content given: not an empty file by accident (the agent hears the call didn't run)
            let content = p.get(&["content", "contents", "text", "body", "data", "file_content", "code", "file_text"])?;
            let f = fence_for(&content);
            Some(format!("{f}write {}\n{}{}{f}", path.trim(), content, if content.ends_with('\n') { "" } else { "\n" }))
        }
        "edit" => {
            let path = p.get(&["path", "file", "file_path", "filepath", "filename", "target_file"])?;
            let old = p.get(&["old_string", "old_str", "old", "search", "find", "old_text", "original"])?;
            let new = p.get(&["new_string", "new_str", "new", "replace", "replacement", "new_text", "updated"]).unwrap_or_default();
            let body = format!("<<<<<<< SEARCH\n{}\n=======\n{}\n>>>>>>> REPLACE", old.trim_end_matches('\n'),
                               new.trim_end_matches('\n'));
            let f = fence_for(&body);
            Some(format!("{f}edit {}\n{body}\n{f}", path.trim()))
        }
        "grep" => {
            let pat = p.get(&["pattern", "regex", "query", "search", "text"]).or_else(|| p.only())?;
            let path = p.get(&["path", "dir", "directory", "file", "include"]).unwrap_or_default();
            Some(one(&format!("{} {}", pat.trim(), path.trim())))
        }
        "glob" => p.get(&["pattern", "glob", "query", "path"]).or_else(|| p.only()).map(|g| one(&g)),
        "search" => p.get(&["query", "q", "search", "text", "term"]).or_else(|| p.only()).map(|q| one(&q)),
        "fetch" => {
            let url = p.get(&["url", "link", "href", "uri", "address"]).or_else(|| p.only())?;
            let part = p.get(&["part", "page"]).unwrap_or_default();
            Some(one(&format!("{} {}", url.trim(), part.trim())))
        }
        "submit" | "done" => {
            let text = p.get(&["report", "summary", "message", "text", "content", "result", "body"]).or_else(|| p.only())?;
            Some(with_body("", &text))
        }
        "team" => {
            let msg = p.get(&["message", "text", "content", "body", "note"]).or_else(|| p.only())?;
            let to = p.get(&["to", "recipient", "target", "who"]).map(|t| format!("@{}", t.trim().trim_start_matches('@')))
                .unwrap_or_default();
            Some(with_body(&to, &msg))
        }
        _ => {
            // the lead's tools and the rest: an argument line and a body
            let arg = p.get(&["arg", "args", "argument", "target", "id", "task", "name", "query", "path", "url", "who"])
                .unwrap_or_default();
            let body = p.get(&["content", "body", "text", "lines", "criteria", "plan", "items", "steps", "message",
                               "details", "brief", "summary", "question"]).unwrap_or_default();
            if arg.trim().is_empty() && body.trim().is_empty() {
                return p.only().map(|v| with_body("", &v));
            }
            Some(if body.trim().is_empty() { one(&arg) } else { with_body(&arg, &body) })
        }
    }
}

fn params_of_json(v: &Value) -> Params {
    let args = v.get("arguments").or_else(|| v.get("parameters")).or_else(|| v.get("args")).or_else(|| v.get("input"));
    let args = match args {
        // arguments sent as a JSON string
        Some(Value::String(s)) => serde_json::from_str::<Value>(s).unwrap_or(Value::String(s.clone())),
        Some(a) => a.clone(),
        None => Value::Null,
    };
    Params(match args {
        Value::Object(m) => m.iter().map(|(k, v)| (k.clone(), text_of(v))).collect(),
        Value::Null => Vec::new(),
        other => vec![("input".into(), text_of(&other))],
    })
}

/// Rewrite every native call `tags` has a tool for as its fenced block;
/// returns the new text and how many calls were rewritten.
pub fn normalize_calls(text: &str, tags: &[&str]) -> (String, usize) {
    let mut n = 0;
    let mut out = text.to_string();
    if out.contains("invoke") {
        out = invoke_rx().replace_all(&out, |c: &regex::Captures| {
            let params = Params(param_rx().captures_iter(&c[2]).map(|p| (p[1].to_string(), param_text(&p[2]))).collect());
            match tag_for(&c[1], tags).and_then(|t| block(&t, &params)) {
                Some(b) => {
                    n += 1;
                    format!("\n{b}\n")
                }
                None => c[0].to_string(),
            }
        }).into_owned();
    }
    if out.contains("<tool_call>") {
        out = json_call_rx().replace_all(&out, |c: &regex::Captures| {
            let Ok(v) = serde_json::from_str::<Value>(&c[1]) else { return c[0].to_string() };
            let name = v.get("name").or_else(|| v.pointer("/function/name")).and_then(Value::as_str).unwrap_or("");
            let params = params_of_json(v.get("function").unwrap_or(&v));
            match tag_for(name, tags).and_then(|t| block(&t, &params)) {
                Some(b) => {
                    n += 1;
                    format!("\n{b}\n")
                }
                None => c[0].to_string(),
            }
        }).into_owned();
    }
    if out.contains("tool▁call▁begin") {
        out = ds_token_rx().replace_all(&out, |c: &regex::Captures| {
            let args = serde_json::from_str::<Value>(&c[2]).unwrap_or(Value::Null);
            let params = params_of_json(&serde_json::json!({ "arguments": args }));
            match tag_for(&c[1], tags).and_then(|t| block(&t, &params)) {
                Some(b) => {
                    n += 1;
                    format!("\n{b}\n")
                }
                None => c[0].to_string(),
            }
        }).into_owned();
    }
    if n > 0 {
        // the wrappers around rewritten calls carry nothing
        out = wrapper_rx().replace_all(&out, "").into_owned();
    }
    (split_glued_openers(&out, tags), n)
}

/// "Here they are: ```criteria" → the text, then the opener on its own line.
/// Only an opener that starts a block (nothing closes it on that line).
fn split_glued_openers(text: &str, tags: &[&str]) -> String {
    if !text.contains("```") {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len() + 16);
    for (i, line) in text.split('\n').enumerate() {
        if i > 0 {
            out.push('\n');
        }
        let t = line.trim_start();
        if t.starts_with("```") {
            out.push_str(line);
            continue;
        }
        let split = line.match_indices("```").find_map(|(at, _)| {
            let rest = &line[at..].trim_start_matches('`');
            let tag_end = rest.find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-')).unwrap_or(rest.len());
            let tag = &rest[..tag_end];
            let known = tags.contains(&tag) || matches!(tag, "write" | "edit" | "patch");
            // an opener: a known tag, and no closing fence after it on this line
            (known && !tag.is_empty() && !rest[tag_end..].contains("```")).then_some(at)
        });
        match split {
            Some(at) => {
                out.push_str(line[..at].trim_end());
                out.push('\n');
                out.push_str(&line[at..]);
            }
            None => out.push_str(line),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{file_blocks, tool_blocks};

    const LEAD: &[&str] = &["read", "grep", "glob", "bash", "test", "search", "fetch", "criteria", "plan", "assign",
        "review", "done", "team", "cancel"];
    const WORKER: &[&str] = &["read", "grep", "glob", "bash", "test", "search", "fetch", "submit", "team"];

    #[test]
    fn deepseek_dsml_runs_as_blocks() {
        // as a DeepSeek worker wrote it (full-width bars, and the ASCII spelling)
        let t = "Checking the layout.\n<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"bash\">\n\
                 <｜DSML｜parameter name=\"command\" string=\"true\">ls -la js/ tests/ 2>&1; echo \"---\"; wc -l js/engine.js</｜DSML｜parameter>\n\
                 </｜DSML｜invoke>\n<｜DSML｜invoke name=\"read_file\">\n<｜DSML｜parameter name=\"path\" string=\"true\">js/game.js</｜DSML｜parameter>\n\
                 <｜DSML｜parameter name=\"start_line\" string=\"false\">10</｜DSML｜parameter>\n<｜DSML｜parameter name=\"end_line\" string=\"false\">40</｜DSML｜parameter>\n\
                 </｜DSML｜invoke>\n</｜DSML｜function_calls>";
        let (out, n) = normalize_calls(t, WORKER);
        assert_eq!(n, 2, "{out}");
        let b = tool_blocks(&out, WORKER);
        assert_eq!((b[0].tag.as_str(), b[0].body.as_str()), ("bash", "ls -la js/ tests/ 2>&1; echo \"---\"; wc -l js/engine.js"));
        assert_eq!((b[1].tag.as_str(), b[1].arg.as_str()), ("read", "js/game.js:10-40"));
        assert!(!out.contains("DSML"), "{out}");
        let ascii = "< |DSML| invoke name=\"bash\"> < |DSML| parameter name=\"command\" string=\"true\">node --test</ |DSML| parameter> </ |DSML| invoke>";
        let (out, n) = normalize_calls(ascii, WORKER);
        assert_eq!((n, tool_blocks(&out, WORKER)[0].body.as_str()), (1, "node --test"), "{out}");
    }

    #[test]
    fn other_native_formats_run_too() {
        let anth = "<function_calls>\n<invoke name=\"write_file\">\n<parameter name=\"path\">src/a.js</parameter>\n\
                    <parameter name=\"content\">const x = 1;\n```js\nexample\n```\n</parameter>\n</invoke>\n</function_calls>";
        let (out, n) = normalize_calls(anth, WORKER);
        assert_eq!(n, 1);
        let f = file_blocks(&out);
        assert_eq!((f[0].path.as_str(), f[0].body.as_str()), ("src/a.js", "const x = 1;\n```js\nexample\n```\n"), "{out}");
        let json = "<tool_call>{\"name\": \"web_search\", \"arguments\": \"{\\\"query\\\": \\\"rathkeale news\\\"}\"}</tool_call>";
        let (out, _) = normalize_calls(json, WORKER);
        assert_eq!(tool_blocks(&out, WORKER)[0].arg, "rathkeale news", "{out}");
        let ds = "<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>str_replace\n```json\n\
                  {\"path\": \"a.py\", \"old_string\": \"x = 1\", \"new_string\": \"x = 2\"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>";
        let (out, n) = normalize_calls(ds, &["edit", "read"]);
        assert_eq!(n, 1, "{out}");
        let f = file_blocks(&out);
        assert_eq!(crate::edit_pairs(&f[0].body), vec![("x = 1".into(), "x = 2".into())]);
        // the lead's own: criteria as a list, a plan, a team message
        let crit = "<invoke name=\"criteria\"><parameter name=\"criteria\">[\"- tests pass :: verify: npm test\", \"- it builds :: verify: npm run build\"]</parameter></invoke>";
        let (out, _) = normalize_calls(crit, LEAD);
        let b = tool_blocks(&out, LEAD);
        assert_eq!(b[0].tag, "criteria");
        assert!(b[0].body.contains("verify: npm test") && b[0].body.contains("npm run build"), "{out}");
        let team = "<invoke name=\"send_message\"><parameter name=\"to\">ds-2</parameter><parameter name=\"message\">engine.js exports create()</parameter></invoke>";
        let (out, _) = normalize_calls(team, WORKER);
        let b = tool_blocks(&out, WORKER);
        assert_eq!((b[0].tag.as_str(), b[0].arg.as_str(), b[0].body.as_str()), ("team", "@ds-2", "engine.js exports create()"));
    }

    #[test]
    fn what_maps_to_no_tool_is_left_alone() {
        let t = "<invoke name=\"launch_missiles\"><parameter name=\"target\">x</parameter></invoke>";
        assert_eq!(normalize_calls(t, WORKER), (t.to_string(), 0));
        // a worker has no `done`; finish means submit for it
        let (out, _) = normalize_calls("<invoke name=\"finish\"><parameter name=\"summary\">all green</parameter></invoke>", WORKER);
        assert_eq!(tool_blocks(&out, WORKER)[0].tag, "submit");
        // plain prose with code is untouched
        let prose = "Run it with:\n```bash\nnpm test\n```\nthen check.";
        assert_eq!(normalize_calls(prose, WORKER).0, prose);
    }

    #[test]
    fn a_block_opened_mid_line_gets_its_own_line() {
        let t = "The criteria didn't register. Re-issuing them: ```criteria\n- a :: verify: npm test\n```";
        let (out, _) = normalize_calls(t, LEAD);
        let b = tool_blocks(&out, LEAD);
        assert_eq!((b.len(), b[0].tag.as_str(), b[0].body.as_str()), (1, "criteria", "- a :: verify: npm test"), "{out}");
        // an inline one-liner and an unknown tag stay as they are
        let inline = "Use ```search rust``` to look it up, or ```python``` code.";
        assert_eq!(normalize_calls(inline, LEAD).0, inline);
    }
}
