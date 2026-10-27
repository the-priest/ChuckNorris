//! A page's readable text: scripts, navigation and the like dropped in one
//! pass, the article (when there is one) preferred over the whole page,
//! paragraph breaks kept, entities decoded.

use regex::Regex;
use std::collections::HashMap;
use std::sync::OnceLock;

fn re(p: &str) -> Regex {
    Regex::new(p).expect("valid regex")
}

const DROP_TAGS: &[&str] = &["head", "title", "template", "script", "style", "nav", "footer", "header", "form", "aside", "noscript", "svg"];

/// Remove `<script>…</script>`, `<nav>…</nav>` and the like in ONE pass.
/// Each tag's closing positions are found once, so a broken page of
/// thousands of unclosed `<nav>`s costs a scan, not a scan per tag.
pub fn drop_tags(raw: &str) -> String {
    static OPEN: OnceLock<Regex> = OnceLock::new();
    let open = OPEN.get_or_init(|| re(&format!(r"(?i)<({})\b[^>]*>", DROP_TAGS.join("|"))));
    let low = raw.to_ascii_lowercase();
    let mut closes: HashMap<String, Vec<usize>> = HashMap::new();
    let mut out = String::with_capacity(raw.len());
    let mut pos = 0;
    for m in open.captures_iter(raw) {
        let whole = m.get(0).unwrap();
        if whole.start() < pos {
            continue; // inside a block already dropped
        }
        out.push_str(&raw[pos..whole.start()]);
        let tag = m[1].to_ascii_lowercase();
        let list = closes.entry(tag.clone()).or_insert_with(|| {
            let needle = format!("</{tag}");
            low.match_indices(&needle).map(|(i, _)| i).collect()
        });
        let k = list.partition_point(|&i| i < whole.end());
        match list.get(k) {
            None => pos = whole.end(), // never closed: drop just the tag
            Some(&end) => {
                pos = low[end..].find('>').map_or(raw.len(), |c| end + c + 1);
                out.push(' ');
            }
        }
    }
    out.push_str(&raw[pos..]);
    out
}

/// Decode `&amp;`, `&#39;`, `&#x2014;` and the common named entities.
pub fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    static ENT: OnceLock<Regex> = OnceLock::new();
    let ent = ENT.get_or_init(|| re(r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[a-zA-Z][a-zA-Z0-9]{1,31});"));
    ent.replace_all(s, |c: &regex::Captures| {
        let name = &c[1];
        let ch = if let Some(hex) = name.strip_prefix("#x").or_else(|| name.strip_prefix("#X")) {
            u32::from_str_radix(hex, 16).ok().and_then(char::from_u32)
        } else if let Some(dec) = name.strip_prefix('#') {
            dec.parse().ok().and_then(char::from_u32)
        } else {
            named(name)
        };
        ch.map_or_else(|| c[0].to_string(), |ch| ch.to_string())
    }).into_owned()
}

fn named(n: &str) -> Option<char> {
    Some(match n {
        "amp" => '&', "lt" => '<', "gt" => '>', "quot" => '"', "apos" => '\'', "nbsp" => ' ',
        "ndash" => '–', "mdash" => '—', "hellip" => '…', "lsquo" => '‘', "rsquo" => '’',
        "ldquo" => '“', "rdquo" => '”', "laquo" => '«', "raquo" => '»', "bull" => '•', "middot" => '·',
        "copy" => '©', "reg" => '®', "trade" => '™', "deg" => '°', "times" => '×', "divide" => '÷',
        "plusmn" => '±', "para" => '¶', "sect" => '§', "euro" => '€', "pound" => '£', "yen" => '¥',
        "cent" => '¢', "larr" => '←', "rarr" => '→', "uarr" => '↑', "darr" => '↓', "le" => '≤',
        "ge" => '≥', "ne" => '≠', "minus" => '−', "shy" => '\u{ad}', "zwj" => '\u{200d}',
        "zwnj" => '\u{200c}', "thinsp" => '\u{2009}', "ensp" => '\u{2002}', "emsp" => '\u{2003}',
        "eacute" => 'é', "egrave" => 'è', "aacute" => 'á', "agrave" => 'à', "ouml" => 'ö',
        "uuml" => 'ü', "auml" => 'ä', "szlig" => 'ß', "ntilde" => 'ñ', "ccedil" => 'ç',
        "iexcl" => '¡', "iquest" => '¿', "frac12" => '½', "frac14" => '¼', "frac34" => '¾',
        _ => return None,
    })
}

/// Tags out, entities decoded, trimmed.
pub fn strip(s: &str) -> String {
    static TAG: OnceLock<Regex> = OnceLock::new();
    unescape(&TAG.get_or_init(|| re(r"<[^>]+>")).replace_all(s, "")).trim().to_string()
}

/// The longest `<article>` or `<main>` that holds real text, else the page.
/// A page of teaser cards uses `<article>` for every card; the first card
/// is not the page.
fn main_part(raw: &str) -> &str {
    static ART: OnceLock<[Regex; 2]> = OnceLock::new();
    let rx = ART.get_or_init(|| [re(r"(?is)<article\b[^>]*>(.*?)</article>"), re(r"(?is)<main\b[^>]*>(.*?)</main>")]);
    let mut best = "";
    for r in rx {
        for c in r.captures_iter(raw) {
            let inner = c.get(1).unwrap().as_str();
            if inner.len() > best.len() {
                best = inner;
            }
        }
    }
    if !best.is_empty() && strip(best).chars().count() >= 500 { best } else { raw }
}

/// The readable text of a page, at most `limit` characters.
pub fn readable(raw: &str, limit: usize) -> String {
    static CELL: OnceLock<Regex> = OnceLock::new();
    static BLOCK: OnceLock<Regex> = OnceLock::new();
    static SPACES: OnceLock<Regex> = OnceLock::new();
    static GAPS: OnceLock<Regex> = OnceLock::new();
    let raw = cut(raw, crate::MAX_PAGE_BYTES);
    let raw = drop_tags(raw);
    let body = main_part(&raw);
    let body = CELL.get_or_init(|| re(r"(?i)</t[dh]>")).replace_all(body, " | ");
    let body = BLOCK.get_or_init(|| re(r"(?is)<br\s*/?>|</(?:p|div|li|h[1-6]|tr|section|article|dt|dd|ul|ol|table|pre|blockquote)>"))
        .replace_all(&body, "\n");
    let text = strip(&body);
    let text = SPACES.get_or_init(|| re(r"[ \t\u{a0}]+")).replace_all(&text, " ");
    let text = GAPS.get_or_init(|| re(r"\n[ \t]*(?:\n[ \t]*)+")).replace_all(&text, "\n\n");
    let text: String = text.lines().map(str::trim).collect::<Vec<_>>().join("\n");
    let text = text.trim();
    text.chars().take(limit).collect()
}

/// The page's `<title>`, if it has one.
pub fn title(raw: &str) -> Option<String> {
    static T: OnceLock<Regex> = OnceLock::new();
    T.get_or_init(|| re(r"(?is)<title[^>]*>(.*?)</title>")).captures(cut(raw, 200_000))
        .map(|c| strip(&c[1]).split_whitespace().collect::<Vec<_>>().join(" "))
        .filter(|t| !t.is_empty())
}

/// At most `n` bytes, on a character boundary.
pub fn cut(s: &str, n: usize) -> &str {
    if s.len() <= n {
        return s;
    }
    let mut k = n;
    while !s.is_char_boundary(k) {
        k -= 1;
    }
    &s[..k]
}

/// Bytes to text in the page's own charset: the Content-Type header, else a
/// `<meta charset>` near the top, else UTF-8 (windows-1252 when that fails).
pub fn decode(body: &[u8], content_type: &str) -> String {
    // a page's NULs aren't text (and nothing past here could show them)
    let t = decode_raw(body, content_type);
    if t.contains('\0') { t.replace('\0', "") } else { t }
}

fn decode_raw(body: &[u8], content_type: &str) -> String {
    static CS: OnceLock<Regex> = OnceLock::new();
    static META: OnceLock<regex::bytes::Regex> = OnceLock::new();
    let cs = CS.get_or_init(|| re(r#"(?i)charset=["']?([\w.:-]+)"#));
    let mut label = cs.captures(content_type).map(|c| c[1].to_string());
    if label.is_none() {
        let head = &body[..body.len().min(4096)];
        label = META.get_or_init(|| regex::bytes::Regex::new(r#"(?i)<meta[^>]+charset=["']?([\w.:-]+)"#).unwrap())
            .captures(head).map(|c| String::from_utf8_lossy(&c[1]).into_owned());
    }
    if let Some(enc) = label.and_then(|l| encoding_rs::Encoding::for_label(l.as_bytes())) {
        let (text, _, bad) = enc.decode(body);
        if !bad || enc != encoding_rs::UTF_8 {
            return text.into_owned();
        }
    }
    match std::str::from_utf8(body) {
        Ok(s) => s.trim_start_matches('\u{feff}').to_string(),
        Err(_) => encoding_rs::WINDOWS_1252.decode(body).0.into_owned(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scripts_and_navigation_go_paragraphs_stay() {
        let page = "<html><head><title>T &amp; U</title><style>p{color:red}</style></head><body>\
            <nav><a href=/>Home</a> <a href=/x>Menu</a></nav>\
            <p>First   paragraph.</p><script>var x = '<p>not text</p>';</script>\
            <p>Second &mdash; with &#39;quotes&#39; and &#x2192;.</p>\
            <table><tr><td>a</td><td>b</td></tr></table><footer>© footer</footer></body></html>";
        let t = readable(page, 10_000);
        assert_eq!(t, "First paragraph.\nSecond — with 'quotes' and →.\na | b |");
        assert_eq!(title(page).as_deref(), Some("T & U"));
    }

    #[test]
    fn the_longest_real_article_wins() {
        let story = "word ".repeat(150);
        let page = format!("<article>teaser one</article><div>sidebar junk</div>\
            <article><p>{story}</p></article><article>teaser two</article>");
        let t = readable(&page, 100_000);
        assert!(t.starts_with("word word"), "{t}");
        assert!(!t.contains("sidebar") && !t.contains("teaser"));
        // a page whose only <article>s are teaser cards: the whole page is read
        let t = readable("<article>card</article><p>the real body</p>", 1000);
        assert!(t.contains("the real body") && t.contains("card"));
    }

    #[test]
    fn unclosed_tags_are_cheap_and_dropped() {
        let page = format!("<p>keep</p>{}<p>also kept</p>", "<nav>".repeat(30_000));
        let t0 = std::time::Instant::now();
        let t = readable(&page, 1000);
        assert!(t0.elapsed().as_secs_f64() < 2.0, "took {:?}", t0.elapsed());
        assert_eq!(t, "keep\nalso kept");
    }

    #[test]
    fn charsets() {
        assert_eq!(decode(b"caf\xe9", "text/html; charset=ISO-8859-1"), "café");
        assert_eq!(decode(b"<meta charset=\"windows-1252\">\x93hi\x94", "text/html"), "<meta charset=\"windows-1252\">“hi”");
        assert_eq!(decode("naïve".as_bytes(), ""), "naïve");
        assert_eq!(decode(b"caf\xe9", ""), "café", "not UTF-8: windows-1252");
        assert_eq!(decode("ok".as_bytes(), "text/html; charset=nonsense"), "ok");
    }

    #[test]
    fn entities() {
        assert_eq!(unescape("a &lt;b&gt; &amp;amp; &#8212; &unknown; &#xZZ;"), "a <b> &amp; — &unknown; &#xZZ;");
        assert_eq!(strip("<b>bold</b> &nbsp;x "), "bold  x");
    }
}
