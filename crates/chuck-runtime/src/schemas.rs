//! The agents' tools as functions the model can call (OpenAI-style
//! `tools`). A model trained on function calling calls them reliably; one
//! left to write calls as text invents formats of its own (DSML, `<read x>`).
//! What it calls comes back parsed by the API and is made into the same
//! fenced block a call written as text would be, so everything after
//! (the gate, the jail, the evidence) is the same either way.
//!
//! Each function takes what its block takes: the common ones named fields,
//! the rest `arg` (the block's first line) and `body` (what's inside it).
//! Descriptions are short: they are sent with every call (and cached).

use serde_json::{json, Value};

fn s(desc: &str) -> Value {
    json!({"type": "string", "description": desc})
}

fn f(name: &str, desc: &str, props: Value, required: &[&str]) -> Value {
    json!({"type": "function", "function": {"name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}})
}

/// A function that takes a block's argument line and body.
fn block(name: &str, desc: &str, arg: &str, body: &str, required: &[&str]) -> Value {
    let mut props = serde_json::Map::new();
    if !arg.is_empty() {
        props.insert("arg".into(), s(arg));
    }
    if !body.is_empty() {
        props.insert("body".into(), s(body));
    }
    f(name, desc, Value::Object(props), required)
}

fn one(tag: &str) -> Option<Value> {
    Some(match tag {
        "bash" => f("bash", "Run shell commands in the project folder (no sudo). Output comes back.",
                    json!({"command": s("the commands")}), &["command"]),
        "test" => f("test", "Run a verification (the tests, a build, a linter, a criterion's verify command).",
                    json!({"command": s("the command")}), &["command"]),
        "read" => f("read", "Read a file (a page at a time) or list a folder.",
                    json!({"path": s("file or folder, relative to the project"), "start_line": json!({"type": "integer"}),
                           "end_line": json!({"type": "integer"})}), &["path"]),
        "write" => f("write", "Write a whole file (created or replaced).",
                     json!({"path": s("relative path"), "content": s("the complete file")}), &["path", "content"]),
        "edit" => f("edit", "Replace an exact, unique piece of a file.",
                    json!({"path": s("relative path"), "old_string": s("the current lines, exactly"),
                           "new_string": s("what replaces them")}), &["path", "old_string", "new_string"]),
        "patch" => block("patch", "Apply a unified diff (all or nothing).", "", "the diff", &["body"]),
        "grep" => f("grep", "Search the code with a regex.",
                    json!({"pattern": s("regex"), "path": s("where (optional)")}), &["pattern"]),
        "glob" => f("glob", "Find files by pattern.", json!({"pattern": s("e.g. src/**/*.rs")}), &["pattern"]),
        "map" => f("map", "The project's files and what each defines.", json!({}), &[]),
        "def" => block("def", "Where a name (or Class.method) is defined.", "the name", "", &["arg"]),
        "refs" => block("refs", "Every use of a name.", "the name", "", &["arg"]),
        "find" => block("find", "Code ranked by meaning, when you don't know its name.", "what the code does", "",
                        &["arg"]),
        "rename" => block("rename", "Rename a symbol and its real uses everywhere.", "old new", "", &["arg"]),
        "search" => f("search", "Search the web.", json!({"query": s("the query")}), &["query"]),
        "fetch" => f("fetch", "Read a web page as text.", json!({"url": s("the address"), "part": s("which part (optional)")}),
                     &["url"]),
        "images" => block("images", "Show the user pictures.", "what of", "", &["arg"]),
        "videos" => block("videos", "Show the user videos.", "what of", "", &["arg"]),
        "submit" => f("submit", "Hand your task to the Lead: what changed, how each criterion was verified.",
                      json!({"report": s("the report")}), &["report"]),
        "team" => f("team", "Message the team: everyone, or one (`ds-2`, `lead`).",
                    json!({"to": s("one name (optional: everyone)"), "message": s("short and concrete")}), &["message"]),
        "browse" => block("browse", "Load a page or file in a real browser: JS errors, failed requests, text, a look.",
                          "url or file, optional WxH, optional `for c1`",
                          "optional action lines: click <css|x,y>, type <text>, press <keys>, wait <ms>, scroll <px>, \
                           eval <js>, goto <url>", &["arg"]),
        "mcp" => block("mcp", "Call an MCP server's tool.", "server.tool", "the arguments as JSON", &["arg"]),
        "criteria" => block("criteria", "Set the acceptance criteria.", "",
                            "one per line: `- <criterion> :: verify: <command>`", &["body"]),
        "plan" => block("plan", "Set the plan, or mark steps (arg `done 2 3`).", "`done N…` to mark steps",
                        "the steps, one per line: `- [ ] <step>`", &[]),
        "assign" => block("assign", "Give a task to a worker.", "`worker`, or `race`; optionally `after t1,t2`",
                          "`title: …`, then `criteria:` and lines `- <criterion> :: verify: <command>`, then `brief:` \
                           and the work (the files it owns, the interfaces it provides or uses)", &["arg", "body"]),
        "review" => block("review", "Your verdict on a submission.", "`t1 accept` or `t1 reject`",
                          "for a rejection, one `- <defect>` line each", &["arg"]),
        "cancel" => block("cancel", "Take a task back: its workers stop.", "`t2 <why>`", "", &["arg"]),
        "justify" => block("justify", "Why a small change fully answers a large ask.", "`proportion` (or `t1 proportion`)",
                           "why", &["arg", "body"]),
        "done" => f("done", "Finish the job (the gate and an independent review check it).",
                    json!({"summary": s("for the user: what changed and how it was verified")}), &["summary"]),
        "ask" => block("ask", "Ask the user a decision that is theirs.", "", "the question", &["body"]),
        "remember" => block("remember", "Keep a durable fact for later chats (`project:` first for this folder only).",
                            "the fact", "", &["arg"]),
        "forget" => block("forget", "Drop remembered facts that match.", "what", "", &["arg"]),
        "remind" => block("remind", "Remind the user.", "`<when>: <what>`", "", &["arg"]),
        "daemon" => block("daemon", "Keep an eye on something and report.", "`<when>: <what to look at>`", "", &["arg"]),
        "agent" => block("agent", "Hand a request to the fleet, which can act on the machine.", "",
                         "the request, complete and self-contained", &["body"]),
        _ => return None,
    })
}

/// The functions for an agent with `tags` (an alias like `sh` is left out;
/// files are written by any agent that runs commands).
pub fn tools(tags: &[&str]) -> Value {
    let mut names: Vec<&str> = tags.iter().copied().filter(|t| *t != "sh").collect();
    if tags.contains(&"bash") {
        names.extend(["write", "edit", "patch"]);
    }
    Value::Array(names.into_iter().filter_map(one).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::exec::{LEAD_TAGS, WORKER_TAGS};

    #[test]
    fn every_tool_an_agent_has_is_a_function_and_its_call_runs_as_its_block() {
        for tags in [WORKER_TAGS, LEAD_TAGS, crate::chat::CHAT_TAGS] {
            let t = tools(tags);
            let names: Vec<&str> = t.as_array().unwrap().iter().map(|f| f["function"]["name"].as_str().unwrap()).collect();
            for tag in tags.iter().filter(|t| **t != "sh") {
                assert!(names.contains(tag), "{tag} has no function");
            }
        }
        // what a call of each comes back as
        let w = |name: &str, args: Value| chuck_tools::native::call_block(name, &args.to_string(), LEAD_TAGS);
        assert_eq!(w("bash", json!({"command": "npm test"})).unwrap(), "```bash\nnpm test\n```");
        assert_eq!(w("read", json!({"path": "a.rs", "start_line": 3, "end_line": 9})).unwrap(), "```read a.rs:3-9```");
        let a = w("assign", json!({"arg": "worker after t1", "body": "title: x\ncriteria:\n- y :: verify: true\nbrief: z"}))
            .unwrap();
        let b = &chuck_tools::tool_blocks(&a, LEAD_TAGS)[0];
        assert_eq!((b.tag.as_str(), b.arg.as_str()), ("assign", "worker after t1"));
        assert!(b.body.contains("criteria:\n- y :: verify: true"));
        let r = w("review", json!({"arg": "t1 reject", "body": "- no test"})).unwrap();
        assert_eq!(chuck_tools::tool_blocks(&r, LEAD_TAGS)[0].body, "- no test");
        assert_eq!(w("map", json!({})).unwrap(), "```map```");
        assert!(w("launch_rocket", json!({})).is_none());
    }
}
