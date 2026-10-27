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
    let has = |t: &str| tags.contains(&t) || (matches!(t, "write" | "append" | "edit" | "patch") && tags.contains(&"bash"));
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
            // `for c1,c3`: the criteria a check proves (as a test block names them)
            let ids: Vec<String> = if tag == "test" {
                p.get(&["criteria", "for", "proves", "criterion"]).map(|c| c.split(|ch: char| !ch.is_ascii_alphanumeric())
                    .filter(|x| !x.is_empty()).map(|x| if x.starts_with(['c', 'C']) { x.to_lowercase() } else { format!("c{x}") })
                    .collect()).unwrap_or_default()
            } else { Vec::new() };
            let link = if ids.is_empty() { String::new() } else { format!("for {}", ids.join(",")) };
            Some(if cmd.contains('\n') || tag != "test" { with_body(&link, &cmd) }
                 else if link.is_empty() { one(&cmd) } else { one(&format!("{cmd} {link}")) })
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
        "write" | "append" => {
            let path = p.get(&["path", "file", "file_path", "filepath", "filename", "target_file"])?;
            // no content given: not an empty file by accident (the agent hears the call didn't run)
            let content = p.get(&["content", "contents", "text", "body", "data", "file_content", "code", "file_text"])?;
            let f = fence_for(&content);
            Some(format!("{f}{tag} {}\n{}{}{f}", path.trim(), content, if content.ends_with('\n') { "" } else { "\n" }))
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
                if tag == "map" {
                    return Some("```map```".into()); // the one tool that takes nothing
                }
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

/// A function call the API returned (its name, its arguments as JSON
/// text) as the block it means, when `tags` has the tool and the arguments
/// are enough to run it.
pub fn call_block(name: &str, args: &str, tags: &[&str]) -> Option<String> {
    let v = serde_json::from_str::<Value>(args.trim()).unwrap_or_else(|_| {
        // not JSON (a truncated call, or a bare string): the one value it gives
        if args.trim().is_empty() { Value::Null } else { Value::String(args.to_string()) }
    });
    let params = params_of_json(&serde_json::json!({ "arguments": v }));
    tag_for(name, tags).and_then(|t| block(&t, &params))
}

/// A JSON string value read up to where the text stops: (the value so far,
/// whether it was closed).
fn json_str_until(s: &str) -> (String, bool) {
    let mut out = String::new();
    let mut it = s.chars();
    while let Some(c) = it.next() {
        match c {
            '"' => return (out, true),
            '\\' => match it.next() {
                Some('n') => out.push('\n'),
                Some('t') => out.push('\t'),
                Some('r') => out.push('\r'),
                Some('b') => out.push('\u{8}'),
                Some('f') => out.push('\u{c}'),
                Some('u') => {
                    let hex: String = it.by_ref().take(4).collect();
                    if hex.len() < 4 {
                        break; // cut inside the escape
                    }
                    let Ok(n) = u32::from_str_radix(&hex, 16) else { break };
                    if (0xD800..0xDC00).contains(&n) {
                        // a surrogate pair: the low half follows as \uXXXX
                        let rest: String = it.by_ref().take(6).collect();
                        let low = rest.strip_prefix("\\u").and_then(|h| u32::from_str_radix(h, 16).ok());
                        match low {
                            Some(l) => out.extend(char::from_u32(0x10000 + ((n - 0xD800) << 10) + (l - 0xDC00))),
                            None => break,
                        }
                    } else {
                        out.extend(char::from_u32(n));
                    }
                }
                Some(o) => out.push(o),
                None => break,
            },
            c => out.push(c),
        }
    }
    (out, false)
}

/// The string value of `key` in JSON that may be cut short.
fn partial_field(args: &str, keys: &[&str]) -> Option<(String, bool)> {
    keys.iter().find_map(|k| {
        let rx = re(&format!(r#""{}"\s*:\s*""#, regex::escape(k)));
        rx.find(args).map(|m| json_str_until(&args[m.end()..]))
    })
}

/// A call the length limit cut off (its arguments end mid-way), as the
/// block it was becoming, left open so the model can continue it as text:
/// a big file's write keeps every character that arrived. None: nothing
/// usable arrived (or the tool takes no long body).
pub fn partial_block(name: &str, args: &str, tags: &[&str]) -> Option<String> {
    let tag = tag_for(name, tags)?;
    let (key, keys): (&str, &[&str]) = match tag.as_str() {
        "write" | "append" => ("path", &["content", "contents", "text", "body", "file_content", "code"]),
        "bash" | "sh" => ("", &["command", "cmd", "script"]),
        _ => return None,
    };
    let path = if key.is_empty() { String::new() } else {
        let (p, closed) = partial_field(args, &["path", "file_path", "file", "filename"])?;
        if !closed || p.trim().is_empty() {
            return None; // not even the file's name arrived
        }
        p
    };
    let (body, _) = partial_field(args, keys).unwrap_or_default();
    let f = fence_for(&body);
    Some(format!("{f}{tag}{}{}\n{body}", if path.is_empty() { "" } else { " " }, path.trim()))
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
    let (out, m) = tag_calls(&out, tags);
    (split_glued_openers(&out, tags), n + m)
}

fn tag_open_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(r"<([A-Za-z_][\w\-]*)((?:\s[^<>]*?)?)\s*(/?)>"))
}

fn attr_rx() -> &'static regex::Regex {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    R.get_or_init(|| re(r#"([A-Za-z_][\w\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')"#))
}

/// Byte ranges of the text inside code: fenced blocks (fence lines
/// included) and `inline code`.
fn code_ranges(text: &str) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut open: Option<(usize, usize)> = None; // (start, fence length)
    let mut at = 0;
    for line in text.split_inclusive('\n') {
        let t = line.trim_start();
        let ticks = t.chars().take_while(|c| *c == '`').count();
        match open {
            Some((start, len)) => {
                if ticks >= len && t.trim_end().chars().all(|c| c == '`') {
                    out.push((start, at + line.len()));
                    open = None;
                }
            }
            None if ticks >= 3 => {
                // a one-line block (```read x```) closes on its own line
                if t[ticks..].contains(&"`".repeat(ticks)) {
                    out.push((at, at + line.len()));
                } else {
                    open = Some((at, ticks));
                }
            }
            None => {
                let mut i = 0;
                let b = line.as_bytes();
                while i < b.len() {
                    if b[i] == b'`' {
                        if let Some(j) = line[i + 1..].find('`') {
                            out.push((at + i, at + i + 1 + j + 1));
                            i += j + 2;
                            continue;
                        }
                    }
                    i += 1;
                }
            }
        }
        at += line.len();
    }
    if let Some((start, _)) = open {
        out.push((start, text.len()));
    }
    out
}

/// A body as a block holds it: blank edges off, one fence around it removed.
fn unwrap_body(body: &str) -> String {
    let b = body.trim_matches('\n').trim_end();
    let lines: Vec<&str> = b.lines().collect();
    if lines.len() >= 2 && lines[0].trim_start().starts_with("```") && lines[lines.len() - 1].trim() == "```" {
        return lines[1..lines.len() - 1].join("\n");
    }
    b.to_string()
}

/// `<read INTERFACES.md>`, `<read path="a.js"/>`, `<bash>npm test</bash>`,
/// `<write path="a.js">…</write>`: a tag named for a tool the agent has,
/// written outside code, made into its block. Text inside code (a file being
/// written, `<search>` in an example) is never touched.
fn tag_calls(text: &str, tags: &[&str]) -> (String, usize) {
    if !text.contains('<') {
        return (text.to_string(), 0);
    }
    let code = code_ranges(text);
    let in_code = |i: usize| code.iter().any(|(a, b)| i >= *a && i < *b);
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    let mut n = 0;
    let mut from = 0;
    while let Some(c) = tag_open_rx().captures_at(text, from) {
        let whole = c.get(0).unwrap();
        from = whole.end();
        if whole.start() < last || in_code(whole.start()) {
            continue;
        }
        let name = &c[1];
        let Some(tag) = tag_for(name, tags) else { continue };
        let attrs = c[2].trim();
        let self_closed = &c[3] == "/";
        // its body, up to the closing tag (when there is one)
        let close = format!("</{name}>");
        let (body, end) = if self_closed {
            (String::new(), whole.end())
        } else {
            match text[whole.end()..].find(&close) {
                Some(i) => (unwrap_body(&text[whole.end()..whole.end() + i]), whole.end() + i + close.len()),
                None => (String::new(), whole.end()),
            }
        };
        let mut params: Vec<(String, String)> = attr_rx().captures_iter(attrs)
            .map(|a| (a[1].to_string(), a.get(2).or(a.get(3)).map_or("", |m| m.as_str()).to_string())).collect();
        let bare = if params.is_empty() { attrs.to_string() } else { String::new() };
        if bare.is_empty() && params.is_empty() && body.trim().is_empty() && tag != "map" {
            continue; // `<read>` alone says nothing to run
        }
        let b = if params.is_empty() {
            // `<tag arg>body</tag>`: the argument line and the body, as a block has them
            let (arg, body) = if bare.is_empty() && !body.contains('\n')
                && !matches!(tag.as_str(), "bash" | "sh" | "write" | "append" | "edit" | "patch" | "submit" | "done" | "team") {
                (body.trim().to_string(), String::new())
            } else {
                (bare, body)
            };
            let f = fence_for(&body);
            Some(if body.trim().is_empty() && !matches!(tag.as_str(), "bash" | "sh") {
                format!("```{tag} {}```", arg.trim())
            } else if body.trim().is_empty() {
                format!("```{tag}\n{}\n```", arg.trim())
            } else {
                format!("{f}{tag}{}{}\n{}\n{f}", if arg.trim().is_empty() { "" } else { " " }, arg.trim(), body)
            })
        } else {
            if !body.trim().is_empty() {
                let key = match tag.as_str() {
                    "bash" | "sh" | "test" => "command",
                    "write" => "content",
                    "submit" | "done" => "report",
                    "team" => "message",
                    "grep" => "pattern",
                    "search" => "query",
                    _ => "body",
                };
                params.push((key.into(), body.clone()));
            }
            block(&tag, &Params(params))
        };
        let Some(b) = b else { continue };
        out.push_str(&text[last..whole.start()]);
        out.push_str(&format!("\n{b}\n"));
        last = end;
        from = end;
        n += 1;
    }
    out.push_str(&text[last..]);
    (out, n)
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
            let known = tags.contains(&tag) || matches!(tag, "write" | "append" | "edit" | "patch");
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
    fn tags_named_for_tools_run_and_code_is_never_touched() {
        // as DeepSeek-V4-Flash wrote it in a real job
        let t = "Let me look at key files. <read INTERFACES.md> <read tests/oracle-modules.test.js>";
        let (out, n) = normalize_calls(t, WORKER);
        assert_eq!(n, 2, "{out}");
        let b = tool_blocks(&out, WORKER);
        assert_eq!(b.iter().map(|b| (b.tag.as_str(), b.arg.as_str())).collect::<Vec<_>>(),
                   vec![("read", "INTERFACES.md"), ("read", "tests/oracle-modules.test.js")]);
        assert_eq!(crate::prose(&out, WORKER).trim(), "Let me look at key files.");
        // the other shapes of it
        let t = "<bash>npm test -- --run</bash>\n<read path=\"js/game.js\" start_line=\"10\" end_line=\"40\"/>\n\
                 <write path=\"js/a.js\">\nexport const a = 1;\n</write>\n<grep>createGame js/</grep>\n\
                 <team @ds-2>engine.js exports create(seed)</team>\n<submit>\nDone: tests pass.\n</submit>";
        let (out, n) = normalize_calls(t, WORKER);
        assert_eq!(n, 6, "{out}");
        let b = tool_blocks(&out, WORKER);
        assert_eq!((b[0].tag.as_str(), b[0].body.as_str()), ("bash", "npm test -- --run"));
        assert_eq!((b[1].tag.as_str(), b[1].arg.as_str()), ("read", "js/game.js:10-40"));
        assert_eq!((b[2].tag.as_str(), b[2].arg.as_str()), ("grep", "createGame js/"));
        assert_eq!((b[3].tag.as_str(), b[3].arg.as_str(), b[3].body.as_str()), ("team", "@ds-2", "engine.js exports create(seed)"));
        assert_eq!((b[4].tag.as_str(), b[4].body.as_str()), ("submit", "Done: tests pass."));
        let f = file_blocks(&out);
        assert_eq!((f[0].path.as_str(), f[0].body.as_str()), ("js/a.js", "export const a = 1;\n"));
        // a file's own markup, an example in backticks, and HTML that names no tool stay as they are
        let html = "```write index.html\n<search><map name=\"m\"></map></search>\n<read me>\n```\nUse `<read x>` then <div>ok</div>.";
        assert_eq!(normalize_calls(html, LEAD), (html.to_string(), 0));
        let (out, n) = normalize_calls("<read>", WORKER);
        assert_eq!((out.as_str(), n), ("<read>", 0), "a bare tag says nothing to run");
    }

    #[test]
    fn a_call_cut_off_mid_file_keeps_what_arrived_as_an_open_block() {
        let args = r#"{"path": "js/big.js", "content": "const a = \"x\";\nfunction f() {\n  return '\u00e9\ud83d\ude00';\n  // and so o"#;
        let b = partial_block("write", args, WORKER).unwrap();
        assert_eq!(b, "```write js/big.js\nconst a = \"x\";\nfunction f() {\n  return 'é😀';\n  // and so o");
        // cut inside an escape: up to the escape
        assert!(partial_block("write", r#"{"path": "a", "content": "ab\u00"#, WORKER).unwrap().ends_with("\nab"));
        // the name not complete yet: nothing to continue
        assert!(partial_block("write", r#"{"path": "js/bi"#, WORKER).is_none());
        // a body holding a fence gets a longer one
        assert!(partial_block("write", r#"{"path": "R.md", "content": "```sh\nls"#, WORKER).unwrap().starts_with("````write R.md\n"));
        assert_eq!(partial_block("bash", r#"{"command": "for i in 1 2; do ech"#, WORKER).unwrap(), "```bash\nfor i in 1 2; do ech");
        assert!(partial_block("read", r#"{"path": "a"#, WORKER).is_none());
    }

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
