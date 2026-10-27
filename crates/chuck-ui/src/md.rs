//! Just enough Markdown for an engineer's replies: paragraphs, headings,
//! lists, **bold**, *italic*, `code`, links and fenced code blocks.

/// A reply split into prose (as Pango markup) and code blocks.
#[derive(Debug, PartialEq)]
pub enum Part {
    Prose(String),
    Code { lang: String, text: String },
}

pub fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

/// Inline marks on one escaped line: `code` first (nothing inside it is
/// styled), then **bold** and *italic*.
fn inline(line: &str) -> String {
    let mut out = String::new();
    let mut rest = line;
    while let Some(a) = rest.find('`') {
        let Some(b) = rest[a + 1..].find('`') else { break };
        out.push_str(&emphasis(&rest[..a]));
        out.push_str(&format!("<tt>{}</tt>", &rest[a + 1..a + 1 + b]));
        rest = &rest[a + 2 + b..];
    }
    out.push_str(&emphasis(rest));
    out
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
        let joined = prose.join("\n").trim().to_string();
        if !joined.is_empty() {
            parts.push(Part::Prose(joined));
        }
        prose.clear();
    };
    while let Some(line) = lines.next() {
        let t = line.trim_start();
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
        let rendered = if let Some(h) = te.strip_prefix("### ").or_else(|| te.strip_prefix("## "))
            .or_else(|| te.strip_prefix("# ")) {
            format!("<b>{}</b>", inline(h))
        } else if let Some(item) = te.strip_prefix("- ").or_else(|| te.strip_prefix("* ")) {
            format!("  •  {}", inline(item))
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
