//! The cleaner and the chunker say exactly what the Python app says
//! (tests/golden.json was made by running its tts_clean and tts_chunks).

#[test]
fn the_same_words_as_the_python_app() {
    let cases: serde_json::Value = serde_json::from_str(include_str!("golden.json")).unwrap();
    let mut bad = Vec::new();
    for c in cases.as_array().unwrap() {
        let input = c["in"].as_str().unwrap();
        let strs = |v: &serde_json::Value| v.as_array().unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect::<Vec<_>>();
        if let Some(size) = c.get("size").and_then(|s| s.as_u64()) {
            let got = chuck_voice::clean::chunks(input, size as usize, c["first"].as_u64().map(|f| f as usize));
            if got != strs(&c["chunks"]) {
                bad.push(format!("chunks({input:?}):\n  got  {got:?}\n  want {:?}", strs(&c["chunks"])));
            }
            continue;
        }
        let clean = chuck_voice::clean::clean(input);
        if clean != c["clean"].as_str().unwrap() {
            bad.push(format!("clean({input:?}):\n  got  {clean:?}\n  want {:?}", c["clean"].as_str().unwrap()));
            continue;
        }
        let got = chuck_voice::clean::chunks(&clean, 260, None);
        if got != strs(&c["chunks"]) {
            bad.push(format!("chunks of {clean:?}:\n  got  {got:?}\n  want {:?}", strs(&c["chunks"])));
        }
        let got = chuck_voice::clean::chunks(&clean, 420, Some(60));
        if got != strs(&c["kokoro"]) {
            bad.push(format!("kokoro chunks of {clean:?}:\n  got  {got:?}\n  want {:?}", strs(&c["kokoro"])));
        }
    }
    assert!(bad.is_empty(), "{} differences:\n{}", bad.len(), bad.join("\n"));
}
