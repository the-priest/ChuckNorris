//! Pictures and videos for Chat: found through SearXNG's image and video
//! categories (DuckDuckGo's image search as the fallback), and pictures
//! downloaded the careful way — public addresses only (every redirect
//! checked), at most 12 MB, and only when the bytes really are an image.

use crate::{html, join, normalize, Web, Why};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::path::{Path, PathBuf};

/// Nothing a chat bubble shows is bigger than this.
pub const MAX_IMAGE_BYTES: usize = 12_000_000;

/// A picture or a video found for a query.
#[derive(Clone, Debug, PartialEq)]
pub struct Media {
    pub title: String,
    /// The page it is on.
    pub page: String,
    /// The image itself, or the video's address.
    pub src: String,
    /// A small preview (an image's thumbnail, a video's poster).
    pub thumb: Option<String>,
    /// A video's length ("12:31").
    pub length: Option<String>,
}

fn abs(u: &str) -> Option<String> {
    let u = u.trim();
    if let Some(rest) = u.strip_prefix("//") {
        return Some(format!("https://{rest}"));
    }
    (u.starts_with("https://") || u.starts_with("http://")).then(|| u.to_string())
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(Value::as_str).map(html::strip).unwrap_or_default()
}

fn images_of(v: &Value, n: usize) -> Vec<Media> {
    v.get("results").and_then(Value::as_array).map(|rs| rs.iter().filter_map(|r| {
        let src = abs(r.get("img_src")?.as_str()?)?;
        Some(Media { title: s(r, "title"), page: r.get("url").and_then(Value::as_str).and_then(abs).unwrap_or_else(|| src.clone()),
                     thumb: r.get("thumbnail_src").and_then(Value::as_str).and_then(abs), src, length: None })
    }).take(n * 2).collect()).unwrap_or_default()
}

fn videos_of(v: &Value, n: usize) -> Vec<Media> {
    v.get("results").and_then(Value::as_array).map(|rs| rs.iter().filter_map(|r| {
        let url = abs(r.get("url")?.as_str()?)?;
        let length = match r.get("length") {
            Some(Value::String(l)) if !l.trim().is_empty() => Some(l.trim().to_string()),
            Some(Value::Number(secs)) => secs.as_f64().map(|t| format!("{}:{:02}", t as u64 / 60, t as u64 % 60)),
            _ => None,
        };
        Some(Media { title: s(r, "title"), page: url.clone(), src: url,
                     thumb: r.get("thumbnail").and_then(Value::as_str).and_then(abs), length })
    }).take(n * 2).collect()).unwrap_or_default()
}

/// At most `per_site` from one site, no repeats, `n` in all.
fn spread(pool: Vec<Media>, n: usize, per_site: usize) -> Vec<Media> {
    let mut seen = HashSet::new();
    let mut per: HashMap<String, usize> = HashMap::new();
    let mut out = Vec::new();
    for m in pool {
        if !seen.insert(m.src.clone()) {
            continue;
        }
        let c = per.entry(crate::domain(&m.page)).or_default();
        if *c >= per_site {
            continue;
        }
        *c += 1;
        out.push(m);
        if out.len() >= n {
            break;
        }
    }
    out
}

/// What the first bytes say a picture is (None: not a picture shown here).
pub fn image_kind(b: &[u8]) -> Option<&'static str> {
    if b.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("png")
    } else if b.starts_with(&[0xff, 0xd8, 0xff]) {
        Some("jpg")
    } else if b.starts_with(b"GIF87a") || b.starts_with(b"GIF89a") {
        Some("gif")
    } else if b.len() > 12 && &b[..4] == b"RIFF" && &b[8..12] == b"WEBP" {
        Some("webp")
    } else if b.len() > 12 && &b[4..8] == b"ftyp" && (&b[8..12] == b"avif" || &b[8..12] == b"avis") {
        Some("avif")
    } else if b.starts_with(b"BM") && b.len() > 26 {
        Some("bmp")
    } else {
        None
    }
}

impl Web {
    /// Up to `n` pictures for `query`, at most three from one site.
    pub fn images(&self, query: &str, n: usize) -> Result<Vec<Media>, String> {
        let query = query.trim();
        if query.is_empty() {
            return Err("no query given".into());
        }
        let mut pool = self.searx_with(query, n + 6, "images", images_of);
        if pool.len() < n {
            pool.extend(self.ddg_images(query, n + 6));
        }
        let out = spread(pool, n, 3);
        if out.is_empty() {
            return Err("no pictures came back (offline, every engine refused, or nothing matched)".into());
        }
        Ok(out)
    }

    /// Up to `n` videos for `query`, at most two from one channel or site.
    pub fn videos(&self, query: &str, n: usize) -> Result<Vec<Media>, String> {
        let query = query.trim();
        if query.is_empty() {
            return Err("no query given".into());
        }
        let mut pool = self.searx_with(query, n + 6, "videos", videos_of);
        if pool.is_empty() {
            // no video engine answered: the ordinary search, kept to video sites
            let hosts = ["youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "peertube", "odysee.com", "rumble.com"];
            pool = self.search(&format!("{query} video"), n + 6).unwrap_or_default().into_iter()
                .filter(|h| hosts.iter().any(|d| crate::domain(&h.url).contains(d)))
                .map(|h| Media { title: h.title, page: h.url.clone(), src: h.url, thumb: None, length: None }).collect();
        }
        let out = spread(pool, n, 2);
        if out.is_empty() {
            return Err("no videos came back (offline, every engine refused, or nothing matched)".into());
        }
        Ok(out)
    }

    fn ddg_images(&self, query: &str, n: usize) -> Vec<Media> {
        let get = |url: &str, referer: bool| -> Option<String> {
            let mut r = self.agent.get(url);
            if referer {
                r = r.header("Referer", "https://duckduckgo.com/");
            }
            let mut resp = r.call().ok()?;
            if resp.status().as_u16() != 200 {
                return None;
            }
            let mut b = Vec::new();
            resp.body_mut().as_reader().take(4_000_000).read_to_end(&mut b).ok()?;
            Some(String::from_utf8_lossy(&b).into_owned())
        };
        let Some(page) = get(&format!("https://duckduckgo.com/?q={}&iax=images&ia=images", crate::enc(query)), false) else {
            return Vec::new();
        };
        static VQD: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        let Some(vqd) = VQD.get_or_init(|| regex::Regex::new(r#"vqd=["']?([\d-]+)["']?"#).unwrap()).captures(&page)
            .map(|c| c[1].to_string()) else { return Vec::new() };
        let Some(js) = get(&format!("https://duckduckgo.com/i.js?l=us-en&o=json&q={}&vqd={vqd}&f=,,,&p=1", crate::enc(query)), true)
        else { return Vec::new() };
        let Ok(v) = serde_json::from_str::<Value>(&js) else { return Vec::new() };
        v.get("results").and_then(Value::as_array).map(|rs| rs.iter().filter_map(|r| {
            let src = abs(r.get("image")?.as_str()?)?;
            Some(Media { title: s(r, "title"), page: r.get("url").and_then(Value::as_str).and_then(abs).unwrap_or_else(|| src.clone()),
                         thumb: r.get("thumbnail").and_then(Value::as_str).and_then(abs), src, length: None })
        }).take(n).collect()).unwrap_or_default()
    }

    /// Download a picture into `dir` (named after its address, so a second
    /// download of the same one is free). Public addresses only, at most
    /// 12 MB, and only if the bytes are an image (PNG, JPEG, GIF, WebP,
    /// AVIF, BMP — never SVG, which can carry scripts).
    pub fn download_image(&self, url: &str, dir: &Path) -> Result<PathBuf, String> {
        let mut url = normalize(url).map_err(|w| w.to_string())?;
        // judged before the copy on disk too: what may not be read isn't served from there either
        self.address_ok(&url).map_err(|w| w.to_string())?;
        let key = {
            use std::hash::{Hash, Hasher};
            let mut h = std::collections::hash_map::DefaultHasher::new();
            url.hash(&mut h);
            format!("{:016x}", h.finish())
        };
        if let Some(done) = ["png", "jpg", "gif", "webp", "avif", "bmp"].iter().map(|e| dir.join(format!("{key}.{e}")))
            .find(|p| p.is_file())
        {
            return Ok(done);
        }
        for _ in 0..=6 {
            self.address_ok(&url).map_err(|w| w.to_string())?;
            let mut resp = self.agent.get(&url).header("Accept", "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8")
                .header("Referer", &format!("https://{}/", crate::domain(&url))).call().map_err(|e| e.to_string())?;
            let st = resp.status().as_u16();
            if (300..400).contains(&st) {
                let loc = resp.headers().get("location").and_then(|v| v.to_str().ok()).ok_or("a redirect to nowhere")?;
                url = normalize(&join(&url, loc)).map_err(|w: Why| w.to_string())?;
                continue;
            }
            if st != 200 {
                return Err(format!("HTTP {st}"));
            }
            let mut body = Vec::new();
            resp.body_mut().as_reader().take(MAX_IMAGE_BYTES as u64 + 1).read_to_end(&mut body).map_err(|e| e.to_string())?;
            if body.len() > MAX_IMAGE_BYTES {
                return Err("bigger than 12 MB".into());
            }
            let kind = image_kind(&body).ok_or("not a picture (or a kind that isn't shown)")?;
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
            let p = dir.join(format!("{key}.{kind}"));
            let tmp = dir.join(format!(".{key}.part"));
            std::fs::write(&tmp, &body).and_then(|_| std::fs::rename(&tmp, &p)).map_err(|e| e.to_string())?;
            return Ok(p);
        }
        Err("too many redirects".into())
    }
}

/// Keep the newest `keep` files in `dir` (downloaded pictures pile up).
pub fn prune(dir: &Path, keep: usize) {
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    let mut files: Vec<(std::time::SystemTime, PathBuf)> = rd.flatten()
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path()))).collect();
    if files.len() <= keep {
        return;
    }
    files.sort();
    for (_, p) in &files[..files.len() - keep] {
        let _ = std::fs::remove_file(p);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn answers_become_media() {
        let v = json!({"results": [
            {"url": "https://a.org/p", "title": "<b>Cat</b>", "img_src": "//img.a.org/cat.jpg", "thumbnail_src": "https://t/cat.jpg"},
            {"url": "https://a.org/q", "title": "no image"},
            {"url": "https://b.org/v", "title": "A video", "thumbnail": "https://t/v.jpg", "length": 754.0},
            {"url": "javascript:alert(1)", "title": "bad", "img_src": "javascript:alert(1)"},
        ]});
        let im = images_of(&v, 5);
        assert_eq!(im.len(), 1);
        assert_eq!((im[0].title.as_str(), im[0].src.as_str()), ("Cat", "https://img.a.org/cat.jpg"));
        let vi = videos_of(&v, 5);
        assert_eq!(vi.iter().find(|m| m.title == "A video").unwrap().length.as_deref(), Some("12:34"));
        assert!(vi.iter().all(|m| m.src.starts_with("http")));
    }

    #[test]
    fn kinds_from_bytes() {
        assert_eq!(image_kind(b"\x89PNG\r\n\x1a\nrest"), Some("png"));
        assert_eq!(image_kind(&[0xff, 0xd8, 0xff, 0xe0]), Some("jpg"));
        assert_eq!(image_kind(b"RIFF\0\0\0\0WEBPVP8 "), Some("webp"));
        assert_eq!(image_kind(b"<svg xmlns=..."), None);
        assert_eq!(image_kind(b"<html>"), None);
    }

    #[test]
    fn spread_over_sites() {
        let m = |p: &str, s: &str| Media { title: String::new(), page: p.into(), src: s.into(), thumb: None, length: None };
        let out = spread(vec![m("https://a.org/1", "x1"), m("https://a.org/2", "x2"), m("https://a.org/3", "x3"),
                              m("https://b.org/1", "x1"), m("https://b.org/2", "y")], 10, 2);
        assert_eq!(out.iter().map(|m| m.src.as_str()).collect::<Vec<_>>(), vec!["x1", "x2", "y"]);
    }
}
