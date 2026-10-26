//! settings.json — the same file the Python app reads and writes
//! (~/.config/chucknorris/settings.json), so both front ends share one key
//! and one set of choices. Unknown keys are kept as they are.

use serde_json::{Map, Value};
use std::io::Write;
use std::path::{Path, PathBuf};

#[derive(Debug, thiserror::Error)]
pub enum SettingsError {
    #[error("can't write {0}: {1}")]
    Write(PathBuf, std::io::Error),
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Settings {
    pub map: Map<String, Value>,
    /// Where it was read from (and is saved to).
    pub path: PathBuf,
    /// Set when the file existed but couldn't be read: a copy was kept.
    pub problem: Option<String>,
}

pub fn config_dir() -> PathBuf {
    home().join(".config").join("chucknorris")
}

fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// JSON with the hand-edit mistake people actually make (a trailing comma)
/// forgiven.
fn parse(raw: &str) -> Option<Map<String, Value>> {
    let v: Option<Value> = serde_json::from_str(raw).ok().or_else(|| {
        let fixed = regex_lite_trailing_commas(raw);
        serde_json::from_str(&fixed).ok()
    });
    match v {
        Some(Value::Object(m)) => Some(m),
        _ => None,
    }
}

/// `,` followed only by whitespace and then `}` or `]`, outside strings.
fn regex_lite_trailing_commas(raw: &str) -> String {
    let b: Vec<char> = raw.chars().collect();
    let mut out = String::with_capacity(raw.len());
    let (mut in_str, mut esc) = (false, false);
    for (i, &c) in b.iter().enumerate() {
        if in_str {
            out.push(c);
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
            continue;
        }
        if c == '"' {
            in_str = true;
        }
        if c == ',' && b[i + 1..].iter().find(|x| !x.is_whitespace()).is_some_and(|x| *x == '}' || *x == ']') {
            continue;
        }
        out.push(c);
    }
    out
}

impl Settings {
    pub fn load() -> Settings {
        Settings::load_from(&config_dir().join("settings.json"),
                            &home().join(".config").join("basilisk").join("settings.json"))
    }

    /// Read `path`; when it has no key, borrow Basilisk's (as the Python app does).
    pub fn load_from(path: &Path, basilisk: &Path) -> Settings {
        let mut s = Settings { path: path.to_path_buf(), ..Settings::default() };
        if let Ok(raw) = std::fs::read_to_string(path) {
            match parse(&raw) {
                Some(m) => s.map = m,
                None => {
                    let ts = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
                        .map(|d| d.as_secs()).unwrap_or(0);
                    let bad = path.with_file_name(format!("settings.json.bad-{ts}"));
                    if write_private(&bad, raw.as_bytes()).is_ok() {
                        s.problem = Some(format!("settings.json couldn't be read — a copy is at {}; starting \
                            from defaults", bad.display()));
                    }
                }
            }
        }
        if s.api_key().is_none() {
            if let Some(k) = std::fs::read_to_string(basilisk).ok().and_then(|r| parse(&r))
                .and_then(|m| m.get("siliconflow_api_key").and_then(Value::as_str).map(str::to_string))
                .filter(|k| !k.trim().is_empty())
            {
                s.map.insert("siliconflow_api_key".into(), Value::String(k));
            }
        }
        s
    }

    pub fn str(&self, key: &str) -> Option<&str> {
        self.map.get(key).and_then(Value::as_str).map(str::trim).filter(|v| !v.is_empty())
    }

    /// A number in [lo, hi]; the default when missing or not a number.
    pub fn num(&self, key: &str, default: f64, lo: f64, hi: f64) -> f64 {
        let v = match self.map.get(key) {
            Some(Value::Number(n)) => n.as_f64(),
            Some(Value::String(s)) => s.trim().parse().ok(),
            _ => None,
        };
        v.filter(|x| x.is_finite()).map_or(default, |x| x.clamp(lo, hi))
    }

    pub fn api_key(&self) -> Option<&str> {
        self.str("siliconflow_api_key")
    }

    pub fn set(&mut self, key: &str, v: Value) {
        self.map.insert(key.into(), v);
    }

    /// Save 0600, atomically: the file holds the API key.
    pub fn save(&self) -> Result<(), SettingsError> {
        let text = serde_json::to_string_pretty(&Value::Object(self.map.clone())).unwrap_or_else(|_| "{}".into());
        if let Some(dir) = self.path.parent() {
            let _ = std::fs::create_dir_all(dir);
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700));
            }
        }
        let tmp = self.path.with_extension("tmp");
        write_private(&tmp, text.as_bytes()).map_err(|e| SettingsError::Write(tmp.clone(), e))?;
        std::fs::rename(&tmp, &self.path).map_err(|e| {
            let _ = std::fs::remove_file(&tmp);
            SettingsError::Write(self.path.clone(), e)
        })
    }
}

/// Create (or truncate) a file that only its owner can read, from the first byte.
pub fn write_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    let mut o = std::fs::OpenOptions::new();
    o.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o600);
    }
    let mut f = o.open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        f.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    f.write_all(data)?;
    f.sync_all()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("chuck-settings-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn reads_forgives_and_saves_private() {
        let d = dir("rw");
        let p = d.join("settings.json");
        std::fs::write(&p, "{\"siliconflow_api_key\": \"sk-1\", \"reasoning_budget\": 99999, \"x\": [1,2,],}").unwrap();
        let mut s = Settings::load_from(&p, &d.join("none.json"));
        assert_eq!(s.api_key(), Some("sk-1"));
        assert_eq!(s.num("reasoning_budget", 4096.0, 0.0, 32768.0), 32768.0);
        assert_eq!(s.num("missing", 120.0, 0.0, 1800.0), 120.0);
        s.set("theme", Value::String("dark".into()));
        s.save().unwrap();
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o600);
        let again = Settings::load_from(&p, &d.join("none.json"));
        assert_eq!(again.str("theme"), Some("dark"));
        assert_eq!(again.map["x"], serde_json::json!([1, 2]), "unknown keys are kept");
    }

    #[test]
    fn a_broken_file_is_kept_and_basilisk_lends_its_key() {
        let d = dir("bad");
        let p = d.join("settings.json");
        std::fs::write(&p, "[not an object").unwrap();
        let b = d.join("basilisk.json");
        std::fs::write(&b, "{\"siliconflow_api_key\": \"sk-b\"}").unwrap();
        let s = Settings::load_from(&p, &b);
        assert!(s.problem.is_some());
        assert_eq!(s.api_key(), Some("sk-b"));
        assert!(std::fs::read_dir(&d).unwrap().flatten()
            .any(|e| e.file_name().to_string_lossy().starts_with("settings.json.bad-")));
        assert_eq!(regex_lite_trailing_commas("{\"a\": \"x,}\", }"), "{\"a\": \"x,}\" }");
    }
}
