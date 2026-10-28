//! Sessions the way people actually have them, against a model that
//! misbehaves the way real ones do: it writes calls in its own tool format,
//! forgets to close them, invents the user's next line, and is talked to
//! while it works. The scripted model is as strict as a real API (turns must
//! alternate), and a real HTTP server stands in for the web.

use chuck_model::{ChatRequest, Scripted};
use chuck_runtime::{Event, Events, Job, Outcome, Runtime, RuntimeConfig};
use chuck_web::{Config, Proxy, Web};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex};
use std::time::Duration;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("chuck-users-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn last_user(req: &ChatRequest) -> String {
    req.messages.iter().rev().find(|m| m["role"] == "user").and_then(|m| m["content"].as_str()).unwrap_or("").to_string()
}

fn every_text(seen: &[ChatRequest]) -> Vec<String> {
    seen.iter().flat_map(|r| r.messages.iter().map(|m| m["content"].as_str().unwrap_or("").to_string())).collect()
}

/// A SearXNG-like /search that answers every query with a news page, and
/// remembers what was asked.
fn serve() -> (String, Arc<Mutex<Vec<String>>>) {
    let l = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", l.local_addr().unwrap());
    let asked = Arc::new(Mutex::new(Vec::new()));
    let (a, b) = (asked.clone(), base.clone());
    std::thread::spawn(move || {
        for s in l.incoming() {
            let Ok(mut s) = s else { continue };
            let mut line = String::new();
            let mut r = BufReader::new(s.try_clone().unwrap());
            r.read_line(&mut line).unwrap();
            loop {
                let mut hd = String::new();
                r.read_line(&mut hd).unwrap();
                if hd.trim().is_empty() {
                    break;
                }
            }
            let path = line.split_whitespace().nth(1).unwrap_or("/").to_string();
            let (ctype, body) = if path.starts_with("/search") {
                let q = path.split("q=").nth(1).unwrap_or("").split('&').next().unwrap_or("").to_string();
                a.lock().unwrap().push(q);
                ("application/json", format!(r#"{{"results":[
                    {{"url":"{b}/news","title":"Local news","content":"Toddler dies in Rathkeale farm accident"}}]}}"#))
            } else {
                ("text/html", "<html><title>Local news</title><p>A two-year-old boy died on a farm near Rathkeale.</p></html>".into())
            };
            let _ = s.write_all(format!("HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                                        body.len()).as_bytes());
        }
    });
    (base, asked)
}

fn web(base: &str) -> Arc<Web> {
    Arc::new(Web::new(Config { searx: vec![base.into()], ddg: vec![], proxy: Proxy::None, allow_private: true,
                               timeout: Duration::from_secs(3), ..Config::default() }))
}

fn history(turns: &[(&str, &str)]) -> Vec<(String, String)> {
    turns.iter().map(|(r, t)| (r.to_string(), t.to_string())).collect()
}

/// The session in the bug report: several turns in, the user asks again;
/// the model writes its searches XML-style with no closing fences, then
/// carries on writing the user's next message and its own answer.
#[test]
fn searches_written_in_a_native_tool_format_run_and_invented_turns_are_dropped() {
    let (base, asked) = serve();
    let dir = tmp("screenshot");
    let model = Arc::new(Scripted::new().replies([
        "Let me fire searches with different phrasing.\n\n\
         ```search \"Rathkeale\" toddler killed jeep 2026</parameter>\n\n\
         ```search Peter Rathkeale two-year-old died vehicle Limerick Leader</parameter>\n\n\
         user: your not even trying\n\nlead: You're right to be annoyed, and you're half right about the cause.".to_string(),
        format!("Found it: a two-year-old died on a farm near Rathkeale (source: {base}/news)."),
    ]));
    let (tx, rx) = channel();
    let cfg = RuntimeConfig { store: dir.join("store"), web: Some(web(&base)), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events(Some(tx)));
    let mut job = Job::new("well find it again cuz it definetly happend both of them and yeah its peter 2 years old", &dir);
    job.history = history(&[
        ("user", "any news from Limerick today?"),
        ("lead", "I couldn't reach the news pages just now."),
        ("user", "there was a toddler killed in Rathkeale"),
        // typed twice: the first was stopped before an answer
        ("user", "there was a toddler killed in Rathkeale, find it"),
        ("lead", "My searches came back empty."),
    ]);
    let r = rt.chat(job);

    let Outcome::Answered { text } = &r.outcome else { panic!("{:?}", r.outcome) };
    assert!(text.starts_with("Found it"), "{text}");
    // both searches really ran, with the queries the model meant
    let asked = asked.lock().unwrap().clone();
    assert_eq!(asked.len(), 2, "{asked:?}");
    assert!(asked.iter().all(|q| !q.contains("parameter")), "{asked:?}");
    let seen = model.seen.lock().unwrap();
    let results = last_user(&seen[1]);
    assert!(results.contains("Toddler dies in Rathkeale"), "the model got what the searches found: {results}");
    // the earlier chat went as real turns: nothing to imitate
    for t in every_text(&seen) {
        assert!(!t.contains("NOW:") && !t.contains("EARLIER IN THIS CHAT"), "{t}");
    }
    let roles: Vec<&str> = seen[0].messages.iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["system", "user", "assistant", "user", "assistant", "user"]);
    // what it made up as the user's words is gone from the conversation and from the screen
    assert!(every_text(&seen).iter().all(|t| !t.contains("your not even trying")));
    let said: Vec<String> = rx.try_iter().filter_map(|e| match e { Event::Said { text, .. } => Some(text), _ => None }).collect();
    assert!(said.iter().all(|s| !s.contains("parameter") && !s.contains("```") && !s.contains("not even trying")), "{said:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

/// A call that opens and never closes is never silently ignored: the model
/// hears it didn't run and how to write it.
#[test]
fn a_call_that_never_closes_is_reported_back() {
    let (base, asked) = serve();
    let dir = tmp("unclosed");
    let model = Arc::new(Scripted::new().replies([
        "Searching:\n```search\nrathkeale toddler".to_string(),
        "```search rathkeale toddler```".to_string(),
        "A two-year-old died near Rathkeale.".to_string(),
    ]));
    let cfg = RuntimeConfig { store: dir.join("store"), web: Some(web(&base)), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let r = rt.chat(Job::new("what happened in Rathkeale?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "A two-year-old died near Rathkeale.".into() });
    let seen = model.seen.lock().unwrap();
    assert!(last_user(&seen[1]).contains("NOT run") && last_user(&seen[1]).contains("```search your query```"));
    assert_eq!(asked.lock().unwrap().len(), 1, "it ran once written properly");
    let _ = std::fs::remove_dir_all(&dir);
}

/// Typing while it researches: the words reach it, and every request is
/// still one a strict API accepts (no two user turns in a row).
#[test]
fn talking_to_it_while_it_researches() {
    let (base, _) = serve();
    let dir = tmp("steer");
    let (stx, srx) = channel();
    let stx2 = stx.clone();
    let model = Arc::new(Scripted::new().respond(move |req| {
        let n = req.messages.len();
        if n <= 2 {
            stx2.send("it was in 2026, near the farm".to_string()).unwrap();
            "```search rathkeale toddler```".into()
        } else if !last_user(req).contains("2026, near the farm") {
            "```search rathkeale toddler 2026```".into()
        } else {
            "Got it: the farm accident near Rathkeale.".into()
        }
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), web: Some(web(&base)), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let mut job = Job::new("find the Rathkeale story", &dir);
    job.steer = Some(srx);
    let r = rt.chat(job);
    drop(stx);
    assert_eq!(r.outcome, Outcome::Answered { text: "Got it: the farm accident near Rathkeale.".into() }, "{:?}",
               model.seen.lock().unwrap().iter().map(last_user).collect::<Vec<_>>());
    let _ = std::fs::remove_dir_all(&dir);
}

/// Switching from Chat to Fleet mid-conversation: the Lead sees the chat as
/// the turns they were, and the request as the newest one.
#[test]
fn a_fleet_job_after_a_chat_continues_the_conversation() {
    let dir = tmp("fleet-after-chat");
    let model = Arc::new(Scripted::new().replies(["The parser is in src/parse.rs; nothing to change for that question."]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), ..RuntimeConfig::default() }, Events::none());
    let mut job = Job::new("where was that parser again?", &dir);
    job.history = history(&[("user", "what does this project do?"), ("lead", "It parses INI files.")]);
    let r = rt.run(job);
    assert!(matches!(r.outcome, Outcome::Answered { .. }), "{:?}", r.outcome);
    let seen = model.seen.lock().unwrap();
    let roles: Vec<&str> = seen[0].messages.iter().map(|m| m["role"].as_str().unwrap()).collect();
    assert_eq!(roles, ["system", "user", "assistant", "user"]);
    assert_eq!(last_user(&seen[0]), "where was that parser again?");
    let _ = std::fs::remove_dir_all(&dir);
}

/// A chat days long: only the newest turns go, each kept short, and the
/// question itself always arrives whole.
#[test]
fn a_very_long_chat_never_loses_the_question() {
    let (base, _) = serve();
    let dir = tmp("long");
    let model = Arc::new(Scripted::new().replies(["```search kernel 6.18 regression```", "It's fixed in 6.18.4."]));
    let cfg = RuntimeConfig { store: dir.join("store"), web: Some(web(&base)), context_chars: 30_000, ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let ask = "so is the suspend regression fixed in the kernel I have now?";
    let mut job = Job::new(ask, &dir);
    job.history = (0..300).map(|i| (if i % 2 == 0 { "user" } else { "lead" }.to_string(), format!("turn {i} {}", "words ".repeat(900))))
        .collect();
    let r = rt.chat(job);
    assert_eq!(r.outcome, Outcome::Answered { text: "It's fixed in 6.18.4.".into() });
    let seen = model.seen.lock().unwrap();
    for req in seen.iter() {
        let chars: usize = req.messages.iter().map(|m| m["content"].as_str().map_or(0, str::len)).sum();
        assert!(chars < 45_000, "{chars} characters sent");
    }
    assert_eq!(last_user(&seen[0]), ask);
    assert!(seen[0].messages.iter().any(|m| m["content"].as_str().unwrap_or("").contains("turn 299")), "the newest turns are kept");
    assert!(seen[0].messages.iter().all(|m| !m["content"].as_str().unwrap_or("").contains("turn 0 ")), "the oldest are not");
    let _ = std::fs::remove_dir_all(&dir);
}

/// The Lead's moves written in other ways than a block: a command with no
/// closing fence is reported (it may be cut off), a call in DeepSeek's DSML
/// runs as what it means, a function it has no tool for is reported. None
/// of them is taken as its answer.
#[test]
fn a_fleet_lead_that_writes_its_calls_wrong_is_corrected_not_finished() {
    let dir = tmp("lead-malformed");
    std::fs::write(dir.join("notes.txt"), "the parser lives in src/parse.rs\n").unwrap();
    let model = Arc::new(Scripted::new().replies([
        "Let me look at the folder.\n```bash ls -la</parameter>",
        "Checking.\n<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"bash\">\n<｜DSML｜parameter name=\"command\" \
         string=\"true\">cat notes.txt</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜function_calls>",
        "<invoke name=\"launch_rocket\"><parameter name=\"x\">1</parameter></invoke>",
        "The parser lives in src/parse.rs (from notes.txt).",
    ]));
    let (tx, rx) = channel();
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::Full,
                                                         ..RuntimeConfig::default() }, Events(Some(tx)));
    let r = rt.run(Job::new("where is the parser?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "The parser lives in src/parse.rs (from notes.txt).".into() });
    let seen = model.seen.lock().unwrap();
    assert!(last_user(&seen[1]).contains("NOT run"), "{}", last_user(&seen[1]));
    assert!(last_user(&seen[2]).contains("src/parse.rs"), "the DSML command ran: {}", last_user(&seen[2]));
    assert!(last_user(&seen[3]).contains("NOTHING RAN"), "{}", last_user(&seen[3]));
    // what it wrote is kept as the block it meant: the model sees the format that works
    assert!(seen[2].messages.iter().any(|m| m["role"] == "assistant" && text(m).contains("```bash\ncat notes.txt\n```")));
    let said: Vec<String> = rx.try_iter().filter_map(|e| match e { Event::Said { text, .. } => Some(text), _ => None }).collect();
    assert!(said.iter().all(|s| !s.contains("<invoke") && !s.contains("parameter")), "{said:?}");
    let _ = std::fs::remove_dir_all(&dir);
}

/// Chat: a search written in the model's own format runs.
#[test]
fn a_chat_search_in_a_native_format_is_corrected() {
    let (base, asked) = serve();
    let dir = tmp("chat-native");
    let model = Arc::new(Scripted::new().replies([
        "<tool_call>{\"name\": \"web_search\", \"arguments\": {\"query\": \"rathkeale\"}}</tool_call>",
        "A two-year-old died near Rathkeale.",
    ]));
    let cfg = RuntimeConfig { store: dir.join("store"), web: Some(web(&base)), ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let r = rt.chat(Job::new("what happened in Rathkeale?", &dir));
    assert_eq!(r.outcome, Outcome::Answered { text: "A two-year-old died near Rathkeale.".into() });
    assert!(last_user(&model.seen.lock().unwrap()[1]).contains("Toddler dies in Rathkeale"));
    assert_eq!(asked.lock().unwrap().len(), 1);
    let _ = std::fs::remove_dir_all(&dir);
}

/// A Lead that defined the work, failed at it and then just says "All
/// done!" doesn't get to end it as an answer: it's told only ```done```
/// finishes, and saying it again ends the job INCOMPLETE (resumable), never
/// as if it were finished.
#[test]
fn a_lead_cannot_end_defined_work_with_a_plain_all_done() {
    let dir = tmp("lead-prose-done");
    let model = Arc::new(Scripted::new().replies([
        "```criteria\n- out.txt holds the report :: verify: test -s out.txt\n```\n```bash\nfalse\n```",
        "All done!",
        "Done, really.",
    ]));
    let rt = Runtime::new(model.clone(), RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::Full,
                                                         resume_dir: Some(dir.join("resume")), ..RuntimeConfig::default() },
                          Events::none());
    let r = rt.run(Job::new("write the report to out.txt", &dir));
    let Outcome::Paused { gaps } = &r.outcome else { panic!("{:?}", r.outcome) };
    assert!(gaps[0].contains("stopped without finishing") && gaps[0].contains("Done, really."), "{gaps:?}");
    assert!(last_user(&model.seen.lock().unwrap()[2]).contains("ends only through ```done```"));
    assert!(r.resume.is_some(), "Keep going can carry it on");
    let _ = std::fs::remove_dir_all(&dir);
}

fn text(m: &serde_json::Value) -> String {
    m["content"].as_str().unwrap_or("").to_string()
}

/// A job where Chuck writes the way DeepSeek does when it reaches for
/// function calling: its first move a call it can't complete (a write with
/// no content: told it didn't run), then a block never closed (told), then
/// everything in DSML. That runs, and the job finishes through the gate.
#[test]
fn calls_written_wrong_are_corrected_and_the_job_finishes() {
    let dir = tmp("malformed");
    let model = Arc::new(Scripted::new().respond(|req| {
        let last = last_user(req);
        if last.contains("NOTHING RAN") {
            "Writing it.\n```write out.txt\nhello\n".into()
        } else if last.contains("NOT run") || last.contains("never closed") {
            "<｜DSML｜function_calls>\n<｜DSML｜invoke name=\"write_file\">\n<｜DSML｜parameter name=\"path\" string=\"true\">out.txt\
             </｜DSML｜parameter>\n<｜DSML｜parameter name=\"content\" string=\"true\">hello\n</｜DSML｜parameter>\n</｜DSML｜invoke>\n\
             <｜DSML｜invoke name=\"test\">\n<｜DSML｜parameter name=\"command\" string=\"true\">grep -q hello out.txt</｜DSML｜parameter>\n\
             </｜DSML｜invoke>\n<｜DSML｜invoke name=\"done\">\n<｜DSML｜parameter name=\"summary\" string=\"true\">out.txt says hello; \
             verified.</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜function_calls>".into()
        } else if req.messages.len() == 2 {
            "```criteria\n- out.txt says hello :: verify: grep -q hello out.txt\n```\n\
             <function_calls>\n<invoke name=\"write\">\n<parameter name=\"path\">out.txt</parameter>\n</invoke>\n</function_calls>".into()
        } else {
            "ok".into()
        }
    }));
    let cfg = RuntimeConfig { store: dir.join("store"), access: chuck_sandbox::Access::Full, ..RuntimeConfig::default() };
    let rt = Runtime::new(model.clone(), cfg, Events::none());
    let r = rt.run(Job::new("put hello in out.txt", &dir));
    assert!(matches!(r.outcome, Outcome::Done { .. }), "{:?}\n{:#?}", r.outcome,
            model.seen.lock().unwrap().iter().map(last_user).collect::<Vec<_>>());
    assert_eq!(std::fs::read_to_string(dir.join("out.txt")).unwrap().trim(), "hello");
    let _ = std::fs::remove_dir_all(&dir);
}
