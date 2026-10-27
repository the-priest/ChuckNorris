//! What the whole OS may spend: a daily and a monthly limit across every
//! chat, agent and job, booked by local day in the window's own file
//! (~/.local/share/chucknorris/spend.json), so the limits hold across the
//! window, `chuck`, and restarts. Raising a limit is the user's call
//! (`allow_more`); nothing here raises one on its own.

use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub fn day_of(secs: i64) -> String {
    crate::ledger::stamp_at(secs, "%Y-%m-%d")
}

fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

#[derive(Default)]
struct Book {
    days: BTreeMap<String, f64>,
    extra: BTreeMap<String, f64>,
}

pub struct Spend {
    path: PathBuf,
    book: Mutex<Book>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Over {
    /// "day" or "month".
    pub scope: &'static str,
    pub limit: f64,
    pub spent: f64,
}

fn clean(v: Option<&Value>) -> BTreeMap<String, f64> {
    v.and_then(Value::as_object).map(|m| m.iter()
        .filter_map(|(k, v)| v.as_f64().filter(|x| *x >= 0.0 && x.is_finite()).map(|x| (k.clone(), x))).collect())
        .unwrap_or_default()
}

impl Spend {
    pub fn default_path() -> PathBuf {
        crate::ledger::data_dir().join("spend.json")
    }

    pub fn at(path: &Path) -> Spend {
        let s = Spend { path: path.to_path_buf(), book: Mutex::new(Book::default()) };
        s.reload();
        s
    }

    /// Read the file again: the window books its spend there too.
    fn reload(&self) {
        let data: Value = std::fs::read_to_string(&self.path).ok()
            .and_then(|t| serde_json::from_str(&t).ok()).unwrap_or(Value::Null);
        let mut b = self.book.lock().unwrap();
        b.days = clean(data.get("days"));
        b.extra = clean(data.get("extra"));
    }

    /// Book a model call's cost (read-modify-write, so the window's own
    /// bookings in between aren't lost).
    pub fn add(&self, usd: f64) {
        self.add_at(usd, now());
    }

    pub fn add_at(&self, usd: f64, at: i64) {
        if !(usd > 0.0 && usd.is_finite()) {
            return;
        }
        self.reload();
        {
            let mut b = self.book.lock().unwrap();
            *b.days.entry(day_of(at)).or_insert(0.0) += usd;
        }
        self.save(at);
    }

    pub fn today(&self, at: i64) -> f64 {
        self.book.lock().unwrap().days.get(&day_of(at)).copied().unwrap_or(0.0).max(0.0)
    }

    pub fn month(&self, at: i64) -> f64 {
        let m = day_of(at)[..7].to_string();
        // an empty sum of f64 is -0.0, which prints as "$-0.0000"
        self.book.lock().unwrap().days.iter().filter(|(d, _)| d.starts_with(&m)).map(|(_, v)| v).sum::<f64>().max(0.0)
    }

    /// A limit reached (with the user's allowances on top), or None. A
    /// limit of 0 means none.
    pub fn over(&self, day_limit: f64, month_limit: f64) -> Option<Over> {
        self.reload();
        self.over_at(day_limit, month_limit, now())
    }

    pub fn over_at(&self, day_limit: f64, month_limit: f64, at: i64) -> Option<Over> {
        let d = day_of(at);
        let extra = |k: &str| self.book.lock().unwrap().extra.get(k).copied().unwrap_or(0.0);
        if day_limit > 0.0 {
            let lim = day_limit + extra(&d);
            let spent = self.today(at);
            if spent >= lim {
                return Some(Over { scope: "day", limit: lim, spent });
            }
        }
        if month_limit > 0.0 {
            let lim = month_limit + extra(&d[..7]);
            let spent = self.month(at);
            if spent >= lim {
                return Some(Over { scope: "month", limit: lim, spent });
            }
        }
        None
    }

    /// Money left before a limit, or None when there is no limit.
    pub fn left(&self, day_limit: f64, month_limit: f64) -> Option<f64> {
        self.reload();
        let at = now();
        let d = day_of(at);
        let b = self.book.lock().unwrap();
        let mut best: Option<f64> = None;
        if day_limit > 0.0 {
            let lim = day_limit + b.extra.get(&d).copied().unwrap_or(0.0);
            best = Some(lim - b.days.get(&d).copied().unwrap_or(0.0));
        }
        if month_limit > 0.0 {
            let m = &d[..7];
            let lim = month_limit + b.extra.get(m).copied().unwrap_or(0.0);
            let spent: f64 = b.days.iter().filter(|(k, _)| k.starts_with(m)).map(|(_, v)| v).sum();
            best = Some(best.map_or(lim - spent, |x| x.min(lim - spent)));
        }
        best.map(|x| x.max(0.0))
    }

    fn save(&self, at: i64) {
        let cutoff = day_of(at - 400 * 86_400);
        let data = {
            let mut b = self.book.lock().unwrap();
            b.days.retain(|d, _| *d >= cutoff);
            let cm = cutoff[..7].to_string();
            b.extra.retain(|d, _| *d >= cm);
            let to = |m: &BTreeMap<String, f64>| Value::Object(m.iter().map(|(k, v)| (k.clone(), json!(v)))
                .collect::<Map<String, Value>>());
            json!({"days": to(&b.days), "extra": to(&b.extra)})
        };
        if let Some(p) = self.path.parent() {
            let _ = std::fs::create_dir_all(p);
        }
        let tmp = self.path.with_extension("tmp");
        if std::fs::write(&tmp, data.to_string()).is_ok() {
            let _ = std::fs::rename(&tmp, &self.path);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn books_by_day_and_month_and_respects_limits() {
        let d = std::env::temp_dir().join(format!("chuck-spend-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        let p = d.join("spend.json");
        let s = Spend::at(&p);
        let t = 1_760_000_000; // a fixed moment
        s.add_at(0.40, t);
        s.add_at(0.70, t);
        s.add_at(-5.0, t); // nonsense is ignored
        assert!((s.today(t) - 1.10).abs() < 1e-9);
        assert_eq!(s.over_at(1.0, 0.0, t).unwrap().scope, "day");
        assert_eq!(s.over_at(2.0, 0.0, t), None);
        assert_eq!(s.over_at(0.0, 1.0, t).unwrap().scope, "month");
        // the window's file format survives a round trip
        let again = Spend::at(&p);
        assert!((again.today(t) - 1.10).abs() < 1e-9);
        let raw: Value = serde_json::from_str(&std::fs::read_to_string(&p).unwrap()).unwrap();
        assert!(raw["days"].as_object().unwrap().contains_key(&day_of(t)));
        let _ = std::fs::remove_dir_all(&d);
    }
}
