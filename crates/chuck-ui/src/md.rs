//! Just enough Markdown for an engineer's replies: paragraphs, headings,
//! lists, quotes, tables, **bold**, *italic*, `code`, links and fenced code
//! blocks.

/// A reply split into prose (as Pango markup) and code blocks.
#[derive(Debug, PartialEq)]
pub enum Part {
    Prose(String),
    Code { lang: String, text: String },
    /// A table: its header and rows, each cell as Pango markup.
    Table { head: Vec<String>, rows: Vec<Vec<String>> },
}

pub fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// Inline marks on one escaped line: `code` (nothing inside it is styled),
/// **bold** and *italic* — across code too (`**run `x` now**`) — and links.
fn inline(line: &str) -> String {
    // code spans step aside (as placeholders no mark or link can match) while the rest is styled
    let mut codes = Vec::new();
    let mut held = String::new();
    let mut rest = line;
    while let Some(a) = rest.find('`') {
        let Some(b) = rest[a + 1..].find('`') else { break };
        held.push_str(&rest[..a]);
        held.push_str(&format!("\u{E000}{}\u{E001}", codes.len()));
        codes.push(format!("<tt>{}</tt>", &rest[a + 1..a + 1 + b]));
        rest = &rest[a + 2 + b..];
    }
    held.push_str(rest);
    let mut out = emphasis(&held);
    for (i, c) in codes.iter().enumerate() {
        out = out.replacen(&format!("\u{E000}{i}\u{E001}"), c, 1);
    }
    out
}

/// A table row's cells (`| a | b |`): pipes inside `code` or escaped (`\|`) stay in their cell.
fn cells(line: &str) -> Vec<String> {
    let t = line.trim();
    let t = t.strip_prefix('|').unwrap_or(t);
    let t = t.strip_suffix('|').filter(|r| !r.ends_with('\\')).unwrap_or(t);
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut code = false;
    let mut chars = t.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\\' if chars.peek() == Some(&'|') => {
                cur.push('|');
                chars.next();
            }
            '`' => {
                code = !code;
                cur.push(c);
            }
            '|' if !code => out.push(std::mem::take(&mut cur).trim().to_string()),
            _ => cur.push(c),
        }
    }
    out.push(cur.trim().to_string());
    out
}

/// `|---|:---:|` — the line under a table's header.
fn table_rule(line: &str) -> bool {
    let c = cells(line);
    line.contains('-') && !c.is_empty() && c.iter().all(|x| {
        let x = x.trim_matches(':');
        !x.is_empty() && x.chars().all(|ch| ch == '-')
    })
}

fn emphasis(s: &str) -> String {
    use std::sync::OnceLock;
    static RX: OnceLock<(regex::Regex, regex::Regex)> = OnceLock::new();
    let (bold, italic) = RX.get_or_init(|| (
        // **x** with no space just inside the marks; *x* standing alone, not part of ** or a word
        regex::Regex::new(r"\*\*([^*\s](?:[^*]*?[^*\s])?)\*\*").unwrap(),
        regex::Regex::new(r"(^|[^*\w])\*([^*\s](?:[^*]*?[^*\s])?)\*($|[^*\w])").unwrap(),
    ));
    let b = bold.replace_all(s, "<b>$1</b>");
    links(&italic.replace_all(&b, "$1<i>$2</i>$3"))
}

/// `[text](https://…)` and bare https:// addresses, as links you can click.
/// (The line is already escaped, so an address's `&` is `&amp;` — as Pango wants it.)
fn links(s: &str) -> String {
    use std::sync::OnceLock;
    static RX: OnceLock<regex::Regex> = OnceLock::new();
    let rx = RX.get_or_init(|| regex::Regex::new(
        r#"\[([^\]]+)\]\((https?://[^\s)"]+)\)|(https?://[^\s<>()"]*[^\s<>()".,;:!?'])"#).unwrap());
    rx.replace_all(s, |c: &regex::Captures| match (c.get(1), c.get(2), c.get(3)) {
        (Some(t), Some(u), _) => format!("<a href=\"{}\">{}</a>", u.as_str(), t.as_str()),
        (_, _, Some(u)) => format!("<a href=\"{0}\">{0}</a>", u.as_str()),
        _ => c[0].to_string(),
    }).into_owned()
}

/// Parse a reply.
pub fn parse(text: &str) -> Vec<Part> {
    let mut parts = Vec::new();
    let mut prose: Vec<String> = Vec::new();
    let mut lines = text.lines().peekable();
    let flush = |prose: &mut Vec<String>, parts: &mut Vec<Part>| {
        // blank lines around go, a first bullet keeps its indent
        let joined = prose.join("\n").trim_end().trim_start_matches('\n').to_string();
        if !joined.trim().is_empty() {
            parts.push(Part::Prose(joined));
        }
        prose.clear();
    };
    while let Some(line) = lines.next() {
        let t = line.trim_start();
        // a table: a row of cells, then its rule line, then rows
        if t.starts_with('|') && lines.peek().is_some_and(|n| table_rule(n)) {
            flush(&mut prose, &mut parts);
            let head: Vec<String> = cells(line).iter().map(|c| inline(&escape(c))).collect();
            lines.next();
            let mut rows = Vec::new();
            while let Some(n) = lines.peek().filter(|n| n.trim_start().starts_with('|')) {
                let mut r: Vec<String> = cells(n).iter().map(|c| inline(&escape(c))).collect();
                r.resize(head.len().max(r.len()), String::new());
                rows.push(r);
                lines.next();
            }
            parts.push(Part::Table { head, rows });
            continue;
        }
        if let Some(lang) = t.strip_prefix("```") {
            flush(&mut prose, &mut parts);
            let mut code = Vec::new();
            for l in lines.by_ref() {
                if l.trim_start().starts_with("```") {
                    break;
                }
                code.push(l);
            }
            parts.push(Part::Code { lang: lang.trim().to_string(), text: code.join("\n") });
            continue;
        }
        let e = escape(line);
        let te = e.trim_start();
        let numbered = te.split_once(['.', ')']).filter(|(n, r)| !n.is_empty() && n.len() <= 3
            && n.chars().all(|c| c.is_ascii_digit()) && r.starts_with(' '));
        let rendered = if let Some(h) = ["#### ", "### ", "## ", "# "].iter().find_map(|p| te.strip_prefix(p)) {
            format!("<b>{}</b>", inline(h))
        } else if let Some(item) = te.strip_prefix("- ").or_else(|| te.strip_prefix("* ")).or_else(|| te.strip_prefix("+ ")) {
            let item = item.strip_prefix("[ ] ").map(|r| format!("☐ {r}"))
                .or_else(|| item.strip_prefix("[x] ").or_else(|| item.strip_prefix("[X] ")).map(|r| format!("☑ {r}")))
                .unwrap_or_else(|| item.to_string());
            // a nested item sits further in
            let depth = (e.len() - te.len()) / 2;
            format!("{}  •  {}", "    ".repeat(depth.min(4)), inline(&item))
        } else if let Some((n, r)) = numbered {
            format!("  {n}.  {}", inline(r.trim_start()))
        } else if let Some(q) = te.strip_prefix("&gt; ").or_else(|| (te == "&gt;").then_some("")) {
            format!("<span foreground=\"#9A97B0\">┃ <i>{}</i></span>", inline(q))
        } else if !te.is_empty() && te.chars().all(|c| c == '-' || c == '*' || c == '_') && te.len() >= 3 {
            "<span foreground=\"#3A3650\">────────────────────</span>".to_string()
        } else {
            inline(&e)
        };
        prose.push(rendered);
    }
    flush(&mut prose, &mut parts);
    parts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prose_and_code() {
        let p = parse("# Plan\nUse **bold** and `a<b>` here.\n- one\n\n```rust\nfn main() {}\n```\nafter");
        assert_eq!(p, vec![
            Part::Prose("<b>Plan</b>\nUse <b>bold</b> and <tt>a&lt;b&gt;</tt> here.\n  •  one".into()),
            Part::Code { lang: "rust".into(), text: "fn main() {}".into() },
            Part::Prose("after".into()),
        ]);
    }

    #[test]
    fn unbalanced_marks_stay_literal() {
        assert_eq!(parse("2 * 3 = 6"), vec![Part::Prose("2 * 3 = 6".into())]);
        assert_eq!(parse("a **b"), vec![Part::Prose("a **b".into())]);
        assert_eq!(parse("x <y> & z"), vec![Part::Prose("x &lt;y&gt; &amp; z".into())]);
        assert_eq!(parse("a *b* c **d** e"), vec![Part::Prose("a <i>b</i> c <b>d</b> e".into())]);
        assert_eq!(parse("glob *.rs and 3*4*5"), vec![Part::Prose("glob *.rs and 3*4*5".into())]);
    }

    #[test]
    fn bold_around_code_and_tables_render() {
        assert_eq!(parse("**If on 8GB, drop to `qwen3.5:4b`** (3.3GB)"),
                   vec![Part::Prose("<b>If on 8GB, drop to <tt>qwen3.5:4b</tt></b> (3.3GB)".into())]);
        assert_eq!(parse("- **`gemma4:e4b`** — April"), vec![Part::Prose("  •  <b><tt>gemma4:e4b</tt></b> — April".into())]);
        assert_eq!(parse("`**not bold**`"), vec![Part::Prose("<tt>**not bold**</tt>".into())]);
        let p = parse("Why it wins\n| | Qwen 9B | Gemma |\n|---|---|:---:|\n| Size | **9B** | `a|b` |\n| Ctx | 256K |\nafter");
        assert_eq!(p[0], Part::Prose("Why it wins".into()));
        assert_eq!(p[1], Part::Table { head: vec!["".into(), "Qwen 9B".into(), "Gemma".into()],
            rows: vec![vec!["Size".into(), "<b>9B</b>".into(), "<tt>a|b</tt>".into()],
                       vec!["Ctx".into(), "256K".into(), "".into()]] });
        assert_eq!(p[2], Part::Prose("after".into()));
        // a line with pipes but no rule under it is prose
        assert_eq!(parse("a | b"), vec![Part::Prose("a | b".into())]);
        assert_eq!(parse("1. one\n2) two\n> said\n---"), vec![Part::Prose("  1.  one\n  2.  two\n\
            <span foreground=\"#9A97B0\">┃ <i>said</i></span>\n<span foreground=\"#3A3650\">────────────────────</span>".into())]);
    }

    #[test]
    fn a_list_first_keeps_its_indent() {
        assert_eq!(parse("\n- one\n- two\n"), vec![Part::Prose("  •  one\n  •  two".into())]);
    }

    #[test]
    fn links_are_clickable() {
        assert_eq!(parse("see [the docs](https://docs.python.org/3/library/functions.html#int)."),
                   vec![Part::Prose("see <a href=\"https://docs.python.org/3/library/functions.html#int\">the docs</a>.".into())]);
        assert_eq!(parse("from https://a.org/x?q=1&r=2, and http://b.org."),
                   vec![Part::Prose("from <a href=\"https://a.org/x?q=1&amp;r=2\">https://a.org/x?q=1&amp;r=2</a>, and \
                                     <a href=\"http://b.org\">http://b.org</a>.".into())]);
        assert_eq!(parse("`https://in.code`"), vec![Part::Prose("<tt>https://in.code</tt>".into())]);
        assert_eq!(parse("[not a link](javascript:alert(1))"), vec![Part::Prose("[not a link](javascript:alert(1))".into())]);
    }
}
