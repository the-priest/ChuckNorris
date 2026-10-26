//! Just enough Markdown for an engineer's replies: paragraphs, headings,
//! lists, **bold**, *italic*, `code` and fenced code blocks.

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
    italic.replace_all(&b, "$1<i>$2</i>$3").into_owned()
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
}
