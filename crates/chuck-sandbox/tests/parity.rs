//! The Rust classifier must say exactly what the Python one says, on every
//! command the Python tests use and a generated corpus around them.
//! parity.json was generated from the Python classifier when the app was
//! ported; it is the golden record of what every command must classify as.

use chuck_sandbox::{add_noconfirm, classify_code, classify_command, enforce_syu};

#[test]
fn same_verdicts_and_rewrites_as_python() {
    let data: serde_json::Value = serde_json::from_str(include_str!("parity.json")).unwrap();
    let mut bad = Vec::new();
    let rows = data["commands"].as_array().unwrap();
    for r in rows {
        let cmd = r["cmd"].as_str().unwrap();
        let level = classify_command(cmd).label();
        if level != r["level"].as_str().unwrap() {
            bad.push(format!("classify {cmd:?}: rust {level:?}, python {:?}", r["level"]));
        }
        let syu = enforce_syu(cmd);
        if syu != r["syu"].as_str().unwrap() {
            bad.push(format!("enforce_syu {cmd:?}: rust {syu:?}, python {:?}", r["syu"]));
        }
        let nc = add_noconfirm(cmd);
        if nc != r["noconfirm"].as_str().unwrap() {
            bad.push(format!("add_noconfirm {cmd:?}: rust {nc:?}, python {:?}", r["noconfirm"]));
        }
    }
    for r in data["code"].as_array().unwrap() {
        let code = r["code"].as_str().unwrap();
        let level = classify_code(code).label();
        if level != r["level"].as_str().unwrap() {
            bad.push(format!("classify_code {code:?}: rust {level:?}, python {:?}", r["level"]));
        }
    }
    assert!(rows.len() > 1000);
    assert!(bad.is_empty(), "{} differences:\n{}", bad.len(), bad.join("\n"));
}
