//! When a daemon wakes. Triggers are kept as the Python app writes them (so
//! both read the same daemons.json):
//!
//! ```text
//! in 20m / at 17:30 / tomorrow at 9am     {"at": epoch}
//! every 30m / every 2h / every day        {"every": seconds}
//! daily 08:30 / every day at 8:30         {"daily": "08:30"}
//! when ~/proj/src changes                 {"watch": path}
//! when a service fails                    {"unit_failed": true}
//! when disk / is above 90%                {"disk": "/", "above": 90}
//! when `checkupdates` changes             {"command": "...", "changes": true}
//! ```
//!
//! Checking one is free: plain code looks at the clock, a file's mtime,
//! the failed units, the disk, a command's output. Only a trigger that
//! fires wakes an agent.

use serde_json::{json, Map, Value};

pub const COMMAND_POLL: f64 = 300.0;

const UNITS: &[(&str, u64)] = &[("s", 1), ("sec", 1), ("m", 60), ("min", 60), ("h", 3600), ("hr", 3600), ("hour", 3600),
    ("d", 86400), ("day", 86400), ("w", 604800), ("week", 604800)];

fn unit(u: &str) -> Option<u64> {
    let u = u.strip_suffix('s').filter(|x| UNITS.iter().any(|(n, _)| n == x)).unwrap_or(u);
    UNITS.iter().find(|(n, _)| *n == u).map(|(_, s)| *s)
}

const SAY_WHEN: &str = "say when: 'in 20m', 'at 17:30', 'tomorrow at 9am', 'every 30m', 'daily 08:30', \
    'when ~/src changes', 'when a service fails', 'when disk / is above 90%' or 'when `cmd` changes'";

// ── local time ──────────────────────────────────────────────────────────

pub fn localtime(t: f64) -> libc::tm {
    let tt = t as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    unsafe {
        libc::localtime_r(&tt, &mut tm);
    }
    tm
}

/// Local midnight-relative time built from the calendar (so the day the
/// clocks change is still one day).
pub fn local_at(year: i32, mon0: i32, mday: i32, h: i32, mi: i32) -> f64 {
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    tm.tm_year = year - 1900;
    tm.tm_mon = mon0;
    tm.tm_mday = mday;
    tm.tm_hour = h;
    tm.tm_min = mi;
    tm.tm_isdst = -1;
    unsafe { libc::mktime(&mut tm) as f64 }
}

fn at_day(offset: i32, h: i32, mi: i32, now: f64) -> f64 {
    let lt = localtime(now);
    local_at(lt.tm_year + 1900, lt.tm_mon, lt.tm_mday + offset, h, mi)
}

fn clock(h: &str, mi: Option<&str>, ampm: Option<&str>) -> Result<(i32, i32), String> {
    let (mut h, mi): (i32, i32) = (h.parse().map_err(|_| "that time doesn't exist")?,
                                    mi.map_or(Ok(0), |m| m.parse()).map_err(|_| "that time doesn't exist")?);
    match ampm {
        Some("pm") if h < 12 => h += 12,
        Some("am") if h == 12 => h = 0,
        _ => {}
    }
    if h > 23 || mi > 59 {
        return Err("that time doesn't exist".into());
    }
    Ok((h, mi))
}

fn expand(p: &str) -> String {
    match (p.strip_prefix('~'), std::env::var("HOME")) {
        (Some(rest), Ok(home)) if rest.is_empty() || rest.starts_with('/') => format!("{home}{rest}"),
        _ => p.to_string(),
    }
}

/// What people write -> a trigger. Err: what to write instead.
pub fn parse_when(text: &str, now: f64) -> Result<Value, String> {
    let raw = text.trim();
    let t = raw.to_lowercase();
    let re = |p: &str| regex::Regex::new(p).unwrap();
    if let Some(c) = re(r"^in\s+(\d+)\s*([a-z]+)$").captures(&t) {
        if let Some(u) = unit(&c[2]) {
            return Ok(json!({"at": now + c[1].parse::<f64>().unwrap_or(0.0) * u as f64}));
        }
    }
    if let Some(c) = re(r"^(?:(today|tomorrow)\s+)?(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*(am|pm)?$").captures(&t) {
        if c.get(1).is_some() || t.starts_with("at ") || c.get(3).is_some() || c.get(4).is_some() {
            let (h, mi) = clock(&c[2], c.get(3).map(|m| m.as_str()), c.get(4).map(|m| m.as_str()))?;
            let day = c.get(1).map(|m| m.as_str());
            let mut at = at_day(i32::from(day == Some("tomorrow")), h, mi, now);
            if at <= now && day != Some("today") {
                at = at_day(1, h, mi, now); // "at 9am" said at 10am means tomorrow
            }
            return Ok(json!({"at": at}));
        }
    }
    if let Some(c) = re(r"^(?:every\s+day\s+at|daily(?:\s+at)?|each\s+day\s+at)\s+(\d{1,2})(?::(\d\d))?\s*(am|pm)?$").captures(&t) {
        let (h, mi) = clock(&c[1], c.get(2).map(|m| m.as_str()), c.get(3).map(|m| m.as_str()))?;
        return Ok(json!({"daily": format!("{h:02}:{mi:02}")}));
    }
    if let Some(c) = re(r"^every\s+(\d+)?\s*([a-z]+)$").captures(&t) {
        if let Some(u) = unit(&c[2]) {
            let n = c.get(1).map_or(1, |m| m.as_str().parse().unwrap_or(1)) * u;
            if n < 60 {
                return Err("the shortest interval is a minute".into());
            }
            return Ok(json!({"every": n}));
        }
    }
    if let Some(c) = re(r"^when\s+(?:the\s+)?disk\s+(?:([/~]\S*)\s+)?(?:is\s+)?(?:above|over|>)\s*(\d{1,3})\s*%?$").captures(&t) {
        let above: u64 = c[2].parse::<u64>().unwrap_or(90).clamp(1, 99);
        return Ok(json!({"disk": expand(c.get(1).map_or("/", |m| m.as_str())), "above": above}));
    }
    if re(r"^when\s+(?:a|any)\s+(?:service|unit)\s+fails$").is_match(&t) {
        return Ok(json!({"unit_failed": true}));
    }
    if let Some(c) = re(r"(?i)^when\s+`([^`]+)`\s+changes$").captures(raw) {
        return Ok(json!({"command": c[1].trim(), "changes": true}));
    }
    if let Some(c) = re(r"(?i)^when\s+(.+?)\s+changes$").captures(raw) {
        return Ok(json!({"watch": expand(c[1].trim().trim_matches(['\'', '"']))}));
    }
    Err(SAY_WHEN.into())
}

pub fn describe(trig: &Value) -> String {
    if let Some(n) = trig.get("every").and_then(Value::as_u64) {
        for (name, sec) in [("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)] {
            if n % sec == 0 {
                let k = n / sec;
                return if k == 1 { format!("every {name}") } else { format!("every {k} {name}s") };
            }
        }
        return format!("every {n}s");
    }
    if let Some(d) = trig.get("daily").and_then(Value::as_str) {
        return format!("daily at {d}");
    }
    if let Some(at) = trig.get("at").and_then(Value::as_f64) {
        return format!("at {}", chuck_store::ledger::stamp_at(at as i64, "%a %d %b %H:%M"));
    }
    if let Some(w) = trig.get("watch").and_then(Value::as_str) {
        return format!("when {w} changes");
    }
    if trig.get("unit_failed").and_then(Value::as_bool) == Some(true) {
        return "when a service fails".into();
    }
    if let Some(d) = trig.get("disk").and_then(Value::as_str) {
        return format!("when disk {d} is above {}%", trig.get("above").and_then(Value::as_u64).unwrap_or(90));
    }
    if let Some(c) = trig.get("command").and_then(Value::as_str) {
        return format!("when `{c}` changes");
    }
    "?".into()
}

/// What a trigger looks at, outside plain code: the probes are passed in,
/// so every trigger is tested without a clock or a system.
pub trait Probe {
    /// A cheap fingerprint of a file or folder ("missing" when it isn't there).
    fn signature(&self, path: &str) -> String;
    fn failed_units(&self) -> Vec<String>;
    fn disk_percent(&self, path: &str) -> Option<f64>;
    fn run(&self, cmd: &str) -> String;
}

fn sha1(s: &str) -> String {
    chuck_store::checkpoint::sha1_hex(s.as_bytes())
}

/// Is this daemon due? -> (due, why, its new state). No model call.
pub fn check(when: &Value, state: &Value, last_run: f64, now: f64, probe: &dyn Probe) -> (bool, String, Value) {
    let mut st: Map<String, Value> = state.as_object().cloned().unwrap_or_default();
    if let Some(n) = when.get("every").and_then(Value::as_f64) {
        return (now - last_run >= n, describe(when), Value::Object(st));
    }
    if let Some(at) = when.get("at").and_then(Value::as_f64) {
        return (now >= at && last_run < at, "it's time".into(), Value::Object(st));
    }
    if let Some(d) = when.get("daily").and_then(Value::as_str) {
        let (hh, mm) = d.split_once(':').map_or((0, 0), |(h, m)| (h.parse().unwrap_or(0), m.parse().unwrap_or(0)));
        let lt = localtime(now);
        // today's hh:mm from the calendar, not "now minus the wall-clock
        // difference": on the day the clocks go back that fired twice
        let target = local_at(lt.tm_year + 1900, lt.tm_mon, lt.tm_mday, hh, mm);
        return (now >= target && last_run < target, format!("it's {d}"), Value::Object(st));
    }
    if let Some(w) = when.get("watch").and_then(Value::as_str) {
        let sig = probe.signature(w);
        let prev = st.insert("sig".into(), json!(sig));
        return (prev.is_some_and(|p| p != json!(sig)), format!("{w} changed"), Value::Object(st));
    }
    if when.get("unit_failed").and_then(Value::as_bool) == Some(true) {
        let mut units = probe.failed_units();
        units.sort();
        let prev: Option<Vec<String>> = st.get("units").and_then(|v| serde_json::from_value(v.clone()).ok());
        st.insert("units".into(), json!(units));
        let new: Vec<&String> = units.iter().filter(|u| !prev.as_ref().is_some_and(|p| p.contains(u))).collect();
        let why = format!("failed: {}", new.iter().take(5).map(|s| s.as_str()).collect::<Vec<_>>().join(", "));
        return (prev.is_some() && !new.is_empty(), why, Value::Object(st));
    }
    if let Some(d) = when.get("disk").and_then(Value::as_str) {
        let above = when.get("above").and_then(Value::as_f64).unwrap_or(90.0);
        let pct = probe.disk_percent(d);
        let was = st.get("above").and_then(Value::as_bool).unwrap_or(false);
        let now_above = pct.is_some_and(|p| p > above);
        st.insert("above".into(), json!(now_above));
        // edge-triggered, and again every 6 hours while it stays above
        let due = now_above && (!was || now - last_run >= 6.0 * 3600.0);
        return (due, pct.map(|p| format!("disk {d} is {p:.0}% full")).unwrap_or_default(), Value::Object(st));
    }
    if let Some(c) = when.get("command").and_then(Value::as_str) {
        // a command (checkupdates asks the mirrors) runs every few minutes, not every tick
        let poll = when.get("poll").and_then(Value::as_f64).unwrap_or(COMMAND_POLL);
        if now - st.get("polled").and_then(Value::as_f64).unwrap_or(0.0) < poll {
            return (false, String::new(), Value::Object(st));
        }
        st.insert("polled".into(), json!(now));
        let out = probe.run(c);
        let h = sha1(&out);
        let prev = st.insert("hash".into(), json!(h));
        let tail: String = out.chars().rev().take(2000).collect::<Vec<_>>().into_iter().rev().collect();
        st.insert("out".into(), json!(tail));
        return (prev.is_some_and(|p| p != json!(h)), format!("`{c}` output changed"), Value::Object(st));
    }
    (false, String::new(), Value::Object(st))
}

/// What the trigger saw, for the woken agent (no need to look again).
pub fn detail(when: &Value, state: &Value) -> String {
    if let (Some(c), Some(out)) = (when.get("command").and_then(Value::as_str), state.get("out").and_then(Value::as_str)) {
        return format!("`{c}` now prints:\n{out}");
    }
    if when.get("unit_failed").and_then(Value::as_bool) == Some(true) {
        if let Some(units) = state.get("units").and_then(Value::as_array).filter(|u| !u.is_empty()) {
            return format!("Failed units now: {}", units.iter().take(20).filter_map(Value::as_str).collect::<Vec<_>>().join(", "));
        }
    }
    String::new()
}

/// The trigger's command, when it has one (it must pass the read-only check).
pub fn command(when: &Value) -> Option<&str> {
    when.get("command").and_then(Value::as_str)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    #[derive(Default)]
    struct Fake {
        sigs: RefCell<std::collections::HashMap<String, String>>,
        units: RefCell<Vec<String>>,
        disk: RefCell<f64>,
        out: RefCell<String>,
        ran: RefCell<Vec<String>>,
    }

    impl Probe for Fake {
        fn signature(&self, p: &str) -> String {
            self.sigs.borrow().get(p).cloned().unwrap_or_else(|| "s0".into())
        }
        fn failed_units(&self) -> Vec<String> {
            self.units.borrow().clone()
        }
        fn disk_percent(&self, _: &str) -> Option<f64> {
            Some(*self.disk.borrow())
        }
        fn run(&self, cmd: &str) -> String {
            self.ran.borrow_mut().push(cmd.into());
            self.out.borrow().clone()
        }
    }

    #[test]
    fn what_people_write() {
        let now = local_at(2026, 2, 10, 10, 0); // Tue 10 Mar 2026, 10:00
        assert_eq!(parse_when("every 30m", now).unwrap(), json!({"every": 1800}));
        assert_eq!(parse_when("every 2 hours", now).unwrap(), json!({"every": 7200}));
        assert_eq!(parse_when("every day", now).unwrap(), json!({"every": 86400}));
        assert_eq!(parse_when("daily 08:30", now).unwrap(), json!({"daily": "08:30"}));
        assert_eq!(parse_when("every day at 7pm", now).unwrap(), json!({"daily": "19:00"}));
        assert_eq!(parse_when("daily at 12am", now).unwrap(), json!({"daily": "00:00"}));
        assert_eq!(parse_when("when a service fails", now).unwrap(), json!({"unit_failed": true}));
        assert_eq!(parse_when("when disk / is above 90%", now).unwrap(), json!({"disk": "/", "above": 90}));
        assert_eq!(parse_when("when disk is above 90%", now).unwrap(), json!({"disk": "/", "above": 90}));
        assert_eq!(parse_when("when disk /home is above 80%", now).unwrap(), json!({"disk": "/home", "above": 80}));
        assert_eq!(parse_when("when `checkupdates` changes", now).unwrap(), json!({"command": "checkupdates", "changes": true}));
        assert_eq!(parse_when("when /etc/fstab changes", now).unwrap(), json!({"watch": "/etc/fstab"}));
        assert_eq!(parse_when("in 20m", now).unwrap(), json!({"at": now + 1200.0}));
        assert_eq!(describe(&parse_when("at 9am", now).unwrap()), "at Wed 11 Mar 09:00", "9am has passed: tomorrow");
        assert_eq!(describe(&parse_when("tomorrow at 9:15", now).unwrap()), "at Wed 11 Mar 09:15");
        assert_eq!(describe(&parse_when("at 5pm", now).unwrap()), "at Tue 10 Mar 17:00");
        for bad in ["every 10s", "daily 25:00", "sometime", ""] {
            let e = parse_when(bad, now).unwrap_err();
            assert!(e.contains("say when") || e.contains("minute") || e.contains("exist"), "{bad}: {e}");
        }
        assert_eq!(describe(&json!({"every": 7200})), "every 2 hours");
        assert_eq!(describe(&json!({"every": 86400})), "every day");
    }

    #[test]
    fn triggers_checked_for_free() {
        let p = Fake::default();
        let every = json!({"every": 3600});
        assert!(!check(&every, &json!({}), 1000.0, 4599.0, &p).0);
        assert!(check(&every, &json!({}), 1000.0, 4600.0, &p).0);

        let midnight = local_at(2026, 0, 2, 0, 0);
        let daily = json!({"daily": "08:30"});
        assert!(!check(&daily, &json!({}), midnight - 86400.0, midnight + 8.0 * 3600.0 + 29.0 * 60.0, &p).0);
        assert!(check(&daily, &json!({}), midnight - 86400.0, midnight + 8.0 * 3600.0 + 31.0 * 60.0, &p).0);
        assert!(!check(&daily, &json!({}), midnight + 8.0 * 3600.0 + 31.0 * 60.0, midnight + 20.0 * 3600.0, &p).0,
                "not again the same day");
        assert!(check(&daily, &json!({}), midnight - 86400.0, midnight + 15.0 * 3600.0, &p).0,
                "a run missed while the computer was off happens when it's back");

        let watch = json!({"watch": "/x"});
        let (due, _, st) = check(&watch, &json!({}), 0.0, 0.0, &p);
        assert!(!due && st["sig"] == "s0", "the first look records, doesn't fire");
        assert!(!check(&watch, &st, 0.0, 0.0, &p).0);
        p.sigs.borrow_mut().insert("/x".into(), "s1".into());
        assert!(check(&watch, &st, 0.0, 0.0, &p).0);

        let units = json!({"unit_failed": true});
        let (_, _, st) = check(&units, &json!({}), 0.0, 0.0, &p);
        p.units.borrow_mut().push("nginx.service".into());
        let (due, why, st) = check(&units, &st, 0.0, 0.0, &p);
        assert!(due && why.contains("nginx.service"));
        assert!(!check(&units, &st, 0.0, 0.0, &p).0, "the same failure doesn't fire twice");
        assert_eq!(detail(&units, &st), "Failed units now: nginx.service");

        let disk = json!({"disk": "/", "above": 90});
        *p.disk.borrow_mut() = 95.0;
        let (due, why, st) = check(&disk, &json!({}), 0.0, 100.0, &p);
        assert!(due && why.contains("95%"), "{why}");
        assert!(!check(&disk, &st, 100.0, 200.0, &p).0);
        assert!(check(&disk, &st, 100.0, 100.0 + 6.0 * 3600.0, &p).0, "again every 6 hours while it stays full");

        let cmd = json!({"command": "checkupdates", "changes": true});
        *p.out.borrow_mut() = "linux 6.1 -> 6.2".into();
        let (_, _, st) = check(&cmd, &json!({}), 0.0, 1000.0, &p);
        *p.out.borrow_mut() = "linux 6.1 -> 6.3".into();
        p.ran.borrow_mut().clear();
        assert!(!check(&cmd, &st, 0.0, 1100.0, &p).0 && p.ran.borrow().is_empty(), "not run at every tick");
        let (due, _, st) = check(&cmd, &st, 0.0, 1300.0, &p);
        assert!(due);
        assert!(detail(&cmd, &st).contains("6.3"));
    }
}
