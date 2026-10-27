//! A reply, made worth listening to: no code, tables or markup, and what
//! people write but never say spelled out — then split into sentence-sized
//! chunks so speech starts at once and nothing is ever cut off. A faithful
//! port of the Python app's tts_clean / tts_chunks (tests/golden.json holds
//! its answers).

use fancy_regex::{Captures, Regex};
use std::sync::OnceLock;

fn re(p: &str) -> Regex {
    Regex::new(p).expect("valid regex")
}

macro_rules! rx {
    ($p:expr) => {{
        static R: OnceLock<Regex> = OnceLock::new();
        R.get_or_init(|| re($p))
    }};
}

fn sub(r: &Regex, s: &str, rep: &str) -> String {
    r.replace_all(s, rep).into_owned()
}

fn sub_with(r: &Regex, s: &str, f: impl Fn(&Captures<'_, str>) -> String) -> String {
    r.replace_all(s, |c: &Captures<'_, str>| f(c)).into_owned()
}

/// Short inline code is part of the sentence ("run `make test` first"):
/// keep its words, drop the symbols nobody says aloud.
fn say_code(code: &str) -> String {
    let code = code.trim();
    if code.is_empty() {
        return " ".into();
    }
    if code.chars().count() > 32 || code.contains('\n') {
        let cmd = rx!(r"^(?:sudo |pip|npm|git|apt|pacman|cargo|python|make|docker|curl|cd )");
        return if cmd.is_match(code).unwrap_or(false) { " the command shown ".into() } else { " the code shown ".into() };
    }
    let code = if code.contains('/') && !code.contains(' ') { sub(rx!(r"^.*/"), code, "") } else { code.to_string() };
    format!(" {} ", sub(rx!(r"[-_=<>{}()\[\];$`\\|]+"), &code, " ").trim())
}

const ABBR: &[(&str, &str)] = &[
    (r"(?i)\be\.g\.", "for example"), (r"(?i)\bi\.e\.", "that is"),
    (r"(?i)\betc\.(?=\s+[A-Z]|\s*$)", "et cetera."), (r"(?i)\betc\.", "et cetera"),
    (r"(?i)\bvs\.?(?=\s)", "versus"), (r"(?i)\bapprox\.", "approximately"), (r"(?i)\bw/o\b", "without"),
    (r"(?i)\bw/(?=\s)", "with"), (r"(?i)\bfyi\b", "for your information"), (r"(?i)\bimo\b", "in my opinion"),
    (r"(?i)\btl;dr\b", "in short"),
];

fn abbr() -> &'static [(Regex, &'static str)] {
    static A: OnceLock<Vec<(Regex, &'static str)>> = OnceLock::new();
    A.get_or_init(|| ABBR.iter().map(|(p, r)| (re(p), *r)).collect())
}

/// Turn a chat reply into something worth listening to.
pub fn clean(text: &str) -> String {
    // code blocks: a fence closes only with one as long (```` holds ```), and
    // one left open (a reply cut short) runs to the end
    let mut s = sub(rx!(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?(?:^[ \t]*\1[ \t]*$|\z)"), text, " ");
    s = sub(rx!(r"(?s)```.*?```"), &s, " ");
    s = sub_with(rx!(r"`([^`]*)`"), &s, |c| say_code(&c[1]));
    s = sub(rx!(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$"), &s, "");
    s = sub(rx!(r"(?m)^\s*\|.*\|\s*$"), &s, "");
    s = sub(rx!(r"(?m)^[ \t]*>+[ \t]?"), &s, "");
    s = sub(rx!(r"<((?:https?|ftp)://[^>\s]+)>"), &s, "$1");
    s = sub(rx!(r"!\[[^\]]*\]\([^)]*\)"), &s, " ");
    s = sub(rx!(r"\[([^\]]+)\]\([^)]*\)"), &s, "$1");
    s = sub_with(rx!(r"https?://\S+|www\.\S+"), &s, |c| {
        let tail = rx!(r"[.,;:!?)\]]*$").find(&c[0]).ok().flatten().map(|m| m.as_str().to_string()).unwrap_or_default();
        format!(" link{tail} ")
    });
    s = sub_with(rx!(r"(?<![\w.])(?:~|\.{1,2})?(?:/[\w.+-]+){2,}/?"), &s, |c| {
        format!(" {} ", c[0].trim_end_matches('/').rsplit('/').next().unwrap_or(""))
    });
    // headings and bullets end in a pause, not run into the next line
    s = sub_with(rx!(r"(?m)^\s*#{1,6}\s*(.+?)\s*$"), &s, |c| format!("{}.", c[1].trim_end_matches(['.', ':'])));
    s = sub_with(rx!(r"(?m)^\s*(?:[-*+]|\d{1,3}[.)])\s+(.+?)\s*$"), &s, |c| {
        let g = &c[1];
        format!("{g}{}", if g.ends_with(['.', '!', '?', ':', ';']) { "" } else { "." })
    });
    for (r, rep) in abbr() {
        s = sub(r, &s, rep);
    }
    s = sub(rx!(r"\s*(?:->|=>|→)\s*"), &s, ", then ");
    s = sub(rx!(r"\s+&\s+"), &s, " and ");
    s = sub(rx!(r"(?<=\s)~(?=\d)"), &s, "about ");
    s = sub(rx!(r"\bv(\d+(?:\.\d+)+)\b"), &s, "version $1");
    s = sub(rx!("[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\u{200D}\u{2190}-\u{21FF}\u{2300}-\u{23FF}\u{2B00}-\u{2BFF}\u{2713}\u{2714}\u{2717}\u{2718}]"), &s, " ");
    s = sub(rx!(r"(?<=[\w)])[ \t]+>=[ \t]+(?=[\w(])"), &s, " at least ");
    s = sub(rx!(r"(?<=[\w)])[ \t]+<=[ \t]+(?=[\w(])"), &s, " at most ");
    s = sub(rx!(r"(?<=[\w)])[ \t]+>[ \t]+(?=[\w(])"), &s, " greater than ");
    s = sub(rx!(r"(?<=[\w)])[ \t]+<[ \t]+(?=[\w(])"), &s, " less than ");
    s = sub(rx!(r"(?<=\d)\s*[*×]\s*(?=\d)"), &s, " times ");
    s = sub(rx!(r"\b([CF])#"), &s, "$1 sharp");
    s = sub(rx!(r"(\*\*|__|~~)(.+?)\1"), &s, "$2");
    s = sub(rx!(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)"), &s, "$1");
    s = sub(rx!(r"(?m)^\s*>+\s?"), &s, "");
    s = s.replace('|', " ");
    s = sub(rx!(r"(?<!\w)[*#~](?!\w)"), &s, " ");
    s = sub(rx!(r"\s+([.,!?;:)])"), &s, "$1");
    s = sub(rx!(r"\(\s+"), &s, "(");
    s = sub(rx!(r"[ \t]+"), &s, " ");
    s = sub(rx!(r"[ \t]*\n\s*"), &s, "\n");
    s.trim().to_string()
}

/// "Dr. Johnson", "the U.S. economy": not sentence ends.
fn abbrev_end(s: &str) -> bool {
    rx!(r"(?:\b(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|Inc|Ltd|Co|No|Fig|approx|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.|\b(?:[A-Z]\.){1,3})$")
        .is_match(s).unwrap_or(false)
}

fn char_len(s: &str) -> usize {
    s.chars().count()
}

/// The first `n` characters, and the rest.
fn split_chars(s: &str, n: usize) -> (&str, &str) {
    let i = s.char_indices().nth(n).map_or(s.len(), |(i, _)| i);
    s.split_at(i)
}

/// Speakable chunks of at most `size` characters, on sentence boundaries
/// where possible. `first` caps the first chunk smaller, so speech starts
/// sooner.
pub fn chunks(text: &str, size: usize, first: Option<usize>) -> Vec<String> {
    let text = text.trim();
    if text.is_empty() {
        return Vec::new();
    }
    let mut parts: Vec<String> = Vec::new();
    let mut last = 0;
    for m in rx!(r"(?<=[.!?:;])\s+|\n+").find_iter(text).flatten() {
        parts.push(text[last..m.start()].to_string());
        last = m.end();
    }
    parts.push(text[last..].to_string());
    let mut merged: Vec<String> = Vec::new();
    for p in parts {
        match merged.last_mut() {
            Some(prev) if abbrev_end(prev) => {
                prev.push(' ');
                prev.push_str(&p);
            }
            _ => merged.push(p),
        }
    }
    let mut out: Vec<String> = Vec::new();
    let mut cur = String::new();
    for p in merged {
        let mut p = p.trim().to_string();
        if p.is_empty() {
            continue;
        }
        while char_len(&p) > size {
            // a monster sentence: wrap on a space
            let (head, _) = split_chars(&p, size);
            let cut = head.rfind(' ').filter(|&i| i > 0).map(|i| char_len(&p[..i])).unwrap_or(size);
            if !cur.is_empty() {
                out.push(cur.trim().to_string());
                cur.clear();
            }
            let (a, b) = split_chars(&p, cut);
            out.push(a.trim().to_string());
            p = b.trim().to_string();
        }
        if char_len(&cur) + char_len(&p) < size {
            cur = format!("{cur} {p}").trim().to_string();
        } else {
            if !cur.is_empty() {
                out.push(cur.trim().to_string());
            }
            cur = p;
        }
    }
    if !cur.is_empty() {
        out.push(cur.trim().to_string());
    }
    out.retain(|c| !c.is_empty());
    if let (Some(f), Some(head)) = (first, out.first().cloned()) {
        if char_len(&head) > f {
            let h = chunks(&head, f, None);
            let mut v = vec![h[0].clone()];
            if h.len() > 1 {
                v.push(h[1..].join(" "));
            }
            v.extend(out.into_iter().skip(1));
            out = v;
        }
    }
    out
}
