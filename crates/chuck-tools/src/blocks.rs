//! Reading what an agent asked for. Agents act through fenced blocks:
//!
//! ````text
//! ```write path/to/file        a whole file (nested ``` fences tracked)
//! ```edit path                 SEARCH/REPLACE blocks
//! ```patch                     a unified diff
//! ```bash  ```test  ```read …  one-line or multi-line tool calls
//! ````
//!
//! The file-block parser is fence-aware: a README's own ```bash examples are
//! part of the file, never run; a 4-backtick block ends only at ````; and a
//! block whose end is genuinely ambiguous writes nothing.

use std::sync::OnceLock;

#[derive(Clone, Debug, PartialEq)]
pub struct FileBlock {
    pub tag: String,
    pub path: String,
    pub body: String,
    pub ambiguous: bool,
    pub start: usize,
    pub end: usize,
}

/// A non-file tool block: ```tag arg\nbody```.
#[derive(Clone, Debug, PartialEq)]
pub struct ToolBlock {
    pub tag: String,
    pub arg: String,
    pub body: String,
    pub start: usize,
    pub end: usize,
}

fn re(p: &str) -> regex::Regex {
    regex::Regex::new(p).expect("valid regex")
}

/// `[ \t]*(```|````)(write|edit|patch)` not followed by a letter.
fn file_fence(line: &str) -> Option<(String, String, String)> {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    let r = R.get_or_init(|| re(r"^[ \t]*(`{3,4})(write|edit|patch)(.*)$"));
    let c = r.captures(line)?;
    let rest = c.get(3).map_or("", |m| m.as_str());
    if rest.chars().next().is_some_and(|ch| ch.is_ascii_alphabetic()) {
        return None;
    }
    Some((c[1].to_string(), c[2].to_string(), rest.trim().to_string()))
}

/// Files whose text may hold ``` fences of their own.
fn doc_ext(path: &str) -> bool {
    let p = path.to_lowercase();
    [".md", ".markdown", ".mdx", ".rst", ".txt", ".adoc", ".ipynb", ".org"].iter().any(|e| p.ends_with(e))
}

const HEAD_LANGS: &[&str] = &["html", "htm", "css", "js", "javascript", "ts", "typescript", "tsx", "jsx",
    "python", "py", "json", "yaml", "yml", "toml", "md", "markdown", "sh", "bash", "text", "txt", "rust",
    "go", "c", "cpp", "java", "ruby", "php", "lua", "sql", "xml", "svg"];

/// The path a model meant: `: app.py`, "app.py", `app.py`, path=app.py,
/// ./app.py and `app.py python` all mean app.py.
pub fn clean_head(head: &str) -> String {
    static PFX: OnceLock<regex::Regex> = OnceLock::new();
    let mut h = head.trim().trim_start_matches(':').trim().to_string();
    let pfx = PFX.get_or_init(|| re(r"(?i)^(?:path|file|filename)\s*[:=]\s*"));
    h = pfx.replace(&h, "").to_string();
    let b = h.as_bytes();
    if b.len() >= 2 && b[0] == b[b.len() - 1] && matches!(b[0], b'"' | b'\'' | b'`') {
        h = h[1..h.len() - 1].trim().to_string();
    }
    if let Some((first, last)) = h.rsplit_once(char::is_whitespace) {
        if HEAD_LANGS.contains(&last.to_lowercase().as_str()) && first.contains('.') {
            h = first.trim().trim_matches(|c| matches!(c, '"' | '\'' | '`')).to_string();
        }
    }
    while let Some(rest) = h.strip_prefix("./") {
        h = rest.to_string();
    }
    h
}

fn is_sr_open(st: &str) -> bool {
    let t = st.trim_end();
    let n = t.chars().take_while(|&c| c == '<').count();
    (5..=9).contains(&n) && t[n..].trim_start_matches(' ') == "SEARCH"
}

fn is_sr_close(st: &str) -> bool {
    let t = st.trim_end();
    let n = t.chars().take_while(|&c| c == '>').count();
    (5..=9).contains(&n) && t[n..].trim_start_matches(' ') == "REPLACE"
}

/// ```write / ```edit / ```patch blocks, nested-fence aware.
pub fn file_blocks(text: &str) -> Vec<FileBlock> {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut offs = Vec::with_capacity(lines.len());
    let mut pos = 0;
    for l in &lines {
        offs.push(pos);
        pos += l.len() + 1;
    }
    let mut out = Vec::new();
    let mut i = 0;
    while i < lines.len() {
        let Some((fence, tag, mut head)) = file_fence(lines[i]) else {
            i += 1;
            continue;
        };
        let mut j = i + 1;
        if head.ends_with(&fence) {
            i += 1; // ```write a.py``` — empty
            continue;
        }
        if tag == "patch" {
            // every diff body line starts with ' ', '+' or '-': only the
            // closing fence can be exactly the fence
            let mut k = j;
            while k < lines.len() && lines[k].trim_end() != fence {
                k += 1;
            }
            if k >= lines.len() {
                i += 1;
                continue;
            }
            let mut body: Vec<&str> = Vec::new();
            if !head.is_empty() {
                body.push(&head);
            }
            body.extend(&lines[j..k]);
            out.push(FileBlock { tag, path: String::new(), body: body.join("\n") + "\n", ambiguous: false,
                                 start: offs[i], end: (offs[k] + lines[k].len()).min(text.len()) });
            i = k + 1;
            continue;
        }
        if head.is_empty() {
            if j >= lines.len() {
                break;
            }
            head = lines[j].trim().to_string();
            j += 1;
        }
        let head = clean_head(&head);
        let body_start = j;
        let (mut end_line, mut end_col) = (None, None);
        let mut depth = 0i32;
        let mut first_bare = None;
        let mut in_sr = false;
        let mut k = j;
        while k < lines.len() {
            let st = lines[k].trim();
            if tag == "edit" {
                if is_sr_open(st) {
                    in_sr = true;
                    k += 1;
                    continue;
                }
                if is_sr_close(st) {
                    in_sr = false;
                    k += 1;
                    continue;
                }
                if in_sr {
                    k += 1;
                    continue;
                }
            }
            if fence.len() == 4 {
                if st == "````" {
                    end_line = Some(k);
                    break;
                }
            } else if st == "```" {
                first_bare.get_or_insert(k);
                if depth == 0 {
                    end_line = Some(k);
                    break;
                }
                depth -= 1;
            } else if st.starts_with("```") && !st.starts_with("````") {
                depth += 1;
            } else if lines[k].trim_end().ends_with(&fence) && depth == 0 {
                end_line = Some(k);
                end_col = Some(lines[k].trim_end().rfind(&fence).unwrap());
                break;
            }
            k += 1;
        }
        if end_line.is_none() {
            end_line = first_bare; // unbalanced: the first bare fence
        }
        let Some(el) = end_line else {
            i += 1; // never closed (cut off)
            continue;
        };
        let mut body = match end_col {
            Some(c) => {
                let mut v: Vec<&str> = lines[body_start..el].to_vec();
                v.push(&lines[el][..c]);
                v.join("\n")
            }
            None => {
                let mut b = lines[body_start..el].join("\n");
                if el > body_start {
                    b.push('\n');
                }
                b
            }
        };
        let mut end = offs[el] + lines[el].len();
        // A bare ``` inside a 3-backtick block is undecidable for a document
        // (or after an empty body): a later fence that closes nothing means
        // we may have closed too early. Write nothing; hide it all.
        let mut ambiguous = false;
        if fence.len() == 3 && tag == "write" && (doc_ext(&head) || body.trim().is_empty()) {
            let mut depth_after = 0;
            for (n, ln) in lines.iter().enumerate().skip(el + 1) {
                if file_fence(ln).is_some() {
                    break;
                }
                let st = ln.trim();
                if st == "```" {
                    if depth_after > 0 {
                        depth_after -= 1;
                        continue;
                    }
                    ambiguous = true;
                    end = offs[n] + ln.len();
                    continue;
                }
                if st.starts_with("```") && !st.starts_with("````") && !(st.len() > 6 && st.ends_with("```")) {
                    depth_after += 1;
                }
            }
        }
        if body.contains('\r') {
            body = body.replace("\r\n", "\n");
        }
        out.push(FileBlock { tag, path: head, body, ambiguous, start: offs[i], end: end.min(text.len()) });
        i = el + 1;
    }
    out
}

/// SEARCH/REPLACE pairs in an edit body. Every marker is a whole line and the
/// divider is exactly seven '='.
pub fn edit_pairs(text: &str) -> Vec<(String, String)> {
    static R: OnceLock<regex::Regex> = OnceLock::new();
    let r = R.get_or_init(|| re(r"(?ms)^<{5,9} ?SEARCH[ \t]*\n(.*?)\n?^={7}[ \t]*\n(.*?)\n?^>{5,9} ?REPLACE[ \t]*$"));
    let t = text.replace("\r\n", "\n");
    r.captures_iter(&t).map(|c| (c[1].to_string(), c[2].to_string())).collect()
}

/// Tool blocks with any of `tags`, outside file blocks: ```tag arg\nbody```
/// or the one-line ```tag arg```.
pub fn tool_blocks(text: &str, tags: &[&str]) -> Vec<ToolBlock> {
    let skip: Vec<(usize, usize)> = file_blocks(text).iter().map(|b| (b.start, b.end)).collect();
    let mut out = Vec::new();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut offs = Vec::new();
    let mut pos = 0;
    for l in &lines {
        offs.push(pos);
        pos += l.len() + 1;
    }
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i].trim_start();
        let inside = skip.iter().any(|(a, b)| offs[i] >= *a && offs[i] < *b);
        let Some(rest) = line.strip_prefix("```").filter(|r| !r.starts_with('`')) else {
            i += 1;
            continue;
        };
        if inside {
            i += 1;
            continue;
        }
        let tag_end = rest.find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-')).unwrap_or(rest.len());
        let tag = &rest[..tag_end];
        if !tags.contains(&tag) {
            // some other fenced block (an example for the user): skip past it
            let mut k = i + 1;
            while k < lines.len() && lines[k].trim() != "```" {
                k += 1;
            }
            i = k + 1;
            continue;
        }
        let after = rest[tag_end..].trim();
        if let Some(one) = after.strip_suffix("```") {
            out.push(ToolBlock { tag: tag.into(), arg: one.trim().into(), body: String::new(),
                                 start: offs[i], end: offs[i] + lines[i].len() });
            i += 1;
            continue;
        }
        let mut k = i + 1;
        while k < lines.len() && lines[k].trim() != "```" {
            k += 1;
        }
        if k >= lines.len() {
            break; // still open (cut off): not a call
        }
        out.push(ToolBlock { tag: tag.into(), arg: after.into(), body: lines[i + 1..k].join("\n"),
                             start: offs[i], end: offs[k] + lines[k].len() });
        i = k + 1;
    }
    out
}

/// Blocks that open but never close (or otherwise weren't read as calls):
/// each as its opening line. Nothing in them ran, and the agent must hear so.
pub fn unclosed(text: &str, tags: &[&str]) -> Vec<String> {
    let mut spans: Vec<(usize, usize)> = file_blocks(text).iter().map(|b| (b.start, b.end)).collect();
    spans.extend(tool_blocks(text, tags).iter().map(|b| (b.start, b.end)));
    // fenced examples in prose (```python … ```) are skipped the way tool_blocks skips them
    let mut out = Vec::new();
    let lines: Vec<&str> = text.split('\n').collect();
    let mut pos = 0;
    let mut in_other = false;
    for l in &lines {
        let off = pos;
        pos += l.len() + 1;
        if spans.iter().any(|(a, b)| off >= *a && off <= *b) {
            continue;
        }
        let t = l.trim_start();
        let Some(rest) = t.strip_prefix("```") else { continue };
        let rest = rest.trim_start_matches('`');
        let tag_end = rest.find(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '-')).unwrap_or(rest.len());
        let tag = &rest[..tag_end];
        if in_other {
            if t.trim() == "```" {
                in_other = false;
            }
            continue;
        }
        if file_fence(l).is_some() || tags.contains(&tag) {
            out.push(l.trim().chars().take(80).collect());
        } else if !tag.is_empty() || t.trim() == "```" {
            in_other = !(t.trim().len() > 3 && t.trim().ends_with("```"));
        }
    }
    out
}

/// The reply with every tool and file block removed: what the user reads.
pub fn prose(text: &str, tags: &[&str]) -> String {
    let mut spans: Vec<(usize, usize)> = file_blocks(text).iter().map(|b| (b.start, b.end)).collect();
    spans.extend(tool_blocks(text, tags).iter().map(|b| (b.start, b.end)));
    spans.sort();
    let mut out = String::new();
    let mut pos = 0;
    for (a, b) in spans {
        if a >= pos {
            out.push_str(&text[pos..a]);
            pos = b.min(text.len());
        } else if b > pos {
            pos = b.min(text.len());
        }
    }
    if pos < text.len() {
        out.push_str(&text[pos..]);
    }
    static NL: OnceLock<regex::Regex> = OnceLock::new();
    NL.get_or_init(|| re(r"\n{3,}")).replace_all(out.trim(), "\n\n").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_readme_with_its_own_fences_is_one_file() {
        let t = "```write README.md\n# Demo\n\n```bash\ntouch PWNED\n```\n\nDone.\n```";
        let b = file_blocks(t);
        assert_eq!(b.len(), 1);
        assert!(b[0].body.contains("touch PWNED") && b[0].body.trim_end().ends_with("Done."));
        assert!(tool_blocks(t, &["bash"]).is_empty(), "the example inside the file never runs");
    }

    #[test]
    fn four_backticks_keep_bare_inner_fences() {
        let b = file_blocks("````write G.md\n# G\n```\nplain\n```\nend\n````");
        assert_eq!(b[0].body, "# G\n```\nplain\n```\nend\n");
    }

    #[test]
    fn an_ambiguous_doc_writes_nothing_and_hides_everything() {
        let t = "```write DOC.md\n# Doc\n```\nrm -rf x\n```\nmore\n```";
        let b = file_blocks(t);
        assert!(b[0].ambiguous && b[0].end == t.len());
    }

    #[test]
    fn a_page_with_a_stray_fence_after_it_is_written() {
        let t = "```write t.html\n<p>hi</p>\n```\n\nWritten.\n```";
        let b = file_blocks(t);
        assert!(!b[0].ambiguous && b[0].body == "<p>hi</p>\n");
        let e = file_blocks("```write t.html\n```\n<p>hi</p>\n```\nok\n```");
        assert!(e[0].ambiguous, "an empty body with more fenced text after it is never an empty file");
    }

    #[test]
    fn paths_as_meant() {
        for (h, want) in [(": a.html", "a.html"), ("\"a.html\"", "a.html"), ("`a.html`", "a.html"),
                          ("path=a.html", "a.html"), ("a.html html", "a.html"), ("./src/a.py", "src/a.py"),
                          ("my notes.txt", "my notes.txt")] {
            assert_eq!(clean_head(h), want, "{h}");
        }
    }

    #[test]
    fn edits_and_tools() {
        let t = "Fixing.\n```edit a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n```\n```test pytest -q```\n```bash\nls\n```";
        let f = file_blocks(t);
        assert_eq!(edit_pairs(&f[0].body), vec![("x = 1".into(), "x = 2".into())]);
        let tb = tool_blocks(t, &["test", "bash"]);
        assert_eq!(tb.len(), 2);
        assert_eq!((tb[0].tag.as_str(), tb[0].arg.as_str()), ("test", "pytest -q"));
        assert_eq!(tb[1].body, "ls");
        assert_eq!(prose(t, &["test", "bash"]), "Fixing.");
    }

    #[test]
    fn blocks_that_never_close_are_reported() {
        let t = "Fixing it.\n```edit app.py\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n";
        assert!(file_blocks(t).is_empty());
        assert_eq!(unclosed(t, &["bash"]), vec!["```edit app.py"]);
        let ok = "```bash\nls\n```\nAn example:\n```python\nprint(1)\n```\n```write a.txt\nhi\n```";
        assert!(unclosed(ok, &["bash"]).is_empty(), "{:?}", unclosed(ok, &["bash"]));
        assert_eq!(unclosed("```bash\nmake\n", &["bash"]), vec!["```bash"]);
    }

    #[test]
    fn a_patch_with_readme_fences_is_one_block() {
        let t = "```patch\n--- a/R.md\n+++ b/R.md\n@@ -1 +1,3 @@\n-x\n+```bash\n+y\n+```\n```";
        let b = file_blocks(t);
        assert_eq!(b.len(), 1);
        assert!(b[0].body.contains("+```bash"));
    }
}
